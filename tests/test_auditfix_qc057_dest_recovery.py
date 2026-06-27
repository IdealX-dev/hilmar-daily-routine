"""Regression test for the QC-057 destination-recovery audit fix.

The live drop: a real Lonny RFQ subject "20' reefer request to Yokohama"
names a known export PORT but no "X to Y" lane, so the legacy
parse_subject_lane returned (None, None). ingest.build_requests skips any row
with no parseable destination, so the RFQ was MISSING from the client report
with no alarm.

The root fix is a curated KNOWN_DESTINATIONS corpus (mirroring the foreign
ports of core._TRADE_REGION_MAP) plus a last-resort recovery branch in
parse_subject_lane that runs ONLY after every prior branch has failed — so it
can add recoveries but never change an existing extraction. These tests lock:
  * the live "to <port>" drop now recovers the port,
  * a bare-port last-resort case recovers,
  * a non-port note never invents a destination,
  * an ordinary lane still parses unchanged,
  * every corpus entry maps to a real (non-"Unmapped") trade region — so the
    corpus can't drift away from the canonical map.

scripts/ is the ACTIVE production tree, so this loads the SCRIPTS copies of
body_parser + core directly (not via the hilmar package).
"""
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"

# scripts/body_parser.py and scripts/core.py use bare top-level imports, so the
# scripts/ dir must be importable as a top-level package path.
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


@pytest.fixture(scope="module")
def prod_bp():
    import body_parser  # scripts/body_parser.py
    return body_parser


@pytest.fixture(scope="module")
def prod_core():
    import core  # scripts/core.py
    return core


def test_live_to_port_drop_now_recovers(prod_bp):
    """The exact subject that was being dropped from the client report."""
    origin, dest = prod_bp.parse_subject_lane("20' reefer request to Yokohama")
    assert dest == "Yokohama", (origin, dest)


def test_bare_port_last_resort_recovers(prod_bp):
    """No 'to', no lane — just a known port token. The absolute last-resort
    bare-token scan recovers it rather than dropping the row."""
    origin, dest = prod_bp.parse_subject_lane("Updated reefer pricing Rotterdam")
    assert dest == "Rotterdam", (origin, dest)


def test_non_port_note_invents_nothing(prod_bp):
    """A subject with NO known port must STILL return (None, None) — the
    recovery branch must never hallucinate a destination."""
    assert prod_bp.parse_subject_lane("REEFER NEEDS") == (None, None)


def test_ordinary_lane_unchanged(prod_bp):
    """A normal 'X to Y' lane still parses exactly as before — the recovery
    branch is purely additive and can't shadow the existing extraction."""
    assert prod_bp.parse_subject_lane("Oakland to Busan") == ("Oakland", "Busan")


def test_every_known_destination_maps_to_a_trade_region(prod_bp, prod_core):
    """Lock the corpus to core._TRADE_REGION_MAP: every recoverable port MUST
    resolve to a real trade region (never None / 'Unmapped'). If someone adds a
    port to KNOWN_DESTINATIONS without extending the map, this fails — the same
    'nothing is ever Unmapped' invariant Michael holds for trade regions."""
    unmapped = [
        d for d in prod_bp.KNOWN_DESTINATIONS
        if prod_core.trade_region_for(d) in (None, "Unmapped")
    ]
    assert not unmapped, (
        f"KNOWN_DESTINATIONS entries with no trade region: {unmapped} — "
        "extend core._TRADE_REGION_MAP or drop the entry."
    )
