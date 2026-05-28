"""Tests for hilmar.parser_accuracy — the module that enforces the ≥95%
parser-accuracy gate (QC-039). It shipped at 0% coverage; the daily test
routine (run_audit_tests.py, added 2026-05-28) surfaced it as the top
untested module, and this closes that gap."""
from __future__ import annotations

from hilmar.parser_accuracy import (
    ACCURACY_THRESHOLD,
    CRITICAL_FIELDS,
    PER_FIELD_THRESHOLDS,
    _is_populated,
    _is_quoted,
    _is_standalone,
    _is_chain_quoted,
    _is_win,
    _threshold_for,
    compute_accuracy,
    format_report,
)


# ── _is_populated ──────────────────────────────────────────────────────────

def test_is_populated_none_and_empty():
    assert _is_populated(None) is False
    assert _is_populated("") is False
    assert _is_populated("   ") is False
    assert _is_populated([]) is False
    assert _is_populated({}) is False


def test_is_populated_real_values():
    assert _is_populated("CMA CGM") is True
    assert _is_populated(0) is True          # 0 is a valid numeric value
    assert _is_populated(False) is True      # bool present counts
    assert _is_populated(["x"]) is True
    assert _is_populated({"k": "v"}) is True


# ── predicates ───────────────────────────────────────────────────────────────

def test_is_win_and_quoted():
    assert _is_win({"status": "WIN"}) is True
    assert _is_win({"status": "Q&L"}) is False
    assert _is_quoted({"status": "Q&L"}) is True
    assert _is_quoted({"status": "LOSS", "quoted": True}) is True
    assert _is_quoted({"status": "NQ"}) is False


def test_standalone_and_chain_quoted():
    standalone = {"request_id": "stand_0007", "status": "WIN", "quoted": True}
    chain = {"request_id": "req_abc", "status": "Q&L"}
    assert _is_standalone(standalone) is True
    assert _is_standalone(chain) is False
    # standalone WIN is quoted but NOT chain-quoted (no rate-response email)
    assert _is_chain_quoted(standalone) is False
    assert _is_chain_quoted(chain) is True


def test_threshold_for_uses_overrides():
    assert _threshold_for("mdolx_ref") == PER_FIELD_THRESHOLDS["mdolx_ref"]
    # unknown field falls back to the global threshold
    assert _threshold_for("totally_unknown_field") == ACCURACY_THRESHOLD


# ── compute_accuracy ─────────────────────────────────────────────────────────

def _good_row(**over):
    row = {
        "request_id": "req_1", "status": "Q&L", "quoted": True,
        "origin": "Oakland", "destination": "Yokohama", "lane": "Oakland → Yokohama",
        "containers": "2x40HC", "container_count": 2, "teu_requested": 4,
        "request_date": "2026-05-01",
        "carrier_quoted": "CMA CGM", "ol_rate": 3500,
        "etd_offered": "2026-05-10", "eta_offered": "2026-05-22",
        "dest_free_time": "7 days", "product": "milk powder", "lonny_notes": "rush",
    }
    row.update(over)
    return row


def test_compute_accuracy_all_populated_passes():
    res = compute_accuracy([_good_row(), _good_row(request_id="req_2")])
    assert res["pass"] is True
    assert res["overall_rate"] >= ACCURACY_THRESHOLD
    assert res["failing_fields"] == []
    assert res["critical_failing"] == []
    assert res["row_count"] == 2


def test_compute_accuracy_critical_field_failure_blocks():
    # carrier_quoted (a CRITICAL field) missing on a quoted row → fail
    bad = _good_row(carrier_quoted=None)
    res = compute_accuracy([bad])
    assert res["pass"] is False
    assert "carrier_quoted" in res["failing_fields"]
    assert "carrier_quoted" in res["critical_failing"]
    assert "carrier_quoted" in CRITICAL_FIELDS


def test_compute_accuracy_empty_input_is_vacuously_passing():
    res = compute_accuracy([])
    assert res["pass"] is True
    assert res["overall_rate"] == 1.0
    assert res["row_count"] == 0


def test_compute_accuracy_na_field_excluded():
    # An NQ row makes quote-only fields N/A rather than failing.
    nq = {
        "request_id": "req_nq", "status": "NQ",
        "origin": "Oakland", "destination": "Osaka", "lane": "Oakland → Osaka",
        "containers": "1x40", "container_count": 1, "teu_requested": 2,
        "request_date": "2026-05-02",
        # product/lonny_notes apply to any non-standalone row (incl. NQ).
        "product": "milk powder", "lonny_notes": "no response from OL",
    }
    res = compute_accuracy([nq])
    # ol_rate only applies to chain-quoted rows; NQ → not applicable → n_a
    assert res["field_stats"]["ol_rate"]["n_a"] is True
    assert res["field_stats"]["carrier_quoted"]["n_a"] is True
    assert res["pass"] is True


def test_per_field_threshold_tolerates_known_gap():
    # mdolx_ref has an 0.80 override: a WIN missing it on 1 of 5 wins (80%)
    # should NOT fail, while carrier_won (no override, 0.95) at 80% would.
    wins = [_good_row(request_id=f"w{i}", status="WIN", carrier_won="ONE",
                      mdolx_ref="MDOLX123") for i in range(4)]
    wins.append(_good_row(request_id="w5", status="WIN", carrier_won="ONE",
                          mdolx_ref=None))
    res = compute_accuracy(wins)
    stats = res["field_stats"]["mdolx_ref"]
    assert stats["rate"] == 0.8
    assert "mdolx_ref" not in res["failing_fields"]  # 0.80 >= 0.80 override


# ── format_report ────────────────────────────────────────────────────────────

def test_format_report_pass_and_fail():
    ok = format_report(compute_accuracy([_good_row()]))
    assert "PASS" in ok and "All fields" in ok
    fail = format_report(compute_accuracy([_good_row(carrier_quoted=None)]))
    assert "FAIL" in fail and "CRITICAL" in fail
