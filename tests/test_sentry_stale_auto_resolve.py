"""Tests for the stale-error auto-resolve added to qc_actions_from_sentry.

Background: HILMAR-DAILY-TRACKER-5 (NameError 'os' not defined) was fixed
in code on 2026-05-17 but sat unresolved in Sentry for 11 days because no
ACTIONS entry mapped to it — it was an unmapped error and ERROR_LEVEL_DEFAULT
routes to `trigger_seer`, which doesn't resolve. From 2026-05-28 (per
Michael "do all 7-9"), any unmapped issue silent >= STALE_AUTO_RESOLVE_DAYS
now auto-resolves with a comment."""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import qc_actions_from_sentry as QA  # noqa: E402


def _issue(*, short_id: str, last_seen: datetime | None, level: str = "error",
           title: str = "Some unmapped error", tags: list | None = None) -> dict:
    return {
        "id": "issue-" + short_id,
        "shortId": short_id,
        "title": title,
        "level": level,
        "tags": tags or [],
        "lastSeen": last_seen.isoformat().replace("+00:00", "Z") if last_seen else None,
    }


def test_unmapped_stale_error_routes_to_auto_resolve():
    """An unmapped error silent for 11 days (the HILMAR-DAILY-TRACKER-5
    case) now routes to resolve_if_stale + auto_resolve_safe=True."""
    eleven_days_ago = datetime.now(timezone.utc) - timedelta(days=11)
    issue = _issue(
        short_id="TRACKER-5",
        title="NameError: name 'os' is not defined",
        last_seen=eleven_days_ago,
    )
    key, spec = QA._action_lookup(issue)
    assert key == "unmapped-stale"
    assert spec["action"] == "resolve_if_stale"
    assert spec["auto_resolve_safe"] is True
    assert "11" in spec["name"]  # mentions the age


def test_unmapped_recent_error_still_routes_to_seer():
    """A generic error that fired in the last day stays on the SEER triage
    path — don't resolve fresh issues."""
    two_hours_ago = datetime.now(timezone.utc) - timedelta(hours=2)
    issue = _issue(
        short_id="TRACKER-42",
        title="TypeError: unexpected NoneType in renderer",
        last_seen=two_hours_ago,
    )
    key, spec = QA._action_lookup(issue)
    assert key == "unmapped-error"
    assert spec["action"] == "trigger_seer"


def test_cron_missed_checkin_routes_to_resolve_if_post_fix():
    """HILMAR-DAILY-TRACKER-9 (cron monitor missed check-in) must NOT go to
    Seer — Seer can't analyze a cron miss, so it never cleared. It routes to
    resolve_if_post_fix so the deployed margin fix closes it (2026-06-16)."""
    one_hour_ago = datetime.now(timezone.utc) - timedelta(hours=1)
    for title in ("Cron failure: hilmar-daily-pipeline",
                  "Your monitor is failing: A missed check-in was detected."):
        issue = _issue(short_id="TRACKER-9", title=title, last_seen=one_hour_ago)
        key, spec = QA._action_lookup(issue)
        assert key == "cron.missed_checkin", title
        assert spec["action"] == "resolve_if_post_fix"
        assert spec["auto_resolve_safe"] is True


def test_unmapped_error_just_under_threshold_does_not_auto_resolve():
    """At STALE_AUTO_RESOLVE_DAYS - 1, stay on the seer path."""
    nearly_stale = datetime.now(timezone.utc) - timedelta(days=QA.STALE_AUTO_RESOLVE_DAYS - 1, hours=22)
    issue = _issue(short_id="TRACKER-X", last_seen=nearly_stale)
    key, spec = QA._action_lookup(issue)
    assert spec["action"] == "trigger_seer"


def test_unmapped_warning_silent_long_also_auto_resolves():
    """Stale auto-resolve isn't level-specific — applies to any unmapped
    issue regardless of error/warning level."""
    long_ago = datetime.now(timezone.utc) - timedelta(days=30)
    issue = _issue(short_id="TRACKER-Y", level="warning", last_seen=long_ago)
    key, spec = QA._action_lookup(issue)
    assert spec["action"] == "resolve_if_stale"


def test_mapped_issue_keeps_its_explicit_action():
    """A mapped issue with an explicit ACTIONS entry retains that route
    even if stale — operator might want to keep it visible (e.g.
    flag_for_operator) regardless of age."""
    long_ago = datetime.now(timezone.utc) - timedelta(days=30)
    # Use a real qc_check that exists in ACTIONS to confirm precedence.
    qc_keys = list(QA.ACTIONS.keys())
    if not qc_keys:
        return  # nothing to test against
    mapped_key = qc_keys[0]
    issue = _issue(
        short_id="MAPPED",
        title=f"{mapped_key}: something went wrong",
        last_seen=long_ago,
        tags=[{"key": "qc_check", "value": mapped_key}],
    )
    key, spec = QA._action_lookup(issue)
    assert key == mapped_key
    # Whatever the mapped action is, it's NOT the stale-auto-resolve route
    assert spec is QA.ACTIONS[mapped_key]


def test_unmapped_with_no_lastseen_falls_through_to_default():
    """No lastSeen → can't compute age → don't try to auto-resolve.
    Falls back to the level-appropriate default."""
    issue = _issue(short_id="NOLS", last_seen=None)
    key, spec = QA._action_lookup(issue)
    assert spec["action"] == "trigger_seer"  # error-level default
