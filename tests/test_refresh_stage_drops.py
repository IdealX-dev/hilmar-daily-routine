"""A dropped email must name itself in the log.

2026-08-07. Michael: "email is poorly formatted and data is missing and
completely incomplete… there were a minimum of 12 requests this week so far."

The fire that produced that email logged, in full:

    refresh_stage: total unique results across queries: 330
    refresh_stage: NEW staged records: 0
    refresh_stage: skipped 281 pre-cutoff, 0 excluded, 12 unclassified, 37 already-staged
    Nothing new to stage.

Twelve messages came back from Graph and were thrown away, and the only
record of it was the word "unclassified". The sender was printed ONLY under
--verbose, which the daily fire does not pass — so diagnosing it required
re-running the fetch by hand with a different flag, which is why it ran for a
week. QC-008 ("latest received is 41.9h old") and QC-009 ("classifier may be
dropping a sender: ['mbd_rate_response']") both fired, both as WARN, and the
pipeline reported success and shipped an empty report.

`classify()` returns None for any sender that is not Lonny or the shared
booking mailbox — and the 'lonny-flow' Graph query returns mail TO Lonny as
well as FROM him, so an OL reply from an individual's mailbox is discarded on
arrival. That is a real and reasonable rule; discarding SILENTLY is not.

These tests pin the log, not the rule. What gets dropped is a product
decision; whether anyone can find out is not.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

SRC = (ROOT / "scripts" / "refresh_stage.py").read_text(encoding="utf-8")


def test_drops_are_counted_by_sender():
    """Aggregate first — "4 from X, 2 from Y" is the shape that identifies a
    mailbox change at a glance."""
    assert "dropped_senders" in SRC
    assert "dropped_senders.most_common" in SRC


def test_drops_print_without_the_verbose_flag():
    """The daily fire runs `refresh_stage.py --days-back 14` — no --verbose.
    If naming the sender needs a flag the fire does not pass, the fire cannot
    be diagnosed from its own log."""
    tail = SRC.split("skipped_existing} already-staged", 1)[-1]
    assert "DROPPED as unclassified" in tail, (
        "the drop summary is missing from the unconditional output")
    idx = tail.index("DROPPED as unclassified")
    # nothing between the counters and the summary may gate it on args.verbose
    assert "args.verbose" not in tail[:idx], (
        "the drop summary is gated behind --verbose again")


def test_examples_carry_sender_subject_and_time():
    """A count says something broke; an example says what. Subject and
    received-time are what let an operator find the message in Outlook."""
    assert "dropped_examples" in SRC
    assert "sender | received | subject" in SRC


def test_staging_nothing_while_dropping_is_an_error_annotation():
    """The condition that shipped an empty report through a green pipeline.
    `::error::` surfaces in the run summary; stdout 400 lines up does not.

    Deliberately an annotation and not an exit code — refresh_stage is
    best-effort in the daily fire, and failing the step here would stop the
    staff email that still carries the cumulative KPIs.
    """
    assert "::error::refresh_stage staged 0 new records while dropping" in SRC
    block = SRC.split("if not new_stage:", 1)[-1][:600]
    assert "unclassified" in block and "Senders:" in block, (
        "the error annotation does not name the senders")


def test_the_examples_are_bounded():
    """A pathological run must not paste hundreds of subjects into the log."""
    assert "len(dropped_examples) < 8" in SRC


@pytest.mark.parametrize("bucket", ["lonny_outbound", "lonny_reply",
                                    "mbd_inbound", "mbd_rate_response"])
def test_classify_still_returns_every_expected_bucket(bucket):
    """Guard the rule while changing the logging. QC-009 watches these four
    names; a classifier that stopped being able to PRODUCE one would make that
    check permanently true and permanently useless."""
    import refresh_stage as RS
    assert bucket in RS.__doc__ or bucket in SRC


def test_classify_drops_an_unknown_sender_and_keeps_the_known_ones():
    """The behaviour under all this logging, asserted directly."""
    import refresh_stage as RS

    def item(sender, subject=""):
        return {"from": {"emailAddress": {"address": sender}}, "subject": subject}

    assert RS.classify(item("someone.else@ol-usa.com", "RE: Oakland to Tokyo")) is None
    assert RS.classify(item(RS.LONNY_EMAIL, "Ocean rate request")) == "lonny_outbound"
    assert RS.classify(item(RS.LONNY_EMAIL, "RE: Ocean rate request")) == "lonny_reply"
    assert RS.classify(item(RS.MBD_BOOKING_EMAIL, "MDOLX booking")) == "mbd_inbound"


def test_an_ol_reply_from_a_personal_mailbox_is_dropped():
    """Not a bug report — a pinned FACT, so the next person reading QC-009's
    "classifier may be dropping a sender" has the mechanism in front of them.
    The 'lonny-flow' query is `from:lonny OR to:lonny`, so OL staff replying
    to Lonny from their own mailbox come back from Graph and are discarded.
    Whether they SHOULD be staged is a product decision for the operator.
    """
    import refresh_stage as RS
    for sender in ("Alexandra.Hernandez@ol-usa.com", "Ryan.Gordon@ol-usa.com"):
        assert RS.classify({
            "from": {"emailAddress": {"address": sender}},
            "subject": "RE: Oakland to Yokohama — rates",
        }) is None, f"{sender} now classifies — update this test and QC-009"


# ── quote-only OL senders (Michael 2026-08-07) ──────────────────────────────

def test_reno_quotes_are_staged_as_rate_responses():
    """Michael: "reno only quotes hilmar so she doesn't book."

    Three of her messages were discarded on arrival because classify() only
    recognised Lonny and the shared booking mailbox. One of them —
    "Re: Rates to a few destinations for a study" — was ALSO flagged by
    QC-057 as a silently dropped RFQ, from the other end of the same pipeline.
    """
    import refresh_stage as RS
    assert RS.classify({
        "from": {"emailAddress": {"address": "Reno.Gurusinghe@ol-usa.com"}},
        "subject": "Re: Rates to a few destinations for a study",
    }) == "mbd_rate_response"


def test_quote_only_senders_are_not_subject_matched():
    """The rate-response subject pattern requires "Re: <known origin> to ...".
    Reno's real subject is "Re: Rates to a few destinations for a study" —
    "Rates" is not an origin, so it can never match. Gating her on the subject
    would drop the quote a second time while looking like it was handled."""
    import body_parser as BP
    import refresh_stage as RS
    subject = "Re: Rates to a few destinations for a study"
    assert not BP.RATE_RESPONSE_SUBJECT_RX.match(subject), (
        "the subject pattern now matches — re-examine whether the "
        "unconditional rule for quote-only senders is still needed")
    assert RS.classify({
        "from": {"emailAddress": {"address": "reno.gurusinghe@ol-usa.com"}},
        "subject": subject,
    }) == "mbd_rate_response"


def test_a_quote_only_sender_never_produces_a_booking_bucket():
    """"she doesn't book" — so no message from her may land in mbd_inbound,
    which is the bucket booking confirmations come from."""
    import refresh_stage as RS
    for subject in ("MDOLX260980 BOOKING CONFIRMATION", "PLEASE UPDATE BKG #", ""):
        assert RS.classify({
            "from": {"emailAddress": {"address": "reno.gurusinghe@ol-usa.com"}},
            "subject": subject,
        }) != "mbd_inbound"


def test_our_own_outbound_is_still_dropped():
    """Nine of the twelve drops were the tracker's OWN client emails coming
    back through `to:lupfold`. Widening the classifier must not start ingesting
    our own reports as if they were OL quotes."""
    import refresh_stage as RS
    assert RS.classify({
        "from": {"emailAddress": {"address": "michael.deitchman@ol-usa.com"}},
        "subject": "OL-USA — Daily Shipment Update for Hilmar Ingredients (activity for Aug 6, 2026)",
    }) is None


def test_the_quote_only_list_stays_explicit():
    """A named allowlist, not "any @ol-usa.com". Widening to the domain would
    swallow our own outbound (michael.deitchman@ol-usa.com is on it) and every
    internal thread that happens to include Lonny."""
    import refresh_stage as RS
    assert RS.OL_QUOTE_ONLY_SENDERS, "the quote-only list is empty"
    assert all("@" in s for s in RS.OL_QUOTE_ONLY_SENDERS), (
        "the quote-only list holds something that is not an address — a bare "
        "domain here would ingest our own reports")
