"""Contract tests for scripts/patch_carriers.py (audit finding [8]).

patch_carriers is Step 7 — the 4-pass carrier/rate/ETD/ERD backfill whose
output feeds the parser-accuracy gate and the WIN/Q&L state machine — yet no
test imported it, so a logic regression shipped silently. These lock the pure
discovery helpers that drive the backfill.
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))


@pytest.fixture(scope="module")
def PC():
    import patch_carriers
    return patch_carriers


def test_discover_lane_from_subjects_simple(PC):
    assert PC._discover_lane_from_subjects(
        ["RFQ Los Angeles to Shanghai 2x40HC"]) == ("Los Angeles", "Shanghai")


def test_discover_lane_from_booking_confirmation_subject(PC):
    subj = "MDOLX260587_ NEW BOOKING // HILMAR - Oakland to Osaka - 2X40'RF // EVERGREEN"
    assert PC._discover_lane_from_subjects([subj]) == ("Oakland", "Osaka")


def test_discover_lane_returns_none_when_no_lane(PC):
    assert PC._discover_lane_from_subjects(["FW: paperwork"]) == (None, None)


def test_discover_lane_scans_until_a_hit(PC):
    subjects = ["no lane here", "Oakland to Tokyo - booking"]
    assert PC._discover_lane_from_subjects(subjects) == ("Oakland", "Tokyo")


def test_discover_carrier_from_booking_subject(PC):
    subj = "MDOLX260587_ NEW BOOKING // HILMAR - Oakland to Osaka - 2X40'RF // EVERGREEN"
    assert PC._discover_carrier_from_subjects([subj]) == "Evergreen"


def test_discover_carrier_returns_none_when_absent(PC):
    assert PC._discover_carrier_from_subjects(["just a subject"]) is None


def test_strip_boilerplate_truncates_at_first_marker(PC):
    body = "CMA CGM at $2,500/40HC. Best Regards, OL desk + legal footer"
    out = PC._strip_boilerplate(body)
    assert out == "CMA CGM at $2,500/40HC. "
    assert "Best Regards" not in out


def test_strip_boilerplate_empty_and_no_marker(PC):
    assert PC._strip_boilerplate("") == ""
    assert PC._strip_boilerplate("no markers here") == "no markers here"
