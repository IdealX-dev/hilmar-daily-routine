"""
gen_insights.py — Daily insights shim: baselines + rule-based context + LLM narrative.

Wires the already-built M3.10–M3.12 modules (src/hilmar/{baselines,insights,
model_router}.py — see docs/INSIGHTS-DESIGN.md) into the scripts/ production
pipeline. Runs BEFORE "Email body HTML" in run_pipeline.py because gen_email
embeds this step's output.

Flow per fire:
  1. Update rolling baselines (M3.10) from tracking-data-v2.json and graft
     them into the in-memory tracking dict so build_context sees them.
     baselines.json lives next to the tracking data (same convention as
     src/hilmar/orchestrator.step_baselines).
  2. insights.build_context (M3.11.a — rule-based, no LLM).
  3. insights.generate_narrative via the ModelRouter (M3.12 — Opus default,
     env-dial-down, 429 cascade). Skipped cleanly when ANTHROPIC_API_KEY is
     absent or any LLM-path exception fires: the rule-based context still
     ships, each narrative section carries a skipped_reason.
  4. Print the day's LLM spend from the router's cost telemetry; loud WARN
     when it exceeds HILMAR_INSIGHTS_COST_ALERT_CENTS (default 200 = $2 —
     per INSIGHTS-DESIGN.md, alert only, never halt).

Produces:
  reports/insights/<YYYY-MM-DD>.json   — context + narrative (structured)
  reports/insights/<YYYY-MM-DD>.html   — all four sections (archive copy)
  reports/insights-business.html       — Business-only snippet (staff email embed)
  reports/insights-full.html           — all four sections (idealx.us audit embed)

ALWAYS exits 0 — this step must never block the client fire (it is also
classified BEST_EFFORT in run_pipeline.py as a second layer of the same
guarantee). Tests: tests/test_gen_insights_wiring.py (LLM always mocked).

Usage:
  python3 scripts/gen_insights.py [--config config.json]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
# The insights engine lives in the src/hilmar package (not scripts/) — same
# import pattern as qc_actions_from_sentry.py and tests/conftest.py.
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

import core  # noqa: E402

from hilmar import baselines as baselines_mod  # noqa: E402
from hilmar import feedback_ingest as fb_mod  # noqa: E402
from hilmar import insights as insights_mod  # noqa: E402
from hilmar.model_router import ModelResponse, ModelRouter  # noqa: E402

#: bundle attribute → router task type, in render order. Mirrors
#: insights.generate_narrative so the skipped-bundle path stays in lockstep.
SECTION_TASKS = {
    "system": "system_critique",
    "design": "design_suggestions",
    "data": "data_suggestions",
    "business": "business_advice",
}


def _skipped_bundle(router: ModelRouter, reason: str) -> insights_mod.NarrativeBundle:
    """A NarrativeBundle whose four sections are all marked skipped — the
    render path then emits "(LLM-narrative skipped: <reason>)" per section,
    exactly the INSIGHTS-DESIGN.md "API down" degradation."""
    def _resp(section: str) -> ModelResponse:
        task = SECTION_TASKS[section]
        return ModelResponse(
            text="", model=router.select(task), task_type=task,
            input_tokens=0, output_tokens=0, cost_cents=0,
            skipped_reason=reason,
        )
    return insights_mod.NarrativeBundle(
        system=_resp("system"), design=_resp("design"),
        data=_resp("data"), business=_resp("business"),
    )


def _generate_bundle(
    ctx: insights_mod.InsightsContext,
    router: ModelRouter,
    data_dir: Path,
    now: datetime,
) -> insights_mod.NarrativeBundle:
    """Run the four narrative calls, degrading to a skipped bundle on ANY
    failure. The missing-key check happens BEFORE the router touches the
    anthropic client — client construction raises without a key and that
    error isn't part of the router's own 429/connection cascade."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("⚠️  ANTHROPIC_API_KEY not set — LLM narrative skipped, rule-based context only")
        return _skipped_bundle(router, "missing ANTHROPIC_API_KEY")
    try:
        feedback_summary = fb_mod.load_feedback_summary(
            data_dir / "insights-feedback.json", now=now,
        ) or None
    except Exception:  # noqa: BLE001 — feedback is bias-only, never load-bearing
        feedback_summary = None
    try:
        return insights_mod.generate_narrative(
            ctx, router=router, feedback_summary=feedback_summary,
        )
    except Exception as e:  # noqa: BLE001 — LLM failure ships rule-based output
        print(f"⚠️  LLM narrative failed ({type(e).__name__}: {e}) — shipping rule-based context only")
        return _skipped_bundle(router, f"{type(e).__name__}: {e}")


def _report_cost(router: ModelRouter) -> None:
    """Print today's spend from the router's cost telemetry. Loud WARN above
    the alert threshold — inform, never halt (INSIGHTS-DESIGN.md M3.12)."""
    cost = router.daily_cost_cents()
    threshold = router.cost_alert_threshold_cents()
    print(f"💰 LLM cost today: {cost}¢ (alert threshold: {threshold}¢)")
    if cost > threshold:
        print(
            f"⚠️⚠️⚠️ WARN: Hilmar Insights spent ${cost / 100:.2f} on the Anthropic API today — "
            f"above the ${threshold / 100:.2f} alert threshold. "
            f"Set HILMAR_INSIGHTS_MODEL=claude-sonnet-4-6 to dial down, or raise "
            f"HILMAR_INSIGHTS_COST_ALERT_CENTS."
        )


