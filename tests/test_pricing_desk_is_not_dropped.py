"""OL's quotes were dropped on arrival — twice over, for two different reasons.

Michael, 2026-08-12, with the operational cause: "the team stopped copying the
group email address for bookings sent to lonny and then lonny's approvals are
not going to group but to the individual email that sent them". And the
correction to my model: "export pricig doesn't book cargo.. mbd oceanbooking
shared books cargo, they send the options and the pricing to the client
normally then lonny books".

So the options-and-pricing mail that used to arrive from
MBD_OceanExportBookingShared now arrives from whichever OL person sent it.
classify() keyed on a three-address whitelist, so those quotes were invisible
and every ask they answered aged out as Not Quoted.

Two defects, both fixed here:

  1. EXCLUDED_SENDERS held MBD_Export_Pricing@ol-usa.com and
     caren.tobel@ol-usa.com — "never stage from these senders". That list came
     from config.json `ingest_scope.mailboxes_excluded` (Michael 2026-04-30,
     "stop searching idealx, ignore MBD_Export_Pricing"), which is about WHICH
     MAILBOXES TO SCAN. Applied as a sender filter it discarded OL mail
     arriving into the mailbox we do scan. A scope rule one layer off.

  2. Identity was a roster. The fix keys on the tenant (@ol-usa.com) plus
     LONNY BEING ON THE MESSAGE — which no roster can drift out of, and which
     keeps every other OL client out, because Lonny is Hilmar's buyer and
     nobody else's. Michael is fixing the copying process at OL; the report
     must not silently depend on that process holding.
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

LONNY = RS.LONNY_EMAIL
PRICING = "MBD_Export_Pricing@ol-usa.com"
CAREN = "caren.tobel@ol-usa.com"


def _msg(sender, to=(), cc=(), subject="RE: Oakland to Yokohama"):
    return {
        "from": {"emailAddress": {"address": sender}},
        "toRecipients": [{"emailAddress": {"address": a}} for a in to],
        "ccRecipients": [{"emailAddress": {"address": a}} for a in cc],
        "subject": subject,
    }


# ── the process break Michael found ────────────────────────────────────────

def test_an_ol_individual_quoting_lonny_direct_is_staged():
    """THE break: no group address on the message. Before this, the quote was
    dropped and the ask aged out as Not Quoted while OL was answering."""
    assert RS.classify(_msg("chelou.jacobe@ol-usa.com", to=[LONNY])) == "mbd_rate_response"


def test_lonny_on_cc_counts_the_same_as_lonny_on_to():
    assert RS.classify(_msg("devon.zimmerman@ol-usa.com", to=["someone@ol-usa.com"],
                            cc=[LONNY])) == "mbd_rate_response"


def test_a_non_lane_subject_from_an_ol_individual_is_inbound_not_dropped():
    """Bookings, amendments and chatter still belong in the corpus — they are
    how MDOLX links are found. Only the BUCKET differs."""
    assert RS.classify(_msg("linda.echevarria@ol-usa.com", to=[LONNY],
                            subject="Booking confirmed MDOLX261099")) == "mbd_inbound"


def test_the_pricing_desk_is_no_longer_deleted_on_arrival():
    """Defect 1: it was on the never-stage list."""
    assert not RS.EXCLUDED_SENDERS, (
        "EXCLUDED_SENDERS is populated again — check it is not a mailbox-scan "
        "rule being applied to senders, which is what discarded OL's quotes")
    assert RS.classify(_msg(PRICING, to=[LONNY])) == "mbd_rate_response"
    assert RS.classify(_msg(CAREN, to=[LONNY])) == "mbd_rate_response"


# ── and it must not let anyone else's mail in ──────────────────────────────

def test_ol_mail_without_lonny_is_still_dropped():
    """Michael's other clients share this mailbox — 3537 messages were
    correctly dropped on the 2026-08-12 fire. Lonny's presence is the whole
    guardrail; without it this rule would sweep in Numidia/Hoogwegt/TTS."""
    assert RS.classify(_msg("devon.zimmerman@ol-usa.com",
                            to=["p.borraz@numidia.nl"],
                            subject="RE: Quote Lubbock TX to Port Klang")) is None


def test_a_non_ol_sender_with_lonny_on_it_is_not_an_ol_quote():
    """A carrier or another forwarder writing to Lonny is not OL quoting."""
    assert RS.classify(_msg("someone@maersk.com", to=[LONNY])) is None


def test_lonnys_own_mail_still_classifies_as_lonny():
    """Lonny is on every one of these messages by definition — the OL rule
    must not shadow the sender rules that run before it."""
    assert RS.classify(_msg(LONNY, to=["x@ol-usa.com"],
                            subject="Oakland to Yokohama")) == "lonny_outbound"
    assert RS.classify(_msg(LONNY, to=["x@ol-usa.com"],
                            subject="RE: Oakland to Yokohama")) == "lonny_reply"


def test_the_group_mailbox_keeps_its_existing_behaviour():
    """Nothing about the working path changes."""
    assert RS.classify(_msg(RS.MBD_BOOKING_EMAIL, to=[LONNY])) == "mbd_rate_response"


# ── the two lists must never contradict each other again ───────────────────

def test_no_ol_quote_sender_is_also_on_the_drop_list():
    dropped = {s.lower() for s in RS.EXCLUDED_SENDERS}
    quoting = {s.lower() for s in RS.OL_QUOTE_ONLY_SENDERS}
    assert not (dropped & quoting), (
        f"{dropped & quoting} is both a quote sender and a dropped sender — "
        "the drop wins in classify(), so those quotes vanish")


def test_the_config_note_keeps_mailboxes_and_senders_apart():
    """The config list stays — it has a real purpose. What must persist is the
    warning that it is about mailboxes to READ."""
    scope = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))["ingest_scope"]
    assert "not a sender filter" in scope.get("_note_mailboxes_excluded", "").lower()
    assert PRICING in scope["mailboxes_excluded"], (
        "the mailbox-scan exclusion was deleted; Michael's 2026-04-30 "
        "instruction still stands for its actual meaning")
