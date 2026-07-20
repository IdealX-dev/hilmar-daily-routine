"""Carrier of the Week must reflect who actually WON.

Regression for the 2026-07-20 weekly summary (week of Jul 13-17): CMA CGM was
crowned "🏆 Carrier of the Week" with 6 quotes / 0 wins / 0.0% win rate, even
though the week had a win (1 win, 4 TEU) that a DIFFERENT carrier took.

Root cause: `carrier_of_week` filtered candidates to `quotes >= 2` BEFORE
ranking. The actual winner had a single quote that week — the one that won — so
the >=2 floor benched it, leaving only 0-win carriers, and the most-active of
those (CMA) got the trophy. The fix: a win always qualifies (a win is the
strongest signal), rank by wins then TEU won, and on a genuine no-win week the
render relabels the box "Most Active Carrier" so a 0-win carrier is never
literally called the week's winner.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import gen_weekly_summary as GWS  # noqa: E402

UTC = timezone.utc

# now=Monday 2026-07-20 → the report week is the PREVIOUS Mon-Fri, 07-13..07-17.
MONDAY_2026_07_20 = datetime(2026, 7, 20, 9, 7, tzinfo=UTC)
IN_WEEK = "2026-07-15"   # a Wednesday inside the reported week


def _win(carrier, teu, rid):
    return {"request_id": rid, "request_date": IN_WEEK, "status": "WIN",
            "quoted": True, "carrier_won": carrier, "carrier_quoted": carrier,
            "teu_won": teu, "teu_requested": teu}


def _quote_lost(carrier, rid):
    return {"request_id": rid, "request_date": IN_WEEK, "status": "LOSS",
            "quoted": True, "carrier_quoted": carrier}


# ── unit: the selection logic ──────────────────────────────────────────────

def test_carrier_of_week_is_the_winner_not_the_most_active_loser():
    # THE bug scenario: one WIN on a single quote (MSC) + a carrier that quoted
    # six times and won nothing (CMA CGM). MSC must win the trophy.
    rows = [_win("MSC", 4, "w1")] + [_quote_lost("CMA CGM", f"l{i}") for i in range(6)]
    cow = GWS.carrier_of_week(rows)
    assert cow is not None
    assert cow["carrier"] == "MSC", (
        f"the actual winner (MSC, 1 win) must be Carrier of the Week, not the "
        f"0-win most-active quoter; got {cow['carrier']!r}"
    )
    assert cow["wins"] == 1 and cow["teu_won"] == 4


def test_carrier_of_week_never_has_zero_wins_when_the_week_has_a_win():
    rows = [_win("ONE", 2, "w1")] + [_quote_lost("MSC", f"l{i}") for i in range(5)]
    cow = GWS.carrier_of_week(rows)
    assert cow["wins"] >= 1, "COW must have >=1 win when the week had any win"


def test_carrier_of_week_ranks_by_wins_then_teu_won():
    # Two carriers with one win each; the one that won more TEU takes it.
    rows = [_win("MSC", 2, "w1"), _win("ONE", 8, "w2")] + \
           [_quote_lost("CMA CGM", f"l{i}") for i in range(9)]
    cow = GWS.carrier_of_week(rows)
    assert cow["carrier"] == "ONE" and cow["teu_won"] == 8


def test_carrier_of_week_attributes_the_win_to_carrier_won():
    # A WIN row whose carrier_quoted differs from carrier_won: the win belongs
    # to the carrier that actually won.
    rows = [{"request_id": "w1", "request_date": IN_WEEK, "status": "WIN",
             "quoted": True, "carrier_quoted": "MSC", "carrier_won": "ONE",
             "teu_won": 3, "teu_requested": 3}]
    cow = GWS.carrier_of_week(rows)
    assert cow["carrier"] == "ONE"


def test_carrier_of_week_none_when_no_carriers():
    assert GWS.carrier_of_week([]) is None
    assert GWS.carrier_of_week([{"request_id": "x", "status": "PENDING"}]) is None


# ── end-to-end: the rendered box ───────────────────────────────────────────

def _render(tmp_path, monkeypatch, rows):
    data = tmp_path / "tracking-data-v2.json"
    data.write_text(json.dumps({"requests": rows}), encoding="utf-8")
    monkeypatch.setattr(GWS, "DATA", data)
    monkeypatch.setattr(GWS, "REPORTS", tmp_path / "reports")
    rc = GWS.main(["--force"], now=MONDAY_2026_07_20)
    assert rc == 0
    return (tmp_path / "reports" / "weekly-summary.html").read_text(encoding="utf-8")


def test_win_week_renders_the_trophy_for_the_winner(tmp_path, monkeypatch):
    rows = [_win("MSC", 4, "w1")] + [_quote_lost("CMA CGM", f"l{i}") for i in range(6)]
    html = _render(tmp_path, monkeypatch, rows)
    assert "Carrier of the Week" in html
    # The winner is named; the 0-win most-active quoter is NOT the crowned one.
    assert "MSC" in html


def test_zero_win_week_is_relabeled_most_active_not_crowned(tmp_path, monkeypatch):
    rows = [_quote_lost("CMA CGM", f"l{i}") for i in range(4)]
    cow = GWS.carrier_of_week(rows)
    assert cow is not None and cow["wins"] == 0
    html = _render(tmp_path, monkeypatch, rows)
    assert "Most Active Carrier" in html
    assert "Carrier of the Week" not in html
