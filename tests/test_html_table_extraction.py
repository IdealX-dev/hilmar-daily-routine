"""OL HTML rate-table extraction — 2026-06-16 Oakland→Yokohama miss.

A real OL quote (Oakland→Yokohama, CMA, $3076, 6x40'RF) was reported NOT
QUOTED because OL renders each rate-table cell as
    <td><p><span>value</span></p></td>
and pretty-prints the HTML source with newlines between tags. html_to_text
then split EVERY cell onto its own line, so parse_rate_table saw no
"cell | cell" row and extracted nothing → the quote vanished. The fix makes
the stripper keep a table row on one pipe-delimited line.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import body_parser as SBP  # noqa: E402  (production tree)

from hilmar import body_parser as HBP  # noqa: E402

# The OL template: every cell is <td><p><span>..</span></p></td>, and the
# source is pretty-printed (newlines between tags) — both reproduced here.
_OL_TABLE_HTML = """<html><body>
<table>
<tr>
<td><p><span>POL</span></p></td>
<td><p><span>POD</span></p></td>
<td><p><span>Container Size</span></p></td>
<td><p><span>Vessel</span></p></td>
<td><p><span>ETD</span></p></td>
<td><p><span>ETA</span></p></td>
<td><p><span>RATE</span></p></td>
<td><p><span>CARRIER</span></p></td>
<td><p><span>TRANSSHIPMENT</span></p></td>
</tr>
<tr>
<td><p><span>Oakland</span></p></td>
<td><p><span>Yokohama</span></p></td>
<td><p><span>6x40'RF</span></p></td>
<td><div>PRESIDENT LB JOHNSON</div></td>
<td><p><span>15-Jul-26</span></p></td>
<td><p><span>26-Jul-26</span></p></td>
<td><p><span>$3076</span></p></td>
<td><p><span>CMA</span></p></td>
<td><p><span>DIRECT</span></p></td>
</tr>
</table>
</body></html>"""


@pytest.mark.parametrize("BP", (SBP, HBP), ids=["scripts", "hilmar"])
def test_html_to_text_keeps_each_row_on_one_line(BP):
    txt = re.sub(r" +", " ", BP.html_to_text(_OL_TABLE_HTML))
    # header cells and data cells each land on a single pipe-joined row
    assert "POL | POD" in txt, txt
    assert "Oakland | Yokohama" in txt, txt


def test_parse_rate_table_from_p_span_cells_production():
    # The production parser must fully extract the quote from this template.
    rt = SBP.parse_rate_table(SBP.html_to_text(_OL_TABLE_HTML))
    assert rt.get("carrier_quoted") == "CMA CGM"
    assert rt.get("ol_rate") == 3076.0
    assert rt.get("pol") == "Oakland"
    assert rt.get("pod") == "Yokohama"
    assert (rt.get("etd_offered") or rt.get("etd"))
    assert (rt.get("eta_offered") or rt.get("eta"))


def test_simple_td_table_still_parses():
    # No-regression: a plain <td>value</td> table (no inner <p>) still works.
    html = ("<table><tr><td>POL</td><td>POD</td><td>RATE</td><td>CARRIER</td></tr>"
            "<tr><td>Oakland</td><td>Busan</td><td>$1200</td><td>MSC</td></tr></table>")
    rt = SBP.parse_rate_table(SBP.html_to_text(html))
    assert rt.get("ol_rate") == 1200.0
    assert rt.get("carrier_quoted") == "MSC"
