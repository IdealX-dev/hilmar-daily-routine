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
    that outlook_send writes only AFTER a successful send),
  - (best-effort) the MSAL token cache is present (a missing/empty cache is the
    classic silent-auth killer).

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
        flag = reports / f"sent-{today}.flag"
        if not flag.exists():
            violations.append(
                f"NO send proof: reports/sent-{today}.flag is absent — the "
                f"client email did NOT ship to the full distribution today")

    # Best-effort: the MSAL token cache should exist (empty/missing = the
    # silent-auth failure mode). Non-fatal on its own, but worth surfacing.
    cache = secrets / "token-cache.json"
    if not cache.exists():
        violations.append("MSAL token cache absent (secrets/token-cache.json) — "
                          "auth may be unconfigured/expired")

    return violations


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

    if not violations:
        print(f"✅ Fire integrity OK — fresh report shipped ({args.date or _et_today()})")
        return 0

    title = "Daily fire integrity FAILED — no verified report shipped"
    body = ("The daily Hilmar fire did NOT prove it shipped a report:\n  - "
            + "\n  - ".join(violations)
            + "\n\nThis fired the OUT-OF-BAND alarm (not Outlook). Recover: re-run "
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
