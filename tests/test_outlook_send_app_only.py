"""Tests for the app-only send path in scripts/outlook_send.py.

App-only auth has no /me context, so the off-Cloud-PC fire must send as the
shared mailbox via /users/{mailbox}/sendMail. These tests assert send_mail()
picks the right endpoint without making any real Graph call.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import outlook_send as os_send  # noqa: E402


class _Resp:
    status_code = 202
    text = ""
    headers = {"request-id": "req-123"}


def _capture_post(monkeypatch):
    """Patch requests.post in outlook_send and return a list that captures
    the URL each send hits."""
    calls = []

    def _fake_post(url, headers=None, json=None, timeout=None):
        calls.append(url)
        return _Resp()

    monkeypatch.setattr(os_send.requests, "post", _fake_post)
    return calls


def test_app_only_sends_via_users_mailbox(monkeypatch):
    calls = _capture_post(monkeypatch)
    # app-only configured → context returns (token, /users/{mailbox}/sendMail)
    monkeypatch.setattr(
        os_send, "_app_only_send_context",
        lambda: ("APP_TOKEN", f"{os_send.GRAPH}/users/shared@ol-usa.com/sendMail"),
    )
    # get_token must NOT be called in app-only mode
    monkeypatch.setattr(os_send, "get_token",
                        lambda: (_ for _ in ()).throw(AssertionError("get_token called")))

    rid = os_send.send_mail(to=["a@b.com"], subject="s", html_body="<p>hi</p>")
    assert rid == "req-123"
    assert calls == [f"{os_send.GRAPH}/users/shared@ol-usa.com/sendMail"]


def test_device_code_path_uses_me_when_app_only_absent(monkeypatch):
    calls = _capture_post(monkeypatch)
    monkeypatch.setattr(os_send, "_app_only_send_context", lambda: None)
    monkeypatch.setattr(os_send, "get_token", lambda: "DEVICE_TOKEN")

    os_send.send_mail(to=["a@b.com"], subject="s", html_body="<p>hi</p>")
    assert calls == [f"{os_send.GRAPH}/me/sendMail"]


def test_explicit_token_uses_me(monkeypatch):
    calls = _capture_post(monkeypatch)
    # If a caller passes a token, keep the legacy /me behavior and don't
    # consult app-only.
    monkeypatch.setattr(
        os_send, "_app_only_send_context",
        lambda: (_ for _ in ()).throw(AssertionError("app-only consulted")),
    )
    os_send.send_mail(to=["a@b.com"], subject="s", html_body="<p>hi</p>", token="CALLER")
    assert calls == [f"{os_send.GRAPH}/me/sendMail"]


def test_app_only_context_returns_none_without_env(monkeypatch):
    for v in ("GRAPH_APP_TENANT_ID", "GRAPH_APP_CLIENT_ID", "GRAPH_APP_CLIENT_SECRET"):
        monkeypatch.delenv(v, raising=False)
    assert os_send._app_only_send_context() is None
