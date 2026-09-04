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
import atexit
import contextlib
import json
import os
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
    # 2026-05-28 (Michael "a complete audit ... daily ... checking that every
    # line of code has testing on it and successful ... must be in routines"):
    # run the pytest suite under coverage and write reports/test-result.json
    # so the daily systems audit can red-flag a code regression. This is an
    # OBSERVER step — run_audit_tests.py always exits 0 so a failing test
    # never blocks the client email; QC-052 + the audit decide severity.
    ("Test + coverage routine",  [PY, str(SCRIPTS / "run_audit_tests.py"), "--quiet"]),
    # 2026-06-24: re-parse the CACHED bodies with the current body_parser
    # BEFORE ingest. refresh_stage parses each email once at fetch time and
    # caches it; ingest consumes that cache, so a parser fix would otherwise
    # only reach newly-fetched mail and the back-catalog in the window would
    # stay stale until a manual reprocess. Running it here makes every parser
    # improvement self-apply to the whole window each fire (no manual
    # re-ingest, ever). Idempotent + atomic; best-effort (a failure falls back
    # to the existing cache and is caught by QC-059). The "break in data flow"
    # Michael named: upstream raw body fine, downstream parse stale → backfill.
    ("Parser backfill (reprocess cache)", [PY, str(SCRIPTS / "reprocess_bodies.py")]),
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
    # 2026-07-11: wire the M3.10-M3.12 insights engine (docs/INSIGHTS-DESIGN.md)
    # into the daily fire: rolling baselines → rule-based InsightsContext →
    # Opus narrative via the model router. Writes reports/insights/<date>.{json,
    # html} + the two embed snippets (insights-business.html for the staff
    # email, insights-full.html for the idealx.us audit). MUST run before
    # "Email body HTML" — gen_email embeds insights-business.html at build
    # time. The shim itself always exits 0 (missing API key / API down degrade
    # to a skipped narrative with the rule-based context still written).
    ("Daily insights (baselines + LLM)", [PY, str(SCRIPTS / "gen_insights.py")]),
    ("Email body HTML",          [PY, str(SCRIPTS / "gen_email.py")]),
    # 2026-07-10: CLIENT-facing daily service update (separate from the staff
    # email above). Builds reports/client-email-{body.html,subject.txt} every
    # fire; the wrapper/workflow send step is GATED by config.json
    # client_report.enabled (false = sample to Michael only; QC-065 pins the
    # approved recipients + scans the body for internal-analytics leaks).
    ("Client-facing email HTML", [PY, str(SCRIPTS / "gen_client_email.py")]),
    # 2026-08-05: the client WEEKLY rollup (Michael, on whether Lonny gets
    # tallied reports — he did not). BUILD ONLY. There is deliberately no send
    # step for it anywhere in this pipeline or in weekly.yml: client_weekly
    # ships enabled=false, and turning it on is a decision a person makes after
    # reading a rendered week, not something a pipeline step can drift into.
    # Building every fire is what makes that review possible.
    ("Client weekly rollup HTML", [PY, str(SCRIPTS / "gen_client_weekly.py")]),
    # 2026-07-10 (Michael "constantly updated instruction manual for users"):
    # the consumer manual, rebuilt every fire from live config so it can
    # never describe a stale system. Attached to the daily staff email.
    ("User manual HTML",         [PY, str(SCRIPTS / "gen_manual.py")]),
    # 2026-07-10: restore the executive summary — it previously fired only from
    # the retired Cloud PC wrapper (run_daily_laptop.cmd), so the GitHub-Actions
    # cutover silently killed it. As of 2026-07-16 the summary covers the PREVIOUS
    # week and its own cadence is Monday 5 AM ET (see weekly.yml); this inline run
    # self-skips (exit 0) on every day except Monday. The workflow attaches the
    # PDF only when fresh.
    ("Weekly executive summary", [PY, str(SCRIPTS / "gen_weekly_summary.py")]),
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
    # 2026-06-24: append finalized (terminal-state) rows to the durable Turso
    # historian so longitudinal stats survive past the 14-day fetch window
    # (Michael "i concur with building the data base for stats"). WRITE-ONLY
    # from the pipeline — never read back as authority — so it can't drift the
    # daily run. No-op (exit 0) when no Turso creds are configured.
    ("Historian (finalized → Turso)", [PY, str(SCRIPTS / "historian.py")]),
    # 2026-05-21: "Reconcile with ol-quote-tracker" step removed. The cross-check
    # assumed ol-quote-tracker independently ingests Hilmar bookings — a live API
    # probe proved it does not (0 Hilmar rows of 24 total quotes; QT tracks a
    # different client set). The reconcile produced only phantom drift. Retired
    # per Michael 2026-05-21. sync_to_quote_tracker (entity registry push) stays.
]