def _write_artifacts(
    *,
    reports_dir: Path,
    today_label: str,
    ctx: insights_mod.InsightsContext,
    bundle: insights_mod.NarrativeBundle,
    cost_alert: bool,
) -> None:
    insights_dir = reports_dir / "insights"
    insights_dir.mkdir(parents=True, exist_ok=True)

    json_path = insights_dir / f"{today_label}.json"
    json_path.write_text(
        json.dumps({
            "context": insights_mod.context_to_dict(ctx),
            "narrative": {
                section: {
                    "model": getattr(bundle, section).model,
                    "text": getattr(bundle, section).text,
                    "cost_cents": getattr(bundle, section).cost_cents,
                    "skipped_reason": getattr(bundle, section).skipped_reason,
                } for section in SECTION_TASKS
            },
            "cost_alert": cost_alert,
        }, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    full_html = insights_mod.render_narrative_html(
        bundle, today_label=today_label, sections=insights_mod.INTERNAL_SECTIONS,
    )
    business_html = insights_mod.render_narrative_html(
        bundle, today_label=today_label, sections=insights_mod.STAFF_SECTIONS,
    )

    html_path = insights_dir / f"{today_label}.html"
    html_path.write_text(full_html, encoding="utf-8")
    # Embed snippets: gen_email.py inlines the business-only file into the
    # staff daily (Michael's 2026-04-28 directive — staff see Business only);
    # gen_improvements_report.py inlines the full four-section narrative into
    # the private idealx.us audit. Both embedders freshness-guard on mtime,
    # so these are rewritten every fire.
    (reports_dir / "insights-business.html").write_text(business_html, encoding="utf-8")
    (reports_dir / "insights-full.html").write_text(full_html, encoding="utf-8")

    print(f"✅ Insights JSON:     {json_path}")
    print(f"✅ Insights HTML:     {html_path}")
    print(f"✅ Business snippet:  {reports_dir / 'insights-business.html'}")
    print(f"✅ Full snippet:      {reports_dir / 'insights-full.html'}")


def _run(config_path: str) -> int:
    cfg = core.load_config(config_path)
    paths = cfg.get("paths") or {}
    data_path = Path(paths.get("data") or (ROOT / "tracking-data-v2.json"))
    reports_dir = Path(paths.get("reports") or (ROOT / "reports"))

    if not data_path.exists():
        print(f"⚠️  Insights skipped — tracking data not found: {data_path}")
        return 0

    tracking = json.loads(data_path.read_text(encoding="utf-8"))
    now = datetime.now(timezone.utc)
    today_label = now.strftime("%Y-%m-%d")
    data_dir = data_path.parent

    # 1. Baselines BEFORE insights (INSIGHTS-DESIGN.md M3.10). Persist next to
    #    the tracking data; graft in-memory only — the scripts pipeline's QC
    #    already ran, and this best-effort step must not rewrite tracking data.
    baselines = baselines_mod.update(
        tracking_data=tracking, baselines_path=data_dir / "baselines.json", now=now,
    )
    tracking = baselines_mod.graft_into_tracking_data(tracking, baselines)

    # 2. Rule-based context (no LLM). qc-result + parser-miss log are optional
    #    enrichments — absent files just mean emptier system-health fields.
    qc_result = {}
    qc_path = Path(paths.get("qc_result") or (reports_dir / "qc-result.json"))
    if qc_path.exists():
        try:
            qc_result = json.loads(qc_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            qc_result = {}
    miss_log = data_dir / "parser_misses.jsonl"
    ctx = insights_mod.build_context(
        tracking_data=tracking,
        qc_result=qc_result,
        parser_miss_log_path=miss_log if miss_log.exists() else None,
        now=now,
    )

    # 3. Narrative via the router (defaults untouched — Opus per Michael
    #    2026-04-26; env vars are the only dial). Cost log lives next to the
    #    tracking data unless HILMAR_LLM_COST_LOG overrides.
    cost_log_env = os.environ.get("HILMAR_LLM_COST_LOG")
    router = ModelRouter(
        cost_log_path=Path(cost_log_env) if cost_log_env else data_dir / "llm-cost-log.jsonl",
    )
    bundle = _generate_bundle(ctx, router, data_dir, now)
    cost_alert = router.should_alert_cost()

    # 4. Persist + telemetry.
    _write_artifacts(
        reports_dir=reports_dir, today_label=today_label,
        ctx=ctx, bundle=bundle, cost_alert=cost_alert,
    )
    _report_cost(router)
    if bundle.any_skipped():
        skipped = [s for s in SECTION_TASKS if getattr(bundle, s).skipped_reason]
        print(f"⚠️  Narrative sections skipped: {', '.join(skipped)}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(ROOT / "config.json"))
    args = ap.parse_args(argv)
    # HARD GUARANTEE: exit 0 on every path. Insights are supplemental — a
    # crash here must never abort the pipeline ahead of the email build
    # (belt to run_pipeline's BEST_EFFORT_STEPS suspenders).
    try:
        return _run(args.config)
    except Exception as e:  # noqa: BLE001 — deliberate catch-all at the CLI edge
        print(f"⚠️  Insights step failed ({type(e).__name__}: {e}) — non-blocking, continuing fire")
        return 0


if __name__ == "__main__":
    sys.exit(main())
