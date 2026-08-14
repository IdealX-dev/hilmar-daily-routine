"""The insights engine reported a 100% win rate, and the delta hid it.

THE BUG. `baselines._carrier_lane_winrates`, `baselines.compute` and
`insights.build_context` each decided which rows were "resolved" with

    r["status"] in ("WIN", "Q&L", "NQ")

Production stores the LEGACY form: `scripts/core.decide_status` returns
"LOSS" for both quoted-and-lost and never-quoted rows, and QC-041 enforces
that LEGACY is what gets written. So that tuple matched WINS AND NOTHING
ELSE — every loss was invisible to the insights engine, the decided set was
all-wins, and the win rate came out wins/wins = 100.0%.

WHY IT SURVIVED. `win_rate_delta_pp = today_win_rate - baselines.
win_rate_pct`, and BOTH sides used the same broken filter. Both computed
100.0%, so the delta was a flat, healthy-looking 0.0. Two alert classes were
disabled outright as a side effect: `win_rate_shift` and `carrier_lane_drop`
compare today against baseline, and neither can fire when both are pinned at
100%. Nothing in the system was in a position to notice.

WHERE IT LANDED. `insights.context_to_dict` feeds the Opus narrative prompt,
so the business advice in the daily email was written from a fabricated 0.0pp
delta.

THE SECOND BUG, fixed in the same commit because they interact. NQ was in
that denominator; the headline win rate is Wins/(Wins+Q&L) and never included
it. Correcting only the storage form would have swung the delta by whatever
NQ happened to be — and since the 2026-08-17 floor the report states plainly
that NQ rows are not counted, while this still counted them.

core.display_status' own docstring warned about the first bug: "Never compare
r['status'] == 'Q&L' directly — it'll silently miss legacy rows."
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from hilmar import baselines as B  # noqa: E402
from hilmar import insights as I  # noqa: E402

UTC = timezone.utc
NOW = datetime(2026, 8, 14, 13, 0, tzinfo=UTC)


def _row(status, *, quoted=None, days_ago=1, carrier="MSC", dest="Manila, PH"):
    r = {
        "status": status,
        "request_timestamp": (NOW - timedelta(days=days_ago)).isoformat(),
        "carrier_quoted": carrier,
        "destination": dest,
        "teu_requested": 2,
    }
    if quoted is not None:
        r["quoted"] = quoted
    return r


# ── is_decided: the predicate itself ──────────────────────────────────────

def test_legacy_quoted_and_lost_is_decided():
    """THE BUG. status=="LOSS" with quoted=True is a Quoted-and-Lost row in
    the form production actually writes. It must count."""
    assert B.is_decided({"status": "LOSS", "quoted": True}) is True


def test_strict_q_and_l_still_decided():
    """The STRICT form must keep working — some stored rows carry it."""
    assert B.is_decided({"status": "Q&L"}) is True


def test_win_is_decided_in_both_forms():
    assert B.is_decided({"status": "WIN"}) is True


def test_pending_is_not_decided():
    """PENDING is still alive — excluded from any win rate."""
    assert B.is_decided({"status": "PENDING"}) is False


def test_not_quoted_is_excluded_in_both_forms():
    """The headline is Wins/(Wins+Q&L). NQ has never been in it, in either
    storage form, and since the 2026-08-17 floor the report says so."""
    assert B.is_decided({"status": "NQ"}) is False
    assert B.is_decided({"status": "LOSS", "quoted": False}) is False


def test_insights_and_baselines_share_one_predicate():
    """Today's recomputation and the persisted baseline MUST use the same
    denominator. Two longhand copies is how they drifted identically for
    months — this asserts there is now exactly one function."""
    assert I._is_decided is B.is_decided


# ── The 100% regression, end to end ───────────────────────────────────────

def _legacy_book():
    """1 win, 3 quoted-and-lost, 1 never-quoted, 1 pending — all LEGACY,
    the form production writes. True headline rate: 1/(1+3) = 25%."""
    return [
        _row("WIN"),
        _row("LOSS", quoted=True),
        _row("LOSS", quoted=True),
        _row("LOSS", quoted=True),
        _row("LOSS", quoted=False),
        _row("PENDING"),
    ]


def test_baseline_win_rate_is_not_100_percent_on_legacy_rows():
    """The regression in one assertion: before the fix this returned 100.0
    because only the single WIN row survived the filter."""
    b = B.compute(_legacy_book(), now=NOW)
    assert b.rolling_14d.win_rate_pct == 25.0, (
        f"win_rate_pct={b.rolling_14d.win_rate_pct} — 100.0 means LEGACY "
        "losses are invisible again and the denominator is wins-only."
    )


def test_context_win_rate_matches_the_headline_denominator():
    """build_context's today_win_rate must equal Wins/(Wins+Q&L)."""
    rows = _legacy_book()
    ctx = I.build_context(
        tracking_data={"requests": rows, "summary": {}},
        baselines={"win_rate_pct": 25.0},
        now=NOW,
    )
    assert ctx.win_rate_delta_pp == 0.0, (
        f"delta={ctx.win_rate_delta_pp} — today's rate should be 25.0 "
        "against a 25.0 baseline."
    )


def test_delta_now_actually_moves_when_the_book_gets_worse():
    """THE POINT OF THE FIX. The old code pinned today AND baseline at
    100%, so the delta was permanently 0.0 and win_rate_shift could never
    fire. A genuinely worse book must now produce a negative delta."""
    worse = _legacy_book() + [_row("LOSS", quoted=True) for _ in range(4)]
    ctx = I.build_context(
        tracking_data={"requests": worse, "summary": {}},
        baselines={"win_rate_pct": 25.0},
        now=NOW,
    )
    assert ctx.win_rate_delta_pp is not None and ctx.win_rate_delta_pp < 0, (
        f"delta={ctx.win_rate_delta_pp} — a book that went from 1/4 to "
        "1/8 must show a drop, not a flat 0.0."
    )


def test_carrier_lane_winrates_see_legacy_losses():
    """Same bug, second site. A carrier that lost 3 of 4 must not read as
    100% — and a lane with only losses must appear at all, which it could
    not when losses were invisible."""
    rates = B._carrier_lane_winrates(_legacy_book())
    assert rates.get("MSC.Manila, PH") == 25.0, (
        f"carrier-lane rates={rates} — 100.0 or a missing key means "
        "LEGACY losses are invisible again."
    )


def test_a_carrier_that_only_loses_is_reported_not_dropped():
    """The old filter dropped all-loss carriers entirely, so a carrier
    losing everything was simply absent from the negotiation view."""
    rows = [
        _row("LOSS", quoted=True, carrier="ZIM"),
        _row("LOSS", quoted=True, carrier="ZIM"),
    ]
    rates = B._carrier_lane_winrates(rows)
    assert rates.get("ZIM.Manila, PH") == 0.0, (
        f"rates={rates} — a carrier losing every quote must show 0.0%, "
        "not vanish from the report."
    )
