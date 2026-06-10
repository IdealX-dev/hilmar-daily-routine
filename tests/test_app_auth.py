"""Tests for hilmar.app_auth — the app-only Graph auth scaffolding.

The integration with GraphClient is gated on three env vars; if any one
is missing we fall back to device-code (existing behavior). These tests
exercise the env-var contract, the credential dataclass, and the MSAL
client-credentials path with a mocked msal module so no real network
call happens."""
from __future__ import annotations

import dataclasses
import sys
from unittest.mock import MagicMock, patch

import pytest

from hilmar import app_auth

# ── env-var contract ────────────────────────────────────────────────────────

def test_credentials_from_env_returns_none_when_all_missing(monkeypatch):
    for v in (app_auth.ENV_TENANT_ID, app_auth.ENV_CLIENT_ID, app_auth.ENV_CLIENT_SECRET):
        monkeypatch.delenv(v, raising=False)
    assert app_auth.app_only_credentials_from_env() is None
    assert app_auth.is_app_only_configured() is False


def test_credentials_from_env_returns_none_when_any_missing(monkeypatch):
    """All three must be present — partial config falls back to device-code."""
    monkeypatch.setenv(app_auth.ENV_TENANT_ID, "tenant-id")
    monkeypatch.setenv(app_auth.ENV_CLIENT_ID, "client-id")
    monkeypatch.delenv(app_auth.ENV_CLIENT_SECRET, raising=False)
    assert app_auth.app_only_credentials_from_env() is None
    assert app_auth.is_app_only_configured() is False


def test_credentials_from_env_returns_dataclass_when_all_set(monkeypatch):
    monkeypatch.setenv(app_auth.ENV_TENANT_ID, "tenant-123")
    monkeypatch.setenv(app_auth.ENV_CLIENT_ID, "client-456")
    monkeypatch.setenv(app_auth.ENV_CLIENT_SECRET, "secret-789")
    creds = app_auth.app_only_credentials_from_env()
    assert creds is not None
    assert creds.tenant_id == "tenant-123"
    assert creds.client_id == "client-456"
    assert creds.client_secret == "secret-789"
    assert app_auth.is_app_only_configured() is True


def test_credentials_immutable_frozen_dataclass():
    """AppOnlyCredentials is frozen — accidentally mutating an instance
    must raise rather than silently produce wrong-auth-token bugs."""
    creds = app_auth.AppOnlyCredentials("t", "c", "s")
    with pytest.raises(dataclasses.FrozenInstanceError):
        creds.client_secret = "different"  # type: ignore[misc]


# ── acquire_app_only_token — happy path & error paths ──────────────────────

def _fake_msal_module(*, response: dict):
    """Build a mock msal module that returns ``response`` from
    acquire_token_for_client."""
    fake_msal = MagicMock()
    fake_app = MagicMock()
    fake_app.acquire_token_for_client.return_value = response
    fake_msal.ConfidentialClientApplication.return_value = fake_app
    return fake_msal, fake_app


def test_acquire_token_happy_path():
    creds = app_auth.AppOnlyCredentials("t", "c", "s")
    fake_msal, fake_app = _fake_msal_module(response={
        "access_token": "ya29.fake-token-abc123",
        "expires_in": 3600,
        "token_type": "Bearer",
    })
    with patch.dict(sys.modules, {"msal": fake_msal}):
        token = app_auth.acquire_app_only_token(creds)
    assert token == "ya29.fake-token-abc123"
    # Confirm we built the confidential client with the right config
    fake_msal.ConfidentialClientApplication.assert_called_once()
    kwargs = fake_msal.ConfidentialClientApplication.call_args.kwargs
    assert kwargs["client_id"] == "c"
    assert kwargs["authority"] == "https://login.microsoftonline.com/t"
    assert kwargs["client_credential"] == "s"
    # Default scope is ".default" for client credentials
    fake_app.acquire_token_for_client.assert_called_once_with(
        scopes=app_auth.APP_ONLY_SCOPE,
    )


def test_acquire_token_custom_scopes():
    creds = app_auth.AppOnlyCredentials("t", "c", "s")
    fake_msal, fake_app = _fake_msal_module(response={"access_token": "x"})
    with patch.dict(sys.modules, {"msal": fake_msal}):
        app_auth.acquire_app_only_token(
            creds, scopes=["https://graph.microsoft.com/Mail.Read"],
        )
    fake_app.acquire_token_for_client.assert_called_once_with(
        scopes=["https://graph.microsoft.com/Mail.Read"],
    )


def test_acquire_token_raises_on_msal_error_response():
    """MSAL signals failure by returning a dict with 'error' instead of
    'access_token'. Must surface as RuntimeError with detail."""
    creds = app_auth.AppOnlyCredentials("t", "c", "bad-secret")
    fake_msal, _ = _fake_msal_module(response={
        "error": "invalid_client",
        "error_description": "Invalid client secret provided.",
    })
    with patch.dict(sys.modules, {"msal": fake_msal}), \
         pytest.raises(RuntimeError, match="invalid_client"):
        app_auth.acquire_app_only_token(creds)


def test_acquire_token_raises_on_non_dict_response():
    """Defensive: if MSAL returns something unexpected (None, string,
    object), don't silently coerce — raise."""
    creds = app_auth.AppOnlyCredentials("t", "c", "s")
    fake_msal, _ = _fake_msal_module(response=None)
    with patch.dict(sys.modules, {"msal": fake_msal}), \
         pytest.raises(RuntimeError, match="non-dict response"):
        app_auth.acquire_app_only_token(creds)
