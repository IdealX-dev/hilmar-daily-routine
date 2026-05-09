"""Tests for scripts/core.py — the pure-function library.

Ported from scripts/run_tests.py (the homemade harness) into pytest so
the test suite can ratchet on every PR via CI instead of being a
manual pre-flight gate.
"""
from __future__ import annotations

from datetime import datetime, timezone

import core


# ── parse_teu ─────────────────────────────────────────────────────────

class TestParseTeu:
    def test_3x40rf(self):
        assert core.parse_teu("3×40'RF") == (3, 6)

    def test_2x40hc(self):
        assert core.parse_teu("2x40HC") == (2, 4)

    def test_1x20dv(self):
        assert core.parse_teu("1x20'DV") == (1, 1)

    def test_none_returns_zero_zero(self):
        assert core.parse_teu(None) == (0, 0)

    def test_empty_string_returns_zero_zero(self):
        assert core.parse_teu("") == (0, 0)

    def test_garbage_returns_zero_zero(self):
        assert core.parse_teu("nope") == (0, 0)


# ── biz_hours_between ────────────────────────────────────────────────────

class TestBizHoursBetween:
    def test_same_day_three_and_a_half_hours(self):
        # Tue Apr 7 2026 9:00 ET → 12:30 ET = 3.5h
        start = datetime(2026, 4, 7, 13, 0, tzinfo=timezone.utc)
        end = datetime(2026, 4, 7, 16, 30, tzinfo=timezone.utc)
        got = core.biz_hours_between(start, end)
        assert got is not None
        assert abs(got - 3.5) < 0.05

    def test_weekend_cross(self):
        # Fri 4:30pm ET → Mon 9:00am ET
        # 1.0h Fri (close at 5:30pm) + 0.5h Mon (open 8:30 → 9:00) = 1.5h
        start = datetime(2026, 4, 3, 20, 30, tzinfo=timezone.utc)
        end = datetime(2026, 4, 6, 13, 0, tzinfo=timezone.utc)
        got = core.biz_hours_between(start, end)
        assert got is not None
        assert abs(got - 1.5) < 0.1

    def test_returns_none_for_none_inputs(self):
        assert core.biz_hours_between(None, None) is None
        assert core.biz_hours_between(
            None, datetime.now(timezone.utc)
        ) is None

    def test_returns_none_when_end_lte_start(self):
        t = datetime(2026, 4, 7, 13, 0, tzinfo=timezone.utc)
        assert core.biz_hours_between(t, t) is None


# ── is_lonny_send_reply ─────────────────────────────────────────────────

class TestIsLonnySendReply:
    def test_send_please_in_reply_is_acceptance(self):
        assert core.is_lonny_send_reply("Send please", is_reply=True) is True

    def test_send_outside_reply_is_not_acceptance(self):
        # A fresh request that starts with "Send" must not count.
        assert core.is_lonny_send_reply("Send", is_reply=False) is False

    def test_send_both_cutoffs_is_not_acceptance(self):
        assert core.is_lonny_send_reply(
            "Can you send both cutoffs?", is_reply=True,
        ) is False

    def test_resend_is_not_acceptance(self):
        assert core.is_lonny_send_reply(
            "Please resend the latest", is_reply=True,
        ) is False

    def test_empty_body_is_not_acceptance(self):
        assert core.is_lonny_send_reply("", is_reply=True) is False


# ── request_id ──────────────────────────────────────────────────────────

class TestRequestId:
    def test_stable_for_same_inputs(self):
        a = core.request_id("CONV-1", "2026-04-10T15:00:00Z", "Shanghai, CN")
        b = core.request_id("CONV-1", "2026-04-10T15:00:00Z", "Shanghai, CN")
        assert a == b
        assert len(a) >= 10

    def test_different_destination_yields_different_id(self):
        a = core.request_id("CONV-1", "2026-04-10T15:00:00Z", "Shanghai, CN")
        b = core.request_id("CONV-1", "2026-04-10T15:00:00Z", "Yokohama, JP")
        assert a != b

    def test_minute_precision_collapses_seconds(self):
        a = core.request_id("CONV-1", "2026-04-10T15:00:00Z", "Shanghai")
        b = core.request_id("CONV-1", "2026-04-10T15:00:59Z", "Shanghai")
        assert a == b


# ── decide_status ────────────────────────────────────────────────────────

