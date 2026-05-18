"""
sentry_api.py — Sentry REST API wrapper for the active-observability layer.

Per Michael 2026-05-17 ("you can use sentry for self check and improvements
as well"). This module is the foundation for Phase 2: pulling Sentry's view
of the system INTO the daily audit + QC loop, so the pipeline becomes an
active participant in its own observability rather than a passive event
emitter.

USAGE

  from sentry_api import SentryAPI
  api = SentryAPI()  # silently no-ops if no auth token configured
  if api.enabled:
      issues = api.list_issues(project="hilmar-daily-tracker",
                                stats_period="14d", limit=20)
      for issue in issues:
          if issue["lastSeen"] is older than 24h and recent_commit_fixed(issue):
              api.resolve_issue(issue["id"], reason="fixed_in_<sha>")

AUTH TOKEN

Loaded from `secrets/sentry-auth-token.txt` (gitignored, same pattern as
DSN + QT password). If missing, all methods silent no-op — observability
must never block the pipeline.

ENDPOINTS

  GET    /api/0/projects/{org}/{project}/issues/         list issues
  PUT    /api/0/projects/{org}/{project}/issues/         bulk update issues
  PUT    /api/0/issues/{id}/                              update single issue
  POST   /api/0/organizations/{org}/releases/             create release
  GET    /api/0/issues/{id}/events/                       events for an issue
  POST   /api/0/organizations/{org}/dashboards/           create dashboard

Rate limit: 40 req/sec on paid tier — comfortably below for our use.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent

#: Sentry org slug — discovered from the API verify step.
DEFAULT_ORG = "idealx-llc"

#: Default project slug for Hilmar daily tracker. Other projects
#: (ol-quote-tracker, rate-blaster) can be addressed by passing
#: project=... to the API methods.
DEFAULT_PROJECT = "hilmar-daily-tracker"

#: Base URL — sentry.io for SaaS, self-hosted users override.
BASE_URL = "https://sentry.io/api/0"


def _load_token() -> Optional[str]:
    """Load auth token from secrets/sentry-auth-token.txt or env. None if missing."""
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


class SentryAPI:
    """Thin HTTP client over Sentry's REST API.

    All methods return Python dicts/lists on success and None on failure
    (never raise). Failure modes (no token, network error, 4xx/5xx) are
    silent — observability must never break the pipeline.
    """

    def __init__(self, org: str = DEFAULT_ORG, project: str = DEFAULT_PROJECT):
        self.org = org
        self.project = project
        self.token = _load_token()
        self.enabled = bool(self.token)
        # Lazy-imported so the module loads on systems without `requests`
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

    def _request(self, method: str, path: str, **kwargs):
        """All requests go through here so we have a single guarded surface."""
        if not self.enabled:
            return None
        s = self._sess()
        if s is None:
            return None
        url = f"{BASE_URL}{path}"
        try:
            r = s.request(method, url, timeout=kwargs.pop("timeout", 30), **kwargs)
            if r.status_code in (200, 201, 202, 204):
                if r.text:
                    return r.json()
                return {}
            # 404 is common (no issue, no release) — silent
            if r.status_code == 404:
                return None
            # Other failures: print quietly, don't crash
            print(f"⚠️  sentry_api {method} {path} → {r.status_code}: {r.text[:200]}")
            return None
        except Exception as e:
            print(f"⚠️  sentry_api {method} {path} → {type(e).__name__}: {e}")
            return None

    # ─────────────────────────────────────────────────────────────────
    # Issues
    # ─────────────────────────────────────────────────────────────────

    def list_issues(
        self,
        *,
        project: Optional[str] = None,
        stats_period: str = "14d",
        query: str = "is:unresolved",
        limit: int = 50,
    ) -> list[dict]:
        """List issues matching the query. Returns [] on any failure.

        Common queries:
          "is:unresolved"
          "is:unresolved level:error"
          "is:unresolved qc_check:QC-039"
          "is:resolved age:-24h"
        """
        proj = project or self.project
        params = {
            "statsPeriod": stats_period,
            "query": query,
            "limit": str(limit),
        }
        resp = self._request(
            "GET",
            f"/projects/{self.org}/{proj}/issues/",
            params=params,
        )
        return resp or []

    def resolve_issue(self, issue_id: str, reason: str = "auto-resolved") -> bool:
        """Mark an issue resolved. reason is stored as a note on the issue."""
        # PUT /api/0/issues/{id}/ with status=resolved
        resp = self._request(
            "PUT",
            f"/issues/{issue_id}/",
            json={"status": "resolved", "statusDetails": {}},
        )
        if resp is None:
            return False
        # Add a note explaining why it was auto-resolved
        self._request(
            "POST",
            f"/issues/{issue_id}/comments/",
            json={"text": f"Auto-resolved by qc_selfheal: {reason}"},
        )
        return True

    def get_issue_events(self, issue_id: str, limit: int = 5) -> list[dict]:
        """Recent events for an issue — useful for diagnosis context."""
        resp = self._request(
            "GET",
            f"/issues/{issue_id}/events/",
            params={"limit": str(limit)},
        )
        return resp or []

    # ─────────────────────────────────────────────────────────────────
    # Releases — for auto-resolve on commit
    # ─────────────────────────────────────────────────────────────────

    def create_release(
        self,
        version: str,
        *,
        projects: Optional[list[str]] = None,
        commits: Optional[list[dict]] = None,
        ref: Optional[str] = None,
    ) -> bool:
        """Create a release in Sentry. When a commit message says
        "Fixes SENTRY-XYZ" or files in the commit match files in an
        issue's stack-trace, Sentry auto-resolves that issue as
        "resolved in release <version>".

        commits format: [{"id": "<sha>", "repository": "<gh-repo>",
                          "message": "...", "author_email": "..."}]
        """
        projs = projects or [self.project]
        body = {
            "version": version,
            "projects": projs,
        }
        if ref:
            body["ref"] = ref
        if commits:
            body["commits"] = commits
        resp = self._request(
            "POST",
            f"/organizations/{self.org}/releases/",
            json=body,
        )
        return resp is not None

    # ─────────────────────────────────────────────────────────────────
    # Dashboards — programmatic KPI dashboard creation
    # ─────────────────────────────────────────────────────────────────

    def list_dashboards(self) -> list[dict]:
        return self._request("GET", f"/organizations/{self.org}/dashboards/") or []

    def create_dashboard(self, title: str, widgets: list[dict]) -> Optional[dict]:
        """Create a dashboard with the given widgets. widgets is a list
        of widget config dicts per Sentry's dashboard widget API."""
        return self._request(
            "POST",
            f"/organizations/{self.org}/dashboards/",
            json={"title": title, "widgets": widgets},
        )


