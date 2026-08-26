"""One booking, one number — every counter in every report agrees.

Michael, 2026-08-24, on a screenshot of the weekly summary: "how are there
16 requests with 9 wins and 10 losses that would be 19 requests", then on
the 4-week trend: "your numbers and percentages are inaccurate and make no
sense" (12 requests / 17 wins / 175% quote rate), then "how more wins then
requests". The rule he set: count SHIPMENTS, not emails. An RFQ booked as
three shipments is "three requests to three wins", and "there are no
bookings without rfqs" — so the denominator expands with the numerator.

#223 implemented that rule in ONE function, gen_weekly_summary.analyze_week.
Verified on main at 8d53fc9, a row carrying three MDOLX refs read:

    weekly KPI tile ................ 3 wins   (booking_count)
    weekly Top Winning Lanes ....... 1 win    (by_lane[...]["wins"] += 1)
    weekly Carrier of the Week ..... 1 win    (by_c[c]["wins"] += 1)
    daily email win tile ........... 1 win    (len(day_wins))
    period-to-date summary ......... 1 win    (len(wins))
    dashboard "Confirmed Wins" ..... 1 booking (len(wins))

One booking, six numbers, in reports read side by side — the same
self-contradiction one level down. These tests fail if any counter drifts
back to counting rows.
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import core  # noqa: E402
import gen_email as GE  # noqa: E402
import gen_weekly_summary as W  # noqa: E402

#: One RFQ, booked as THREE shipments — the shape Michael described.
THREE_BOOKINGS = {
    "status": "WIN", "quoted": True,
    "request_date": "2026-08-18", "date": "2026-08-18",
    "lane": "Oakland → Osaka", "origin": "Oakland", "destination": "Osaka",
    "carrier_won": "CMA CGM", "carrier_quoted": "CMA CGM",
    "teu_won": 6, "teu_requested": 6,
    "mdolx_ref": "MDOLX261025",
    "mdolx_refs_all": ["MDOLX261025", "MDOLX261026", "MDOLX261027"],
    "booking_timestamp": "2026-08-19T15:00:00+00:00",
    "status_history": [{"from": "PENDING", "to": "WIN",
                        "at": "2026-08-19T15:00:00+00:00"}],
}

#: A quote that lost — one request, one loss, whatever refs it lacks.
ONE_LOSS = {
    "status": "LOSS", "quoted": True,
    "request_date": "2026-08-18", "date": "2026-08-18",
    "lane": "Oakland → Kobe", "origin": "Oakland", "destination": "Kobe",
    "carrier_quoted": "ONE", "teu_requested": 2,
}


def test_shipment_count_is_the_one_rule():
    assert core.shipment_count(THREE_BOOKINGS) == 3
    assert core.booking_count(THREE_BOOKINGS) == 3
    # Every non-win row is worth exactly one, whatever else is on it.
    assert core.shipment_count(ONE_LOSS) == 1
    assert core.booking_count(ONE_LOSS) == 0
    assert core.shipment_count({"status": "PENDING"}) == 1
    # A win with no MDOLX still happened — never zero.
    assert core.shipment_count({"status": "WIN"}) == 1


def test_period_summary_and_weekly_agree_on_the_same_rows():
    rows = [THREE_BOOKINGS, ONE_LOSS]
    ptd = core.aggregate_summary(rows)
    wk = W.analyze_week(rows, [THREE_BOOKINGS])
    assert ptd["wins"] == wk["wins"] == 3
    assert ptd["quoted_lost"] == wk["ql"] == 1
    assert ptd["total_entries"] == wk["total"] == 4
    assert ptd["win_rate"] == wk["win_rate"] == 75.0


def test_no_rate_can_exceed_one_hundred_percent():
    # The 175% quote rate. Both rates are inside one population now.
    ptd = core.aggregate_summary([THREE_BOOKINGS, ONE_LOSS])
    assert 0 <= ptd["win_rate"] <= 100
    assert 0 <= ptd["quote_rate"] <= 100
    # And wins can never exceed requests — "how more wins then requests".
    assert ptd["wins"] <= ptd["total_entries"]


def test_weekly_tiles_and_weekly_tables_agree():
    rows = [THREE_BOOKINGS, ONE_LOSS]
    wk = W.analyze_week(rows, [THREE_BOOKINGS])
    lanes = W.top_lanes_by_teu_won(rows)
    cow = W.carrier_of_week(rows, [THREE_BOOKINGS])
    assert sum(x["wins"] for x in lanes) == wk["wins"] == 3
    assert cow["wins"] == 3
    # quotes expands with wins, so this carrier's win_rate stays sane.
    assert cow["quotes"] >= cow["wins"]
    assert 0 <= cow["win_rate"] <= 100


def test_daily_email_tile_agrees_with_the_weekly():
    day = GE._today_summary([THREE_BOOKINGS], date(2026, 8, 19))
    assert day["wins"] == 3, "the daily win tile must count bookings"
    wk = W.analyze_week([THREE_BOOKINGS], [THREE_BOOKINGS])
    assert day["wins"] == wk["wins"]


def test_daily_tile_requests_expand_with_wins():
    # Arrived and booked the same day: three requests AND three wins, never
    # three wins against one request.
    same_day = dict(THREE_BOOKINGS, request_date="2026-08-19",
                    date="2026-08-19")
    day = GE._today_summary([same_day], date(2026, 8, 19))
    assert day["wins"] == 3
    assert day["total"] == 3
    assert day["wins"] <= day["total"]


def test_core_rollups_reconcile_to_the_summary():
    # QC-075 prints "reconciles to summary" under the trade-region table.
    # It only reconciles if both count the same way.
    rows = [THREE_BOOKINGS, ONE_LOSS]
    ptd = core.aggregate_summary(rows)
    regions = core.aggregate_trade_regions(rows)
    assert sum(m["requests"] for m in regions.values()) == ptd["total_entries"]
    assert sum(m["wins"] for m in regions.values()) == ptd["wins"]
    lanes = core.aggregate_lanes(rows)
    assert sum(m["requests"] for m in lanes.values()) == ptd["total_entries"]
    assert sum(m["wins"] for m in lanes.values()) == ptd["wins"]


def test_carrier_win_rate_cannot_exceed_one_hundred():
    car = core.aggregate_carriers([THREE_BOOKINGS, ONE_LOSS])
    cma = car["CMA CGM"]
    assert cma["wins"] == 3
    # quotes had to expand too, or 3/1 = 300%.
    assert cma["quotes"] >= cma["wins"]
    assert 0 <= cma["win_rate"] <= 100


def test_a_floored_nq_row_still_counts_in_the_total():
    # total_entries counts every row by shipment — NOT the sum of the four
    # buckets. An NQ row from before NQ_VALID_FROM is excluded from
    # `not_quoted` but must stay in the total, or QC-075's reconciliation
    # fires on healthy data.
    floored = {"status": "LOSS", "quoted": False,
               "request_date": "2020-01-02", "date": "2020-01-02",
               "destination": "Kobe", "teu_requested": 1}
    ptd = core.aggregate_summary([THREE_BOOKINGS, floored])
    assert ptd["not_quoted_excluded"] >= 1
    assert ptd["total_entries"] == 4, "the floored row must not vanish"


def test_the_library_tree_counts_the_same_way():
    # src/hilmar stores STRICT status; the counting rule must not differ.
    from hilmar import core as lib
    strict = dict(THREE_BOOKINGS, status=lib.STATUS_WIN)
    assert lib.shipment_count(strict) == 3
    assert lib.booking_count(strict) == 3
    assert lib.shipment_count({"status": lib.STATUS_NQ}) == 1
    s = lib.aggregate_summary([strict, dict(ONE_LOSS, status=lib.STATUS_Q_AND_L)])
    assert s["wins"] == 3 and s["total_entries"] == 4
    assert 0 <= s["win_rate"] <= 100


# ── COPILOT ON #226, BOTH VERIFIED ──────────────────────────────────────
#
# The first pass at this fix routed scripts/core's rollups through
# shipment_count but left the LIBRARY tree's aggregate_carriers at += 1,
# and left the dashboard's wins drill-in rendering only mdolx_ref under a
# heading that now counts bookings. Both are the same defect the whole
# change is about — a number that disagrees with the number beside it —
# so both get a test rather than a fix alone.

def test_library_carriers_reconcile_to_the_library_summary():
    from hilmar import core as lib
    win = dict(THREE_BOOKINGS, status=lib.STATUS_WIN)
    loss = dict(ONE_LOSS, status=lib.STATUS_Q_AND_L)
    s = lib.aggregate_summary([win, loss])
    car = lib.aggregate_carriers([win, loss])
    assert sum(c["wins"] for c in car.values()) == s["wins"] == 3
    for c in car.values():
        assert c["quotes"] >= c["wins"], "quotes must expand with wins"
        assert 0 <= c["win_rate"] <= 100
    lanes = lib.aggregate_lanes([win, loss])
    assert sum(m["wins"] for m in lanes.values()) == s["wins"]
    assert sum(m["requests"] for m in lanes.values()) == s["total_entries"]


def test_dashboard_wins_drill_in_shows_every_booking_it_counts():
    # The heading says "N bookings"; the table under it must name N of them,
    # or a reader who scrolls down to verify the tile finds it contradicted.
    import json
    import re

    import gen_dashboard
    cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    data = {"version": cfg["version"], "requests": [THREE_BOOKINGS],
            "summary": core.aggregate_summary([THREE_BOOKINGS]),
            "last_updated": core.now_utc().isoformat()}
    html = gen_dashboard.render(cfg, data)
    heading = re.search(r"Confirmed Wins — (\d+) bookings", html)
    assert heading, "wins section heading not rendered"
    section = html.split('id="sec-wins"')[1].split("</table>")[0]
    shown = set(re.findall(r"MDOLX\d+", section))
    assert int(heading.group(1)) == 3
    assert len(shown) == 3, (
        f"heading counts 3 bookings, drill-in names {sorted(shown)}")
