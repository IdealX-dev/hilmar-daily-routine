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
import os
import re
import sys
from pathlib import Path
from unittest.mock import patch

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
              "DOC_MONO_STACK", "DOC_SANS_STACK", "DOC_TNUM",
              # ── the expressive half, added 2026-08-05 ──────────────────
              # #146 shipped the restraint tokens above and left the
              # annotation out, which is the "just boring" Michael reported.
              # These are the devices that put colour ON THE DATA.
              # NOTE: DOC_RULE_* are deliberately NOT listed here — the hex
              # test below selects by name suffix and "DOC_RULE_HAIRLINE"
              # ends in "LINE", so it would be asserted to be a bare hex when
              # it is a full CSS rule. They are covered by their own test.
              # DOC_SERIES_PINS is also absent: it is empty by design and
              # would fail the non-empty assertion.
              "DOC_GOOD_BG", "DOC_WARN_BG", "DOC_BAD_BG",
              "DOC_SERIES", "DOC_SERIES_UNKNOWN",
              "DOC_BEST_ROW_BG", "DOC_TAG_BG", "DOC_ON_SOLID",
              "DOC_BAN_BG", "DOC_BAN_FG",
              "DOC_SECTION_CHIP_BG", "DOC_SECTION_CHIP_FG",
              "DOC_CO_BORDER_WARN", "DOC_CO_BORDER_GOOD", "DOC_CO_BORDER_BAD",
              "DOC_BADGE_TONES", "DOC_TYPE", "DOC_RADIUS", "DOC_TRACK",
              "DOC_NULL", "DOC_WARN_GLYPH"]


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


# ── the expressive half: colour as annotation, not as chrome ───────────────
#
# #146 implemented the reference's restraint and left out its expression, so
# what shipped was grey. The tokens and helpers below put colour back ON THE
# DATA — which carrier a row belongs to, which row won its table, which quote
# carries a caveat — and never back on the container. These tests are the
# guard rails on that distinction.

EXPRESSION_COLOURS = [
    "DOC_GOOD_BG", "DOC_WARN_BG", "DOC_BAD_BG", "DOC_SERIES_UNKNOWN",
    "DOC_BEST_ROW_BG", "DOC_TAG_BG", "DOC_ON_SOLID", "DOC_BAN_BG",
    "DOC_BAN_FG", "DOC_SECTION_CHIP_BG", "DOC_SECTION_CHIP_FG",
    "DOC_CO_BORDER_WARN", "DOC_CO_BORDER_GOOD", "DOC_CO_BORDER_BAD",
]

# One representative call per helper. Built lazily so the sweep tests below
# exercise the live functions rather than a snapshot taken at import.
def _helper_outputs():
    return {
        "doc_badge(ok)": B.doc_badge("ok"),
        "doc_badge(warn)": B.doc_badge("warn"),
        "doc_badge(bad)": B.doc_badge("bad"),
        "doc_badge(neutral)": B.doc_badge("neutral"),
        "doc_badge_html": B.doc_badge_html("2 pending >24h", "warn"),
        "doc_dot": B.doc_dot("MSC"),
        "doc_dot_html": B.doc_dot_html("MSC"),
        "doc_tag_best": B.doc_tag_best(),
        "doc_best_row": B.doc_best_row(),
        "doc_callout(warn)": B.doc_callout("warn"),
        "doc_callout(good)": B.doc_callout("good"),
        "doc_callout(bad)": B.doc_callout("bad"),
        "doc_section_chip": B.doc_section_chip(1),
        "doc_banner": B.doc_banner("INTERNAL USE ONLY"),
        "doc_basis": B.doc_basis(),
        "doc_num": B.doc_num(),
        "doc_num(bold)": B.doc_num(True),
        "doc_total_row": B.doc_total_row(),
        "doc_card_footnote": B.doc_card_footnote(),
        "doc_method_note": B.doc_method_note(),
    }


@pytest.mark.parametrize("name", EXPRESSION_COLOURS)
def test_expression_colours_are_six_digit_lowercase_hex(name):
    assert re.fullmatch(r"#[0-9a-f]{6}", getattr(B, name)), (
        f"branding.{name} must be a 6-digit lowercase hex — Word's engine does "
        f"not parse shorthand or named colours reliably")


# ── identity hues: which party, never which status ────────────────────────

