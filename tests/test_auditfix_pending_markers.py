"""Pending must show WHO to chase, not one lumped amber 'PENDING' (user-reported).

Two materially different waits:
  - PENDING_OL     — RFQ sent, OL hasn't quoted → chase OL     ("Pending OL", amber)
  - PENDING_HILMAR — OL quoted, Lonny hasn't decided → chase Hilmar ("Pending Hilmar", violet)

Michael 2026-06-27: "one should be 'pending OL' and one is 'pending Hilmar'
for how you show pending" — so the party word is OL vs HILMAR (not "Lonny"),
and EVERY surface must agree: the email status-change pills, the email daily
sections + KPI tiles, the PDF watchlist, and the dashboard.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import viz as V  # noqa: E402


def test_pending_ol_marker_is_amber_and_labeled():
    html = V.pending_pill("PENDING_OL")
    assert "PENDING OL" in html
    assert "#f59e0b" in html  # amber border — chase OL


def test_pending_hilmar_marker_is_violet_and_labeled():
    html = V.pending_pill("PENDING_HILMAR")
    assert "HILMAR" in html          # Michael: "pending Hilmar", not "Lonny"
    assert "LONNY" not in html
    assert "#7c3aed" in html  # violet border — chase Hilmar


def test_two_pending_markers_are_visually_distinct():
    assert V.pending_pill("PENDING_OL") != V.pending_pill("PENDING_HILMAR")


def test_unknown_substate_falls_back_to_plain_pending():
    html = V.pending_pill(None)
    assert "PENDING" in html
    assert "PENDING OL" not in html and "PENDING HILMAR" not in html


def test_pending_label_helper_is_the_shared_source_of_truth():
    """The plain-text label the PDF + dashboard tables share with the pills."""
    assert V.pending_label("PENDING_OL") == "Pending OL"
    assert V.pending_label("PENDING_HILMAR") == "Pending Hilmar"
    assert V.pending_label(None) == "Pending"
    assert V.pending_label("anything_else") == "Pending"


def test_status_pill_label_override():
    assert "WAITING" in V.status_pill("PENDING", label="WAITING")


def test_kpi_tiles_show_the_pending_split():
    """The cover-email KPI pending tiles must surface the Pending OL vs Pending
    Hilmar split, not a generic lumped 'any party' sublabel."""
    import gen_email as GE
    summary = {"total_entries": 3, "wins": 1, "quoted_lost": 1, "not_quoted": 0,
               "pending_hilmar": 2, "win_rate": 50.0, "quote_rate": 90.0,
               "teu_won": 2, "teu_quoted_lost": 1, "teu_not_quoted": 0,
               "teu_requested": 5, "teu_pending": 3, "turnaround_avg_biz_hours": 5.0}
    requests = [
        {"status": "PENDING", "quoted": False, "request_date": "2026-06-26",
         "destination": "Tokyo", "teu_requested": 1},   # PENDING_OL
        {"status": "PENDING", "quoted": True, "request_date": "2026-06-26",
         "destination": "Osaka", "teu_requested": 2},    # PENDING_HILMAR
    ]
    from datetime import date
    html = GE._kpi_block_html(summary, requests, report_date=date(2026, 6, 26))
    assert "Pending OL" in html and "Pending Hilmar" in html, (
        "pending KPI tiles must show the Pending OL vs Pending Hilmar split"
    )
    assert "any party" not in html  # the old lumped sublabel is gone


# ── every render surface uses the same OL / Hilmar vocabulary ─────────────
def test_pdf_watchlist_splits_ol_vs_hilmar():
    """The PDF watchlist must distinguish Pending OL vs Pending Hilmar per row,
    not lump everything under one 'Pending Hilmar decision' header."""
    src = (ROOT / "scripts" / "gen_pdf.py").read_text(encoding="utf-8")
    assert 'f"Pending Hilmar decision' not in src   # old lumped header gone
    assert "Pending Watchlist" in src               # new neutral header
    assert "Waiting On" in src                       # per-row who-to-chase column
    assert "pending_substate" in src and "V.pending_label" in src


def test_dashboard_pending_uses_shared_label():
    """The dashboard 'Waiting On' cell uses the shared label, not the old
    'OL quote' / 'Hilmar decision' wording."""
    src = (ROOT / "scripts" / "gen_dashboard.py").read_text(encoding="utf-8")
    assert "V.pending_label" in src
    assert "Hilmar decision</span>" not in src
    assert "OL quote</span>" not in src
