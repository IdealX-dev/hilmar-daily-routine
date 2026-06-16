"""Lonny's real RFQ format — 2026-06-16 blank-fields failure.

Michael forwarded an actual Lonny RFQ and said "your parser is bad ... that's
why the fields are blank":

    Subject: Oakland to HCMC
    4-40' HC's
    Oakland to HCMC
    ETA 8/7
    Product Protein
    14 days demurrage requested let me know what you have

Two confirmed gaps, both pinned here across BOTH parser trees:
  1. requested ETA: Lonny writes a BARE "ETA 8/7" — _ETA_REQ_ANCHORS had no
     bare "eta" anchor, so eta_requested was blank on his quotes.
  2. relative asks: "ETD next week" / "next Monday" / "end of month" resolve
     against the email's SEND date (Michael: "sometime he says for etd next
     week").
(Containers — "4-40' HC's" — already parse via core.parse_teu on the body
preview; locked here so a regex change can't silently break them.)
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import body_parser as SBP  # noqa: E402  (production tree)
import core as score  # noqa: E402
import fetch_bodies as FB  # noqa: E402

from hilmar import body_parser as HBP  # noqa: E402  (test/accuracy tree)

_BOTH = (SBP, HBP)

# Lonny's actual email, verbatim.
LONNY_BODY = (
    "4-40' HC's\n\n"
    "Oakland to HCMC\n\n"
    "ETA 8/7\n\n"
    "Product Protein\n\n"
    "14 days demurrage requested let me know what you have\n\n"
    "Thanks,\nLonny Upfold\nLogistics Coordinator"
)
# Monday June 15, 2026 — the email's send date, for relative-date anchoring.
REF = date(2026, 6, 15)


@pytest.mark.parametrize("BP", _BOTH, ids=["scripts", "hilmar"])
def test_bare_eta_requested_parses(BP):
    # "ETA 8/7" is Lonny's requested arrival — was blank pre-fix.
    assert BP.parse_eta_requested("ETA 8/7") == "2026-08-07"
    assert BP.parse_eta_requested(LONNY_BODY) == "2026-08-07"


@pytest.mark.parametrize("BP", _BOTH, ids=["scripts", "hilmar"])
def test_product_still_parses(BP):
    assert BP.parse_product(LONNY_BODY) == "Protein"


def test_containers_parse_from_body_preview():
    # "4-40' HC's" -> 4 containers, 8 TEU (the hyphen form Lonny uses).
    import ingest as I
    assert score.parse_teu("4-40' HC's") == (4, 8)
    count, teu, canonical = I.guess_teu_from_preview("4-40' HC's Oakland to HCMC")
    assert (count, teu) == (4, 8)
    assert canonical and canonical.startswith("4-40'")


@pytest.mark.parametrize("BP", _BOTH, ids=["scripts", "hilmar"])
@pytest.mark.parametrize("text,expected", [
    ("ETD next week", "2026-06-22"),          # Monday of the following week
    ("ETD end of month", "2026-06-30"),
    ("need to ship next Monday", "2026-06-22"),
    ("arrival next Friday", "2026-06-26"),
])
def test_relative_dates_resolve_against_send_date(BP, text, expected):
    # ETA-side phrasings via parse_eta_requested, ETD-side via parse_etd_requested;
    # both accept ref_date. Use whichever anchor the phrase carries.
    got = (BP.parse_etd_requested(text, ref_date=REF)
           or BP.parse_eta_requested(text, ref_date=REF))
    assert got == expected


@pytest.mark.parametrize("BP", _BOTH, ids=["scripts", "hilmar"])
def test_relative_dates_need_a_reference(BP):
    # Without the send date there is nothing to anchor to → None (not a guess).
    assert BP.parse_etd_requested("ETD next week") is None


@pytest.mark.parametrize("BP", _BOTH, ids=["scripts", "hilmar"])
def test_absolute_date_still_wins_over_relative(BP):
    assert BP.parse_etd_requested("ship by 7/1 next week", ref_date=REF) == "2026-07-01"


@pytest.mark.parametrize("BP", _BOTH, ids=["scripts", "hilmar"])
@pytest.mark.parametrize("text,expected", [
    ("14 days demurrage requested", "14d demurrage"),
    ("10 days detention please", "10d detention"),
    ("7 days free time", "7d free time"),
])
def test_free_time_requested(BP, text, expected):
    assert BP.parse_free_time_requested(text) == expected


def test_free_time_none_when_absent():
    assert SBP.parse_free_time_requested("Oakland to HCMC 2-40' HC") is None


def test_fetch_bodies_captures_free_time_for_lonny():
    p = FB._parse_all(LONNY_BODY, "Oakland to HCMC", "lonny_outbound",
                      sent_ts="2026-06-15T20:42:00Z")
    assert p["free_time_requested"] == "14d demurrage"


def test_fetch_bodies_threads_send_date_for_relative_asks():
    sent = "2026-06-15T20:42:00Z"   # Mon Jun 15
    p = FB._parse_all(LONNY_BODY, "Oakland to HCMC", "lonny_outbound", sent_ts=sent)
    assert p["eta_requested"] == "2026-08-07"
    rel = FB._parse_all("Oakland to HCMC\nETD next week\n2-40' HC",
                        "Oakland to HCMC", "lonny_outbound", sent_ts=sent)
    assert rel["etd_requested"] == "2026-06-22"
