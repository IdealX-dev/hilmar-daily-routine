"""PENDING_HILMAR quote-decision window (Michael 2026-07-14):
"pending hilmar is 48 hours most.. then it's lost if we don't win.. except
fridays.. it's 72 hours." A quote awaiting Lonny's decision ages to Q&L after
48 CLOCK hours from the OL quote — 72 when OL quoted on a Friday (ET), so the
weekend lands Lonny on Monday. Supersedes the prior 24h-biz + Tuesday-18:00
carve-out that left 73-78h Friday quotes stuck PENDING.
"""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import core  # noqa: E402

ET = ZoneInfo("America/New_York")


def _q(y, mo, d, h, mi):
    return dt.datetime(y, mo, d, h, mi, tzinfo=ET)


def test_constants_48_and_72():
    assert core.PENDING_HILMAR_LOSS_HOURS == 48
    assert core.PENDING_HILMAR_LOSS_HOURS_FRIDAY == 72


def test_non_friday_ages_at_48_clock_hours():
    resp = _q(2026, 7, 13, 10, 0)          # Mon 10:00 ET
    assert core.pending_hilmar_stale(resp, resp + dt.timedelta(hours=47.9)) is False
    assert core.pending_hilmar_stale(resp, resp + dt.timedelta(hours=48)) is True


def test_friday_extends_to_72_clock_hours():
    resp = _q(2026, 7, 10, 12, 55)         # Fri 12:55 ET (a real screenshot row)
    # At 48h (Sunday) a Friday quote is NOT yet lost — the weekend is carried.
    assert core.pending_hilmar_stale(resp, resp + dt.timedelta(hours=48)) is False
    assert core.pending_hilmar_stale(resp, resp + dt.timedelta(hours=71.9)) is False
    assert core.pending_hilmar_stale(resp, resp + dt.timedelta(hours=72)) is True


def test_screenshot_friday_rows_age_out_monday_evening():
    now = _q(2026, 7, 13, 19, 8)           # Mon Jul 13 7:08 PM ET (report time)
    # Both Friday-quoted rows are past 72h -> Q&L.
    assert core.pending_hilmar_stale(_q(2026, 7, 10, 17, 58), now) is True   # 73.2h
    assert core.pending_hilmar_stale(_q(2026, 7, 10, 12, 55), now) is True   # 78.2h
    # Monday-quoted rows (< 48h) stay PENDING.
    assert core.pending_hilmar_stale(_q(2026, 7, 13, 10, 40), now) is False  # 8.5h
    assert core.pending_hilmar_stale(_q(2026, 7, 13, 11, 34), now) is False  # 7.6h


def test_decide_status_ages_a_stale_friday_quote_to_qandl():
    now = _q(2026, 7, 13, 19, 8)
    d = core.decide_status(
        has_send=False, mdolx_ref=None,
        response_timestamp=_q(2026, 7, 10, 12, 55).astimezone(dt.timezone.utc).isoformat(),
        quoted=True, etd_fit_days=None, now=now)
    assert d.status == "LOSS"     # Quoted & Lost
    assert d.quoted is True

    # A fresh Monday quote stays PENDING.
    d2 = core.decide_status(
        has_send=False, mdolx_ref=None,
        response_timestamp=_q(2026, 7, 13, 10, 40).astimezone(dt.timezone.utc).isoformat(),
        quoted=True, etd_fit_days=None, now=now)
    assert d2.status == "PENDING"


def test_none_timestamp_not_stale():
    assert core.pending_hilmar_stale(None, core.now_utc()) is False


def test_trees_agree_on_behavior():
    sys.path.insert(0, str(ROOT / "src"))
    import hilmar.core as hc
    now = _q(2026, 7, 13, 19, 8)
    for resp in (_q(2026, 7, 10, 12, 55), _q(2026, 7, 13, 10, 40),
                 _q(2026, 7, 9, 15, 0)):
        assert core.pending_hilmar_stale(resp, now) == hc.pending_hilmar_stale(resp, now)