SKIPPABLE = {"Ingest (stage → requests)": "--skip-ingest"}

# Step classification — which failures abort the pipeline vs. log + continue.
#
# A CLIENT-BLOCKING step produces (or directly enables) the daily email +
# dashboard + PDF — the artifacts the 10-recipient distribution depends on.
# If one of these fails, stop; the wrapper must NOT proceed to send a stale
# or incomplete email.
#
# A BEST-EFFORT step is downstream telemetry / housekeeping / supplemental
# output. None of these affect what Hilmar receives. Their failures are
# logged + audited but never abort the pipeline. The wrapper proceeds to
# send the email regardless.
#
# Added 2026-06-01 after `Sync to ol-quote-tracker` returned 1 on TTSWW and
# aborted the entire wrapper — preventing outlook_send, qc_alert_if_needed,
# the audit email, AND backup_offline from running. A downstream-bonus step
# must never gate the upstream client deliverable.
BEST_EFFORT_STEPS = {
    # Code-health SELF-AUDIT (writes test-result.json for QC-052). NOT a client
    # deliverable — the real test gate is CI (test.yml on every push/PR). On the
    # Cloud PC pytest isn't installed so it skips instantly; on a host where
    # pytest IS installed (GitHub Actions) it runs the full suite + coverage, so
    # a slow run or TIMEOUT must never abort the client fire. The "always exits
    # 0" assumption above breaks on a timeout (rc=124, the process is KILLED),
    # which is exactly what aborted the 2026-06-30 GitHub production-fire — so
    # the safety has to be this classification, not the script's own exit code.
    "Test + coverage routine",
    "Parser backfill (reprocess cache)",  # cache refresh; on failure ingest uses existing cache, QC-059 catches drift
    "Sentry-driven QC actions",        # Sentry housekeeping; no client impact
    "Sentry Seer autofix trigger",     # autofix attempts; no client impact
    "Carrier scorecard PDFs",          # supplemental per-carrier PDFs; not in email
    "Daily insights (baselines + LLM)",  # supplemental narrative; shim self-degrades + exits 0, email ships without it
    "Client-facing email HTML",        # brand-new gated client artifact — must never block the staff email
    "Client weekly rollup HTML",       # gated, nobody receives it yet — it must never be why the staff email doesn't go
    "User manual HTML",                # consumer manual attachment; email ships without it on failure
    "Weekly executive summary",        # Friday-only supplemental; must never block the daily
    "Share to client_intelligence",    # SHARED-folder export; no client impact
    "Rate intelligence",               # idealx.us-only cheat sheet; not in email
    "Sync to ol-quote-tracker",        # downstream registry push; no client impact
    "Historian (finalized → Turso)",   # durable stats append; no client impact
}

# Option A hard gate (CLAUDE.md rule #2): the exit code qc_selfheal returns from
# its POST-PATCH run when the QC-039 parser-accuracy gate fails. The post-patch
# QC step returning THIS code is the one QC failure that is CLIENT-BLOCKING — it
# aborts the fire so a sub-95% report never ships. Any other QC exit code (crash,
# timeout) stays non-blocking so a QC bug can't drop the email. Must match
# qc_selfheal.QC039_GATE_BLOCK_RC (locked by tests/test_auditfix_qc039_gate.py).
QC039_GATE_BLOCK_RC = 39


# Sentry observability — initialized lazily so the pipeline runs fine
# even when sentry-sdk isn't installed or the DSN is missing.
sys.path.insert(0, str(SCRIPTS))
try:
    import sentry_setup as _sentry
except ImportError:
    _sentry = None


