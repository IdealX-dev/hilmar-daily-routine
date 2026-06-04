"""Direct tests for the PRODUCTION classifier in scripts/core.py.

Why a dedicated file: the Cloud PC runs scripts/, but the suite + the
--cov=hilmar gate only measured src/hilmar/. scripts/core.decide_status
— the function that actually sets every WIN/LOSS the client sees — had
no direct tests, which is how it ran the old "has_send OR mdolx -> WIN"
rule (producing phantom WINs) for a month with a green suite. These
lock the production classifier's LEGACY-form behaviour.
"""
from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"

spec = importlib.util.spec_from_file_location("scripts_core_prod", SCRIPTS / "core.py")
SC = importlib.util.module_from_spec(spec)
sys.modules["scripts_core_prod"] = SC
spec.loader.exec_module(SC)

UTC = timezone.utc


def test_win_requires_both_send_and_mdolx():
    d = SC.decide_status(has_send=True, mdolx_ref="MDX1",
                         response_timestamp="2026-04-21T03:00:00Z",
                         quoted=True, etd_fit_days=0)
    assert d.status == "WIN"
    assert d.loss_reason is None


def test_send_only_fresh_is_pending_awaiting_mdolx():
    """The bug's core: send with NO mdolx is NOT a WIN — it's PENDING
    until the booking lands (old rule wrongly returned WIN here)."""
    now = datetime(2026, 4, 21, 12, 0, tzinfo=UTC)  # Tue, 9h after send
    d = SC.decide_status(has_send=True, mdolx_ref=None,
                         response_timestamp="2026-04-21T03:00:00Z",
                         quoted=True, etd_fit_days=0,
                         send_signal_events=[{"at": "2026-04-21T03:00:00Z"}],
                         now=now)
    assert d.status == "PENDING"
    assert d.loss_reason == "AWAITING_MDOLX"


def test_send_only_stale_demotes_to_loss_send_no_booking():
    """Send-only that goes stale demotes to LEGACY LOSS / SEND_NO_BOOKING
    (the audit displays this as Q&L). This is what clears the QC-049
    phantom-win backlog."""
    now = datetime(2026, 4, 27, 13, 0, tzinfo=UTC)  # next Monday
    d = SC.decide_status(has_send=True, mdolx_ref=None,
                         response_timestamp="2026-04-21T13:00:00Z",
                         quoted=True, etd_fit_days=0,
                         send_signal_events=[{"at": "2026-04-21T13:00:00Z"}],
                         now=now)
    assert d.status == "LOSS"             # LEGACY form preserved
    assert d.loss_reason == "SEND_NO_BOOKING"


def test_mdolx_without_send_is_pending_anomaly():
    d = SC.decide_status(has_send=False, mdolx_ref="MDX9",
                         response_timestamp="2026-04-21T03:00:00Z",
                         quoted=True, etd_fit_days=0)
    assert d.status == "PENDING"
    assert d.loss_reason == "MDOLX_NO_SEND"


def test_friday_send_survives_weekend_then_demotes_tuesday_evening():
    """Per Michael 2026-06-04 — Friday send stays PENDING through Monday
    AND Tuesday morning/afternoon, only demotes to SEND_NO_BOOKING after
    Tuesday 18:00 ET. Lonny gets the full Mon + Tue window to reply."""
    fri = "2026-04-24T20:00:00Z"
    mon_pm = datetime(2026, 4, 27, 23, 0, tzinfo=UTC)   # Mon 19:00 ET
    tue_pm = datetime(2026, 4, 28, 23, 0, tzinfo=UTC)   # Tue 19:00 ET
    common = dict(has_send=True, mdolx_ref=None, response_timestamp=fri,
                  quoted=True, etd_fit_days=0, send_signal_events=[{"at": fri}])
    assert SC.decide_status(now=mon_pm, **common).status == "PENDING", \
        "Friday send still alive Monday evening"
    assert SC.decide_status(now=tue_pm, **common).loss_reason == "SEND_NO_BOOKING"


