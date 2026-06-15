"""The Manila carrier blind spot — 2026-06-15 live failure.

The Oakland→Manila OL quote (Linda Echevarria, $797) shipped in the client
email with the carrier column BLANK ("nothing should be blank"). Root cause:
the PRODUCTION parser (scripts/body_parser.py) read the carrier only from a
column literally headed "Carrier" (`cells.get("carrier")`). When OL relabeled
that column ("Ocean Carrier" / "Line" / "SSL") — or dropped the carrier into
an unlabeled cell or the surrounding prose — the rate parsed and the carrier
blanked.

Pinned here:
  - detect_carrier_token honors word boundaries + the ambiguous-token guard
  - parse_rate_table resolves the carrier from a relabeled column header
  - ... from an unlabeled data cell, and ... from the body prose
  - BOTH trees (scripts/ production + src/hilmar/ test target) extract it
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import body_parser as SBP  # noqa: E402  (production tree)

from hilmar import body_parser as HBP  # noqa: E402  (test/accuracy tree)


# ── detect_carrier_token ──────────────────────────────────────────────────
def test_detect_carrier_token_multiword_wins():
    assert SBP.detect_carrier_token("CMA CGM service direct") == "CMA CGM"


def test_detect_carrier_token_distinctive_in_prose():
    assert SBP.detect_carrier_token("quoted via Maersk this week") == "Maersk"
    assert SBP.detect_carrier_token("MSC vessel proposed") == "MSC"


def test_detect_carrier_token_skips_ambiguous_in_prose():
    # "ONE" as an English word must NOT read as the carrier ONE in prose.
    assert SBP.detect_carrier_token("free time for ONE day") is None
    # ... but a dedicated carrier cell may say exactly "ONE".
    assert SBP.detect_carrier_token("ONE", allow_short=True) == "ONE"


# ── parse_rate_table: relabeled carrier column (the Manila failure) ────────
_RELABELED = (
    "POL | POD | Container Size | Ocean Carrier | RATE | ETD | ETA\n"
    "Oakland | Manila | 2x20'ST | MSC | $797 | | "
)


def test_relabeled_carrier_column_scripts_tree():
    rt = SBP.parse_rate_table(_RELABELED)
    assert rt.get("ol_rate") == 797.0
    assert rt.get("carrier_quoted") == "MSC"


def test_relabeled_carrier_column_src_tree():
    rt = HBP.parse_rate_table(_RELABELED)
    assert rt.get("carrier_quoted") == "MSC"


# ── parse_rate_table: carrier only in an unlabeled cell ────────────────────
def test_carrier_in_unlabeled_cell_scripts_tree():
    text = (
        "POL | POD | Size | RATE | ETD | ETA | Notes\n"
        "Oakland | Manila | 2x20'ST | $797 | | | MSC service, direct"
    )
    rt = SBP.parse_rate_table(text)
    assert rt.get("ol_rate") == 797.0
    assert rt.get("carrier_quoted") == "MSC"


# ── parse_rate_table: carrier only in body prose around the grid ───────────
def test_carrier_in_body_prose_scripts_tree():
    text = (
        "Pleased to offer the below on Maersk for your Oakland-Manila move.\n"
        "POL | POD | Size | RATE | ETD | ETA\n"
        "Oakland | Manila | 2x20'ST | $797 | | "
    )
    rt = SBP.parse_rate_table(text)
    assert rt.get("carrier_quoted") == "Maersk"


# ── a standard "Carrier"-headed table still works (no regression) ──────────
def test_standard_carrier_header_unchanged():
    text = (
        "POL | POD | Size | Vessel | Voyage | RATE | CARRIER\n"
        "Oakland | Yokohama | 2x40'RF | WAN HAI A01 | W017 | $3500 | CMA"
    )
    rt = SBP.parse_rate_table(text)
    assert rt.get("ol_rate") == 3500.0
    assert rt.get("carrier_quoted") == "CMA CGM"
