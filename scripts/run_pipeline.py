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
    ("QC self-heal (pre-patch)", [PY, str(SCRIPTS / "qc_selfheal.py")]),
    ("Carrier enrichment patch", [PY, str(SCRIPTS / "patch_carriers.py")]),
    ("QC self-heal (post-patch)", [PY, str(SCRIPTS / "qc_selfheal.py")]),
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


def run_step(name, cmd, dry_run=False):
    print()
    print("═" * 70)
    print(f"▶  {name}")
    print(f"   cmd: {' '.join(cmd)}")
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

    try:
        result = subprocess.run(cmd, cwd=str(ROOT))
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
    for name, cmd in STEPS:
        if args.skip_ingest and SKIPPABLE.get(name) == "--skip-ingest":
            print()
            print(f"⏭️   SKIP — {name} (--skip-ingest)")
            continue
        if not run_step(name, cmd, dry_run=args.dry_run):
            failures.append(name)
            # QC is allowed to warn; anything else stops the line
            if "QC self-heal" not in name:
                break

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
