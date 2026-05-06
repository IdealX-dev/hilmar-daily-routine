"""
healthcheck_ping.py — POST a success/failure ping to Healthchecks.io.

Usage (from run_pipeline.py final step):
    from scripts.healthcheck_ping import ping_success, ping_failure
    ping_success()  # or ping_failure("reason")

Configuration:
- Reads HEALTHCHECK_URL from env var, OR from a file at .healthcheck_url
  (project root, gitignored).
- If neither is set, no-op (dev/local mode).

Healthchecks.io contract:
- POST {URL}        → success ping (resets grace window)
- POST {URL}/fail   → failure ping (fires alert immediately)
- POST {URL}/start  → start ping (optional — marks pipeline began)

Setup (one-time):
1. Sign up at https://healthchecks.io (free tier — fine for this).
2. Create a check named "Hilmar Tracker — Mon/Wed/Fri 4 PM ET".
3. Schedule: cron 0 16 * * 1,3,5, timezone America/New_York.
4. Grace: 20 min.
5. Alert email: michael.deitchman@idealx.us.
6. Copy the unique ping URL → set as HEALTHCHECK_URL env var on VM.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# Prefer requests (better timeout handling), fall back to urllib stdlib.
try:
    import requests  # type: ignore
    _USE_REQUESTS = True
except ImportError:
    import urllib.request
    import urllib.error
    _USE_REQUESTS = False


PROJECT_ROOT = Path(__file__).resolve().parent.parent
URL_FILE = PROJECT_ROOT / ".healthcheck_url"


def _resolve_url() -> str | None:
    """Return Healthchecks ping URL from env, then file, then None."""
    url = os.environ.get("HEALTHCHECK_URL", "").strip()
    if url:
        return url
    if URL_FILE.exists():
        url = URL_FILE.read_text().strip()
        if url:
            return url
    return None


def _post(url: str, body: str = "", timeout: int = 10) -> bool:
    """POST to URL with body. Return True on 2xx, False otherwise. Never raises."""
    try:
        if _USE_REQUESTS:
            r = requests.post(url, data=body.encode("utf-8"), timeout=timeout)
            return 200 <= r.status_code < 300
        else:
            req = urllib.request.Request(
                url,
                data=body.encode("utf-8"),
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return 200 <= resp.status <= 299
    except Exception as e:
        print(f"[healthcheck] ping failed: {e}", file=sys.stderr)
        return False


def ping_start() -> bool:
    """Optional: signal pipeline started. Logged on Healthchecks dashboard."""
    url = _resolve_url()
    if not url:
        print("[healthcheck] no URL configured, skipping start ping", file=sys.stderr)
        return False
    return _post(f"{url.rstrip('/')}/start", body="pipeline started")


def ping_success(detail: str = "") -> bool:
    """Send success ping. Resets grace window. detail is shown in dashboard."""
    url = _resolve_url()
    if not url:
        print("[healthcheck] no URL configured, skipping success ping", file=sys.stderr)
        return False
    return _post(url, body=detail or "pipeline ok")


def ping_failure(reason: str = "") -> bool:
    """Send failure ping. Fires alert email immediately."""
    url = _resolve_url()
    if not url:
        print("[healthcheck] no URL configured, skipping failure ping", file=sys.stderr)
        return False
    return _post(f"{url.rstrip('/')}/fail", body=reason or "pipeline failed")


if __name__ == "__main__":
    # CLI: python healthcheck_ping.py [start|success|failure] [detail]
    args = sys.argv[1:]
    op = args[0] if args else "success"
    detail = " ".join(args[1:])
    fn = {"start": ping_start, "success": ping_success, "failure": ping_failure}.get(op)
    if not fn:
        print(f"Unknown op: {op}. Use start, success, or failure.", file=sys.stderr)
        sys.exit(2)
    ok = fn(detail) if op != "start" else fn()
    sys.exit(0 if ok else 1)