# 2026-05-28 (Michael — Sentry-9 "Cron failure: hilmar-daily-pipeline" firing
# at ~14:30 ET, 4.5h after the 10 AM fire): a step is hanging long enough
# that the cron monitor's max_runtime=60min check-in window expires before
# the pipeline calls finish. Network-bound steps (Sentry actions, Seer
# autofix, Turso sync, ol-quote-tracker login) are the prime suspects.
# Cap each step at STEP_TIMEOUT_S so a hung step can't drag the pipeline
# past the cron window. Override per-step via STEP_TIMEOUTS_S below.
STEP_TIMEOUT_S = 300            # 5 minutes default per step
STEP_TIMEOUTS_S = {
    # Quick steps don't need the full budget; long ones get more.
    "Backup snapshot":            60,
    "Test + coverage routine":    600,   # full suite + coverage on a runner is slow; best-effort, so a timeout never blocks the fire
    "Drift check (pre-QC)":       120,
    "QC self-heal (pre-patch)":   180,
    "QC self-heal (post-patch)":  180,
    "Sentry-driven QC actions":   240,   # polls 7 issues, 30s each worst-case
    "Sentry Seer autofix trigger": 180,
    "Daily insights (baselines + LLM)": 480,  # 4 sequential Opus calls + a 429-retry cascade can exceed the 300s default
    "Sync to ol-quote-tracker":   180,
    "Share to client_intelligence": 180,
}


#: Where the fire's transcript lands. The Cloud-PC wrapper
#: (deploy/run_daily_laptop.cmd) has always appended to this path with
#: `>> "%LOG%"`; on GitHub Actions NOTHING wrote it, so every consumer that
#: reads it has been inert on the only host that actually fires.
RUN_LOG = ROOT / "reports" / "run-log.txt"


def should_tee_run_log(env=None) -> bool:
    """Should run_pipeline write reports/run-log.txt itself this run?

    NOT on the Cloud PC. There the wrapper already redirects our stdout INTO
    that file, so teeing would write every line twice — and QC-021 counts
    markers in it.

    On an ephemeral runner there is no wrapper, and the absence is not
    cosmetic. Measured on production fire 33864188808 (2026-09-04):

        ##[warning]No files were found with the provided path:
        reports/run-log.txt reports/qc-result.json reports/test-result.json
        reports/coverage.json. No artifacts will be uploaded.

    — the "Upload run artifacts" step went green having uploaded NOTHING, and
    list_workflow_run_artifacts on that run returns total_count 0. So the one
    channel that carries a failed fire's evidence off the runner was empty,
    which is why "QC self-heal (post-patch) TIMEOUT @ 180s" ran 31 fires
    before anyone could see it, and why QC-057's PII-scrubbed body snippets
    — written expressly so a parser fix could be scoped from them — have
    never once been readable.

    The default keys off AZURE_STORAGE_CONNECTION_STRING, the same predicate
    qc_selfheal already calls _BLOB_HOST for "ephemeral runner". Explicit
    HILMAR_RUN_LOG_TEE=0/1 overrides it in both directions, so a Cloud PC that
    ever gains the connection string cannot start double-logging silently.
    """
    env = os.environ if env is None else env
    explicit = str(env.get("HILMAR_RUN_LOG_TEE", "")).strip().lower()
    if explicit in ("1", "true", "yes", "on"):
        return True
    if explicit in ("0", "false", "no", "off"):
        return False
    return bool(env.get("AZURE_STORAGE_CONNECTION_STRING"))


