"""Regression test for the body_parser two-tree-drift audit fix.

The origin-parsing fix (removing "Hilmar"/"Hilmar, CA" from _KNOWN_ORIGINS)
landed only in src/hilmar/body_parser.py. The ACTIVE production copy,
scripts/body_parser.py, still listed "Hilmar" as a known origin, so
_scan_for_origin greedily picked "Hilmar" over the real port "Oakland" in
MDOLX booking subjects — producing lane labels like "Hilmar -> Tokyo" that
split the carrier-scoreboard lane bucket.

The existing suite imports `from hilmar import body_parser` (the already-fixed
src/ tree), so it stayed green while production shipped the bug. This test
loads the SCRIPTS copy explicitly (not via the hilmar package) to close the
parity hole, and asserts both trees agree.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_BODY_PARSER = REPO_ROOT / "scripts" / "body_parser.py"


def _load_scripts_body_parser():
    """Import scripts/body_parser.py under a private name so it never
    collides with the hilmar.body_parser package module."""
    spec = importlib.util.spec_from_file_location(
        "scripts_body_parser_under_test", SCRIPTS_BODY_PARSER
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# Subjects where the buggy production copy mis-labeled the origin as "Hilmar"
# instead of the real port-of-loading "Oakland".
_PORT_SUBJECTS = [
    ("HILMAR 1x20'DV Oakland to Bangkok", ("Oakland", "Bangkok")),
    ("MDOLX260432 HILMAR 3x40'RF Oakland to Tokyo", ("Oakland", "Tokyo")),
    ("HILMAR 2X40'RF Oakland to Yokohama", ("Oakland", "Yokohama")),
]


def test_scripts_body_parser_origin_is_port_not_customer():
    """Production scripts/body_parser.py must pick the real port (Oakland),
    not the customer name (Hilmar). Fails before the _KNOWN_ORIGINS fix."""
    sbp = _load_scripts_body_parser()
    for subject, expected in _PORT_SUBJECTS:
        assert sbp.parse_subject_lane(subject) == expected, subject


def test_scripts_body_parser_known_origins_excludes_customer_name():
    """The customer-name reference must not be a known origin in the
    production tree (it caused greedy origin mis-selection)."""
    sbp = _load_scripts_body_parser()
    lowered = {o.lower() for o in sbp._KNOWN_ORIGINS}
    assert "hilmar" not in lowered
    assert "hilmar, ca" not in lowered


def test_scripts_and_hilmar_body_parser_agree_on_lanes():
    """Close the two-tree-drift hole: scripts/ and src/hilmar/ must produce
    identical lane parses (the suite otherwise only walks the fixed src/ copy)."""
    sbp = _load_scripts_body_parser()
    from hilmar import body_parser as hbp  # src/ copy (added to path by conftest)

    subjects = [s for s, _ in _PORT_SUBJECTS] + [
        "Oakland to Bangkok",
        # The explicit arrow form legitimately keeps HILMAR as captured origin
        # via _LANE_RX_B in BOTH trees — parity must hold here too.
        "HILMAR -> Tokyo",
    ]
    for subject in subjects:
        assert sbp.parse_subject_lane(subject) == hbp.parse_subject_lane(subject), subject
