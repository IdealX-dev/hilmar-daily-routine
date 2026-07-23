"""QC-066 — outcome must never predate its own request.

Regression for the 2026-07-23 report (Michael: "your quality control system is
not functioning"): a NEW Jul-22 HCMC request surfaced already carrying a stale
outcome inherited from Lonny's recurring Outlook thread, so PENDING OL showed 0
while the request was neither responded-to nor open anywhere. QC-066 flags any
row whose newest status event predates its own request date, and any report-day
request sitting in a terminal status with no same-day-or-later event.
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import qc_selfheal as q  # noqa: E402

RD = date(2026, 7, 22)


def _r(rid, req_date, status, hist_dates, **kw):
    return {"request_id": rid, "request_date": req_date, "status": status,
            "quoted": kw.pop("quoted", True),
            "status_history": [{"from": "PENDING", "to": status,
                                "at": f"{d}T18:00:00Z"} for d in hist_dates],
            **kw}


def test_flags_outcome_predating_request():
    # THE HCMC shape: requested Jul 22, newest event Jul 20 (old thread's win).
    row = _r("req_hcmc", "2026-07-22", "WIN", ["2026-07-20"])
    bad = q.qc066_impossible_states([row], report_day=RD)
    assert len(bad) == 1 and bad[0][0] == "req_hcmc"
    assert "predates request" in bad[0][1]


def test_flags_report_day_terminal_without_same_day_event():
    row = _r("req_t", "2026-07-22", "LOSS", ["2026-07-22"])
    # same-day event → clean
    assert q.qc066_impossible_states([row], report_day=RD) == []


def test_clean_normal_flow_not_flagged():
    rows = [
        _r("req_ok", "2026-07-22", "WIN", ["2026-07-22"]),           # same-day win
        _r("req_later", "2026-07-20", "WIN", ["2026-07-21"]),        # older ask, later win
        {"request_id": "req_legacy", "request_date": "2026-07-22",
         "status": "WIN", "quoted": True, "status_history": []},     # legacy: skip
        _r("req_pend", "2026-07-22", "PENDING", [], quoted=False),   # open request
    ]
    assert q.qc066_impossible_states(rows, report_day=RD) == []


def test_evening_et_event_not_false_flagged():
    # Event Jul 22 9:30 PM EDT = Jul 23 01:30Z — ET conversion keeps it Jul 22.
    row = {"request_id": "req_eve", "request_date": "2026-07-22", "status": "WIN",
           "quoted": True,
           "status_history": [{"from": "PENDING", "to": "WIN",
                               "at": "2026-07-23T01:30:00Z"}]}
    assert q.qc066_impossible_states([row], report_day=RD) == []


def test_standalone_rows_exempt_from_report_day_rule_but_not_causality():
    ok = {"request_id": "stand_1", "request_date": "2026-07-22", "status": "WIN",
          "quoted": True,
          "status_history": [{"from": "PENDING", "to": "WIN",
                              "at": "2026-07-22T18:00:00Z"}]}
    assert q.qc066_impossible_states([ok], report_day=RD) == []
