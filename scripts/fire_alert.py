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

# Make sibling scripts (sentry_setup) importable even when fire_alert is invoked
# directly via main(); mirrors preflight_env.py / assert_fire_integrity.py.
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _scrub(s: str) -> str:
    """Redact PII before egress so every out-of-band channel (GitHub issue,
    Teams, queue, stderr) shares ONE redaction boundary with the Sentry path.

    Reuses sentry_setup._scrub_string (pure-regex, never raises). If that
    shared scrubber is unavailable, fail-CLOSED on the most obvious leak
    (emails) while staying fail-OPEN on delivery — an alert must never be
    suppressed by a scrub-import failure (this module is best-effort)."""
    try:
        import sentry_setup
        return sentry_setup._scrub_string(s)
    except Exception:
        import re
        return re.sub(r"[\w.%+-]+@[\w.-]+\.[A-Za-z]{2,}", "[EMAIL_REDACTED]", s or "")


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


def _existing_open_issue(title: str) -> str:
    """Number of an already-open issue with this exact title, or "".

    Dedupe, because several alert sources REPEAT by design and this channel
    only became live on 2026-07-28. QC-063 fires whenever the last 3 fires
    share a failed step — true on every fire until the step is fixed — so a
    best-effort step dead for a week would have filed five identical issues.
    liveness.yml already de-dupes this way (comment on today's issue instead
    of opening another); fire_alert never did, because until now the channel
    was a permanent no-op and it did not matter.

    Best-effort and quiet: any failure returns "" and the caller just creates,
    which is the old behaviour. Duplicate noise is much cheaper than a missed
    alert, so this must never be able to suppress one.
    """
    try:
        if not _have_gh():
            return ""
        r = subprocess.run(
            ["gh", "issue", "list", "-R", GITHUB_REPO, "--state", "open",
             "--search", title, "--json", "number,title", "--limit", "50"],
            capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            return ""
        for item in json.loads(r.stdout or "[]"):
            if (item.get("title") or "").strip() == title.strip():
                return str(item.get("number") or "")
    except Exception:
        pass
    return ""


def _github_issue(title: str, body: str, labels: tuple) -> bool:
    """Create a GitHub issue out-of-band. Prefer the gh CLI (the wrapper
    already uses it for heartbeats); fall back to the REST API with a PAT.
    Returns False (not raising) when no auth/channel is available.

    An identical open issue is COMMENTED on rather than duplicated — see
    _existing_open_issue. A comment still counts as delivered: it bumps the
    thread and notifies its subscribers, which is what "the alert got out"
    means here."""
    label_args = []
    for label in labels:
        label_args += ["--label", label]
    # 1) gh CLI
    try:
        if _have_gh():
            dupe = _existing_open_issue(title)
            if dupe:
                rc = subprocess.run(
                    ["gh", "issue", "comment", dupe, "-R", GITHUB_REPO,
                     "--body", body],
                    capture_output=True, text=True, timeout=60).returncode
                if rc == 0:
                    return True
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


def github_configured() -> bool:
    """True when the GitHub channel has *some* credential to try.

    This mirrors _github_issue's two auth paths EXACTLY — gh CLI first (which
    authenticates from gh's own stored credentials and needs no GH_TOKEN at
    all), then a PAT for the REST fallback. QC-076 calls this rather than
    re-reading the env itself: an earlier version checked only GH_TOKEN, so it
    reported the channel dead on a box with `gh auth login` done, and alive on
    a runner whose token existed but was powerless.

    HONEST SCOPE — read this before trusting it: a credential existing is not
    the same as the credential WORKING. The 2026-07-27 outage had two causes
    (no token AND no `issues: write`); this function can only see the first.
    A token without `issues: write` still returns True here and still 403s in
    _github_issue. The permission half is asserted statically against
    daily.yml in tests/test_audit_batch7.py, because proving it at runtime
    would mean spending an API call on every fire to test the alarm.
    """
    return bool(_have_gh()
                or os.environ.get("GH_TOKEN")
                or os.environ.get("GITHUB_TOKEN"))


def _teams_webhook_url() -> str:
    """Resolve the Teams Incoming Webhook URL SECRET-FIRST, so it never has to
    live in the committed config.json:
      1. TEAMS_WEBHOOK_URL env var      (GitHub Actions secret / Cloud PC env)
      2. secrets/teams-webhook-url.txt  (gitignored -- the box's secret file)
      3. config.alerts.teams_webhook_url (back-compat ONLY; discouraged --
         config.json is committed, so a URL there lands in git history)
    """
    url = (os.environ.get("TEAMS_WEBHOOK_URL") or "").strip()
    if url:
        return url
    try:
        f = ROOT / "secrets" / "teams-webhook-url.txt"
        if f.exists():
            url = f.read_text(encoding="utf-8").strip()
            if url:
                return url
    except Exception:
        pass
    try:
        cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
        return ((cfg.get("alerts") or {}).get("teams_webhook_url") or "").strip()
    except Exception:
        return ""


def _teams(title: str, body: str) -> bool:
    """POST to the Teams Incoming Webhook (resolved secret-first; see
    _teams_webhook_url). No-op (returns False) when not configured. Independent
    of Outlook/MSAL."""
    url = _teams_webhook_url()
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
               labels: tuple = ("fire-alert",)) -> dict:
    """Raise an out-of-band alert on every available channel. Best-effort:
    a failing channel never blocks the others. Returns {channel: delivered?}.

    LABELS: the default is `fire-alert` alone. It used to include
    `cloud-pc-down`, and liveness.yml's recovery step closes EVERY open
    `cloud-pc-down` issue the moment it sees a fresh heartbeat. So a critical
    alert that defaulted its labels — e.g. assert_fire_integrity's "no
    verified report shipped" — could be filed and then auto-closed within
    hours by an unrelated watchdog, while the condition it reported was still
    true. Callers that genuinely mean "the box is down" pass that label
    explicitly and opt into the auto-close."""
    # Scrub once at the boundary so all four channels share one redaction
    # boundary (parity with the Sentry before_send hook). _scrub_string is
    # idempotent, so double-scrubbing is a harmless no-op.
    s_title = _scrub(title)
    s_body = _scrub(body)
    full_body = f"{s_body}\n\n— raised {_now()} (level={level})"
    record = {"ts": _now(), "level": level, "title": s_title, "body": s_body}
    results = {
        "stderr": _stderr_banner(s_title, full_body),
        "queue": _append_queue(record),
        "github": _github_issue(s_title, full_body, labels),
        "teams": _teams(s_title, full_body),
    }
    _warn_if_undeliverable(results)
    return results


