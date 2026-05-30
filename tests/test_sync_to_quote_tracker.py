"""Tests for scripts/sync_to_quote_tracker.py — the Hilmar→ol-quote-tracker
Turso sync. Network paths are mocked; we exercise entity-build logic,
graceful degradation, and the audit-log writer that QC-037 reads."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

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
