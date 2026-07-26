"""parse_teu must never mine a reference number, and must not undercount.

Found by the 2026-07-26 data audit and reproduced against the live function:
the old pattern was `(\\d+)\\s*[×x\\-]?\\s*(\\d{2})`, whose greedy `\\d+` ate a
PO number — "PO 4451440" returned qty=44,514 / 89,028 TEU. One such row
poisons every volume figure in that day's email, dashboard, PDF, lane rollups
and the client report. The mirror failure was silent under-count: the reverse
phrasing "40'HC x 2" returned 0 TEU on a real 2-container booking.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

import core as SC  # noqa: E402

from hilmar import core as HC  # noqa: E402

CORES = pytest.mark.parametrize("core", [SC, HC], ids=["scripts", "hilmar"])


@CORES
@pytest.mark.parametrize("text", [
    "PO 4451440 Oakland to HCMC",      # THE case: 7-digit PO ending in 40
    "ref 1234520 please quote",        # reference ending in 20
    "invoice 998845 attached",
    "container 2250F",                 # pre-existing guard, must still hold
    "quote 10040 for the move",
])
def test_reference_numbers_are_never_containers(core, text):
    count, teu = core.parse_teu(text)
    assert (count, teu) == (0, 0), f"{text!r} mined as containers: {count}x/{teu}TEU"


@CORES
@pytest.mark.parametrize("text,expected", [
    ("2-20'", (2, 2)),
    ("1-40' HC", (1, 2)),
    ("3 x 40 HC", (3, 6)),
    ("2×40'RF", (2, 4)),
    ("1x20'DV", (1, 1)),
    ("2-40' HC Reefers", (2, 4)),
    ("3×20'DV + 1×40'HC", (4, 5)),
    ("1-45' HC", (1, 2)),
])
def test_real_container_specs_still_parse(core, text, expected):
    assert core.parse_teu(text) == expected


@CORES
@pytest.mark.parametrize("text,expected", [
    ("40'HC x 2", (2, 4)),
    ("20' x 3", (3, 3)),
])
def test_reverse_phrasing_no_longer_undercounts(core, text, expected):
    assert core.parse_teu(text) == expected


@CORES
def test_reverse_form_never_double_counts_a_forward_spec(core):
    """The reverse pattern is only consulted when the forward one found
    nothing — otherwise "2-40'HC" could be counted twice."""
    assert core.parse_teu("2-40'HC") == (2, 4)


@CORES
def test_empty_and_none_are_safe(core):
    assert core.parse_teu(None) == (0, 0)
    assert core.parse_teu("") == (0, 0)
    assert core.parse_teu("no containers mentioned") == (0, 0)


def test_both_trees_agree():
    for s in ["PO 4451440", "2-20'", "40'HC x 2", "3×20'DV + 1×40'HC", "1-45' HC", None]:
        assert SC.parse_teu(s) == HC.parse_teu(s), f"tree drift on {s!r}"
