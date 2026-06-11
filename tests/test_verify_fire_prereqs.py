"""verify_fire_prereqs — the named-secret validator added after the
2026-06-10 verification fires died mid-run on wrong-field secret pastes."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import verify_fire_prereqs as vp  # noqa: E402


class _Resp:
    def __init__(self, status):
        self.status_code = status


def test_tenant_valid_when_discovery_200():
    ok, msg = vp.check_tenant("e8bc0287-e74f-47f1-a572-d83d32d60622",
                              http_get=lambda url: _Resp(200))
    assert ok


def test_tenant_invalid_names_the_fix():
    ok, msg = vp.check_tenant("not-a-tenant", http_get=lambda url: _Resp(400))
    assert not ok
    assert "Directory (tenant) ID" in msg


def test_storage_rejects_bare_key(monkeypatch):
    # Fake the SDK (not installed in every dev env): parsing a bare Key
    # raises ValueError, exactly like the real BlobServiceClient.
    import types

    fake_blob = types.ModuleType("azure.storage.blob")

    class _FakeSvc:
        @staticmethod
        def from_connection_string(conn):
            raise ValueError("Connection string is either blank or malformed.")

    fake_blob.BlobServiceClient = _FakeSvc
    monkeypatch.setitem(sys.modules, "azure", types.ModuleType("azure"))
    monkeypatch.setitem(sys.modules, "azure.storage", types.ModuleType("azure.storage"))
    monkeypatch.setitem(sys.modules, "azure.storage.blob", fake_blob)

    ok, msg = vp.check_storage("c29tZWJhc2U2NGtleQ==")
    assert not ok
    assert "not the bare Key" in msg


def test_client_id_must_be_guid():
    ok, msg = vp.check_client_id_shape("Hilmar Daily Tracker (app-only)")
    assert not ok
    assert "Application (client) ID" in msg
    ok, _ = vp.check_client_id_shape("12345678-abcd-ef01-2345-67890abcdef0")
    assert ok


def test_delegated_cache_check_runs_when_no_app_env(monkeypatch):
    # With no GRAPH_APP_* set, main() must consult the delegated cache,
    # not demand app-only secrets.
    for v in ("GRAPH_APP_TENANT_ID", "GRAPH_APP_CLIENT_ID", "GRAPH_APP_CLIENT_SECRET"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("SENTRY_DSN", "https://k@o.ingest.example/1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    monkeypatch.delenv("AZURE_STORAGE_CONNECTION_STRING", raising=False)
    called = {}
    monkeypatch.setattr(vp, "check_delegated_cache",
                        lambda: (called.setdefault("hit", True), "delegated ok"))
    rc = vp.main()
    assert called.get("hit") is True
    assert rc == 1  # storage conn string absent → still fails overall, by design
