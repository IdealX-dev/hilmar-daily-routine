"""Cross-tree parity for body_parser (audit findings [13]/[19]).

body_parser.py is a CLAUDE.md §2 paired file, but the cross-tree guard used to
cover only core.py enums — so the "Hilmar -> X" origin drift (the src tree was
fixed, production wasn't) shipped invisibly. These lock the shared surface:
  - KNOWN_ORIGINS must be byte-identical (also enforced at runtime by QC-040).
  - parse_subject_lane must agree on the curated subjects that exercise the
    origin-strip fix, so the two parsers can't diverge on lane bucketing again.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

import body_parser as SBP  # noqa: E402  scripts/body_parser.py

from hilmar import body_parser as HBP  # noqa: E402


def test_known_origins_are_identical():
    assert tuple(SBP.KNOWN_ORIGINS) == tuple(HBP.KNOWN_ORIGINS), (
        "scripts/ and src/hilmar/ KNOWN_ORIGINS drifted — this is the constant "
        "whose drift caused the 'Hilmar -> X' lane bug. QC-040 also guards this "
        "at runtime; mirror the edit to the paired file."
    )


def test_known_destinations_are_identical():
    """QC-057 corpus is a CLAUDE.md §2 paired surface — must be byte-identical
    so the destination-recovery branch can't recover different ports in the two
    trees (which would silently split lane buckets the same way KNOWN_ORIGINS
    drift once did)."""
    assert tuple(SBP.KNOWN_DESTINATIONS) == tuple(HBP.KNOWN_DESTINATIONS), (
        "scripts/ and src/hilmar/ KNOWN_DESTINATIONS drifted — mirror the edit "
        "to the paired file."
    )


def test_dest_recovery_live_in_both_trees():
    """The live QC-057 drop: a bare 'to <known port>' RFQ that no prior lane
    branch matched must recover the destination in BOTH trees, not (None,None)."""
    for bp in (SBP, HBP):
        origin, dest = bp.parse_subject_lane("20' reefer request to Yokohama")
        assert dest == "Yokohama", (origin, dest)


# Subjects chosen to exercise the origin-strip fix + ordinary lanes.
_SUBJECTS = [
    "MDOLX260587_ NEW BOOKING // HILMAR - Oakland to Osaka - 2X40'RF // EVERGREEN",
    "HILMAR 1x20'DV Oakland to Bangkok",
    "RFQ Los Angeles to Shanghai 2x40HC",
    "Dalhart, TX to Hamburg",
    "MDOLX260432 HILMAR 3x40'RF Oakland to Tokyo",
    "FW: paperwork only, no lane",
]


def test_parse_subject_lane_agrees_across_trees():
    diffs = []
    for s in _SUBJECTS:
        a = SBP.parse_subject_lane(s)
        b = HBP.parse_subject_lane(s)
        if a != b:
            diffs.append((s, a, b))
    assert not diffs, f"parse_subject_lane drift between trees: {diffs}"


def test_origin_strip_fix_is_live_in_both_trees():
    """The actual bug: 'HILMAR' must NOT be picked as the origin over the port."""
    for bp in (SBP, HBP):
        assert bp.parse_subject_lane("HILMAR 1x20'DV Oakland to Bangkok") == (
            "Oakland", "Bangkok")


# ── The rate-table core must be BYTE-IDENTICAL in both trees ──────────────
#
# 2026-08-13. This is the guard that was missing. The two parse_rate_table
# implementations had silently diverged into completely different algorithms —
# production read the pipe grid by header alignment and was correct, while
# src/hilmar had no table parser at all and regex-scanned the whole body,
# returning carrier "MSC" out of OL's Dummy-SI footer and vessel "dive" out of
# "vessel diversion". Nothing in the suite compared them, so it shipped.
#
# The table core is now one block of source, copied verbatim into both files.
# If someone edits one copy, this fails and names the file to mirror.
_CORE_START = "# ---------- OL rate table: HEADER-TO-CELL ALIGNMENT ----------"
_CORE_END = "def _canon_carrier(name):"


def _table_core(path):
    text = (ROOT / path).read_text(encoding="utf-8")
    assert _CORE_START in text, f"{path}: rate-table core marker missing"
    assert _CORE_END in text, f"{path}: rate-table core end marker missing"
    return text[text.index(_CORE_START):text.index(_CORE_END)]


def test_rate_table_core_is_byte_identical_across_trees():
    a = _table_core("scripts/body_parser.py")
    b = _table_core("src/hilmar/body_parser.py")
    assert a == b, (
        "The OL rate-table core drifted between scripts/body_parser.py and "
        "src/hilmar/body_parser.py. This exact drift is what let the "
        "boilerplate-scraping parse_rate_table ship in src/hilmar while "
        "production was already correct. Mirror the edit to the paired file.")


def test_table_cell_aliases_are_identical_across_trees():
    """The single list of OL column names. It replaced two lists that could
    disagree (_TABLE_HEADER_HINTS and _CARRIER_HEADER_ALIASES)."""
    assert SBP._TABLE_CELL_ALIASES == HBP._TABLE_CELL_ALIASES


def test_only_the_declared_contract_flag_differs():
    """The trees are allowed to differ on ONE thing: the output contract flag
    that keeps each side's persisted date format and legacy key spellings.
    Anything else diverging is drift."""
    assert SBP._LEGACY_SRC_CONTRACT is False, "scripts/ is production: raw dates"
    assert HBP._LEGACY_SRC_CONTRACT is True, "src/hilmar: ISO dates + etd/eta"
