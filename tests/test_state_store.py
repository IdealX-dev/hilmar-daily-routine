"""Tests for scripts/state_store.py — Azure-blob state sync for the
off-Cloud-PC (GH Actions) daily fire. Uses an in-memory fake container
client so no Azure account or azure-sdk import is needed.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import state_store as ss  # noqa: E402


class _FakeBlob:
    def __init__(self, store: dict, name: str):
        self._store = store
        self._name = name

    def exists(self) -> bool:
        return self._name in self._store

    def download_blob(self):
        data = self._store[self._name]

        class _D:
            def readall(_self):
                return data
        return _D()

    def upload_blob(self, data: bytes, overwrite: bool = False):
        if self._name in self._store and not overwrite:
            raise AssertionError("blob exists and overwrite=False")
        self._store[self._name] = bytes(data)


class _FakeContainer:
    """Minimal stand-in for an Azure ContainerClient backed by a dict."""
    def __init__(self, seed: dict | None = None):
        self.store: dict[str, bytes] = dict(seed or {})

    def get_blob_client(self, name: str) -> _FakeBlob:
        return _FakeBlob(self.store, name)


def test_push_uploads_present_state_files(tmp_path):
    (tmp_path / "tracking-data-v2.json").write_text('{"requests": []}', encoding="utf-8")
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "stage_emails.txt").write_text("stage", encoding="utf-8")
    # stage_emails_bodies.txt intentionally absent — push skips missing.
    cc = _FakeContainer()
    pushed = ss.push(tmp_path, container=cc)
    assert "tracking-data-v2.json" in pushed
    assert "scripts/stage_emails.txt" in pushed
    assert "scripts/stage_emails_bodies.txt" not in pushed
    assert cc.store["tracking-data-v2.json"] == b'{"requests": []}'


def test_pull_downloads_into_local_tree(tmp_path):
    cc = _FakeContainer({
        "tracking-data-v2.json": b'{"requests": [1]}',
        "scripts/stage_emails.txt": b"cached-stage",
    })
    pulled = ss.pull(tmp_path, container=cc)
    assert set(pulled) == {"tracking-data-v2.json", "scripts/stage_emails.txt"}
    assert (tmp_path / "tracking-data-v2.json").read_bytes() == b'{"requests": [1]}'
    # nested path created
    assert (tmp_path / "scripts" / "stage_emails.txt").read_bytes() == b"cached-stage"


def test_pull_skips_missing_blobs_first_run(tmp_path):
    cc = _FakeContainer()  # empty store — first-ever run
    pulled = ss.pull(tmp_path, container=cc)
    assert pulled == []
    assert not (tmp_path / "tracking-data-v2.json").exists()


def test_round_trip_push_then_pull(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "tracking-data-v2.json").write_text("DATA", encoding="utf-8")
    cc = _FakeContainer()
    ss.push(src, container=cc)

    dst = tmp_path / "dst"
    dst.mkdir()
    ss.pull(dst, container=cc)
    assert (dst / "tracking-data-v2.json").read_text(encoding="utf-8") == "DATA"


def test_push_overwrites_existing_blob(tmp_path):
    (tmp_path / "tracking-data-v2.json").write_text("v2", encoding="utf-8")
    cc = _FakeContainer({"tracking-data-v2.json": b"v1"})
    ss.push(tmp_path, container=cc)
    assert cc.store["tracking-data-v2.json"] == b"v2"


def test_main_is_noop_when_unconfigured(monkeypatch, capsys):
    monkeypatch.delenv(ss.ENV_CONN, raising=False)
    assert ss.main(["pull"]) == 0
    assert ss.main(["push"]) == 0
    out = capsys.readouterr().out
    assert "skipped" in out


def test_is_configured_reflects_env(monkeypatch):
    monkeypatch.delenv(ss.ENV_CONN, raising=False)
    assert ss.is_configured() is False
    monkeypatch.setenv(ss.ENV_CONN, "DefaultEndpointsProtocol=https;...")
    assert ss.is_configured() is True
