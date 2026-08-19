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


# ── THE FAN-OUT, 2026-08-19 ───────────────────────────────────────────────
#
# Michael, on the Aug-18 report showing OL-USA RESPONSES (11) against NEW
# REQUESTS FROM LONNY (4): "there is data missing and the request count and
# reply count vary greatly as well as container count."
#
# The tell was in the screenshot before any data was pulled: FOUR Singapore
# rows all stamped "Aug 18 1:44 PM ET" and BOTH Xingang rows "4:42 PM ET" —
# one real email fanned across every old same-lane row. That fire's own log
# confirmed 17 stamps, including one Aug-13 Yokohama quote landing on eight
# separate booking-confirmed WINs.
#
# Root cause: "same lane + rate to the cent" is not a fingerprint on lanes
# with STANDING rates. $3,289 Oakland→Singapore matches every Singapore row
# for weeks, so the heal dated July asks off an August quote and put rows
# into a day section that no email supports.

def _sing(rid, **over):
    r = {"request_id": rid, "status": "LOSS", "quoted": True,
         "lane": "Oakland → Singapore", "origin": "Oakland",
         "destination": "Singapore", "ol_rate": 3289.0,
         "carrier_quoted": None, "response_timestamp": None,
         "request_timestamp": "2026-07-09T17:48:00Z",
         "request_date": "2026-07-09", "teu_requested": 10}
    r.update(over)
    return r


def _sing_dated(**over):
    base = {"status": "PENDING",
            "request_timestamp": "2026-08-17T20:31:00Z",
            "request_date": "2026-08-17",
            "response_timestamp": "2026-08-18T17:44:45Z",
            "teu_requested": 12}
    base.update(over)
    return _sing("req_sing_quoted", **base)


def test_one_quote_does_not_date_four_old_asks():
    """THE REGRESSION. Four July/August Singapore asks all carrying the
    standing $3,289 were stamped with one Aug-18 quote time, and all four
    rendered in that day's OL-USA RESPONSES."""
    olds = [_sing(f"req_sing_{i}", request_timestamp=f"2026-07-0{i}T12:00:00Z",
                  request_date=f"2026-07-0{i}", teu_requested=None)
            for i in (1, 2, 3, 4)]
    n, log = _run(olds + [_sing_dated()])
    assert n == 0, (
        f"{n} rows stamped off ONE Aug-18 Singapore quote — a standing lane "
        "rate is not a fingerprint, and this is what put 11 responses against "
        "4 new requests on the Aug-18 report.")
    assert all(r["response_timestamp"] is None for r in olds)


def test_the_refusal_names_the_rows_it_refused():
    """A silent refusal is how the count reached 41 last time. The log must
    say which rows shared the source so a human can settle it."""
    olds = [_sing("req_sing_a", teu_requested=None),
            _sing("req_sing_b", teu_requested=None)]
    _n, log = _run(olds + [_sing_dated()])
    text = " ".join(log.warnings)
    assert "req_sing_a" in text and "req_sing_b" in text, (
        f"refusal did not name the contending rows: {text[:300]}")


def test_a_single_unambiguous_row_is_still_stamped():
    """The Algeciras case this heal exists for is ONE half-copied row. The
    fan-out guard must not cost that."""
    undated = _row("req_0818ca58087a1cc8")
    n, _ = _run([undated, _dated_sibling()])
    assert n == 1
    assert undated["response_timestamp"] == "2026-08-12T20:57:02+00:00"


def test_a_conflicting_container_count_is_a_different_ask():
    """Michael named this directly: "as well as container count". The Aug-18
    fire dated an Aug-5 ask for 15x20' off a quote answering an ask for 8."""
    undated = _sing("req_15_boxes", teu_requested=15)
    n, _ = _run([undated, _sing_dated(teu_requested=8)])
    assert n == 0, "a quote for 8 boxes must not date an ask for 15"


def test_container_count_conflict_also_blocks():
    undated = _row("req_cc", container_count=15)
    n, _ = _run([undated, _dated_sibling(container_count=8)])
    assert n == 0


def test_a_missing_shape_on_either_side_does_not_block():
    """Absent is not conflicting — most rows carry no container_count, and
    requiring it would silently disable the heal entirely."""
    undated = _row("req_no_shape", teu_requested=None, container_count=None)
    n, _ = _run([undated, _dated_sibling(teu_requested=8)])
    assert n == 1


def test_a_booking_confirmed_win_is_never_stamped():
    """Eight Yokohama WINs took one Aug-13 quote's time on 2026-08-18, and
    one produced a fabricated 29.2 biz-hr turnaround sample. A booked row's
    resolving event was the BOOKING; there is no quote clock to recover."""
    won = _row("req_1debac530d998acb", status="WIN", mdolx_ref="261026")
    n, _ = _run([won, _dated_sibling()])
    assert n == 0
    assert won["response_timestamp"] is None
    assert won.get("turnaround_biz_hours") is None


def test_stamped_rows_are_marked_as_borrowed():
    """The report has to tell a borrowed date from an evidenced one — that
    marker is what keeps these rows out of the day's OL-USA RESPONSES."""
    undated = _row("req_marked")
    n, _ = _run([undated, _dated_sibling()])
    assert n == 1
    assert undated["response_time_source"] == "sibling_quote"
