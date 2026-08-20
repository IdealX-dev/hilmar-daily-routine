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

from datetime import datetime, timedelta, timezone  # noqa: E402

import core  # noqa: E402

#: These tests are about the BOOKING-DERIVED predicate and QC-077's advice,
#: not about recency. core.undated_quote_is_current now drops anything older
#: than 14 days, so a hardcoded June date would remove the rows before the
#: predicate under test ever saw them — the fixture has to stay current.
_RECENT = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat().replace("+00:00", "Z")
import gen_email as GE  # noqa: E402


def _row(**over):
    r = {"request_id": "req_x", "status": "WIN", "quoted": True,
         "lane": "Oakland → Yokohama", "origin": "Oakland",
         "destination": "Yokohama",
         "request_timestamp": _RECENT,
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
    # CALL the predicate, do not re-type it. This assertion was a hand-copy
    # of QC-077's four clauses until 2026-08-19, when production grew a fifth
    # (booking-confirmed WINs) and this test stayed green while the banner and
    # QC-077 reported different numbers off one dataset — the very #148 bug it
    # was written to prevent. Mutation-proven: deleting the exclusion from
    # qc_selfheal changed nothing in the suite.
    note_ids = {r["request_id"] for r in GE.undated_quotes({"requests": rows})}
    qc_ids = {r["request_id"] for r in rows if core.is_undated_quote(r)}
    assert note_ids == qc_ids
    assert "drop_1" not in note_ids and "drop_2" not in note_ids
    # stand_* is excluded by has_no_rfq_chain, as it always was.
    assert "stand_1" not in note_ids
    assert QS is not None


def test_a_booking_confirmed_win_is_in_neither_count():
    """2026-08-19. The sibling heal stopped stamping booking-confirmed WINs
    (it was manufacturing phantom "OL quoted today" rows), which un-dated
    eight Yokohama rows at once. Both the banner and QC-077 must ignore them:
    a booked row is a closed outcome with no OL send time left to chase.

    The fixture carries a REAL rate, so quote_evidence_is_booking_derived is
    False — this row is excluded by is_confirmed_win alone, which is what
    makes it a genuine guard on the new clause rather than a restatement of
    the old one."""
    won = _row(request_id="req_yoko", status="WIN", mdolx_ref="261026",
               ol_rate=3176.0, carrier_quoted="CMA CGM")
    assert core.quote_evidence_is_booking_derived(won) is False
    assert core.is_undated_quote(won) is False
    assert GE.undated_quotes({"requests": [won]}) == []


def test_a_plain_undated_quote_is_still_counted():
    """The exclusions must not swallow the thing the check exists for."""
    plain = _row(request_id="req_plain", ol_rate=570.0, status="LOSS")
    assert core.is_undated_quote(plain) is True
    assert [r["request_id"] for r in GE.undated_quotes({"requests": [plain]})] \
        == ["req_plain"]


def test_a_win_without_a_booking_ref_is_still_counted():
    """is_confirmed_win requires the MDOLX ref, not merely WIN status. A row
    that flipped to WIN on a send-signal has no booking behind it and its
    missing quote time is still a real gap — QC-049 has said so since May."""
    unconfirmed = _row(request_id="req_sendonly", status="WIN", ol_rate=570.0)
    assert core.is_confirmed_win(unconfirmed) is False
    assert core.is_undated_quote(unconfirmed) is True


# ─────────────────────────────────────────────────────────────────────
# QC-077's ADVICE has to match the bucket it is describing.
#
# 2026-08-13, after the shared mailbox and the booking-derived fix took the
# count 22 -> 7, every survivor landed in one bucket: "links to a cached
# message that carries no send time or could not be classified". That is the
# ask-only bucket — the row's single linked message is Lonny's own RFQ, and
# core.quote_evidence_ok refuses to stamp a quote time from it because doing
# so manufactured the resp==req same-day quotes behind the W31/W32 phantom
# Q&L run.
#
# The message nonetheless told the reader to "Re-pull with --days-back N to
# widen the cache". Those messages are already cached. The advice sent the
# reader to do work that cannot help and implied the data was recoverable.
# ─────────────────────────────────────────────────────────────────────

def _fire_qc077(rows, bodies=None):
    """Fire phase 6 with a body cache we control.

    _undated_reason reads the REAL scripts/stage_emails_bodies.txt, which does
    not exist in the test environment — so without this every row lands in the
    "no_body" bucket and the ask-only branch can never be exercised. Patching
    the loader is what lets the two advice branches be told apart.
    """
    import pytest as _pytest
    import qc_selfheal as QS
    mp = _pytest.MonkeyPatch()
    try:
        mp.setattr(QS, "_load_bodies_index", lambda: dict(bodies or {}))
        log = QS.Log()
        QS.phase_6_rules(log, {"requests": rows})
    finally:
        mp.undo()
    return [e for e in log.warnings if "QC-077" in e]


def _undated(rid, **over):
    r = {"request_id": rid, "status": "LOSS", "quoted": True,
         "lane": "Oakland → Algeciras", "origin": "Oakland",
         "destination": "Algeciras", "ol_rate": 4938.0,
         "carrier_quoted": "CMA CGM", "response_timestamp": None,
         "request_timestamp": _RECENT,
         "source_imids": ["<ask@namprd22>"]}
    r.update(over)
    return r


#: The ask itself: cached, has a send time, but Lonny sent it — so
#: core.quote_evidence_ok refuses to read a QUOTE time off it.
_ASK_CACHED = {"<ask@namprd22>": {
    "imid": "<ask@namprd22>",
    "sender_email": "lupfold@hilmaringredients.com",
    "sent_ts": "2026-06-20T12:00:00Z",
    "text_body": "Please quote Oakland to Algeciras, 1x40HC.",
}}


def test_the_advice_does_not_send_the_reader_to_re_pull_a_cache_that_is_full():
    msgs = _fire_qc077([_undated(f"req_{i}") for i in range(7)],
                       bodies=_ASK_CACHED)
    assert msgs, "QC-077 did not fire"
    m = msgs[0]
    assert "will not shrink it" in m, m[:500]
    assert "fabricates turnaround" in m
    assert "--days-back" not in m, (
        "still telling the reader to widen a cache that already holds the "
        "linked message")


def test_a_real_cache_gap_still_gets_the_re_pull_advice():
    """The advice must stay correct for the bucket it WAS written for: a row
    whose linked message is genuinely no longer cached."""
    msgs = _fire_qc077([_undated("req_gone",
                                 source_imids=["<evicted@ol-usa.com>"])],
                       bodies={})
    assert msgs
    assert "--days-back" in msgs[0], msgs[0][:500]
    assert "will not shrink it" not in msgs[0]
