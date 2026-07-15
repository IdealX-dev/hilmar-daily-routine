#!/usr/bin/env python3
"""CLI: send ONE Sentry cron check-in for the hilmar-daily-pipeline monitor.

Called by .github/workflows/heartbeat.yml at the end of every daily fire from
ANY host (Cloud PC or GitHub Actions), so the Sentry cron monitor reads the
same 'the fire ran' signal that liveness.yml does — instead of the in-pipeline
check-in that false-paged when a firing host's Sentry init didn't reach Sentry
(HILMAR-DAILY-TRACKER-A). See sentry_setup.heartbeat_checkin for the rationale.

Usage:  python scripts/sentry_cron_checkin.py --status success|failed
Exit code is ALWAYS 0 — observability must never fail the heartbeat job.
"""
from __future__ import annotations

import argparse
import sys

import sentry_setup


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Send a Sentry cron heartbeat check-in.")
    ap.add_argument(
        "--status",
        default="success",
        help="Fire outcome. 'success'/'ok' → ok check-in; anything else → error.",
    )
    args = ap.parse_args(argv)
    success = args.status.strip().lower() in ("success", "ok", "true")
    sent = sentry_setup.heartbeat_checkin(success)
    state = "ok" if success else "error"
    if sent:
        print(f"OK Sentry cron check-in sent (status={state}) for "
              f"{sentry_setup.MONITOR_SLUG}")
    else:
        print("INFO Sentry cron check-in skipped (no DSN / no sentry-sdk) — no-op.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
