"""
assert_fire_integrity.py — PROVE the daily fire shipped a report, or scream.

The single mechanism behind the 2026-06 silent week: the wrapper assumed
success by reaching its last line — `run_daily_laptop.cmd` echoed the send exit
code but never branched on it, ended with `exit /b 0`, and dispatched a
heartbeat hardcoded status="success". Nothing PROVED the client email left the
mailbox. So a failed/odd send produced a green heartbeat, liveness saw green,
and reports were silently absent for a week.

This is the MANDATORY final wrapper step. It flips the model from "assume
success unless an exception escaped" to "prove the deliverable shipped or
scream." It asserts:
  - the pipeline exited 0,
  - today's client artifacts (email-subject.txt / email-body.html /
    hilmar-report.pdf) exist and are dated TODAY,
  - today's full-distribution send actually happened (the sent-YYYY-MM-DD.flag
    that outlook_send writes only AFTER a successful send).

It also surfaces (best-effort) a WARNING if the MSAL token cache is absent (the
classic silent-auth killer). That is NOT a delivery-proof violation: a missing
cache after a successful send (OneDrive sync lag, cleanup, an auth in progress)
must not fake a "no verified report shipped" page. The token-cache check rides
the warning channel (level="warning") and never gates the exit code.

On ANY violation it raises an OUT-OF-BAND alert (fire_alert: GitHub issue +
Teams + durable queue + stderr — never the Outlook path it's alarming about)
and exits non-zero, so Task Scheduler's Last-Run-Result turns red and the
wrapper passes status=failed to the heartbeat (gating liveness honestly).

NOTE: the strongest proof is a Graph SentItems probe (the mailbox is the source
of truth). That needs the MSAL token + is a follow-up; the sent-flag + fresh
artifacts are a large step up from "reached the last line."
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPORTS = ROOT / "reports"
SECRETS = ROOT / "secrets"

sys.path.insert(0, str(ROOT / "scripts"))


def _et_today() -> str:
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/New_York")).date().isoformat()
    except Exception:
        return datetime.now(timezone.utc).date().isoformat()


def _mtime_date(p: Path) -> str:
    try:
        from zoneinfo import ZoneInfo
        return datetime.fromtimestamp(
            p.stat().st_mtime, ZoneInfo("America/New_York")).date().isoformat()
    except Exception:
        return datetime.fromtimestamp(
            p.stat().st_mtime, timezone.utc).date().isoformat()


def _flag_day(today: str) -> str:
    """The day the send-flag is keyed to: the REPORT business day (matches
    outlook_send._flag_date). Only differs from the calendar day off-hours —
    e.g. a 00:40 fire reports (and flags) the evening that just ended, so the
    send proof must be checked under THAT name, not the empty new day's.
    Falls back to the calendar day if core can't import."""
    try:
        from zoneinfo import ZoneInfo as _zi

        import core as _core
        _now = datetime.now(_zi("America/New_York"))
        # Trust core only when checking "now"; an explicitly injected test
        # date is used verbatim.
        if today == _now.date().isoformat():
            return _core.report_business_day(_now).isoformat()
    except Exception:
        pass
    return today


def check_integrity(*, pipeline_rc: int, require_send: bool = True,
                    today: str | None = None, reports: Path = REPORTS,
                    secrets: Path = SECRETS) -> list[str]:
    """Return a list of integrity violations (empty == the fire genuinely
    shipped a fresh report). Pure — all inputs injectable for tests."""
    today = today or _et_today()
    violations: list[str] = []

    if pipeline_rc != 0:
        violations.append(f"pipeline exited rc={pipeline_rc} (expected 0)")

    for name in ("email-subject.txt", "email-body.html", "hilmar-report.pdf"):
        p = reports / name
        if not p.exists():
            violations.append(f"client artifact MISSING: reports/{name}")
        else:
            d = _mtime_date(p)
            if d != today:
                violations.append(f"client artifact STALE: reports/{name} dated {d} (not {today})")

    if require_send:
        # The flag is keyed to the REPORT day (see _flag_day / outlook_send's
        # wee-hours rule); accept the calendar-day name too so proofs written
        # under either keying (transition, injected test dates) still count.
        _fd = _flag_day(today)
        candidates = {_fd, today}
        if not any((reports / f"sent-{d}.flag").exists() for d in candidates):
            violations.append(
                f"NO send proof: reports/sent-{_fd}.flag is absent — the "
                f"client email did NOT ship to the full distribution today")

    return violations


