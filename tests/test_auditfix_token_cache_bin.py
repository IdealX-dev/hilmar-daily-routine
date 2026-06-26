"""Token cache is non-indexed .bin, with safe legacy-.json fallback (finding [6]).

The MSAL cache holds a LIVE delegated OAuth refresh token. Naming it .json so
SharePoint indexes it made the credential search-discoverable. The fix: WRITE
the non-indexed .bin always, READ a legacy .json only when it's the sole cache
present (so a mid-migration box never breaks the fire). These lock that split.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import outlook_send as os_send  # noqa: E402
import state_store as ss  # noqa: E402


def test_canonical_write_path_is_bin():
    assert os_send.TOKEN_CACHE_PATH.suffix == ".bin", (
        "the cache must be WRITTEN to the non-indexed .bin, never the "
        "search-indexed .json"
    )


def test_read_prefers_bin_when_both_exist(tmp_path, monkeypatch):
    b = tmp_path / "token-cache.bin"
    j = tmp_path / "token-cache.json"
    b.write_text("BIN", encoding="utf-8")
    j.write_text("JSON", encoding="utf-8")
    monkeypatch.setattr(os_send, "_CACHE_BIN", b)
    monkeypatch.setattr(os_send, "_CACHE_JSON_LEGACY", j)
    assert os_send._token_cache_read_path() == b


def test_read_falls_back_to_legacy_json(tmp_path, monkeypatch):
    """A box still on .json must keep authenticating until it migrates."""
    b = tmp_path / "token-cache.bin"  # absent
    j = tmp_path / "token-cache.json"
    j.write_text("JSON", encoding="utf-8")
    monkeypatch.setattr(os_send, "_CACHE_BIN", b)
    monkeypatch.setattr(os_send, "_CACHE_JSON_LEGACY", j)
    assert os_send._token_cache_read_path() == j


def test_read_defaults_to_bin_when_neither_exists(tmp_path, monkeypatch):
    b = tmp_path / "token-cache.bin"
    j = tmp_path / "token-cache.json"
    monkeypatch.setattr(os_send, "_CACHE_BIN", b)
    monkeypatch.setattr(os_send, "_CACHE_JSON_LEGACY", j)
    assert os_send._token_cache_read_path() == b


def test_state_store_syncs_bin_during_migration():
    # .bin must be synced so the non-indexed cache persists cross-host; .json
    # stays during the transition so a mid-migration host isn't stranded.
    assert "secrets/token-cache.bin" in ss.STATE_FILES
    assert "secrets/token-cache.json" in ss.STATE_FILES
