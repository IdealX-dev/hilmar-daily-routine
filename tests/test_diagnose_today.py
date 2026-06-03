"""Tests for scripts/diagnose_today.py — the single-paste incident-response
diagnostic. Created 2026-06-03 to remove the multi-round-trip friction of
"paste qc-result.json, also paste run-log, also paste Sentry".

Locks in:
  - Each section degrades to a clear "<missing>" / "<not configured>" line
    instead of crashing when its input is absent (so the tool works on a
    cold CI box, not just on the Cloud PC).
  - When inputs ARE present, the section headers + key fields surface.
  - --sections subset works.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "diagnose_today.py"


def _run(*args, cwd: Path) -> subprocess.CompletedProcess:
    """Launch the COPY of diagnose_today.py we staged inside `cwd`, not the
    in-repo original — otherwise `Path(__file__).parent.parent` resolves to
    the real repo and the test's tmp_path reports/ + data file are ignored."""
    staged = cwd / "scripts" / "diagnose_today.py"
    return subprocess.run(
        [sys.executable, str(staged), *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        env={"PYTHONIOENCODING": "utf-8", "PATH": __import__("os").environ.get("PATH", "")},
        timeout=60,
    )


def _stage_repo(tmp_path: Path) -> Path:
    """Make a minimal repo layout under tmp_path that diagnose_today can run
    against. Mirrors the real ROOT detection (scripts/ + reports/ at root)."""
    (tmp_path / "scripts").mkdir()
    (tmp_path / "reports").mkdir()
    (tmp_path / "docs").mkdir()
    # Copy diagnose_today.py + its single in-repo dep (core for ET tz)
    (tmp_path / "scripts" / "diagnose_today.py").write_bytes(SCRIPT.read_bytes())
    core_src = REPO_ROOT / "scripts" / "core.py"
    if core_src.exists():
        (tmp_path / "scripts" / "core.py").write_bytes(core_src.read_bytes())
    return tmp_path


def test_cold_run_does_not_crash(tmp_path: Path):
    """Empty reports/ + no tracking-data-v2.json + no Sentry token — the
    tool must still produce a usable report (no traceback, exit 0)."""
    repo = _stage_repo(tmp_path)
    r = _run(cwd=repo)
    assert r.returncode == 0, f"stderr: {r.stderr}"
    assert "QC RESULT" in r.stdout
    assert "<reports/qc-result.json missing>" in r.stdout
    assert "DATA" in r.stdout
    assert "Traceback" not in r.stdout
    assert "Traceback" not in r.stderr


def test_qc_section_renders_error_and_warning_details(tmp_path: Path):
    repo = _stage_repo(tmp_path)
    qc = {
        "status": "HAS_ERRORS",
        "fixes": 1,
        "warnings": 2,
        "errors": 1,
        "counts": {"total": 100, "wins": 20, "ql": 60, "nq": 10, "pending": 10},
        "error_details": ["QC-007: R-1234 still PENDING past 24h"],
        "warning_details": [
            "QC-011: email-subject.txt is 24.0h stale",
            "QC-040: cross-folder drift in LOSS_REASONS",
        ],
        "data_freshness": {"data_last_updated": "2026-06-03T14:01:00+00:00"},
    }
    (repo / "reports" / "qc-result.json").write_text(json.dumps(qc), encoding="utf-8")
    r = _run("--sections", "qc", cwd=repo)
    assert r.returncode == 0, f"stderr: {r.stderr}"
    assert "QC-007: R-1234 still PENDING past 24h" in r.stdout
    assert "QC-011: email-subject.txt is 24.0h stale" in r.stdout
    assert "QC-040: cross-folder drift in LOSS_REASONS" in r.stdout
    assert "status=HAS_ERRORS" in r.stdout


def test_sections_subset_excludes_others(tmp_path: Path):
    repo = _stage_repo(tmp_path)
    r = _run("--sections", "files", cwd=repo)
    assert r.returncode == 0
    assert "ARTIFACT FRESHNESS" in r.stdout
    # Other sections must NOT appear
    assert "QC RESULT" not in r.stdout
    assert "RUN-LOG" not in r.stdout
    assert "SENTRY" not in r.stdout


def test_log_tail_respects_log_tail_flag(tmp_path: Path):
    repo = _stage_repo(tmp_path)
    # Build a fake run-log with 200 numbered lines
    lines = [f"line {i:03d}" for i in range(1, 201)]
    (repo / "reports" / "run-log.txt").write_text("\n".join(lines), encoding="utf-8")
    r = _run("--sections", "log", "--log-tail", "10", cwd=repo)
    assert r.returncode == 0, f"stderr: {r.stderr}"
    # First lines should NOT be present, last lines should
    assert "line 001" not in r.stdout
    assert "line 200" in r.stdout
    assert "line 191" in r.stdout


def test_data_section_summarizes_tracking_data(tmp_path: Path):
    repo = _stage_repo(tmp_path)
    data = {
        "last_updated": "2026-06-03T14:01:00+00:00",
        "requests": [{"i": i} for i in range(157)],
        "summary": {
            "wins": 35, "quoted_lost": 100, "not_quoted": 10, "pending_hilmar": 12,
            "win_rate": 25.9, "quote_rate": 91.4,
        },
    }
    (repo / "tracking-data-v2.json").write_text(json.dumps(data), encoding="utf-8")
    r = _run("--sections", "data", cwd=repo)
    assert r.returncode == 0
    assert '"row_count": 157' in r.stdout
    assert '"wins": 35' in r.stdout
    assert '"quote_rate": 91.4' in r.stdout


def test_unknown_section_does_not_crash(tmp_path: Path):
    repo = _stage_repo(tmp_path)
    r = _run("--sections", "files,bogus,qc", cwd=repo)
    assert r.returncode == 0
    assert "UNKNOWN SECTION: bogus" in r.stdout
    assert "ARTIFACT FRESHNESS" in r.stdout
    assert "QC RESULT" in r.stdout
