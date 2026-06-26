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
