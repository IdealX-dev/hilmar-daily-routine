"""patch_carriers PASS 2b — destination/lane from the booking PDF's POD.

The Jul 9 email rendered two 'Lane unresolved' rows (stand_260895 /
stand_260905): bare booking amendments whose body is signature-only and
whose subject names no port — the ONLY lane source is the attached booking
PDF's Port of Discharge, which PASS 2 already backfills into r["pod"].
_dest_from_pod maps that POD onto the curated KNOWN_DESTINATIONS corpus;
anything else returns None so a garbled PDF cell can never invent a lane.
Zero 'Lane unresolved' in the daily email is the standard (Michael,
2026-07-09/10).
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import body_parser as BP  # noqa: E402
import patch_carriers as PC  # noqa: E402


def test_known_port_maps_to_canonical_destination():
    assert PC._dest_from_pod("Singapore") == "Singapore"
    assert PC._dest_from_pod("SINGAPORE") == "Singapore"


def test_pod_with_trailing_country_resolves():
    assert PC._dest_from_pod("Yokohama, Japan") == "Yokohama"


def test_garbage_pod_never_invents_a_lane():
    assert PC._dest_from_pod("Dear Customer") is None
    assert PC._dest_from_pod("") is None
    assert PC._dest_from_pod(None) is None
    assert PC._dest_from_pod(123) is None


def test_every_known_destination_roundtrips():
    # The helper must accept the whole curated corpus (case-insensitive).
    for d in BP.KNOWN_DESTINATIONS:
        assert PC._dest_from_pod(d.upper()) == d, d