def test_no_response_is_legacy_loss_no_response():
    d = SC.decide_status(has_send=False, mdolx_ref=None,
                         response_timestamp=None, quoted=False, etd_fit_days=None)
    assert d.status == "LOSS"
    assert d.loss_reason == "NO_RESPONSE"


def test_quoted_within_window_is_pending():
    """Inside the 24h biz window on a normal weekday — stays PENDING.
    Wed 13:00 quote viewed Thu 12:00 UTC = 23h later → inside 24h."""
    now = datetime(2026, 4, 23, 12, 0, tzinfo=UTC)  # Thu 23h after Wed quote
    d = SC.decide_status(has_send=False, mdolx_ref=None,
                         response_timestamp="2026-04-22T13:00:00Z",  # Wed
                         quoted=True, etd_fit_days=0, now=now)
    assert d.status == "PENDING"


def test_quoted_aged_is_loss():
    now = datetime(2026, 4, 25, 12, 0, tzinfo=UTC)
    d = SC.decide_status(has_send=False, mdolx_ref=None,
                         response_timestamp="2026-04-21T03:00:00Z",  # ~4 days
                         quoted=True, etd_fit_days=2, now=now)
    assert d.status == "LOSS"
    # 2026-06-02: PRICE only fires with a concrete rate gap; the catch-all
    # is now UNDIFFERENTIATED. Both are valid Q&L outcomes.
    assert d.loss_reason in (
        "PRICE", "ETD_MISS", "OTHER", "QUOTED_NOT_BOOKED", "UNDIFFERENTIATED"
    )


def test_quoted_friday_not_stale_monday_morning():
    """Friday/weekend carve-out per Michael 2026-06-04: Friday quote not
    LOSS until Tuesday 18:00 ET. Monday morning is still PENDING."""
    fri_quote = "2026-04-24T20:00:00Z"   # Fri ~16:00 ET
    mon_morning = datetime(2026, 4, 27, 18, 0, tzinfo=UTC)  # Mon 14:00 ET
    d = SC.decide_status(has_send=False, mdolx_ref=None,
                         response_timestamp=fri_quote, quoted=True,
                         etd_fit_days=0, now=mon_morning)
    assert d.status == "PENDING", "Friday quote must survive the weekend"


def test_quoted_friday_not_stale_monday_evening():
    """Per Michael 2026-06-04 rule: Friday quote stays PENDING through
    Monday — Lonny gets BOTH Monday and Tuesday to reply."""
    fri_quote = "2026-04-24T20:00:00Z"
    mon_evening = datetime(2026, 4, 27, 23, 0, tzinfo=UTC)  # Mon 19:00 ET
    d = SC.decide_status(has_send=False, mdolx_ref=None,
                         response_timestamp=fri_quote, quoted=True,
                         etd_fit_days=0, now=mon_evening)
    assert d.status == "PENDING", "Friday quote still alive Monday evening"


def test_quoted_friday_stale_tuesday_evening():
    """Friday quote IS stale after Tuesday 18:00 ET — flips to LEGACY LOSS.
    This is the 'by Tuesday' deadline Michael restated 2026-06-04."""
    fri_quote = "2026-04-24T20:00:00Z"
    tue_evening = datetime(2026, 4, 28, 23, 0, tzinfo=UTC)  # Tue 19:00 ET
    d = SC.decide_status(has_send=False, mdolx_ref=None,
                         response_timestamp=fri_quote, quoted=True,
                         etd_fit_days=0, now=tue_evening)
    assert d.status == "LOSS"             # LEGACY form
    # 2026-06-02: PRICE only fires with a concrete rate gap; the catch-all
    # is now UNDIFFERENTIATED. Both are valid Q&L outcomes.
    assert d.loss_reason in (
        "PRICE", "ETD_MISS", "OTHER", "QUOTED_NOT_BOOKED", "UNDIFFERENTIATED"
    )


def test_pending_window_hours_is_24():
    """Lock the production constant per Michael 2026-06-04 ('if hilmar
    doesn't reply after 24 hours during biz week the deal is lost').
    Was 48 from 2026-05-30 to 2026-06-04; corrected to 24."""
    assert SC.PENDING_WINDOW_HOURS == 24
