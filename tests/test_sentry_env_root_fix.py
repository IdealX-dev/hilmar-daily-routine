"""Root fix for the daily HILMAR-DAILY-TRACKER-A 'missed check-in' pages.

Sentry Crons alerts PER ENVIRONMENT. _detect_environment only recognized the
Cloud PC hostname as 'production', so after the GitHub Actions cutover every
check-in landed in the 'manual' environment while the monitor's 'production'
environment (seeded by the Cloud PC era) sat check-in-less and paged missed
check-in every weekday at 22:57 ET — 26 straight — even on nights the
heartbeat check-in verifiably succeeded (2026-07-15 job log: 'OK Sentry cron
check-in sent').

Locks: GH Actions reports env 'production'; explicit SENTRY_ENVIRONMENT still
wins; ensure_monitor_schedule DETECTS orphaned monitor environments and warns
with the manual fix — it never deletes monitoring config on its own (operator
decision, per the 2026-07-16 review).
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import sentry_api  # noqa: E402
import sentry_setup  # noqa: E402

# ── _detect_environment ──────────────────────────────────────────────

def test_github_actions_is_production(monkeypatch):
    """The GH Actions runner IS the production fire host since the cutover."""
    monkeypatch.delenv("SENTRY_ENVIRONMENT", raising=False)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    assert sentry_setup._detect_environment() == "production"


def test_explicit_env_override_beats_github_actions(monkeypatch):
    monkeypatch.setenv("SENTRY_ENVIRONMENT", "staging")
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    assert sentry_setup._detect_environment() == "staging"


def test_non_actions_host_is_not_production(monkeypatch):
    monkeypatch.delenv("SENTRY_ENVIRONMENT", raising=False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    assert sentry_setup._detect_environment() != "production"


# ── ensure_monitor_schedule DETECTS orphaned environments (no delete) ─

def _fake_api(environments):
    api = MagicMock()
    api.enabled = True
    api.update_monitor.return_value = True
    api.get_monitor.return_value = {"environments": environments}
    return api


def test_orphaned_environment_is_warned_not_deleted(monkeypatch, capsys):
    """An env that is NOT what this host reports pages 'missed' forever —
    surface it loudly with the manual fix, but NEVER auto-delete monitoring
    config (operator decision)."""
    api = _fake_api([{"name": "manual"}, {"name": "production"}])
    monkeypatch.setattr(sentry_api, "SentryAPI", lambda *a, **k: api)
    monkeypatch.setattr(sentry_setup, "_detect_environment", lambda: "production")
    assert sentry_setup.ensure_monitor_schedule() is True
    out = capsys.readouterr().out
    assert "orphaned" in out and "'manual'" in out
    assert "MANUAL FIX" in out
    # The API object must expose no delete path that this code calls.
    assert not [c for c in api.mock_calls if "delete" in str(c).lower()], (
        "ensure_monitor_schedule must never delete monitor config"
    )


def test_current_environment_alone_stays_quiet(monkeypatch, capsys):
    api = _fake_api([{"name": "production"}])
    monkeypatch.setattr(sentry_api, "SentryAPI", lambda *a, **k: api)
    monkeypatch.setattr(sentry_setup, "_detect_environment", lambda: "production")
    assert sentry_setup.ensure_monitor_schedule() is True
    assert "orphaned" not in capsys.readouterr().out


def test_detection_failure_never_breaks_schedule_alignment(monkeypatch):
    """Detection is best-effort: get_monitor raising must not fail the call."""
    api = _fake_api([])
    api.get_monitor.side_effect = RuntimeError("api down")
    monkeypatch.setattr(sentry_api, "SentryAPI", lambda *a, **k: api)
    assert sentry_setup.ensure_monitor_schedule() is True
