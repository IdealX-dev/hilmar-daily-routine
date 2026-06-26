"""Regression test for the audit fix to scripts/fire_alert.py.

Audit finding (security, low): send_alert() forwarded caller-supplied
title/body verbatim to a GitHub issue and a Teams webhook with no PII
scrubbing — the parallel out-of-band path leaked the same class of data the
Sentry path is careful to scrub (emails, MDOLX refs, internal req_ ids).

The fix routes title/body through fire_alert._scrub (which reuses
sentry_setup._scrub_string) before every egress channel. These tests fail
without the fix (raw PII reaches the captured bodies / queue / stderr) and
pass with it.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import fire_alert  # noqa: E402

_RAW_EMAIL = "lupfold@hilmaringredients.com"
_RAW_MDOLX = "MDOLX12345"
_RAW_REQ = "req_0123456789abcdef0123"
_BODY = (
    f"Pipeline failed for {_RAW_EMAIL} on booking {_RAW_MDOLX} "
    f"(internal {_RAW_REQ}); investigate."
)


def _no_raw_pii(text: str) -> None:
    assert _RAW_EMAIL not in text
    assert _RAW_MDOLX not in text
    assert _RAW_REQ not in text


def test_github_and_teams_bodies_are_scrubbed(monkeypatch, tmp_path):
    """The body handed to the GitHub-issue and Teams channels must be redacted."""
    captured: dict[str, str] = {}

    monkeypatch.setattr(fire_alert, "REPORTS", tmp_path)
    monkeypatch.setattr(fire_alert, "ALERTS_QUEUE", tmp_path / "alerts-queue.json")

    def fake_github(title, body, labels):
        captured["github_title"] = title
        captured["github_body"] = body
        return True

    def fake_teams(title, body):
        captured["teams_title"] = title
        captured["teams_body"] = body
        return True

    monkeypatch.setattr(fire_alert, "_github_issue", fake_github)
    monkeypatch.setattr(fire_alert, "_teams", fake_teams)

    fire_alert.send_alert("Fire failed", _BODY, level="critical")

    for key in ("github_body", "teams_body"):
        _no_raw_pii(captured[key])
    # Confirm the redaction markers are present (i.e. it was actually scrubbed,
    # not merely that the raw strings happened to differ).
    assert "[EMAIL_REDACTED]" in captured["github_body"]
    assert "[MDOLX_REDACTED]" in captured["github_body"]
    assert "[REQ_ID]" in captured["github_body"]


def test_queue_record_is_scrubbed(monkeypatch, tmp_path):
    """The durable alerts-queue.json record must not persist raw PII."""
    q = tmp_path / "alerts-queue.json"
    monkeypatch.setattr(fire_alert, "REPORTS", tmp_path)
    monkeypatch.setattr(fire_alert, "ALERTS_QUEUE", q)
    monkeypatch.setattr(fire_alert, "_github_issue", lambda *a, **k: False)
    monkeypatch.setattr(fire_alert, "_teams", lambda *a, **k: False)

    fire_alert.send_alert("Fire failed", _BODY)

    raw = q.read_text(encoding="utf-8")
    _no_raw_pii(raw)
    assert "[EMAIL_REDACTED]" in raw


def test_stderr_banner_is_scrubbed(monkeypatch, tmp_path, capsys):
    """The stderr ::error:: banner (captured in the run-log) must be redacted."""
    monkeypatch.setattr(fire_alert, "REPORTS", tmp_path)
    monkeypatch.setattr(fire_alert, "ALERTS_QUEUE", tmp_path / "alerts-queue.json")
    monkeypatch.setattr(fire_alert, "_github_issue", lambda *a, **k: False)
    monkeypatch.setattr(fire_alert, "_teams", lambda *a, **k: False)

    fire_alert.send_alert("Fire failed", _BODY)

    err = capsys.readouterr().err
    _no_raw_pii(err)


def test_scrub_helper_never_raises_and_is_idempotent():
    """_scrub must be pure best-effort: never raise, and idempotent."""
    once = fire_alert._scrub(_BODY)
    twice = fire_alert._scrub(once)
    assert once == twice
    _no_raw_pii(once)
    # Edge inputs must not blow up the alert path.
    assert fire_alert._scrub("") == ""


def test_send_alert_still_best_effort(monkeypatch, tmp_path):
    """Scrubbing must not change the best-effort contract: a failing channel
    never blocks the others, and send_alert returns a {channel: bool} map."""
    monkeypatch.setattr(fire_alert, "REPORTS", tmp_path)
    monkeypatch.setattr(fire_alert, "ALERTS_QUEUE", tmp_path / "alerts-queue.json")
    monkeypatch.setattr(fire_alert, "_github_issue", lambda *a, **k: False)
    monkeypatch.setattr(fire_alert, "_teams", lambda *a, **k: False)

    res = fire_alert.send_alert("test title", "plain body", level="error")
    assert set(res) == {"stderr", "queue", "github", "teams"}
    assert res["queue"] is True and res["stderr"] is True
