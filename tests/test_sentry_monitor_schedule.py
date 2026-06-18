"""Sentry cron-monitor schedule alignment — 2026-06-17 stale-schedule fix.

The live monitor stayed pinned to the old 10 AM ET / 95-min config while the
code moved to 6 PM ET / 290 — because the SDK check-in's monitor_config does
NOT reliably update an existing monitor's schedule. So Sentry paged 'missed
check-in' every day at 11:42 AM ET (= 10:07 + 95). ensure_monitor_schedule()
force-aligns the monitor via the auth-token REST API instead.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import sentry_setup as SS  # noqa: E402
from sentry_api import SentryAPI  # noqa: E402


def test_monitor_config_rest_shape():
    c = SS._monitor_config_rest()
    # REST shape = flat schedule string + schedule_type (not the SDK's nested form)
    assert c["schedule_type"] == "crontab"
    assert c["schedule"] == "7 18 * * 1-5"
    assert c["timezone"] == "America/New_York"
    assert c["checkin_margin"] == 290
    assert c["max_runtime"] == 60


def test_update_monitor_put_then_post_fallback(monkeypatch):
    api = SentryAPI()
    api.enabled = True  # force on without a real token
    calls = []

    def fake_request(method, path, **kw):
        calls.append((method, path, kw.get("json")))
        return None if method == "PUT" else {"slug": "hilmar-daily-pipeline"}

    monkeypatch.setattr(api, "_request", fake_request)
    ok = api.update_monitor("hilmar-daily-pipeline", SS._monitor_config_rest())
    assert ok is True
    # PUT to the slug endpoint first; POST to create on 404
    assert calls[0][0] == "PUT" and calls[0][1].endswith("/monitors/hilmar-daily-pipeline/")
    assert calls[1][0] == "POST" and calls[1][1].endswith("/monitors/")
    body = calls[0][2]
    assert body["type"] == "cron_job"
    assert body["slug"] == "hilmar-daily-pipeline"
    assert body["config"]["schedule"] == "7 18 * * 1-5"
    assert body["config"]["checkin_margin"] == 290


def test_update_monitor_put_success_skips_post(monkeypatch):
    api = SentryAPI()
    api.enabled = True
    calls = []

    def fake_request(method, path, **kw):
        calls.append(method)
        return {"slug": "x"}   # PUT succeeds

    monkeypatch.setattr(api, "_request", fake_request)
    assert api.update_monitor("hilmar-daily-pipeline", {}) is True
    assert calls == ["PUT"]   # no POST when the update lands


def test_ensure_monitor_schedule_noop_without_token(monkeypatch):
    # No auth token → SentryAPI disabled → safe no-op, never raises.
    import sentry_api
    monkeypatch.setattr(sentry_api, "_load_token", lambda: None)
    assert SS.ensure_monitor_schedule() is False
