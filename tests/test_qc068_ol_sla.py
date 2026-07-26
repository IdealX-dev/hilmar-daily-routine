"""QC-068 — OL owes a quote within 3 BUSINESS hours.

Michael 2026-07-26: "ol response time has to be 3 hours" and, separately,
"win loss timer is the 24/72 hours". Those are two DIFFERENT clocks and this
test pins the distinction:

  * 3 business hours = OL's response SLA. Past it OL is OVERDUE and must be
    chased — but the row stays OPEN (PENDING_OL). It is not a loss.
  * 24h (72h Friday) = the win/loss timer that actually resolves the deal.

Collapsing the two would re-bury live business as "lost" — the 2026-07-24
defect this whole area exists to prevent.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import core as C  # noqa: E402
import qc_selfheal as q  # noqa: E402


def _open_rfq(rid, ts, lane="Oakland → HCMC"):
    return {"request_id": rid, "lane": lane, "status": "PENDING",
            "quoted": False, "request_timestamp": ts}


def test_breach_uses_business_hours_not_wall_clock():
    """The real Jul-22 HCMC RFQ: sent 6:42 PM ET, well after the desk closed.
    Overnight must NOT burn OL's SLA — at 9:30 AM next morning only 1.0
    business hour has elapsed even though ~15 wall-clock hours have."""
    rfq = "2026-07-22T22:42:00Z"                                  # Wed 6:42 PM ET
    morning = datetime(2026, 7, 23, 13, 30, tzinfo=timezone.utc)  # Thu 9:30 AM ET
    assert q.qc068_ol_sla_breaches([_open_rfq("hcmc", rfq)], now=morning) == []

    afternoon = datetime(2026, 7, 23, 17, 30, tzinfo=timezone.utc)  # Thu 1:30 PM ET
    bad = q.qc068_ol_sla_breaches([_open_rfq("hcmc", rfq)], now=afternoon)
    assert len(bad) == 1 and bad[0][2] >= C.PENDING_OL_SLA_BIZ_HOURS


def test_weekend_never_counts_against_ol():
    friday_4pm = "2026-07-17T20:00:00Z"                             # Fri 4 PM ET
    monday_9am = datetime(2026, 7, 20, 13, 0, tzinfo=timezone.utc)  # Mon 9 AM ET
    # Fri 4:00-5:30 = 1.5 biz h, plus Mon 8:30-9:00 = 0.5 -> 2.0 < 3.
    assert q.qc068_ol_sla_breaches([_open_rfq("fri", friday_4pm)], now=monday_9am) == []


def test_only_flags_open_pending_ol_rows():
    now = datetime(2026, 7, 23, 17, 30, tzinfo=timezone.utc)
    rows = [
        dict(_open_rfq("quoted", "2026-07-22T22:42:00Z"), quoted=True),
        dict(_open_rfq("won", "2026-07-22T22:42:00Z"), status="WIN"),
        dict(_open_rfq("lost", "2026-07-22T22:42:00Z"), status="LOSS"),
    ]
    assert q.qc068_ol_sla_breaches(rows, now=now) == []


def test_breach_does_not_make_the_row_a_loss():
    """The SLA is an alert, not a status change — decide_status must still
    hold the row PENDING_OL well past 3 business hours."""
    rfq = "2026-07-23T13:00:00Z"                                  # Thu 9 AM ET
    later = datetime(2026, 7, 23, 19, 0, tzinfo=timezone.utc)     # Thu 3 PM ET, 6 biz h
    assert q.qc068_ol_sla_breaches([_open_rfq("x", rfq)], now=later)
    d = C.decide_status(has_send=False, mdolx_ref=None, response_timestamp=None,
                        quoted=False, etd_fit_days=None,
                        request_timestamp=rfq, now=later)
    assert d.status == "PENDING", "an SLA breach must not turn an open RFQ into a loss"
    assert C.pending_substate({"status": d.status, "quoted": d.quoted}) == "PENDING_OL"


def test_the_two_timers_are_independent():
    """3-biz-hour SLA and the 24h win/loss timer must not be the same number
    or the same clock."""
    assert C.PENDING_OL_SLA_BIZ_HOURS == 3
    assert C.PENDING_OL_LOSS_HOURS == 24
    assert C.PENDING_HILMAR_LOSS_HOURS == 24
    assert C.PENDING_HILMAR_LOSS_HOURS_FRIDAY == 72


def test_sorted_worst_first():
    now = datetime(2026, 7, 23, 19, 0, tzinfo=timezone.utc)
    rows = [_open_rfq("newer", "2026-07-23T16:00:00Z", "Lane B"),
            _open_rfq("older", "2026-07-22T22:42:00Z", "Lane A")]
    out = q.qc068_ol_sla_breaches(rows, now=now)
    assert [r[0] for r in out] == ["older", "newer"], "worst breach first"
