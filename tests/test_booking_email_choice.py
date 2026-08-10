"""A booking is represented by its CONFIRMATION, not by the first email that
happened to mention it.

2026-08-10. Michael: "read the emails.. that's your job to decide if it's a
problem with the file or a new win." I read them. Verdict: not a missing win —
both MDOLX260769 and MDOLX260797 were already WIN rows from June, and the
August messages were ETA updates. But one of the two rows was materially
wrong, and that IS a file problem:

    stand_260769   carrier_won=CMA CGM  teu_won=0  etd=22-Apr-26  eta=26-May-26
    req_5d2685f3…  carrier_won=CMA CGM  teu_won=8  etd=1-Jul-26   eta=2026-07-25

Zero TEU on a 3X40'RF (should be 6), and sailing dates two months BEFORE the
16 June booking existed. The thread explains it — two emails, three minutes
apart:

    17:14:37  MDOLX260769_ *NEED UPDATE TO BOOKING # NAM8482648 // HILMAR
    17:17:34  MDOLX260769_ *NEW BOOKING CONFIRMATION // HILMAR -
              Oakland to Osaka - 3X40'RF // CMA BKG # NAM8482648

collect_bookings kept "the earliest sighting of this MDOLX", which is the
17:14 email — OL asking CMA to CHANGE a booking, body "Can you please update
this booking per below: -Reduce to 3 x 40'RF". Its subject carries no lane and
no container spec, and the row derives lane / carrier / containers / TEU from
the SUBJECT of whichever email is chosen. So the fields came out empty and the
real confirmation, three minutes later, was thrown away.

The fix ranks candidates instead of taking the earliest, and these tests use
the real subjects from that thread. What is pinned:
  - a confirmation beats an ops message no matter which arrived first
  - among confirmations, EARLIEST still wins, so a June creation is never
    displaced by an August "UPDATED ETA" revision
  - the set of MDOLX numbers that become bookings does not change — only
    which email represents each one
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

# Verbatim from the production thread (diag-bookings run 3, 2026-08-10).
OPS_ASK = "MDOLX260769_ *NEED UPDATE TO BOOKING # NAM8482648 // HILMAR"
CONFIRMATION = ("MDOLX260769_ *NEW BOOKING CONFIRMATION // HILMAR - Oakland to "
                "Osaka - 3X40'RF // CMA BKG # NAM8482648")
UPDATED_ETA = ("RE: MDOLX260769_UPDATED ETA BOOKING CONFIRMATION// HILMAR - "
               "Oakland to Osaka - 3X40'RF // CMA BKG # NAM8482648")
DEMURRAGE = ("MDOLX260769_ *ORIGIN EXPORT DEMURRAGE INVOICE // HILMAR - "
             "Oakland to Osaka - 3X40'RF // CMA BKG # NAM8482648")


def _row(subject, sent, imid="<x>"):
    return {"bucket": "mbd_inbound", "subject": subject, "sent": sent,
            "received": sent, "imid": imid, "summary_preview": "",
            "is_hilmar": True}


def test_the_confirmation_wins_over_the_earlier_ops_message():
    """THE defect, with the real subjects and the real three-minute gap."""
    import ingest as IN
    rows = [_row(OPS_ASK, "2026-06-16T17:14:37Z", "<a>"),
            _row(CONFIRMATION, "2026-06-16T17:17:34Z", "<b>")]
    got = IN.collect_bookings(rows)
    assert "260769" in got
    assert got["260769"]["subject"] == CONFIRMATION, (
        "the booking is still represented by the ops message — lane, carrier, "
        "containers and TEU will all parse from the wrong subject")


def test_order_of_arrival_in_the_stage_file_does_not_matter():
    """Stage order is not chronological — refresh_stage appends as Graph
    returns, and a re-fetch can reverse two same-minute messages."""
    import ingest as IN
    rows = [_row(CONFIRMATION, "2026-06-16T17:17:34Z", "<b>"),
            _row(OPS_ASK, "2026-06-16T17:14:37Z", "<a>")]
    assert IN.collect_bookings(rows)["260769"]["subject"] == CONFIRMATION


def test_the_original_confirmation_beats_a_later_revision():
    """The half that must NOT regress. An August "UPDATED ETA BOOKING
    CONFIRMATION" is still a confirmation; if it displaced the June original,
    the win would re-date to August and the June week would lose a booking —
    trading this bug for a worse one."""
    import ingest as IN
    rows = [_row(CONFIRMATION, "2026-06-16T17:17:34Z", "<b>"),
            _row(UPDATED_ETA, "2026-08-05T14:39:53Z", "<c>")]
    got = IN.collect_bookings(rows)["260769"]
    assert got["subject"] == CONFIRMATION
    assert got["sent"] == "2026-06-16T17:17:34Z", (
        "the booking re-dated to the revision — its win would move weeks")


def test_a_revision_is_still_better_than_an_ops_message():
    """When no NEW confirmation was ever staged — the thread starts mid-life,
    which is normal for a backfill — a revision still carries the lane and
    container spec an invoice does not."""
    import ingest as IN
    rows = [_row(DEMURRAGE, "2026-07-06T14:43:08Z", "<d>"),
            _row(UPDATED_ETA, "2026-08-05T14:39:53Z", "<c>")]
    assert IN.collect_bookings(rows)["260769"]["subject"] == UPDATED_ETA


def test_the_set_of_bookings_is_unchanged():
    """Blast radius. This changes WHICH email represents a booking, never
    WHETHER an MDOLX becomes one — the gates above are untouched."""
    import ingest as IN
    rows = [_row(OPS_ASK, "2026-06-16T17:14:37Z", "<a>"),
            _row(CONFIRMATION, "2026-06-16T17:17:34Z", "<b>"),
            _row(DEMURRAGE, "2026-07-06T14:43:08Z", "<d>"),
            _row("MDOLX260797_NEW BOOKING CONFIRMATION// HILMAR 4X40'RF "
                 "Oakland to Osaka// CMA: NAM8526263", "2026-06-18T21:45:52Z", "<e>")]
    got = IN.collect_bookings(rows)
    assert sorted(got) == ["260769", "260797"]


def test_the_chosen_subject_actually_carries_the_container_spec():
    """Why the choice matters, asserted end-to-end rather than by inspection:
    the row's TEU is parsed from the chosen subject. 3X40'RF is 6 TEU; the ops
    message yields nothing, which is exactly the 0 that shipped."""
    import body_parser as BP
    import core as C
    import ingest as IN

    rows = [_row(OPS_ASK, "2026-06-16T17:14:37Z", "<a>"),
            _row(CONFIRMATION, "2026-06-16T17:17:34Z", "<b>")]
    chosen = IN.collect_bookings(rows)["260769"]["subject"]

    containers = BP.parse_subject_containers(chosen)
    count, teu = C.parse_teu(containers) if containers else (0, 0)
    assert teu > 0, f"the chosen subject yields no TEU: {chosen!r}"
    assert teu == 6, f"3X40'RF should be 6 TEU, parsed {teu}"

    # and the ops message, for contrast — this is what shipped as teu_won=0
    ops_containers = BP.parse_subject_containers(OPS_ASK)
    ops_teu = C.parse_teu(ops_containers)[1] if ops_containers else 0
    assert ops_teu == 0, (
        "the ops message now yields TEU — the contrast this test rests on is "
        "gone, so re-check whether the ranking is still needed")


def test_rank_is_a_pure_function_of_subject_and_time():
    """No hidden state — it is called twice per row in a hot loop."""
    import ingest as IN
    a = IN._booking_rank(CONFIRMATION, "2026-06-16T17:17:34Z")
    b = IN._booking_rank(CONFIRMATION, "2026-06-16T17:17:34Z")
    assert a == b
    assert IN._booking_rank(None, None) == IN._booking_rank(None, None)
    # missing timestamps must not raise or outrank a real confirmation
    assert IN._booking_rank(CONFIRMATION, None) > IN._booking_rank(OPS_ASK, None)
