"""
sentry_seer.py — Sentry Seer (AI-powered issue analysis + autofix) integration.

Per Michael 2026-05-18 ("are you using sentry/seer to properly handle qc
and self heal and check also? you have the web hooks.. the way we do in
main ratechecker system with sentry/seer"). The main rate-checker
(ol-quote-tracker) uses Seer for AI-driven issue analysis + autofix.
This module mirrors that pattern for hilmar-daily-tracker.

WHAT SEER DOES

Seer is Sentry's AI feature suite:
  1. Issue Summary — reads the issue + recent events + stack-trace,
     produces a 1-paragraph plain-English diagnosis
  2. Autofix — proposes a code change that would fix the issue,
     either as a patch or a step-by-step plan
  3. Solution — recommends actions even when autofix can't directly
     produce a code change (e.g., "look at config X" or "run command Y")

INTEGRATION FLOW

Every daily pipeline fire:
  1. qc_selfheal queries Sentry for unresolved issues (sentry_api.py)
  2. For each unresolved issue, poll Seer for:
     - Issue Summary (sentry_seer.get_issue_summary)
     - Autofix state (sentry_seer.get_autofix_state)
  3. Include Seer's findings in the daily audit email (gen_improvements_report)
  4. For high-confidence autofix suggestions, log them as
     suggested-by-Seer items in the audit so operator can review + apply

ENABLEMENT (manual, one-time)

Seer needs to be enabled per-project in Sentry UI:
  1. Sentry → Settings → Projects → hilmar-daily-tracker
  2. "Seer Automation" or "AI Features" section
  3. Toggle ON. Accept the data-processing terms.
  4. Optional: set Seer to auto-trigger on new issues vs only-on-demand

Until enabled, the API endpoints return 404 and this module gracefully
no-ops — the daily audit still works without the Seer section.

ENDPOINTS (per Sentry docs as of 2025)

  POST /api/0/issues/{id}/autofix/             trigger autofix
  GET  /api/0/issues/{id}/autofix/state/       poll autofix progress + result
  POST /api/0/issues/{id}/summarize/           trigger issue summary
  GET  /api/0/issues/{id}/                     issue metadata incl. seer fields
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
BASE_URL = "https://sentry.io/api/0"


def _load_token() -> Optional[str]:
    f = ROOT / "secrets" / "sentry-auth-token.txt"
    if not f.exists():
        f = ROOT.parent / "secrets" / "sentry-auth-token.txt"
    if f.exists():
        try:
            t = f.read_text(encoding="utf-8").strip()
            if t and (t.startswith("sntrys_") or t.startswith("sntryu_")):
                return t
        except Exception:
            pass
    return os.environ.get("SENTRY_AUTH_TOKEN") or None


class SentrySeer:
    """Thin client for Sentry Seer endpoints. All methods return None on
    failure (404 = Seer not enabled; 4xx/5xx = real error). Never raises.
    """

    def __init__(self):
        self.token = _load_token()
        self.enabled = bool(self.token)
        self._session = None

    def _sess(self):
        if self._session is None:
            try:
                import requests
                self._session = requests.Session()
                self._session.headers.update({
                    "Authorization": f"Bearer {self.token}",
                    "Content-Type": "application/json",
                })
            except ImportError:
                self.enabled = False
        return self._session

    def _req(self, method: str, path: str, **kwargs):
        if not self.enabled:
            return None
        s = self._sess()
        if s is None:
            return None
        try:
            r = s.request(method, f"{BASE_URL}{path}",
                           timeout=kwargs.pop("timeout", 30), **kwargs)
            if r.status_code in (200, 201, 202, 204):
                return r.json() if r.text else {}
            if r.status_code == 404:
                # Seer not enabled or no autofix triggered yet — silent
                return None
            return None
        except Exception:
            return None

    def get_issue_summary(self, issue_id: str) -> Optional[dict]:
        """Get Seer's plain-English summary of an issue. Returns None if
        Seer not enabled OR no summary has been generated."""
        return self._req("GET", f"/issues/{issue_id}/summarize/")

    def trigger_summary(self, issue_id: str) -> Optional[dict]:
        """Ask Seer to generate a summary for this issue (POST). Returns
        the queued/in-progress response."""
        return self._req("POST", f"/issues/{issue_id}/summarize/")

    def get_autofix_state(self, issue_id: str) -> Optional[dict]:
        """Check Seer's autofix state for this issue. Returns:
          { status: 'NEED_MORE_INFORMATION' | 'PROCESSING' | 'COMPLETED' | 'ERROR',
            steps: [...], pr_url: '...', confidence: float, ... }
        None if autofix never triggered or Seer not enabled."""
        return self._req("GET", f"/issues/{issue_id}/autofix/")

    def trigger_autofix(self, issue_id: str, instruction: str = "") -> Optional[dict]:
        """Ask Seer to attempt an autofix for this issue. instruction is
        an optional natural-language hint to focus the AI."""
        body = {"instruction": instruction} if instruction else {}
        return self._req("POST", f"/issues/{issue_id}/autofix/", json=body)


def enrich_audit_with_seer(issues: list[dict], *, max_issues: int = 5) -> list[dict]:
    """For each issue, pull Seer summary + autofix state. Returns the
    original list with `seer_summary` and `seer_autofix` keys added per
    issue when available. Silent no-op when Seer not enabled.

    Bounded to `max_issues` to keep daily-audit generation fast — Seer
    polls are async and we don't want to wait on a slow API to render
    the audit.
    """
    seer = SentrySeer()
    if not seer.enabled:
        return issues
    enriched = []
    for issue in issues[:max_issues]:
        try:
            summary = seer.get_issue_summary(issue["id"])
            if summary:
                issue["seer_summary"] = summary.get("summary", "")
            autofix = seer.get_autofix_state(issue["id"])
            if autofix:
                issue["seer_autofix"] = {
                    "status": autofix.get("status"),
                    "pr_url": autofix.get("pr_url"),
                    "confidence": autofix.get("confidence"),
                }
        except Exception:
            pass
        enriched.append(issue)
    # Pass through any issues beyond max_issues unchanged
    enriched.extend(issues[max_issues:])
    return enriched


def trigger_autofix_for_recent_errors(*, hours: int = 2, dry_run: bool = False) -> dict:
    """Auto-trigger Seer autofix on any error-level issue that fired in
    the last `hours`. Designed to be called from qc_selfheal at the end
    of every pipeline fire — Seer gets a head start on diagnosing before
    the next-day audit needs the results.

    Returns: {"triggered": N, "skipped": M, "details": [...]}
    """
    from datetime import datetime, timedelta, timezone
    result = {"triggered": 0, "skipped": 0, "details": []}
    seer = SentrySeer()
    if not seer.enabled:
        return result

    # Pull recent error-level issues via the regular API
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from sentry_api import SentryAPI
    api = SentryAPI()
    if not api.enabled:
        return result

    issues = api.list_issues(stats_period=f"{hours}h",
                              query="is:unresolved level:error",
                              limit=20)
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=hours)
    for issue in issues:
        last_seen = issue.get("lastSeen", "")
        if not last_seen:
            continue
        try:
            ls = datetime.fromisoformat(last_seen.replace("Z", "+00:00"))
            if ls < cutoff:
                result["skipped"] += 1
                continue
        except Exception:
            continue
        # Check existing autofix state — don't double-trigger
        state = seer.get_autofix_state(issue["id"])
        if state and state.get("status") in ("PROCESSING", "COMPLETED"):
            result["skipped"] += 1
            result["details"].append(f"{issue.get('shortId')}: autofix already {state.get('status')}")
            continue
        if dry_run:
            result["details"].append(f"would trigger: {issue.get('shortId')} - {issue.get('title', '')[:60]}")
            continue
        trig = seer.trigger_autofix(issue["id"])
        if trig is not None:
            result["triggered"] += 1
            result["details"].append(f"triggered autofix: {issue.get('shortId')}")
    return result


if __name__ == "__main__":
    import sys as _sys
    seer = SentrySeer()
    if not seer.enabled:
        print("⚠️  No auth token — Seer integration disabled")
        _sys.exit(1)

    cmd = _sys.argv[1] if len(_sys.argv) > 1 else "trigger"
    if cmd == "trigger":
        dry = "--apply" not in _sys.argv
        result = trigger_autofix_for_recent_errors(dry_run=dry)
        print(f"Seer autofix: triggered={result['triggered']} skipped={result['skipped']}"
              + (" (DRY-RUN — pass --apply to actually trigger)" if dry else ""))
        for d in result["details"][:20]:
            print(f"  {d}")
    elif cmd == "summary":
        if len(_sys.argv) < 3:
            print("usage: sentry_seer.py summary <issue_id>")
            _sys.exit(2)
        s = seer.get_issue_summary(_sys.argv[2])
        print(json.dumps(s, indent=2) if s else "(no summary available — Seer may not be enabled)")
    elif cmd == "autofix":
        if len(_sys.argv) < 3:
            print("usage: sentry_seer.py autofix <issue_id>")
            _sys.exit(2)
        s = seer.get_autofix_state(_sys.argv[2])
        print(json.dumps(s, indent=2) if s else "(no autofix state — Seer not enabled or autofix never triggered)")
    else:
        print(f"unknown command: {cmd}")
        print("usage: sentry_seer.py [trigger [--apply] | summary <issue_id> | autofix <issue_id>]")
        _sys.exit(2)
