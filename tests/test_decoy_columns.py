"""A column the parser only half-understands must not become the carrier.

Written from an adversarial review of the 2026-08-13 rate-table rewrite,
which found a HIGH-severity regression I had already committed and pushed.

The rewrite added a token fallback so qualified headers keep mapping —
"RATE (USD)" is still the rate, "ETD (POL)" still the ETD. It mapped a
header if ANY of its word tokens matched a field alias. `operator` and
`line` are carrier aliases, so:

    POL | POD | Terminal Operator | ETD | RATE | CARRIER
    Oakland | Algeciras | SSA MARINE | 7-Sep-26 | $4938 | CMA

gave carrier_quoted = "SSA MARINE" — a terminal, not a carrier — because
the decoy sat LEFT of the real CARRIER column and first-mapped-wins. The
same over-match reached ol_rate through an "Inland Rate" column, and a
money field taking a value from the wrong column is how a client report
quotes the wrong price.

The rule now: an unrecognised word DISQUALIFIES the header. A column we
half-understand is a column we do not understand.

These are the cases the rewrite's own 35 tests did not cover — every one of
those used unambiguous headers, which is precisely the input the fallback
cannot get wrong.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for p in (str(ROOT / "scripts"), str(ROOT / "src")):
    if p not in sys.path:
        sys.path.insert(0, p)

import body_parser as SBP  # noqa: E402  production

from hilmar import body_parser as HBP  # noqa: E402  mirror

TREES = pytest.mark.parametrize("BP", [SBP, HBP], ids=["scripts", "src"])


@TREES
@pytest.mark.parametrize("header", [
    "Terminal Operator",   # a terminal is not the carrier
    "Service Line",        # a service string is not the carrier
    "Line Haul",           # an inland leg is not the carrier
    "Booking Line",
    "Inland Rate",         # NOT the ocean rate — wrong money is worse than none
    "Validity",
    "Commodity",
])
def test_a_decoy_header_maps_to_nothing(BP, header):
    assert BP._header_key(header) is None, f"{header!r} claimed a field"


@TREES
@pytest.mark.parametrize("header,want", [
    ("CARRIER", "carrier"),
    ("Ocean Carrier", "carrier"),
    ("Vessel Operator", "carrier"),   # exact alias — this one IS the carrier
    ("SSL", "carrier"),
    ("RATE", "rate"),
    ("RATE (USD)", "rate"),
    ("Ocean Rate", "rate"),
    ("ETD (POL)", "etd"),
    ("POL", "pol"),
    ("POD", "pod"),
])
def test_the_real_headers_still_map(BP, header, want):
    """The hardening must not cost recognition of what OL actually sends."""
    assert BP._header_key(header) == want


@TREES
def test_a_decoy_left_of_the_real_column_does_not_win(BP):
    """The exact table the reviewer built. First-mapped-wins made position
    decide the carrier, so the assertion is on the VALUE, not the header."""
    text = (
        "POL | POD | Terminal Operator | ETD | RATE | CARRIER\n"
        "Oakland | Algeciras | SSA MARINE | 7-Sep-26 | $4938 | CMA\n"
    )
    out = BP.parse_rate_table(text) or {}
    assert out.get("carrier_quoted") == "CMA CGM", out.get("carrier_quoted")
    assert "SSA MARINE" not in str(out.get("carrier_quoted"))


@TREES
def test_an_inland_rate_column_never_becomes_the_ocean_rate(BP):
    text = (
        "POL | POD | Inland Rate | RATE | CARRIER\n"
        "Oakland | Algeciras | 31 | $4938 | CMA\n"
    )
    out = BP.parse_rate_table(text) or {}
    assert out.get("ol_rate") == 4938.0, out.get("ol_rate")


@TREES
def test_a_real_low_rate_is_still_accepted(BP):
    """OL's HCMC quote is $475.00. A blanket "rates are >= 500" sanity floor
    would reject a genuine rate, which is why the defence is the HEADER and
    not a magic number."""
    text = ("POL | POD | RATE | CARRIER\n"
            "Oakland | HCMC (CAT LAI) | $475.00 | ONE LINE\n")
    out = BP.parse_rate_table(text) or {}
    assert out.get("ol_rate") == 475.0
    assert out.get("carrier_quoted") == "ONE"


@TREES
def test_an_ambiguous_merged_header_stays_unmapped(BP):
    """"Vessel/Voyage" names two fields; guessing which would put a voyage
    code in the vessel field."""
    assert BP._header_key("Vessel / Voyage") is None
