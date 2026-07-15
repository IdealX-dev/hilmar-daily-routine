"""Heartbeat-model Sentry cron check-in (HILMAR-DAILY-TRACKER-A fix).

The cron check-in used to be sent ONLY from run_pipeline.py's in-process
start/finish, so when a firing host's Sentry init didn't reach Sentry the
monitor false-paged 'missed check-in' even though the report shipped.
heartbeat.yml now sends a host-agnostic check-in via
sentry_setup.heartbeat_checkin (the same signal liveness trusts).

These lock: success→'ok', failure→'error', the monitor slug, the no-DSN
no-op, and the CLI status mapping + always-exit-0 contract.
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import sentry_setup  # noqa: E402


def _fake_sdk(monkeypatch):
    """Force init True + a fake sentry_sdk so heartbeat_checkin exercises the
    real capture_checkin call path without a live DSN."""
    fake = MagicMock()
    monkeypatch.setitem(sys.modules, "sentry_sdk", fake)
    monkeypatch.setattr(sentry_setup, "init", lambda *a, **k: True)
    monkeypatch.setattr(sentry_setup, "ensure_monitor_schedule", lambda: True)
    return fake


def test_success_sends_ok_checkin_for_the_monitor(monkeypatch):
    fake = _fake_sdk(monkeypatch)
    assert sentry_setup.heartbeat_checkin(True) is True
    kwargs = fake.crons.capture_checkin.call_args.kwargs
    assert kwargs["monitor_slug"] == sentry_setup.MONITOR_SLUG == "hilmar-daily-pipeline"
    assert kwargs["status"] == "ok"
    # monitor_config passed so the schedule auto-provisions/updates.
    assert kwargs["monitor_config"] is sentry_setup._MONITOR_CONFIG


def test_failure_sends_error_checkin(monkeypatch):
    fake = _fake_sdk(monkeypatch)
    assert sentry_setup.heartbeat_checkin(False) is True
    assert fake.crons.capture_checkin.call_args.kwargs["status"] == "error"


def test_flushes_before_return(monkeypatch):
    """A short-lived CLI must flush or the check-in event is lost on exit."""
    fake = _fake_sdk(monkeypatch)
    sentry_setup.heartbeat_checkin(True)
    assert fake.flush.called


def test_noop_when_no_dsn(monkeypatch):
    """No DSN / no SDK → clean no-op, never raises, returns False."""
    monkeypatch.setattr(sentry_setup, "init", lambda *a, **k: False)
    assert sentry_setup.heartbeat_checkin(True) is False


def test_cli_maps_status_and_always_exits_zero(monkeypatch):
    import sentry_cron_checkin
    seen = []
    monkeypatch.setattr(
        sentry_cron_checkin.sentry_setup, "heartbeat_checkin",
        lambda success: seen.append(success) or True,
    )
    assert sentry_cron_checkin.main(["--status", "success"]) == 0
    assert seen == [True]
    seen.clear()
    assert sentry_cron_checkin.main(["--status", "failed"]) == 0
    assert seen == [False]


def test_cli_no_op_path_exits_zero(monkeypatch):
    """End-to-end no-op (heartbeat_checkin returns False) still exits 0 —
    observability must never fail the heartbeat job."""
    import sentry_cron_checkin
    monkeypatch.setattr(
        sentry_cron_checkin.sentry_setup, "heartbeat_checkin", lambda success: False
    )
    assert sentry_cron_checkin.main(["--status", "success"]) == 0


def test_heartbeat_checkin_never_raises_on_sdk_error(monkeypatch):
    """If capture_checkin itself throws, heartbeat_checkin swallows it and
    returns False — the pipeline/heartbeat must never crash on Sentry."""
    fake = MagicMock()
    fake.crons.capture_checkin.side_effect = RuntimeError("sentry down")
    monkeypatch.setitem(sys.modules, "sentry_sdk", fake)
    monkeypatch.setattr(sentry_setup, "init", lambda *a, **k: True)
    monkeypatch.setattr(sentry_setup, "ensure_monitor_schedule", lambda: True)
    assert sentry_setup.heartbeat_checkin(True) is False
