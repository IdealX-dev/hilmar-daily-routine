"""PENDING_OL must be reachable — an unanswered RFQ is open business, not a loss.

Root-cause regression for 2026-07-24 (Michael: "your quality control system is
not functioning", and earlier "three requests mentioned only 2 ol responses …
yet mention waiting hilmar for the hcmc that doesn't show ol responded to or
open").

Before this fix, decide_status classified ANY unquoted row as LOSS/NO_RESPONSE
the instant it was ingested, with zero grace period. Consequences, both proven
against the live Jul-22 and Jul-23 sent reports:
  * "PENDING OL (0) — awaiting OL quote" was PERMANENTLY empty: no combination
    of inputs could produce PENDING_OL (brute-forced below).
  * A live RFQ Lonny sent that morning was STORED in tracking-data-v2.json as a
    LOSS — so nobody chased OL for the quote.

Now an unquoted row holds PENDING (quoted=False -> PENDING_OL) until the
response window expires (48 clock hours; 72 when Lonny asked on a Friday ET),
then ages to NQ/NO_RESPONSE as before.
"""
from __future__ import annotations

import itertools
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

import core as SC  # noqa: E402

from hilmar import core as HC  # noqa: E402

CORES = pytest.mark.parametrize("core", [SC, HC], ids=["scripts", "hilmar"])

NOW = datetime(2026, 7, 23, 14, 30, tzinfo=timezone.utc)   # Thu Jul 23, 10:30 ET
FRESH_RFQ = "2026-07-22T22:42:00Z"                          # Wed Jul 22, 3:42 PM PT


def _pending(core, d):
    return core.pending_substate({"status": d.status, "quoted": d.quoted})


@CORES
def test_fresh_unanswered_rfq_is_pending_ol_not_a_loss(core):
    """THE defect: the Jul-22 Oakland->HCMC RFQ. OL answered the NEXT day, so at
    report time it was genuinely awaiting OL — it must be PENDING_OL."""
    d = core.decide_status(has_send=False, mdolx_ref=None, response_timestamp=None,
                           quoted=False, etd_fit_days=None,
                           request_timestamp=FRESH_RFQ, now=NOW)
    assert d.status in ("PENDING",), f"expected PENDING, got {d.status}"
    assert d.quoted is False
    assert _pending(core, d) == "PENDING_OL"
    assert d.loss_reason is None, "an open request is not a loss"


@CORES
def test_pending_ol_is_reachable_at_all(core):
    """Brute-force the input space. Pre-fix this yielded ZERO combinations —
    the bucket the operator reads every morning could never be populated."""
    hits = 0
    for has_send, mdolx, resp, quoted, etd in itertools.product(
            [True, False], [None, "260999"], [None, "2026-07-23T12:00:00Z"],
            [True, False], [None, 0, 6]):
        d = core.decide_status(has_send=has_send, mdolx_ref=mdolx,
                               response_timestamp=resp, quoted=quoted,
                               etd_fit_days=etd, request_timestamp=FRESH_RFQ, now=NOW)
        if _pending(core, d) == "PENDING_OL":
            hits += 1
    assert hits > 0, "PENDING_OL is structurally unreachable"


@CORES
def test_abandoned_rfq_still_ages_to_no_response(core):
    """The window must actually close — a 3-week-silent RFQ is a real NQ."""
    d = core.decide_status(has_send=False, mdolx_ref=None, response_timestamp=None,
                           quoted=False, etd_fit_days=None,
                           request_timestamp="2026-07-01T15:00:00Z", now=NOW)
    assert d.quoted is False
    assert d.loss_reason == "NO_RESPONSE"
    assert _pending(core, d) is None, "an aged-out row is no longer pending"


@CORES
def test_friday_rfq_gets_the_longer_window(core):
    """Friday ask + Monday morning read = 65h. Inside the 72h Friday window."""
    friday_4pm_et = "2026-07-17T20:00:00Z"          # Fri Jul 17, 4 PM ET
    monday_9am_et = datetime(2026, 7, 20, 13, 0, tzinfo=timezone.utc)
    d = core.decide_status(has_send=False, mdolx_ref=None, response_timestamp=None,
                           quoted=False, etd_fit_days=None,
                           request_timestamp=friday_4pm_et, now=monday_9am_et)
    assert _pending(core, d) == "PENDING_OL", "Friday RFQ must survive the weekend"


@CORES
def test_undateable_row_preserves_legacy_behavior(core):
    """No date = no measurable window. Keep the old immediate-NQ result rather
    than leaking a row that stays PENDING forever."""
    d = core.decide_status(has_send=False, mdolx_ref=None, response_timestamp=None,
                           quoted=False, etd_fit_days=None,
                           request_timestamp=None, now=NOW)
    assert d.loss_reason == "NO_RESPONSE"
    assert _pending(core, d) is None


@CORES
def test_quoted_rows_are_untouched_by_this_change(core):
    """The Hilmar-side window must be unaffected: a fresh quote stays
    PENDING_HILMAR, and OL responding with no rate is still NQ."""
    quoted_fresh = core.decide_status(
        has_send=False, mdolx_ref=None, response_timestamp="2026-07-23T12:00:00Z",
        quoted=True, etd_fit_days=None, request_timestamp=FRESH_RFQ, now=NOW)
    assert _pending(core, quoted_fresh) == "PENDING_HILMAR"

    no_rate = core.decide_status(
        has_send=False, mdolx_ref=None, response_timestamp="2026-07-23T12:00:00Z",
        quoted=False, etd_fit_days=None, request_timestamp=FRESH_RFQ, now=NOW)
    assert no_rate.loss_reason == "RESPONSE_NO_RATE"


@CORES
def test_pending_ol_stale_helper_parity(core):
    assert core.pending_ol_stale(None) is True
    fresh = core.parse_iso(FRESH_RFQ)
    assert core.pending_ol_stale(fresh, NOW) is False
    old = core.parse_iso("2026-07-01T15:00:00Z")
    assert core.pending_ol_stale(old, NOW) is True


def test_both_trees_agree_on_the_window():
    """Byte-level policy parity — the two cores must not drift."""
    assert SC.PENDING_OL_LOSS_HOURS == HC.PENDING_OL_LOSS_HOURS == 24
    assert SC.PENDING_OL_LOSS_HOURS_FRIDAY == HC.PENDING_OL_LOSS_HOURS_FRIDAY == 72
    for ts in (FRESH_RFQ, "2026-07-01T15:00:00Z", None):
        a = SC.decide_status(has_send=False, mdolx_ref=None, response_timestamp=None,
                             quoted=False, etd_fit_days=None, request_timestamp=ts, now=NOW)
        b = HC.decide_status(has_send=False, mdolx_ref=None, response_timestamp=None,
                             quoted=False, etd_fit_days=None, request_timestamp=ts, now=NOW)
        assert (a.quoted, a.loss_reason) == (b.quoted, b.loss_reason), f"drift at {ts}"
