"""One Send, one shipment — scripts/ingest.apply_send_signals.

THE DEFECT (operator-reported 2026-08-27, Oakland -> Tokyo)

Lonny asked for one move on two days (2026-08-25 and 2026-08-26): identical
equipment, TEU, carrier (Wan Hai) and rate ($2,884). `core.request_id` keys on
the message's OWN internetMessageId, so two emails about one move are two rows
by construction, and neither dedupe can see it — `_merge_thread_dupes` buckets
on `request_date` and qc_selfheal's content dedupe on `(conversation_id,
destination, request_date, containers)`, and 08-25 != 08-26 defeats both.

OL booked the move once, on MDOLX261145, against the 08-26 row.
`link_bookings_to_requests` runs BEFORE `apply_send_signals`, and the send
matcher then:

  * SKIPPED every row already WIN (so the row the Send actually belongs to was
    invisible to it), and
  * took the latest remaining same-lane row within 7 days,

so Lonny's single "Send" — already spent on MDOLX261145 — cascaded onto the
08-25 row, promoted it to WIN, and `age_requests` later demoted it to
LOSS/SEND_NO_BOOKING: an invented loss on a shipment that shipped, and an
"OL never confirmed the booking" accusation about a booking OL confirmed.

THE FIX: a Send is thread evidence, spent once. The reply's own In-Reply-To /
References / conversation_id (staged for every message by
refresh_stage.build_stage_record) anchor it; a booked row is an eligible
target, not a skipped one; and each row absorbs at most one Send per fire so
two GENUINE sends still promote two rows.

Every test here drives the production functions in `scripts/ingest.py`, in the
order `main()` runs them.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

UTC = timezone.utc


@pytest.fixture(scope="module")
def ing():
    import ingest  # scripts/ingest.py — production
    return ingest


# ─────────────────────────────────────────────────────────────────────
# Builders — the stage-row shape refresh_stage.build_stage_record writes
# ─────────────────────────────────────────────────────────────────────

def _rfq(ing, *, imid, sent, subject="Oakland to Tokyo",
         preview="2x40HC", conv=None, in_reply_to=None, references=None):
    return {
        "imid": imid,
        "id": imid,
        "bucket": "lonny_outbound",
        "sent": sent,
        "subject": subject,
        "summary_preview": preview,
        "in_reply_to": in_reply_to,
        "references": references or [],
        "conversation_id": conv,
        "body_parsed": {},
    }


def _send_reply(*, imid, sent, subject="RE: Oakland to Tokyo",
                conv=None, in_reply_to=None, references=None):
    return {
        "imid": imid,
        "id": imid,
        "bucket": "lonny_reply",
        "sent": sent,
        "subject": subject,
        "summary_preview": "Send",
        "in_reply_to": in_reply_to,
        "references": references or [],
        "conversation_id": conv,
        "body_parsed": {"send_signal": True},
    }


def _booking(*, mdolx, sent, subject, in_reply_to=None, references=None):
    return {
        "mdolx": mdolx,
        "subject": subject,
        "sent": sent,
        "preview": "",
        "source_bucket": "mbd_inbound",
        "source_imid": f"<bk-{mdolx}>",
        "source_id": f"<bk-{mdolx}>",
        "body_signer": None,
        "body_parsed": {},
        "in_reply_to": in_reply_to,
        "references": references or [],
    }


def _by_date(requests):
    return {r["request_date"]: r for r in requests}


# ─────────────────────────────────────────────────────────────────────
# 1. The reported defect
# ─────────────────────────────────────────────────────────────────────

def test_send_consumed_by_its_booking_does_not_cascade_to_the_sibling_ask(ing):
    """The Oakland -> Tokyo pair. FAILS on the pre-2026-08-27 matcher, which
    promoted the 08-25 row on a Send that MDOLX261145 had already consumed."""
    rows = [
        _rfq(ing, imid="<rfq-0825@hilmar>", sent="2026-08-25T16:10:00Z"),
        _rfq(ing, imid="<rfq-0826@hilmar>", sent="2026-08-26T15:40:00Z"),
    ]
    requests = ing.build_requests(rows)
    assert len(requests) == 2, "two emails, two rows — the merge cannot see this"

    bookings = {
        "261145": _booking(mdolx="261145", sent="2026-08-26T23:05:00Z",
                           subject="MDOLX261145_ NEW BOOKING CONFIRMATION // "
                                   "WAN HAI - Oakland to Tokyo - 2X40'HC"),
    }
    requests, standalones = ing.link_bookings_to_requests(requests, bookings)
    assert not standalones

    promos = ing.apply_send_signals(
        requests, [_send_reply(imid="<send-1@hilmar>", sent="2026-08-26T20:00:00Z")])

    rows_by_date = _by_date(requests)
    won = rows_by_date["2026-08-26"]
    twin = rows_by_date["2026-08-25"]

    assert won["status"] == "WIN" and won["mdolx_ref"] == "261145"
    assert twin["status"] != "WIN", (
        "the sibling ask absorbed a Send that was already spent on MDOLX261145")
    assert not twin.get("has_send"), (
        "has_send on the twin is what ages it to LOSS/SEND_NO_BOOKING")
    assert promos == 0, "the Send was consumed by the booking, not promoted"

    # And it must never become the phantom loss the operator saw.
    ing.age_requests(requests, now=datetime(2026, 8, 31, 13, 0, tzinfo=UTC))
    assert twin.get("loss_reason") != "SEND_NO_BOOKING"
    assert rows_by_date["2026-08-26"]["status"] == "WIN"


def test_the_booked_row_records_the_send_that_bought_it(ing):
    """Consumption is evidence, not a silent drop: the reply's imid lands on
    the row it settled, so the audit can see where the Send went."""
    requests = ing.build_requests([
        _rfq(ing, imid="<rfq-0826@hilmar>", sent="2026-08-26T15:40:00Z")])
    requests, _ = ing.link_bookings_to_requests(requests, {
        "261145": _booking(mdolx="261145", sent="2026-08-26T23:05:00Z",
                           subject="MDOLX261145_ NEW BOOKING CONFIRMATION // "
                                   "WAN HAI - Oakland to Tokyo - 2X40'HC")})
    ing.apply_send_signals(
        requests, [_send_reply(imid="<send-1@hilmar>", sent="2026-08-26T20:00:00Z")])
    assert "<send-1@hilmar>" in requests[0]["source_imids"]
    assert requests[0].get("_send_match_via")


# ─────────────────────────────────────────────────────────────────────
# 2. Thread anchoring — the Send lands on ITS ask, not the newest one
# ─────────────────────────────────────────────────────────────────────

def test_thread_anchored_send_beats_recency(ing):
    """Two genuinely open asks; the Send replies to the OLDER one's thread.

    Pre-fix this promoted the newest row on the lane regardless of what the
    reply was actually answering — the same wrong-row promotion as the
    reported defect, with no booking involved.
    """
    requests = ing.build_requests([
        _rfq(ing, imid="<rfq-A@hilmar>", sent="2026-08-24T16:00:00Z",
             preview="2x40HC", conv="CONV-A"),
        _rfq(ing, imid="<rfq-B@hilmar>", sent="2026-08-26T16:00:00Z",
             preview="1x20DV", conv="CONV-B"),
    ])
    promos = ing.apply_send_signals(requests, [
        _send_reply(imid="<send-A@hilmar>", sent="2026-08-26T18:00:00Z",
                    conv="CONV-A", in_reply_to="<rfq-A@hilmar>",
                    references=["<rfq-A@hilmar>"]),
    ])
    by_date = _by_date(requests)
    assert promos == 1
    assert by_date["2026-08-24"]["status"] == "WIN", "the Send named this thread"
    assert by_date["2026-08-26"]["status"] != "WIN", (
        "a newer unrelated ask must not absorb another thread's acceptance")
    assert by_date["2026-08-24"]["_send_match_via"] == "thread"


def test_conversation_id_alone_anchors_when_headers_are_missing(ing):
    """Older stage records carry conversation_id but no In-Reply-To chain."""
    requests = ing.build_requests([
        _rfq(ing, imid="<rfq-A@hilmar>", sent="2026-08-24T16:00:00Z", conv="CONV-A"),
        _rfq(ing, imid="<rfq-B@hilmar>", sent="2026-08-26T16:00:00Z", conv="CONV-B"),
    ])
    ing.apply_send_signals(requests, [
        _send_reply(imid="<send-A@hilmar>", sent="2026-08-26T18:00:00Z", conv="CONV-A")])
    by_date = _by_date(requests)
    assert by_date["2026-08-24"]["status"] == "WIN"
    assert by_date["2026-08-26"]["status"] != "WIN"


# ─────────────────────────────────────────────────────────────────────
# 3. The over-merge guards — REAL second asks still win
# ─────────────────────────────────────────────────────────────────────

def test_two_genuine_sends_on_one_lane_still_promote_two_rows(ing):
    """Lonny really does accept twice on a lane. One Send is consumed by the
    booking; the second must still promote the open ask."""
    requests = ing.build_requests([
        _rfq(ing, imid="<rfq-0825@hilmar>", sent="2026-08-25T16:10:00Z"),
        _rfq(ing, imid="<rfq-0826@hilmar>", sent="2026-08-26T15:40:00Z"),
    ])
    requests, _ = ing.link_bookings_to_requests(requests, {
        "261145": _booking(mdolx="261145", sent="2026-08-26T23:05:00Z",
                           subject="MDOLX261145_ NEW BOOKING CONFIRMATION // "
                                   "WAN HAI - Oakland to Tokyo - 2X40'HC")})
    promos = ing.apply_send_signals(requests, [
        _send_reply(imid="<send-1@hilmar>", sent="2026-08-26T20:00:00Z"),
        _send_reply(imid="<send-2@hilmar>", sent="2026-08-26T21:30:00Z"),
    ])
    by_date = _by_date(requests)
    assert promos == 1
    assert by_date["2026-08-26"]["status"] == "WIN"
    assert by_date["2026-08-25"]["status"] == "WIN", (
        "a second, distinct acceptance must still land — consumption is "
        "per-Send, never a lane-wide refusal")
    assert by_date["2026-08-25"]["has_send"] is True


def test_send_never_crosses_terminals(ing):
    """core.same_port still governs: a Manila (South) Send cannot touch the
    booked Manila (North) row, nor promote it, nor be consumed by it."""
    requests = ing.build_requests([
        _rfq(ing, imid="<rfq-N@hilmar>", sent="2026-08-25T16:00:00Z",
             subject="Oakland to Manila (North)"),
        _rfq(ing, imid="<rfq-S@hilmar>", sent="2026-08-24T16:00:00Z",
             subject="Oakland to Manila (South)"),
    ])
    requests, _ = ing.link_bookings_to_requests(requests, {
        "261200": _booking(mdolx="261200", sent="2026-08-26T01:00:00Z",
                           subject="MDOLX261200_ NEW BOOKING CONFIRMATION // "
                                   "Oakland to Manila (North) - 2X40'HC")})
    north = [r for r in requests if "North" in r["destination"]][0]
    south = [r for r in requests if "South" in r["destination"]][0]
    assert north["mdolx_ref"] == "261200"

    ing.apply_send_signals(requests, [
        _send_reply(imid="<send-S@hilmar>", sent="2026-08-25T18:00:00Z",
                    subject="RE: Oakland to Manila (South)")])
    assert south["status"] == "WIN", "the South Send belongs to the South row"
    assert south.get("mdolx_ref") is None
    assert north["mdolx_ref"] == "261200"


def test_send_outside_the_7_day_window_still_matches_nothing(ing):
    requests = ing.build_requests([
        _rfq(ing, imid="<rfq-old@hilmar>", sent="2026-08-10T16:00:00Z")])
    assert ing.apply_send_signals(requests, [
        _send_reply(imid="<send-late@hilmar>", sent="2026-08-26T18:00:00Z")]) == 0
    assert requests[0]["status"] != "WIN"


def test_matching_is_independent_of_reply_order(ing):
    """Deterministic by construction — stage-file order must not decide which
    row a Send lands on (the 2026-07-27 lesson, in the send matcher)."""
    def run(order):
        requests = ing.build_requests([
            _rfq(ing, imid="<rfq-0825@hilmar>", sent="2026-08-25T16:10:00Z"),
            _rfq(ing, imid="<rfq-0826@hilmar>", sent="2026-08-26T15:40:00Z"),
        ])
        requests, _ = ing.link_bookings_to_requests(requests, {
            "261145": _booking(mdolx="261145", sent="2026-08-26T23:05:00Z",
                               subject="MDOLX261145_ NEW BOOKING CONFIRMATION // "
                                       "WAN HAI - Oakland to Tokyo - 2X40'HC")})
        ing.apply_send_signals(requests, order)
        return {r["request_date"]: r["status"] for r in requests}

    early = _send_reply(imid="<send-1@hilmar>", sent="2026-08-26T20:00:00Z")
    late = _send_reply(imid="<send-2@hilmar>", sent="2026-08-26T21:30:00Z")
    assert run([early, late]) == run([late, early])


# ─────────────────────────────────────────────────────────────────────
# 4. The paired library tree — same mechanism, or it is drift
# ─────────────────────────────────────────────────────────────────────

def test_library_tree_also_refuses_the_cascade():
    """src/hilmar/ingest.py is a CLAUDE.md-paired file. Its send matcher had
    the same shape (skip MDOLX rows, take the latest on the lane), so the same
    Send cascaded there too — with a 5-day window instead of 7."""
    from hilmar import ingest as HI

    twin = {
        "request_id": "req_0825", "destination": "Tokyo", "origin": "Oakland",
        "lane": "Oakland → Tokyo", "request_timestamp": "2026-08-25T16:10:00Z",
        "status": "PENDING", "quoted": True, "carrier_quoted": "WAN HAI",
        "teu_requested": 2,
    }
    won = {
        "request_id": "req_0826", "destination": "Tokyo", "origin": "Oakland",
        "lane": "Oakland → Tokyo", "request_timestamp": "2026-08-26T15:40:00Z",
        "status": "WIN", "quoted": True, "carrier_quoted": "WAN HAI",
        "mdolx_ref": "261145", "booking_timestamp": "2026-08-26T23:05:00Z",
        "has_send": True, "teu_requested": 2,
    }
    promos = HI.apply_send_signals([twin, won], [
        {"imid": "<send-1@hilmar>", "sent": "2026-08-26T20:00:00Z",
         "subject": "RE: Oakland to Tokyo", "body_parsed": {"send_signal": True}},
    ])
    assert promos == 0
    assert not twin.get("has_send"), (
        "the library tree must not promote the sibling ask either")


def test_library_tree_thread_anchor_beats_recency():
    from hilmar import ingest as HI

    older = {"request_id": "A", "destination": "Tokyo", "origin": "Oakland",
             "request_timestamp": "2026-08-24T16:00:00Z", "status": "PENDING",
             "conversation_id": "CONV-A", "source_imids": ["<rfq-A@hilmar>"],
             "teu_requested": 2}
    newer = {"request_id": "B", "destination": "Tokyo", "origin": "Oakland",
             "request_timestamp": "2026-08-26T16:00:00Z", "status": "PENDING",
             "conversation_id": "CONV-B", "source_imids": ["<rfq-B@hilmar>"],
             "teu_requested": 1}
    HI.apply_send_signals([older, newer], [
        {"imid": "<send-A@hilmar>", "sent": "2026-08-26T18:00:00Z",
         "subject": "RE: Oakland to Tokyo", "in_reply_to": "<rfq-A@hilmar>",
         "references": ["<rfq-A@hilmar>"], "conversation_id": "CONV-A",
         "body_parsed": {"send_signal": True}},
    ])
    assert older.get("has_send") is True
    assert not newer.get("has_send")


# ── ARRIVED WITH ITS CAUSE ─────────────────────────────────────────────────
#
# This test was written alongside the UN/LOCODE merge and HELD BACK from it,
# because the behaviour it pins has nothing to do with LOCODEs: it guards
# _prior_win_captured against canonical_port_key's "unknown" sentinel, and
# that sentinel only reaches the comparison once BOTH sides route through the
# alias-aware key — which is a change in THIS branch, not that one.
#
# The old bare .lower() produced "" and got this right by accident, because an
# empty string is falsy. "unknown" is not. Two rows that each failed to
# resolve a lane would match each other as ("unknown", date) and DROP a prior
# WIN — the exact opposite of what the carry-forward is for.


def test_two_lane_less_rows_are_not_evidence_of_each_other(ing):
    """canonical_port_key returns the sentinel "unknown" for a MISSING
    destination, not a place. The old bare `.lower()` produced "" and got this
    right by accident (falsy); the alias-aware key must say it out loud, or a
    prior WIN with no resolved lane is DROPPED instead of preserved — the exact
    opposite of what the carry-forward exists to do."""
    import core
    key = core.canonical_port_key
    assert ing._prior_win_captured(
        None, [], set(), key(""), "2026-08-20",
        {("unknown", "2026-08-20")}) is False
    assert ing._prior_win_captured(
        None, [], set(), key("Unknown"), "2026-08-20",
        {("unknown", "2026-08-20")}) is False
    # And a REAL lane must still match, or the guard has broken the feature
    # it is protecting.
    assert ing._prior_win_captured(
        None, [], set(), key("Yokohama"), "2026-08-20",
        {("yokohama", "2026-08-20")}) is True
