"""QC-063 consecutive-failure ratchet — a step dead for DAYS, not a blip.

The 8 best-effort steps + the test routine exit 0 by design, so per-fire a dead
step is invisible and a step failing every fire for a week looks identical to a
one-day blip. run_pipeline records each fire's failed steps; QC-063 escalates a
step that failed the last 3 consecutive fires.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import qc_selfheal as q  # noqa: E402
import run_pipeline as RP  # noqa: E402


# ── pure helper: consecutive_failed_steps ────────────────────────────────
def test_consecutive_failed_steps_flags_a_step_dead_3_fires():
    hist = [
        {"failed": ["Sync to ol-quote-tracker", "Rate intelligence"]},
        {"failed": ["Sync to ol-quote-tracker"]},
        {"failed": ["Sync to ol-quote-tracker", "Historian (finalized → Turso)"]},
    ]
    assert q.consecutive_failed_steps(hist, n=3) == ["Sync to ol-quote-tracker"]


def test_consecutive_failed_steps_ignores_a_one_day_blip():
    hist = [
        {"failed": ["Sync to ol-quote-tracker"]},
        {"failed": []},
        {"failed": ["Sync to ol-quote-tracker"]},
    ]
    assert q.consecutive_failed_steps(hist, n=3) == []


def test_consecutive_failed_steps_needs_n_fires_of_history():
    assert q.consecutive_failed_steps([{"failed": ["X"]}, {"failed": ["X"]}], n=3) == []


# ── run_pipeline records history ─────────────────────────────────────────
def test_record_step_history_appends_and_rolls(tmp_path):
    p = tmp_path / "step-history.json"
    for i in range(5):
        RP._record_step_history([f"step{i}"], path=p, keep=3)
    hist = json.loads(p.read_text(encoding="utf-8"))
    assert len(hist) == 3                          # rolled to keep=3
    assert hist[-1]["failed"] == ["step4"]
    assert "ts" in hist[-1]


def test_record_step_history_survives_corrupt_file(tmp_path):
    p = tmp_path / "step-history.json"
    p.write_text("not json", encoding="utf-8")
    RP._record_step_history(["step"], path=p)       # must not raise
    hist = json.loads(p.read_text(encoding="utf-8"))
    assert hist[-1]["failed"] == ["step"]


# ── QC-063 via the real phase_6_rules ────────────────────────────────────
def _base_data():
    return {"version": "2", "requests": [],
            "summary": {"wins": 0, "quoted_lost": 0, "not_quoted": 0,
                        "pending_hilmar": 0, "win_rate": 0.0, "quote_rate": 0.0,
                        "teu_requested": 0, "teu_won": 0, "teu_quoted_lost": 0,
                        "teu_not_quoted": 0, "teu_pending": 0, "total_entries": 0}}


def _run(monkeypatch, history):
    monkeypatch.setenv("HILMAR_QC_NO_PIP", "1")
    monkeypatch.setattr(q, "load_step_history", lambda: history)
    log = q.Log()
    q.phase_6_rules(log, _base_data())
    return log


def test_qc063_warns_on_a_step_dead_3_fires(monkeypatch):
    hist = [{"failed": ["Rate intelligence"]} for _ in range(3)]
    log = _run(monkeypatch, hist)
    assert any("QC-063" in m and "Rate intelligence" in m for m in log.warnings), log.warnings


def test_qc063_quiet_when_no_step_dead_3_fires(monkeypatch):
    hist = [{"failed": ["Rate intelligence"]}, {"failed": []}, {"failed": ["Rate intelligence"]}]
    log = _run(monkeypatch, hist)
    assert not any("QC-063" in m for m in log.warnings + log.errors)


def test_qc063_skips_with_no_history(monkeypatch, capsys):
    log = _run(monkeypatch, [])
    out = capsys.readouterr().out
    assert "QC-063" in out and "skipped" in out
    assert not any("QC-063" in m for m in log.warnings + log.errors)
