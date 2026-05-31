"""Tests for aggregate_loss_reasons — the book-wide "why did we lose" lens.

Added 2026-05-31 alongside the new function (per 2026-05-31 revenue audit:
the per-carrier loss_reasons mix has existed for months; the book-wide
rollup that powers the daily-email mix chart did not).

Tests both src/hilmar/core (canonical) and scripts/core (production mirror)
to lock parity. The function output drives client-facing numbers, so the
shape contract here is load-bearing.
"""
from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


# Import hilmar.core THE NORMAL WAY so coverage.py instruments it
# correctly — dynamic-loading at module scope (as test_core_parity.py
# does) makes pytest-cov miss the file. Discovered 2026-05-31 while
# wiring the loss-reason aggregator tests.
from hilmar import core as hilmar_core  # noqa: E402


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# scripts/core.py is the production mirror — it lives outside the
# `hilmar` package, so a normal `import` won't find it. Dynamic load is
# acceptable here because scripts/core.py is NOT what --cov=hilmar
# measures (it's the mirrored copy; src/hilmar/core.py is the canonical
# source of truth tests are written against).
scripts_core = _load(SCRIPTS / "core.py", "scripts_core_alr_test")

UTC = timezone.utc


def _row(*, status, loss_reason, response_ts=None, request_ts=None):
    """Minimal row shape consumed by aggregate_loss_reasons."""
    r = {"status": status}
    if loss_reason is not None:
        r["loss_reason"] = loss_reason
    if response_ts is not None:
        r["response_timestamp"] = response_ts
    if request_ts is not None:
        r["request_timestamp"] = request_ts
    return r


# Both modules tested for the same behaviour — parametrize over them.
@pytest.fixture(params=[scripts_core, hilmar_core], ids=["scripts", "hilmar"])
def core(request):
    return request.param


# ── shape contract ──────────────────────────────────────────────────────

def test_empty_input_returns_zeroes(core):
    out = core.aggregate_loss_reasons([])
    assert out["total"] == 0
    assert out["by_reason"] == {}
    assert out["ranked"] == []
    assert out["window_days"] is None
    assert out["actionable_mix"] == {
        "rate_driven": 0, "etd_driven": 0, "ol_silent": 0, "other": 0,
    }


def test_output_keys_are_present_even_when_empty(core):
    out = core.aggregate_loss_reasons([])
    # Renderers will index these — promise the keys exist.
    for k in ("total", "by_reason", "ranked", "window_days", "actionable_mix"):
        assert k in out


# ── basic counting + bucketing ─────────────────────────────────────────

def test_counts_only_loss_statuses(core):
    rows = [
        _row(status="WIN",     loss_reason=None),
        _row(status="PENDING", loss_reason=None),
        _row(status="LOSS",    loss_reason="PRICE"),
        _row(status="Q&L",     loss_reason="ETD_MISS"),
        _row(status="NQ",      loss_reason="NO_RESPONSE"),
    ]
    out = core.aggregate_loss_reasons(rows)
    assert out["total"] == 3
    assert out["by_reason"] == {"PRICE": 1, "ETD_MISS": 1, "NO_RESPONSE": 1}


def test_drops_rows_without_loss_reason(core):
    rows = [
        _row(status="LOSS", loss_reason="PRICE"),
        _row(status="LOSS", loss_reason=None),
        _row(status="LOSS", loss_reason=""),
    ]
    out = core.aggregate_loss_reasons(rows)
    assert out["total"] == 1
    assert out["by_reason"] == {"PRICE": 1}


def test_ranked_descending_by_count(core):
    rows = [
        _row(status="LOSS", loss_reason="PRICE"),
        _row(status="LOSS", loss_reason="PRICE"),
        _row(status="LOSS", loss_reason="PRICE"),
        _row(status="LOSS", loss_reason="ETD_MISS"),
        _row(status="LOSS", loss_reason="ETD_MISS"),
        _row(status="LOSS", loss_reason="OTHER"),
    ]
    out = core.aggregate_loss_reasons(rows)
    assert out["ranked"] == [("PRICE", 3), ("ETD_MISS", 2), ("OTHER", 1)]


# ── actionable_mix bucketing ────────────────────────────────────────────

