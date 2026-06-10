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
