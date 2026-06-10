"""Tests for scripts/sync_to_quote_tracker.py — the Hilmar→ol-quote-tracker
Turso sync. Network paths are mocked; we exercise entity-build logic,
graceful degradation, and the audit-log writer that QC-037 reads."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import sync_to_quote_tracker as ST  # noqa: E402

# ── _carrier_notes ───────────────────────────────────────────────────────────

def test_carrier_notes_full():
    summary = {"CMA CGM": {"quotes": 10, "wins": 3, "win_rate_pct": 30,
                            "rate_median": 3500, "transit_median_days": 12}}
    notes = ST._carrier_notes(summary, "CMA CGM")
    assert "10x" in notes
    assert "3 wins (30% rate)" in notes
    assert "median $3,500" in notes
    assert "~12d transit" in notes


def test_carrier_notes_missing_carrier_returns_none():
    assert ST._carrier_notes({}, "Nonexistent") is None


def test_carrier_notes_with_no_stats_returns_none():
    # Carrier present but with no measurable stats → returns None (no bits)
    assert ST._carrier_notes({"Foo": {}}, "Foo") is None


# ── _load_password ───────────────────────────────────────────────────────────

def test_load_password_from_env_when_no_secrets_file(monkeypatch, tmp_path):
    monkeypatch.setattr(ST, "ROOT", tmp_path)  # no secrets/ subdir
    monkeypatch.setenv("QT_APP_PASSWORD", "envpass")
    assert ST._load_password() == "envpass"


def test_load_password_secrets_file_wins_over_env(monkeypatch, tmp_path):
    monkeypatch.setattr(ST, "ROOT", tmp_path)
    (tmp_path / "secrets").mkdir()
    (tmp_path / "secrets" / "quote-tracker-pwd.txt").write_text("filepass\n")
    monkeypatch.setenv("QT_APP_PASSWORD", "envpass")
    assert ST._load_password() == "filepass"


def test_load_password_returns_none_when_unset(monkeypatch, tmp_path):
    monkeypatch.setattr(ST, "ROOT", tmp_path)
    monkeypatch.delenv("QT_APP_PASSWORD", raising=False)
    assert ST._load_password() is None


# ── sync_entities — graceful degradation ─────────────────────────────────────

def test_sync_dry_run_does_not_call_network():
    """--dry must not attempt login/sync — only return the preview."""
    with patch.object(ST, "requests") as mock_req:
        result = ST.sync_entities([{"name": "x", "role": "client"}], dry=True)
    assert mock_req.Session.call_count == 0
    assert result["ok"] is False
    assert result.get("dry") is True
    assert result["preview"] == [{"name": "x", "role": "client"}]
    assert result["entity_count"] == 1


def test_sync_without_password_errors_gracefully():
    """No password → result.error explains, no network call attempted."""
    with patch.object(ST, "requests") as mock_req:
        result = ST.sync_entities([{"name": "x"}], password=None, dry=False)
    assert mock_req.Session.call_count == 0
    assert result["ok"] is False
    assert "APP_PASSWORD" in result["error"]


def test_sync_network_exception_captured_in_error():
    """A raised exception during the call is captured, not re-raised — the
    pipeline must never crash because Turso is down."""
    fake_session = type("S", (), {
        "post": lambda self, *a, **k: (_ for _ in ()).throw(ConnectionError("net down")),
    })()
    with patch.object(ST, "requests") as mock_req:
        mock_req.Session.return_value = fake_session
        result = ST.sync_entities([{"name": "x"}], password="pw", dry=False)
    assert result["ok"] is False
    assert "ConnectionError" in result["error"]
    assert "net down" in result["error"]


# ── write_audit ──────────────────────────────────────────────────────────────

def test_write_audit_appends_parseable_line(tmp_path, monkeypatch):
    """QC-037 parses these lines — format must stay stable."""
    monkeypatch.setattr(ST, "REPORTS", tmp_path)
    ST.write_audit({
        "entity_count": 5,
        "ok": True,
        "response": {"upserted": 5},
        "error": None,
    })
    log = (tmp_path / "quote-tracker-sync.log").read_text()
    assert "entities=5" in log
    assert "ok=True" in log
    assert "upserted=5" in log
    assert "err=-" in log


def test_write_audit_records_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(ST, "REPORTS", tmp_path)
    ST.write_audit({
        "entity_count": 5, "ok": False, "response": None,
        "error": "sync 500: Internal Server Error",
    })
    log = (tmp_path / "quote-tracker-sync.log").read_text()
    assert "ok=False" in log
    assert "err=sync 500" in log


def test_write_audit_appends_not_overwrites(tmp_path, monkeypatch):
    """QC-037 scans backwards through the file — old lines must remain."""
    monkeypatch.setattr(ST, "REPORTS", tmp_path)
    ST.write_audit({"entity_count": 1, "ok": True, "response": {"upserted": 1}, "error": None})
    ST.write_audit({"entity_count": 2, "ok": False, "response": None, "error": "boom"})
    lines = [ln for ln in (tmp_path / "quote-tracker-sync.log").read_text().splitlines() if ln]
    assert len(lines) == 2
    assert "ok=True" in lines[0]
    assert "ok=False" in lines[1]


# ── build_entities (smoke) ───────────────────────────────────────────────────

def test_build_entities_with_minimal_share_intel(monkeypatch, tmp_path):
    """Hilmar + Lonny entities are always present, even with no quote history."""
    cdir = tmp_path / "hilmar"
    cdir.mkdir()
    # No quotes.jsonl, no carrier_summary.json → build_entities should still
    # return the two baseline client entities.
    monkeypatch.setattr(ST.SI, "_client_dir", lambda _name: cdir)
    monkeypatch.setattr(ST.SI, "_load_jsonl", lambda _p: [])
    entities = ST.build_entities()
    names = {e["name"] for e in entities}
    assert "Hilmar Ingredients" in names
    assert "Lonny Upfold" in names
    roles = {e["role"] for e in entities}
    assert roles == {"client"}  # no carriers, no OL operators


# ── main() — the best-effort contract (Layer 1, 2026-06-01) ──────────────
#
# main() MUST NEVER return non-zero. Sync is downstream-bonus; if it fails
# for any reason — missing password, missing SHARED dir, network down, bad
# data, programmer error — the daily pipeline must continue so the client
# email goes out. The audit log records the failure; QC-037 surfaces it.
#
# These tests lock that contract for every failure mode actually observed
# in production (the 2026-06-01 TTSWW run was a missing SHARED dir →
# uncaught FileNotFoundError → pipeline rc=1 → wrapper abort → no email).

from unittest import mock as _mock  # noqa: E402


def _patch_argv(*args):
    return _mock.patch.object(sys, "argv", ["sync_to_quote_tracker.py", *args])


def test_main_returns_0_when_password_missing(monkeypatch, capsys):
    """Existing baseline — preserved by Layer 1: no password → exit 0."""
    monkeypatch.setattr(ST, "_load_password", lambda: None)
    with _patch_argv():
        rc = ST.main()
    assert rc == 0
    out = capsys.readouterr().out
    assert "No APP_PASSWORD" in out or "no APP_PASSWORD" in out.lower()


def test_main_returns_0_when_build_entities_raises_filenotfound(monkeypatch, capsys):
    """The 2026-06-01 TTSWW bug. SHARED/client_intelligence/hilmar/ wasn't
    synced; build_entities raised FileNotFoundError; the script exited 1;
    run_pipeline aborted; the daily email + audit email were never sent.
    Now caught at main() with a clear log line."""
    monkeypatch.setattr(ST, "_load_password", lambda: "fake_pwd")
    monkeypatch.setattr(ST, "build_entities",
                        _mock.Mock(side_effect=FileNotFoundError("hilmar/quotes.jsonl")))
    monkeypatch.setattr(ST, "write_audit", _mock.Mock())
    with _patch_argv():
        rc = ST.main()
    assert rc == 0
    out = capsys.readouterr().out
    assert "build_entities failed" in out
    assert "FileNotFoundError" in out


def test_main_returns_0_for_any_build_entities_exception(monkeypatch):
    """build_entities can fail many ways — JSONDecodeError, PermissionError,
    KeyError on malformed data, etc. ALL must short-circuit to exit 0."""
    monkeypatch.setattr(ST, "_load_password", lambda: "fake_pwd")
    monkeypatch.setattr(ST, "write_audit", _mock.Mock())
    for exc in (PermissionError("denied"), json.JSONDecodeError("bad", "doc", 0),
                KeyError("status"), ValueError("bad shape"), RuntimeError("?")):
        monkeypatch.setattr(ST, "build_entities", _mock.Mock(side_effect=exc))
        with _patch_argv():
            rc = ST.main()
        assert rc == 0, f"main() returned {rc} for {type(exc).__name__}"


def test_build_entities_failure_still_writes_audit_log(monkeypatch):
    """When build_entities crashes, audit log MUST still record it so
    QC-037 surfaces the recurring failure to the operator."""
    monkeypatch.setattr(ST, "_load_password", lambda: "fake_pwd")
    monkeypatch.setattr(ST, "build_entities",
                        _mock.Mock(side_effect=PermissionError("denied")))
    write_audit_mock = _mock.Mock()
    monkeypatch.setattr(ST, "write_audit", write_audit_mock)
    with _patch_argv():
        ST.main()
    write_audit_mock.assert_called_once()
    result = write_audit_mock.call_args[0][0]
    assert result["ok"] is False
    assert "PermissionError" in result["error"]


def test_audit_log_write_failure_does_not_propagate(monkeypatch):
    """If write_audit itself fails (disk full), main() must STILL return
    0. The audit-log write is best-effort within best-effort."""
    monkeypatch.setattr(ST, "_load_password", lambda: "fake_pwd")
    monkeypatch.setattr(ST, "build_entities",
                        _mock.Mock(side_effect=ValueError("bad data")))
    monkeypatch.setattr(ST, "write_audit", _mock.Mock(side_effect=OSError("disk full")))
    with _patch_argv():
        rc = ST.main()
    assert rc == 0


def test_main_returns_0_when_sync_entities_itself_raises(monkeypatch):
    """sync_entities is supposed to catch its own exceptions, but if a
    programmer-error path escapes (e.g., AttributeError before the
    try/except scope), the outer try in main() saves the pipeline."""
    monkeypatch.setattr(ST, "_load_password", lambda: "fake_pwd")
    monkeypatch.setattr(ST, "build_entities",
                        lambda: [{"role": "client", "name": "Hilmar"}])
    monkeypatch.setattr(ST, "write_audit", _mock.Mock())
    monkeypatch.setattr(ST, "sync_entities",
                        _mock.Mock(side_effect=AttributeError("regression")))
    with _patch_argv():
        rc = ST.main()
    assert rc == 0


def test_main_returns_0_on_successful_sync(monkeypatch, capsys):
    """Happy path: nothing has changed for successful runs."""
    monkeypatch.setattr(ST, "_load_password", lambda: "fake_pwd")
    monkeypatch.setattr(ST, "build_entities",
                        lambda: [{"role": "client", "name": "Hilmar"},
                                  {"role": "contact", "name": "Lonny"}])
    monkeypatch.setattr(ST, "write_audit", _mock.Mock())
    monkeypatch.setattr(ST, "sync_entities",
                        lambda *a, **kw: {"base_url": "http://x", "entity_count": 2,
                                          "ok": True, "error": None, "response": {"synced": 2}})
    with _patch_argv():
        rc = ST.main()
    assert rc == 0
    out = capsys.readouterr().out
    assert "built 2 entities" in out
    assert '"ok": true' in out
