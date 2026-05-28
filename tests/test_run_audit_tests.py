"""Tests for scripts/run_audit_tests.py — the daily test+coverage routine
added 2026-05-28. Standing rule: a new pattern ships with its own tests."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import run_audit_tests as RAT  # noqa: E402


def test_read_gate_from_pyproject():
    # pyproject.toml sets --cov-fail-under=85
    assert RAT._read_gate_from_pyproject() == 85.0


def test_read_gate_fallback(tmp_path, monkeypatch):
    # Point ROOT at a dir with no pyproject → fallback 85.
    monkeypatch.setattr(RAT, "ROOT", tmp_path)
    assert RAT._read_gate_from_pyproject() == 85.0


def test_parse_counts():
    line = "587 passed, 2 failed, 1 error, 3 skipped in 3.60s"
    counts = RAT._parse_counts(line)
    assert counts == {"passed": 587, "failed": 2, "error": 1, "skipped": 3}


def test_parse_counts_partial():
    counts = RAT._parse_counts("587 passed in 3.60s")
    assert counts["passed"] == 587
    assert counts["failed"] == 0
    assert counts["error"] == 0


def test_pytest_available_returns_tuple():
    ok, reason = RAT._pytest_available()
    # In the test environment pytest is obviously importable.
    assert ok is True
    assert reason == ""


def test_write_creates_artifact(tmp_path, monkeypatch):
    monkeypatch.setattr(RAT, "REPORTS", tmp_path)
    monkeypatch.setattr(RAT, "ARTIFACT", tmp_path / "test-result.json")
    RAT._write({"status": "PASS", "total_coverage": 90.0})
    import json
    written = json.loads((tmp_path / "test-result.json").read_text())
    assert written["status"] == "PASS"
    assert written["total_coverage"] == 90.0


def test_skipped_path_writes_artifact_and_exits_zero(tmp_path, monkeypatch):
    # Force the "pytest unavailable" branch and confirm it writes a SKIPPED
    # artifact and returns 0 (observer must never block the pipeline).
    monkeypatch.setattr(RAT, "REPORTS", tmp_path)
    monkeypatch.setattr(RAT, "ARTIFACT", tmp_path / "test-result.json")
    monkeypatch.setattr(RAT, "_pytest_available",
                        lambda: (False, "ModuleNotFoundError: No module named 'pytest'"))
    monkeypatch.setattr(sys, "argv", ["run_audit_tests.py", "--quiet"])
    rc = RAT.main()
    assert rc == 0
    import json
    written = json.loads((tmp_path / "test-result.json").read_text())
    assert written["status"] == "SKIPPED"
    assert "pytest" in written["reason"]
