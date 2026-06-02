"""Tests for the smarter PRICE classifier (2026-06-02).

Before this change, ``core.decide_status`` labeled every Q&L row with
``etd_fit_days<5`` as ``PRICE`` — a catch-all that produced 94%-rate-driven
loss-mix readouts even when winning and losing medians cleared at the
same price on a lane. The new rule:

  - PRICE             — ``ol_rate > lane_winning_median * 1.05``
  - UNDIFFERENTIATED  — rate competitive OR no signal to call PRICE
  - ETD_MISS          — unchanged, ``etd_fit_days >= 5``

These tests exercise the new branches in BOTH ``scripts/core.py`` and
``src/hilmar/core.py``, plus ``compute_lane_winning_medians`` and a
parity scenario set.
"""
from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# scripts/core.py is the production mirror; src/hilmar/core.py is the canonical
# copy. Both must agree.
scripts_core = _load(SCRIPTS / "core.py", "scripts_core_smartprice_test")

from hilmar import core as hilmar_core  # noqa: E402

UTC = timezone.utc

# A timestamp comfortably past the 48h PENDING window so the row is Q&L.
QL_RESPONSE_TS = "2026-04-01T03:00:00Z"
NOW = datetime(2026, 4, 27, 13, 0, tzinfo=UTC)
LANE = "Oakland → Yokohama"


@pytest.fixture(params=[scripts_core, hilmar_core], ids=["scripts", "hilmar"])
def core(request):
    return request.param


def _ql_kwargs(**extra):
    """Base kwargs that produce a Q&L row (quoted, aged-out, no send/mdolx)."""
    base = dict(
        has_send=False,
        mdolx_ref=None,
        response_timestamp=QL_RESPONSE_TS,
        quoted=True,
        etd_fit_days=0,  # ETD OK so ETD_MISS doesn't pre-empt the PRICE branch
        now=NOW,
    )
    base.update(extra)
    return base


# ── PRICE: rate above winning median by >5% ─────────────────────────────

def test_price_when_ol_rate_clearly_above_winning_median(core):
    """5%+ premium = PRICE. The classic rate-driven loss."""
    d = core.decide_status(**_ql_kwargs(
        ol_rate=4000,
        lane=LANE,
        lane_winning_median={LANE: 3500.0},
    ))
    assert d.loss_reason == "PRICE", d.reason_detail


def test_price_threshold_just_above_5_pct(core):
    """Just barely above the 5% threshold — still PRICE."""
    # 3500 * 1.05 = 3675 → 3676 just clears
    d = core.decide_status(**_ql_kwargs(
        ol_rate=3676,
        lane=LANE,
        lane_winning_median={LANE: 3500.0},
    ))
    assert d.loss_reason == "PRICE"


# ── UNDIFFERENTIATED: rate competitive, or no signal ─────────────────────

def test_undifferentiated_when_rate_matches_winning_median(core):
    """Winning median == losing rate. NOT a price story.

    Before this change, this row labeled PRICE and contributed to the
    "94% rate-driven" actionable_mix readout. Now it's the honest
    UNDIFFERENTIATED bucket.
    """
    d = core.decide_status(**_ql_kwargs(
        ol_rate=3500,
        lane=LANE,
        lane_winning_median={LANE: 3500.0},
    ))
    assert d.loss_reason == "UNDIFFERENTIATED", d.reason_detail


def test_undifferentiated_at_threshold_boundary(core):
    """Exactly 5% above is NOT PRICE — only > 5%."""
    # 3500 * 1.05 = 3675 → exactly equal is NOT a PRICE call
    d = core.decide_status(**_ql_kwargs(
        ol_rate=3675,
        lane=LANE,
        lane_winning_median={LANE: 3500.0},
    ))
    assert d.loss_reason == "UNDIFFERENTIATED"


def test_undifferentiated_when_rate_below_winning_median(core):
    """OL underbid the winning rate and STILL lost — strong UNDIFFERENTIATED
    signal: clearly not a rate issue. Operator should investigate the
    email thread for what tipped the loss."""
    d = core.decide_status(**_ql_kwargs(
        ol_rate=3200,
        lane=LANE,
        lane_winning_median={LANE: 3500.0},
    ))
    assert d.loss_reason == "UNDIFFERENTIATED"


def test_undifferentiated_when_lane_has_no_winning_history(core):
    """Lane not in the lookup → no benchmark → UNDIFFERENTIATED."""
    d = core.decide_status(**_ql_kwargs(
        ol_rate=4000,
        lane="Oakland → Nowhere",
        lane_winning_median={LANE: 3500.0},  # different lane only
    ))
    assert d.loss_reason == "UNDIFFERENTIATED"


def test_undifferentiated_when_ol_rate_is_none(core):
    """No rate to compare → UNDIFFERENTIATED, never PRICE."""
    d = core.decide_status(**_ql_kwargs(
        ol_rate=None,
        lane=LANE,
        lane_winning_median={LANE: 3500.0},
    ))
    assert d.loss_reason == "UNDIFFERENTIATED"