def test_the_identity_series_is_a_tuple_of_distinct_lowercase_hexes():
    assert isinstance(B.DOC_SERIES, tuple), (
        "DOC_SERIES must be an immutable sequence — a list invites a renderer "
        "to append to it and re-colour every other artifact in the run")
    assert len(B.DOC_SERIES) >= 5, "the reference carries five identity hues"
    for hue in B.DOC_SERIES:
        assert re.fullmatch(r"#[0-9a-f]{6}", hue), f"{hue} is not 6-digit lowercase hex"
    assert len(set(B.DOC_SERIES)) == len(B.DOC_SERIES), (
        "two identity hues are the same colour — the cycle would silently give "
        "two carriers the same dot before it has even wrapped")


@pytest.mark.parametrize("status", ["DOC_GOOD", "DOC_WARN", "DOC_BAD",
                                    "DOC_INK", "DOC_MUTED"])
def test_no_identity_hue_is_also_a_status_hue(status):
    """THE hazard the reference shipped with: its --nax #B9740F and its --warn
    #b9740f are the same colour in different case, so NAXCO's identity dot
    rendered in exactly the amber that a "read the caveat" badge uses.

    The reference could afford it — NAXCO never sat beside a badge. Hilmar
    can't: a carrier column sits next to a status column on every table we
    render, and identity read as status is the one failure the identity set
    exists to prevent. Index 4 is deep teal for this reason. If a later change
    "restores" #b9740f to DOC_SERIES to match the reference, this test is what
    explains why it must not.
    """
    val = getattr(B, status)
    assert val not in B.DOC_SERIES, (
        f"identity hue {val} is also branding.{status} — a carrier dot in that "
        f"colour cannot be told apart from a status signal")


def test_the_identity_green_is_not_the_status_green():
    """The reference is deliberate about this: identity #2e7d5b is a different
    green from status #1f7a4d so "this row is carrier X" can never be misread
    as "this row won"."""
    assert "#2e7d5b" in B.DOC_SERIES, "the reference's identity green is missing"
    assert B.DOC_GOOD == "#1f7a4d"
    assert B.DOC_GOOD not in B.DOC_SERIES


def test_the_first_four_identity_hues_are_the_reference_values():
    """Provenance: --mr / --ads / --es / --dt, lowercased, otherwise unchanged.
    Only the fifth was re-picked, and only because of the DOC_WARN collision."""
    assert B.DOC_SERIES[:4] == ("#c0392b", "#2e7d5b", "#8e44ad", "#2c5f8a")


# ── the carrier -> hue mapper is stable, and provably so ──────────────────

CARRIERS = ["MSC", "Maersk", "CMA CGM", "Hapag-Lloyd", "ONE", "Evergreen",
            "COSCO", "ZIM", "HMM", "Yang Ming", "OOCL", "PIL"]


def test_the_same_carrier_gets_the_same_hue_across_processes():
    """THE regression this mapper exists to prevent, and it cannot be caught
    in-process. Python randomises str hashing per interpreter (PYTHONHASHSEED)
    and run_pipeline.py launches the dashboard, the PDF and the email as three
    SEPARATE processes — so a mapper built on the built-in hash() would give
    one carrier three different colours inside a single pipeline run, while
    looking perfectly stable to a single-process test.
    """
    import subprocess
    prog = (
        f"import sys;sys.path.insert(0,{str(ROOT / 'scripts')!r});"
        f"import branding as B;"
        f"print(','.join(B.doc_series_colour(c) for c in {CARRIERS!r}))"
    )
    runs = []
    for seed in ("0", "1", "12345", "random"):
        env = {**os.environ, "PYTHONHASHSEED": seed}
        out = subprocess.run([sys.executable, "-c", prog], capture_output=True,
                             text=True, env=env, check=True)
        runs.append(out.stdout.strip())
    assert len(set(runs)) == 1, (
        "carrier colours changed between interpreters with different hash "
        f"seeds — the mapper is not deterministic across processes: {runs}")
    # and the in-process answer must agree with the subprocess answer
    assert runs[0] == ",".join(B.doc_series_colour(c) for c in CARRIERS)


def test_the_mapper_does_not_use_the_builtin_hash():
    """Parsed, not grepped: the docstrings in branding.py talk ABOUT hash() at
    length, and a regex cannot tell prose from a call."""
    import ast
    tree = ast.parse((ROOT / "scripts" / "branding.py").read_text(encoding="utf-8"))
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
             and n.func.id == "hash"]
    assert not calls, (
        "branding.py calls the built-in hash() — it is salted per process and "
        "cannot colour a carrier consistently across the three renderers")