def auto_resolve_stale_issues(
    api: SentryAPI,
    *,
    stale_hours: int = 24,
    dry_run: bool = False,
) -> dict:
    """Auto-resolution pass: any UNRESOLVED issue that hasn't fired in
    the last `stale_hours` hours is closed with reason='auto-resolved-
    stale'. This kills the long tail of issues that were fixed but
    never manually resolved.

    Returns: {"resolved": N, "skipped": M, "errors": K}
    """
    stats = {"resolved": 0, "skipped": 0, "errors": 0, "details": []}
    if not api.enabled:
        return stats
    issues = api.list_issues(stats_period="14d", query="is:unresolved", limit=100)
    now = datetime.now(timezone.utc)
    cutoff = now.timestamp() - stale_hours * 3600

    for issue in issues:
        try:
            last_seen_str = issue.get("lastSeen", "")
            if not last_seen_str:
                stats["skipped"] += 1
                continue
            last_seen = datetime.fromisoformat(last_seen_str.replace("Z", "+00:00"))
            if last_seen.timestamp() >= cutoff:
                stats["skipped"] += 1  # still recent
                continue
            # Stale — resolve
            if dry_run:
                stats["details"].append(f"would resolve {issue['shortId']} (last seen {last_seen_str})")
            else:
                ok = api.resolve_issue(
                    issue["id"],
                    reason=f"stale: no events in {stale_hours}h+",
                )
                if ok:
                    stats["resolved"] += 1
                    stats["details"].append(f"resolved {issue['shortId']} ({issue['title'][:60]})")
                else:
                    stats["errors"] += 1
        except Exception as e:
            stats["errors"] += 1
            stats["details"].append(f"err on {issue.get('shortId', '?')}: {e}")

    return stats


