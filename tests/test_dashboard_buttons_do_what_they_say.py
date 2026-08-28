"""Every dashboard tile's label must name a scope the filter can enforce.

Michael, 2026-08-27, having clicked the "Won — Wed Aug 26" tile: "in portal
if you notice filter active.. it still lists every move ever won". Then,
correctly: "so all buttons need checking".

All thirteen were. THREE defects, none of them the one it looked like:

 1. NO DATE EXISTED ANYWHERE. Tiles carried only a status string, and no row
    carried a date attribute at all — `grep data-date` over the whole file
    returned zero. The day filter was not broken, it was absent, so "Wins —
    Wed Aug 26" showed every win since January.

 2. THE SELECTOR WAS DOCUMENT-WIDE. The Pending tile opens the Pending TAB,
    but its filter reached across and dimmed every row of the Confirmed Wins
    table on the Summary tab — invisibly, and it stayed dimmed until Clear
    Filter. The only tile that did damage rather than nothing.

 3. QL, NQ AND `quoted` HAD NO BRANCH. Those tiles lit a banner naming a
    scope and filtered nothing, so you landed on the wins table under a
    heading about Quoted & Lost.

THE TRAP IN THE OBVIOUS FIX, which these tests exist to keep shut: the day
"Won" tile counts bookings CONFIRMED that day, whatever day the RFQ came in
— the dashboard's own header says so. The table DISPLAYS Req Date. Filtering
on the displayed column would dim the very row booked that day and show an
empty table under a tile reading 2. The date attribute is win_event_date.
"""
from __future__ import annotations

import json
import re
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import core  # noqa: E402
import gen_dashboard  # noqa: E402

SRC = (ROOT / "scripts" / "gen_dashboard.py").read_text(encoding="utf-8")


def _cfg():
    return json.loads((ROOT / "config.json").read_text(encoding="utf-8"))


def _render(rows):
    data = {"version": _cfg()["version"], "requests": rows,
            "summary": core.aggregate_summary(rows),
            "last_updated": core.now_utc().isoformat()}
    return gen_dashboard.render(_cfg(), data)


def _tiles(html):
    """Every KPI tile as (filter, filter_date, label)."""
    out = []
    for tag in re.findall(r"<a class=\"kpi[^\"]*\"[^>]*>", html):
        f = re.search(r'data-filter="([^"]*)"', tag)
        d = re.search(r'data-filter-date="([^"]*)"', tag)
        lab = re.search(r'data-filter-label="([^"]*)"', tag)
        if f:
            out.append((f.group(1), d.group(1) if d else "",
                        lab.group(1) if lab else ""))
    return out


# ── the rows: one win booked on the report day, one booked weeks earlier ──

def _rows():
    today = date(2026, 8, 26)
    old = today - timedelta(days=60)
    return [
        {   # booked ON the report day, but REQUESTED long before —
            # the exact shape that a request-date filter would wrongly hide.
            "status": "WIN", "quoted": True,
            "request_date": old.isoformat(), "date": old.isoformat(),
            "origin": "Oakland", "destination": "Osaka",
            "lane": "Oakland → Osaka", "containers": "1x40HC",
            "teu_won": 2, "teu_requested": 2,
            "carrier_won": "CMA CGM", "carrier_quoted": "CMA CGM",
            "mdolx_ref": "261900",
            "booking_timestamp": "2026-08-26T18:00:00Z",
            "status_history": [{"to": "WIN", "at": "2026-08-26T18:00:00Z"}],
        },
        {   # booked weeks earlier — must be dimmed by the day tile
            "status": "WIN", "quoted": True,
            "request_date": old.isoformat(), "date": old.isoformat(),
            "origin": "Oakland", "destination": "Tokyo",
            "lane": "Oakland → Tokyo", "containers": "1x40HC",
            "teu_won": 2, "teu_requested": 2,
            "carrier_won": "ONE", "carrier_quoted": "ONE",
            "mdolx_ref": "261100",
            "booking_timestamp": "2026-06-27T18:00:00Z",
            "status_history": [{"to": "WIN", "at": "2026-06-27T18:00:00Z"}],
        },
    ]


def _filterable_rows(html):
    """Rows the filter can actually reach: those inside a data-filterable
    table. NOT every `.win-row` — that class is reused by the Top Winning
    Lanes table for its green left border, and those are per-LANE aggregates
    with no booking date and no business being date-filtered. If that table
    ever gains data-filterable, this helper is where it must be handled.
    """
    out = []
    for tbl in re.findall(r"<table data-filterable=.*?</table>", html, re.S):
        out.extend(re.findall(r"<tr[^>]*data-status=[^>]*>", tbl))
    return out


def test_every_filterable_win_row_carries_its_booking_date():
    html = _render(_rows())
    rows = _filterable_rows(html)
    assert rows, "no filterable rows rendered — fixture stopped exercising this"
    for tr in rows:
        m = re.search(r'data-win-date="([^"]*)"', tr)
        assert m and m.group(1), (
            f"a filterable row carries no bookable date, so no day filter "
            f"can ever work: {tr[:120]}")