def test_a_carriers_hue_does_not_depend_on_which_other_carriers_exist():
    """No dict-ordering dependence, no positional assignment: adding a carrier
    tomorrow must not re-colour the ones already on the page."""
    alone = B.doc_series_colour("MSC")
    for c in reversed(CARRIERS):
        B.doc_series_colour(c)
    assert B.doc_series_colour("MSC") == alone
    forward = {c: B.doc_series_colour(c) for c in CARRIERS}
    backward = {c: B.doc_series_colour(c) for c in reversed(CARRIERS)}
    assert forward == backward


def test_the_mapper_normalises_case_spacing_and_punctuation():
    assert B.doc_series_colour("MSC") == B.doc_series_colour("  msc ")
    assert B.doc_series_colour("CMA CGM") == B.doc_series_colour("cma-cgm")
    assert B.doc_series_colour("Hapag-Lloyd") == B.doc_series_colour("HAPAG LLOYD")


def test_distinct_carriers_stay_distinct():
    """Normalisation must not merge two carriers the tracker records
    separately — that would hide a data problem behind a colour."""
    assert B.doc_series_colour("Maersk") != ""
    assert B._series_key("Maersk") != B._series_key("Maersk Line")


def test_every_hue_in_the_cycle_is_reachable():
    """Proves the modulo actually spans the tuple rather than parking on one
    hue — a mapper that returns red for everything is stable and useless."""
    seen = {B.doc_series_colour(f"carrier {i}") for i in range(200)}
    assert seen == set(B.DOC_SERIES), f"unreachable hues: {set(B.DOC_SERIES) - seen}"


def test_a_missing_carrier_is_muted_not_a_hue():
    """"No carrier recorded" is its own data condition and must not look like
    a sixth carrier."""
    for blank in (None, "", "   ", "—", "-"):
        assert B.doc_series_colour(blank) == B.DOC_SERIES_UNKNOWN
    assert B.DOC_SERIES_UNKNOWN not in B.DOC_SERIES


def test_the_pin_table_is_empty_and_overrides_the_hash_when_it_is_not():
    """Empty because no production carrier data ships in this repo, so pinning
    names would be inventing them. The mechanism is tested even though the
    table is empty, because the next agent will populate it."""
    assert B.DOC_SERIES_PINS == {}, (
        "pins were added — confirm they came from the real carrier "
        "distribution, then update this test to assert those pins")
    pinned = dict(B.DOC_SERIES_PINS)
    pinned[B._series_key("Maersk")] = 2
    with patch.object(B, "DOC_SERIES_PINS", pinned):
        assert B.doc_series_colour("maersk") == B.DOC_SERIES[2]
        assert B.doc_series_colour("MSC") == B.doc_series_colour("MSC")
    assert B.DOC_SERIES_PINS == {}, "the patch leaked into module state"


def test_the_collision_reporter_names_carriers_that_share_a_dot():
    """Five hues over twelve carriers must collide. A legend built without
    checking shows two carriers wearing the same dot, which destroys the one
    thing the dot is for."""
    collisions = B.doc_series_collisions(CARRIERS)
    assert collisions, (
        "twelve carriers over five hues reported no collision — the reporter "
        "is not actually comparing hues")
    for hue, names in collisions.items():
        assert hue in B.DOC_SERIES
        assert len(names) > 1
        assert names == sorted(names), "output must be sorted to stay stable"
        assert len({B.doc_series_colour(n) for n in names}) == 1
    assert B.doc_series_collisions(CARRIERS) == B.doc_series_collisions(list(reversed(CARRIERS)))
    assert B.doc_series_collisions(["MSC"]) == {}
    assert B.doc_series_collisions([None, "", "  "]) == {}


# ── badges: the tone qualifies, the label carries the fact ────────────────

@pytest.mark.parametrize("tone", ["ok", "warn", "bad", "neutral"])
def test_a_badge_emits_its_token_pair_and_nothing_new(tone):
    fg, bg = B.DOC_BADGE_TONES[tone]
    style = B.doc_badge(tone)
    assert f"background-color:{bg}" in style
    assert f"color:{fg}" in style
    assert "border:" not in style, (
        "the reference's badges have no border — tint ground and matching "
        "text only; a border makes them read as chrome")


