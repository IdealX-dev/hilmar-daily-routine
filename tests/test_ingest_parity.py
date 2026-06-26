"""Cross-tree parity for ingest's shared pure helpers (audit finding [19]).

ingest.py is a CLAUDE.md §2 paired file and the two trees differ substantially
by design (production has the additive-merge + booking-linker; the library does
not). But the pure normalization helpers that BOTH trees expose must agree —
they decide lane keys, origins, destinations, and MDOLX refs that flow into the
WIN/Q&L state machine and the parser-accuracy gate. A silent divergence here is
the same class as the body_parser origin drift.

This is intentionally scoped to the SHARED pure helpers; the diverging
production-only logic (link_bookings_to_requests, additive merge) is not
compared. If a helper legitimately diverges later, document it here rather than
deleting the assertion.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

import ingest as SI  # noqa: E402  scripts/ingest.py

from hilmar import ingest as HI  # noqa: E402


def _agree(fn_name, cases):
    s, h = getattr(SI, fn_name), getattr(HI, fn_name)
    diffs = [(c, s(c), h(c)) for c in cases if s(c) != h(c)]
    assert not diffs, f"{fn_name} drift between scripts/ and src/hilmar/: {diffs}"


def test_canonical_lane_key_agrees():
    _agree("canonical_lane_key",
           ["Oakland to Tokyo", "Shanghai", "  Bangkok  ", "Los Angeles to Osaka"])


def test_clean_destination_agrees():
    _agree("clean_destination",
           ["Oakland to Tokyo", "Los Angeles to Shanghai (2)", "Dalhart, TX to Hamburg",
            "no lane here", ""])


def test_clean_origin_agrees():
    _agree("clean_origin",
           ["Dalhart, TX to Hamburg", "Oakland to Tokyo", "Los Angeles to Shanghai",
            "random subject"])


def test_extract_mdolx_agrees():
    _agree("extract_mdolx",
           ["MDOLX260100 booking", "no ref", "MDOLM999", "MDOLF12345 confirm", None])


def test_dest_rx_pattern_is_identical():
    """The fallback regex must stay mirrored (the 2026-06-11 Dalhart fix)."""
    assert SI.DEST_RX.pattern == HI.DEST_RX.pattern
