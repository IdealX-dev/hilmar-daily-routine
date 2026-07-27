"""Tests for scripts/state_store.py — Azure-blob state sync for the
off-Cloud-PC (GH Actions) daily fire. Uses an in-memory fake container
client so no Azure account or azure-sdk import is needed.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

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
    # Valid tracking JSON — push() now refuses to publish a file that
    # does not parse, so a placeholder string can no longer stand in.
    (src / "tracking-data-v2.json").write_text('{"requests": ["DATA"]}', encoding="utf-8")
    cc = _FakeContainer()
    ss.push(src, container=cc)

    dst = tmp_path / "dst"
    dst.mkdir()
    ss.pull(dst, container=cc)
    assert (dst / "tracking-data-v2.json").read_text(encoding="utf-8") == '{"requests": ["DATA"]}'


def test_push_overwrites_existing_blob(tmp_path):
    (tmp_path / "tracking-data-v2.json").write_text('{"requests": [2]}', encoding="utf-8")
    cc = _FakeContainer({"tracking-data-v2.json": b'{"requests": [1]}'})
    ss.push(tmp_path, container=cc)
    assert cc.store["tracking-data-v2.json"] == b'{"requests": [2]}'


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


def test_state_paths_include_todays_et_flags():
    paths = ss.state_paths("2026-06-10")
    assert "reports/sent-2026-06-10.flag" in paths
    assert "reports/improvements-sent-2026-06-10.flag" in paths
    for core in ss.STATE_FILES:
        assert core in paths


def test_flags_round_trip_keeps_idempotency_across_runners(tmp_path, monkeypatch):
    # The double-send guard: runner A fires + pushes its flag; runner B
    # (fresh checkout, same day) pulls and must see the flag locally.
    monkeypatch.setattr(ss, "_today_et", lambda: "2026-06-10")
    runner_a = tmp_path / "a"
    (runner_a / "reports").mkdir(parents=True)
    (runner_a / "reports" / "sent-2026-06-10.flag").write_text(
        "Sent 2026-06-10 10:04 ET req=r1 to=10 recipient(s)\n", encoding="utf-8")
    cc = _FakeContainer()
    pushed = ss.push(runner_a, container=cc)
    assert "reports/sent-2026-06-10.flag" in pushed

    runner_b = tmp_path / "b"
    runner_b.mkdir()
    pulled = ss.pull(runner_b, container=cc)
    assert "reports/sent-2026-06-10.flag" in pulled
    assert "10:04 ET" in (runner_b / "reports" / "sent-2026-06-10.flag").read_text(encoding="utf-8")


def test_stale_flags_do_not_sync(tmp_path, monkeypatch):
    # Yesterday's flag in the store must NOT appear on today's runner — a
    # stale flag would wrongly block today's send.
    monkeypatch.setattr(ss, "_today_et", lambda: "2026-06-10")
    cc = _FakeContainer({"reports/sent-2026-06-09.flag": b"old"})
    pulled = ss.pull(tmp_path, container=cc)
    assert pulled == []
    assert not (tmp_path / "reports" / "sent-2026-06-09.flag").exists()


def test_malformed_connection_string_gets_actionable_error(tmp_path, monkeypatch):
    # The 2026-06-10 verification fire failed with the SDK's terse
    # "Connection string is either blank or malformed" — because the secret
    # held the bare access Key. The wrapper must say what to paste instead.
    import sys
    import types

    fake_blob = types.ModuleType("azure.storage.blob")

    class _FakeSvc:
        @staticmethod
        def from_connection_string(conn):
            raise ValueError("Connection string is either blank or malformed.")

    fake_blob.BlobServiceClient = _FakeSvc
    fake_azure = types.ModuleType("azure")
    fake_storage = types.ModuleType("azure.storage")
    monkeypatch.setitem(sys.modules, "azure", fake_azure)
    monkeypatch.setitem(sys.modules, "azure.storage", fake_storage)
    monkeypatch.setitem(sys.modules, "azure.storage.blob", fake_blob)
    monkeypatch.setenv(ss.ENV_CONN, "NOT-A-CONNECTION-STRING")

    import pytest

    with pytest.raises(ss.StateStoreError) as ei:
        ss._container_client()
    msg = str(ei.value)
    assert "DefaultEndpointsProtocol=https;AccountName=" in msg
    assert "not the bare Key" in msg


def test_token_cache_is_synced_state():
    # The no-IT auth path (OL declined the app-only registration) lives or
    # dies on this file riding the store — pin it.
    assert "secrets/token-cache.json" in ss.STATE_FILES


def test_backup_writes_dated_gzip_and_prunes(tmp_path, monkeypatch):
    import gzip

    monkeypatch.setattr(ss, "_today_et", lambda: "2026-06-12")
    (tmp_path / "tracking-data-v2.json").write_text('{"requests": [1]}', encoding="utf-8")

    class _ListingFake(_FakeContainer):
        def list_blobs(self, name_starts_with=""):
            return [n for n in sorted(self.store) if n.startswith(name_starts_with)]

        def delete_blob(self, name):
            del self.store[name]

    cc = _ListingFake({
        f"{ss.BACKUP_PREFIX}2026-05-01.json.gz": b"ancient",   # > 14d — pruned
        f"{ss.BACKUP_PREFIX}2026-06-05.json.gz": b"recent",    # kept
    })
    name = ss.backup(tmp_path, container=cc)
    # The name leads with the ET DATE (the prune and QC-032's age check parse
    # it) and carries a UTC time after it, so a second run the same day cannot
    # replace the first — audit finding #21. Asserted as a prefix + shape
    # rather than a literal, because the time half is a real clock read.
    assert name.startswith(f"{ss.BACKUP_PREFIX}2026-06-12T")
    assert name.endswith(".json.gz")
    assert len(name) == len(f"{ss.BACKUP_PREFIX}2026-06-12T125801.json.gz")
    assert gzip.decompress(cc.store[name]) == b'{"requests": [1]}'
    assert f"{ss.BACKUP_PREFIX}2026-05-01.json.gz" not in cc.store
    assert f"{ss.BACKUP_PREFIX}2026-06-05.json.gz" in cc.store
    # QC-032's blob-side freshness read
    assert ss.latest_backup_age_days(container=cc) is not None


def test_backup_noop_without_data_file(tmp_path):
    assert ss.backup(tmp_path, container=_FakeContainer()) is None


def test_quote_history_db_rides_the_state_sync():
    # The historian DB must sync like the rest of the mutable state —
    # without this entry the longitudinal store resets on every ephemeral
    # runner and the history silently never accumulates.
    import state_store
    assert "data/quote-history.db" in state_store.STATE_FILES


# ── push() refuses to publish a corrupt tracking file ───────────────────────
#
# The daily workflow pushes under `if: always()`, so it runs even when the
# pipeline died partway through. Before this guard a truncated
# tracking-data-v2.json was uploaded straight over the last good blob — and
# the backup could not help, because it snapshots the same corrupt file.

@pytest.mark.parametrize("body,why", [
    ('{"requests": [1], ', "truncated mid-JSON — the crash shape"),
    ("", "empty file"),
    ("not json at all", "not JSON"),
    ('{"summary": {}}', "parses, but carries no requests list"),
    ('[1, 2, 3]', "top level is a list, not the tracking object"),
])
def test_push_refuses_a_corrupt_tracking_file(tmp_path, body, why):
    (tmp_path / "tracking-data-v2.json").write_text(body, encoding="utf-8")
    cc = _FakeContainer({"tracking-data-v2.json": b'{"requests": [1]}'})
    with pytest.raises(RuntimeError, match="REFUSING to push"):
        ss.push(tmp_path, container=cc)
    # The good blob is untouched — that is the whole point.
    assert cc.store["tracking-data-v2.json"] == b'{"requests": [1]}', why


def test_push_still_publishes_a_valid_tracking_file(tmp_path):
    """The guard must not become a blanket refusal."""
    (tmp_path / "tracking-data-v2.json").write_text('{"requests": []}', encoding="utf-8")
    cc = _FakeContainer()
    assert "tracking-data-v2.json" in ss.push(tmp_path, container=cc)


def test_other_state_files_are_not_json_gated(tmp_path):
    """Only the tracking file is JSON. The stage cache is plain text and must
    keep syncing."""
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "stage_emails.txt").write_text("not json", encoding="utf-8")
    cc = _FakeContainer()
    assert "scripts/stage_emails.txt" in ss.push(tmp_path, container=cc)
