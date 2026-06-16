"""Lonny's acceptance phrasings — 2026-06-16 missed-win failure.

Michael: "why are you not showing these as wins" — a June 11 "Send Carter"
(pick the President Carter sailing) and other booking instructions never
flipped their row to WIN. Root cause: is_lonny_send_reply only matched a
bare "send" + a tiny whitelist at the very start of the first line, so
"Send Carter", "book it", "go ahead", "proceed", "please send" all returned
False. Broadened to the vocabulary Lonny actually uses while still rejecting
request-like "send me the rates".

Pinned across BOTH core trees (the production scripts/core processes live
email; hilmar/core is the test+accuracy target — they must stay identical,
see test_core_parity).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import core as scripts_core  # noqa: E402  (production tree)

from hilmar import core as hilmar_core  # noqa: E402  (test/accuracy tree)

_BOTH = (scripts_core, hilmar_core)

# Real booking instructions Lonny sends — every one must promote to WIN.
ACCEPT = [
    "SEND", "Send", "Send please", "please send",
    "Send Carter",                      # vessel selection IS acceptance
    "Send the Carter", "Send President Carter",
    "send carter\n--\nLonny",
    "Book it", "book the Carter",
    "go ahead", "go ahead and book",
    "proceed", "Confirm booking", "Accepted",
    "let's book", "let us book",
    "Yes, send it", "Sounds good, book the Carter",
    "SEND - book the Carter sailing",
]

# Look-alikes that are actually a REQUEST for info (or a non-acceptance) —
# must NOT create a false win.
REJECT = [
    "Can you send both cutoffs?",
    "send me the rates", "send the pricing", "send us the quote",
    "please send me the rate breakdown", "send over the schedule",
    "Sending shortly", "what is the rate?", "need pricing",
]


@pytest.mark.parametrize("mod", _BOTH, ids=["scripts", "hilmar"])
@pytest.mark.parametrize("body", ACCEPT)
def test_accepts_real_booking_phrasings(mod, body):
    assert mod.is_lonny_send_reply(body, is_reply=True) is True


@pytest.mark.parametrize("mod", _BOTH, ids=["scripts", "hilmar"])
@pytest.mark.parametrize("body", REJECT)
def test_rejects_requests_and_lookalikes(mod, body):
    assert mod.is_lonny_send_reply(body, is_reply=True) is False


@pytest.mark.parametrize("mod", _BOTH, ids=["scripts", "hilmar"])
def test_acceptance_still_requires_a_reply(mod):
    # A brand-new request that opens with "Send Carter" is NOT acceptance.
    assert mod.is_lonny_send_reply("Send Carter", is_reply=False) is False


def test_both_trees_agree_token_for_token():
    # Parity guard: production and src must classify identically.
    for body in ACCEPT + REJECT:
        assert (scripts_core.is_lonny_send_reply(body, is_reply=True)
                == hilmar_core.is_lonny_send_reply(body, is_reply=True)), body
