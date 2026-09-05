"""
sentry_setup.py — Single source of truth for initializing Sentry across
all Hilmar pipeline entry points.

Per Michael 2026-05-17: "i don't care how much $ this costs or what
other tools are needed... never to allow drift like this as standard."
Sentry catches the kind of silent failure that lets parser regressions,
pipeline crashes, and drift slip through the daily-email cycle. Where
the QC checks (39 / 40 / 41) DETECT problems, Sentry SURFACES them
in real time instead of waiting for the next 6 PM ET fire.

INIT FLOW

1. `init()` reads the DSN from `secrets/sentry-dsn.txt` (gitignored),
   falls back to env var `SENTRY_DSN`, and silently no-ops if neither
   is configured. The pipeline never breaks because Sentry isn't set up.
2. Process-scope tags on every event: component, pipeline_run_id (one
   per process), python_version. `environment` and `release` (which
   carries the short git sha) ride as event attributes, not tags — there
   is no `git_sha` tag. `init()` sets them and
   tests/test_sentry_qc_fingerprint.py pins them by running the real init.
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

import contextlib
import os
import re
import subprocess
import sys
import uuid
from pathlib import Path

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


def _scrub_frame_vars(stacktrace) -> None:
    """Scrub the local-variable snapshot of every frame in one stacktrace
    dict, in place. Shared by the `exception` and `threads` interfaces so
    the scrubber cannot walk one and not the other."""
    if not isinstance(stacktrace, dict):
        return
    for fr in stacktrace.get("frames") or []:
        if isinstance(fr, dict) and isinstance(fr.get("vars"), dict):
            fr["vars"] = _walk_scrub(fr["vars"])


def _before_send(event, hint):
    """Sentry hook: scrub PII from the event before transmission.

    Two kinds of interface, and every key in `_PII_BEARING_KEYS` (the
    fail-closed drop list) is one or the other. `tests/test_auditfix_sentry_
    scrubber_failclosed.py` parametrises over that tuple, so a key added to
    the drop list without a normal-path scrub goes red — `logentry` sat in
    the drop list and NOT here from the day the list was written, and the
    stdlib LoggingIntegration shipped a raw email through its `message`,
    `formatted` and `params` (measured 2026-09-05, real SDK).

      - FREE TEXT — scrubbed wholesale (`_FREE_TEXT_KEYS`): message,
        logentry, extra, request, user, plus tags and contexts defensively.
      - STRUCTURED stack / crumb interfaces — scrubbed at the fields that
        carry DATA, never the ones that carry CODE: exception (value + frame
        vars), threads (frame vars), breadcrumbs (message + data). Walking a
        stacktrace wholesale would redact `namedtuple` in a context line:
        `_CARRIER_REF_RX` is case-insensitive `NAM` + six characters.
    """
    try:
        for key in _FREE_TEXT_KEYS:
            if key in event:
                event[key] = _walk_scrub(event[key])
        if "exception" in event and "values" in event["exception"]:
            for exc in event["exception"]["values"]:
                if "value" in exc:
                    exc["value"] = _scrub_string(exc.get("value", ""))
                _scrub_frame_vars(exc.get("stacktrace"))
        # `attach_stacktrace=True` puts a capture_message's stack under
        # `threads`, NOT `exception` — and its frame locals hold the RAW
        # message (`msg` / `summary` / `text`) that the line above has just
        # redacted. Measured 2026-09-05: one QC event, scrubbed message,
        # the request id and an email address raw three times in `threads`.
        if "threads" in event and "values" in event["threads"]:
            for th in event["threads"]["values"]:
                _scrub_frame_vars(th.get("stacktrace"))
        if "breadcrumbs" in event and "values" in event["breadcrumbs"]:
            for crumb in event["breadcrumbs"]["values"]:
                if "message" in crumb:
                    crumb["message"] = _scrub_string(crumb.get("message", ""))
                if "data" in crumb and isinstance(crumb["data"], dict):
                    crumb["data"] = _walk_scrub(crumb["data"])
    except Exception:
        # FAIL CLOSED on PII (audit finding [30]). The old behavior returned the
        # raw, UNSCRUBBED event on any scrub fault — the exact opposite of this
        # hook's purpose ("observability WITHOUT leaking client data"): a
        # malformed event that trips the scrubber would ship raw emails / MDOLX
        # / conv-IDs to the third-party SaaS. Instead, keep the alert (so we
        # still learn an error occurred) but hard-redact every field that can
        # carry free-text PII to a fixed placeholder. We do NOT return the raw
        # event, and we never crash the real error path.
        try:
            return _fail_closed_event(event)
        except Exception:
            # If even the redaction fails, drop the event rather than leak.
            return None
    return event


# Fields that carry free-text and therefore client PII. On a scrub fault we
# blunt-redact these wholesale rather than risk shipping them unscrubbed.
# Every entry is ALSO scrubbed on the normal path — either wholesale
# (`_FREE_TEXT_KEYS`) or field-by-field (exception / threads / breadcrumbs in
# `_before_send`); the scrubber test parametrises over this tuple.
_PII_BEARING_KEYS = ("message", "exception", "threads", "extra", "breadcrumbs",
                     "logentry", "request", "user")

# Event keys whose whole value is data, never code: walked wholesale by
# `_before_send`. `logentry` is the stdlib LoggingIntegration's interface
# ({message, formatted, params}); `request` / `user` never occur in this
# pipeline (no HTTP server, `send_default_pii=False`) and are here so the
# drop list above has no member the normal path skips. `tags` / `contexts`
# are ours and pre-scrubbed; walking them costs nothing.
_FREE_TEXT_KEYS = ("message", "logentry", "extra", "request", "user",
                   "tags", "contexts")


def _fail_closed_event(event):
    """Minimal, never-raising redaction used when the normal scrubber faulted.

    Strips the free-text/PII-bearing fields to a placeholder while preserving
    the skeleton Sentry needs to record that *an* error happened (level, tags
    we control, the fingerprint). Better a near-empty alert than a raw leak.
    """
    if not isinstance(event, dict):
        return None
    safe = {}
    for k, v in event.items():
        if k in _PII_BEARING_KEYS:
            continue  # drop the whole PII-bearing field
        safe[k] = v
    safe["message"] = "[SCRUBBER_FAILED — event redacted to prevent PII leak]"
    return safe


# ─────────────────────────────────────────────────────────────────────
# DSN loading
# ─────────────────────────────────────────────────────────────────────

def _load_dsn() -> str | None:
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
    """Production = the scheduled daily fire host. Manual = anywhere else.

    2026-07-15 ROOT CAUSE of the recurring HILMAR-DAILY-TRACKER-A pages: this
    only recognized the CLOUD PC hostname as production, so after the GitHub
    Actions cutover every check-in (cron heartbeat included) landed in the
    'manual' monitor ENVIRONMENT while the monitor's 'production' environment
    — seeded by the Cloud PC era — sat check-in-less and paged 'missed
    check-in' every weekday at 22:57 ET (26 straight). Sentry Crons tracks
    each environment separately. GitHub Actions IS the production fire host,
    so it must report as 'production'.
    """
    # Explicit override always wins.
    if "SENTRY_ENVIRONMENT" in os.environ:
        return os.environ["SENTRY_ENVIRONMENT"]
    # GitHub Actions — the production daily-fire host since the cutover.
    if os.environ.get("GITHUB_ACTIONS") == "true":
        return "production"
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


def _sdk_options(dsn: str, env: str, release: str, *, sample_rate: float = 1.0) -> dict:
    """The `sentry_sdk.init` options, as ONE dict, so a test can run the real
    SDK on the production configuration (plus a recording transport) instead
    of a copy of it that drifts."""
    return dict(
        dsn=dsn,
        environment=env,
        release=release,
        # Performance transactions: capture every step (low volume — ~14/fire/day)
        traces_sample_rate=sample_rate,
        # PII handling: opt OUT of automatic PII capture. We control what
        # gets sent via explicit capture_message() calls + the scrubber.
        send_default_pii=False,
        # STANDARDS §6: `with_locals=False` (the 1.x name; 2.x spells it
        # include_local_variables). Never set until 2026-09-05 — with
        # attach_stacktrace on, every QC event carried the RAW message in
        # the `threads` frame locals beside its scrubbed copy.
        include_local_variables=False,
        # Limit context to keep payload small
        max_breadcrumbs=50,
        attach_stacktrace=True,
        # The scrubber — strips emails / MDOLX / conv IDs / IMIDs / req IDs
        before_send=_before_send,
        before_send_transaction=_before_send,
        # Don't auto-instrument network libs (not needed for this pipeline)
        auto_enabling_integrations=False,
    )


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

    sentry_sdk.init(**_sdk_options(dsn, env, release, sample_rate=sample_rate))

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


def qc_event_message(check_name: str, summary: str) -> str:
    """The one Sentry message for a QC finding, prefixed by its check EXACTLY
    ONCE. qc_selfheal's Log hands over the full log line, which already opens
    with the check tag, so re-prefixing produced "QC-039: QC-039: parser
    accuracy ..." on every event (HILMAR-DAILY-TRACKER-8, 119 occurrences).
    A summary that does not carry the tag still gets it — the tag is what
    groups the issue."""
    text = str(summary or "").strip()
    if text.startswith(f"{check_name}:"):
        return text
    return f"{check_name}: {text}"


# The name qc_selfheal._extract_check_name emits for a log message that
# carries no QC-NNN prefix (91 such log sites on 2026-09-05: "Data JSON
# MISSING at …", "'requests' is not an array", …). OWNED HERE and read by
# qc_selfheal at call time (never re-spelled there), because this is the one
# name `event_fingerprint` must recognise: the ONLY name that keeps Sentry's
# default grouping.
# Forcing it into a fingerprint would fold every prefix-less message into
# one catch-all issue titled by whichever fired first.
QC_UNKNOWN_CHECK = "QC-unknown"


def event_fingerprint(check_name: str, *parts) -> list[str] | None:
    """The Sentry fingerprint for a capture_qc_* event: ``[check_name,
    *parts]`` — one issue per NAME, split further only by what the caller
    passes in ``parts`` — or ``None`` (Sentry's default grouping) for the
    ``QC_UNKNOWN_CHECK`` sentinel and for an empty name, nothing else.

    Every caller gets deterministic per-name grouping: a ``QC-NNN`` id from
    ``qc_selfheal.Log`` (any sub-variant letter included — the shape is the
    emitter's business, not this function's), ``pipeline.step_failure`` from
    ``run_pipeline`` (with the step and the failure kind as parts, so a
    step's TIMEOUT and its deliberate exit code are two issues), and
    ``patch_carriers.ambiguous_match``. The first cut of this function
    (2026-09-05, same day) returned ``None`` for every name that did not
    match a QC-id regex of its own — so both real non-QC callers kept
    exactly the shared-stack grouping it existed to kill, and the regex
    restated ``_extract_check_name``'s with nothing pinning the pair.
    ``parts`` are stringified; an empty part is dropped, never spelled
    ``"None"`` into an issue key.

    WHY (2026-09-05, HILMAR-DAILY-TRACKER-K). For four months the
    ``capture_qc_error`` docstring promised per-check grouping while the code
    set a tag and passed NO fingerprint. With ``attach_stacktrace=True``
    Sentry then grouped every ``capture_message`` by the stack of the ONE
    shared ``Log.error -> capture_qc_error`` path, so grouping was wrong in
    both directions — measured on 90 days of ``component:qc_selfheal``:
    issue TRACKER-3 held EIGHT checks (QC-077/055/015/052/054/019/039/061),
    TRACKER-K held 21 QC-073 events under a QC-072 title and a QC-072 Seer
    analysis, and QC-039 alone was spread over THREE issues, because the
    fingerprinted stack moves whenever qc_selfheal.py gains a line. Every
    consumer of "one issue = one check" was wrong with it: occurrence counts
    (CHANGELOG 2026-09-04 item 11 read 75 for a check that fired 4 times),
    Seer analyses, and ``qc_actions_from_sentry._action_lookup``, which
    routes a WHOLE issue — ``auto_resolve_safe`` included — off the first
    ``qc_check`` tag it finds on it.
    """
    if not isinstance(check_name, str) or not check_name.strip():
        return None
    if check_name.strip() == QC_UNKNOWN_CHECK:
        return None
    fp = [check_name]
    for part in parts:
        if part is None:
            continue
        text = str(part).strip()
        if text:
            fp.append(text)
    return fp


def _capture_qc(check_name: str, summary: str, level: str, group_by=()) -> None:
    """ONE composition for both QC capture paths, so the identity rules
    cannot hold for ERRORs and not for WARNs.

    Per-EVENT data only: ``tags`` and ``fingerprint`` ride the event through
    ``capture_message``'s scope kwargs (sentry-sdk 2.x
    ``Scope.update_from_kwargs``; verified on the pinned 2.68.1 source). The
    old ``sentry_sdk.set_tag("qc_check", ...)`` wrote the ISOLATION scope,
    which outlives the call — every later event in the process (a step
    failure, an uncaught exception) inherited the LAST check's id, and
    ``_action_lookup`` keys on exactly that tag.

    The summary already carries its own ``QC-NNN:`` prefix on every
    qc_selfheal path, so it is not prefixed twice (the issue titles read
    ``QC-072: QC-072: request ...`` before this).
    """
    import sentry_sdk
    kwargs: dict = {"tags": {"qc_check": check_name}}
    fp = event_fingerprint(check_name, *tuple(group_by or ()))
    if fp is not None:
        kwargs["fingerprint"] = fp
    text = qc_event_message(check_name, summary)
    sentry_sdk.capture_message(text, level=level, **kwargs)


def capture_qc_error(check_name: str, summary: str, *, group_by=(), **extras) -> None:
    """Capture a QC ERROR-severity finding as a Sentry event.

    Use from qc_selfheal.py when log.error() fires. The check_name IS the
    issue fingerprint (``event_fingerprint``): one Sentry issue per name —
    a ``QC-NNN`` check across rows, fires and releases, or a dotted non-QC
    name such as ``pipeline.step_failure`` — and no other name can join
    it. ``group_by`` appends discriminators to that fingerprint for a
    caller whose one name covers several defects (run_pipeline passes the
    step and the failure kind). The ONLY name that keeps Sentry's default
    grouping is the ``QC_UNKNOWN_CHECK`` sentinel a prefix-less log
    message yields. The ``qc_check`` tag rides the EVENT, never the
    process scope, so a later unrelated event cannot inherit it. Pinned,
    with the real callers enumerated by AST, in
    tests/test_sentry_qc_fingerprint.py.
    """
    try:
        if not _INITIALIZED:
            return
        _capture_qc(check_name, summary, "error", group_by)
    except Exception:
        pass  # observability must never crash the pipeline


def capture_qc_warning(check_name: str, summary: str, *, group_by=(), **extras) -> None:
    """Like capture_qc_error but at warning level — for QC WARNs that
    are worth surfacing in real time (e.g. parser regressions just under
    the error threshold). Same fingerprint rule, same ``group_by``."""
    try:
        if not _INITIALIZED:
            return
        _capture_qc(check_name, summary, "warning", group_by)
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
# Sentry Crons — heartbeat the daily 6 PM ET fire
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
# Schedule:     Mon-Fri at 6:07 PM ET (the GH Actions cron)
# Margin:       Check-in must arrive before the FULL evening backstop window
#               closes (~10:57 PM ET) — see checkin_margin below.
# Max runtime:  60 min before declaring the run hung/missed
#
# The monitor is AUTO-PROVISIONED by the first check-in that supplies a
# monitor_config — no need for Sentry UI setup or auth-token-based API.

MONITOR_SLUG = "hilmar-daily-pipeline"

_MONITOR_CONFIG = {
    # Mon-Fri 08:07 ET (2026-07-18: the MORNING fire now runs Mon-Fri, each
    # reporting the prior business day — Friday morning reports Thursday and
    # closes the old Thursday coverage gap. Friday ALSO fires a 4:30 PM wrap-up
    # of Friday itself, covered by liveness.yml not this monitor — a single
    # crontab can't express two daily times, and the wrap-up check-in still
    # lands harmlessly). No weekend fire, so the schedule is dow 1-5. The margin
    # absorbs GitHub's cron jitter (observed 30min-4.5h) AND the late-morning
    # liveness backstop (~11:30 AM ET last tick → check-in ~11:45 AM ET). 290 min
    # (06:30 → ~11:20 AM ET) means the alert fires ONLY when the morning cron AND
    # every liveness recovery failed — a true "pipeline never ran today", which
    # is exactly when a page is warranted.
    # MOVED WITH THE FIRE 2026-08-27 (8:07 -> 6:30 AM ET). This monitor and
    # daily.yml's crons are one setting in two files: leave it at 8:07 and
    # Sentry pages every weekday for a check-in that now arrives 97 minutes
    # earlier. tests/test_fire_time_consistency.py pins them together.
    "schedule": {"type": "crontab", "value": "30 6 * * 1-5"},
    "schedule_type": "crontab",
    "timezone": "America/New_York",
    "checkin_margin": 290,   # alert ~12:57 PM ET — only after the full backstop fails
    "max_runtime": 60,       # alert if pipeline runs >60 min (typical = 30-60s, lots of headroom)
    "failure_issue_threshold": 1,   # 1 missed/failed run = create issue immediately
    "recovery_threshold": 1,        # 1 successful run = resolve the issue
}


def _monitor_config_rest() -> dict:
    """``_MONITOR_CONFIG`` translated to the Sentry REST-API monitor shape.

    The SDK ``monitor_config`` nests the schedule as ``{"type", "value"}``;
    the REST monitor API wants a flat ``schedule`` string + ``schedule_type``.
    Keep both in sync from the one source of truth above.
    """
    sch = _MONITOR_CONFIG["schedule"]
    return {
        "schedule_type": "crontab",
        "schedule": sch["value"] if isinstance(sch, dict) else sch,
        "timezone": _MONITOR_CONFIG.get("timezone", "UTC"),
        "checkin_margin": _MONITOR_CONFIG.get("checkin_margin"),
        "max_runtime": _MONITOR_CONFIG.get("max_runtime"),
        "failure_issue_threshold": _MONITOR_CONFIG.get("failure_issue_threshold", 1),
        "recovery_threshold": _MONITOR_CONFIG.get("recovery_threshold", 1),
    }


def ensure_monitor_schedule() -> bool:
    """Force-align the LIVE Sentry monitor to ``_MONITOR_CONFIG`` via the REST
    API (auth-token path), independent of the SDK check-in upsert.

    Why this exists: the check-in's ``monitor_config`` does NOT reliably update
    an existing monitor's schedule. On 2026-06-17 the monitor was still pinned
    to the old 10 AM ET / 95-min config while the code had moved to 6 PM ET /
    290, so Sentry paged 'missed check-in' every day at 11:42 AM ET (= 10:07 +
    95). Run once per fire — cheap, idempotent, and self-heals any drift.
    Best-effort: never raises, never blocks the pipeline.
    """
    try:
        from sentry_api import SentryAPI
        api = SentryAPI()
        if not api.enabled:
            return False
        ok = api.update_monitor(MONITOR_SLUG, _monitor_config_rest())
        if ok:
            print(f"✅ Sentry monitor '{MONITOR_SLUG}' schedule aligned "
                  f"({_monitor_config_rest()['schedule']} "
                  f"{_monitor_config_rest()['timezone']}, "
                  f"margin {_monitor_config_rest()['checkin_margin']}m)")
        # DETECT (never delete) orphaned monitor environments — 2026-07-15
        # root cause of the daily HILMAR-DAILY-TRACKER-A pages: Sentry alerts
        # missed check-ins PER ENVIRONMENT. When the fire host changes (Cloud
        # PC → GH Actions) the abandoned environment stops receiving check-ins
        # and pages 'missed check-in' every scheduled day, forever. Deleting
        # monitoring config automatically is an operator decision, so this
        # only WARNS loudly with the exact manual fix (Sentry UI: Crons →
        # hilmar-daily-pipeline → delete the orphaned environment). Best-effort.
        with contextlib.suppress(Exception):
            current_env = _detect_environment()
            detail = api.get_monitor(MONITOR_SLUG) or {}
            orphans = [
                (e or {}).get("name") for e in detail.get("environments") or []
                if (e or {}).get("name") and (e or {}).get("name") != current_env
            ]
            if orphans:
                print(
                    f"⚠️  Sentry monitor '{MONITOR_SLUG}' has orphaned "
                    f"environment(s) {orphans} — this host reports "
                    f"'{current_env}', so those will page 'missed check-in' "
                    f"every scheduled day. MANUAL FIX (operator): Sentry UI → "
                    f"Crons → {MONITOR_SLUG} → delete the orphaned "
                    f"environment(s)."
                )
        return ok
    except Exception as _e:
        with contextlib.suppress(Exception):
            print(f"⚠️  ensure_monitor_schedule failed (non-fatal): {_e}")
        return False


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
        with contextlib.suppress(Exception):
            print(f"⚠️  Sentry cron start failed (pipeline continues): {_e}")
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


def heartbeat_checkin(success: bool) -> bool:
    """Single terminal cron check-in for the HEARTBEAT model — the
    host-agnostic 'the daily fire ran' signal.

    Why (2026-07-15, HILMAR-DAILY-TRACKER-A false page): the cron check-in
    used to be sent ONLY from inside run_pipeline.py (start + finish), which
    couples the monitor to one code path's Sentry init on whichever host
    fires. On 2026-07-14 the GitHub Actions fire shipped the report
    (heartbeat = success, dispatched by github-actions[bot]) but the
    in-process check-in never registered with Sentry, so the monitor
    false-paged 'missed check-in' at ~10:57 PM ET even though the report
    shipped. (Initially misattributed to the Cloud PC — the heartbeat
    workflow's old hardcoded name said "Cloud PC fired" for every host.)

    liveness.yml already treats heartbeat.yml — dispatched by EVERY firing
    host at the end of a fire — as the source of truth for 'did the fire
    run'. This lets the Sentry monitor read the SAME signal: heartbeat.yml
    calls this, so a successful fire ALWAYS yields an 'ok' check-in
    regardless of host, and a true no-fire day yields none → the monitor
    pages only on a real miss, in agreement with liveness.

    A lone ``status='ok'`` check-in (no preceding ``in_progress``) is a
    valid, complete Sentry Crons check-in. Best-effort: never raises.
    Returns True if a check-in was sent, False on no-op (no DSN / no SDK).
    """
    if not init(component="heartbeat"):
        return False
    try:
        import sentry_sdk
        sentry_sdk.crons.capture_checkin(
            monitor_slug=MONITOR_SLUG,
            status="ok" if success else "error",
            monitor_config=_MONITOR_CONFIG,
        )
        # Keep the live monitor's schedule aligned from this path too, so
        # schedule drift self-heals even on days the in-pipeline check-in
        # (which also aligns it) never runs.
        with contextlib.suppress(Exception):
            ensure_monitor_schedule()
        sentry_sdk.flush(timeout=5)
        return True
    except Exception as _e:
        with contextlib.suppress(Exception):
            print(f"⚠️  heartbeat_checkin failed (non-fatal): {_e}")
        return False


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
    print("   Check https://o4511407070904320.sentry.io/issues/ for the message.")
    print("   The email + MDOLX in the message body should appear as [EMAIL_REDACTED] + [MDOLX_REDACTED].")
    sentry_sdk.flush(timeout=5)