def test_the_date_is_the_booking_date_not_the_request_date():
    # THE TRAP. Both fixture rows were REQUESTED 60 days ago; one was BOOKED
    # on the report day. The attribute must reflect the booking.
    html = _render(_rows())
    dates = [re.search(r'data-win-date="([^"]*)"', tr).group(1)
             for tr in _filterable_rows(html)]
    assert "2026-08-26" in dates, (
        "the row booked on the report day is not stamped with it — the "
        "attribute is probably coming from request_date")
    assert not all(d == dates[0] for d in dates), (
        "every row carries the same date; request_date would do that, "
        "win_event_date would not")


def test_the_day_wins_tile_carries_the_report_day():
    html = _render(_rows())
    day_wins = [t for t in _tiles(html)
                if t[0] == "WIN" and t[1]]
    assert day_wins, (
        "no tile carries both a WIN filter and a date — the day tile cannot "
        "scope to its day")
    for _f, d, _lab in day_wins:
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", d), f"bad filter date {d!r}"


def test_no_tile_promises_a_filter_token_the_script_cannot_honour():
    # The JS handles exactly WIN and PENDING (plus 'all', which skips
    # filtering). A tile shipping QL / NQ / quoted lights a banner and
    # filters nothing — the defect Michael hit on three separate tiles.
    handled = {"all", "WIN", "PENDING"}
    bad = [t for t in _tiles(_render(_rows())) if t[0] not in handled]
    assert not bad, (
        f"tiles carry filter tokens the script has no branch for: "
        f"{[(f, lab) for f, _d, lab in bad]}")


def test_the_filter_is_scoped_to_the_clicked_section():
    # Otherwise Pending (a different TAB) dims the Wins table behind it.
    assert "scope.querySelectorAll('table[data-filterable] tbody tr')" in SRC, (
        "the row filter still runs document-wide; a tile targeting one "
        "section will reach into every other filterable table")
    assert "var scope = target || document;" in SRC


def test_the_header_row_is_in_a_thead_so_it_cannot_be_dimmed():
    # Without <thead> the header <tr> lands in the implicit <tbody>, matches
    # the row selector, has no data-status, and fades to 25%. That faint
    # header was the entire visible effect of clicking Wins.
    html = _render(_rows())
    assert '<table data-filterable="wins"><thead>' in html
    assert "</thead><tbody>" in html
    assert "</tbody></table>" in html


def test_a_day_label_only_appears_where_a_date_filter_backs_it():
    # The rule this whole file enforces: a banner may not name a day unless
    # the tile carries a date the script can match on.
    html = _render(_rows())
    report_label = None
    m = re.search(r"📅 ([^(]+) \(ET\)", html)
    if m:
        report_label = m.group(1).strip()
    if not report_label:
        return  # no day label rendered at all; nothing to check
    for f, d, lab in _tiles(html):
        if report_label and report_label in lab:
            assert d, (
                f"tile banner names the day {report_label!r} but carries no "
                f"data-filter-date, so nothing enforces it: {lab!r}")
            assert f in ("WIN", "PENDING"), (
                f"a day-scoped banner needs a filter the script honours, "
                f"got {f!r}: {lab!r}")


def test_huangpu_and_jpyok_both_resolve_now():
    """Michael, 2026-08-27: "still shows things unmapped" — and then, on the
    second one: "JPYOK and Yokohama are samy JPYOK is the UN LOC code for
    Yokohama ... makes no sense and for you to fix".

    THIS TEST USED TO ASSERT THE OPPOSITE for Jpyok. When it was written the
    code could prove our parser had INVENTED the name (body_parser._norm
    Title-Cases any all-caps token over three characters, so JPYOK became
    "Jpyok") but could not prove what the code stood for — nothing in the
    repo knew what a UN/LOCODE was. So it was pinned Unmapped deliberately,
    to keep the only detector pointing at it rather than paper it over with
    a map entry that would have split Yokohama forever.

    The operator supplied the missing fact. Yokohama is 44 of the 134
    bookings in data/ol-transaction-report-2026.json — the largest lane in
    the book by 3x — and the split was starving its winning median below
    PRICE_GAP_MIN_LANE_WINS (measured: split -> no median at all, merged ->
    3150.0), which flipped that lane's Q&L losses from PRICE to
    UNDIFFERENTIATED. So the merge is the fix, not the map entry.
    """
    assert core.trade_region_for("Huangpu") == "Far East"
    # Jpyok now RESOLVES rather than being detected — through the port it
    # names, not through a region entry of its own.
    assert core.resolve_locode("JPYOK") == "Yokohama"
    assert core.resolve_locode("Jpyok") == "Yokohama", (
        "rows written before the fix carry the Title-Cased spelling; the "
        "resolver must reach them too")
    assert core.trade_region_for("Jpyok") == "Far East"
    assert core.same_port("Yokohama", "JPYOK"), "the whole point is the merge"
    # And the five-letter REAL ports must be untouched — a shape rule would
    # have eaten every one of these.
    for real in ("BUSAN", "OSAKA", "TOKYO", "GENOA", "HAIFA", "LAGOS"):
        assert core.resolve_locode(real) is None, (
            f"{real} is a real port, not a LOCODE — the table gate failed")