def test_badge_tones_reuse_existing_tokens_rather_than_inventing_colours():
    """Sixteen colours in the whole reference. Every badge hue must already be
    a named token, or the palette grows one quiet hex at a time."""
    known = {getattr(B, n) for n in DOC_TOKENS if isinstance(getattr(B, n), str)}
    for tone, (fg, bg) in B.DOC_BADGE_TONES.items():
        assert fg in known, f"badge tone {tone} foreground {fg} is not a DOC_ token"
        assert bg in known, f"badge tone {tone} background {bg} is not a DOC_ token"


def test_an_unknown_badge_tone_fails_loudly():
    """Tones come from code, never from ingested data, so an unknown one is a
    programming error. Rendering the wrong signal quietly is worse."""
    with pytest.raises(ValueError, match="unknown badge tone"):
        B.doc_badge("green")
    with pytest.raises(ValueError, match="unknown callout tone"):
        B.doc_callout("info")


def test_a_badge_label_is_escaped():
    out = B.doc_badge_html('2 & <b>pending</b>', "warn")
    assert "<b>" not in out
    assert "&amp;" in out and "&lt;b&gt;" in out


def test_the_winning_row_tint_is_the_same_value_as_the_ok_badge_ground():
    """One system, not two greens: the reference reuses --bestbg for both so
    a tinted row and an ok badge read as the same statement."""
    assert B.DOC_BEST_ROW_BG == B.DOC_GOOD_BG
    assert B.doc_best_row() == f"background-color:{B.DOC_GOOD_BG}"


def test_the_three_solid_fills_are_drawn_from_existing_tokens():
    """The reference contains exactly three solid colour fills — the red
    classification strip, the ink section chip, the one green LOWEST pill.
    A fourth is the signal that the hierarchy is wrong somewhere else."""
    assert B.DOC_BAN_BG == B.DOC_BAD
    assert B.DOC_SECTION_CHIP_BG == B.DOC_INK
    assert B.DOC_TAG_BG == B.DOC_GOOD
    assert len({B.DOC_BAN_BG, B.DOC_SECTION_CHIP_BG, B.DOC_TAG_BG}) == 3
    for fg in (B.DOC_BAN_FG, B.DOC_SECTION_CHIP_FG):
        assert fg == B.DOC_ON_SOLID


def test_the_callout_default_is_watch_not_good():
    """The reference's bare .co is warn and .good OVERRIDES it — observed
    ratio one good to five warn. Inverting that turns every judgement into
    a congratulation."""
    assert B.doc_callout() == B.doc_callout("warn")
    assert f"border-left:4px solid {B.DOC_WARN}" in B.doc_callout()
    assert f"border-left:4px solid {B.DOC_GOOD}" in B.doc_callout("good")


def test_a_callout_colours_only_its_edge():
    """Colour appears in the 4px left edge, never as a tint behind the text —
    that asymmetry is the device."""
    style = B.doc_callout("bad")
    assert f"background-color:{B.DOC_CARD}" in style
    assert f"color:{B.DOC_INK}" in style
    assert f"background-color:{B.DOC_BAD}" not in style


# ── every helper is Outlook-safe and pure ─────────────────────────────────

def test_no_helper_output_can_be_mistaken_for_an_unrendered_placeholder():
    """Guards the email's placeholder-leak test from the other side: if a
    helper ever returned a string containing braces, embedding it in the body
    would trip that detector — or worse, hide a real leak."""
    for label, out in _helper_outputs().items():
        leaks = re.findall(r"\{[A-Za-z_][A-Za-z0-9_]{2,}\}", out)
        assert not leaks, f"{label} emits placeholder-shaped text: {leaks}"
        assert "{" not in out and "}" not in out, f"{label} emits braces"


def test_every_helper_is_outlook_safe():
    """Word's engine: no custom properties, no flex, no grid. A helper that
    reaches for any of them renders unstyled on every desk at OL."""
    for label, out in _helper_outputs().items():
        assert "var(--" not in out, f"{label} uses a CSS custom property"
        assert "display:flex" not in out, f"{label} uses flex"
        assert "display:grid" not in out, f"{label} uses grid"
        assert "linear-gradient" not in out, f"{label} uses a gradient"
        assert "box-shadow" not in out, f"{label} uses a shadow"
        assert "http" not in out, f"{label} pulls remote content"


