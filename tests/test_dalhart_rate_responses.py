"""The Dalhart blind spot — 2026-06-11 live failure.

Four real OL quotes ("Re: Dalhart to Caucedo" / "Re: Dalhart to Hamburg",
from the MBD shared mailbox, with full rate tables) were staged as
mbd_inbound because the rate-response classifier was literally
"re: oakland to" — so the client email reported all four RFQs as Not
Quoted. Pinned here:

  - the rate-response pattern is BUILT from body_parser.KNOWN_ORIGINS
    (one list to extend for the next plant, never another hardcode)
  - refresh_stage buckets a Dalhart reply as mbd_rate_response
  - ingest RE-DERIVES rate responses from already-staged mbd_inbound rows,
    so history is honored without a stage-file migration
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from hilmar import body_parser as HBP  # noqa: E402
from hilmar import ingest as HI  # noqa: E402


def test_rate_response_rx_built_from_origin_list():
    rx = HBP.RATE_RESPONSE_SUBJECT_RX
    # every known origin site must produce a match in reply form
    for origin in HBP.KNOWN_ORIGINS:
        assert rx.match(f"Re: {origin} to Somewhere"), origin
    # the four real misses from 2026-06-11
    assert rx.match("Re: Dalhart to Caucedo")
    assert rx.match("Re: Dalhart to Hamburg")
    # non-lane MBD traffic must NOT be promoted
    assert not rx.match("MDOLX260432_ BOOKING CONFIRMATION// HILMAR")
    assert not rx.match("Re: Free time extension to carrier")
    assert not rx.match("Re: Booking schedule inconsistency")


def test_src_classifier_accepts_dalhart_lanes():
    assert HI.DEST_RX.match("Dalhart to Caucedo")
    assert HI.is_hilmar_subject("Re: Dalhart to Hamburg")
    assert not HI.DEST_RX.match("Random subject to nowhere")


def test_ingest_rederives_misfiled_rate_responses():
    # The exact shape of the staged 2026-06-11 misses: bucket says
    # mbd_inbound (stamped by the Oakland-locked classifier), subject says
    # rate response.
    misfiled = {"bucket": "mbd_inbound", "subject": "Re: Dalhart to Caucedo"}
    proper = {"bucket": "mbd_rate_response", "subject": "Re: Oakland to Busan"}
    booking = {"bucket": "mbd_inbound", "subject": "MDOLX260432_ BOOKING CONFIRMATION// HILMAR"}
    lonny = {"bucket": "lonny_reply", "subject": "Re: Dalhart to Caucedo"}

    assert HI.counts_as_rate_response(misfiled) is True
    assert HI.counts_as_rate_response(proper) is True
    assert HI.counts_as_rate_response(booking) is False
    # a Lonny reply quoting the same subject is NOT an OL rate response
    assert HI.counts_as_rate_response(lonny) is False


def test_scripts_tree_mirrors_the_fix():
    import body_parser as SBP
    import ingest as SI
    import refresh_stage as rs

    assert SBP.RATE_RESPONSE_SUBJECT_RX.match("Re: Dalhart to Caucedo")
    assert rs.RATE_RESPONSE_SUBJECT is SBP.RATE_RESPONSE_SUBJECT_RX
    assert SI.counts_as_rate_response(
        {"bucket": "mbd_inbound", "subject": "Re: Dalhart to Hamburg"})
    assert not SI.counts_as_rate_response(
        {"bucket": "mbd_inbound", "subject": "MDOLX260432_ BOOKING CONFIRMATION"})
