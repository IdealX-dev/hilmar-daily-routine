"""GraphClient app-only auth integration — the branch that picks app-only
(client credentials) over device-code when GRAPH_APP_* env vars are set.

This is the wiring that lets the daily fire run unattended off the Cloud PC
(GH Actions) using the registered Entra app instead of a human's device-code
token. The app_auth helpers are unit-tested in test_app_auth.py; this file
tests that GraphClient.authenticate() actually routes to them.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from hilmar import graph_client as gc  # noqa: E402
from hilmar.app_auth import AppOnlyCredentials  # noqa: E402


def _client(tmp_path):
    # token_cache_path pointed at a non-existent tmp file so no real cache
    # is read; app-only path shouldn't touch it anyway.
    return gc.GraphClient(token_cache_path=tmp_path / "no-cache.json")


def test_authenticate_uses_app_only_when_env_configured(tmp_path, monkeypatch):
    creds = AppOnlyCredentials(tenant_id="t", client_id="c", client_secret="s")
    monkeypatch.setattr(gc, "app_only_credentials_from_env", lambda: creds)
    monkeypatch.setattr(gc, "acquire_app_only_token", lambda c: "APP_ONLY_TOKEN")

    # If app-only is taken, the device-code MSAL app is never built.
    def _boom():
        raise AssertionError("device-code path must not run when app-only is configured")
    client = _client(tmp_path)
    monkeypatch.setattr(client, "_build_msal_app", _boom)

    token = client.authenticate(interactive_ok=True)
    assert token == "APP_ONLY_TOKEN"
    assert client._access_token == "APP_ONLY_TOKEN"


def test_app_only_failure_raises_graph_auth_error(tmp_path, monkeypatch):
    creds = AppOnlyCredentials(tenant_id="t", client_id="c", client_secret="s")
    monkeypatch.setattr(gc, "app_only_credentials_from_env", lambda: creds)

    def _fail(c):
        raise RuntimeError("Client credentials auth failed: invalid_client: bad secret")
    monkeypatch.setattr(gc, "acquire_app_only_token", _fail)

    client = _client(tmp_path)
    with pytest.raises(gc.GraphAuthError) as ei:
        client.authenticate(interactive_ok=False)
    assert "App-only auth failed" in str(ei.value)


def test_falls_through_to_device_code_when_not_configured(tmp_path, monkeypatch):
    # No app-only creds → must NOT use the app-only token, must reach the
    # delegated path (which, with interactive_ok=False and no cache, raises
    # the documented GraphAuthError).
    monkeypatch.setattr(gc, "app_only_credentials_from_env", lambda: None)

    def _must_not_call(c):
        raise AssertionError("app-only token must not be acquired when env is unset")
    monkeypatch.setattr(gc, "acquire_app_only_token", _must_not_call)

    client = _client(tmp_path)
    with pytest.raises(gc.GraphAuthError) as ei:
        client.authenticate(interactive_ok=False)
    # Reached the delegated branch's silent-auth-failed message, not app-only.
    assert "Silent auth failed" in str(ei.value)
