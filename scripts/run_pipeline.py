"""
run_pipeline.py — Master orchestrator for the Hilmar Daily Shipment Tracker.

Runs the full, idempotent pipeline end-to-end:
  1. Snapshot current tracking-data-v2.json (rotating backup)
  2. Ingest staged emails → rebuild requests
  3. QC self-heal (validate + fix + recompute summaries)
  4. Apply carrier-enrichment patch (idempotent — only fills gaps)
  5. Re-run QC after patch
  6. Generate dashboard HTML
  7. Generate 6-page client PDF
  8. Generate per-carrier scorecard PDFs
  9. Generate HTML email body

Each step is guarded — if a step fails, pipeline logs + exits with code 1 so the
scheduled-task runner can surface the failure. Prior step outputs are preserved
(backup.py gives us a rollback target).

Usage:
  python3 scripts/run_pipeline.py                  # full run
  python3 scripts/run_pipeline.py --skip-ingest    # use existing staging (artifact-only refresh)
  python3 scripts/run_pipeline.py --dry-run        # show what would run, don't execute
"""
from __future__ import annotations
import argparse
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
PY = sys.executable  # use same interpreter

#: Sentry-suppress env injected into the PRE-PATCH qc_selfheal step.
#: That step naturally measures incomplete state (carriers/rates not yet
#: backfilled), and surfacing those findings to Sentry creates false-positive
#: alerts (e.g. parser accuracy looks 14 points lower than the actual
#: post-patch shipped state). Only the POST-PATCH qc run represents the
#: real shipped quality — that's what fires Sentry alerts.
_PRE_PATCH_ENV = {"HILMAR_QC_PHASE": "pre-patch"}
_POST_PATCH_ENV = {"HILMAR_QC_PHASE": "post-patch"}

STEPS = [
    ("Backup snapshot",          [PY, str(SCRIPTS / "backup.py")]),
    ("Ingest (stage → requests)", [PY, str(SCRIPTS / "ingest.py")]),
    # 2026-05-06: drift_check wired in per orchestrator.md step 3.5. Runs
    # BEFORE QC. 6 phases: imid uniqueness, matcher quality, quote-rate
    # floor (FAILs if < 80%), NQ schema, WIN schema, lonny_covered honor.
    # Auto-heals phase 1 (dup imids) + phase 4 (NQ schema). Writes
    # reports/drift-result.json. Exits non-zero on FAIL — stops the pipeline
    # before bad data lands in the daily email.
    ("Drift check (pre-QC)",     [PY, str(SCRIPTS / "drift_check.py"), "--auto-heal"]),
    # Pre-patch QC runs BEFORE patch_carriers backfills missing fields.
    # HILMAR_QC_PHASE=pre-patch tells qc_selfheal not to fire Sentry events
    # on findings that the patch step will fix moments later.
    ("QC self-heal (pre-patch)", [PY, str(SCRIPTS / "qc_selfheal.py")], _PRE_PATCH_ENV),
    ("Carrier enrichment patch", [PY, str(SCRIPTS / "patch_carriers.py")]),
    # Post-patch QC represents the actual shipped state — this is the one
    # that fires real-time Sentry alerts.
    ("QC self-heal (post-patch)", [PY, str(SCRIPTS / "qc_selfheal.py")], _POST_PATCH_ENV),
    # 2026-05-19 Task #11 (Michael "go with task 11 and llm"): scan
    # unresolved Sentry issues from the last 26h + dispatch QC
    # remediation actions per the ACTIONS table in qc_actions_from_sentry.py.
    # Polling equivalent to Sentry webhooks (Cloud PC has no public IP
    # for webhook receive). Auto-resolves issues with a documented post-fix
    # commit; flags others for operator. --apply needed to actually post
    # comments + resolve (dry-run is default for safety).
    ("Sentry-driven QC actions",  [PY, str(SCRIPTS / "qc_actions_from_sentry.py"), "--apply"]),
    # 2026-05-19 PM (Michael "assume you are now locked with sentry for
    # auto fix and seer"): Seer is enabled in the hilmar-daily-tracker
    # Sentry project. Trigger Seer autofix on any error-level issue that
    # fired in the last 2 hours — Seer chews on it asynchronously and the
    # next audit email shows the diagnosis + proposed fix. --apply commits
    # real triggers; dry-run is default in the CLI but the pipeline step
    # opts in.
    ("Sentry Seer autofix trigger", [PY, str(SCRIPTS / "sentry_seer.py"), "trigger", "--apply"]),
    ("Dashboard HTML",           [PY, str(SCRIPTS / "gen_dashboard.py")]),
    ("Client PDF (6-page)",      [PY, str(SCRIPTS / "gen_pdf.py")]),
    ("Carrier scorecard PDFs",   [PY, str(SCRIPTS / "gen_carrier_scorecard_pdf.py")]),
    ("Email body HTML",          [PY, str(SCRIPTS / "gen_email.py")]),
    # 2026-05-13: Cross-project intelligence export + rate-negotiation
    # analytics. share_intel pushes Hilmar's data to the SHARED cross-
    # project store (consumed by rate-tracker for cross-client insights).
    # gen_rate_intelligence reads from there to produce the negotiation
    # cheat sheet + cooling/regression alerts in the daily idealx.us audit.
    ("Share to client_intelligence", [PY, str(SCRIPTS / "share_intel.py"), "export"]),
    ("Rate intelligence",        [PY, str(SCRIPTS / "gen_rate_intelligence.py"), "--quiet"]),
    # 2026-05-16: push entities to ol-quote-tracker's Turso-backed
    # client_intelligence registry via /api/intelligence/sync. Per Michael
    # "the shared you are using" + "client intelligence is client intelligence
    # and should be all encompassing". No-op if APP_PASSWORD missing.
    ("Sync to ol-quote-tracker", [PY, str(SCRIPTS / "sync_to_quote_tracker.py")]),
    # 2026-05-17: cross-check Hilmar wins against ol-quote-tracker. Per Michael
    # "you see hilmar data is also on there as a good check point for won
    # bookings". Both systems independently ingest the same OL emails — counts
    # SHOULD match. Drift surfaces a missed/mis-classified email in one system.
    # No-op if APP_PASSWORD missing. Output → reports/reconcile-quote-tracker.*
    # Wrapped in QC-038 freshness + drift check.
    ("Reconcile with ol-quote-tracker", [PY, str(SCRIPTS / "reconcile_with_quote_tracker.py")]),
]

