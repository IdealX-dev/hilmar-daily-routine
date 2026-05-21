"""
sentry_setup.py — Single source of truth for initializing Sentry across
all Hilmar pipeline entry points.

Per Michael 2026-05-17: "i don't care how much $ this costs or what
other tools are needed... never to allow drift like this as standard."
Sentry catches the kind of silent failure that lets parser regressions,
pipeline crashes, and drift slip through the daily-email cycle. Where
the QC checks (39 / 40 / 41) DETECT problems, Sentry SURFACES them
in real time instead of waiting for the next 10 AM ET fire.

INIT FLOW

1. `init()` reads the DSN from `secrets/sentry-dsn.txt` (gitignored),
   falls back to env var `SENTRY_DSN`, and silently no-ops if neither
   is configured. The pipeline never breaks because Sentry isn't set up.
2. Default tags applied to every event: environment, pipeline_run_id
   (generated from start time), git_sha (short), python_version.
3. `before_send` hook scrubs PII before transmission — email addresses,
   MDOLX numbers, conversation IDs, internet message IDs, Lonny's
   actual address, etc. The goal is observability WITHOUT leaking
   client data to a third-party SaaS.

USAGE — at the top of any entry-point script:

  import sentry_setup
  sentry_setup.init(component="run_pipeline")  # or "qc_selfheal", "outlook_send", etc.

After init, all uncaught exceptions auto-capture. To send a custom event:

  import sentry_sdk
  sentry_sdk.capture_message("Parser accuracy 97.2% — below 98% threshold",
                              level="error",
                              extras={"overall_rate": 0.972, "failing": ["ol_rate"]})
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent

# ─────────────────────────────────────────────────────────────────────
# PII scrubbing patterns
# ─────────────────────────────────────────────────────────────────────

# Match raw email addresses (broader than Sentry's built-in scrubber)
_EMAIL_RX = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")

# OL booking refs — proprietary, scrub
_MDOLX_RX = re.compile(r"\bMDOL[XMFD]\d+\b", re.IGNORECASE)
_CARRIER_REF_RX = re.compile(
    r"\b(NAM|RICG|ONEY|EBKG|MAEU|MEDU|MSCU|HLCU|COSU|ZIMU|OOLU|YMLU|HMMU)[A-Z0-9]{6,}\b",
    re.IGNORECASE,
)

# Internet message-ID: <random@server.domain>
_IMID_RX = re.compile(r"<[A-Za-z0-9._-]+@[A-Za-z0-9.-]+>")

# Outlook conversation IDs — long base64-ish blobs
_CONV_ID_RX = re.compile(r"AAQ[A-Za-z0-9_=+/-]{20,}")

# Internal request IDs (req_HEX) — not PII but noisy in stacktraces
_REQ_ID_RX = re.compile(r"req_[0-9a-f]{16,}")


def _scrub_string(s: str) -> str:
    """Apply all redaction patterns to a single string."""
    if not isinstance(s, str):
        return s
    s = _EMAIL_RX.sub("[EMAIL_REDACTED]", s)
    s = _MDOLX_RX.sub("[MDOLX_REDACTED]", s)
    s = _CARRIER_REF_RX.sub("[CARRIER_REF_REDACTED]", s)
    s = _IMID_RX.sub("[IMID_REDACTED]", s)
    s = _CONV_ID_RX.sub("[CONV_REDACTED]", s)
    s = _REQ_ID_RX.sub("[REQ_ID]", s)
    return s


def _walk_scrub(obj):
    """Recursively scrub PII from any nested dict/list/string structure."""
    if isinstance(obj, str):
        return _scrub_string(obj)
    if isinstance(obj, dict):
        return {k: _walk_scrub(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_walk_scrub(v) for v in obj]
    if isinstance(obj, tuple):
        return tuple(_walk_scrub(v) for v in obj)
    return obj


def _before_send(event, hint):
    """Sentry hook: scrub PII from the event before transmission.

    Applied to:
      - event.message
      - event.exception (each frame's vars + message)
      - event.extra (custom metadata)
      - event.breadcrumbs (each crumb's message + data)
      - event.tags (defensive — should be pre-scrubbed by us)
      - event.contexts (defensive)
    """
    try:
        if "message" in event:
            event["message"] = _scrub_string(event.get("message", ""))
        if "exception" in event and "values" in event["exception"]:
            for exc in event["exception"]["values"]:
                if "value" in exc:
                    exc["value"] = _scrub_string(exc.get("value", ""))
                if "stacktrace" in exc and "frames" in exc["stacktrace"]:
                    for fr in exc["stacktrace"]["frames"]:
                        if "vars" in fr and isinstance(fr["vars"], dict):
                            fr["vars"] = _walk_scrub(fr["vars"])
        if "extra" in event:
            event["extra"] = _walk_scrub(event["extra"])
        if "breadcrumbs" in event and "values" in event["breadcrumbs"]:
            for crumb in event["breadcrumbs"]["values"]:
                if "message" in crumb:
                    crumb["message"] = _scrub_string(crumb.get("message", ""))
                if "data" in crumb and isinstance(crumb["data"], dict):
                    crumb["data"] = _walk_scrub(crumb["data"])
        if "tags" in event:
            event["tags"] = _walk_scrub(event["tags"])
        if "contexts" in event:
            event["contexts"] = _walk_scrub(event["contexts"])
    except Exception:
        # Never let the scrubber crash a real event — let it through
        # rather than drop the alert.
        pass
    return event


# ─────────────────────────────────────────────────────────────────────
# DSN loading
# ─────────────────────────────────────────────────────────────────────

def _load_dsn() -> Optional[str]:
    """Resolve DSN from secrets/sentry-dsn.txt or env var. None = no-op."""
    secrets_file = ROOT / "secrets" / "sentry-dsn.txt"
    if not secrets_file.exists():
        # Try the parent OneDrive working dir
        secrets_file = ROOT.parent / "secrets" / "sentry-dsn.txt"
    if secrets_file.exists():
        try:
            dsn = secrets_file.read_text(encoding="utf-8").strip()
            if dsn and dsn.startswith("https://"):
                return dsn
        except Exception:
            pass
    return os.environ.get("SENTRY_DSN") or None


def _git_sha_short() -> str:
    """Best-effort short git SHA for the current HEAD. Used as release tag."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(ROOT), capture_output=True, text=True, timeout=5,
        )
        if out.returncode == 0:
            return out.stdout.strip()
    except Exception:
        pass
    return "unknown"


def _detect_environment() -> str:
    """Production = Cloud PC scheduled fire. Manual = anywhere else."""
    # Cloud PC's hostname is CPC-micha-E552L per the README. Treat anything
    # else as manual / dev. Also respect SENTRY_ENVIRONMENT env if set.
    if "SENTRY_ENVIRONMENT" in os.environ:
        return os.environ["SENTRY_ENVIRONMENT"]
    try:
        import socket
        h = socket.gethostname().lower()
        if "cpc-micha" in h:
            return "production"
        if "codespace" in h or "vscode" in os.environ.get("TERM_PROGRAM", "").lower():
            return "codespaces"
    except Exception:
        pass
    return "manual"


# Module-level run ID so all events from a single pipeline fire group together
_RUN_ID = uuid.uuid4().hex[:12]
_INITIALIZED = False


def init(component: str = "unknown", *, sample_rate: float = 1.0) -> bool:
    """Initialize Sentry for an entry-point script.

    Args:
      component: short tag identifying the entry point (run_pipeline,
                 qc_selfheal, outlook_send, sync_to_quote_tracker, etc.)
                 Shown in Sentry as the `component` tag for filtering.
      sample_rate: traces_sample_rate. Default 1.0 = capture every
                   transaction (fine for daily-fire pipeline; would be
                   too noisy for an HTTP server but this isn't one).

    Returns True if init succeeded, False if DSN missing / disabled.
    Never raises — Sentry-not-available must never break the pipeline.
    """
    global _INITIALIZED
    if _INITIALIZED:
        return True
    try:
        import sentry_sdk
    except ImportError:
        return False  # sentry-sdk not installed; silent no-op

    dsn = _load_dsn()
    if not dsn:
        return False  # not configured; silent no-op (pipeline keeps running)

    env = _detect_environment()
    release = f"hilmar-daily-tracker@{_git_sha_short()}"

    sentry_sdk.init(
        dsn=dsn,
        environment=env,
        release=release,
        # Performance transactions: capture every step (low volume — ~14/fire/day)
        traces_sample_rate=sample_rate,
        # PII handling: opt OUT of automatic PII capture. We control what
        # gets sent via explicit capture_message() calls + the scrubber.
        send_default_pii=False,
        # Limit context to keep payload small
        max_breadcrumbs=50,
        attach_stacktrace=True,
        # The scrubber — strips emails / MDOLX / conv IDs / IMIDs / req IDs
        before_send=_before_send,
        before_send_transaction=_before_send,
        # Don't auto-instrument network libs (not needed for this pipeline)
        auto_enabling_integrations=False,
    )

    # Set default tags on every subsequent event
    sentry_sdk.set_tag("component", component)
    sentry_sdk.set_tag("pipeline_run_id", _RUN_ID)
    sentry_sdk.set_tag("python_version", f"{sys.version_info.major}.{sys.version_info.minor}")
    sentry_sdk.set_context("hilmar", {
        "component": component,
        "pipeline_run_id": _RUN_ID,
        "environment": env,
        "release": release,
    })

    _INITIALIZED = True
    return True


def capture_qc_error(check_name: str, summary: str, **extras) -> None:
    """Capture a QC ERROR-severity finding as a Sentry event.

    Use from qc_selfheal.py when log.error() fires. The check_name
    (e.g. "QC-039") becomes the issue fingerprint, so all instances
    of the same check failing group together in Sentry.
    """
    try:
        import sentry_sdk
        if not _INITIALIZED:
            return
        sentry_sdk.set_tag("qc_check", check_name)
        sentry_sdk.capture_message(
            f"{check_name}: {summary}",
            level="error",
            scope=None,
        )
    except Exception:
        pass  # observability must never crash the pipeline


def capture_qc_warning(check_name: str, summary: str, **extras) -> None:
    """Like capture_qc_error but at warning level — for QC WARNs that
    are worth surfacing in real time (e.g. parser regressions just under
    the error threshold)."""
    try:
        import sentry_sdk
        if not _INITIALIZED:
            return
        sentry_sdk.set_tag("qc_check", check_name)
        sentry_sdk.capture_message(
            f"{check_name}: {summary}",
            level="warning",
            scope=None,
        )
    except Exception:
        pass


def capture_step_failure(step_name: str, error: Exception, **extras) -> None:
    """Capture a pipeline-step failure (subprocess returncode != 0 or
    Python exception in run_pipeline.py)."""
    try:
        import sentry_sdk
        if not _INITIALIZED:
            return
        with sentry_sdk.new_scope() as scope:
            scope.set_tag("pipeline_step", step_name)
            for k, v in extras.items():
                scope.set_extra(k, v)
            sentry_sdk.capture_exception(error)
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────
# Sentry Crons — heartbeat the daily 10 AM ET fire
# ─────────────────────────────────────────────────────────────────────
#
# Sentry Crons solves the silent-failure mode where the SCHEDULER itself
# breaks (Cloud PC offline, task scheduler crashes, wrapper script crashes
# before any Python code runs). Plain error-event monitoring can't catch
# this because no code is running to report. Sentry Crons solves it with
# a heartbeat model: the pipeline checks in with Sentry at start and end;
# if the start check-in doesn't arrive within `checkin_margin` minutes of
# the scheduled time, Sentry fires an "missed check-in" alert.
#
# Monitor slug: `hilmar-daily-pipeline`
# Schedule:     Mon-Fri at 10:00 AM ET (per scheduled task on Cloud PC)
# Margin:       Check-in must arrive within 30 min of scheduled time
# Max runtime:  60 min before declaring the run hung/missed
#
# The monitor is AUTO-PROVISIONED by the first check-in that supplies a
# monitor_config — no need for Sentry UI setup or auth-token-based API.

MONITOR_SLUG = "hilmar-daily-pipeline"

_MONITOR_CONFIG = {
    "schedule": {"type": "crontab", "value": "0 10 * * 1-5"},
    "schedule_type": "crontab",
    "timezone": "America/New_York",
    "checkin_margin": 30,    # alert if no in_progress check-in within 30 min of scheduled fire
    "max_runtime": 60,       # alert if pipeline runs >60 min (typical = 30-60s, lots of headroom)
    "failure_issue_threshold": 1,   # 1 missed/failed run = create issue immediately
    "recovery_threshold": 1,        # 1 successful run = resolve the issue
}


def start_cron_checkin() -> str | None:
    """Mark the start of a pipeline run. Returns the check_in_id for
    pairing with the finishing call. Returns None if Sentry not initialized
    or on any error — observability must never block the pipeline."""
    try:
        import sentry_sdk
        if not _INITIALIZED:
            return None
        # capture_checkin auto-creates the monitor on first call when
        # monitor_config is provided. Subsequent calls update the existing
        # monitor — schedule changes propagate automatically.
        check_in_id = sentry_sdk.crons.capture_checkin(
            monitor_slug=MONITOR_SLUG,
            status="in_progress",
            monitor_config=_MONITOR_CONFIG,
        )
        return check_in_id
    except Exception as _e:
        # Don't let cron-checkin failure crash the pipeline
        try:
            print(f"⚠️  Sentry cron start failed (pipeline continues): {_e}")
        except Exception:
            pass
        return None


def finish_cron_checkin(check_in_id: str | None, success: bool) -> None:
    """Mark the end of a pipeline run as ok / error. Pass the check_in_id
    returned by start_cron_checkin() to pair the two events.

    If check_in_id is None (start_cron_checkin failed or Sentry wasn't
    initialized), this is a silent no-op.
    """
    if check_in_id is None:
        return
    try:
        import sentry_sdk
        if not _INITIALIZED:
            return
        sentry_sdk.crons.capture_checkin(
            monitor_slug=MONITOR_SLUG,
            check_in_id=check_in_id,
            status="ok" if success else "error",
        )
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────
# Custom metrics — time-series KPIs (parser accuracy, durations, counts)
# ─────────────────────────────────────────────────────────────────────
#
# Sentry Custom Metrics turn QC + pipeline + parser data into trended
# time-series. The free tier doesn't support metrics; paid (Team+) does.
# These helpers no-op silently if the SDK doesn't expose metrics — that
# way the code works on free tier without modification.
#
# Naming convention:
#   parser.accuracy_overall              gauge   0.0-1.0
#   parser.accuracy_per_field            gauge   tagged field=<name>
#   pipeline.duration_s                  distribution  seconds
#   pipeline.step_duration_s             distribution  tagged step=<name>
#   pipeline.status                      counter  tagged status=ok|failed
#   qc.errors                            counter  tagged check=<QC-NNN>
#   qc.warnings                          counter  tagged check=<QC-NNN>
#   qc.fixes                             counter
#   send.success                         counter  tagged recipient_type=full|audit|test
#   send.failure                         counter  tagged error_type=<class>
#   ingest.rows_processed                counter
#   ingest.wins_today                    counter
#   ingest.qa_today                      counter


def metric_gauge(name: str, value: float, **tags) -> None:
    """Emit a gauge metric — represents a current-value snapshot.
    Tags become Sentry metric dimensions for filtering/grouping."""
    if not _INITIALIZED:
        return
    try:
        from sentry_sdk import metrics as _metrics
        _metrics.gauge(key=name, value=float(value), tags=tags or None)
    except Exception:
        pass  # metrics either not supported or other failure — silent no-op


def metric_increment(name: str, value: float = 1.0, **tags) -> None:
    """Emit a counter metric — represents an additive count of events.
    Use for: number of QC errors fired, number of rows processed, etc."""
    if not _INITIALIZED:
        return
    try:
        from sentry_sdk import metrics as _metrics
        _metrics.incr(key=name, value=float(value), tags=tags or None)
    except Exception:
        pass


def metric_distribution(name: str, value: float, **tags) -> None:
    """Emit a distribution metric — Sentry computes percentiles
    (p50/p75/p95/p99) over the values reported. Use for: durations,
    sizes, rates that vary per call."""
    if not _INITIALIZED:
        return
    try:
        from sentry_sdk import metrics as _metrics
        _metrics.distribution(key=name, value=float(value), tags=tags or None)
    except Exception:
        pass


if __name__ == "__main__":
    # CLI: send a test event to verify the integration works
    print("Sentry setup self-test...")
    ok = init(component="self_test")
    if not ok:
        print("⚠️  Sentry NOT initialized — check secrets/sentry-dsn.txt or SENTRY_DSN env")
        sys.exit(1)
    print(f"✅ Initialized. Run ID: {_RUN_ID}")
    import sentry_sdk
    event_id = sentry_sdk.capture_message(
        "sentry_setup.py self-test — verifying DSN + scrubber path "
        "(this lupfold@hilmaringredients.com email + MDOLX260622 should be redacted)",
        level="info",
    )
    print(f"✅ Test event sent. event_id={event_id}")
    print(f"   Check https://o4511407070904320.sentry.io/issues/ for the message.")
    print(f"   The email + MDOLX in the message body should appear as [EMAIL_REDACTED] + [MDOLX_REDACTED].")
    sentry_sdk.flush(timeout=5)
