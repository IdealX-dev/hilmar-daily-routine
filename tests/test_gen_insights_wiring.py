"""Wiring tests for the insights engine → production pipeline (2026-07-11).

Covers the four integration surfaces added when docs/INSIGHTS-DESIGN.md
M3.10–M3.12 got wired into the scripts/ pipeline:

  * scripts/gen_insights.py — the CLI shim (baselines → context → narrative
    → four artifacts). LLM ALWAYS mocked: generate_narrative is monkey-
    patched, or the shim's own missing-key skip path is exercised —
    no test here may construct a real Anthropic client.
  * scripts/gen_email.py — staff-email embed of insights-business.html
    (fresh-today only; stale/absent/empty/oversize render nothing).
  * scripts/gen_improvements_report.py — audit embed of insights-full.html
    (mirrors the rate-intelligence inline pattern + freshness guard).
  * scripts/run_pipeline.py — step registered before "Email body HTML" and
    classified BEST_EFFORT so it can never block the client fire.
"""
from __future__ import annotations

import copy
import inspect
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import gen_email as ge  # noqa: E402
import gen_improvements_report as gir  # noqa: E402
import gen_insights as gi  # noqa: E402
import run_pipeline as rp  # noqa: E402

from hilmar import insights as insights_mod  # noqa: E402
from hilmar.model_router import ModelResponse  # noqa: E402

GOLDEN_DAY = REPO_ROOT / "tests" / "fixtures" / "golden_day.json"

INSIGHTS_STEP = "Daily insights (baselines + LLM)"


# ─────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────


def _write_config(tmp_path: Path) -> Path:
    """A minimal config whose paths.root exists → core.load_config performs
    no session-path healing and every artifact lands under tmp_path."""
    reports = tmp_path / "reports"
    reports.mkdir(exist_ok=True)
    cfg = {
        "paths": {
            "root": str(tmp_path),
            "data": str(tmp_path / "tracking-data-v2.json"),
            "reports": str(reports),
            "email_body": str(reports / "email-body.html"),
            "qc_result": str(reports / "qc-result.json"),
        },
    }
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
    return cfg_path


def _stage_golden_day(tmp_path: Path) -> None:
    (tmp_path / "tracking-data-v2.json").write_text(
        GOLDEN_DAY.read_text(encoding="utf-8"), encoding="utf-8",
    )


def _stub_response(task: str, text: str) -> ModelResponse:
    return ModelResponse(
        text=text, model="claude-opus-4-6", task_type=task,
        input_tokens=100, output_tokens=50, cost_cents=10,
    )


def _stub_bundle() -> insights_mod.NarrativeBundle:
    return insights_mod.NarrativeBundle(
        system=_stub_response("system_critique", "- System: add a QC phase"),
        design=_stub_response("design_suggestions", "- Design: sparkline the KPI row"),
        data=_stub_response("data_suggestions", "- Data: track reefer temp field"),
        business=_stub_response("business_advice", "- **Business**: push ONE on `HCMC`"),
    )


def _today_utc_label() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


# ─────────────────────────────────────────────────────────────────────
# 1. gen_insights.py shim — artifacts, mocked narrative
# ─────────────────────────────────────────────────────────────────────


def test_shim_writes_all_four_artifacts_with_mocked_narrative(tmp_path, monkeypatch):
    _stage_golden_day(tmp_path)
    cfg_path = _write_config(tmp_path)
    # Key present so the shim takes the narrative path — but the narrative
    # itself is fully mocked; no Anthropic client is ever constructed.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-never-used")
    monkeypatch.delenv("HILMAR_LLM_COST_LOG", raising=False)
    monkeypatch.setattr(
        gi.insights_mod, "generate_narrative",
        lambda ctx, *, router, feedback_summary=None: _stub_bundle(),
    )

    rc = gi.main(["--config", str(cfg_path)])
    assert rc == 0

    today = _today_utc_label()
    reports = tmp_path / "reports"
    json_path = reports / "insights" / f"{today}.json"
    html_path = reports / "insights" / f"{today}.html"
    business_path = reports / "insights-business.html"
    full_path = reports / "insights-full.html"
    for p in (json_path, html_path, business_path, full_path):
        assert p.exists(), f"missing artifact: {p}"

    # Structured JSON: context + all four narrative sections.
    doc = json.loads(json_path.read_text(encoding="utf-8"))
    assert doc["context"]["total"] == 5  # golden_day summary.total_entries
    assert set(doc["narrative"]) == {"system", "design", "data", "business"}
    assert doc["narrative"]["business"]["text"].startswith("- **Business**")
    assert doc["narrative"]["business"]["skipped_reason"] is None

    # Dated HTML + insights-full.html carry all four sections.
    for p in (html_path, full_path):
        html = p.read_text(encoding="utf-8")
        for label in ("System", "Design", "Data", "Business"):
            assert f">{label}</h4>" in html, f"{p.name} missing section {label}"

    # Business snippet is Business ONLY (staff must not see System/Design/Data).
    business_html = business_path.read_text(encoding="utf-8")
    assert ">Business</h4>" in business_html
    for internal_only in ("System", "Design", "Data"):
        assert f">{internal_only}</h4>" not in business_html

    # Baselines were updated next to the tracking data BEFORE insights ran.
    assert (tmp_path / "baselines.json").exists()


