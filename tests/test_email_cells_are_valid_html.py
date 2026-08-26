"""Every <td> in the daily email carries ONE style attribute, and money
columns line up under their headers.

Two defects, both silent, both in the tables Michael asked for a design
proof of on 2026-08-26:

  1. `_TD_STYLE.replace("text-align:left", "text-align:right")` was used 11
     times and was a NO-OP — _TD_STYLE never contained text-align:left; only
     _TH_STYLE did. So every rate, TEU, wait-hours and Time-to-Quote cell was
     left-aligned under a centered or right-aligned header.

  2. `<td {_TD_STYLE};font-weight:600;font-size:14px>` appended declarations
     after the style attribute's closing quote. html.parser reads that as:

         [('style', 'padding:...'), (';font-weight:600;font-size:14px', None)]

     — a garbage attribute NAME, not CSS. The rate column was neither bold
     nor sized, in an email whose whole point that day was that the number
     should be findable in ten seconds on a phone.

Nothing caught either one: the output was still valid-looking HTML and every
existing assertion was about content, not attributes.
"""
from __future__ import annotations

import re
import sys
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import gen_email as GE  # noqa: E402

#: The only attributes a cell in this email is allowed to carry.
_ALLOWED = {"style", "colspan", "rowspan", "align", "valign", "width",
            "title", "class", "id"}


class _Cells(HTMLParser):
    def __init__(self):
        super().__init__()
        self.cells = []      # (tag, dict(attrs), [raw names])
        self.bad = []

    def handle_starttag(self, tag, attrs):
        if tag not in ("td", "th"):
            return
        names = [a for a, _v in attrs]
        self.cells.append((tag, dict(attrs), names))
        for a, _v in attrs:
            if a not in _ALLOWED:
                self.bad.append((tag, a))


def _parse(html):
    p = _Cells()
    p.feed(html)
    return p


def _sample_rows():
    return [
        {"status": "PENDING", "quoted": True,
         "request_date": "2026-08-19", "date": "2026-08-19",
         "lane": "Oakland → Osaka", "origin": "Oakland",
         "destination": "Osaka", "containers": "2x40HC", "teu_requested": 4,
         "carrier_quoted": "CMA CGM", "ol_rate": 3210.0,
         "ol_signer": "Linda Chen",
         "request_timestamp": "2026-08-19T16:00:00+00:00",
         "response_timestamp": "2026-08-19T20:00:00+00:00",
         "turnaround_biz_hours": 4.0,
         "status_history": [{"from": "PENDING_OL", "to": "QUOTED",
                             "at": "2026-08-19T20:00:00+00:00"}]},
    ]


def test_no_cell_carries_a_garbage_attribute():
    # The declarations-outside-the-quote defect, caught by an HTML parser
    # rather than by eye.
    rows = _sample_rows()
    html = "".join(GE._today_block_html("Aug 19, 2026", rows, rows, [], rows,
                                        [], as_of_label="now"))
    p = _parse(html)
    assert p.cells, "rendered no cells — the fixture stopped exercising this"
    assert not p.bad, (
        "cells carry attributes that are not HTML — almost certainly CSS "
        f"appended after the style attribute's closing quote: {p.bad}")


def test_every_cell_style_is_a_single_well_formed_declaration_list():
    rows = _sample_rows()
    html = "".join(GE._today_block_html("Aug 19, 2026", rows, rows, [], rows,
                                        [], as_of_label="now"))
    for tag, attrs, names in _parse(html).cells:
        assert names.count("style") <= 1, (
            f"<{tag}> has two style attributes; only the first is honored")
        st = attrs.get("style")
        if not st:
            continue
        assert '"' not in st and "'" not in st, f"quote inside style: {st!r}"
        for decl in filter(None, st.split(";")):
            assert ":" in decl, (
                f"<{tag}> style has a fragment that is not a CSS "
                f"declaration: {decl!r} — full: {st!r}")


def test_the_source_has_no_declarations_outside_the_attribute():
    # A guard on the SHAPE, so the pattern cannot come back in a table this
    # test's fixture doesn't happen to render.
    src = (ROOT / "scripts" / "gen_email.py").read_text(encoding="utf-8")
    # Comments describe the defect by name; scan the CODE only.
    code = "\n".join(ln for ln in src.splitlines()
                     if not ln.lstrip().startswith("#"))
    offenders = re.findall(r"\{_[A-Za-z_]*STYLE[^}]*\};[a-z-]+:", code)
    assert not offenders, (
        "CSS appended after a style attribute's closing quote — it parses as "
        f"an attribute name, not as style: {offenders}")


def test_no_no_op_alignment_replace_survives():
    # _TD_STYLE has no text-align:left to replace, so any .replace() of it is
    # a no-op that silently does nothing.
    src = (ROOT / "scripts" / "gen_email.py").read_text(encoding="utf-8")
    code = "\n".join(ln for ln in src.splitlines()
                     if not ln.lstrip().startswith("#"))
    assert '_TD_STYLE.replace(' not in code, (
        "_TD_STYLE.replace() is a no-op — _TD_STYLE carries no text-align to "
        "replace. Build the cell with _cell(..., align=...) instead.")


def test_money_columns_are_right_aligned_under_their_headers():
    rows = _sample_rows()
    html = "".join(GE._today_block_html("Aug 19, 2026", rows, rows, [], rows,
                                        [], as_of_label="now"))
    # Find the rate cells by their TEXT — the money is in the cell body, not
    # in any attribute, so a parser walking attrs alone can never see it.
    rate_cells = re.findall(r"<td ([^>]*)>\$[\d,]+</td>", html)
    assert rate_cells, "no rate cell rendered — fixture no longer covers this"
    for attr in rate_cells:
        assert "text-align:right" in attr, (
            f"a money cell is not right-aligned: {attr!r}")
        assert "font-weight:600" in attr, (
            f"a money cell lost its weight to the quoting bug: {attr!r}")
        # One style attribute carrying all of it — not two, and nothing
        # trailing outside the quotes.
        assert attr.count("style=") == 1, f"doubled style on a money cell: {attr!r}"
        assert not attr.rstrip().endswith(";"), (
            f"declarations trailing outside the attribute: {attr!r}")