def test_undifferentiated_when_lane_winning_median_is_none(core):
    """Caller didn't compute medians (backward-compat path) → never PRICE."""
    d = core.decide_status(**_ql_kwargs(
        ol_rate=4000,
        lane=LANE,
        lane_winning_median=None,
    ))
    assert d.loss_reason == "UNDIFFERENTIATED"


def test_undifferentiated_when_lane_param_is_none(core):
    """No lane to look up → never PRICE."""
    d = core.decide_status(**_ql_kwargs(
        ol_rate=4000,
        lane=None,
        lane_winning_median={LANE: 3500.0},
    ))
    assert d.loss_reason == "UNDIFFERENTIATED"


# ── ETD_MISS still wins regardless of price ─────────────────────────────

def test_etd_miss_preempts_price_branch(core):
    """ETD missed by 7d is the concrete cause, even if rate was also bad."""
    d = core.decide_status(**_ql_kwargs(
        etd_fit_days=7,
        ol_rate=4000,
        lane=LANE,
        lane_winning_median={LANE: 3500.0},
    ))
    assert d.loss_reason == "ETD_MISS"


# ── Backward compatibility — old calls (no new kwargs) still work ────────

def test_backward_compat_old_signature_returns_undifferentiated(core):
    """Existing callers that haven't migrated to pass lane_winning_median
    must still work — no crashes, fall through to UNDIFFERENTIATED on Q&L."""
    d = core.decide_status(
        has_send=False, mdolx_ref=None,
        response_timestamp=QL_RESPONSE_TS,
        quoted=True, etd_fit_days=0, now=NOW,
    )
    # Q&L (LOSS in scripts; "Q&L" in hilmar) — both fall through to UNDIFFERENTIATED
    assert d.loss_reason == "UNDIFFERENTIATED"


# ── ol_rate accepts both string and numeric forms ────────────────────────

@pytest.mark.parametrize("rate_input", [
    "$4000/40HC",   # string with $ prefix — both trees parse it
    4000,           # bare int — both trees handle via float() cast
    4000.0,         # bare float — both trees handle via float() cast
])
def test_ol_rate_accepts_string_and_numeric(core, rate_input):
    """ingest stores ol_rate as either a string or a bare number. Both must
    parse correctly into the PRICE/UNDIFFERENTIATED branch.

    NB: bare digit strings like "4000" (no $) are NOT included — scripts/
    core.parse_rate intentionally requires the $ prefix (its regex
    ``\\$\\s*...``); src/hilmar/core.parse_rate accepts both. That's a
    legacy difference NOT in scope for this PR. ingest writes both forms
    of typed values, never bare numeric strings."""
    d = core.decide_status(**_ql_kwargs(
        ol_rate=rate_input,
        lane=LANE,
        lane_winning_median={LANE: 3500.0},
    ))
    assert d.loss_reason == "PRICE", f"Failed for ol_rate={rate_input!r}: {d.reason_detail}"


# ── compute_lane_winning_medians ────────────────────────────────────────

def test_compute_lane_winning_medians_basic(core):
    rows = [
        {"status": "WIN", "lane": "Oakland → Yokohama", "ol_rate": 3400},
        {"status": "WIN", "lane": "Oakland → Yokohama", "ol_rate": 3500},
        {"status": "WIN", "lane": "Oakland → Yokohama", "ol_rate": 3600},
    ]
    medians = core.compute_lane_winning_medians(rows)
    assert medians == {"Oakland → Yokohama": 3500.0}


def test_compute_lane_winning_medians_excludes_lanes_below_min_wins(core):
    """Lanes with fewer than 3 WINs should NOT appear in the lookup —
    too little signal to trust a median."""
    rows = [
        # Yokohama has 3 WINs — included
        {"status": "WIN", "lane": "Oakland → Yokohama", "ol_rate": 3500},
        {"status": "WIN", "lane": "Oakland → Yokohama", "ol_rate": 3500},
        {"status": "WIN", "lane": "Oakland → Yokohama", "ol_rate": 3500},
        # Singapore has only 2 WINs — excluded
        {"status": "WIN", "lane": "Oakland → Singapore", "ol_rate": 2000},
        {"status": "WIN", "lane": "Oakland → Singapore", "ol_rate": 2000},
    ]
    medians = core.compute_lane_winning_medians(rows)
    assert "Oakland → Yokohama" in medians
    assert "Oakland → Singapore" not in medians


def test_compute_lane_winning_medians_ignores_non_wins(core):
    rows = [
        {"status": "WIN",  "lane": LANE, "ol_rate": 3500},
        {"status": "WIN",  "lane": LANE, "ol_rate": 3500},
        {"status": "WIN",  "lane": LANE, "ol_rate": 3500},
        {"status": "LOSS", "lane": LANE, "ol_rate": 9000},     # ignored
        {"status": "PENDING", "lane": LANE, "ol_rate": 9000},  # ignored
        {"status": "Q&L",  "lane": LANE, "ol_rate": 9000},     # ignored
    ]
    medians = core.compute_lane_winning_medians(rows)
    assert medians[LANE] == 3500.0


