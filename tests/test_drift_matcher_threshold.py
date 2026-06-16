"""drift_check phase-2 matcher-drift severity — 2026-06-16 stranded-fire fix.

HILMAR-DAILY-TRACKER-6: the production daily fire failed (no client email)
for 4 runs because phase-2 flagged ONE low-confidence "matcher drift
candidate" and that unconditionally HALTED the send — yet phase 2 is
report-only (can't auto-heal), so the block was unrecoverable without manual
intervention. Now 1–2 candidates WARN (surface for operator reattach) and
only a systemic count (>= MATCHER_DRIFT_FAIL_FLOOR) blocks. Genuine
data-corruption gates (quote_rate / dup imids / NQ schema) still hard-block.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import drift_check as DC  # noqa: E402


def _drift_pair(dest: str, idx: int) -> list[dict]:
    """A quoted record attached 10h off + a same-dest NQ only 1h off the
    response → a phase-2 drift candidate (ratio 10x, gap 9h)."""
    return [
        {"request_id": f"q{idx}", "status": "WIN", "quoted": True,
         "destination": dest, "carrier_won": "MSC",
         "request_timestamp": "2026-06-01T00:00:00Z",
         "response_timestamp": "2026-06-01T10:00:00Z"},
        {"request_id": f"n{idx}", "status": "LOSS", "quoted": False,
         "loss_reason": "NO_RESPONSE", "destination": dest,
         "request_timestamp": "2026-06-01T09:00:00Z"},
    ]


def _run(tmp_path, requests):
    cfg = json.loads((ROOT / "config.json").read_text())
    data = {"version": "2", "requests": requests,
            "summary": {"quote_rate": 100.0, "wins": 0, "quoted_lost": 0,
                        "not_quoted": 0, "pending_hilmar": 0}}
    (tmp_path / "data.json").write_text(json.dumps(data))
    (tmp_path / "reports").mkdir(exist_ok=True)
    cfg.setdefault("paths", {})["data"] = str(tmp_path / "data.json")
    cfg["paths"]["reports"] = str(tmp_path / "reports")
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps(cfg))
    rc = DC.run(str(cfg_file), auto_heal=True, dry=True)
    result = json.loads((tmp_path / "reports" / "drift-result.json").read_text())
    return rc, result


def test_single_drift_candidate_warns_not_blocks(tmp_path):
    rc, result = _run(tmp_path, _drift_pair("Tokyo", 1))
    assert result["phase2"]["matcher_drift_count"] == 1
    assert result["status"] == "WARN"     # surfaced, not blocked
    assert rc == 0                         # does NOT halt the daily send
    assert any("matcher drift candidate" in w for w in result["warn_reasons"])
    assert not result["fail_reasons"]


def test_two_drift_candidates_still_warn(tmp_path):
    reqs = _drift_pair("Tokyo", 1) + _drift_pair("Osaka", 2)
    rc, result = _run(tmp_path, reqs)
    assert result["phase2"]["matcher_drift_count"] == 2
    assert result["status"] == "WARN"
    assert rc == 0


def test_systemic_drift_blocks(tmp_path):
    reqs = (_drift_pair("Tokyo", 1) + _drift_pair("Osaka", 2)
            + _drift_pair("Busan", 3))   # 3 == MATCHER_DRIFT_FAIL_FLOOR
    rc, result = _run(tmp_path, reqs)
    assert result["phase2"]["matcher_drift_count"] >= DC.MATCHER_DRIFT_FAIL_FLOOR
    assert result["status"] == "FAIL"     # systemic → block
    assert rc == 1
    assert any("systemic" in f for f in result["fail_reasons"])


def test_no_drift_passes(tmp_path):
    clean = [{"request_id": "q1", "status": "WIN", "quoted": True,
              "destination": "Tokyo", "carrier_won": "MSC",
              "request_timestamp": "2026-06-01T09:30:00Z",
              "response_timestamp": "2026-06-01T10:00:00Z"}]
    rc, result = _run(tmp_path, clean)
    assert result["phase2"]["matcher_drift_count"] == 0
    assert rc == 0
