"""Pending must show WHO to chase, not one lumped amber 'PENDING' (user-reported).

Two materially different waits:
  - PENDING_OL     — RFQ sent, OL hasn't quoted → chase OL  (amber marker)
  - PENDING_HILMAR — OL quoted, Lonny hasn't decided → chase Lonny (violet marker)

These lock the distinct markers (viz.pending_pill) + that the cover-email KPI
tiles surface the split rather than a generic "any party" sublabel.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import viz as V  # noqa: E402


def test_pending_ol_marker_is_amber_and_labeled():
    html = V.pending_pill("PENDING_OL")
    assert "OL QUOTE" in html
    assert "#f59e0b" in html  # amber border — chase OL


def test_pending_hilmar_marker_is_violet_and_labeled():
    html = V.pending_pill("PENDING_HILMAR")
    assert "LONNY" in html
    assert "#7c3aed" in html  # violet border — chase Lonny


def test_two_pending_markers_are_visually_distinct():
    assert V.pending_pill("PENDING_OL") != V.pending_pill("PENDING_HILMAR")


def test_unknown_substate_falls_back_to_plain_pending():
    html = V.pending_pill(None)
    assert "PENDING" in html
    assert "OL QUOTE" not in html and "LONNY" not in html


def test_status_pill_label_override():
    assert "WAITING" in V.status_pill("PENDING", label="WAITING")


def test_kpi_tiles_show_the_pending_split():
    """The cover-email KPI pending tiles must surface the OL-quote vs Lonny
    split, not a generic lumped 'any party' sublabel."""
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
    assert "OL quote" in html and "Lonny" in html, (
        "pending KPI tiles must show the OL-quote vs Lonny split"
    )
    assert "any party" not in html  # the old lumped sublabel is gone
