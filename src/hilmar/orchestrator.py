"""
hilmar.orchestrator — The 8-step daily entrypoint.

Mirrors ``../orchestrator.md`` (the project runbook). Console script:
``hilmar-run``.

Steps:
  1. Preflight  — pytest must pass before anything else (skipped here;
                  CI handles it before deploy).
  2. Snapshot   — :mod:`hilmar.backup` snapshots tracking-data-v2.json.
  3. Ingest     — :mod:`hilmar.ingest` fetches new emails via Graph.
  4. QC         — :mod:`hilmar.qc` self-heals + writes qc-result.json.
  5. Render     — :mod:`hilmar.render` writes dashboard / pdf / scorecards
                  / email body.
  6. Dry-run gate — :envvar:`HILMAR_DRY_RUN=true` halts here (default).
  7. Send       — :mod:`hilmar.send` mails the daily + uploads to OneDrive.
  8. Archive    — moves today's outputs into reports/history/<date>.

Failure model: any unhandled exception aborts the run, logs the trace,
and (if not dry-run) tries to email Michael with a panic note. Never
overwrite tracking-data-v2.json without a fresh snapshot in step 2.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import sys
import traceback
from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import baselines as baselines_mod
from . import ingest as ingest_mod
from . import insights as insights_mod
from . import qc as qc_mod
from . import render as render_mod
from . import send as send_mod
from .graph_client import GraphClient
from .model_router import ModelRouter
from .paths import (
    backup_dir,
    data_dir,
    reports_dir,
    schema_file,
)

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────
# Dry-run gate
# ─────────────────────────────────────────────────────────────────────

def is_dry_run() -> bool:
    """``HILMAR_DRY_RUN`` defaults to ``true``. Anything other than ``"false"``
    (case-insensitive) keeps us in dry-run.
    """
    return os.environ.get("HILMAR_DRY_RUN", "true").strip().lower() != "false"


# ─────────────────────────────────────────────────────────────────────
# Step bodies
# ─────────────────────────────────────────────────────────────────────

def step_snapshot(*, data_path: Path, snapshots_dir: Path) -> Path | None:
    """Step 2 — copy tracking-data-v2.json to ``snapshots_dir`` with a
    timestamp. Returns the snapshot path or ``None`` if the data file
    doesn't exist yet (first run).
    """
    if not data_path.exists():
        log.info("step_snapshot: %s does not exist yet — first run, skipping", data_path)
        return None
    snapshots_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    snap = snapshots_dir / f"tracking-data-v2.{ts}.json"
    shutil.copy2(data_path, snap)
    log.info("step_snapshot: wrote %s", snap)

    # Retention: keep the most-recent 14.
    snaps = sorted(snapshots_dir.glob("tracking-data-v2.*.json"))
    for old in snaps[:-14]:
        try:
            old.unlink()
        except OSError as e:
            log.warning("retention prune failed for %s: %s", old, e)
    return snap


def step_ingest(
    *,
    client: GraphClient,
    data_path: Path,
    days_back: int = 14,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Step 3 — pull new emails, idempotent merge into tracking-data-v2.json."""
    end = now or datetime.now(timezone.utc)
    start = end - timedelta(days=days_back)
    return ingest_mod.run_ingest(
        client=client,
        data_path=data_path,
        window_start=start,
        window_end=end,
        now=now,
    )


def step_qc(
    *,
    data_path: Path,
    schema_path: Path,
    snapshots_dir: Path,
    qc_result_path: Path,
) -> dict[str, Any]:
    """Step 4 — run the 7-phase QC self-heal."""
    result, _log = qc_mod.run_qc(
        data_path=data_path,
        schema_path=schema_path,
        backups_dir=snapshots_dir,
        result_path=qc_result_path,
        do_backup=False,  # already snapshotted in step 2
    )
    if result.get("status") == "BLOCKED":
        raise RuntimeError(f"QC blocked: {result.get('blockers')}")
    return result