SKIPPABLE = {"Ingest (stage → requests)": "--skip-ingest"}


# Sentry observability — initialized lazily so the pipeline runs fine
# even when sentry-sdk isn't installed or the DSN is missing.
sys.path.insert(0, str(SCRIPTS))
try:
    import sentry_setup as _sentry
except ImportError:
    _sentry = None


def run_step(name, cmd, dry_run=False, extra_env=None):
    print()
    print("═" * 70)
    print(f"▶  {name}")
    print(f"   cmd: {' '.join(cmd)}")
    if extra_env:
        print(f"   env: {extra_env}")
    print("═" * 70)
    if dry_run:
        print("   (dry-run — skipped)")
        return True

    # Wrap each step in a Sentry transaction so step durations + failures
    # are visible in Sentry's Performance view. Each step is a child of
    # the pipeline-level transaction started in main().
    txn_cm = None
    if _sentry is not None:
        try:
            import sentry_sdk
            txn_cm = sentry_sdk.start_span(op="pipeline.step", name=name)
            txn_cm.__enter__()
        except Exception:
            txn_cm = None

    # Inject extra env into the subprocess (e.g. HILMAR_QC_PHASE=pre-patch)
    # without polluting our own process env.
    import os as _os
    import time as _time
    sub_env = _os.environ.copy()
    if extra_env:
        sub_env.update(extra_env)

    step_started = _time.monotonic()
    try:
        result = subprocess.run(cmd, cwd=str(ROOT), env=sub_env)
        step_elapsed = _time.monotonic() - step_started

        # Per-step duration metric — powers the Sentry dashboard heatmap
        # showing which step is slowest. Tagged by step name so we can
        # group / filter.
        if _sentry is not None:
            try:
                _sentry.metric_distribution(
                    "pipeline.step_duration_s",
                    step_elapsed,
                    step=name,
                    status="ok" if result.returncode == 0 else "failed",
                )
            except Exception:
                pass

        if result.returncode != 0:
            print(f"❌ FAIL — {name} exited {result.returncode}")
            if _sentry is not None:
                try:
                    _sentry.capture_qc_error(
                        "pipeline.step_failure",
                        f"{name} exited rc={result.returncode}",
                    )
                except Exception:
                    pass
            return False
        return True
    finally:
        if txn_cm is not None:
            try:
                txn_cm.__exit__(None, None, None)
            except Exception:
                pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-ingest", action="store_true",
                    help="Skip the ingest step (use when rerunning against existing staged data)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print the steps that would run without executing them")
    args = ap.parse_args()

    # Initialize Sentry FIRST so any subsequent error gets captured.
    if _sentry is not None:
        _sentry.init(component="run_pipeline")

    started = datetime.now()
    print(f"🚀 Hilmar Tracker pipeline — started {started.isoformat(timespec='seconds')}")
    print(f"   Repo: {ROOT}")
    print(f"   Python: {PY}")

    # Sentry Cron heartbeat — `start` check-in. Sentry auto-creates the
    # monitor (slug=hilmar-daily-pipeline) on first call. Schedule is
    # "0 10 * * 1-5" America/New_York (Mon-Fri 10 AM ET). If the
    # finishing check-in doesn't arrive within max_runtime=60 min,
    # Sentry fires an alert — catches the silent-failure mode where the
    # Cloud PC / scheduler / wrapper dies before any error-event code
    # gets to run.
    cron_id = None
    if _sentry is not None:
        try:
            cron_id = _sentry.start_cron_checkin()
        except Exception:
            cron_id = None

    # Sentry Release marker — create a release for this fire's git SHA.
    # Per Michael 2026-05-17 ("use sentry for self check and improvements
    # as well"). Sentry uses releases to:
    #   - Auto-resolve issues when "Fixes SENTRY-XYZ" appears in commit msg
    #   - Show "first seen in release X" vs "fixed in release Y" on each issue
    #   - Track regression rate per release
    # The release tag was already on every event (release=hilmar-...@<sha>);
    # now we ALSO register the release explicitly so Sentry knows it exists
    # and can correlate commits.
    try:
        sys.path.insert(0, str(SCRIPTS))
        from sentry_api import SentryAPI
        _api = SentryAPI()
        if _api.enabled:
            import subprocess as _sp
            try:
                sha = _sp.run(["git", "rev-parse", "--short", "HEAD"],
                              cwd=str(ROOT), capture_output=True, text=True,
                              timeout=5).stdout.strip() or "unknown"
                msg = _sp.run(["git", "log", "-1", "--pretty=%B"],
                              cwd=str(ROOT), capture_output=True, text=True,
                              timeout=5).stdout.strip()[:500]
            except Exception:
                sha, msg = "unknown", ""
            version = f"hilmar-daily-tracker@{sha}"
            _api.create_release(
                version,
                commits=[{
                    "id": sha,
                    "repository": "IdealX-dev/hilmar-daily-routine",
                    "message": msg,
                }] if sha != "unknown" and msg else None,
            )
    except Exception:
        # Never let release tagging crash the pipeline
        pass

    # Wrap the entire pipeline run in a top-level Sentry transaction.
    pipeline_txn = None
    if _sentry is not None:
        try:
            import sentry_sdk
            pipeline_txn = sentry_sdk.start_transaction(
                op="pipeline.run",
                name="hilmar.daily_pipeline",
            )
            pipeline_txn.__enter__()
        except Exception:
            pipeline_txn = None

    failures = []
    for step in STEPS:
        # Each step is either (name, cmd) or (name, cmd, extra_env_dict)
        if len(step) == 3:
            name, cmd, extra_env = step
        else:
            name, cmd = step
            extra_env = None
        if args.skip_ingest and SKIPPABLE.get(name) == "--skip-ingest":
            print()
            print(f"⏭️   SKIP — {name} (--skip-ingest)")
            continue
        if not run_step(name, cmd, dry_run=args.dry_run, extra_env=extra_env):
            failures.append(name)
            # QC is allowed to warn; anything else stops the line
            if "QC self-heal" not in name:
                break

    # Pipeline-level metrics — duration + final status counter. These
    # power the Sentry dashboard's "Pipeline duration trend" and
    # "Failure rate over time" widgets. Use distribution for duration
    # (gives p50/p75/p95/p99) and a counter tagged by status for the
    # success/fail breakdown.
    elapsed_s = (datetime.now() - started).total_seconds()
    if _sentry is not None:
        try:
            _sentry.metric_distribution(
                "pipeline.duration_s",
                elapsed_s,
                skip_ingest=str(args.skip_ingest).lower(),
            )
            _sentry.metric_increment(
                "pipeline.status",
                1,
                status="failed" if failures else "ok",
            )
        except Exception:
            pass

    if pipeline_txn is not None:
        try:
            pipeline_txn.set_tag("pipeline_failures", len(failures))
            if failures:
                pipeline_txn.set_tag("pipeline_status", "failed")
            else:
                pipeline_txn.set_tag("pipeline_status", "ok")
            pipeline_txn.__exit__(None, None, None)
        except Exception:
            pass

    # Cron heartbeat — finish check-in. status=ok if all steps succeeded
    # (or only QC-self-heal had warnings); status=error otherwise. This
    # pairs with the start check-in above so Sentry knows the pipeline
    # completed (and how long it took).
    if _sentry is not None and cron_id is not None:
        try:
            _sentry.finish_cron_checkin(cron_id, success=not failures)
        except Exception:
            pass

    # Flush Sentry queue before exit so events aren't lost on quick pipeline runs
    if _sentry is not None:
        try:
            import sentry_sdk
            sentry_sdk.flush(timeout=5)
        except Exception:
            pass

    elapsed = (datetime.now() - started).total_seconds()
    print()
    print("═" * 70)
    if failures:
        print(f"❌ PIPELINE FAILED in {elapsed:.1f}s")
        print(f"   Failed steps: {', '.join(failures)}")
        sys.exit(1)
    print(f"✅ PIPELINE COMPLETE in {elapsed:.1f}s")
    print(f"   Reports: {ROOT / 'reports'}")
    print(f"   Data:    {ROOT / 'tracking-data-v2.json'}")


if __name__ == "__main__":
    main()
