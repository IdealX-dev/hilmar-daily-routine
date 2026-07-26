"""QC-067 — an unanswered RFQ inside the response window is never a loss.

Daily proof-on-live-data for the 2026-07-24 root cause. decide_status used to
file every unquoted row as LOSS/NO_RESPONSE with zero grace, so PENDING_OL was
unreachable and live open business was stored as lost. QC-067 re-tests that
day's real rows on EVERY fire and self-heals anything that slips through.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import core as C  # noqa: E402
import qc_selfheal as q  # noqa: E402

NOW = datetime(2026, 7, 23, 14, 30, tzinfo=timezone.utc)   # Thu Jul 23, 10:30 ET
OPEN_RFQ = "2026-07-22T22:42:00Z"                           # Wed Jul 22, 3:42 PM PT


def _row(rid, **kw):
    base = {"request_id": rid, "quoted": False, "status": "LOSS",
            "loss_reason": "NO_RESPONSE", "request_timestamp": OPEN_RFQ}
    base.update(kw)
    return base


def test_flags_the_live_hcmc_shape():
    """The actual Jul-22 Oakland->HCMC RFQ: OL answered the next day."""
    bad = q.qc067_open_rfq_misfiled_as_lost([_row("hcmc")], now=NOW)
    assert len(bad) == 1
    assert bad[0][0] == "hcmc"
    assert 0 < bad[0][1] < 48, "reports hours waiting, inside the window"


def test_does_not_flag_a_genuinely_abandoned_rfq():
    rows = [_row("old", request_timestamp="2026-07-01T15:00:00Z")]
    assert q.qc067_open_rfq_misfiled_as_lost(rows, now=NOW) == []


def test_does_not_flag_quoted_or_other_loss_reasons():
    rows = [
        _row("quoted", quoted=True, status="PENDING", loss_reason=None),
        _row("price", loss_reason="PRICE"),
        _row("norate", loss_reason="RESPONSE_NO_RATE"),
        _row("won", status="WIN", loss_reason=None),
    ]
    assert q.qc067_open_rfq_misfiled_as_lost(rows, now=NOW) == []


def test_undateable_row_not_flagged():
    rows = [_row("nodate", request_timestamp=None, request_date=None)]
    assert q.qc067_open_rfq_misfiled_as_lost(rows, now=NOW) == []


def test_falls_back_to_request_date_when_timestamp_missing():
    rows = [_row("dateonly", request_timestamp=None, request_date="2026-07-23")]
    assert len(q.qc067_open_rfq_misfiled_as_lost(rows, now=NOW)) == 1


def test_agrees_with_decide_status_so_the_two_cannot_drift():
    """QC-067 must flag exactly the rows decide_status would now hold PENDING —
    otherwise the detector and the state machine disagree and the report
    contradicts itself again."""
    for ts in (OPEN_RFQ, "2026-07-01T15:00:00Z"):
        d = C.decide_status(has_send=False, mdolx_ref=None, response_timestamp=None,
                            quoted=False, etd_fit_days=None,
                            request_timestamp=ts, now=NOW)
        would_hold_pending = (d.status == "PENDING")
        flagged = bool(q.qc067_open_rfq_misfiled_as_lost(
            [_row("x", request_timestamp=ts)], now=NOW))
        assert would_hold_pending == flagged, f"detector/state-machine drift at {ts}"
