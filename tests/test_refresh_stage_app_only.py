"""refresh_stage app-only auth routing — the gap the 2026-06-10 verification
fire caught: PR #33 wired app-only into GraphClient and outlook_send, but
refresh_stage did its own silent-refresh token dance and /me endpoints, so
the GH Actions fire died with "no cached MSAL account".

Pinned here:
  - GRAPH_APP_* configured → get_token() returns the app-only token and all
    reads target /users/{READ_MAILBOX} (app-only has no /me)
  - not configured → unchanged delegated path: /me + silent refresh
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import refresh_stage as rs  # noqa: E402


def _reset_base(monkeypatch):
    monkeypatch.setattr(rs, "_mailbox_base", f"{rs.GRAPH}/me")


def test_app_only_routes_to_users_mailbox(monkeypatch):
    _reset_base(monkeypatch)
    monkeypatch.setattr(rs, "_app_only_token", lambda: "APP_TOKEN")
    monkeypatch.setattr(rs, "get_token_silent",
                        lambda: (_ for _ in ()).throw(AssertionError("silent path used")))
    assert rs.get_token() == "APP_TOKEN"
    assert rs._mailbox_base == f"{rs.GRAPH}/users/{rs.READ_MAILBOX}"


def test_delegated_path_keeps_me_base(monkeypatch):
    _reset_base(monkeypatch)
    monkeypatch.setattr(rs, "_app_only_token", lambda: None)
    monkeypatch.setattr(rs, "get_token_silent", lambda: "DEVICE_TOKEN")
    assert rs.get_token() == "DEVICE_TOKEN"
    assert rs._mailbox_base == f"{rs.GRAPH}/me"


def test_app_only_token_none_without_env(monkeypatch):
    for v in ("GRAPH_APP_TENANT_ID", "GRAPH_APP_CLIENT_ID", "GRAPH_APP_CLIENT_SECRET"):
        monkeypatch.delenv(v, raising=False)
    assert rs._app_only_token() is None


def test_no_hardcoded_me_endpoints_remain():
    # Every Graph read must go through _mailbox_base — a literal /me/ URL
    # would 404 under app-only auth and silently re-open this gap.
    src = (ROOT / "scripts" / "refresh_stage.py").read_text(encoding="utf-8")
    me_literals = [ln for ln in src.splitlines()
                   if "{GRAPH}/me" in ln and "_mailbox_base = " not in ln]
    assert me_literals == [], f"hardcoded /me endpoints: {me_literals}"
