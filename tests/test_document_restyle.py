"""One design language across the dashboard, the PDF and the email body.

Michael shared an internal OL air-freight comparison on 2026-07-22 — "this is
a gorgeous formatting someone else at ol is using" — then "i like it all",
meaning all three artifacts. #138 restyled the dashboard against it. This
covers the other two, and the thing that makes it a language rather than a
coincidence: ONE set of tokens in branding.DOC_*.

The email is where this is easiest to get quietly wrong, for two reasons:

  1. Desktop Outlook renders with Word's engine. No CSS custom properties,
     no flex, no grid, <style> ignored outside @media. So the tokens must be
     interpolated into INLINE styles as literal hex before the mail is sent.
  2. Most of the email is built from Python string literals. A style written
     into a PLAIN string instead of an f-string ships the text "{TH_STYLE}"
     to nine recipients. Three such sites existed in the first pass of this
     restyle and rendered exactly that. Hence the placeholder-leak test.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import branding as B  # noqa: E402
import core  # noqa: E402
import gen_dashboard as GD  # noqa: E402
import gen_email as GE  # noqa: E402
import gen_pdf as GP  # noqa: E402


@pytest.fixture(scope="module")
def cfg():
    return core.load_config(str(ROOT / "config.json"))


@pytest.fixture(scope="module")
def data():
    return json.loads((ROOT / "tests" / "fixtures" / "golden_day.json").read_text())


@pytest.fixture(scope="module")
def email_html(data, cfg):
    return GE.build_body(data, cfg)


@pytest.fixture(scope="module")
def dash_html(data, cfg):
    return GD.render(cfg, data)


# ── the tokens are shared, not copied ──────────────────────────────────────

DOC_TOKENS = ["DOC_PAPER", "DOC_CARD", "DOC_INK", "DOC_MUTED", "DOC_LINE",
              "DOC_TH_BG", "DOC_GOOD", "DOC_WARN", "DOC_BAD",
              "DOC_MONO_STACK", "DOC_SANS_STACK", "DOC_TNUM"]


@pytest.mark.parametrize("name", DOC_TOKENS)
def test_branding_defines_the_token(name):
    assert hasattr(B, name), f"branding.{name} is the single source; it is missing"
    assert getattr(B, name), f"branding.{name} is empty"


@pytest.mark.parametrize("name", [t for t in DOC_TOKENS if t.endswith(("PAPER", "CARD", "INK", "MUTED", "LINE", "TH_BG", "GOOD", "WARN", "BAD"))])
def test_colour_tokens_are_six_digit_hex(name):
    assert re.fullmatch(r"#[0-9a-f]{6}", getattr(B, name)), (
        f"branding.{name} must be a 6-digit lowercase hex — Word's engine does "
        f"not parse shorthand or named colours reliably")


def test_the_three_renderers_read_the_same_paper_token(dash_html, email_html):
    """If someone re-hardcodes #f4f3ef in one renderer and edits branding for
    another, this is what catches the drift."""
    assert B.DOC_PAPER in dash_html
    assert B.DOC_PAPER in email_html
    assert GP.PAPER.hexval()[2:] == B.DOC_PAPER.lstrip("#"), (
        "the PDF page ground must be the same token, not a lookalike")


# ── the email is Outlook-safe ──────────────────────────────────────────────

def test_no_uninterpolated_placeholders_reach_the_email(email_html):
    """THE regression. A style written into a plain '' instead of an f''
    ships the literal text "{TH_STYLE}" into the message body. Three sites
    did exactly that during this restyle; the rendered output is the only
    place that shows it."""
    leaks = sorted(set(re.findall(r"\{[A-Za-z_][A-Za-z0-9_]{2,}\}", email_html)))
    assert not leaks, f"unrendered placeholders in the email body: {leaks}"


def test_the_email_uses_no_css_custom_properties(email_html):
    """var() is the natural way to write this and it is wrong here: Word's
    engine drops the declaration and the element renders unstyled."""
    assert "var(--" not in email_html, (
        "desktop Outlook does not support CSS custom properties — tokens must "
        "be resolved to literal hex in Python before send")


def test_the_email_fetches_no_fonts_from_a_cdn(email_html):
    for needle in ("fonts.googleapis.com", "fonts.gstatic.com", "cdn.jsdelivr.net"):
        assert needle not in email_html, (
            f"the email body pulls {needle} — remote content trips Outlook's "
            f"'download pictures?' bar and OL's proxy, and makes the same "
            f"report render differently desk to desk")


def test_the_email_header_is_flat_not_a_gradient(email_html):
    """QC-045's rule, now satisfied by construction rather than by fallback:
    there is no gradient left to strip."""
    assert "linear-gradient" not in email_html


def test_every_email_div_is_closed(email_html):
    """The restyle wraps the whole card in a paper-ground div. An unclosed
    wrapper makes Outlook swallow the rest of the message."""
    assert email_html.count("<div") == email_html.count("</div>"), (
        "unbalanced <div> in the email body")


def test_the_email_sits_on_the_paper_ground(email_html):
    assert f"background-color:{B.DOC_PAPER}" in email_html


# ── the loud chrome is gone ────────────────────────────────────────────────

@pytest.mark.parametrize("old", [
    "#1e3a5f",   # the solid navy table-header bar
    "#7f1d1d",   # the dark-red losing-lanes bar
])
def test_the_old_header_bars_are_gone_from_the_email(email_html, old):
    """Three tables used three different saturated bars with white text. The
    reference replaces all of it with quiet muted uppercase over one ink rule,
    so the DATA is the loud part."""
    assert old not in email_html, f"{old} chrome survived the restyle"


def test_table_headers_are_quiet_and_ruled(email_html):
    assert f"color:{B.DOC_MUTED}" in email_html
    assert f"border-bottom:2px solid {B.DOC_INK}" in email_html, (
        "a table header needs the single ink rule that separates head from "
        "body once the solid bar is gone")


def test_kpi_figures_are_monospaced_in_the_email(email_html):
    """Six tiles in a row whose decimals do not line up is the thing the
    reference document fixes."""
    assert B.DOC_MONO_STACK in email_html, "KPI figures are not in the mono stack"


def test_kpi_tiles_are_cards_not_solid_blocks(email_html):
    """The colour is demoted to a 3px top rule; the tile itself is a white
    card on paper, held by a hairline."""
    assert f"border:1px solid {B.DOC_LINE};border-top:3px solid" in email_html, (
        "KPI tiles should be hairline cards with a coloured top rule, not "
        "saturated blocks with white text")


# ── the PDF ────────────────────────────────────────────────────────────────

def test_pdf_palette_is_driven_by_the_shared_tokens():
    for pdf_name, token in [("NAVY", "DOC_INK"), ("SLATE", "DOC_MUTED"),
                            ("BORDER", "DOC_LINE"), ("LIGHT", "DOC_TH_BG"),
                            ("GREEN", "DOC_GOOD"), ("RED", "DOC_BAD"),
                            ("AMBER", "DOC_WARN"), ("PAPER", "DOC_PAPER")]:
        got = getattr(GP, pdf_name).hexval()[2:].lower()
        want = getattr(B, token).lstrip("#").lower()
        assert got == want, f"gen_pdf.{pdf_name} is {got}, branding.{token} is {want}"


def test_pdf_table_style_is_one_shared_definition():
    """Eight tables repeated the same six inline commands — a solid navy
    header bar, white header text, a full 0.3pt GRID. Eight copies is eight
    places for it to drift, and the drift is invisible until someone prints
    two pages side by side."""
    assert callable(GP.table_style)
    cmds = GP.table_style().getCommands()
    kinds = {c[0] for c in cmds}
    assert "GRID" not in kinds, (
        "a full grid is the cage the restyle removes — columns are separated "
        "by alignment and whitespace")
    assert "LINEBELOW" in kinds, "rows need the hairline rule"


def test_pdf_table_header_is_quiet():
    cmds = GP.table_style().getCommands()
    header_text = [c for c in cmds if c[0] == "TEXTCOLOR" and c[1] == (0, 0) and c[2] == (-1, 0)]
    assert header_text, "no header text colour set"
    assert header_text[-1][3] == GP.SLATE, (
        "the table header should be muted text on a light ground, not white "
        "text on a solid bar")


def test_pdf_source_no_longer_carries_the_old_navy_bar():
    src = (ROOT / "scripts" / "gen_pdf.py").read_text(encoding="utf-8")
    assert '("BACKGROUND",(0,0),(-1,0),NAVY),("TEXTCOLOR",(0,0),(-1,0),colors.white)' not in src, (
        "an inline solid-bar header survived; route it through table_style()")


def test_pdf_kpi_figures_use_the_mono_face():
    ss = GP.make_styles()
    assert ss["KPINum"].fontName == GP.MONO_FONT, (
        "KPI figures must be monospaced so the tiles' decimals line up with "
        "each other and with the tables below")
    assert ss["KPINum"].fontName != GP.MONO_FONT_BOLD, (
        "kpi_cell wraps the value in <b>; setting the bold face here makes "
        "reportlab look for the bold OF a bold")


def test_the_pdf_actually_builds_and_paints_the_paper_ground(tmp_path, data, cfg):
    """Renders a real PDF and reads the page content stream back. The paper
    ground is drawn by the page callback, so nothing short of building the
    document proves it is there."""
    import base64
    import contextlib
    import zlib
    from datetime import datetime, timezone

    from reportlab.lib.pagesizes import LETTER
    from reportlab.lib.units import inch
    from reportlab.platypus import SimpleDocTemplate

    out = tmp_path / "restyle.pdf"
    styles = GP.make_styles()
    story = []
    GP.build_cover(story, styles, data, cfg)
    GP.build_dod(story, styles, data)
    GP.build_carriers(story, styles, data)
    GP.build_lanes(story, styles, data)
    doc = SimpleDocTemplate(str(out), pagesize=LETTER, leftMargin=0.5 * inch,
                            rightMargin=0.5 * inch, topMargin=0.7 * inch,
                            bottomMargin=0.55 * inch)
    gen = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    def _page(c, d):
        return GP._header_footer(c, d, "Hilmar", "OL-USA", gen)

    doc.build(story, onFirstPage=_page, onLaterPages=_page)
    assert out.exists() and out.stat().st_size > 3000

    raw = out.read_bytes()
    streams = []
    for m in re.finditer(rb"stream\r?\n(.*?)endstream", raw, re.S):
        body = m.group(1).strip()
        # reportlab's pageCompression setting decides whether a Flate layer
        # sits under the ASCII85 one, and it is not the same on every host —
        # so try both rather than pinning the encoder's mood.
        with contextlib.suppress(Exception):
            body = base64.a85decode(body, adobe=True)
        with contextlib.suppress(Exception):
            body = zlib.decompress(body)
        streams.append(body)
    blob = b"\n".join(streams)
    assert blob, "no readable content streams in the generated PDF"

    r, g, b = (int(B.DOC_PAPER[i:i + 2], 16) / 255 for i in (1, 3, 5))
    # reportlab writes ".956863", not "0.956863"
    pat = rb"\.%b \.%b \.%b rg" % tuple(
        f"{v:.6f}".lstrip("0").lstrip(".").encode() for v in (r, g, b))
    assert re.search(pat, blob), (
        f"the warm paper ground {B.DOC_PAPER} is not painted on the page")
    assert re.search(rb"0 0 612(\.\d+)? 792(\.\d+)? re", blob), (
        "the paper fill must cover the whole page")

    fonts = set(re.findall(rb"/BaseFont\s*/([A-Za-z0-9\-]+)", raw))
    assert b"Courier" in fonts or b"Courier-Bold" in fonts, (
        "no monospace face in the PDF — the figures are not mono")


def test_the_pdf_draws_hairlines_not_a_cage(tmp_path):
    """Builds a table with real rows and reads the drawing ops back.

    Deliberately NOT driven off the golden fixture: that fixture is thin
    enough that several build_* sections short-circuit to "No lane data yet",
    and a table that never rendered emits no rules at all — which would let
    this pass while proving nothing.
    """
    import base64
    import contextlib
    import zlib

    from reportlab.lib.pagesizes import LETTER
    from reportlab.lib.units import inch
    from reportlab.platypus import SimpleDocTemplate, Table

    rows = [["Lane", "Wins", "TEU"],
            ["Oakland → Shanghai", "3", "12"],
            ["Oakland → Busan", "1", "4"]]
    t = Table(rows, colWidths=[3 * inch, 1 * inch, 1 * inch])
    t.setStyle(GP.table_style([("FONTNAME", (1, 1), (-1, -1), GP.MONO_FONT)]))
    out = tmp_path / "rules.pdf"
    SimpleDocTemplate(str(out), pagesize=LETTER).build([t])

    raw = out.read_bytes()
    streams = []
    for m in re.finditer(rb"stream\r?\n(.*?)endstream", raw, re.S):
        body = m.group(1).strip()
        with contextlib.suppress(Exception):
            body = base64.a85decode(body, adobe=True)
        with contextlib.suppress(Exception):
            body = zlib.decompress(body)
        streams.append(body)
    blob = b"\n".join(streams)

    assert re.search(rb"\.25 w", blob), "0.25pt row hairlines missing"
    assert re.search(rb"\.9 w", blob), "0.9pt rule under the table head missing"
    assert not re.search(rb"0?\.3 w", blob), (
        "the old 0.3pt full GRID is back — that is the cage the restyle removes")


# ── a verification send must never consume a real send's guard ─────────────
#
# Filed here rather than in a new module because it is the same root cause the
# restyle work kept running into: two things that must differ are allowed to
# be identical, and nothing checks.

def _outlook_send():
    import outlook_send
    return outlook_send


class _Args:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def test_a_verification_send_is_tagged_and_cannot_touch_idempotency(tmp_path, capsys):
    """THE regression. The mailbox guard dedupes on EXACT subject across
    hosts, so an untagged test copy makes the later real send look like a
    duplicate: it returns 0, sends nothing, and writes a flag recording a
    delivery that never happened.

    Live twice: 2026-07-30 a verification fire blocked the real staff send,
    and 2026-08-04 the 21:04 catch-up preview blocked its own staff run.
    """
    OS_ = _outlook_send()
    subj = tmp_path / "s.txt"
    body = tmp_path / "b.html"
    subj.write_text("Hilmar — Weekly Executive Summary (week of Jul 27)", encoding="utf-8")
    body.write_text("<p>x</p>", encoding="utf-8")

    args = _Args(subject_from_file=str(subj), body_from_file=str(body),
                 to=["michael.deitchman@idealx.us"], cc=None, to_from_config=False,
                 attach=None, dry=True, force=False, no_flag=False,
                 flag_name=None, verification=True)
    OS_.cmd_daily(args)

    assert args.force is True, "--verification must imply --force"
    assert args.no_flag is True, "--verification must imply --no-flag"
    out = capsys.readouterr().out
    # Assert on the SUBJECT LINE THAT WOULD BE SENT, not merely that the
    # prefix appeared somewhere in the log — a banner saying "tagged" while
    # the untagged subject goes out is precisely the failure being prevented.
    assert (f"→ SUBJECT: {OS_.VERIFY_PREFIX}Hilmar — Weekly Executive Summary "
            f"(week of Jul 27)") in out, (
        "the verification send did not tag the subject it actually sends — it "
        "stays indistinguishable from the real message in the sent-items the "
        f"guard reads. Got:\n{out}")


def test_the_verify_prefix_is_not_applied_twice(tmp_path, capsys):
    """A re-run must not produce '[VERIFY] [VERIFY] …' — the guard keys on the
    exact string, so a prefix that grows each run stops matching its own
    earlier sends and the dedupe quietly dies."""
    OS_ = _outlook_send()
    subj = tmp_path / "s.txt"
    body = tmp_path / "b.html"
    subj.write_text(f"{OS_.VERIFY_PREFIX}Hilmar — already tagged", encoding="utf-8")
    body.write_text("<p>x</p>", encoding="utf-8")
    args = _Args(subject_from_file=str(subj), body_from_file=str(body),
                 to=["michael.deitchman@idealx.us"], cc=None, to_from_config=False,
                 attach=None, dry=True, force=False, no_flag=False,
                 flag_name=None, verification=True)
    OS_.cmd_daily(args)
    line = [ln for ln in capsys.readouterr().out.splitlines()
            if ln.startswith("→ SUBJECT:")][0]
    assert line.count(OS_.VERIFY_PREFIX) == 1, f"prefix applied twice: {line}"


def test_a_real_send_is_never_tagged(tmp_path, capsys):
    """The tag marks a test copy. Putting it on a staff send would be worse
    than the bug it fixes."""
    OS_ = _outlook_send()
    subj = tmp_path / "s.txt"
    body = tmp_path / "b.html"
    subj.write_text("Hilmar — Weekly Executive Summary (week of Jul 27)", encoding="utf-8")
    body.write_text("<p>x</p>", encoding="utf-8")
    args = _Args(subject_from_file=str(subj), body_from_file=str(body),
                 to=None, cc=None, to_from_config=False, attach=None, dry=True,
                 force=False, no_flag=True, flag_name="weekly-sent",
                 verification=False)
    OS_.cmd_daily(args)
    assert OS_.VERIFY_PREFIX not in capsys.readouterr().out


def test_every_test_send_path_in_the_workflows_uses_verification():
    """Helper written, wiring not — three times in one day. The flag only
    protects the paths that actually pass it."""
    for wf in ("daily.yml", "weekly.yml"):
        text = (ROOT / ".github" / "workflows" / wf).read_text(encoding="utf-8")
        code = "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))
        assert "--force --no-flag" not in code, (
            f"{wf} still has a raw --force --no-flag test send; route it through "
            "--verification so the subject is tagged too")
        assert "--verification" in code, f"{wf} has no verification-tagged send path"
