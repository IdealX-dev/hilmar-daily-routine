"""Regression test for the additive-merge phantom-WIN precedence bug
(audit finding [4]) in scripts/ingest.py.

The additive merge carries a prior-run WIN forward only when it is NOT
already represented in the freshly-built wins ("captured"). The old inline
test was::

    if wm and wm not in new_mdolx_all or wma and not any(...):
        captured = False

which Python parses as ``(wm and wm not in new_mdolx_all) or (wma and ...)``
— OR'd on the PRIMARY mdolx_ref alone. A request accumulates
``mdolx_refs_all`` across runs and the primary ref is just the last-linked
one, so a prior WIN whose primary ref was absent from the new build but
whose SECONDARY ref was present got marked not-captured and re-appended as a
DUPLICATE WIN row for one booking — inflating wins, win_rate, and teu_won.

``_prior_win_captured`` fixes this: a WIN is captured when ANY of its refs
(primary or secondary) appears in the new build; lane+date is the no-ref
fallback. These tests fail against the old precedence and pass after the fix.
"""
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"

# scripts/ingest.py uses bare ``import body_parser as BP`` / ``import core as C``.
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


@pytest.fixture(scope="module")
def prod_ingest():
    import ingest  # scripts/ingest.py
    return ingest


def test_secondary_ref_present_is_captured_not_preserved(prod_ingest):
    """THE BUG: primary ref gone, secondary ref survives → already represented.

    wm='260100' (gone), wma=['260100','260200'], new build has '260200'.
    The same booking is present under the secondary ref, so the prior WIN must
    be captured (True) and NOT carried forward as a duplicate.
    """
    captured = prod_ingest._prior_win_captured(
        "260100", ["260100", "260200"], {"260200"}, "tokyo", "2026-06-01", set())
    assert captured is True, (
        "a prior WIN whose secondary MDOLX ref is in the new build must be "
        "captured (not re-appended as a duplicate)"
    )


def test_genuinely_lost_win_is_preserved(prod_ingest):
    """No ref of the prior WIN survives → genuinely lost → carry forward."""
    captured = prod_ingest._prior_win_captured(
        "260100", ["260100", "260200"], {"999999"}, "tokyo", "2026-06-01", set())
    assert captured is False


def test_primary_ref_present_is_captured(prod_ingest):
    captured = prod_ingest._prior_win_captured(
        "260200", ["260200"], {"260200"}, "tokyo", "2026-06-01", set())
    assert captured is True


def test_no_refs_falls_back_to_lane_date(prod_ingest):
    """Send-signal-promoted WIN with no MDOLX: lane+date match decides."""
    lane_dates = {("tokyo", "2026-06-01")}
    assert prod_ingest._prior_win_captured(
        None, [], set(), "tokyo", "2026-06-01", lane_dates) is True
    # Different day on the same lane → not the same logical win.
    assert prod_ingest._prior_win_captured(
        None, [], set(), "tokyo", "2026-06-02", lane_dates) is False
    # No refs and no lane match → preserve.
    assert prod_ingest._prior_win_captured(
        None, [], set(), "osaka", "2026-06-01", lane_dates) is False


def test_empty_string_refs_are_ignored(prod_ingest):
    """A row with empty-string refs must not be treated as having a real ref;
    it falls through to the lane+date path."""
    lane_dates = {("tokyo", "2026-06-01")}
    assert prod_ingest._prior_win_captured(
        "", [""], set(), "tokyo", "2026-06-01", lane_dates) is True
