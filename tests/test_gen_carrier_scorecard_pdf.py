"""Smoke/contract test for scripts/gen_carrier_scorecard_pdf.py (finding [8]).

Step 13 — per-carrier negotiation scorecards — had no test. This unit-tests the
pure helpers (_slug, _aggregate_lanes) and renders a real scorecard PDF for a
synthetic carrier (the golden fixture carries no carrier_summary), so a render
crash or empty document fails CI.
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))


@pytest.fixture(scope="module")
def GS():
    import gen_carrier_scorecard_pdf
    return gen_carrier_scorecard_pdf


@pytest.fixture(scope="module")
def cfg():
    import json
    return json.loads((ROOT / "config.json").read_text())


def test_slug_is_filename_safe(GS):
    assert GS._slug("CMA CGM") == "cma-cgm"
    assert "/" not in GS._slug("ONE / Ocean Network")
    assert " " not in GS._slug("Hapag Lloyd")


def test_aggregate_lanes_counts_wins(GS):
    reqs = [
        {"status": "WIN", "lane": "Oakland→Tokyo", "teu_requested": 2, "ol_rate": 2500, "containers": "2x40HC"},
        {"status": "WIN", "lane": "Oakland→Tokyo", "teu_requested": 1, "ol_rate": 2400, "containers": "1x40HC"},
        {"status": "LOSS", "lane": "Oakland→Osaka", "teu_requested": 3, "quoted": True, "ol_rate": 3000},
    ]
    won = GS._aggregate_lanes(reqs, won=True)
    assert won["Oakland→Tokyo"]["count"] == 2
    assert won["Oakland→Tokyo"]["teu"] == 3
    lost = GS._aggregate_lanes(reqs, won=False)
    assert "Oakland→Osaka" in lost
    assert lost["Oakland→Osaka"]["count"] == 1


def test_aggregate_lanes_excludes_unquoted_losses(GS):
    reqs = [{"status": "LOSS", "lane": "X→Y", "quoted": False, "teu_requested": 1}]
    assert GS._aggregate_lanes(reqs, won=False) == {}


def test_build_scorecard_renders_valid_pdf(tmp_path, GS, cfg):
    data = {
        "date_range": "2026-06-01 — 2026-06-26",
        "carrier_summary": {
            "CMA CGM": {"quotes": 4, "wins": 2, "losses": 1, "pending": 1,
                        "win_rate": 0.667, "teu_won": 3, "teu_lost": 2,
                        "lanes_quoted": 2, "avg_turnaround_biz_hours": 6.5,
                        "avg_etd_fit_days": 0},
        },
        "requests": [
            {"status": "WIN", "carrier_won": "CMA CGM", "carrier_quoted": "CMA CGM",
             "lane": "Oakland→Tokyo", "teu_requested": 2, "ol_rate": 2500, "containers": "2x40HC"},
            {"status": "LOSS", "carrier_quoted": "CMA CGM", "quoted": True,
             "lane": "Oakland→Osaka", "teu_requested": 2, "ol_rate": 3100, "containers": "1x40HC"},
        ],
    }
    story = GS.build_scorecard("CMA CGM", data, cfg)
    assert story

    out = tmp_path / "cma-cgm-scorecard.pdf"
    doc = GS.SimpleDocTemplate(
        str(out), pagesize=GS.LETTER,
        leftMargin=0.5 * GS.inch, rightMargin=0.5 * GS.inch,
        topMargin=0.7 * GS.inch, bottomMargin=0.55 * GS.inch,
    )
    doc.build(story)
    blob = out.read_bytes()
    assert blob.startswith(b"%PDF")
    assert len(blob) > 2000
