"""Unit tests for hilmar.core — ported from ../scripts/run_tests.py.

Pure-function tests; no IO except loading the golden fixture for the
aggregate_summary check.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from hilmar import core

# ── parse_teu ─────────────────────────────────────────────────────────

def test_parse_teu_3x40_reefer():
    count, teu = core.parse_teu("3×40'RF")
    assert count == 3
    assert teu == 6


def test_parse_teu_2x40_high_cube():
    count, teu = core.parse_teu("2x40HC")
    assert count == 2
    assert teu == 4


def test_parse_teu_empty_or_none():
    assert core.parse_teu(None) == (0, 0)
    assert core.parse_teu("") == (0, 0)


# ── biz_hours_between (DST-safe ET 8:30–17:30 Mon–Fri) ────────────────

def test_biz_hours_same_day_tue_9_to_1230_is_3_5h():
    # Tue 2026-04-07: 13:00 UTC == 09:00 EDT, 16:30 UTC == 12:30 EDT
    start = datetime(2026, 4, 7, 13, 0, tzinfo=timezone.utc)
    end = datetime(2026, 4, 7, 16, 30, tzinfo=timezone.utc)
    got = core.biz_hours_between(start, end)
    assert got is not None
    assert abs(got - 3.5) < 0.05, f"got {got}"


def test_biz_hours_crosses_weekend_fri_430pm_to_mon_9am_is_1_5h():
    # Fri 2026-04-03 20:30 UTC == 16:30 EDT → 1.0h until 17:30 close
    # Mon 2026-04-06 13:00 UTC == 09:00 EDT → 0.5h past 08:30 open
    start = datetime(2026, 4, 3, 20, 30, tzinfo=timezone.utc)
    end = datetime(2026, 4, 6, 13, 0, tzinfo=timezone.utc)
    got = core.biz_hours_between(start, end)
    assert got is not None
    assert abs(got - 1.5) < 0.1, f"got {got}"


# ── is_lonny_send_reply ───────────────────────────────────────────────

def test_is_lonny_send_reply_send_please_is_true():
    assert core.is_lonny_send_reply("Send please", is_reply=True) is True


def test_is_lonny_send_reply_requires_is_reply_true():
    # A fresh request that starts with "Send" must NOT count as acceptance.
    assert core.is_lonny_send_reply("Send", is_reply=False) is False


def test_is_lonny_send_reply_rejects_send_both_cutoffs():
    assert core.is_lonny_send_reply(
        "Can you send both cutoffs?", is_reply=True
    ) is False


# ── request_id (dedup key stability) ──────────────────────────────────

def test_request_id_is_stable_for_same_inputs():
    a = core.request_id("CONV-1", "2026-04-10T15:00:00Z", "Shanghai, CN")
    b = core.request_id("CONV-1", "2026-04-10T15:00:00Z", "Shanghai, CN")
    assert a == b
    assert len(a) >= 10


# ── decide_status state machine ───────────────────────────────────────

def test_decide_status_win_when_has_send_and_mdolx_ref():
    """Reading B (2026-04-27): WIN requires BOTH signals — has_send AND
    mdolx_ref. Both present → WIN."""
    d = core.decide_status(
        has_send=True,
        mdolx_ref="MDX-1",
        response_timestamp="2026-04-10T15:00:00Z",
        quoted=True,
        etd_fit_days=0,
    )
    assert d.status == "WIN", f"got {d.status}"
    assert d.loss_reason is None


def test_decide_status_pending_awaiting_mdolx_when_send_only():
    """Reading B: send received, MDOLX not yet — booking handoff in
    flight. Stages as PENDING(AWAITING_MDOLX) and auto-promotes to WIN
    on a later run when OL generates MDOLX. Test pins ``now`` within
    the 72h aging cutoff so we exercise the AWAITING_MDOLX branch and
    not the SEND_NO_BOOKING aging branch."""
    now = datetime(2026, 4, 11, 12, 0, tzinfo=timezone.utc)  # 21h after response
    d = core.decide_status(
        has_send=True,
        mdolx_ref=None,
        response_timestamp="2026-04-10T15:00:00Z",
        quoted=True,
        etd_fit_days=0,
        now=now,
    )
    assert d.status == "PENDING"
    assert d.loss_reason == "AWAITING_MDOLX"
    assert d.has_send is True


def test_decide_status_pending_awaiting_mdolx_via_send_signal_events():
    """Secondary-field check on the send side: send_signal_events alone
    is enough to satisfy has_send_eff even when has_send flag is False
    (parser may have populated only the events list)."""
    now = datetime(2026, 4, 11, 12, 0, tzinfo=timezone.utc)
    d = core.decide_status(
        has_send=False,
        mdolx_ref=None,
        response_timestamp="2026-04-10T15:00:00Z",
        quoted=True,
        etd_fit_days=0,
        send_signal_events=[{"at": "2026-04-10T15:00:00Z", "source": "lonny_reply"}],
        now=now,
    )
    assert d.status == "PENDING"
    assert d.loss_reason == "AWAITING_MDOLX"


def test_decide_status_mdolx_no_send_anomaly_when_mdolx_only():
    """Reading B: MDOLX without send is rare — anomaly. PENDING with
    MDOLX_NO_SEND so QC can flag for ops review."""
    d = core.decide_status(
        has_send=False,
        mdolx_ref="MDX-2",
        response_timestamp="2026-04-10T15:00:00Z",
        quoted=True,
        etd_fit_days=0,
    )
    assert d.status == "PENDING"
    assert d.loss_reason == "MDOLX_NO_SEND"
    assert d.has_send is False


def test_decide_status_win_via_secondary_fields_only():
    """has_send=False but send_signal_events non-empty AND mdolx_ref=None
    but mdolx_refs_all non-empty → still WIN. The secondary-field path
    catches rows whose primary flags didn't get set by parsers but whose
    list/event fields recorded the signal."""
    d = core.decide_status(
        has_send=False,
        mdolx_ref=None,
        response_timestamp="2026-04-10T15:00:00Z",
        quoted=True,
        etd_fit_days=0,
        send_signal_events=[{"at": "2026-04-10T15:00:00Z"}],
        mdolx_refs_all=["260432"],
    )
    assert d.status == "WIN"


def test_decide_status_awaiting_mdolx_ages_out_to_q_and_l_after_48h():
    """Send-only PENDING(AWAITING_MDOLX) auto-demotes to Q&L(SEND_NO_BOOKING)
    once the send goes stale. Real wins confirm same/next business day, so
    the cutoff is 48h business-hours (Michael 2026-05-30). Use a Tuesday
    send checked the following Monday — unambiguously past 48h."""
    # 2026-04-21 is a Tuesday; 2026-04-27 is the next Monday (~6 days later).
    aged_send_at = "2026-04-21T13:00:00Z"
    now = datetime(2026, 4, 27, 13, 0, tzinfo=timezone.utc)
    d = core.decide_status(
        has_send=True,
        mdolx_ref=None,
        response_timestamp=aged_send_at,
        quoted=True,
        etd_fit_days=0,
        send_signal_events=[{"at": aged_send_at, "source": "lonny_reply"}],
        now=now,
    )
    assert d.status == "Q&L"
    assert d.loss_reason == "SEND_NO_BOOKING"


def test_decide_status_awaiting_mdolx_stays_within_48h_window():
    """Inside the 48h cutoff, send-only stays PENDING(AWAITING_MDOLX)
    and remains eligible to mature to WIN when MDOLX lands. Tuesday send
    checked 9h later — well inside the window."""
    fresh_send_at = "2026-04-21T03:00:00Z"   # Tuesday
    now = datetime(2026, 4, 21, 12, 0, tzinfo=timezone.utc)  # 9h later, same day
    d = core.decide_status(
        has_send=True,
        mdolx_ref=None,
        response_timestamp="2026-04-21T03:00:00Z",
        quoted=True,
        etd_fit_days=0,
        send_signal_events=[{"at": fresh_send_at, "source": "lonny_reply"}],
        now=now,
    )
    assert d.status == "PENDING"
    assert d.loss_reason == "AWAITING_MDOLX"


def test_decide_status_friday_send_not_stale_monday_morning():
    """The Friday carve-out: a Friday send is NOT stale Monday morning
    (OL doesn't book over the weekend) — only after Monday 18:00 ET.
    2026-04-24 is a Friday; check Monday 2026-04-27 at 14:00 ET (18:00 UTC)."""
    fri_send = "2026-04-24T20:00:00Z"   # Fri ~16:00 ET
    mon_morning = datetime(2026, 4, 27, 18, 0, tzinfo=timezone.utc)  # Mon 14:00 ET
    d = core.decide_status(
        has_send=True, mdolx_ref=None, response_timestamp=fri_send,
        quoted=True, etd_fit_days=0,
        send_signal_events=[{"at": fri_send}], now=mon_morning,
    )
    assert d.status == "PENDING", "Friday send must survive the weekend"
    assert d.loss_reason == "AWAITING_MDOLX"


def test_decide_status_friday_send_stale_monday_evening():
    """Same Friday send IS stale after Monday 18:00 ET."""
    fri_send = "2026-04-24T20:00:00Z"
    mon_evening = datetime(2026, 4, 27, 23, 0, tzinfo=timezone.utc)  # Mon 19:00 ET
    d = core.decide_status(
        has_send=True, mdolx_ref=None, response_timestamp=fri_send,
        quoted=True, etd_fit_days=0,
        send_signal_events=[{"at": fri_send}], now=mon_evening,
    )
    assert d.status == "Q&L"
    assert d.loss_reason == "SEND_NO_BOOKING"


def test_decide_status_quoted_friday_not_stale_monday_morning():
    """Quote-aging branch: a Friday-afternoon quote with no Send by
    Monday morning must STAY PENDING (Lonny's workday hasn't started
    yet). Same weekend carve-out as send-signal aging — added 2026-05-30
    to fix the production drift where scripts/ used 24h flat-clock and
    flipped premature Q&L on Friday quotes."""
    fri_quote = "2026-04-24T20:00:00Z"   # Fri ~16:00 ET
    mon_morning = datetime(2026, 4, 27, 18, 0, tzinfo=timezone.utc)  # Mon 14:00 ET
    d = core.decide_status(
        has_send=False, mdolx_ref=None, response_timestamp=fri_quote,
        quoted=True, etd_fit_days=0, now=mon_morning,
    )
    assert d.status == "PENDING", "Friday quote must survive the weekend"


def test_decide_status_quoted_friday_stale_monday_evening():
    """Same Friday quote IS stale after Monday 18:00 ET — falls through
    to Q&L (or LEGACY LOSS in scripts/)."""
    fri_quote = "2026-04-24T20:00:00Z"
    mon_evening = datetime(2026, 4, 27, 23, 0, tzinfo=timezone.utc)  # Mon 19:00 ET
    d = core.decide_status(
        has_send=False, mdolx_ref=None, response_timestamp=fri_quote,
        quoted=True, etd_fit_days=0, now=mon_evening,
    )
    # src/hilmar uses STRICT vocab Q&L; scripts uses LEGACY LOSS. This
    # test runs under src/hilmar so STRICT.
    assert d.status == "Q&L"


def test_decide_status_quoted_wednesday_within_48h():
    """Inside the 48h biz window on a normal weekday — stays PENDING."""
    wed_quote = "2026-04-22T13:00:00Z"   # Wed
    thu_check = datetime(2026, 4, 23, 12, 0, tzinfo=timezone.utc)  # ~23h later
    d = core.decide_status(
        has_send=False, mdolx_ref=None, response_timestamp=wed_quote,
        quoted=True, etd_fit_days=0, now=thu_check,
    )
    assert d.status == "PENDING"


def test_decide_status_quoted_wednesday_past_48h():
    """Past 48h biz on a normal weekday — flips to Q&L."""
    wed_quote = "2026-04-22T13:00:00Z"
    fri_check = datetime(2026, 4, 24, 14, 0, tzinfo=timezone.utc)  # ~49h later
    d = core.decide_status(
        has_send=False, mdolx_ref=None, response_timestamp=wed_quote,
        quoted=True, etd_fit_days=0, now=fri_check,
    )
    assert d.status == "Q&L"


def test_decide_status_maturation_send_only_then_mdolx_arrives_promotes_to_win():
    """The maturation contract: a row classified PENDING(AWAITING_MDOLX)
    on day N, re-ingested on day N+1 with MDOLX now present, must
    classify as WIN. Tested by calling decide_status twice with the
    same has_send but mdolx_ref None then populated. Both calls pin
    ``now`` inside the 72h aging cutoff so we exercise maturation, not
    SEND_NO_BOOKING aging."""
    now = datetime(2026, 4, 11, 12, 0, tzinfo=timezone.utc)  # 21h after send
    common = dict(
        response_timestamp="2026-04-10T15:00:00Z",
        quoted=True,
        etd_fit_days=0,
        now=now,
    )
    day_n = core.decide_status(has_send=True, mdolx_ref=None, **common)
    assert day_n.status == "PENDING" and day_n.loss_reason == "AWAITING_MDOLX"

    day_n_plus_1 = core.decide_status(has_send=True, mdolx_ref="MDX-PROMOTED", **common)
    assert day_n_plus_1.status == "WIN"
    assert day_n_plus_1.loss_reason is None


def test_decide_status_loss_no_response_when_not_quoted_after_window():
    """Truly silent — Lonny outbound, no MBD response. Post 2026-04-27
    classifies as NQ (was: LOSS+NO_RESPONSE)."""
    now = datetime(2026, 4, 15, 12, 0, tzinfo=timezone.utc)
    d = core.decide_status(
        has_send=False,
        mdolx_ref=None,
        response_timestamp=None,
        quoted=False,
        etd_fit_days=None,
        now=now,
    )
    assert d.status == "NQ"
    assert d.loss_reason == "NO_RESPONSE"


def test_decide_status_nq_response_no_rate_edge():
    """MBD acked but extracted no rate — distinct from true silence."""
    now = datetime(2026, 4, 15, 12, 0, tzinfo=timezone.utc)
    d = core.decide_status(
        has_send=False,
        mdolx_ref=None,
        response_timestamp="2026-04-12T15:00:00Z",
        quoted=False,
        etd_fit_days=None,
        now=now,
    )
    assert d.status == "NQ"
    assert d.loss_reason == "RESPONSE_NO_RATE"


def test_decide_status_pending_when_quoted_within_window():
    """Quoted < 24h ago and Lonny silent → still PENDING, not Q&L."""
    now = datetime(2026, 4, 15, 12, 0, tzinfo=timezone.utc)
    d = core.decide_status(
        has_send=False,
        mdolx_ref=None,
        response_timestamp="2026-04-15T05:00:00Z",  # 7h before now
        quoted=True,
        etd_fit_days=0,
        now=now,
    )
    assert d.status == "PENDING"
    assert d.loss_reason is None


def test_decide_status_q_and_l_etd_miss_when_etd_fit_large():
    """Quoted past 24h, no booking, ETD missed by ≥5d → Q&L ETD_MISS."""
    now = datetime(2026, 4, 15, 12, 0, tzinfo=timezone.utc)
    d = core.decide_status(
        has_send=False,
        mdolx_ref=None,
        response_timestamp="2026-04-13T05:00:00Z",  # ~55h before now
        quoted=True,
        etd_fit_days=7,
        now=now,
    )
    assert d.status == "Q&L"
    assert d.loss_reason == "ETD_MISS"


def test_decide_status_q_and_l_price_when_etd_fit_ok():
    """Quoted past 24h, no booking, ETD fit OK → Q&L PRICE."""
    now = datetime(2026, 4, 15, 12, 0, tzinfo=timezone.utc)
    d = core.decide_status(
        has_send=False,
        mdolx_ref=None,
        response_timestamp="2026-04-13T05:00:00Z",
        quoted=True,
        etd_fit_days=1,
        now=now,
    )
    assert d.status == "Q&L"
    assert d.loss_reason == "PRICE"


def test_decide_status_q_and_l_quoted_not_booked_when_no_etd_signal():
    """Quoted past 24h, no booking, no ETD signal → Q&L QUOTED_NOT_BOOKED."""
    now = datetime(2026, 4, 15, 12, 0, tzinfo=timezone.utc)
    d = core.decide_status(
        has_send=False,
        mdolx_ref=None,
        response_timestamp="2026-04-13T05:00:00Z",
        quoted=True,
        etd_fit_days=None,
        now=now,
    )
    assert d.status == "Q&L"
    assert d.loss_reason == "QUOTED_NOT_BOOKED"


# ── etd_fit_days ──────────────────────────────────────────────────────

def test_etd_fit_days_positive_when_offered_later_than_requested():
    got = core.etd_fit_days("2026-04-10", "2026-04-13")
    assert got == 3, f"got {got}"


# ── aggregate_summary (golden fixture sanity) ─────────────────────────

def test_aggregate_summary_reproduces_fixture_win_rate(golden_day):
    s = core.aggregate_summary(golden_day["requests"])
    # Decided = wins + quoted_lost + not_quoted = 2 + 1 + 1 = 4; wins=2 → 50%
    assert abs(s["win_rate"] - 50.0) < 0.5, f"got {s['win_rate']}"
    assert s["wins"] == 2
    assert s["pending_hilmar"] == 1


# ── M3.8 / coverage push ──────────────────────────────────────────────


def test_normalize_carrier_handles_cma_variants():
    # CMA, CMA-CGM, CMA CGM all normalise to the same canonical form.
    a = core.normalize_carrier("CMA")
    b = core.normalize_carrier("CMA-CGM")
    c = core.normalize_carrier("CMA CGM")
    assert a == b == c, f"normaliser inconsistent: {a!r} {b!r} {c!r}"


def test_normalize_carrier_passes_through_unknown():
    assert core.normalize_carrier("Random Carrier") == "Random Carrier"


def test_normalize_carrier_handles_none():
    assert core.normalize_carrier(None) is None


def test_parse_rate_extracts_dollar_amount():
    assert core.parse_rate("$2,400") == 2400.0
    assert core.parse_rate("$1500.50") == 1500.5
    assert core.parse_rate("OL rate: $3000") == 3000.0


def test_parse_rate_handles_missing():
    assert core.parse_rate(None) is None
    assert core.parse_rate("") is None
    assert core.parse_rate("no dollar value") is None
    # PR #37: numeric inputs are now accepted (ingest stores ol_rate
    # as float; the old isinstance(str) check made every WIN row
    # produce rate_per_feu=None, collapsing the Value-won KPI to $0).
    assert core.parse_rate(2400) == 2400.0


def test_parse_rate_handles_unparseable_after_match():
    """Defensive: a regex match that nonetheless fails float() returns None."""
    assert core.parse_rate("$abc") is None


def test_etd_fit_days_loose_date_parser_handles_eta_prefix():
    # Lonny says "ETA 5/15/2026", OL offers "5/20/2026" → +5 days.
    got = core.etd_fit_days("ETA 5/15/2026", "5/20/2026")
    assert got == 5


def test_etd_fit_days_handles_short_date_with_fallback_year():
    got = core.etd_fit_days("5/15", "5/20", fallback_year=2026)
    assert got == 5


def test_etd_fit_days_returns_none_on_garbage():
    assert core.etd_fit_days("not a date", "5/15/2026") is None
    assert core.etd_fit_days(None, None) is None


# ── snapshot_state + compute_dod ──────────────────────────────────────


def _r(rid: str, **kw) -> dict:
    base = {
        "request_id": rid,
        "status": "PENDING",
        "lane": "Oakland → Shanghai",
        "destination": "Shanghai",
        "containers": "1-40' HC",
        "teu_requested": 2,
        "lonny_time_pt": "08:00 PT",
        "olusa_time_et": "11:00 ET",
        "turnaround_biz_hours": 2.0,
        "request_date": "2026-04-26",
    }
    base.update(kw)
    return base


def test_snapshot_state_captures_minimal_diff_keys():
    snap = core.snapshot_state([
        _r("a", status="WIN", quoted=True, has_send=True,
           carrier_won="MSC", mdolx_ref="123"),
        _r("b"),  # no request_id missing — included
        {"no_id": True},  # missing id — skipped
    ])
    assert "a" in snap and "b" in snap
    assert snap["a"]["status"] == "WIN"
    assert snap["a"]["mdolx_ref"] == "123"
    assert snap["b"]["status"] == "PENDING"


def test_compute_dod_flags_new_request_when_id_not_in_prev():
    prev: dict = {}
    curr = [_r("new", status="PENDING")]
    dod = core.compute_dod(prev, curr, today_iso="2026-04-26")
    assert len(dod["new_requests"]) == 1
    assert dod["new_requests"][0]["lane"] == "Oakland → Shanghai"


def test_compute_dod_flags_status_transition_to_win():
    prev = {"a": {"status": "PENDING", "quoted": True, "has_send": False,
                  "carrier_won": None, "mdolx_ref": None,
                  "response_timestamp": "2026-04-25T15:00:00+00:00"}}
    curr = [_r("a", status="WIN", carrier_won="MSC", mdolx_ref="999",
              quoted=True, has_send=True,
              response_timestamp="2026-04-25T15:00:00+00:00")]
    dod = core.compute_dod(prev, curr)
    assert any(s["from"] == "PENDING" and s["to"] == "WIN" for s in dod["status_changes"])
    assert len(dod["new_wins"]) == 1
    assert dod["new_wins"][0]["mdolx"] == "999"


def test_compute_dod_flags_newly_lost_when_quoted_to_loss():
    """Newly-lost = transition into Q&L specifically. NQ rows aren't
    'newly lost' in the customer-facing sense — they're 'never quoted',
    a different bucket in compute_dod (status_changes still records the
    transition; newly_lost is reserved for the Q&L bucket)."""
    prev = {"a": {"status": "PENDING", "quoted": True, "has_send": False,
                  "carrier_won": None, "mdolx_ref": None,
                  "response_timestamp": "2026-04-25T15:00:00+00:00"}}
    curr = [_r("a", status="Q&L", quoted=True,
              carrier_quoted="ZIM", ol_rate="$3500",
              response_timestamp="2026-04-25T15:00:00+00:00")]
    dod = core.compute_dod(prev, curr)
    assert len(dod["newly_lost"]) == 1
    assert dod["newly_lost"][0]["carrier"] == "ZIM"


def test_compute_dod_flags_new_pending():
    prev: dict = {}
    curr = [_r("a", status="PENDING", quoted=True,
              carrier_quoted="MSC", ol_rate="$2400")]
    dod = core.compute_dod(prev, curr)
    assert len(dod["new_pending"]) == 1
    assert dod["new_pending"][0]["carrier"] == "MSC"


def test_compute_dod_summary_text_counts_match():
    prev: dict = {}
    curr = [
        _r("new1", status="PENDING"),
        _r("new2", status="PENDING"),
        _r("won", status="WIN"),
    ]
    # Force "won" through a transition by giving prev a stale entry.
    prev = {"won": {"status": "PENDING", "quoted": False, "has_send": False,
                    "carrier_won": None, "mdolx_ref": None}}
    curr[2]["quoted"] = True
    curr[2]["carrier_won"] = "MSC"
    dod = core.compute_dod(prev, curr)
    assert "2 new requests" in dod["summary_text"]


def test_compute_dod_new_responses_carry_quoted_by_and_requested_at():
    """Per Michael 2026-04-29: the New quotes table needs to surface who
    quoted (signer) and when the request came in (PT). Both fields must
    propagate from the underlying request into new_responses entries
    even though the older lists are now consumed by active_conversations
    in the email."""
    prev: dict = {}
    curr = [_r(
        "a", status="Q&L", quoted=True,
        carrier_quoted="MSC", ol_rate=2400.0,
        response_timestamp="2026-04-26T15:00:00+00:00",
        ol_responder_signer="Alexandra Hernandez",
    )]
    dod = core.compute_dod(prev, curr)
    assert len(dod["new_responses"]) == 1
    resp = dod["new_responses"][0]
    assert resp["quoted_by"] == "Alexandra Hernandez"
    assert resp["requested_at_pt"] == "08:00 PT"


def test_compute_dod_active_conversations_dedupe_across_states():
    """Same request that flows new-rate-ask → quoted → pending-send in a
    single day must appear EXACTLY ONCE in active_conversations, at its
    most-evolved state ("AWAITING SEND"). Prevents the 3-table duplication
    Michael flagged 2026-04-29 (same row in New rate asks / New quotes /
    Newly pending Lonny ack)."""
    prev: dict = {}  # totally new today
    curr = [_r(
        "a", status="PENDING", quoted=True,
        carrier_quoted="CMA CGM", ol_rate=3600.0,
        response_timestamp="2026-04-26T18:00:00+00:00",
        ol_responder_signer="Alexandra Hernandez",
    )]
    dod = core.compute_dod(prev, curr)
    active = dod["active_conversations"]
    assert len(active) == 1, f"expected 1 row, got {len(active)}: {active}"
    row = active[0]
    assert row["state"] == "AWAITING SEND"
    assert row["carrier"] == "CMA CGM"
    assert row["quoted_by"] == "Alexandra Hernandez"
    assert row["rate"] == 3600.0
    assert row["requested_at_pt"] == "08:00 PT"
    assert row["quoted_at_et"] == "11:00 ET"


def test_compute_dod_active_conversations_sort_pending_first():
    """Sort priority: AWAITING SEND > QUOTED > AWAITING. Two rows in
    different states should land in that order regardless of input
    order, so the email's most-actionable rows are at the top."""
    prev: dict = {}
    curr = [
        _r("quoted-only", status="Q&L", quoted=True,
           carrier_quoted="ONE", ol_rate=420.0,
           response_timestamp="2026-04-26T17:00:00+00:00"),
        _r("pending", status="PENDING", quoted=True,
           carrier_quoted="MSC", ol_rate=2400.0,
           response_timestamp="2026-04-26T18:00:00+00:00"),
    ]
    dod = core.compute_dod(prev, curr)
    active = dod["active_conversations"]
    # Q&L doesn't go into active_conversations (it's a terminal state) —
    # only PENDING and the awaiting-quote case do.
    assert [c["state"] for c in active] == ["AWAITING SEND"]


def test_compute_dod_active_conversations_includes_awaiting():
    """A brand-new request with no quote yet should land in
    active_conversations as 'AWAITING' so it's still visible without
    needing a separate New rate asks table."""
    prev: dict = {}
    curr = [_r("brandnew", status="PENDING", quoted=False)]
    dod = core.compute_dod(prev, curr)
    active = dod["active_conversations"]
    assert len(active) == 1
    assert active[0]["state"] == "AWAITING QUOTE"
    assert active[0]["carrier"] == "—"
    assert active[0]["rate"] is None


# ── rate_trends ───────────────────────────────────────────────────────


def test_rate_trends_excludes_pairs_below_min_pct():
    # Two MSC.Shanghai datapoints with 0% movement → excluded.
    requests = [
        _r("a", carrier_quoted="MSC", destination="Shanghai",
           ol_rate="$2400", request_date="2026-04-20"),
        _r("b", carrier_quoted="MSC", destination="Shanghai",
           ol_rate="$2400", request_date="2026-04-25"),
    ]
    out = core.rate_trends(requests)
    assert out == []


def test_rate_trends_returns_movers_sorted_by_abs_pct():
    requests = [
        # MSC.Shanghai: 2400 -> 2640 = +10%
        _r("a", carrier_quoted="MSC", destination="Shanghai",
           ol_rate="$2400", request_date="2026-04-20"),
        _r("b", carrier_quoted="MSC", destination="Shanghai",
           ol_rate="$2640", request_date="2026-04-25"),
        # ZIM.Tokyo: 3000 -> 1500 = -50%
        _r("c", carrier_quoted="ZIM", destination="Tokyo",
           ol_rate="$3000", request_date="2026-04-20"),
        _r("d", carrier_quoted="ZIM", destination="Tokyo",
           ol_rate="$1500", request_date="2026-04-25"),
    ]
    out = core.rate_trends(requests)
    assert len(out) == 2
    # Sorted by absolute pct change desc: ZIM first.
    assert out[0]["carrier"] == "ZIM"
    assert out[1]["carrier"] == "MSC"
    assert out[0]["pct_change"] == -50.0
    assert out[1]["pct_change"] == 10.0


def test_rate_trends_skips_singletons_and_unparseable():
    requests = [
        _r("a", carrier_quoted="MSC", destination="Tokyo",
           ol_rate="$2400", request_date="2026-04-20"),  # singleton — skip
        _r("b", carrier_quoted="MSC", destination="Hamburg",
           ol_rate="not-a-rate", request_date="2026-04-20"),  # unparseable — skip
    ]
    out = core.rate_trends(requests)
    assert out == []


def test_rate_trends_skips_when_prior_avg_is_zero():
    """Defensive — can't divide by zero; pair gets excluded."""
    requests = [
        _r("a", carrier_quoted="MSC", destination="Tokyo",
           ol_rate="$0", request_date="2026-04-20"),
        _r("b", carrier_quoted="MSC", destination="Tokyo",
           ol_rate="$2400", request_date="2026-04-25"),
    ]
    out = core.rate_trends(requests)
    assert out == []


# ── aggregate_lanes / aggregate_carriers ──────────────────────────────


def test_aggregate_lanes_counts_per_lane():
    lanes = core.aggregate_lanes([
        _r("a", origin="Oakland", destination="Shanghai", status="WIN"),
        _r("b", origin="Oakland", destination="Shanghai", status="Q&L", quoted=True),
        _r("c", origin="Oakland", destination="Tokyo",    status="WIN"),
    ])
    assert "Oakland → Shanghai" in lanes
    assert lanes["Oakland → Shanghai"]["requests"] == 2
    assert lanes["Oakland → Shanghai"]["wins"] == 1
    assert lanes["Oakland → Shanghai"]["quoted_lost"] == 1


def test_aggregate_carriers_counts_per_carrier():
    out = core.aggregate_carriers([
        _r("a", carrier_won="MSC", carrier_quoted="MSC", status="WIN"),
        _r("b", carrier_quoted="MSC", status="Q&L", quoted=True),
        _r("c", carrier_won="ZIM", carrier_quoted="ZIM", status="WIN"),
    ])
    # Carriers keyed by name. MSC has 1 win + 1 loss.
    assert "MSC" in out
    assert out["MSC"]["wins"] == 1


def test_aggregate_carriers_skips_na_and_empty_carriers():
    """Defensive — "N/A" and "" must not produce phantom carrier rows."""
    out = core.aggregate_carriers([
        _r("a", carrier_quoted="N/A"),
        _r("b", carrier_quoted=""),
        _r("c", carrier_quoted="MSC", status="WIN", carrier_won="MSC"),
    ])
    assert "N/A" not in out
    assert "" not in out
    assert "MSC" in out


# ── biz_hours_between edge cases ──────────────────────────────────────


def test_biz_hours_between_returns_none_for_invalid_inputs():
    assert core.biz_hours_between(None, None) is None


def test_clock_hours_between_simple():
    a = datetime(2026, 4, 26, 10, 0, tzinfo=timezone.utc)
    b = datetime(2026, 4, 26, 12, 30, tzinfo=timezone.utc)
    got = core.clock_hours_between(a, b)
    assert got is not None
    assert abs(got - 2.5) < 0.01


def test_clock_hours_between_handles_none():
    assert core.clock_hours_between(None, None) is None


# ── parse_iso edge cases ──────────────────────────────────────────────


def test_parse_iso_returns_none_on_garbage():
    assert core.parse_iso("not a timestamp") is None
    assert core.parse_iso(None) is None
    assert core.parse_iso("") is None


def test_parse_iso_round_trips_z_suffix():
    dt = core.parse_iso("2026-04-26T12:00:00Z")
    assert dt is not None
    assert dt.tzinfo is not None
    assert dt.year == 2026 and dt.hour == 12


# ── now_utc / fmt_pt / fmt_et basic sanity ────────────────────────────


def test_now_utc_is_aware():
    n = core.now_utc()
    assert n.tzinfo is not None


def test_fmt_pt_handles_none():
    assert core.fmt_pt(None) == "—"


def test_fmt_et_handles_none():
    assert core.fmt_et(None) == "—"


# ── persist_daily_snapshot / load_previous_snapshot ───────────────────


def test_persist_daily_snapshot_writes_compact_payload(tmp_path):
    """One file per day, rich enough for both compute_dod (row_state)
    and trend analysis (summary)."""
    data = {
        "requests": [
            {"request_id": "a", "status": "WIN", "quoted": True,
             "has_send": True, "carrier_won": "MSC", "mdolx_ref": "X1",
             "response_timestamp": "2026-04-28T10:00:00Z"},
            {"request_id": "b", "status": "Q&L", "quoted": True,
             "has_send": False, "carrier_won": None, "mdolx_ref": None,
             "response_timestamp": "2026-04-26T15:00:00Z"},
        ],
        "summary": {"wins": 1, "quoted_lost": 1},
    }
    snaps = tmp_path / "daily_snapshots"
    out = core.persist_daily_snapshot(data, snaps, today_iso="2026-04-28")
    assert out.exists()
    payload = json.loads(out.read_text())
    assert payload["date"] == "2026-04-28"
    assert "row_state" in payload
    assert payload["row_state"]["a"]["status"] == "WIN"
    assert payload["row_state"]["b"]["status"] == "Q&L"
    assert payload["summary"]["wins"] == 1


def test_load_previous_snapshot_returns_strictly_prior_date(tmp_path):
    """Today's snapshot is excluded — load_previous_snapshot returns the
    most recent snapshot whose date is < today."""
    snaps = tmp_path / "daily_snapshots"
    snaps.mkdir()
    (snaps / "2026-04-26.json").write_text('{"date": "2026-04-26", "row_state": {}, "summary": {}}')
    (snaps / "2026-04-27.json").write_text('{"date": "2026-04-27", "row_state": {"x": {}}, "summary": {}}')
    (snaps / "2026-04-28.json").write_text('{"date": "2026-04-28", "row_state": {"y": {}}, "summary": {}}')

    prev = core.load_previous_snapshot(snaps, today_iso="2026-04-28")
    assert prev is not None
    assert prev["date"] == "2026-04-27"  # NOT 2026-04-28
    assert "x" in prev["row_state"]


def test_load_previous_snapshot_returns_none_on_first_run(tmp_path):
    """No prior snapshots → None (caller falls back to today_events
    in the email template)."""
    snaps = tmp_path / "daily_snapshots"
    # Don't create the dir — first-run case.
    assert core.load_previous_snapshot(snaps, today_iso="2026-04-28") is None


# ── per-carrier loss-reason distribution ─────────────────────────────


def test_aggregate_carriers_captures_loss_reason_distribution():
    """Per-carrier loss_reasons dict + summary string. PR #11 surfaces
    'why we lost' attribution in the carrier scoreboard so reps can
    see at a glance whether MSC's losses are price-driven or
    ETD-driven."""
    requests = [
        # MSC: 1 WIN, 3 Q&L (2 PRICE, 1 ETD_MISS)
        {"request_id": "w1", "status": "WIN", "carrier_won": "MSC",
         "carrier_quoted": "MSC", "destination": "Shanghai",
         "teu_requested": 2, "teu_won": 2},
        {"request_id": "l1", "status": "Q&L", "carrier_quoted": "MSC",
         "destination": "Shanghai", "loss_reason": "PRICE",
         "teu_requested": 2, "quoted": True},
        {"request_id": "l2", "status": "Q&L", "carrier_quoted": "MSC",
         "destination": "Tokyo", "loss_reason": "PRICE",
         "teu_requested": 4, "quoted": True},
        {"request_id": "l3", "status": "Q&L", "carrier_quoted": "MSC",
         "destination": "Busan", "loss_reason": "ETD_MISS",
         "teu_requested": 2, "quoted": True},
    ]
    out = core.aggregate_carriers(requests)
    msc = out["MSC"]
    assert msc["wins"] == 1
    assert msc["losses"] == 3
    assert msc["loss_reasons"] == {"PRICE": 2, "ETD_MISS": 1}
    # Sorted DESC by count: PRICE (2) first, then ETD_MISS (1).
    assert msc["loss_reason_summary"] == "2 PRICE, 1 ETD_MISS"


# ── period trends + pricing levels (PR #19) ──────────────────────────


def test_compute_period_trends_handles_no_snapshots(tmp_path):
    """Empty snapshots dir → all blocks present, all marked insufficient."""
    out = core.compute_period_trends(tmp_path / "daily_snapshots", today=core.date(2026, 4, 28))
    assert "wow" in out and "mom" in out and "ytd" in out
    assert out["wow"]["sufficient"] is False
    assert out["mom"]["sufficient"] is False
    assert out["wow"]["n_days_have"] == 0
    assert out["wow"]["n_days_need"] == 7


def test_compute_period_trends_wow_with_full_history(tmp_path):
    """7+7 days of snapshots → WoW current vs prior, end-of-period values.

    Each snapshot stores a CUMULATIVE running total (aggregate_summary
    output), so the period aggregator takes the LAST snapshot's value
    in each window — not a sum (that would double-count cumulative
    metrics).
    """
    snaps_dir = tmp_path / "daily_snapshots"
    snaps_dir.mkdir()
    today = core.date(2026, 4, 28)
    # 14 daily snapshots: prior 7 (days -13..-7) + current 7 (days -6..0)
    # End-of-prior-week running total wins=7; end-of-current-week wins=14.
    for i in range(14):
        d = today - core.timedelta(days=13 - i)
        cumulative_wins = i + 1  # wins grows by 1/day
        payload = {
            "date": d.isoformat(),
            "row_state": {},
            "summary": {
                "wins": cumulative_wins,
                "win_rate": 50.0 if i >= 7 else 25.0,
                "quoted_lost": cumulative_wins,
                "not_quoted": 0, "pending_hilmar": 0,
                "quote_rate": 50.0,
                "teu_won": cumulative_wins * 2, "teu_requested": cumulative_wins * 4,
            },
        }
        (snaps_dir / f"{d.isoformat()}.json").write_text(json.dumps(payload))

    out = core.compute_period_trends(snaps_dir, today=today)
    wow = out["wow"]
    assert wow["sufficient"] is True
    # End-of-current-period (Apr 28) running total = 14
    assert wow["current"]["wins"] == 14
    # End-of-prior-period (Apr 21) running total = 7
    assert wow["prior"]["wins"] == 7
    # Delta = (14 - 7) / 7 = +100%
    assert wow["delta"]["wins"] == 100.0
    # win_rate is a percentage — delta is in pp, not %.
    assert wow["delta"]["win_rate"] == 25.0  # 50 - 25 = +25pp


def test_compute_period_trends_zero_baseline_uses_new_sentinel(tmp_path):
    """When prior period had 0 wins (cumulative) and current has wins,
    delta should be the string sentinel 'new', NOT inf% — keeps the
    email rendering sane and the math meaningful."""
    snaps_dir = tmp_path / "daily_snapshots"
    snaps_dir.mkdir()
    today = core.date(2026, 4, 28)
    # 14 daily snapshots: prior 7 days = 0 wins (no activity yet),
    # current 7 days = wins climb 1..7 cumulatively (end-of-Apr-28 total = 7).
    for i in range(14):
        d = today - core.timedelta(days=13 - i)
        cumulative_wins = (i - 6) if i >= 7 else 0
        payload = {
            "date": d.isoformat(),
            "row_state": {},
            "summary": {
                "wins": cumulative_wins,
                "win_rate": 25.0 if cumulative_wins else 0.0,
                "quoted_lost": 1, "not_quoted": 0, "pending_hilmar": 0,
                "quote_rate": 50.0,
                "teu_won": cumulative_wins * 2, "teu_requested": 4,
            },
        }
        (snaps_dir / f"{d.isoformat()}.json").write_text(json.dumps(payload))
    out = core.compute_period_trends(snaps_dir, today=today)
    wow = out["wow"]
    # End-of-current-period running total = 7 (the last day).
    assert wow["current"]["wins"] == 7
    # End-of-prior-period running total = 0.
    assert wow["prior"]["wins"] == 0
    # Sentinel rather than inf — renderer can show "(new)".
    assert wow["delta"]["wins"] == "new"


def test_compute_period_trends_does_not_sum_cumulative_metrics(tmp_path):
    """REGRESSION: snapshot summaries are CUMULATIVE running totals
    (aggregate_summary output). If the period aggregator sums them
    across days, you get nonsense like wins=70 from 7 cumulative
    snapshots of wins=10. Should take last (end-of-period) value."""
    snaps_dir = tmp_path / "daily_snapshots"
    snaps_dir.mkdir()
    today = core.date(2026, 4, 28)
    # 7 snapshots, each with cumulative wins=10 (today's running total
    # back-projected to every day). End-of-period value should be 10,
    # NOT 70 (= 7 * 10, the broken-sum answer).
    for i in range(7):
        d = today - core.timedelta(days=6 - i)
        payload = {
            "date": d.isoformat(),
            "row_state": {},
            "summary": {
                "wins": 10, "quoted_lost": 22, "not_quoted": 9,
                "pending_hilmar": 4, "win_rate": 24.4, "quote_rate": 80.0,
                "teu_won": 16, "teu_requested": 154,
            },
        }
        (snaps_dir / f"{d.isoformat()}.json").write_text(json.dumps(payload))
    out = core.compute_period_trends(snaps_dir, today=today)
    wow = out["wow"]
    # End-of-period (NOT sum) — matches the actual running total.
    assert wow["current"]["wins"] == 10, "expected end-of-period wins=10, not sum=70"
    assert wow["current"]["quoted_lost"] == 22
    assert wow["current"]["teu_won"] == 16
    # YTD also takes end-of-period — same numbers as today.
    assert out["ytd"]["current"]["wins"] == 10


def test_compute_pricing_levels_flags_expensive_and_cheap_lanes():
    """Per-lane median + our-quote-vs-median classification."""
    requests = [
        # MSC.Shanghai: rates 2000, 2200, 2400 → median 2200
        # Latest = 2400 → +9% (just under threshold, not flagged)
        {"carrier_quoted": "MSC", "destination": "Shanghai", "ol_rate": "$2000",
         "response_timestamp": "2026-04-20T10:00:00Z"},
        {"carrier_quoted": "MSC", "destination": "Shanghai", "ol_rate": "$2200",
         "response_timestamp": "2026-04-22T10:00:00Z"},
        {"carrier_quoted": "MSC", "destination": "Shanghai", "ol_rate": "$2400",
         "response_timestamp": "2026-04-25T10:00:00Z"},
        # ZIM.Tokyo: rates 1000, 2000 → median 1500. Latest = 2000 → +33% expensive
        {"carrier_quoted": "ZIM", "destination": "Tokyo", "ol_rate": "$1000",
         "response_timestamp": "2026-04-20T10:00:00Z"},
        {"carrier_quoted": "ZIM", "destination": "Tokyo", "ol_rate": "$2000",
         "response_timestamp": "2026-04-25T10:00:00Z"},
        # ONE.Busan: rates 5000, 3000 → median 4000. Latest = 3000 → -25% cheap
        {"carrier_quoted": "ONE", "destination": "Busan", "ol_rate": "$5000",
         "response_timestamp": "2026-04-20T10:00:00Z"},
        {"carrier_quoted": "ONE", "destination": "Busan", "ol_rate": "$3000",
         "response_timestamp": "2026-04-25T10:00:00Z"},
    ]
    out = core.compute_pricing_levels(requests)
    by_key = {(p["carrier"], p["destination"]): p for p in out["per_lane"]}
    assert by_key[("MSC", "Shanghai")]["median"] == 2200
    assert by_key[("MSC", "Shanghai")]["latest"] == 2400
    # Expensive list: ZIM Tokyo at +33%
    assert any(p["carrier"] == "ZIM" and p["destination"] == "Tokyo" for p in out["expensive"])
    # Cheap list: ONE Busan at -25%
    assert any(p["carrier"] == "ONE" and p["destination"] == "Busan" for p in out["cheap"])


def test_compute_pricing_levels_skips_single_quote_lanes():
    """Lanes with only one rate quoted → can't compute meaningful median;
    omitted from output entirely (no expensive/cheap classification)."""
    requests = [
        {"carrier_quoted": "MSC", "destination": "Shanghai", "ol_rate": "$2000",
         "response_timestamp": "2026-04-20T10:00:00Z"},
    ]
    out = core.compute_pricing_levels(requests, min_quotes_per_lane=2)
    assert out["per_lane"] == []
    assert out["expensive"] == []
    assert out["cheap"] == []


def test_sparkline_renders_unicode_blocks_for_series():
    """Sparkline maps 0→▁, max→█. Email-client safe (no images, no JS)."""
    out = core.sparkline([1, 2, 3, 4, 5, 6, 7, 8])
    assert len(out) == 8
    assert out[0] == "▁"
    assert out[-1] == "█"


def test_sparkline_handles_none_values_and_empty():
    """Gaps render as space; empty input → empty string."""
    out = core.sparkline([1, None, 3])
    assert " " in out
    assert core.sparkline([]) == ""
    assert core.sparkline([None, None]) == ""


# ── status_as_of + backfill (PR #20) ────────────────────────────────


def test_status_as_of_no_history_uses_current_status():
    """Row with no status_history → use current status as the as-of value."""
    row = {
        "request_id": "r1", "request_date": "2026-04-15", "status": "WIN",
        "response_timestamp": "2026-04-15T15:00:00Z",
    }
    assert core.status_as_of(row, core.date(2026, 4, 20)) == "WIN"


def test_status_as_of_returns_none_before_request_date():
    """Rows that didn't exist yet (request_date > as_of) → None.
    Caller filters these out before aggregating."""
    row = {"request_id": "r1", "request_date": "2026-04-20", "status": "WIN"}
    assert core.status_as_of(row, core.date(2026, 4, 15)) is None


def test_status_as_of_walks_history():
    """Status walks the history to the latest entry on or before as_of."""
    row = {
        "request_id": "r1",
        "request_date": "2026-04-15",
        "status": "WIN",  # current state
        "status_history": [
            {"at": "2026-04-15T15:00:00Z", "from": "PENDING", "to": "Q&L", "reason": ""},
            {"at": "2026-04-20T09:00:00Z", "from": "Q&L", "to": "WIN", "reason": ""},
        ],
        "response_timestamp": "2026-04-15T15:00:00Z",
    }
    # On Apr 16, only the Q&L transition has happened.
    assert core.status_as_of(row, core.date(2026, 4, 16)) == "Q&L"
    # On Apr 20, the WIN transition has happened.
    assert core.status_as_of(row, core.date(2026, 4, 20)) == "WIN"
    # On Apr 25, still WIN (no later entries).
    assert core.status_as_of(row, core.date(2026, 4, 25)) == "WIN"


def test_synthesize_snapshot_excludes_future_rows():
    """Rows with request_date after as_of are filtered out — they
    didn't exist on that day."""
    requests = [
        {"request_id": "old", "request_date": "2026-04-10", "status": "WIN",
         "teu_requested": 2, "teu_won": 2, "destination": "X"},
        {"request_id": "future", "request_date": "2026-04-20", "status": "WIN",
         "teu_requested": 4, "teu_won": 4, "destination": "Y"},
    ]
    snap = core.synthesize_snapshot_for_date(requests, core.date(2026, 4, 15))
    # Only "old" exists by Apr 15 — "future" is excluded.
    assert "old" in snap["row_state"]
    assert "future" not in snap["row_state"]
    assert snap["summary"]["wins"] == 1
    assert snap["_synthesized"] is True


def test_backfill_daily_snapshots_skips_existing_unless_overwrite(tmp_path):
    """Today's real snapshot must not be silently replaced by a backfill —
    overwrite=False (default) skips existing files."""
    snaps = tmp_path / "daily_snapshots"
    snaps.mkdir()
    # Pre-existing real snapshot for Apr 28
    real_payload = {"date": "2026-04-28", "_synthesized": False, "summary": {}}
    (snaps / "2026-04-28.json").write_text(json.dumps(real_payload))

    requests = [
        {"request_id": "r1", "request_date": "2026-04-25", "status": "WIN",
         "teu_requested": 2, "teu_won": 2, "destination": "X"},
    ]
    written = core.backfill_daily_snapshots(
        requests, snaps,
        start_date=core.date(2026, 4, 26), end_date=core.date(2026, 4, 28),
    )
    # 2026-04-28 already existed → skipped; 26+27 written.
    assert written == 2
    # Real snapshot preserved (still _synthesized=False).
    real = json.loads((snaps / "2026-04-28.json").read_text())
    assert real.get("_synthesized") is False
    # Backfilled ones tagged _synthesized=True.
    syn = json.loads((snaps / "2026-04-27.json").read_text())
    assert syn.get("_synthesized") is True


# ─── Per-lane sparklines (PR #27) ─────────────────────────────────────


def test_compute_lane_activity_sparklines_basic():
    """Each lane gets a 14-day request sparkline keyed by Origin → Dest."""
    today = core.date(2026, 4, 28)
    requests = [
        # Oakland → Shanghai: 3 requests last week, including 1 win
        {"request_timestamp": "2026-04-22T10:00Z", "destination": "Shanghai",
         "status": "Q&L"},
        {"request_timestamp": "2026-04-24T10:00Z", "destination": "Shanghai",
         "status": "WIN"},
        {"request_timestamp": "2026-04-26T10:00Z", "destination": "Shanghai",
         "status": "Q&L"},
        # Oakland → Tokyo: 1 request earlier today
        {"request_timestamp": "2026-04-28T08:00Z", "destination": "Tokyo",
         "status": "PENDING"},
        # Oakland → Dubai: outside window (16 days back)
        {"request_timestamp": "2026-04-12T10:00Z", "destination": "Dubai",
         "status": "WIN"},
    ]
    out = core.compute_lane_activity_sparklines(requests, days=14, today=today)
    assert "Oakland → Shanghai" in out
    assert "Oakland → Tokyo" in out
    # Dubai outside window → not in output
    assert "Oakland → Dubai" not in out
    shanghai = out["Oakland → Shanghai"]
    assert shanghai["n_total"] == 3
    assert shanghai["n_wins"] == 1
    assert shanghai["days"] == 14
    assert len(shanghai["sparkline_total"]) == 14


def test_compute_lane_activity_sparklines_empty_input():
    """No requests → empty dict, never crashes."""
    out = core.compute_lane_activity_sparklines([], days=14)
    assert out == {}


def test_compute_lane_activity_sparklines_skips_rows_without_timestamp():
    """Rows missing both request_timestamp and response_timestamp are
    skipped (cant pin to a day)."""
    today = core.date(2026, 4, 28)
    requests = [
        {"destination": "Shanghai", "status": "WIN"},  # no timestamp
        {"request_timestamp": "2026-04-25T10:00Z", "destination": "Shanghai",
         "status": "WIN"},
    ]
    out = core.compute_lane_activity_sparklines(requests, days=14, today=today)
    assert out["Oakland → Shanghai"]["n_total"] == 1



# ─── Schema fields (PR #28): equipment_size / rate_per_feu / trade_region / validity_window ───


def test_equipment_size_basic_40hc():
    assert core.equipment_size("2×40'HC") == "40HC"


def test_equipment_size_handles_reefer_alias():
    assert core.equipment_size("3×40' Reefer") == "40RF"


def test_equipment_size_mixed_returns_plus_separated():
    out = core.equipment_size("2×20'DV + 1×40'HC")
    assert "20" in out and "40HC" in out


def test_equipment_size_handles_none_and_empty():
    assert core.equipment_size(None) is None
    assert core.equipment_size("") is None


def test_parse_rate_per_feu_doubles_when_quoted_per_20():
    """$1200/20 → 2400 per FEU."""
    rpf = core.parse_rate_per_feu("$1200/20'", containers="2×20'DV")
    assert rpf == 2400.0


def test_parse_rate_per_feu_passthrough_for_40():
    rpf = core.parse_rate_per_feu("$2400 per 40HC", containers="2×40'HC")
    assert rpf == 2400.0


def test_parse_rate_per_feu_defaults_to_40_when_no_size_signal():
    """No size info → assume per-40 (most common rate-desk default)."""
    assert core.parse_rate_per_feu("$2400") == 2400.0


def test_parse_rate_per_feu_returns_none_for_unparseable():
    assert core.parse_rate_per_feu(None) is None
    assert core.parse_rate_per_feu("") is None
    assert core.parse_rate_per_feu("call for rate") is None


def test_trade_region_china():
    assert core.trade_region("Shanghai") == "China"
    assert core.trade_region("ningbo, CN") == "China"


def test_trade_region_japan_and_korea():
    assert core.trade_region("Yokohama") == "Japan"
    assert core.trade_region("Busan") == "Korea"


def test_trade_region_unknown_returns_other():
    assert core.trade_region("Mars") == "Other"
    assert core.trade_region(None) == "Other"


def test_parse_validity_window_basic():
    body = "Rate valid 5/15-5/31. Please confirm."
    assert core.parse_validity_window(body) == "5/15-5/31"


def test_parse_validity_window_through_phrasing():
    assert core.parse_validity_window("rate valid through 5/31") == "5/31"


def test_parse_validity_window_expires_phrasing():
    assert core.parse_validity_window("Expires 5/31/2026") == "5/31/2026"


def test_parse_validity_window_returns_none_when_absent():
    assert core.parse_validity_window("just a note about timing") is None
    assert core.parse_validity_window(None) is None



# ─── Banner stripping + signer rejection (PR #30 — data quality) ─────


def test_strip_external_banner_removes_outlook_caution_block():
    """Outlook external banners must NOT leak into the parser feed."""
    from hilmar.ingest import strip_external_banner
    sample = (
        "CAUTION: THIS EMAIL ORIGINATED FROM OUTSIDE OF OUR COMPANY. "
        "DO NOT CLICK LINKS OR OPEN ANY ATTACHMENTS UNLESS YOU "
        "RECOGNIZE THE SENDER AND KNOW THE CONTENT IS SAFE. "
        "I need two identical bookings 2x40HC Oakland to Shanghai."
    )
    out = strip_external_banner(sample)
    assert out is not None and "I need two identical bookings" in out
    assert "CAUTION" not in out
    assert "OUTSIDE OF OUR COMPANY" not in out


def test_strip_external_banner_passthrough_when_no_banner():
    from hilmar.ingest import strip_external_banner
    s = "1x40'HC Oakland to Shanghai needed for 5/15"
    assert strip_external_banner(s) == s


def test_strip_external_banner_handles_empty_or_none():
    from hilmar.ingest import strip_external_banner
    assert strip_external_banner(None) is None
    assert strip_external_banner("") == ""


def test_guess_teu_from_preview_strips_banner_before_parsing():
    """The extraction now happens AFTER banner stripping, so previews
    that lead with CAUTION still produce clean canonical strings."""
    from hilmar.ingest import guess_teu_from_preview
    preview = (
        "CAUTION: THIS EMAIL ORIGINATED FROM OUTSIDE OF OUR COMPANY. "
        "Customer needs 3x40'HC Oakland to Shanghai for 5/15."
    )
    count, teu, canonical = guess_teu_from_preview(preview)
    assert count == 3
    assert teu == 6
    assert canonical == "3-40' HC"


def test_is_shared_mailbox_label_catches_all_known_variants():
    """The pre-fix rejection set missed 'MBD Ocean Export Booking
    (Shared)' — the variant Outlook actually emits. The new helper
    matches case + variant tolerantly."""
    from hilmar.ingest import _is_shared_mailbox_label
    assert _is_shared_mailbox_label("MBD Ocean Export Booking") is True
    assert _is_shared_mailbox_label("MBD Ocean Export Booking (Shared)") is True
    assert _is_shared_mailbox_label("mbd ocean export booking (shared)") is True
    assert _is_shared_mailbox_label("MBD_OceanExportBookingShared") is True
    assert _is_shared_mailbox_label("MBD-Ocean-Export-Booking") is True
    assert _is_shared_mailbox_label("") is True
    assert _is_shared_mailbox_label(None) is True
    # Real human signers must NOT be rejected.
    assert _is_shared_mailbox_label("Ryan Dolan") is False
    assert _is_shared_mailbox_label("Linda Echevarria") is False
    assert _is_shared_mailbox_label("Carrie Murphy") is False


# ─── parse_rate accepts bare numbers (PR #33) ────────────────────────


def test_parse_rate_bare_number_no_dollar_sign():
    """Pre-fix the regex required '$' — but ingest stores ol_rate as
    bare numeric strings like '3500.0', so parse_rate returned None
    and the dashboard's Rate (per FEU) cells all rendered '—'."""
    assert core.parse_rate("3500.0") == 3500.0
    assert core.parse_rate("2400") == 2400.0


def test_parse_rate_still_handles_dollar_prefixed():
    assert core.parse_rate("$2400") == 2400.0
    assert core.parse_rate("$2,400.50") == 2400.50


def test_parse_rate_skips_small_ints_below_rate_floor():
    """Don't catch stray digits like '40' (FEU size) as a rate. Bare
    numbers must be ≥ 100 to qualify, OR have a $ prefix."""
    assert core.parse_rate("40 HC") is None
    assert core.parse_rate("$40") == 40.0  # explicit $ overrides floor
    assert core.parse_rate("$2,400 per 40HC") == 2400.0


def test_parse_rate_per_feu_works_on_bare_number_with_size():
    """Containers param drives the *2 multiplier when rate is per-20'."""
    assert core.parse_rate_per_feu("1200", containers="2x20'") == 2400.0
    assert core.parse_rate_per_feu("3500.0") == 3500.0


# ─── parse_rate accepts numeric (PR #37) ──────────────────────────────


def test_parse_rate_accepts_float_input():
    """ol_rate is stored as float in tracking-data-v2.json (e.g. 420.0).
    Pre-fix, the isinstance(str) check rejected this and rate_per_feu
    came back None for every WIN row → $0 in subject line / KPI."""
    assert core.parse_rate(420.0) == 420.0
    assert core.parse_rate(2400.5) == 2400.5


def test_parse_rate_accepts_int_input():
    assert core.parse_rate(2400) == 2400.0
    # 0 is a valid (if unusual) numeric input — return 0.0, not None.
    assert core.parse_rate(0) == 0.0


def test_parse_rate_per_feu_works_on_numeric_ol_rate():
    """Cooperation: ol_rate as float passes through parse_rate, then
    parse_rate_per_feu applies the size multiplier."""
    assert core.parse_rate_per_feu(420.0, containers="1-20'") == 840.0
    assert core.parse_rate_per_feu(3500.0, containers="1-40' HC") == 3500.0
