"""The cover-email tables must not squish/truncate (user-reported render bug).

The OL-USA Responses table (11 columns) and Status Changes table crammed their
columns inside a 900px container with content-driven sizing + white-space:nowrap
date cells, so columns overflowed and the Status Changes "Reason" column
truncated to "MBD rate re…". Fix: table-layout:fixed + an explicit <colgroup>
on the wide tables, drop the overflow-forcing nowrap, and let the Reason cell
wrap. These lock the readable layout while preserving Outlook-safety (QC-042
no data: URIs, QC-044 no double-escape, QC-045 no linear-gradient headers).
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import gen_email as GE  # noqa: E402


def _render():
    ol_resp = [{
        "lane": "Oakland → Yokohama", "containers": "6-40' HC", "teu_requested": 12,
        "carrier_quoted": "CMA CGM", "ol_rate": 3076, "ol_responder_signer": "Linda Echevarria",
        "request_timestamp": "2026-06-16T14:05:00Z", "response_timestamp": "2026-06-26T21:26:00Z",
        "turnaround_biz_hours": None, "etd_offered": "15-Jul-26", "eta_offered": "27-Jul-26",
    }]
    status_ch = [({
        "lane": "Oakland → Xingang", "container_count": 9, "teu_requested": 9,
        "containers": "9-20'", "request_date": "2026-06-26",
        "carrier_quoted": "CMA CGM", "ol_rate": 598,
    }, {
        "from": "PENDING", "to": "QUOTED",
        "reason": "MBD rate response — carrier=CMA CGM, rate=598.0 vs prior carrier rate=620.0; "
                  "competitive re-quote on lane, Lonny to decide",
    })]
    return GE._today_block_html("Fri Jun 26", [], ol_resp, status_ch, [])


def test_wide_tables_use_fixed_layout_with_colgroup():
    html = _render()
    assert html.count("table-layout:fixed") >= 2, "wide tables must use table-layout:fixed"
    assert html.count("<colgroup>") >= 2, "wide tables must declare explicit column widths"


def test_response_date_cells_do_not_force_nowrap_overflow():
    html = _render()
    assert "white-space:nowrap;font-size:11px" not in html, (
        "the nowrap date cells were the overflow driver — they must wrap within "
        "their fixed column"
    )


def test_status_change_reason_cell_wraps():
    html = _render()
    assert "word-break:break-word" in html, "the Reason cell must wrap, not truncate"
    # The full long reason text must be present (not clipped at render time).
    assert "competitive re-quote on lane" in html


def test_render_stays_outlook_safe():
    html = _render()
    assert "linear-gradient" not in html      # QC-045: Outlook strips it on th
    assert "data:image" not in html           # QC-042: no data URIs in email
    assert "&amp;amp;" not in html            # QC-044: no double-escape


def test_colgroup_widths_sum_to_100():
    """A colgroup whose widths don't sum to ~100% defeats table-layout:fixed."""
    import re
    html = _render()
    for grp in re.findall(r"<colgroup>(.*?)</colgroup>", html):
        widths = [float(w) for w in re.findall(r"width:([\d.]+)%", grp)]
        assert abs(sum(widths) - 100.0) < 0.5, f"colgroup widths sum to {sum(widths)}, not 100"
