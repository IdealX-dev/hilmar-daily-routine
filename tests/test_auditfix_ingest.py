"""Regression test for the two-tree-drift audit fix in scripts/ingest.py.

The production ``scripts/ingest.py`` DEST_RX fallback regex used to be
hardcoded to ``oakland to ...`` while the paired ``src/hilmar/ingest.py``
had already been rebuilt to be origin-general from ``BP.KNOWN_ORIGINS``
(the 2026-06-11 "Dalhart blind spot" fix). ingest.py is a QC-040-paired
file, so the two copies must not drift.

These tests fail against the old hardcoded-oakland regex and pass once the
production copy mirrors the origin-general version.
"""
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"

# scripts/ingest.py uses bare ``import body_parser as BP`` / ``import core as C``,
# so the scripts/ dir must be importable as a top-level package path.
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


@pytest.fixture(scope="module")
def prod_ingest():
    import ingest  # scripts/ingest.py
    return ingest


def test_dest_rx_still_matches_oakland(prod_ingest):
    """Behavior-preserving: the original Oakland case must still match."""
    m = prod_ingest.DEST_RX.match("Oakland to Manila")
    assert m is not None
    assert m.group(1).strip() == "Manila"


def test_dest_rx_matches_dalhart_blind_spot(prod_ingest):
    """The fix: non-Oakland origins (the Dalhart blind spot) must match too.

    Against the old ``^\\s*oakland\\s+to\\s+...`` regex this returns None.
    """
    m = prod_ingest.DEST_RX.match("Dalhart, TX to Hamburg")
    assert m is not None, "DEST_RX must be origin-general, not Oakland-only"
    assert m.group(1).strip() == "Hamburg"


def test_dest_rx_is_origin_general_over_all_known_origins(prod_ingest):
    """Every known Hilmar origin site must be an accepted DEST_RX prefix."""
    import body_parser as BP

    failures = []
    for origin in BP.KNOWN_ORIGINS:
        subject = f"{origin} to Rotterdam"
        if prod_ingest.DEST_RX.match(subject) is None:
            failures.append(origin)
    assert not failures, f"DEST_RX failed to match origins: {failures}"


def test_scripts_dest_rx_mirrors_src_hilmar(prod_ingest):
    """The paired-file constructs must not drift: scripts/ and src/hilmar/
    DEST_RX must compile to the same pattern (origin-general)."""
    src_dir = REPO_ROOT / "src"
    if str(src_dir) not in sys.path:
        sys.path.insert(0, str(src_dir))
    from hilmar import ingest as lib_ingest

    assert prod_ingest.DEST_RX.pattern == lib_ingest.DEST_RX.pattern
