"""Tests for scripts/run_audit_tests.py — the daily test+coverage routine
added 2026-05-28. Standing rule: a new pattern ships with its own tests."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import run_audit_tests as RAT  # noqa: E402


def test_read_gate_from_pyproject():
    # The gate is a ratchet — read whatever the current pyproject value is
    # rather than pinning a specific number (else this test fights the
    # ratchet every time we bump it).
    import re
    pp = (RAT.ROOT / "pyproject.toml").read_text(encoding="utf-8")
    m = re.search(r"--cov-fail-under[=\s]+(\d+(?:\.\d+)?)", pp)
    assert m, "pyproject.toml is missing --cov-fail-under in addopts"
    expected = float(m.group(1))
    assert RAT._read_gate_from_pyproject() == expected
    assert expected >= 85.0, "Gate must never be lowered below 85 (regression ratchet)"


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


# ── _test_root() — Cloud PC layout detection ─────────────────────────────────

def test_test_root_finds_dev_layout(monkeypatch, tmp_path):
    """When tests/ and src/hilmar/ are siblings of ROOT, return ROOT."""
    (tmp_path / "tests").mkdir()
    (tmp_path / "src" / "hilmar").mkdir(parents=True)
    monkeypatch.setattr(RAT, "ROOT", tmp_path)
    assert RAT._test_root() == tmp_path


def test_test_root_finds_cloudpc_layout(monkeypatch, tmp_path):
    """When ROOT is a production xcopy dir (no tests/ or src/) but
    ROOT/hilmar-daily-routine/ has them, return the sibling — this is
    the failure mode QC-052 hit on the 2026-05-30 Cloud PC fire."""
    repo = tmp_path / "hilmar-daily-routine"
    (repo / "tests").mkdir(parents=True)
    (repo / "src" / "hilmar").mkdir(parents=True)
    monkeypatch.setattr(RAT, "ROOT", tmp_path)
    assert RAT._test_root() == repo


def test_test_root_returns_none_when_neither_layout_present(monkeypatch, tmp_path):
    """No tests/ + no src/hilmar/ anywhere → caller must SKIP gracefully
    instead of feeding pytest a dir it can't find tests in."""
    monkeypatch.setattr(RAT, "ROOT", tmp_path)
    assert RAT._test_root() is None


def test_main_skips_when_no_test_root(monkeypatch, tmp_path):
    """Reproduce the Cloud PC failure: pytest available but no test
    layout found → must write SKIPPED status, not bomb with collection
    errors (the 0/22 mode the audit reported)."""
    monkeypatch.setattr(RAT, "REPORTS", tmp_path)
    monkeypatch.setattr(RAT, "ARTIFACT", tmp_path / "test-result.json")
    monkeypatch.setattr(RAT, "ROOT", tmp_path)  # no tests/, no src/, no sibling
    monkeypatch.setattr(sys, "argv", ["run_audit_tests.py", "--quiet"])
    rc = RAT.main()
    assert rc == 0
    import json
    written = json.loads((tmp_path / "test-result.json").read_text())
    assert written["status"] == "SKIPPED"
    assert "tests/" in written["reason"]
