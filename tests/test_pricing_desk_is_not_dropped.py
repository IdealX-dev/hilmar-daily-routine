"""The OL export pricing desk's quotes were deleted on arrival.

Michael, 2026-08-12: "ol responded to everything ... they are in my mailbox
... where they always have been since day one". Both true. classify() held a
three-address whitelist and, worse, an EXCLUDED_SENDERS drop list containing
MBD_Export_Pricing@ol-usa.com and caren.tobel@ol-usa.com — the desk that
answers Lonny's rate requests, the address on the daily report's own
distribution list, and the sender used in this repo's OL-body fixtures.

Provenance of the mistake: config.json `ingest_scope.mailboxes_excluded`,
from Michael 2026-04-30 "stop searching idealx, ignore MBD_Export_Pricing".
That is an instruction about WHICH MAILBOXES TO SCAN. The code applied it as a
SENDER filter, so quotes arriving INTO the scanned mailbox were discarded.
A scope rule applied one layer off, which is the same shape as every other
defect found today.

These tests pin the distinction, because the list still exists in config for
its real purpose and the next reader will meet it again.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for p in (str(ROOT / "scripts"), str(ROOT / "src")):
    if p not in sys.path:
        sys.path.insert(0, p)

import refresh_stage as RS  # noqa: E402

PRICING = "MBD_Export_Pricing@ol-usa.com"
CAREN = "caren.tobel@ol-usa.com"


def _msg(sender: str, subject: str = "RE: Oakland to Yokohama"):
    return {"from": {"emailAddress": {"address": sender}}, "subject": subject}


def test_the_export_pricing_desk_is_staged_as_a_rate_response():
    """THE defect: every quote from this desk was dropped before it could be
    classified, so every ask it answered rendered Not Quoted."""
    assert RS.classify(_msg(PRICING)) == "mbd_rate_response", (
        "the OL export pricing desk is still being discarded — its quotes "
        "cannot reach any request, and the table will keep saying OL was "
        "silent when it was not")


def test_caren_tobel_is_staged_as_a_rate_response():
    assert RS.classify(_msg(CAREN)) == "mbd_rate_response"


def test_the_desk_is_matched_case_insensitively():
    """Graph returns whatever casing the sender used; config spells it
    MBD_Export_Pricing, Outlook may send mbd_export_pricing."""
    for spelling in (PRICING.lower(), PRICING.upper(), PRICING):
        assert RS.classify(_msg(spelling)) == "mbd_rate_response", spelling


def test_a_subject_that_is_not_a_lane_still_counts():
    """Same reasoning as Reno (OL_QUOTE_ONLY_SENDERS): these desks quote, they
    do not book, and their subjects do not follow the shared mailbox's
    'Re: <origin> to <dest>' shape. Subject-matching them drops the quote a
    second time and looks like it was handled."""
    assert RS.classify(
        _msg(PRICING, "Rates to a few destinations for a study")
    ) == "mbd_rate_response"


def test_no_ol_quote_sender_is_also_on_the_drop_list():
    """The two lists contradicting each other is exactly how this happened.
    Whatever EXCLUDED_SENDERS holds in future, it must never re-drop a sender
    the pipeline depends on for quotes."""
    dropped = {s.lower() for s in RS.EXCLUDED_SENDERS}
    quoting = {s.lower() for s in RS.OL_QUOTE_ONLY_SENDERS}
    assert not (dropped & quoting), (
        f"{dropped & quoting} is both a quote sender and a dropped sender — "
        "the drop wins in classify(), so those quotes vanish")


def test_the_fetch_side_reaches_them_too():
    """classify() can only keep what a query fetched. q3 derives from
    OL_QUOTE_ONLY_SENDERS precisely so the two ends cannot drift."""
    q3 = dict(RS.graph_queries())["ol-quote-senders"]
    for s in RS.OL_QUOTE_ONLY_SENDERS:
        assert f"from:{s}" in q3, f"{s} is kept by classify but fetched by nothing"


def test_the_config_note_keeps_mailboxes_and_senders_apart():
    """The config list stays — it has a real purpose. What must persist is the
    warning that it is about mailboxes to READ, so the next person does not
    re-apply it to senders."""
    scope = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))["ingest_scope"]
    note = scope.get("_note_mailboxes_excluded", "")
    assert "not a sender filter" in note.lower(), (
        "the mailboxes-vs-senders distinction is undocumented again")
    assert PRICING in scope["mailboxes_excluded"], (
        "the mailbox-scan exclusion was deleted; Michael's 2026-04-30 "
        "instruction still stands for its actual meaning")