def test_compute_lane_winning_medians_even_count(core):
    """Even-count median = average of the two middles."""
    rows = [
        {"status": "WIN", "lane": LANE, "ol_rate": 3000},
        {"status": "WIN", "lane": LANE, "ol_rate": 3500},
        {"status": "WIN", "lane": LANE, "ol_rate": 4000},
        {"status": "WIN", "lane": LANE, "ol_rate": 4500},
    ]
    medians = core.compute_lane_winning_medians(rows)
    assert medians[LANE] == 3750.0  # (3500 + 4000) / 2


def test_compute_lane_winning_medians_handles_string_rates(core):
    rows = [
        {"status": "WIN", "lane": LANE, "ol_rate": "$3400/40HC"},
        {"status": "WIN", "lane": LANE, "ol_rate": "$3500/40HC"},
        {"status": "WIN", "lane": LANE, "ol_rate": "$3600/40HC"},
    ]
    medians = core.compute_lane_winning_medians(rows)
    assert medians[LANE] == 3500.0


def test_compute_lane_winning_medians_skips_invalid_rates(core):
    rows = [
        {"status": "WIN", "lane": LANE, "ol_rate": 3500},
        {"status": "WIN", "lane": LANE, "ol_rate": None},        # skipped
        {"status": "WIN", "lane": LANE, "ol_rate": 0},           # skipped (<=0)
        {"status": "WIN", "lane": LANE, "ol_rate": "no rate"},   # skipped
        {"status": "WIN", "lane": LANE, "ol_rate": 3500},
        {"status": "WIN", "lane": LANE, "ol_rate": 3500},
    ]
    medians = core.compute_lane_winning_medians(rows)
    assert medians[LANE] == 3500.0


def test_compute_lane_winning_medians_falls_back_to_origin_destination(core):
    """Rows missing `lane` should still bucket via origin/destination."""
    rows = [
        {"status": "WIN", "origin": "Oakland", "destination": "Yokohama", "ol_rate": 3500},
        {"status": "WIN", "origin": "Oakland", "destination": "Yokohama", "ol_rate": 3500},
        {"status": "WIN", "origin": "Oakland", "destination": "Yokohama", "ol_rate": 3500},
    ]
    medians = core.compute_lane_winning_medians(rows)
    assert medians == {"Oakland → Yokohama": 3500.0}


def test_compute_lane_winning_medians_empty_input(core):
    assert core.compute_lane_winning_medians([]) == {}
    assert core.compute_lane_winning_medians(None) == {}


# ── Cross-tree parity over 5+ representative scenarios ──────────────────

PARITY_SCENARIOS = [
    # 1. Rate above threshold → PRICE
    dict(ol_rate=4000, lane=LANE, lane_winning_median={LANE: 3500.0}),
    # 2. Rate competitive → UNDIFFERENTIATED
    dict(ol_rate=3500, lane=LANE, lane_winning_median={LANE: 3500.0}),
    # 3. No lane in lookup → UNDIFFERENTIATED
    dict(ol_rate=4000, lane="Oakland → Mystery", lane_winning_median={LANE: 3500.0}),
    # 4. No ol_rate → UNDIFFERENTIATED
    dict(ol_rate=None, lane=LANE, lane_winning_median={LANE: 3500.0}),
    # 5. ETD miss + rate above → ETD_MISS (concrete signal wins)
    dict(ol_rate=4000, lane=LANE, lane_winning_median={LANE: 3500.0}, etd_fit_days=7),
    # 6. Backward compat — no new kwargs at all → UNDIFFERENTIATED
    dict(),
    # 7. lane_winning_median empty dict → UNDIFFERENTIATED
    dict(ol_rate=4000, lane=LANE, lane_winning_median={}),
]


@pytest.mark.parametrize("extra_kwargs", PARITY_SCENARIOS)
def test_decide_status_parity_smart_price(extra_kwargs):
    """scripts/core.py and src/hilmar/core.py must produce the same
    loss_reason for identical inputs. The status string differs by design
    (LEGACY 'LOSS' vs STRICT 'Q&L') but the reason must agree."""
    kwargs = _ql_kwargs(**extra_kwargs)
    a = scripts_core.decide_status(**kwargs)
    b = hilmar_core.decide_status(**kwargs)
    assert a.loss_reason == b.loss_reason, (
        f"loss_reason drift for {extra_kwargs}: "
        f"scripts={a.loss_reason} hilmar={b.loss_reason}"
    )


def test_old_price_catchall_is_gone_in_both_trees():
    """Regression test: the exact bug that drove the "94% PRICE"
    distortion. A Q&L row with etd_fit_days<5 and no lane signal must
    NOT be PRICE in either tree."""
    kwargs = _ql_kwargs(etd_fit_days=2, ol_rate=None)
    assert scripts_core.decide_status(**kwargs).loss_reason != "PRICE"
    assert hilmar_core.decide_status(**kwargs).loss_reason != "PRICE"