#: Channels that can actually REACH a human who is not already reading the
#: run log. stderr and queue are local: on an ephemeral runner stderr is
#: buried in a log nobody opens on a normal day, and the queue file dies with
#: the container.
REMOTE_CHANNELS = ("github", "teams")


def undeliverable(results: dict) -> bool:
    """True when an alert reached NO remote channel — i.e. it was raised into
    the void and no human will learn of it from this call."""
    return not any(results.get(c) for c in REMOTE_CHANNELS)


def _warn_if_undeliverable(results: dict) -> None:
    """Say so, loudly, when the alarm itself failed to reach anyone.

    On 2026-07-27 the production fire was blocked by the QC-039 gate and
    raised a FIRE-ALERT — which returned {'github': False, 'teams': False}
    because the workflow gave that step no GH_TOKEN and no `issues: write`,
    and no Teams webhook is configured. The alert existed only as a stderr
    banner inside a failed job's log and a queue file on a runner that was
    then destroyed. Michael found out because the report never arrived.

    An alarm that cannot deliver is a silent failure of the thing whose whole
    job is not being silent, so it must not itself be silent. This prints a
    distinct, greppable line; QC-076 checks the same condition BEFORE a fire
    needs the alarm, so a broken channel is found while everything is fine.
    """
    if not undeliverable(results):
        return
    # Wrapped, like _stderr_banner: this runs on the ALREADY-BAD path (both
    # remote channels dead), and send_alert's contract is best-effort and
    # never-blocking. An unwrapped print here raises UnicodeEncodeError on a
    # non-UTF-8 stderr (the em dash) or ValueError on a closed one, turning
    # "the alarm could not deliver" into "the caller crashed" at the worst
    # possible moment. ASCII-only text, and swallow anything that's left.
    try:
        tried = ", ".join(f"{c}={bool(results.get(c))}" for c in REMOTE_CHANNELS)
        print(
            "\n!!! ALERT UNDELIVERABLE - this alarm reached NO remote channel "
            f"({tried}).\n"
            "!!! It exists only in this log and in a local queue file. If this "
            "run is on an ephemeral runner, that queue dies with the container "
            "and NOBODY WILL BE TOLD.\n"
            "!!! Fix: give the job `issues: write` + GH_TOKEN (GitHub channel), "
            "or set TEAMS_WEBHOOK_URL (Teams channel). See QC-076.",
            file=sys.stderr, flush=True)
    except Exception:
        pass


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Raise an out-of-band fire alert")
    ap.add_argument("--title", required=True)
    ap.add_argument("--body", default="")
    ap.add_argument("--level", default="error")
    args = ap.parse_args()
    res = send_alert(args.title, args.body, level=args.level)
    print(json.dumps(res))
    # Exit 0 only if a REMOTE channel took it. `any(res.values())` used to be
    # the test, but stderr and queue are always True on a healthy process, so
    # the CLI exited 0 for an alert that reached nobody — the exact condition
    # undeliverable() names. Same definition as the banner, one source of truth.
    return 1 if undeliverable(res) else 0


if __name__ == "__main__":
    raise SystemExit(main())