class RunLogTee:
    """Duplicate everything written to fd 1 and fd 2 into `path`, appending.

    MUST be done at the FILE-DESCRIPTOR level, not by swapping sys.stdout.
    run_step calls subprocess.run without capture, so every step's real
    output — the QC check lines, the tracebacks, the "TIMEOUT ... was killed"
    banner — is written by a CHILD process straight to the inherited fd. A
    Python-level sys.stdout wrapper never sees any of it, which would tee the
    orchestrator's own banners and lose the entire contents.

    Opens in APPEND mode: the wrapper's history and successive same-day fires
    accumulate, which is what QC-021 (searches for today's marker, then reads
    forward) and QC-055 (reads the tail) both expect.
    """

    def __init__(self, path):
        self.path = Path(path)
        self._fh = None
        self._thread = None
        self._saved = {}
        self._write_fd = None

    def __enter__(self):
        import threading
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "ab", buffering=0)
        read_fd, self._write_fd = os.pipe()
        # Keep the ORIGINAL destinations so the runner's own console (and the
        # Actions log) still gets every line — a tee that swallows its input
        # would trade one blind channel for another.
        for fd in (1, 2):
            self._saved[fd] = os.dup(fd)
        sys.stdout.flush()
        sys.stderr.flush()
        real_out = self._saved[1]

        def _pump():
            with os.fdopen(read_fd, "rb", buffering=0) as pipe:
                while True:
                    chunk = pipe.read(4096)
                    if not chunk:
                        break
                    for sink in (real_out, None):
                        try:
                            if sink is None:
                                self._fh.write(chunk)
                            else:
                                os.write(sink, chunk)
                        except OSError:
                            pass

        self._thread = threading.Thread(target=_pump, name="run-log-tee",
                                        daemon=True)
        self._thread.start()
        os.dup2(self._write_fd, 1)
        os.dup2(self._write_fd, 2)
        # Line buffering keeps our own banners interleaved with the children's
        # output in the order they actually happened.
        with contextlib.suppress(Exception):
            sys.stdout.reconfigure(line_buffering=True)
            sys.stderr.reconfigure(line_buffering=True)
        return self

    def __exit__(self, *exc):
        # ORDER IS LOAD-BEARING and the first draft got it wrong: it closed
        # the saved console fd before joining the pump, so anything still in
        # flight hit a closed descriptor and vanished from the console while
        # landing in the file. Measured — "PARENT: after child" reached the
        # log and never reached the terminal.
        #
        #   1. flush our own buffers into the pipe
        #   2. restore fds 1/2, which closes the pipe's duplicated write ends
        #   3. close the original write end — only now can the reader see EOF
        #   4. join the pump; it may still be writing to the saved console fd
        #   5. only then close the saved fds and the file
        with contextlib.suppress(Exception):
            sys.stdout.flush()
            sys.stderr.flush()
        for fd, saved in self._saved.items():
            with contextlib.suppress(OSError):
                os.dup2(saved, fd)
        with contextlib.suppress(OSError):
            os.close(self._write_fd)
        if self._thread is not None:
            self._thread.join(timeout=5)
        for saved in self._saved.values():
            with contextlib.suppress(OSError):
                os.close(saved)
        with contextlib.suppress(Exception):
            self._fh.close()
        return False


def run_log_header(started, host: str) -> str:
    """The wrapper-compatible preamble for one fire.

    QC-021 locates today's fire by finding either %m/%d/%Y or %Y-%m-%d in the
    log and then reading FORWARD, so both spellings go in. `PY:` is the line
    QC-055's sibling diagnostics and the qc_actions_from_sentry runbook tell
    an operator to read for the interpreter that actually ran.
    """
    return (
        "\n" + "=" * 70 + "\n"
        f"HILMAR FIRE {started.strftime('%Y-%m-%d')} "
        f"({started.strftime('%m/%d/%Y')} {started.strftime('%H:%M:%S')}) "
        f"host={host}\n"
        f"PY: {PY}\n"
        + "=" * 70 + "\n"
    )


