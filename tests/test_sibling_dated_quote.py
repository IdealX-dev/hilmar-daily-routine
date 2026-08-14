"""A row that borrowed a quote's rate must borrow its date.

2026-08-14. The report told Michael "1 recent quote has a rate or carrier but
no response time, so it is missing from the dated responses above". Michael:
"untrue again as i gave this to you before and emailed you a copy."

HE WAS RIGHT, measured on stored state (diag run 31808701667). The row was
the Aug-4 Oakland→Algeciras ask:

    req_0818ca58087a1cc8   rate=4938.0  carrier='CMA CGM'  resp_ts=None
    source_imids = [Lonny's own Aug-4 ask]

and the quote email he forwarded WAS in stage — an mbd_rate_response at
2026-08-12T20:57:02Z, correctly dated onto the Aug-12 sibling ask
(req_d1f44ef27de8f3bd, same lane, same $4,938, same CMA CGM).

The sibling heals had propagated the quote's RATE and CARRIER onto the older
ask, but not its TIMESTAMP. Half the evidence travelled, and the half-copy
is what manufactured the "quoted but undateable" row the banner reported.
No email was missing. The pipeline split one quote across two fields.

_stamp_response_from_dated_sibling completes the copy: same lane + rate to
the cent + response postdating the ask ⇒ the sibling's response time is
stamped, earliest covering quote first. Timing obeys the same >40 biz-hour
rule as everywhere else: date kept, statistic excluded.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import qc_selfheal as QS  # noqa: E402


def _row(rid, **over):
    r = {"request_id": rid, "status": "LOSS", "quoted": True,
         "lane": "Oakland → Algeciras", "origin": "Oakland",
         "destination": "Algeciras", "ol_rate": 4938.0,
         "carrier_quoted": "CMA CGM", "response_timestamp": None,
         "request_timestamp": "2026-08-04T17:07:04Z",
         "request_date": "2026-08-04"}
    r.update(over)
    return r


def _dated_sibling(**over):
    return _row("req_d1f44ef27de8f3bd", status="PENDING",
                request_timestamp="2026-08-12T17:05:06Z",
                request_date="2026-08-12",
                response_timestamp="2026-08-12T20:57:02Z", **over)


def _run(rows):
    log = QS.Log()
    n = QS._stamp_response_from_dated_sibling(log, rows)
    return n, log


def test_the_algeciras_row_gets_the_date_michael_gave_us():
    """The exact stored shape, end to end."""
    undated = _row("req_0818ca58087a1cc8")
    rows = [undated, _dated_sibling()]
    n, _log = _run(rows)
    assert n == 1
    assert undated["response_timestamp"] == "2026-08-12T20:57:02+00:00"
    # Aug 4 ask → Aug 12 quote is far past 40 biz-hours: date kept, timing out.
    assert undated["turnaround_biz_hours"] is None
    assert undated["turnaround_hours"] is None


def test_a_quote_cannot_answer_a_question_not_yet_asked():
    """A dated quote BEFORE this row's ask is a different negotiation."""
    undated = _row("req_late_ask", request_timestamp="2026-08-13T09:00:00Z")
    rows = [undated, _dated_sibling()]  # sibling quoted Aug 12, ask is Aug 13
    n, _ = _run(rows)
    assert n == 0
    assert undated["response_timestamp"] is None


def test_a_different_rate_is_a_different_quote():
    undated = _row("req_other_rate", ol_rate=4874.0)
    n, _ = _run([undated, _dated_sibling()])
    assert n == 0


def test_a_different_carrier_is_a_different_quote():
    undated = _row("req_other_carrier", carrier_quoted="MSC")
    n, _ = _run([undated, _dated_sibling()])
    assert n == 0


def test_the_earliest_covering_quote_wins():
    undated = _row("req_two_quotes")
    early = _dated_sibling()
    late = _dated_sibling()
    late["request_id"] = "req_later"
    late["response_timestamp"] = "2026-08-13T10:00:00Z"
    n, _ = _run([undated, late, early])
    assert n == 1
    assert undated["response_timestamp"].startswith("2026-08-12T20:57:02")


def test_a_short_gap_keeps_its_timing():
    """Inside 40 biz-hours the turnaround is real and kept."""
    undated = _row("req_recent_ask", request_timestamp="2026-08-12T14:00:00Z")
    n, _ = _run([undated, _dated_sibling()])
    assert n == 1
    assert isinstance(undated["turnaround_biz_hours"], float)
    assert undated["turnaround_biz_hours"] <= 40


def test_standalone_bookings_and_dated_rows_are_untouched():
    stand = _row("stand_260842", response_timestamp=None)
    dated = _dated_sibling()
    before = dated["response_timestamp"]
    n, _ = _run([stand, dated])
    assert n == 0
    assert stand["response_timestamp"] is None
    assert dated["response_timestamp"] == before


def test_carrier_only_rows_are_not_fingerprintable():
    """No rate → nothing to match on. Booking-derived carriers stay put."""
    undated = _row("req_no_rate", ol_rate=None)
    n, _ = _run([undated, _dated_sibling()])
    assert n == 0
