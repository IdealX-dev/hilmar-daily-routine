"""An incoterm is not a port, and a word containing "lax" is not Los Angeles.

Shared reference-data contract, rule 5: "3-letter IATA codes ARE the
identifier — match them, but mind POSITION. Seven incoterms are live IATA
codes: FOB Shanghai resolves FOB to Fort Bragg, CPT Hamburg to Cape Town;
12.4 CBM is Columbus. A 3-letter token after a number is a unit; an incoterm
before a place name is the incoterm."

I recorded rule 5 as "NOT ASSESSED — ocean-only, exposure likely nil" on
2026-08-30. That was wrong, and "likely" was doing the work. This repo has no
IATA table to collide with, but it reads lane ENDPOINTS out of free subject
text, and an incoterm sits in exactly the position a port does.

MEASURED 2026-08-31 against the production module, before the fix:

    parse_subject_lane('Relaxed cutoff to Tokyo')        -> ('LAX', 'Tokyo')
    parse_subject_lane('Flaxseed shipment to Busan')     -> ('LAX', 'Busan')
    parse_subject_lane('Updated Rates FOB Korea from …') -> ('Dalhart', 'FOB')
    parse_subject_lane('Rates CPT Japan from Tulare')    -> ('Tulare', 'CPT')

re-LAX-ed. f-LAX-seed. `_KNOWN_ORIGINS` carries the bare forms "SLC", "OAK"
and "LAX", and `_scan_for_origin` looked for them with an unanchored
`str.find()`. Separately, the "<DEST> <region> from <ORIGIN>" branch pops a
trailing region word and takes whatever capitalised token sits behind it —
which, for "FOB Korea", is the incoterm.

Both endpoints key a lane. A bogus one splits the lane bucket and mis-labels
the carrier scoreboard — the same damage the "HILMAR" entry did before it was
removed from `_KNOWN_ORIGINS` for exactly this reason.

HALF THIS FILE IS POSITIVE ASSERTIONS. A parser that returns (None, None) for
everything passes every negative test here.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for p in (str(ROOT / "src"), str(ROOT / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)

import body_parser as BP  # noqa: E402

from hilmar import body_parser as HBP  # noqa: E402

TREES = [pytest.param(BP, id="scripts"), pytest.param(HBP, id="hilmar")]


# ── an English word containing a code is not that code ────────────────────

@pytest.mark.parametrize("mod", TREES)
@pytest.mark.parametrize("subject", [
    "Relaxed cutoff to Tokyo",
    "Please relax the deadline to Osaka",
    "Flaxseed shipment to Busan",
    "Relaxation of the cutoff to Ningbo",
])
def test_lax_inside_an_ordinary_word_is_not_los_angeles(mod, subject):
    origin, _dest = mod.parse_subject_lane(subject)
    assert origin != "LAX", (
        f"{subject!r} produced origin LAX from a substring match — that is a "
        f"lane endpoint, and a wrong one splits the lane bucket")


@pytest.mark.parametrize("mod", TREES)
def test_the_origin_scan_is_word_bounded(mod):
    # Direct on the scanner, so a future rewrite of parse_subject_lane cannot
    # hide a regression here.
    assert mod._scan_for_origin("relaxed cutoff") is None
    assert mod._scan_for_origin("flaxseed") is None
    assert mod._scan_for_origin("croaking") is None
    # ...and it still finds a real one
    found = mod._scan_for_origin("oakland to tokyo")
    assert found and found[0] == "Oakland"


@pytest.mark.parametrize("mod", TREES)
def test_the_long_form_still_wins_over_its_own_abbreviation(mod):
    """Word-bounding "OAK" must not cost us "Oakland" — the long form is in
    the list ahead of the short one and matches at its own index."""
    found = mod._scan_for_origin("hilmar oakland to yokohama")
    assert found and found[0] == "Oakland"


# ── an incoterm is not a destination ──────────────────────────────────────

@pytest.mark.parametrize("mod", TREES)
@pytest.mark.parametrize("subject,bad", [
    ("Updated Rates FOB Korea from Dalhart", "FOB"),
    ("Rates CPT Japan from Tulare", "CPT"),
    ("Pricing CIF China from Modesto", "CIF"),
    ("Quote EXW Vietnam from Fresno", "EXW"),
    ("Rates DDP Taiwan from Visalia", "DDP"),
])
def test_an_incoterm_is_never_a_lane_endpoint(mod, subject, bad):
    origin, dest = mod.parse_subject_lane(subject)
    assert dest != bad, f"{subject!r} read the incoterm {bad} as a port"
    assert origin != bad, f"{subject!r} read the incoterm {bad} as an origin"


@pytest.mark.parametrize("mod", TREES)
def test_every_incoterm_and_unit_is_refused_as_an_endpoint(mod):
    """The table is the guard, so assert the table — a fix that covered only
    the two tokens I happened to test would pass a narrower version of this."""
    for tok in mod._INCOTERMS | mod._UNIT_TOKENS:
        assert tok in mod._NOT_A_PLACE
    for tok in ("fob", "cif", "cpt", "exw", "ddp", "dap", "cbm", "kgs", "fcl", "lcl"):
        assert tok in mod._NOT_A_PLACE, f"{tok!r} is missing from the guard"
    # and it must not have swallowed a real port name
    for port in ("busan", "tokyo", "osaka", "kobe", "oakland", "lima", "dubai"):
        assert port not in mod._NOT_A_PLACE, (
            f"{port!r} is a real place and the guard would refuse it")


# ── POSITIVE: the parser must still do its job ────────────────────────────

@pytest.mark.parametrize("mod", TREES)
@pytest.mark.parametrize("subject,expected", [
    # the exact shape the "from" branch was built for (2026-06-24 Busan miss)
    ("Updated Cheese Rates Busan Korea from Dalhart", ("Dalhart", "Busan")),
    ("Rates Ningbo China from Tulare", ("Tulare", "Ningbo")),
    # the ordinary lane form
    ("HILMAR Oakland to Yokohama 2x40RF", ("Oakland", "Yokohama")),
    ("MDOLX261145_ HILMAR Oakland to Cat Lai 2x40RF", ("Oakland", "Cat Lai")),
])
def test_a_real_lane_still_parses(mod, subject, expected):
    assert mod.parse_subject_lane(subject) == expected


@pytest.mark.parametrize("mod", TREES)
def test_the_qc057_destination_recovery_still_fires(mod):
    # A real Lonny RFQ naming a port but no "X to Y" lane. Without this branch
    # ingest.build_requests drops the row entirely.
    _origin, dest = mod.parse_subject_lane("20 reefer request to Yokohama")
    assert dest == "Yokohama"


def test_both_trees_agree_on_every_case_in_this_file():
    for subject in ("Relaxed cutoff to Tokyo", "Rates CPT Japan from Tulare",
                    "Updated Cheese Rates Busan Korea from Dalhart",
                    "HILMAR Oakland to Yokohama 2x40RF",
                    "Flaxseed shipment to Busan"):
        assert BP.parse_subject_lane(subject) == HBP.parse_subject_lane(subject), (
            f"the trees disagree about {subject!r}")
