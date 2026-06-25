"""
fire_alert.py — OUT-OF-BAND loud alerting for the daily fire.

The root cause of the 2026-06 silent week: the pipeline's only alarm (the QC
audit email + qc_alert_if_needed) routes through the SAME Outlook/MSAL channel
most likely to be broken when there's something to alarm about. An alarm that
rides the failing subsystem is no alarm.

This module raises alerts on channels that do NOT depend on the box's MSAL /
Outlook path:
  1. stderr `::error::` banner — always (captured in the fire's run-log and, on
     GitHub Actions, surfaced as an annotation).
  2. reports/alerts-queue.json — always (durable local record; the existing
     queue the rest of the system already drains/reads).
  3. GitHub issue — via the `gh` CLI if present, else the REST API with a PAT
     (GH_TOKEN / GITHUB_TOKEN). Box-independent; this is what liveness.yml also
     watches.
  4. Teams Incoming Webhook — config.alerts.teams_webhook_url (no-op until
     configured; independent HTTP POST, never Outlook).

Every channel is best-effort and isolated: one failing never blocks the others.
send_alert() returns {channel: bool} so callers/tests can assert delivery.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPORTS = ROOT / "reports"
ALERTS_QUEUE = REPORTS / "alerts-queue.json"
GITHUB_REPO = os.environ.get("GITHUB_REPOSITORY", "IdealX-dev/hilmar-daily-routine")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stderr_banner(title: str, body: str) -> bool:
    try:
        print(f"::error::FIRE-ALERT: {title}", file=sys.stderr)
        print(body, file=sys.stderr)
        sys.stderr.flush()
        return True
    except Exception:
        return False


def _append_queue(record: dict) -> bool:
    try:
        REPORTS.mkdir(parents=True, exist_ok=True)
        queue = []
        if ALERTS_QUEUE.exists():
            try:
                queue = json.loads(ALERTS_QUEUE.read_text(encoding="utf-8"))
                if not isinstance(queue, list):
                    queue = []
            except Exception:
                queue = []
        queue.append(record)
        ALERTS_QUEUE.write_text(json.dumps(queue[-200:], indent=2), encoding="utf-8")
        return True
    except Exception:
        return False


def _github_issue(title: str, body: str, labels: tuple) -> bool:
    """Create a GitHub issue out-of-band. Prefer the gh CLI (the wrapper
    already uses it for heartbeats); fall back to the REST API with a PAT.
    Returns False (not raising) when no auth/channel is available."""
    label_args = []
    for label in labels:
        label_args += ["--label", label]
    # 1) gh CLI
    try:
        if _have_gh():
            rc = subprocess.run(
                ["gh", "issue", "create", "-R", GITHUB_REPO,
                 "--title", title, "--body", body, *label_args],
                capture_output=True, text=True, timeout=60).returncode
            if rc == 0:
                return True
    except Exception:
        pass
    # 2) REST API
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        return False
    try:
        import requests
        r = requests.post(
            f"https://api.github.com/repos/{GITHUB_REPO}/issues",
            headers={"Authorization": f"Bearer {token}",
                     "Accept": "application/vnd.github+json"},
            json={"title": title, "body": body, "labels": list(labels)},
            timeout=30)
        return 200 <= r.status_code < 300
    except Exception:
        return False


def _have_gh() -> bool:
    try:
        return subprocess.run(["gh", "--version"], capture_output=True,
                              timeout=10).returncode == 0
    except Exception:
        return False


def _teams(title: str, body: str) -> bool:
    """POST to the Teams Incoming Webhook from config.alerts.teams_webhook_url.
    No-op (returns False) when not configured. Independent of Outlook/MSAL."""
    url = ""
    try:
        cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
        url = (cfg.get("alerts") or {}).get("teams_webhook_url") or ""
    except Exception:
        url = ""
    if not url:
        return False
    try:
        import requests
        r = requests.post(url, json={
            "@type": "MessageCard", "@context": "http://schema.org/extensions",
            "themeColor": "B60205", "summary": title,
            "title": f"🔴 Hilmar fire alert — {title}",
            "text": body.replace("\n", "  \n")}, timeout=30)
        return 200 <= r.status_code < 300
    except Exception:
        return False


def send_alert(title: str, body: str, *, level: str = "error",
               labels: tuple = ("fire-alert", "cloud-pc-down")) -> dict:
    """Raise an out-of-band alert on every available channel. Best-effort:
    a failing channel never blocks the others. Returns {channel: delivered?}."""
    full_body = f"{body}\n\n— raised {_now()} (level={level})"
    record = {"ts": _now(), "level": level, "title": title, "body": body}
    results = {
        "stderr": _stderr_banner(title, full_body),
        "queue": _append_queue(record),
        "github": _github_issue(title, full_body, labels),
        "teams": _teams(title, full_body),
    }
    return results


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Raise an out-of-band fire alert")
    ap.add_argument("--title", required=True)
    ap.add_argument("--body", default="")
    ap.add_argument("--level", default="error")
    args = ap.parse_args()
    res = send_alert(args.title, args.body, level=args.level)
    print(json.dumps(res))
    # Exit 0 if ANY channel delivered (the alert is out); 1 if all failed.
    return 0 if any(res.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