def test_every_colour_a_helper_emits_is_six_digit_lowercase_hex():
    for label, out in _helper_outputs().items():
        for hexval in re.findall(r"#[0-9a-fA-F]+", out):
            assert re.fullmatch(r"#[0-9a-f]{6}", hexval), (
                f"{label} emits {hexval} — shorthand and uppercase hex are not "
                f"parsed reliably by Word's engine")


def test_the_helpers_are_pure():
    """No clock, no randomness, no module state: two calls in a row must be
    byte-identical, and calling them must not mutate the tokens."""
    before = {n: getattr(B, n) for n in DOC_TOKENS}
    first = _helper_outputs()
    second = _helper_outputs()
    assert first == second
    assert {n: getattr(B, n) for n in DOC_TOKENS} == before, (
        "a helper mutated a shared token")


def test_style_helpers_have_no_trailing_semicolon():
    """Matches the TH_STYLE / H2_STYLE convention already in gen_email, so a
    caller can append its own declarations without producing ';;'."""
    for label in ("doc_badge(ok)", "doc_dot", "doc_best_row", "doc_callout(warn)",
                  "doc_basis", "doc_num", "doc_total_row", "doc_card_footnote",
                  "doc_method_note"):
        assert not _helper_outputs()[label].endswith(";"), f"{label} ends with ';'"


# ── the measured scales ───────────────────────────────────────────────────

def test_the_type_scale_has_no_step_between_the_section_head_and_the_title():
    """The reference's hierarchy below the h1 is carried by WEIGHT AND RULE,
    not by size — there is deliberately nothing between 15px and 22px. A new
    18px heading means someone reached for size instead of a rule."""
    sizes = sorted(float(v.removesuffix("px")) for v in B.DOC_TYPE.values())
    assert 15.0 in sizes and 22.0 in sizes
    assert not [s for s in sizes if 15.0 < s < 22.0], (
        f"a mid-size heading crept into the type scale: {sizes}")


def test_the_tag_and_the_badge_are_never_the_same_shape():
    """4px vs 5px radius, so the once-per-document LOWEST pill and an ordinary
    badge never read as the same object."""
    assert B.DOC_RADIUS["tag"] != B.DOC_RADIUS["badge"]
    assert B.DOC_RADIUS["dot"] == "50%"


def test_the_tracking_scale_runs_quiet_to_loud():
    vals = [float(B.DOC_TRACK[k].removesuffix("em")) for k in ("quiet", "key", "loud")]
    assert vals == sorted(vals) and len(set(vals)) == 3


def test_there_are_exactly_four_rule_weights_and_each_uses_a_token():
    """1px hairline, 1px dashed, 1.5px ink, 2px ink — each means something
    different, and none of them hardcodes a colour."""
    rules = [B.DOC_RULE_HAIRLINE, B.DOC_RULE_DASHED, B.DOC_RULE_TOTAL,
             B.DOC_RULE_SECTION]
    assert len(set(rules)) == 4
    assert f"1px solid {B.DOC_LINE}" == B.DOC_RULE_HAIRLINE
    assert f"1px dashed {B.DOC_LINE}" == B.DOC_RULE_DASHED
    assert f"1.5px solid {B.DOC_INK}" == B.DOC_RULE_TOTAL
    assert f"2px solid {B.DOC_INK}" == B.DOC_RULE_SECTION
    assert B.DOC_RULE_TOTAL in B.doc_total_row()
    assert B.DOC_RULE_DASHED in B.doc_card_footnote()


def test_the_null_placeholder_and_severity_glyph_are_entities():
    """Both must survive Outlook, where images are blocked and CSS is
    unreliable — a character entity always renders."""
    assert B.DOC_NULL == "&mdash;"
    assert B.DOC_WARN_GLYPH == "&#9888;"


def test_the_figure_helper_marks_the_decision_column():
    """Bold on EVERY row of the one column the reader is comparing; the row
    tint and the LOWEST pill then mark the winner within it."""
    assert "font-weight:700" in B.doc_num(bold=True)
    assert "font-weight:700" not in B.doc_num()
    for style in (B.doc_num(), B.doc_num(True), B.doc_basis()):
        assert B.DOC_MONO_STACK in style
        assert "text-align:right" in style


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
