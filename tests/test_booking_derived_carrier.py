"""A carrier a BOOKING wrote is not a quote we failed to date.

Michael 2026-08-13, on the report banner reading "22 further quotes are
recorded with a rate or carrier but no response time": "still shouldn't
exist".

MEASURED, not inferred (diag-blob run 31732181146, stored state). The 22:

    10  LOSS, rate present, no booking ref      <- real undated quotes
     8  WIN, NO rate, booking ref, operator-corrected
     3  WIN, rate present, booking ref
     1  WIN, NO rate, booking ref

Nine carry NO rate at all. Their only evidence is carrier_quoted, and that
carrier was written by the reconciliation that folded in OL's transaction
report — CMA CGM on MDOLX261026-33, ONE on MDOLX261068. That is BOOKING
evidence: it says a shipment moved and on whose vessel. It says nothing about
a quote email arriving, and for Jun-Aug none did — OL replied to Lonny with
the group copied and it never reached the mailbox the tracker read.

So `rate OR carrier` is the right test for "did OL respond with something"
and the wrong one for "is there a quote here we failed to date".

THE COUNT IN THE NOTE AND THE COUNT IN THE AUDIT COME FROM TWO DIFFERENT
FUNCTIONS and have drifted before (#148). Both are pinned here.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import core  # noqa: E402
import gen_email as GE  # noqa: E402


def _row(**over):
    r = {"request_id": "req_x", "status": "WIN", "quoted": True,
         "lane": "Oakland → Yokohama", "origin": "Oakland",
         "destination": "Yokohama",
         "request_timestamp": "2026-06-20T12:00:00Z",
         "ol_rate": None, "carrier_quoted": None, "response_timestamp": None}
    r.update(over)
    return r


# ── the nine ────────────────────────────────────────────────────────

def test_carrier_with_a_booking_and_no_rate_is_booking_derived():
    """req_1debac530d998acb and its seven siblings, exactly as stored."""
    r = _row(carrier_quoted="CMA CGM", mdolx_ref="261029")
    assert core.quote_evidence_is_booking_derived(r) is True


def test_booking_ref_may_arrive_as_any_of_the_booking_fields():
    for field, value in (("mdolx_ref", "261029"),
                         ("mdolx_refs_all", ["261029"]),
                         ("booking_no", "NAM8664234"),
                         ("booking_timestamp", "2026-06-21T10:00:00Z")):
        r = _row(carrier_quoted="CMA CGM", **{field: value})
        assert core.quote_evidence_is_booking_derived(r) is True, field


# ── the line this must not cross ────────────────────────────────────

def test_a_real_rate_always_wins():
    """The 3 WINs that DO carry a rate stay counted. A rate is quote
    evidence whatever else the row holds."""
    r = _row(ol_rate=3176.0, carrier_quoted="CMA CGM", mdolx_ref="260364")
    assert core.quote_evidence_is_booking_derived(r) is False


def test_a_bare_carrier_with_no_booking_is_still_a_quote():
    """OL does sometimes quote a carrier with the rate to follow — QC-056's
    own note says so. Without a booking to explain it, the carrier is the
    quote."""
    r = _row(carrier_quoted="Wan Hai")
    assert core.quote_evidence_is_booking_derived(r) is False


def test_a_row_with_no_evidence_at_all_is_not_this_case():
    assert core.quote_evidence_is_booking_derived(_row()) is False
    assert core.quote_evidence_is_booking_derived({}) is False
    assert core.quote_evidence_is_booking_derived(None) is False


def test_the_placeholder_rate_string_does_not_count_as_a_rate():
    """qc_selfheal's NQ heal writes the STRING 'Not Quoted' into ol_rate. It
    is not a rate, so a booking-derived carrier is still booking-derived."""
    r = _row(ol_rate="Not Quoted", carrier_quoted="CMA CGM", mdolx_ref="261029")
    assert core.quote_evidence_is_booking_derived(r) is True


# ── the note and the audit must agree ───────────────────────────────

def test_the_report_note_drops_the_booking_derived_rows():
    rows = [
        _row(request_id="keep_rate", ol_rate=570.0, status="LOSS"),
        _row(request_id="keep_bare_carrier", carrier_quoted="Wan Hai",
             status="LOSS"),
        _row(request_id="drop_1", carrier_quoted="CMA CGM", mdolx_ref="261029"),
        _row(request_id="drop_2", carrier_quoted="CMA CGM", mdolx_ref="261030"),
    ]
    got = {r["request_id"] for r in GE.undated_quotes({"requests": rows})}
    assert got == {"keep_rate", "keep_bare_carrier"}


def test_qc077_and_the_note_count_the_same_rows():
    """#148 shipped two numbers off one dataset. Pin them together."""
    import qc_selfheal as QS

    rows = [
        _row(request_id="keep_rate", ol_rate=570.0, status="LOSS"),
        _row(request_id="drop_1", carrier_quoted="CMA CGM", mdolx_ref="261029"),
        _row(request_id="drop_2", carrier_quoted="ONE", mdolx_ref="261068"),
        _row(request_id="stand_1", carrier_quoted="ONE", ol_rate=100.0,
             status="WIN"),
    ]
    note_ids = {r["request_id"] for r in GE.undated_quotes({"requests": rows})}
    qc_ids = {
        r["request_id"] for r in rows
        if (core.is_real_rate(r.get("ol_rate")) or r.get("carrier_quoted"))
        and not r.get("response_timestamp")
        and not core.has_no_rfq_chain(r)
        and not core.quote_evidence_is_booking_derived(r)
    }
    assert note_ids == qc_ids
    assert "drop_1" not in note_ids and "drop_2" not in note_ids
    # stand_* is excluded by has_no_rfq_chain, as it always was.
    assert "stand_1" not in note_ids
    assert QS is not None
