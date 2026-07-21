"""Day-KPI "Won" must count by WIN-EVENT date, not request date.

Regression for the 2026-07-21 sent report (reporting Mon Jul 20): "What
Happened — STATUS CHANGES" showed 2 wins on Jul 20 (a Jul-16 request whose
MDOLX260963 booking confirmed that day, plus a same-day request won via Lonny's
reply), while the day KPI tile said "0 Won — Mon Jul 20 / 0 TEU won". Cause:
_today_summary bucketed wins by request_date == report day, so a win that
HAPPENED on the report day for an older request never counted. Michael:
"firstly data missing … NO.. CHECK YOUR REPORT".

Now: wins count →WIN status_history transitions dated the report day (matching
the Status Changes section); WIN rows with no dated →WIN transition fall back
to request_date bucketing (legacy rows) so each win is attributed exactly once.
Requests/Q&L/NQ/Pending stay request-date-bucketed (they describe that day's
intake), and the KPI sub-line says so.
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import gen_email as GE  # noqa: E402

RD = date(2026, 7, 20)   # the report day (Monday)


def _row(rid, req_date, status, teu=2, win_at=None, quoted=True):
    r = {"request_id": rid, "request_date": req_date, "status": status,
         "quoted": quoted, "teu_requested": teu, "status_history": []}
    if win_at:
        r["status_history"] = [{"from": "PENDING", "to": "WIN",
                                "at": f"{win_at}T18:30:00Z"}]
        r["teu_won"] = teu
    return r


def test_win_on_report_day_for_older_request_counts():
    # THE Jul-20 case: requested Jul 16, booking confirmed Jul 20.
    rows = [_row("r-old", "2026-07-16", "WIN", teu=3, win_at="2026-07-20")]
    s = GE._today_summary(rows, report_date=RD)
    assert s["wins"] == 1, "a win that HAPPENED on the report day must count"
    assert s["teu_won"] == 3
    # It was not REQUESTED that day, so it is not in the day's request bucket.
    assert s["total"] == 0


def test_same_day_request_and_win_counts_once():
    rows = [_row("r-new", "2026-07-20", "WIN", teu=2, win_at="2026-07-20")]
    s = GE._today_summary(rows, report_date=RD)
    assert s["wins"] == 1 and s["teu_won"] == 2
    assert s["total"] == 1


def test_jul20_report_shape_two_wins():
    # Full reported shape: old-request win + same-day win + 2 pending quotes.
    rows = [
        _row("r-old", "2026-07-16", "WIN", teu=3, win_at="2026-07-20"),
        _row("r-new", "2026-07-20", "WIN", teu=2, win_at="2026-07-20"),
        _row("r-p1", "2026-07-20", "PENDING"),
        _row("r-p2", "2026-07-20", "PENDING"),
    ]
    s = GE._today_summary(rows, report_date=RD)
    assert s["wins"] == 2, "day KPI must agree with the 2 wins in Status Changes"
    assert s["teu_won"] == 5
    assert s["pending"] == 2


def test_win_on_a_different_day_does_not_count():
    rows = [_row("r-fri", "2026-07-16", "WIN", teu=3, win_at="2026-07-17")]
    s = GE._today_summary(rows, report_date=RD)
    assert s["wins"] == 0, "a Friday win must not leak into Monday's tile"


def test_legacy_win_row_without_dated_transition_uses_request_date():
    # No status_history: attributed once, under its request_date (old behavior).
    legacy = {"request_id": "r-legacy", "request_date": "2026-07-20",
              "status": "WIN", "quoted": True, "teu_requested": 4,
              "status_history": []}
    s = GE._today_summary([legacy], report_date=RD)
    assert s["wins"] == 1 and s["teu_won"] == 4
    s_other = GE._today_summary([legacy], report_date=date(2026, 7, 21))
    assert s_other["wins"] == 0, "legacy win must count exactly once (its request day)"


def test_requested_today_won_tomorrow_is_not_orphaned_and_not_double_counted():
    """Post-#111 review 🔴: requested Jul 20, booking confirmed Jul 21 —
    on Jul 20's tile the row must surface as won_later (not vanish from every
    bucket), and the WIN must count exactly once, on Jul 21's tile."""
    row = _row("r-next", "2026-07-20", "WIN", teu=2, win_at="2026-07-21")
    s20 = GE._today_summary([row], report_date=date(2026, 7, 20))
    assert s20["wins"] == 0, "win belongs to Jul 21, not Jul 20"
    assert s20["won_later"] == 1, "row must be surfaced, not orphaned"
    assert s20["total"] == 1
    s21 = GE._today_summary([row], report_date=date(2026, 7, 21))
    assert s21["wins"] == 1 and s21["won_later"] == 0
    # Exactly once across both day tiles.
    assert s20["wins"] + s21["wins"] == 1


def test_won_later_zero_when_win_is_same_day():
    row = _row("r-same", "2026-07-20", "WIN", teu=2, win_at="2026-07-20")
    s = GE._today_summary([row], report_date=RD)
    assert s["wins"] == 1 and s["won_later"] == 0


def test_evening_et_win_stays_on_its_et_day_not_utc():
    """Post-#112 review 🟡: booking confirmed Mon 9:30 PM EDT = Tue 01:30Z.
    UTC slicing pushed it to Tuesday (misfiring won_later); ET conversion
    keeps it on Monday — same day it actually happened."""
    row = {"request_id": "r-eve", "request_date": "2026-07-20", "status": "WIN",
           "quoted": True, "teu_requested": 2, "teu_won": 2,
           "status_history": [{"from": "PENDING", "to": "WIN",
                               "at": "2026-07-21T01:30:00Z"}]}
    s = GE._today_summary([row], report_date=date(2026, 7, 20))
    assert s["wins"] == 1, "9:30 PM EDT Monday win must count as Monday"
    assert s["won_later"] == 0, "no won_later misfire from the UTC date roll"
    s_tue = GE._today_summary([row], report_date=date(2026, 7, 21))
    assert s_tue["wins"] == 0, "and must not double count on Tuesday"


def test_et_date_helper_semantics():
    assert GE._et_date("2026-07-21T01:30:00Z") == date(2026, 7, 20)   # 9:30 PM EDT Mon
    assert GE._et_date("2026-07-20T18:30:00Z") == date(2026, 7, 20)   # 2:30 PM EDT Mon
    assert GE._et_date("2026-07-20") == date(2026, 7, 20)             # date-only untouched
    assert GE._et_date(None) is None


def test_dashboard_day_tile_uses_the_email_bucketing():
    """Post-#112 review 🟣: gen_dashboard re-derived tdy_wins by request_date +
    current status, contradicting the email's event-dated Won tile in the SAME
    daily send. The dashboard must consume gen_email._today_summary — one
    source, no drift."""
    src = (Path(__file__).resolve().parent.parent / "scripts" / "gen_dashboard.py").read_text(encoding="utf-8")
    assert "GE._today_summary(" in src, (
        "gen_dashboard must take its day KPIs from gen_email._today_summary"
    )
    assert 'tdy_wins    = sum(1 for r in today_reqs if r.get("status") == "WIN")' not in src, (
        "the independent request-date+status Won bucketing must be gone"
    )