def get_issue_summary(
    api: SentryAPI,
    *,
    period: str = "24h",
) -> dict:
    """One-shot summary for the daily audit email's Sentry section.

    Returns:
      {
        "unresolved_count": int,
        "new_in_period": list[issue dicts],
        "recurring": list[issue dicts where eventCount>=3 in period],
        "resolved_in_period": list[issue dicts],
        "total_events_in_period": int,
      }
    """
    summary = {
        "unresolved_count": 0,
        "new_in_period": [],
        "recurring": [],
        "resolved_in_period": [],
        "total_events_in_period": 0,
    }
    if not api.enabled:
        return summary

    # Unresolved issues — full list
    unresolved = api.list_issues(stats_period="14d", query="is:unresolved", limit=100)
    summary["unresolved_count"] = len(unresolved)

    # New in period (firstSeen within period)
    period_h = _period_to_hours(period)
    cutoff_ts = datetime.now(timezone.utc).timestamp() - period_h * 3600
    for issue in unresolved:
        first_seen_s = issue.get("firstSeen", "")
        if not first_seen_s:
            continue
        try:
            first_seen = datetime.fromisoformat(first_seen_s.replace("Z", "+00:00"))
            if first_seen.timestamp() >= cutoff_ts:
                summary["new_in_period"].append(issue)
        except Exception:
            continue
        # Recurring: count events in period
        try:
            count = int(issue.get("count", 0))
            if count >= 3:
                summary["recurring"].append(issue)
        except Exception:
            pass

    # Resolved in period
    resolved = api.list_issues(
        stats_period=period,
        query="is:resolved",
        limit=20,
    )
    summary["resolved_in_period"] = resolved

    # Total events in period
    summary["total_events_in_period"] = sum(
        int(i.get("count", 0)) for i in (unresolved or [])
    )

    return summary


def _period_to_hours(period: str) -> int:
    """'24h' → 24, '7d' → 168, '14d' → 336, etc."""
    if not period:
        return 24
    period = period.strip().lower()
    if period.endswith("h"):
        return int(period[:-1])
    if period.endswith("d"):
        return int(period[:-1]) * 24
    return 24


# ─────────────────────────────────────────────────────────────────────
# CLI — quick sanity checks + manual queries
# ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    api = SentryAPI()
    if not api.enabled:
        print("⚠️  Sentry API disabled — no auth token configured.")
        sys.exit(1)
    cmd = sys.argv[1] if len(sys.argv) > 1 else "summary"
    if cmd == "summary":
        s = get_issue_summary(api)
        print(f"Unresolved issues: {s['unresolved_count']}")
        print(f"New in last 24h: {len(s['new_in_period'])}")
        print(f"Recurring (>=3 events): {len(s['recurring'])}")
        print(f"Resolved in last 24h: {len(s['resolved_in_period'])}")
        print(f"Total events 24h: {s['total_events_in_period']}")
        for issue in s["new_in_period"][:5]:
            print(f"  🆕 {issue.get('shortId', '?'):12} {issue.get('title', '?')[:70]}")
        for issue in s["recurring"][:5]:
            print(f"  🔁 {issue.get('shortId', '?'):12} ({issue.get('count', '?')}x) {issue.get('title', '?')[:60]}")
    elif cmd == "list":
        for issue in api.list_issues(stats_period="14d"):
            print(f"  {issue.get('shortId', '?'):12} [{issue.get('level', '?'):7}] "
                  f"{issue.get('count', '?'):>4}x  {issue.get('title', '?')[:80]}")
    elif cmd == "stale":
        dry = "--apply" not in sys.argv
        result = auto_resolve_stale_issues(api, dry_run=dry)
        print(f"Stale resolve: resolved={result['resolved']} "
              f"skipped={result['skipped']} errors={result['errors']}"
              + (" (DRY-RUN — pass --apply to actually resolve)" if dry else ""))
        for d in result["details"][:20]:
            print(f"  {d}")
    elif cmd == "release":
        if len(sys.argv) < 3:
            print("usage: sentry_api.py release <version>")
            sys.exit(2)
        version = sys.argv[2]
        ok = api.create_release(version)
        print(f"Release {version} created: {ok}")
    else:
        print(f"unknown command: {cmd}")
        print("usage: sentry_api.py [summary|list|stale [--apply]|release <version>]")
        sys.exit(2)
