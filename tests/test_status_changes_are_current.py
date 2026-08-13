"""STATUS CHANGES must hold what happened, not what the tracker caught up on.

Michael 2026-08-13, against a report whose section read STATUS CHANGES (16):
"clean up the massive status changes asap to just what's current last two
days.. we don't need to see all that you fixed".

MEASURED on the stored state (diag-blob run 31731525694), not inferred:

  2026-08-12 — 16 transitions
      2  MBD rate response — carrier=ONE, rate=475.0        <- real
     11  Operator correction: MDOLX2610xx booked (...)      <- his .xls, folded in
  2026-08-13 — 249 transitions
     35  OL-USA never responded with a quote
     32  Send received but no MDOLX within the 48h cutoff
    ~180 Quoted NNNN.Nh ago, no Send — ages up to 2926 HOURS

status_history is stamped when the PIPELINE decided (record_transition
defaults `at` to now), not when the business event happened. So a backlog
flush lands entirely on one day, and the next morning's report would have
opened with a 249-row wall of things that did not happen yesterday.
"""
from __future__ import annotations

import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import gen_email as GE  # noqa: E402

NOW = datetime(2026, 8, 13, 14, 0, tzinfo=timezone.utc)


def _iso(dt):
    return dt.isoformat().replace("+00:00", "Z")


def test_an_operator_correction_is_not_a_status_change():
    """The 11 MDOLX bookings reconciled out of Michael's transaction report.
    None were booked on Aug 12; they were ENTERED on Aug 12."""
    r = {"status": "WIN", "request_timestamp": _iso(NOW - timedelta(days=30))}
    h = {"at": _iso(NOW), "from": "LOSS", "to": "WIN",
         "reason": "Operator correction: MDOLX261029 booked (CMA CGM, booking NAM8664234)"}
    assert GE._is_current_status_change(r, h) is False


def test_a_prior_build_restore_is_not_a_status_change():
    r = {"status": "WIN", "request_timestamp": _iso(NOW - timedelta(days=30))}
    h = {"at": _iso(NOW), "from": "LOSS", "to": "WIN",
         "reason": "Prior-build WIN restored (MDOLX260123) — booking not visible"}
    assert GE._is_current_status_change(r, h) is False


def test_a_months_old_quote_aging_out_today_is_not_todays_news():
    """The ~180. 2926 hours is 122 days."""
    r = {"status": "LOSS",
         "request_timestamp": _iso(NOW - timedelta(hours=2930)),
         "response_timestamp": _iso(NOW - timedelta(hours=2926))}
    h = {"at": _iso(NOW), "from": "PENDING", "to": "LOSS",
         "reason": "Quoted 2926.4h ago, no Send — Quoted & Lost (rate $540 ...)"}
    assert GE._is_current_status_change(r, h) is False


def test_a_quote_that_just_aged_out_IS_news():
    """The line this must not cross: a quote from two days ago timing out is a
    real event on the day it times out. Only the BACKLOG is being cut."""
    r = {"status": "LOSS",
         "request_timestamp": _iso(NOW - timedelta(hours=50)),
         "response_timestamp": _iso(NOW - timedelta(hours=49))}
    h = {"at": _iso(NOW), "from": "PENDING", "to": "LOSS",
         "reason": "Quoted 49.0h ago, no Send — Quoted & Lost"}
    assert GE._is_current_status_change(r, h) is True


def test_a_booking_landing_is_always_news_however_old_the_ask():
    """Lonny asks in June, OL books today. That IS today's activity, and an
    age test alone would have thrown it away."""
    r = {"status": "WIN", "request_timestamp": _iso(NOW - timedelta(days=60))}
    h = {"at": _iso(NOW), "from": "PENDING", "to": "WIN",
         "reason": "MDOLX261099 booking confirmed"}
    assert GE._is_current_status_change(r, h) is True


def test_ol_answering_is_always_news():
    r = {"status": "PENDING", "request_timestamp": _iso(NOW - timedelta(days=9)),
         "response_timestamp": _iso(NOW)}
    h = {"at": _iso(NOW), "from": "PENDING", "to": "QUOTED",
         "reason": "MBD rate response — carrier=ONE, rate=475.0"}
    assert GE._is_current_status_change(r, h) is True


def test_a_transition_with_no_clock_is_kept_not_hidden():
    """A transition we cannot date is a data defect for the audit to surface,
    not something to quietly drop."""
    r = {"status": "LOSS"}
    h = {"at": None, "from": "PENDING", "to": "LOSS", "reason": "aged"}
    assert GE._is_current_status_change(r, h) is True


def test_the_aug_12_section_keeps_the_two_real_quotes_and_drops_the_eleven():
    """End to end through _today_events, on the shape the fire actually
    produced: 2 real OL answers + 11 reconciled bookings on one day."""
    day = date(2026, 8, 12)
    at = "2026-08-12T20:46:10Z"
    rows = [
        {"request_id": "req_quote_a", "status": "PENDING", "quoted": True,
         "request_timestamp": "2026-08-12T13:43:39Z",
         "response_timestamp": at, "request_date": "2026-08-12",
         "status_history": [{"at": at, "from": "PENDING", "to": "QUOTED",
                             "reason": "MBD rate response — carrier=ONE, rate=475.0"}]},
        {"request_id": "req_quote_b", "status": "PENDING", "quoted": True,
         "request_timestamp": "2026-08-12T17:05:06Z",
         "response_timestamp": "2026-08-12T20:57:02Z", "request_date": "2026-08-12",
         "status_history": [{"at": "2026-08-12T20:57:02Z", "from": "PENDING",
                             "to": "QUOTED",
                             "reason": "MBD rate response — carrier=CMA CGM, rate=4938.0"}]},
    ]
    for i in range(11):
        rows.append({
            "request_id": f"req_corr_{i}", "status": "WIN",
            "request_timestamp": "2026-06-20T12:00:00Z",
            "request_date": "2026-06-20",
            "status_history": [{"at": "2026-08-12T18:00:00Z", "from": "LOSS",
                                "to": "WIN",
                                "reason": f"Operator correction: MDOLX2610{i:02d} booked (CMA CGM)"}],
        })
    _new, _resp, status_ch, _pending = GE._today_events({"requests": rows}, day)
    assert len(status_ch) == 2, [h.get("reason") for _r, h in status_ch]
    assert all(h["to"] == "QUOTED" for _r, h in status_ch)


def test_a_backlog_flush_day_does_not_produce_a_wall():
    """2026-08-13's 249. After the filter the section must be readable."""
    day = date(2026, 8, 13)
    rows = []
    for i in range(249):
        rows.append({
            "request_id": f"req_bulk_{i}", "status": "LOSS",
            "request_timestamp": "2026-04-01T12:00:00Z",
            "response_timestamp": "2026-04-01T15:00:00Z",
            "request_date": "2026-04-01",
            "status_history": [{"at": "2026-08-13T13:00:00Z", "from": "PENDING",
                                "to": "LOSS",
                                "reason": "Quoted 2926.4h ago, no Send — Quoted & Lost"}],
        })
    _new, _resp, status_ch, _pending = GE._today_events({"requests": rows}, day)
    assert status_ch == []
