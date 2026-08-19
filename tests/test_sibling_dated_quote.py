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


def test_two_quotes_on_one_lane_and_rate_are_ambiguous():
    """DECISION REVERSED 2026-08-19. This test used to assert "the earliest
    covering quote wins" — pick the first quote that could have answered.

    That tie-break is the fan-out bug in miniature. Two dated rows carrying
    the SAME rate on the SAME lane are either two separate quote events or one
    quote captured twice, and nothing in the data distinguishes them. Choosing
    the earlier one is a guess, and on the standing-rate lanes that caused the
    2026-08-18 incident ($3,289 Oakland->Singapore, quoted repeatedly for
    weeks) that guess is wrong most of the time.

    So: more than one quote on a fingerprint means no pairing is evidenced,
    and the heal refuses and says so. The single-quote case — the one this
    heal was actually built for — is unaffected."""
    undated = _row("req_two_quotes")
    early = _dated_sibling()
    late = _dated_sibling()
    late["request_id"] = "req_later"
    late["response_timestamp"] = "2026-08-13T10:00:00Z"
    n, log = _run([undated, late, early])
    assert n == 0
    assert undated["response_timestamp"] is None
    assert log.warnings and "2 dated quote(s)" in log.warnings[0]


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


# ── SECOND PASS, 2026-08-19: the guards that the first fix got wrong ──────
#
# The first attempt at the fan-out guard keyed ambiguity on the SOURCE
# TIMESTAMP. That looked right and was worthless: `best` is chosen per row as
# the earliest quote covering THAT row's own ask, so several old asks on one
# standing-rate lane pick DIFFERENT quotes, land in different groups, and every
# one is stamped. Reproduced on the exact $3,289 Oakland->Singapore shape from
# the incident: 3 stamps, 0 warnings, 2 fabricated turnaround samples — silent,
# where the bug it replaced at least announced itself.
#
# Ambiguity is now judged on the FINGERPRINT the heal actually trusts:
# lane + rate to the cent.

def _sing_ask(rid, ts):
    return {"request_id": rid, "status": "LOSS", "quoted": True,
            "lane": "Oakland → Singapore", "origin": "Oakland",
            "destination": "Singapore", "ol_rate": 3289.0,
            "carrier_quoted": None, "response_timestamp": None,
            "request_timestamp": ts, "request_date": ts[:10]}


def _sing_quote(rid, ask_ts, resp_ts):
    r = _sing_ask(rid, ask_ts)
    r["status"] = "PENDING"
    r["response_timestamp"] = resp_ts
    return r


def test_a_standing_rate_lane_with_many_quotes_stamps_nothing():
    """THE REGRESSION THE FIRST FIX MISSED. Three asks, three quotes, one
    lane, one rate. Under timestamp-keyed grouping all three were stamped
    with no warning at all."""
    asks = [_sing_ask("ask_A", "2026-08-01T17:00:00Z"),
            _sing_ask("ask_B", "2026-08-10T17:00:00Z"),
            _sing_ask("ask_C", "2026-08-17T17:00:00Z")]
    quotes = [_sing_quote("q1", "2026-08-02T09:00:00Z", "2026-08-02T17:00:00Z"),
              _sing_quote("q2", "2026-08-11T09:00:00Z", "2026-08-11T17:00:00Z"),
              _sing_quote("q3", "2026-08-18T09:00:00Z", "2026-08-18T17:44:45Z")]
    n, log = _run(asks + quotes)
    assert n == 0, (
        f"{n} rows stamped on a lane carrying 3 separate quotes at one "
        "standing rate — no single pairing is evidenced.")
    assert all(a["response_timestamp"] is None for a in asks)
    assert log.warnings, "silently refusing is how this went unnoticed"


def test_no_fabricated_turnaround_survives_that_lane():
    """Two of the three silent stamps carried turnaround samples that fed the
    KPI and the carrier scorecard. Nothing may be left behind."""
    asks = [_sing_ask("ask_A", "2026-08-01T17:00:00Z"),
            _sing_ask("ask_B", "2026-08-10T17:00:00Z")]
    quotes = [_sing_quote("q1", "2026-08-02T09:00:00Z", "2026-08-02T17:00:00Z"),
              _sing_quote("q2", "2026-08-11T09:00:00Z", "2026-08-11T17:00:00Z")]
    _run(asks + quotes)
    for a in asks:
        assert a.get("turnaround_biz_hours") is None
        assert a.get("turnaround_hours") is None


def test_one_quote_on_the_lane_still_stamps_the_single_ask():
    """The guard must not disable the heal outright — a lane with exactly one
    ask and one quote is unambiguous and is the case it was built for."""
    ask = _sing_ask("ask_only", "2026-08-17T17:00:00Z")
    q = _sing_quote("q_only", "2026-08-18T09:00:00Z", "2026-08-18T17:44:45Z")
    n, _ = _run([ask, q])
    assert n == 1
    assert ask["response_timestamp"] == "2026-08-18T17:44:45+00:00"


def test_running_the_heal_twice_does_not_chain_stamps():
    """qc_selfheal runs TWICE per fire (run_pipeline.py:78 and :82, with
    patch_carriers between, over the file core.save_data persists). If a
    borrowed date may act as a source, pass 1's stamp becomes pass 2's
    evidence and the ambiguity check — which only sees this invocation's
    candidates — cannot notice."""
    ask1 = _row("req_first")
    sib = _dated_sibling()
    n1, _ = _run([ask1, sib])
    assert n1 == 1 and ask1["response_time_source"] == "sibling_quote"

    # Second pass, now with another undated row on the same lane+rate.
    ask2 = _row("req_second", request_timestamp="2026-08-05T17:07:04Z",
                request_date="2026-08-05")
    n2, _ = _run([ask1, ask2, sib])
    assert ask2["response_timestamp"] is None, (
        "the second pass stamped a row using a date the FIRST pass had "
        "already borrowed — the heal is chaining copies of one quote.")
    assert n2 == 0


def test_a_borrowed_date_is_never_a_source():
    """Directly: a row whose only date is borrowed must not appear in the
    candidate pool at all."""
    borrowed = _row("req_borrowed",
                    response_timestamp="2026-08-12T20:57:02+00:00",
                    response_time_source="sibling_quote")
    undated = _row("req_wants_it", request_timestamp="2026-08-05T17:07:04Z",
                   request_date="2026-08-05")
    n, _ = _run([undated, borrowed])
    assert n == 0
    assert undated["response_timestamp"] is None


def test_confirmed_win_guard_uses_the_shared_predicate():
    """core.is_confirmed_win accepts mdolx_refs_all as well as the primary
    ref; a fourth hand-spelling would miss that shape. core.py:1407 says to
    keep the definitions in step."""
    won = _row("req_refs_all", status="WIN", mdolx_ref=None,
               mdolx_refs_all=["261026"])
    n, _ = _run([won, _dated_sibling()])
    assert n == 0
    assert won["response_timestamp"] is None
