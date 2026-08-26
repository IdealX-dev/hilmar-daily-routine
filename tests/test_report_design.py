"""What the daily email must look like, after the 2026-08-26 design proof.

Michael: "usa aibox to proof this system for improvements and graphical ones
and design", then chose all four recommendations.

The findings that drove these, each verified against the code before it was
acted on:

  · the same open quote rendered FOUR times in one email — sections 7 and 16
    applied the identical pending_substate filter to the same list
  · body text at 9-11px, which iPhone Outlook does not auto-zoom
  · an 11-column table on a 375px screen: ~34px per column, in the section
    the reader most needs
  · the only action item sat at position 7 of 15, below the fold every morning
  · seven of fifteen sections were whole-dataset ANALYSIS, already present in
    the attached dashboard and PDF
  · emoji on all 18 headings, so none of them signalled anything
"""
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import core  # noqa: E402
import gen_email as GE  # noqa: E402


def _rd():
    return GE._report_date(datetime.now(timezone.utc).astimezone(core.ET))


def _at(h=14):
    d = _rd()
    return datetime(d.year, d.month, d.day, h, 0, tzinfo=timezone.utc).isoformat()


def _row(rid, lane, status, **over):
    r = {"request_id": rid, "lane": lane, "origin": "Oakland",
         "destination": lane.split("→")[1].strip(), "containers": "1-40' HC",
         "teu_requested": 2, "status": status, "quoted": True,
         "carrier_quoted": "CMA CGM", "ol_rate": 3010.0,
         "ol_responder_signer": "Maria Machado",
         "request_timestamp": _at(12), "response_timestamp": _at(14),
         "etd_offered": "7-Sep-26", "eta_offered": "22-Sep-26",
         "request_date": _rd().isoformat(),
         "status_history": [{"at": _at(14), "from": "PENDING", "to": "QUOTED",
                             "reason": "MBD rate response"}]}
    r.update(over)
    return r


def _html(rows):
    return GE.build_body({"requests": rows}, {})


# ── The duplicate that started it ──────────────────────────────────────────

def test_an_open_quote_gets_one_pending_detail_table_not_two():
    """The defect was two FULL DETAIL TABLES of the same pending rows —
    sections 7 and 16 applying the identical pending_substate filter to the
    same list.

    NOT a bug, and this test says so explicitly: one lane legitimately appears
    in several sections of a day's report — it arrived (New Requests), was
    quoted (OL Responses), moved state (Status Changes) and is now open
    (Awaiting Lonny). That is the day's story told once each way, and an
    earlier version of this test wrongly called it duplication.
    """
    html = _html([_row("p1", "Oakland → Durban", "PENDING")])
    # the retired second table's own header string
    assert "Who Quoted (OL signer)" not in html
    action = html[html.index("AWAITING LONNY"):html.index("NEW REQUESTS FROM LONNY")]
    assert action.count("Oakland → Durban") == 1, (
        "the action list rendered the same open quote more than once")


# ── The action item leads ──────────────────────────────────────────────────

def test_the_action_item_comes_before_the_history():
    html = _html([_row("p1", "Oakland → Durban", "PENDING"),
                  _row("l1", "Oakland → Osaka", "LOSS")])
    action = html.index("AWAITING LONNY")
    for later in ("NEW REQUESTS FROM LONNY", "OL-USA RESPONSES", "STATUS CHANGES"):
        assert action < html.index(later), f"{later} rendered above the action list"


def test_an_empty_section_is_one_line_not_an_empty_grid():
    html = _html([_row("l1", "Oakland → Osaka", "LOSS")])
    assert "nothing open" in html or "nothing outstanding" in html
    # and it must not have produced a header-only table for the empty section
    assert "AWAITING LONNY'S DECISION (0)" not in html


# ── Column budget ──────────────────────────────────────────────────────────

def _widest_table(html):
    return max(len(re.findall(r"<th", t))
               for t in re.findall(r"<table.*?</table>", html, re.S))


def test_no_table_exceeds_the_phone_column_budget():
    html = _html([_row(f"r{i}", "Oakland → Osaka", "LOSS") for i in range(3)]
                 + [_row("p1", "Oakland → Durban", "PENDING")])
    assert _widest_table(html) <= 8, (
        "a table wide enough to need horizontal scrolling on a 375px screen")


def test_the_response_table_keeps_what_a_decision_needs():
    html = _html([_row("r1", "Oakland → Osaka", "LOSS")])
    for keep in ("Lane · Equipment", "Carrier · Who quoted", "Rate /container",
                 "Time to Quote", "Sails (ETD → ETA)"):
        assert keep in html, keep


def test_the_dropped_columns_are_really_gone():
    html = _html([_row("r1", "Oakland → Osaka", "LOSS")])
    for gone in ("Who Responded (OL signer)", "ETD Offered", "ETA Offered"):
        assert gone not in html, f"{gone} still renders"


# ── The colour edge ────────────────────────────────────────────────────────

def test_each_row_carries_a_colour_for_its_state():
    import viz as V
    assert GE._edge_for({"status": "WIN"}) == V.B.DOC_GOOD
    assert GE._edge_for({"status": "LOSS", "quoted": True}) == V.B.DOC_BAD
    won = GE._edge_for({"status": "PENDING", "quoted": True})
    ol = GE._edge_for({"status": "PENDING", "quoted": False})
    assert won and ol and won != ol, "the two waits must not look the same"


def test_the_edge_renders_as_an_email_safe_cell():
    cell = GE._edge_cell("#c0392b")
    assert "background:#c0392b" in cell
    assert "<img" not in cell and "class=" not in cell


def test_a_stateless_row_still_keeps_the_column_count():
    assert "background:transparent" in GE._edge_cell(None)


# ── Type size ──────────────────────────────────────────────────────────────

def test_body_text_is_readable_without_pinch_zoom():
    src = (ROOT / "scripts" / "gen_email.py").read_text(encoding="utf-8")
    for tiny in ("font-size:9px", "font-size:10px", "font-size:11px"):
        assert tiny not in src, (
            f"{tiny} in the email body — iPhone Outlook does not auto-zoom")


# ── The analysis moved out ─────────────────────────────────────────────────

def test_the_whole_dataset_analysis_is_not_in_the_daily_email():
    html = _html([_row("r1", "Oakland → Osaka", "LOSS")])
    for moved in ("Carrier Performance", "Volume by Trade Region",
                  "Top Winning Lanes", "Top Losing Lanes",
                  "This Week vs Last Week"):
        assert moved not in html, f"{moved} still ships in the daily email"


def test_the_row_builders_survive_for_the_dashboard():
    """Moved OUT of the email, not deleted — gen_dashboard and gen_pdf use
    them, and a weekly caller may want them again."""
    for fn in ("_week_rows", "_carrier_rows", "_winning_lane_rows",
               "_losing_lane_rows", "_carrier_block_html", "_trade_region_html"):
        assert hasattr(GE, fn), fn


def test_the_email_got_materially_smaller():
    html = _html([_row(f"r{i}", "Oakland → Osaka", "LOSS") for i in range(4)])
    assert len(html) < 60_000, f"{len(html):,} bytes — was ~84,000"