def check_warnings(*, secrets: Path = SECRETS) -> list[str]:
    """Return non-gating warnings (empty == nothing to surface). These are NOT
    delivery-proof violations — they must never drive the exit code or the
    critical alarm, only a warning-level out-of-band note. Pure/injectable."""
    warnings: list[str] = []

    # Best-effort: the MSAL token cache should exist (empty/missing = the
    # silent-auth failure mode). Non-fatal on its own, but worth surfacing — a
    # transiently-absent cache after a successful send must not fake a critical
    # "no verified report shipped" page. Accept EITHER the canonical non-indexed
    # .bin or a legacy .json (mid-migration) — see outlook_send TOKEN_CACHE_PATH.
    if not (secrets / "token-cache.bin").exists() and not (secrets / "token-cache.json").exists():
        warnings.append("MSAL token cache absent (secrets/token-cache.bin|.json) — "
                        "auth may be unconfigured/expired")

    return warnings


def main() -> int:
    ap = argparse.ArgumentParser(description="Prove the daily fire shipped a report")
    ap.add_argument("--pipeline-rc", type=int, default=0,
                    help="Exit code of run_pipeline.py (the wrapper captures it)")
    ap.add_argument("--no-require-send", action="store_true",
                    help="Skip the send-proof check (build-only / iteration runs)")
    ap.add_argument("--date", default=None, help="Override today (ET ISO date)")
    args = ap.parse_args()

    violations = check_integrity(
        pipeline_rc=args.pipeline_rc,
        require_send=not args.no_require_send,
        today=args.date)
    warnings = check_warnings()

    if not violations:
        print(f"✅ Fire integrity OK — fresh report shipped ({args.date or _et_today()})")
        # The report DID ship; a missing token cache is a non-gating warning,
        # never the critical "no verified report shipped" page.
        if warnings:
            wtitle = "Daily fire shipped OK — non-fatal warning"
            wbody = ("The daily Hilmar fire shipped a verified report, but a "
                     "non-fatal condition is worth surfacing:\n  - "
                     + "\n  - ".join(warnings))
            print("⚠ " + wtitle, file=sys.stderr)
            for w in warnings:
                print("   - " + w, file=sys.stderr)
            try:
                import fire_alert
                res = fire_alert.send_alert(wtitle, wbody, level="warning",
                                            labels=("fire-alert",))
                print(f"   out-of-band warning channels: {res}", file=sys.stderr)
            except Exception as e:
                print(f"   (fire_alert failed: {e})", file=sys.stderr)
        return 0

    title = "Daily fire integrity FAILED — no verified report shipped"
    body = ("The daily Hilmar fire did NOT prove it shipped a report:\n  - "
            + "\n  - ".join(violations))
    if warnings:
        # Fold warnings in for context, but they do NOT gate the exit code.
        body += "\n\nAdditional (non-gating) warnings:\n  - " + "\n  - ".join(warnings)
    body += ("\n\nThis fired the OUT-OF-BAND alarm (not Outlook). Recover: re-run "
             "deploy\\run_daily_laptop.cmd on the Cloud PC, or check the run-log "
             "for the failing step. The wrapper passes status=failed to the "
             "heartbeat so liveness will also flag this.")
    print("❌ " + title, file=sys.stderr)
    for v in violations:
        print("   - " + v, file=sys.stderr)
    try:
        import fire_alert
        res = fire_alert.send_alert(title, body, level="critical")
        print(f"   out-of-band alert channels: {res}", file=sys.stderr)
    except Exception as e:
        print(f"   (fire_alert failed: {e})", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