def test_actionable_mix_buckets_correctly(core):
    """The 4 buckets drive Michael's "what to push next" decision:
       - rate_driven (PRICE) → push carriers
       - etd_driven (ETD_MISS) → push ops
       - ol_silent (NO_RESPONSE/RESPONSE_NO_RATE/SEND_NO_BOOKING) → push OL
       - other (OTHER/QUOTED_NOT_BOOKED/COVERED/DRAFT_ONLY) → no signal
    """
    rows = [
        _row(status="LOSS", loss_reason="PRICE"),
        _row(status="LOSS", loss_reason="PRICE"),
        _row(status="LOSS", loss_reason="ETD_MISS"),
        _row(status="LOSS", loss_reason="NO_RESPONSE"),
        _row(status="LOSS", loss_reason="RESPONSE_NO_RATE"),
        _row(status="LOSS", loss_reason="SEND_NO_BOOKING"),
        _row(status="LOSS", loss_reason="OTHER"),
        _row(status="LOSS", loss_reason="QUOTED_NOT_BOOKED"),
        _row(status="LOSS", loss_reason="COVERED"),
    ]
    out = core.aggregate_loss_reasons(rows)
    assert out["actionable_mix"] == {
        "rate_driven": 2,   # PRICE×2
        "etd_driven":  1,   # ETD_MISS
        "ol_silent":   3,   # NO_RESPONSE + RESPONSE_NO_RATE + SEND_NO_BOOKING
        "other":       3,   # OTHER + QUOTED_NOT_BOOKED + COVERED
    }


# ── window_days filtering ───────────────────────────────────────────────

def test_window_days_filters_by_response_timestamp(core):
    now = datetime(2026, 5, 31, 12, 0, tzinfo=UTC)
    rows = [
        _row(status="LOSS", loss_reason="PRICE",
             response_ts=(now - timedelta(days=15)).isoformat()),    # inside 30d
        _row(status="LOSS", loss_reason="ETD_MISS",
             response_ts=(now - timedelta(days=45)).isoformat()),    # outside 30d
        _row(status="LOSS", loss_reason="PRICE",
             response_ts=(now - timedelta(days=5)).isoformat()),     # inside 30d
    ]
    out = core.aggregate_loss_reasons(rows, window_days=30, now=now)
    assert out["total"] == 2
    assert out["by_reason"] == {"PRICE": 2}
    assert out["window_days"] == 30


def test_window_falls_back_to_request_timestamp_when_response_missing(core):
    now = datetime(2026, 5, 31, 12, 0, tzinfo=UTC)
    rows = [
        _row(status="LOSS", loss_reason="PRICE",
             request_ts=(now - timedelta(days=10)).isoformat()),     # no response_ts
        _row(status="LOSS", loss_reason="ETD_MISS",
             request_ts=(now - timedelta(days=60)).isoformat()),     # outside
    ]
    out = core.aggregate_loss_reasons(rows, window_days=30, now=now)
    assert out["by_reason"] == {"PRICE": 1}


def test_window_drops_rows_with_no_usable_timestamp(core):
    rows = [_row(status="LOSS", loss_reason="PRICE")]  # no timestamps
    out = core.aggregate_loss_reasons(rows, window_days=30,
                                       now=datetime(2026, 5, 31, tzinfo=UTC))
    assert out["total"] == 0


# ── cross-tree parity (locks the drift catcher) ─────────────────────────

def test_aggregate_loss_reasons_parity_realistic_mix():
    """Realistic data + 30d window — output must match across trees."""
    now = datetime(2026, 5, 31, 12, 0, tzinfo=UTC)
    rows = [
        _row(status="LOSS", loss_reason="PRICE",
             response_ts=(now - timedelta(days=2)).isoformat()),
        _row(status="LOSS", loss_reason="PRICE",
             response_ts=(now - timedelta(days=10)).isoformat()),
        _row(status="LOSS", loss_reason="ETD_MISS",
             response_ts=(now - timedelta(days=20)).isoformat()),
        _row(status="LOSS", loss_reason="NO_RESPONSE",
             response_ts=(now - timedelta(days=4)).isoformat()),
        _row(status="LOSS", loss_reason="SEND_NO_BOOKING",
             response_ts=(now - timedelta(days=8)).isoformat()),
        _row(status="LOSS", loss_reason="ETD_MISS",
             response_ts=(now - timedelta(days=45)).isoformat()),    # outside
        _row(status="WIN",  loss_reason=None),                       # not a loss
    ]
    a = scripts_core.aggregate_loss_reasons(rows, window_days=30, now=now)
    b = hilmar_core.aggregate_loss_reasons(rows, window_days=30, now=now)
    assert a == b, f"aggregate_loss_reasons drift:\n  scripts={a}\n  hilmar={b}"