def test_shim_exits_zero_without_api_key_and_marks_narrative_skipped(tmp_path, monkeypatch, capsys):
    _stage_golden_day(tmp_path)
    cfg_path = _write_config(tmp_path)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    def _boom(*a, **k):  # the no-key path must never reach the LLM engine
        raise AssertionError("generate_narrative must not be called without an API key")
    monkeypatch.setattr(gi.insights_mod, "generate_narrative", _boom)

    rc = gi.main(["--config", str(cfg_path)])
    assert rc == 0

    today = _today_utc_label()
    doc = json.loads(
        (tmp_path / "reports" / "insights" / f"{today}.json").read_text(encoding="utf-8"),
    )
    # Rule-based context still shipped; every narrative section marked skipped.
    assert doc["context"]["total"] == 5
    for section in ("system", "design", "data", "business"):
        assert doc["narrative"][section]["skipped_reason"] == "missing ANTHROPIC_API_KEY"
    html = (tmp_path / "reports" / "insights-full.html").read_text(encoding="utf-8")
    assert "skipped" in html.lower()
    assert "ANTHROPIC_API_KEY not set" in capsys.readouterr().out


def test_shim_exits_zero_when_data_file_missing(tmp_path, capsys):
    cfg_path = _write_config(tmp_path)  # no tracking-data-v2.json staged
    rc = gi.main(["--config", str(cfg_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "not found" in out
    # Nothing half-written.
    assert not (tmp_path / "reports" / "insights-business.html").exists()


def test_shim_exits_zero_even_on_unexpected_crash(tmp_path, monkeypatch, capsys):
    """The catch-all at the CLI edge: any internal explosion still exits 0 —
    this step can never abort the fire ahead of the email build."""
    _stage_golden_day(tmp_path)
    cfg_path = _write_config(tmp_path)
    monkeypatch.setattr(
        gi.baselines_mod, "update",
        lambda **k: (_ for _ in ()).throw(RuntimeError("synthetic wiring crash")),
    )
    rc = gi.main(["--config", str(cfg_path)])
    assert rc == 0
    assert "synthetic wiring crash" in capsys.readouterr().out


def test_shim_prints_cost_and_warns_above_threshold(tmp_path, monkeypatch, capsys):
    """Cost telemetry comes from the router's own log; > threshold → loud WARN."""
    _stage_golden_day(tmp_path)
    cfg_path = _write_config(tmp_path)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)  # skip path — no LLM
    monkeypatch.delenv("HILMAR_LLM_COST_LOG", raising=False)
    monkeypatch.delenv("HILMAR_INSIGHTS_COST_ALERT_CENTS", raising=False)
    # Seed today's cost log with 300¢ — above the default 200¢ threshold.
    ts = datetime.now(timezone.utc).isoformat()
    (tmp_path / "llm-cost-log.jsonl").write_text(
        "\n".join(
            json.dumps({"ts": ts, "task": "business_advice",
                        "model": "claude-opus-4-6", "cost_cents": c})
            for c in (150, 150)
        ) + "\n",
        encoding="utf-8",
    )
    rc = gi.main(["--config", str(cfg_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "LLM cost today: 300¢" in out
    assert "WARN" in out and "$3.00" in out


# ─────────────────────────────────────────────────────────────────────
# 2. gen_email.py — staff-email embed of insights-business.html
# ─────────────────────────────────────────────────────────────────────


def _email_cfg(reports_dir: Path) -> dict:
    return {"paths": {"reports": str(reports_dir)}}


def _golden_data() -> dict:
    return copy.deepcopy(json.loads(GOLDEN_DAY.read_text(encoding="utf-8")))


def test_gen_email_embeds_fresh_business_section(tmp_path):
    snippet = "<h4 style='color:#0b3d91'>Business</h4><ul><li>Push ONE on HCMC</li></ul>"
    (tmp_path / "insights-business.html").write_text(snippet, encoding="utf-8")

    html = ge.build_body(_golden_data(), _email_cfg(tmp_path))
    assert "🤖 AI Insights — Business" in html
    assert "Push ONE on HCMC" in html
    # Embedded BEFORE the footer (attached-files guide).
    assert html.index("AI Insights — Business") < html.index("ATTACHED FILES")


def test_gen_email_skips_stale_business_section(tmp_path):
    path = tmp_path / "insights-business.html"
    path.write_text("<ul><li>YESTERDAYS-INSIGHT-MUST-NOT-RENDER</li></ul>", encoding="utf-8")
    stale = (datetime.now() - timedelta(days=1)).timestamp()
    os.utime(path, (stale, stale))

    html = ge.build_body(_golden_data(), _email_cfg(tmp_path))
    assert "AI Insights — Business" not in html
    assert "YESTERDAYS-INSIGHT-MUST-NOT-RENDER" not in html


def test_gen_email_skips_absent_business_section(tmp_path):
    html = ge.build_body(_golden_data(), _email_cfg(tmp_path))
    assert "AI Insights — Business" not in html


def test_gen_email_helper_skips_empty_and_oversize(tmp_path):
    path = tmp_path / "insights-business.html"
    path.write_text("   \n", encoding="utf-8")
    assert ge._ai_insights_business_html(_email_cfg(tmp_path)) == ""
    path.write_text("<li>x</li>" * 10_000, encoding="utf-8")  # ~100KB > 40KB cap
    assert ge._ai_insights_business_html(_email_cfg(tmp_path)) == ""


def test_gen_email_helper_never_raises(tmp_path):
    """Any failure renders nothing — a broken snippet can't break the email."""
    assert ge._ai_insights_business_html(None) == ""
    assert ge._ai_insights_business_html({"paths": {"reports": 42}}) == ""


# ─────────────────────────────────────────────────────────────────────
# 3. gen_improvements_report.py — audit embed of insights-full.html
# ─────────────────────────────────────────────────────────────────────


def test_improvements_report_embeds_full_insights_when_present(tmp_path, monkeypatch):
    monkeypatch.setattr(gir, "REPORTS", tmp_path)
    (tmp_path / "insights-full.html").write_text(
        "<h4>System</h4><ul><li>FULL-NARRATIVE-MARKER</li></ul>", encoding="utf-8",
    )
    out = gir._insights_full_section_inline()
    assert "AI Insights — System / Design / Data / Business" in out
    assert "FULL-NARRATIVE-MARKER" in out


def test_improvements_report_skips_stale_or_absent_full_insights(tmp_path, monkeypatch):
    monkeypatch.setattr(gir, "REPORTS", tmp_path)
    assert gir._insights_full_section_inline() == ""  # absent
    path = tmp_path / "insights-full.html"
    path.write_text("<li>stale</li>", encoding="utf-8")
    stale = (datetime.now() - timedelta(days=1)).timestamp()
    os.utime(path, (stale, stale))
    assert gir._insights_full_section_inline() == ""  # stale mtime


def test_improvements_report_render_html_includes_the_inline_call():
    """Same static-source style as the rate-intel embed guard: render_html
    must actually call the inline function, or the section silently dies."""
    src = inspect.getsource(gir.render_html)
    assert "_insights_full_section_inline()" in src
    assert "_rate_intel_section_inline()" in src  # neighbor pattern untouched


# ─────────────────────────────────────────────────────────────────────
# 4. run_pipeline.py — step registration + classification
# ─────────────────────────────────────────────────────────────────────


def test_pipeline_lists_insights_step_before_email_body():
    names = [s[0] for s in rp.STEPS]
    assert INSIGHTS_STEP in names
    assert names.index(INSIGHTS_STEP) < names.index("Email body HTML"), (
        "gen_email embeds the insights output — the insights step must run first"
    )
    step = next(s for s in rp.STEPS if s[0] == INSIGHTS_STEP)
    assert any("gen_insights.py" in part for part in step[1])


def test_pipeline_classifies_insights_step_best_effort():
    assert INSIGHTS_STEP in rp.BEST_EFFORT_STEPS, (
        "an LLM/telemetry step must never block the client fire"
    )


def test_pipeline_neighbor_steps_undisturbed():
    """The 2026-07-10 additions (manual + weekly summary) keep their order —
    the insights insertion must not have shuffled them."""
    names = [s[0] for s in rp.STEPS]
    assert (
        names.index(INSIGHTS_STEP)
        < names.index("Email body HTML")
        < names.index("Client-facing email HTML")
        < names.index("User manual HTML")
        < names.index("Weekly executive summary")
    )