def run_step(name, cmd, dry_run=False, extra_env=None):
    print()
    # QC-021 reads these markers out of the run log to name the step a dead
    # fire stopped at: re.findall(r"^---\s*(.+?)\s*---\s*$", ...). The
    # wrapper writes them for its own steps; without one per PIPELINE step the
    # best QC-021 could ever say was "run_pipeline".
    print(f"--- {name} ---")
    print("═" * 70)
    print(f"▶  {name}")
    print(f"   cmd: {' '.join(cmd)}")
    if extra_env:
        print(f"   env: {extra_env}")
    print("═" * 70)
    if dry_run:
        print("   (dry-run — skipped)")
        return 0

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
    # UNBUFFERED, ALWAYS. A step that exceeds its timeout is SIGKILLed by
    # subprocess.run, and SIGKILL discards whatever is still sitting in the
    # child's stdio buffer. Python block-buffers stdout whenever it is not a
    # TTY, which on a runner it never is — so the output of a killed step was
    # thrown away precisely when it was the only evidence of what went wrong.
    #
    # MEASURED, same harness, only this variable differing:
    #   child prints a line, sleeps past the timeout, is killed
    #   buffered          -> line LOST
    #   PYTHONUNBUFFERED=1 -> line SURVIVES
    #
    # That is the whole reason QC-057's PII-scrubbed body snippets have never
    # been readable: the post-patch pass computed them and died holding them.
    # Fire 33864188808 shows both QC passes killed mid-write, their last log
    # line physically fused with the next process's first line.
    sub_env["PYTHONUNBUFFERED"] = "1"
    if extra_env:
        sub_env.update(extra_env)

    timeout_s = STEP_TIMEOUTS_S.get(name, STEP_TIMEOUT_S)
    step_started = _time.monotonic()
    timed_out = False
    try:
        try:
            result = subprocess.run(cmd, cwd=str(ROOT), env=sub_env, timeout=timeout_s)
            rc = result.returncode
        except subprocess.TimeoutExpired:
            timed_out = True
            rc = 124  # GNU `timeout` convention — distinguishable from app exit codes
            print(f"⏱️  TIMEOUT — {name} exceeded {timeout_s}s limit and was killed")
        step_elapsed = _time.monotonic() - step_started

        # Per-step duration metric — powers the Sentry dashboard heatmap
        # showing which step is slowest. Tagged by step name so we can
        # group / filter.
        if _sentry is not None:
            try:
                status = "timeout" if timed_out else ("ok" if rc == 0 else "failed")
                _sentry.metric_distribution(
                    "pipeline.step_duration_s",
                    step_elapsed,
                    step=name,
                    status=status,
                )
            except Exception:
                pass

        if rc != 0:
            label = f"TIMEOUT @ {timeout_s}s" if timed_out else f"exited {rc}"
            print(f"❌ FAIL — {name} {label}")
            if _sentry is not None:
                with contextlib.suppress(Exception):
                    _sentry.capture_qc_error(
                        "pipeline.step_failure",
                        f"{name} {label}",
                    )
            return rc
        return 0
    finally:
        if txn_cm is not None:
            with contextlib.suppress(Exception):
                txn_cm.__exit__(None, None, None)


STEP_HISTORY = ROOT / "reports" / "step-history.json"