def step_baselines(
    *,
    data_path: Path,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Step 4 (Phase B) — compute rolling baselines BEFORE qc, persist them
    to baselines.json AND graft into tracking-data on disk so qc.phases 8/9
    can read them inline.

    Pre-Phase-B this ran AFTER qc (combined with insights) and grafted
    only in-memory — phases 8/9 always saw `data.get("baselines") = None`
    and silently skipped. The parser-regression and ingest-gap alert
    engines were dormant in production. Phase B fixes the ordering.
    """
    now = now or datetime.now(timezone.utc)
    tracking = json.loads(data_path.read_text(encoding="utf-8"))
    baselines_path = data_path.parent / "baselines.json"
    baselines = baselines_mod.update(
        tracking_data=tracking, baselines_path=baselines_path, now=now,
    )
    # Graft into tracking AND write back to disk so qc reads the same
    # baselines block phases 8/9 expect.
    tracking = baselines_mod.graft_into_tracking_data(tracking, baselines)
    from .core import save_data
    save_data(tracking, data_path)
    log.info("baselines computed pre-QC and grafted into tracking-data: %s", baselines_path)
    return {
        "baselines": baselines,
        "baselines_path": baselines_path,
    }


def step_insights(
    *,
    data_path: Path,
    out_reports_dir: Path,
    qc_result: dict[str, Any],
    router_factory: Callable[[], ModelRouter] | None = None,
    skip_llm: bool = False,
    today_label: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Step 6 (Phase B) — build InsightsContext using post-QC tracking-data,
    run the LLM narrative (unless ``skip_llm`` is True), persist
    reports/insights/<date>.{json,html}.

    Splits cleanly from step_baselines now that baselines run pre-QC.
    InsightsContext sees post-QC requests + post-QC summary + the
    baselines that were grafted in step_baselines.
    """
    now = now or datetime.now(timezone.utc)
    today_label = today_label or now.strftime("%Y-%m-%d")

    tracking = json.loads(data_path.read_text(encoding="utf-8"))

    miss_log = data_path.parent / "parser_misses.jsonl"
    ctx = insights_mod.build_context(
        tracking_data=tracking, qc_result=qc_result, now=now,
        parser_miss_log_path=miss_log if miss_log.exists() else None,
    )

    insights_dir = out_reports_dir / "insights"
    insights_dir.mkdir(parents=True, exist_ok=True)

    cost_alert = False
    bundle = None
    insights_html = ""
    insights_html_staff = ""
    insights_html_internal = ""

    if not skip_llm:
        try:
            router = (router_factory or ModelRouter)()
        except Exception as e:  # noqa: BLE001
            log.warning("ModelRouter init failed (%s) — skipping LLM narrative", e)
            router = None

        if router is not None:
            # Optional feedback summary fed into next-run prompts.
            feedback_path = data_path.parent / "insights-feedback.json"
            from . import feedback_ingest as fb_mod
            feedback_summary = fb_mod.load_feedback_summary(
                feedback_path, now=now,
            ) or None
            # The narrative call can ALSO fail (anthropic SDK missing,
            # API unreachable, every task throttled twice). Per the
            # INSIGHTS-DESIGN.md "Cascade-down on errors" rule, an LLM
            # failure must not kill the daily run — we ship rule-based
            # metrics + a "LLM-narrative skipped" note.
            try:
                bundle = insights_mod.generate_narrative(
                    ctx, router=router, feedback_summary=feedback_summary,
                )
                # Render the narrative TWICE per Michael's 2026-04-28
                # directive: staff (eventual TO) only see Business
                # — strategic actions, no system-internal observations
                # like parser miss rates or QC fix counts. Internal
                # version (System+Design+Data+Business) goes only to
                # Michael via the internal review email path.
                insights_html_staff = insights_mod.render_narrative_html(
                    bundle, today_label=today_label,
                    sections=insights_mod.STAFF_SECTIONS,
                )
                insights_html_internal = insights_mod.render_narrative_html(
                    bundle, today_label=today_label,
                    sections=insights_mod.INTERNAL_SECTIONS,
                )
                # Backward-compat alias — anything still reading
                # ``insights_html`` (legacy callers) gets the staff
                # version, which is the safe default for the email.
                insights_html = insights_html_staff
                cost_alert = router.should_alert_cost()
            except Exception as e:  # noqa: BLE001
                log.warning(
                    "LLM narrative failed (%s) — shipping rule-based metrics only", e,
                )
                bundle = None
                insights_html = (
                    "<p style='color:#92400e'>⚠️ LLM narrative skipped: "
                    f"{type(e).__name__}: {e}</p>"
                )
                insights_html_staff = insights_html
                insights_html_internal = insights_html
                cost_alert = False

    # Persist structured + rendered output for archive / debugging.
    json_path = insights_dir / f"{today_label}.json"
    json_path.write_text(
        json.dumps({
            "context": insights_mod.context_to_dict(ctx),
            "narrative": (
                None if bundle is None else {
                    section: {
                        "model": getattr(bundle, section).model,
                        "text": getattr(bundle, section).text,
                        "cost_cents": getattr(bundle, section).cost_cents,
                        "skipped_reason": getattr(bundle, section).skipped_reason,
                    } for section in ("system", "design", "data", "business")
                }
            ),
            "cost_alert": cost_alert,
        }, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    # Two on-disk HTML files: the staff-facing one matches what the
    # email body shows; the internal one is the full operational view
    # for Michael (also delivered via separate internal email below).
    html_path = insights_dir / f"{today_label}.html"
    html_path.write_text(insights_html_staff or "<p>(LLM narrative skipped)</p>", encoding="utf-8")
    internal_html_path = insights_dir / f"{today_label}.internal.html"
    internal_html_path.write_text(
        insights_html_internal or "<p>(LLM narrative skipped)</p>",
        encoding="utf-8",
    )

    return {
        "context": ctx,
        "context_dict": insights_mod.context_to_dict(ctx),  # for render's anomaly banner
        "bundle": bundle,
        "insights_html": insights_html,                # staff-facing (default)
        "insights_html_staff": insights_html_staff,
        "insights_html_internal": insights_html_internal,
        "cost_alert": cost_alert,
        "json_path": json_path,
        "html_path": html_path,
        "internal_html_path": internal_html_path,
    }


def step_render(
    *,
    data_path: Path,
    out_reports_dir: Path,
    insights_html: str | None = None,
    dod: dict[str, Any] | None = None,
    insights_ctx: dict[str, Any] | None = None,
    period_trends: dict[str, Any] | None = None,
    pricing_levels: dict[str, Any] | None = None,
    lane_sparklines: dict[str, dict] | None = None,
) -> dict[str, Any]:
    """Step 7 — regenerate all 4 artifact families. ``period_trends``
    drives the WoW/MoM/YTD section; ``pricing_levels`` drives the
    expensive/cheap rate-vs-median surface; ``lane_sparklines`` adds
    a 14-day activity sparkline column to winning/losing lane tables."""
    out_reports_dir.mkdir(parents=True, exist_ok=True)
    dash = render_mod.render_dashboard(
        data_path=data_path,
        out_path=out_reports_dir / "hilmar-dashboard.html",
        period_trends=period_trends,
        pricing_levels=pricing_levels,
    )
    pdf = render_mod.render_pdf(
        data_path=data_path,
        out_path=out_reports_dir / "hilmar-report.pdf",
    )
    email = render_mod.render_email(
        data_path=data_path,
        out_path=out_reports_dir / "email-body.html",
        insights_html=insights_html,
        dod=dod,
        insights_ctx=insights_ctx,
        period_trends=period_trends,
        pricing_levels=pricing_levels,
        lane_sparklines=lane_sparklines,
    )
    scorecards = render_mod.render_scorecards(
        data_path=data_path,
        out_dir=out_reports_dir / "carrier-scorecards",
    )
    return {
        "dashboard": dash,
        "pdf": pdf,
        "email": email,
        "scorecards": scorecards,
    }


def step_compute_analytics(
    *,
    data_path: Path,
    daily_snapshots_dir: Path,
    today: date | None = None,
) -> dict[str, Any]:
    """Step 6.6 — compute period-over-period trends + pricing-level
    analysis. Pure-function delegation to core; returns a bundle the
    render step threads through to template."""
    from . import core
    tracking = json.loads(data_path.read_text(encoding="utf-8"))
    return {
        "period_trends": core.compute_period_trends(
            daily_snapshots_dir, today=today,
        ),
        "pricing_levels": core.compute_pricing_levels(
            tracking.get("requests") or [],
        ),
        "lane_sparklines": core.compute_lane_activity_sparklines(
            tracking.get("requests") or [], days=14, today=today,
        ),
    }


def step_compute_dod(
    *,
    data_path: Path,
    snapshots_dir: Path,
    today_iso: str | None = None,
) -> dict[str, Any] | None:
    """Step 6.5 — load yesterday's daily snapshot and diff against today's
    requests via ``core.compute_dod``. Returns the dod block (consumed by
    render_email's "What happened since last run" section) or None on
    first-run where no prior snapshot exists yet."""
    from . import core
    prev = core.load_previous_snapshot(snapshots_dir, today_iso=today_iso)
    if not prev:
        return None
    tracking = json.loads(data_path.read_text(encoding="utf-8"))
    return core.compute_dod(
        prev.get("row_state") or {},
        tracking.get("requests") or [],
        today_iso=today_iso,
    )


def step_persist_snapshot(
    *,
    data_path: Path,
    snapshots_dir: Path,
    today_iso: str | None = None,
) -> Path:
    """Step 8.5 — write today's snapshot to ``snapshots_dir/{date}.json``
    so tomorrow's run can diff. Idempotent — re-running the same day
    overwrites with last-run-of-the-day state, which is the desired
    semantic for "what was true at end-of-day"."""
    from . import core
    tracking = json.loads(data_path.read_text(encoding="utf-8"))
    return core.persist_daily_snapshot(tracking, snapshots_dir, today_iso=today_iso)


def _resolve_distribution() -> list[str]:
    """The OL-USA daily distribution list. ENV-driven so deploy can swap
    test/full lists without code change.
    """
    raw = os.environ.get("HILMAR_DAILY_TO", "michael.deitchman@ol-usa.com")
    return [a.strip() for a in raw.split(",") if a.strip()]


def _compose_headline_subject(data_path: Path, today: str) -> str:
    """Build a high-signal subject line with the day's headline metrics.

    Format: ``Hilmar — 10 W / 22 Q&L / $44.2k won · 24.4% — Apr 28``

    Recipients see the punchline before opening: how many wins, how
    many quoted-losses, how much money OL booked, and the win rate.
    Falls back to the plain date label if data is unreadable.
    """
    try:
        data = json.loads(Path(data_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return f"Hilmar Rate-Desk Daily — {today}"
    summ = data.get("summary") or {}
    wins = int(summ.get("wins") or 0)
    ql = int(summ.get("quoted_lost") or 0)
    win_rate = summ.get("win_rate") or 0
    won_dollars = _total_value_won(data.get("requests") or [])
    if won_dollars >= 1000:
        money = f"${won_dollars/1000:.1f}k"
    else:
        money = f"${won_dollars:.0f}" if won_dollars else "—"
    return (
        f"Hilmar — {wins}W / {ql} Q&L / {money} won · {win_rate}% — {today}"
    )


def _total_value_won(requests: list[dict]) -> float:
    """Sum ``rate_per_feu × FEU-equivalent count`` across WIN rows.

    Used by the subject line + the KPI grid's "$ won today" cell.
    rate_per_feu is the normalized number from PR #28; teu_won/2 gives
    FEU count (since 1 FEU = 2 TEU).
    """
    total = 0.0
    for r in requests:
        if r.get("status") != "WIN":
            continue
        rpf = r.get("rate_per_feu")
        if not rpf:
            continue
        teu = r.get("teu_won") or r.get("teu_requested") or 0
        feu = (teu or 0) / 2.0
        try:
            total += float(rpf) * feu
        except (TypeError, ValueError):
            continue
    return round(total, 2)


def step_send(
    *,
    client: GraphClient,
    artifacts: dict[str, Any],
    data_path: Path | None = None,
    subject: str | None = None,
) -> str:
    """Step 7 — mail the daily + always CC HILMAR_DAILY_CC.

    Attachments: PDF + each per-carrier scorecard PDF. The HTML
    dashboard is NO LONGER attached (per Michael 2026-04-28).

    Subject line includes headline metrics (wins / Q&L / $ won /
    win-rate / date) per Michael 2026-04-28 — recipients see the
    punchline before opening.
    """
    email_html = Path(artifacts["email"]).read_text(encoding="utf-8")
    today = datetime.now(timezone.utc).strftime("%b %d, %Y")
    if subject is None and data_path is not None:
        subject = _compose_headline_subject(data_path, today)
    subj = subject or f"Hilmar Rate-Desk Daily — {today}"
    attachments: list[Path] = [Path(artifacts["pdf"])]
    for sc in artifacts.get("scorecards") or []:
        attachments.append(Path(sc))
    return send_mod.send_daily_email(
        client=client,
        to=_resolve_distribution(),
        subject=subj,
        html_body=email_html,
        attachments=attachments,
    )


def _resolve_internal_distribution() -> list[str]:
    """Internal-review recipient list. Defaults to HILMAR_DAILY_CC
    (Michael's idealx) so the operational-internal narrative reaches
    Michael even before HILMAR_INTERNAL_TO is explicitly set on the VM.
    Empty list → step_send_internal logs + skips (still safe)."""
    raw = os.environ.get("HILMAR_INTERNAL_TO")
    if not raw:
        raw = os.environ.get("HILMAR_DAILY_CC", "michael.deitchman@idealx.us")
    return [a.strip() for a in raw.split(",") if a.strip()]


def step_send_internal(
    *,
    client: GraphClient,
    insights_html_internal: str,
    qc_result: dict[str, Any],
    today_label: str,
) -> str:
    """Step 7.5 — internal-only operational review. System / Design /
    Data / Business narrative + a compact QC summary + parser-fallback
    stats. Recipient: HILMAR_INTERNAL_TO (defaults to HILMAR_DAILY_CC).

    Per Michael's 2026-04-28 directive — these signals are for system
    operators, NOT staff; this email is the channel that surfaces them
    without polluting the staff-facing daily.
    """
    qc_block = (
        "<h3 style='margin: 16px 0 6px 0; color: #0b3d91;'>QC summary</h3>"
        "<ul style='font-size: 13px; line-height: 1.6;'>"
        f"<li>status: <b>{qc_result.get('status', '?')}</b></li>"
        f"<li>fixes: {qc_result.get('fixes', 0)}</li>"
        f"<li>warnings: {qc_result.get('warnings', 0)}</li>"
        f"<li>errors: {qc_result.get('errors', 0)}</li>"
        "</ul>"
    )
    if qc_result.get("error_details"):
        qc_block += (
            "<p style='font-size: 12px; color: #991b1b;'>Errors:</p><ul>"
            + "".join(f"<li>{e}</li>" for e in qc_result["error_details"])
            + "</ul>"
        )
    body = (
        f"<html><body style='font-family: -apple-system, sans-serif; padding: 16px;'>"
        f"<h2 style='color: #0b3d91; margin: 0 0 4px 0;'>Hilmar Tracker — Internal Review</h2>"
        f"<p style='font-size: 12px; opacity: 0.7; margin: 0 0 16px 0;'>"
        f"{today_label} · operational signals only — not for staff distribution"
        f"</p>"
        f"{qc_block}"
        f"<h3 style='margin: 20px 0 6px 0; color: #0b3d91;'>Narrative (full)</h3>"
        f"{insights_html_internal or '<p>(no narrative)</p>'}"
        f"</body></html>"
    )
    return send_mod.send_internal_review(
        client=client,
        to=_resolve_internal_distribution(),
        subject=f"[INTERNAL] Hilmar Tracker review — {today_label}",
        html_body=body,
    )


def step_upload(
    *,
    client: GraphClient,
    data_path: Path,
    artifacts: dict[str, Any],
) -> dict[Path, str]:
    """Step 8 — push the canonical artifacts to OneDrive.

    Non-fatal by design. step_send already happened by the time we get
    here — recipients have the email in their inbox. A failure here is
    an archival miss, not a missed daily. We log + continue so the
    daily run still succeeds end-to-end (and step_archive afterward
    still moves today's local artifacts into reports/history/).

    Common failure modes: stale ``HILMAR_ONEDRIVE_FOLDER_ID``, expired
    token on the folder owner, OneDrive permissions changed. None of
    these justify aborting the daily.

    Prefers ``HILMAR_ONEDRIVE_FOLDER_PATH`` over the legacy
    ``HILMAR_ONEDRIVE_FOLDER_ID`` — paths auto-create the folder on
    first use and survive renames/moves. Default path
    ``"Hilmar Tracker Reports"`` if neither var is set.
    """
    folder_path = os.environ.get("HILMAR_ONEDRIVE_FOLDER_PATH")
    folder_id = os.environ.get("HILMAR_ONEDRIVE_FOLDER_ID")
    if not folder_path and not folder_id:
        # Default to a sensible path so the feature works without any
        # env config — auto-creates "Hilmar Tracker Reports" on first
        # upload. Easy to override per env when needed.
        folder_path = "Hilmar Tracker Reports"
        log.info(
            "HILMAR_ONEDRIVE_FOLDER_PATH/ID unset — defaulting to %r (auto-created)",
            folder_path,
        )
    try:
        return send_mod.upload_artifacts(
            client=client,
            folder_path=folder_path,
            folder_id=folder_id if not folder_path else None,
            paths=[data_path, Path(artifacts["dashboard"]), Path(artifacts["pdf"])],
        )
    except Exception as e:  # noqa: BLE001 — upload is archival, not load-bearing
        msg = str(e)
        # Stale-folder 404 is the known, non-actionable noise pattern
        # (Michael's OneDrive folder was renamed/moved). Log it as info
        # so the daily journalctl stays clean; everything else stays a
        # warning since it's likely a real auth/permission regression.
        is_stale_folder = "404" in msg or "itemNotFound" in msg
        log_fn = log.info if is_stale_folder else log.warning
        log_fn(
            "step_upload skipped (%s) — email already sent, archival miss only. "
            "%sdownstream steps continuing.",
            "stale OneDrive folder" if is_stale_folder else "unexpected error",
            "" if is_stale_folder else "Check HILMAR_ONEDRIVE_FOLDER_PATH validity. ",
        )
        return {}


def step_archive(*, reports_dir_path: Path, today_label: str) -> Path:
    """Step (10) archive — move today's outputs into reports/history/<date>."""
    target = reports_dir_path / "history" / today_label
    target.mkdir(parents=True, exist_ok=True)
    for fname in (
        "hilmar-dashboard.html",
        "hilmar-report.pdf",
        "email-body.html",
        "qc-result.json",
    ):
        src = reports_dir_path / fname
        if src.exists():
            shutil.copy2(src, target / fname)
    sc_dir = reports_dir_path / "carrier-scorecards"
    if sc_dir.exists():
        sc_target = target / "carrier-scorecards"
        if sc_target.exists():
            shutil.rmtree(sc_target)
        shutil.copytree(sc_dir, sc_target)
    return target


# ─────────────────────────────────────────────────────────────────────
# Public entrypoint
# ─────────────────────────────────────────────────────────────────────

def run(
    *,
    client_factory: Callable[[], GraphClient] | None = None,
    router_factory: Callable[[], ModelRouter] | None = None,
    skip_llm: bool = False,
    days_back: int = 14,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Run the daily pipeline.

    ``client_factory`` lets tests inject a stub GraphClient. Production code
    leaves it ``None`` and we instantiate a real :class:`GraphClient` with
    silent (cron) auth.

    Returns a dict with run metadata + per-step outputs.
    """
    data_path = data_dir() / "tracking-data-v2.json"
    # schema.json is a static repo-root file, not runtime data. Use
    # paths.schema_file() which honours HILMAR_SCHEMA_PATH env first, then
    # falls back to the repo-root copy via package-install introspection.
    # Previously this was `data_dir() / "schema.json"` which produced
    # /opt/hilmar-tracker/data/schema.json on the VM — never populated,
    # making QC report HAS_ERRORS every run.
    schema_path = schema_file()
    snapshots_dir = backup_dir()  # full-file timestamped backups (rolling, 14d)
    daily_snapshots_dir = data_dir() / "daily_snapshots"  # one compact file per day
    out_reports_dir = reports_dir()
    qc_result_path = out_reports_dir / "qc-result.json"

    if client_factory is None:
        def _factory() -> GraphClient:
            gc = GraphClient()
            gc.authenticate(interactive_ok=False)
            return gc
        client_factory = _factory

    client = client_factory()
    today_label = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%d")

    # Step 2 — snapshot before any mutation.
    snap = step_snapshot(data_path=data_path, snapshots_dir=snapshots_dir)

    # Step 3 — ingest.
    doc = step_ingest(client=client, data_path=data_path, days_back=days_back, now=now)

    # Step 4 (Phase B) — baselines BEFORE qc so phases 8/9 (parser
    # regression, ingest gap detection) can read them from
    # ``data["baselines"]`` and actually fire. Pre-Phase-B these phases
    # were dormant — baselines were computed AFTER qc and only grafted
    # in-memory, so phases 8/9 always saw ``None`` and silently skipped.
    step_baselines(data_path=data_path, now=now)

    # Step 5 — QC. Reads tracking-data with baselines now grafted.
    qc_result = step_qc(
        data_path=data_path,
        schema_path=schema_path,
        snapshots_dir=snapshots_dir,
        qc_result_path=qc_result_path,
    )

    # Refresh in-memory doc from disk now that QC's phase_5 has
    # re-aggregated and persisted ``data["summary"]``. Without this the
    # ``doc_summary`` field in the orchestrator's return dict reports
    # pre-QC counts (e.g. 11 wins) while ``qc_result["counts"]`` shows
    # the canonical post-QC counts (e.g. 10 wins).
    doc = json.loads(data_path.read_text(encoding="utf-8"))

    # Step 6 — insights. Builds InsightsContext on post-QC tracking
    # (which carries the baselines grafted in step_baselines), runs LLM
    # narrative, persists reports/insights/<date>.{json,html}.
    insights_pkg = step_insights(
        data_path=data_path,
        out_reports_dir=out_reports_dir,
        qc_result=qc_result,
        router_factory=router_factory,
        skip_llm=skip_llm,
        today_label=today_label,
        now=now,
    )

    # Step 6.5 — diff today's state against yesterday's snapshot. Returns
    # None on first-run (no prior snapshot exists yet), in which case the
    # email falls back to its today_events list.
    dod = step_compute_dod(
        data_path=data_path,
        snapshots_dir=daily_snapshots_dir,
        today_iso=today_label,
    )

    # Step 6.6 — compute period trends + pricing levels. Pricing works
    # immediately on the 14-day window; trends start showing real values
    # as daily_snapshots/{date}.json files accumulate over the warm-up
    # period (7 days for WoW, 30 for MoM, since-Jan-1 for YTD).
    analytics = step_compute_analytics(
        data_path=data_path,
        daily_snapshots_dir=daily_snapshots_dir,
        today=(now or datetime.now(timezone.utc)).date(),
    )

    # Step 7 — render. Insights HTML, dod, anomaly banner, period
    # trends, and pricing-levels all flow into the email + dashboard.
    artifacts = step_render(
        data_path=data_path,
        out_reports_dir=out_reports_dir,
        insights_html=insights_pkg["insights_html"] or None,
        dod=dod,
        insights_ctx=insights_pkg.get("context_dict"),
        period_trends=analytics.get("period_trends"),
        pricing_levels=analytics.get("pricing_levels"),
        lane_sparklines=analytics.get("lane_sparklines"),
    )

    # Step 8.5 — write today's snapshot for tomorrow's diff. Done AFTER
    # render so render reads the canonical state, not a partially-written
    # snapshot. Idempotent on re-run.
    step_persist_snapshot(
        data_path=data_path,
        snapshots_dir=daily_snapshots_dir,
        today_iso=today_label,
    )

    # Step 6 — dry-run gate.
    if is_dry_run():
        log.info("HILMAR_DRY_RUN enabled — stopping after render. Artifacts at %s",
                 out_reports_dir)
        return {
            "dry_run": True,
            "snapshot": snap,
            "doc_summary": doc.get("summary"),
            "qc_status": qc_result.get("status"),
            "artifacts": artifacts,
            "insights": {
                "json": insights_pkg["json_path"],
                "html": insights_pkg["html_path"],
                "cost_alert": insights_pkg["cost_alert"],
            },
        }

    # Step 7 — send.
    msg_id = step_send(client=client, artifacts=artifacts, data_path=data_path)

    # Step 7.5 — internal-only operational review (Michael only).
    # Carries the System/Design/Data narrative + QC summary that staff
    # should NOT see in the daily distribution.
    internal_msg_id = ""
    try:
        internal_msg_id = step_send_internal(
            client=client,
            insights_html_internal=insights_pkg.get("insights_html_internal") or "",
            qc_result=qc_result,
            today_label=today_label,
        )
    except Exception as e:  # noqa: BLE001
        log.warning(
            "step_send_internal failed (%s) — staff email already sent, internal review missed only",
            e,
        )

    # Step 8 — upload.
    uploads = step_upload(client=client, data_path=data_path, artifacts=artifacts)

    # Step 10 — archive (numbering matches orchestrator.md; step 9 = escalation,
    # not implemented in M3 — see HANDOFF for M4+ scope).
    archive = step_archive(reports_dir_path=out_reports_dir, today_label=today_label)

    return {
        "dry_run": False,
        "snapshot": snap,
        "doc_summary": doc.get("summary"),
        "qc_status": qc_result.get("status"),
        "artifacts": artifacts,
        "message_id": msg_id,
        "internal_message_id": internal_msg_id,
        "uploads": uploads,
        "archive": archive,
        "insights": {
            "json": insights_pkg["json_path"],
            "html": insights_pkg["html_path"],
            "cost_alert": insights_pkg["cost_alert"],
        },
    }


def _page_on_failure(traceback_text: str) -> None:
    """Best-effort failure paging. Tries TWO channels in sequence:

    1. ``HILMAR_FAILURE_EMAIL`` — sends a failure email via Graph.
       Default ``michael.deitchman@idealx.us``. Set to empty string to
       disable. Lazy-inits a GraphClient inside this function so a
       Graph-auth failure earlier in the run doesn't poison the pager.
    2. ``HILMAR_FAILURE_WEBHOOK`` — POSTs a JSON payload (Slack-compat).
       Useful if Graph itself is the failure mode.

    Both paths swallow their own errors: we MUST NOT mask the original
    exception that brought us here.
    """
    _try_failure_email(traceback_text)
    _try_failure_webhook(traceback_text)


def _try_failure_email(traceback_text: str) -> None:
    """Email branch of :func:`_page_on_failure`. No-op when
    ``HILMAR_FAILURE_EMAIL`` is explicitly empty."""
    recipient = os.environ.get("HILMAR_FAILURE_EMAIL", "michael.deitchman@idealx.us")
    if not recipient:
        return
    try:
        from .graph_client import GraphClient
        from .send import send_failure_email
        host = os.environ.get("HOSTNAME", "unknown-host")
        run_at = datetime.now(timezone.utc).isoformat()
        gc = GraphClient()
        gc.authenticate(interactive_ok=False)
        msg_id = send_failure_email(
            gc, to=recipient, host=host, run_at=run_at,
            traceback_text=traceback_text,
        )
        log.info("failure email sent to %s (message_id=%s)", recipient, msg_id)
    except Exception as e:  # noqa: BLE001
        log.warning("failure email itself failed (%s) — swallowing", e)


def _try_failure_webhook(traceback_text: str) -> None:
    """Webhook branch of :func:`_page_on_failure`. Original Slack-compat
    JSON POST. Doesn't depend on Graph auth.
    """
    webhook = os.environ.get("HILMAR_FAILURE_WEBHOOK")
    if not webhook:
        return
    try:
        import json as _json
        import urllib.request
        host = os.environ.get("HOSTNAME", "unknown-host")
        run_at = datetime.now(timezone.utc).isoformat()
        # Take only the last 30 lines of the traceback — Slack message
        # length limits are real and the bottom of the trace usually
        # has the most actionable signal.
        tb_tail = "\n".join(traceback_text.splitlines()[-30:])
        payload = {
            "text": (
                f":rotating_light: *hilmar-tracker daily run failed* "
                f"on `{host}` at {run_at}\n```{tb_tail}```"
            ),
            "host": host,
            "run_at": run_at,
            "traceback_tail": tb_tail,
            "service": "hilmar-tracker",
            "severity": "error",
        }
        req = urllib.request.Request(
            webhook,
            data=_json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            log.info("failure webhook posted: HTTP %s", resp.status)
    except Exception as e:  # noqa: BLE001
        log.warning("failure webhook itself failed (%s) — swallowing", e)


def main() -> int:
    """``hilmar-run`` console-script entry."""
    from . import logging_config

    logging_config.configure_from_env()
    log.info("orchestrator boot", extra={"runtime": logging_config.runtime_summary()})

    try:
        result = run()
    except Exception:
        log.exception("orchestrator failed")
        tb = traceback.format_exc()
        traceback.print_exc(file=sys.stderr)
        # Best-effort page out — email + webhook, both swallow own errors.
        _page_on_failure(tb)
        return 1

    print("Orchestrator OK. Summary:")
    for k, v in result.items():
        if k == "artifacts":
            print("  artifacts:")
            for name, path in v.items():
                print(f"    {name}: {path}")
        else:
            print(f"  {k}: {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