class TestDecideStatus:
    def test_win_when_has_send_and_mdolx(self):
        d = core.decide_status(
            has_send=True, mdolx_ref="MDX-1",
            response_timestamp="2026-04-10T15:00:00Z",
            quoted=True, etd_fit_days=0,
        )
        assert d.status == "WIN"

    def test_win_when_only_has_send(self):
        d = core.decide_status(
            has_send=True, mdolx_ref=None,
            response_timestamp="2026-04-10T15:00:00Z",
            quoted=True, etd_fit_days=0,
        )
        assert d.status == "WIN"

    def test_win_when_only_mdolx(self):
        d = core.decide_status(
            has_send=False, mdolx_ref="MDX-1",
            response_timestamp=None, quoted=False, etd_fit_days=None,
        )
        assert d.status == "WIN"

    def test_loss_no_response_when_not_quoted(self):
        now = datetime(2026, 4, 15, 12, 0, tzinfo=timezone.utc)
        d = core.decide_status(
            has_send=False, mdolx_ref=None,
            response_timestamp=None, quoted=False,
            etd_fit_days=None, now=now,
        )
        assert d.status == "LOSS"
        assert d.loss_reason == "NO_RESPONSE"

    def test_pending_when_recent_quote_within_24h(self):
        now = datetime(2026, 4, 15, 12, 0, tzinfo=timezone.utc)
        d = core.decide_status(
            has_send=False, mdolx_ref=None,
            response_timestamp="2026-04-15T08:00:00Z",  # 4h ago
            quoted=True, etd_fit_days=0, now=now,
        )
        assert d.status == "PENDING"

    def test_loss_price_after_pending_window(self):
        now = datetime(2026, 4, 15, 12, 0, tzinfo=timezone.utc)
        d = core.decide_status(
            has_send=False, mdolx_ref=None,
            response_timestamp="2026-04-13T10:00:00Z",  # ~50h ago
            quoted=True, etd_fit_days=2, now=now,
        )
        assert d.status == "LOSS"
        assert d.loss_reason == "PRICE"

    def test_loss_etd_miss_when_etd_fit_geq_5(self):
        now = datetime(2026, 4, 15, 12, 0, tzinfo=timezone.utc)
        d = core.decide_status(
            has_send=False, mdolx_ref=None,
            response_timestamp="2026-04-13T10:00:00Z",
            quoted=True, etd_fit_days=7, now=now,
        )
        assert d.status == "LOSS"
        assert d.loss_reason == "ETD_MISS"


# ── etd_fit_days ─────────────────────────────────────────────────────────

class TestEtdFitDays:
    def test_offered_later_returns_positive(self):
        assert core.etd_fit_days("2026-04-10", "2026-04-13") == 3

    def test_offered_earlier_returns_negative(self):
        assert core.etd_fit_days("2026-04-13", "2026-04-10") == -3

    def test_same_date_returns_zero(self):
        assert core.etd_fit_days("2026-04-10", "2026-04-10") == 0

    def test_unparseable_returns_none(self):
        assert core.etd_fit_days("garbage", "2026-04-10") is None
        assert core.etd_fit_days(None, "2026-04-10") is None
        assert core.etd_fit_days("2026-04-10", None) is None


# ── normalize_carrier ────────────────────────────────────────────────────

class TestNormalizeCarrier:
    def test_cma_aliases_collapse_to_canonical(self):
        assert core.normalize_carrier("CMA") == "CMA CGM"
        assert core.normalize_carrier("CMA-CGM") == "CMA CGM"
        assert core.normalize_carrier("cma cgm") == "CMA CGM"

    def test_msc_passthrough(self):
        assert core.normalize_carrier("MSC") == "MSC"

    def test_maersk_family_collapses(self):
        assert core.normalize_carrier("MAERSK") == "Maersk"
        assert core.normalize_carrier("sealand") == "Maersk"

    def test_unknown_carrier_preserved(self):
        # Falls through cleaned with original casing; whitespace collapsed.
        assert core.normalize_carrier("Some New Carrier") == "Some New Carrier"

    def test_none_or_empty_returns_none(self):
        assert core.normalize_carrier(None) is None
        assert core.normalize_carrier("") is None
        assert core.normalize_carrier("   ") is None


# ── parse_rate ────────────────────────────────────────────────────────────

class TestParseRate:
    def test_dollar_with_thousands_separator(self):
        assert core.parse_rate("$2,400") == 2400.0

    def test_dollar_with_decimals(self):
        assert core.parse_rate("$2,400.50") == 2400.5

    def test_no_dollar_prefix_returns_none(self):
        # The regex requires a literal $.
        assert core.parse_rate("2400") is None

    def test_none_returns_none(self):
        assert core.parse_rate(None) is None

    def test_empty_returns_none(self):
        assert core.parse_rate("") is None


# ── trade_region_for ─────────────────────────────────────────────────────

class TestTradeRegionFor:
    def test_shanghai_far_east(self):
        assert core.trade_region_for("Shanghai") == "Far East"

    def test_jebel_ali_middle_east(self):
        assert core.trade_region_for("Jebel Ali") == "Middle East"

    def test_acajutla_central_america(self):
        assert core.trade_region_for("Acajutla") == "Central America"

    def test_unmapped_returns_unmapped(self):
        assert core.trade_region_for("Atlantis") == "Unmapped"

    def test_none_returns_unmapped(self):
        assert core.trade_region_for(None) == "Unmapped"

    def test_paren_suffix_falls_back_to_first_token(self):
        # "HCMC (Cat Lai)" should match "hcmc" → SE Asia.
        assert core.trade_region_for("HCMC (Cat Lai)") == "SE Asia"