def _record_step_history(failed_steps, *, path=None, keep=30):
    """Append this fire's failed-step list to step-history.json (rolling
    `keep` entries). Best-effort — a write failure never affects the fire."""
    path = path or STEP_HISTORY
    try:
        from datetime import timezone
        hist = []
        if path.exists():
            try:
                hist = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(hist, list):
                    hist = []
            except Exception:
                hist = []
        hist.append({"ts": datetime.now(timezone.utc).isoformat(),
                     "failed": sorted(set(failed_steps))})
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(hist[-keep:], indent=2), encoding="utf-8")
    except Exception:
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-ingest", action="store_true",
                    help="Skip the ingest step (use when rerunning against existing staged data)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print the steps that would run without executing them")
    args = ap.parse_args()

    started = datetime.now()

    # Start the transcript BEFORE anything else can fail. A --dry-run prints
    # what it would do and must not append a fire header to the real log.
    if should_tee_run_log() and not args.dry_run:
        _tee = RunLogTee(RUN_LOG)
        _tee.__enter__()
        # atexit, not `with`: main() ends via sys.exit() on a blocking
        # failure, and the transcript of a FAILED fire is the one that has to
        # survive. SystemExit still runs atexit handlers.
        atexit.register(_tee.__exit__, None, None, None)
        print(run_log_header(started, host="github-actions"), end="")

    # Initialize Sentry FIRST so any subsequent error gets captured.
    if _sentry is not None:
        _sentry.init(component="run_pipeline")

    print(f"🚀 Hilmar Tracker pipeline — started {started.isoformat(timespec='seconds')}")
    print(f"   Repo: {ROOT}")
    print(f"   Python: {PY}")

    # Sentry Cron heartbeat — `start` check-in. Sentry auto-creates the
    # monitor (slug=hilmar-daily-pipeline) on first call. Schedule is
    # "7 18 * * 1-5" America/New_York (Mon-Fri 6 PM ET). If the
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
        # Force-align the live Sentry monitor's schedule to our config via the
        # REST API — the check-in's monitor_config does NOT reliably update an
        # existing monitor, which left it stuck on the old 10 AM schedule and
        # paging 'missed check-in' daily (2026-06-17). Best-effort, never blocks.
        with contextlib.suppress(Exception):
            _sentry.ensure_monitor_schedule()

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
    gate_blocked = False
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
        # The dedicated test.yml CI workflow is the authoritative suite gate
        # (it runs the full pytest suite on every push/PR). On a CI runner the
        # in-pipeline coverage run is REDUNDANT and slow: it runs the whole
        # suite + coverage and TIMES OUT even at 600s, which — though it's
        # best-effort and no longer aborts the fire (2026-06-30 fix) — still
        # wastes ~10 min and spams Sentry (pipeline.step_failure ×N) every fire.
        # daily.yml sets this for the GitHub production-fire; the Cloud PC leaves
        # it unset (there pytest isn't installed, so the step skips instantly).
        if name == "Test + coverage routine" and os.environ.get("HILMAR_SKIP_PIPELINE_TESTS") == "1":
            print()
            print(f"⏭️   SKIP — {name} (HILMAR_SKIP_PIPELINE_TESTS=1; test.yml CI is the authoritative suite gate)")
            continue
        _rc = run_step(name, cmd, dry_run=args.dry_run, extra_env=extra_env)
        if _rc != 0:
            failures.append(name)
            # Option A hard gate (CLAUDE.md rule #2): the ONE QC failure that is
            # client-blocking is the post-patch parser-accuracy gate (QC-039).
            # qc_selfheal returns QC039_GATE_BLOCK_RC for it and has ALREADY
            # fired the out-of-band alarm; abort so the wrapper never ships a
            # sub-95% report. Any other QC exit code (crash/timeout) stays
            # non-blocking below, so a QC engine bug can't drop the client email.
            if name == "QC self-heal (post-patch)" and _rc == QC039_GATE_BLOCK_RC:
                print("❌ QC-039 parser-accuracy gate FAILED post-patch — BLOCKING "
                      "the client ship (CLAUDE.md rule #2); aborting before email build.")
                gate_blocked = True
                break
            # Non-blocking failure classes:
            #   - QC self-heal: WARN-grade findings expected (already-handled exception)
            #   - Best-effort steps: telemetry + housekeeping, never blocks client output
            # Anything else (Ingest, Drift, Carrier patch, Dashboard, PDF, Email
            # body) is client-blocking — failing a step there means the email
            # would be stale/broken, so STOP. The wrapper sees rc=1 and skips
            # outlook_send (which is the correct behaviour for those steps).
            if "QC self-heal" in name or name in BEST_EFFORT_STEPS:
                print(f"⚠️  Best-effort step '{name}' failed — continuing pipeline")
                continue
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

    # Cron heartbeat — finish check-in. THE CHECK-IN REPORTS THE SAME VERDICT
    # AS THE EXIT CODE, and it did not until 2026-09-04.
    #
    # It used `success=not failures` — the FULL list, including best-effort
    # steps — while the exit code below is gated on `blocking_failures` only.
    # So a best-effort step failing sent Sentry status=error on a run that
    # exited 0 and shipped the report. That is the whole of
    # HILMAR-DAILY-TRACKER-H "Cron failure: hilmar-daily-pipeline" (16
    # occurrences): every fire the post-patch QC self-heal timed out, Sentry
    # paged a pipeline failure for a pipeline that succeeded, and the next
    # fire's ok check-in auto-resolved it — a daily alarm that named the
    # wrong thing and trained its reader to ignore it.
    #
    # A monitor that cries failure on success is worse than no monitor. The
    # best-effort failure is NOT silenced: it still raises
    # pipeline.step_failure to Sentry (see _run_step) and still prints in the
    # summary below. This changes only what the CRON check-in claims about
    # the run as a whole.
    _cron_blocking = [f for f in failures
                      if "QC self-heal" not in f and f not in BEST_EFFORT_STEPS]
    if _sentry is not None and cron_id is not None:
        with contextlib.suppress(Exception):
            _sentry.finish_cron_checkin(cron_id, success=not _cron_blocking)

    # Flush Sentry queue before exit so events aren't lost on quick pipeline runs
    if _sentry is not None:
        try:
            import sentry_sdk
            sentry_sdk.flush(timeout=5)
        except Exception:
            pass

    elapsed = (datetime.now() - started).total_seconds()
    # Append this fire's failed-step list to reports/step-history.json so
    # QC-063 can detect a best-effort/observer step that has been dead for
    # SEVERAL fires (which, per-fire, looks identical to a one-day blip — the
    # silent-degradation-for-a-week failure mode). Best-effort; never blocks.
    _record_step_history(failures)
    # Partition failures into client-blocking vs best-effort. The pipeline's
    # exit code is gated only on client-blocking ones so a single bad
    # telemetry/sync step can't drop the daily email.
    # SAME list the cron check-in used above — computed once, so the check-in
    # and the exit code cannot drift apart again.
    blocking_failures = list(_cron_blocking)
    best_effort_failures = [f for f in failures if f not in blocking_failures]
    # Option A (CLAUDE.md rule #2): the post-patch parser-accuracy gate is
    # client-blocking even though it's a "QC self-heal" step — reclassify it so
    # the pipeline exits non-zero and the wrapper skips the send.
    if gate_blocked:
        _gate_step = "QC self-heal (post-patch)"
        if _gate_step in best_effort_failures:
            best_effort_failures.remove(_gate_step)
        blocking_failures.append(_gate_step + ": QC-039 parser-accuracy gate")
    print()
    print("═" * 70)
    if blocking_failures:
        print(f"❌ PIPELINE FAILED in {elapsed:.1f}s")
        print(f"   Client-blocking failures: {', '.join(blocking_failures)}")
        if best_effort_failures:
            print(f"   Also (best-effort): {', '.join(best_effort_failures)}")
        # Same wording the Cloud-PC wrapper writes, so QC-021 parses one
        # format on both hosts rather than two.
        print("Pipeline exit code: 1")
        sys.exit(1)
    if best_effort_failures:
        # Successful client deliverable, but telemetry/sync misbehaved. Exit 0
        # so the wrapper proceeds to send the email; QC-052 + the audit will
        # surface the warnings at the next pass.
        print(f"✅ PIPELINE COMPLETE in {elapsed:.1f}s  "
              f"(with {len(best_effort_failures)} best-effort warning"
              f"{'s' if len(best_effort_failures) != 1 else ''})")
        print(f"   Best-effort failures (non-blocking): {', '.join(best_effort_failures)}")
    else:
        print(f"✅ PIPELINE COMPLETE in {elapsed:.1f}s")
    print(f"   Reports: {ROOT / 'reports'}")
    print(f"   Data:    {ROOT / 'tracking-data-v2.json'}")
    print("Pipeline exit code: 0")

    # FOOTGUN GUARD (2026-06-25): run_pipeline BUILDS but does not SEND — the
    # wrapper's outlook_send step does. A hand-run that stops here produces
    # artifacts and NO email, and the "✅ COMPLETE" above reads like success
    # (exactly what bit Michael: ran the pipeline, saw COMPLETE, no report
    # landed). If today's full-distribution send-flag isn't present, say so
    # LOUDLY so "built" is never mistaken for "shipped".
    try:
        from zoneinfo import ZoneInfo as _ZI
        _today = datetime.now(_ZI("America/New_York")).date().isoformat()
    except Exception:
        _today = datetime.now().date().isoformat()
    _sent_flag = ROOT / "reports" / f"sent-{_today}.flag"
    if not _sent_flag.exists():
        print()
        print("⚠️ " + "─" * 66)
        print("⚠️  BUILD COMPLETE — NOTHING WAS SENT.")
        print("⚠️  run_pipeline only builds the report; it does not email it.")
        print("⚠️  To ship today's report:")
        print("⚠️    deploy\\run_daily_laptop.cmd            (full daily fire + send)")
        print("⚠️    — or, to send only to yourself first —")
        print("⚠️    python scripts\\outlook_send.py daily --to michael.deitchman@idealx.us \\")
        print("⚠️        --force --no-flag --subject-from-file reports\\email-subject.txt \\")
        print("⚠️        --body-from-file reports\\email-body.html \\")
        print("⚠️        --attach reports\\hilmar-dashboard.html reports\\hilmar-report.pdf")
        print("⚠️ " + "─" * 66)


if __name__ == "__main__":
    main()
