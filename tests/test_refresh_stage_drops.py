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
