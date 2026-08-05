"""
branding.py — Hilmar logo / brand asset access helpers.

Single source of truth for getting the Hilmar logo into HTML emails,
dashboards, audit reports, and reportlab PDFs.

Per Michael 2026-05-14: "can you save this in your schema/data base and
also add it to the system fo rhilmar? properly, checked as vector?"

USAGE
    from branding import logo_data_uri, logo_reportlab_image, has_logo

    # HTML (email, dashboard, audit, weekly summary)
    if has_logo():
        html += f'<img src="{logo_data_uri()}" alt="Hilmar" height="36">'

    # PDF (reportlab)
    from reportlab.platypus import Image
    img = logo_reportlab_image(width=140)  # may be None if no file
    if img: story.append(img)

VECTOR PREFERENCE
If a .svg exists at assets/branding/hilmar-logo.svg, it's preferred for
HTML (true vector — scales perfectly). If only .png exists, that's
base64-embedded inside an SVG wrapper (raster, but still travels in-line).
PDFs always use the raster since reportlab's SVG support is limited.

GRACEFUL DEGRADATION
If no logo file exists, all helpers no-op and HTML/PDF fall back to the
emoji + text header that was there before. No errors, no broken images —
the logo just doesn't appear.
"""
from __future__ import annotations

import base64
import hashlib
import html
import re
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BRAND_DIR = ROOT / "assets" / "branding"

LOGO_PNG = BRAND_DIR / "hilmar-logo.png"
LOGO_SVG = BRAND_DIR / "hilmar-logo.svg"

# Hilmar brand colors — for headers, accents, etc.
HILMAR_BLUE = "#1a3d9c"
HILMAR_GREEN = "#76b82a"
HILMAR_NAVY = "#0a2350"

# ── Document design tokens ────────────────────────────────────────────────
# Michael shared an internal OL air-freight comparison on 2026-07-22 and
# called the formatting gorgeous, then "i like it all" — dashboard, PDF and
# email. #138 restyled the dashboard against it; these constants are the
# same palette lifted out so the other two renderers cannot drift from it.
# THREE COPIES OF A PALETTE IS NOT A DESIGN LANGUAGE. Every hex below was
# read out of that reference document's :root block.
#
# The idea, in one line: warm paper ground, hairline rules instead of drop
# shadows, every figure in monospace so decimals align down the column, and
# quiet uppercase table headers so the DATA is the loud part.
DOC_PAPER = "#f4f3ef"    # warm paper ground (reference --bg)
DOC_CARD = "#ffffff"     # card / table surface (--card)
DOC_INK = "#1f2328"      # body text (--ink)
DOC_MUTED = "#5f6670"    # labels, captions, table headers (--muted)
DOC_LINE = "#e3e1da"     # hairline rule — replaces every box-shadow (--line)
DOC_TH_BG = "#fbfaf7"    # table-header ground, a half-step off the card

# Semantic accents, also from the reference. Deliberately desaturated
# against the old SaaS palette (#059669 / #dc2626): on paper they read as
# annotation rather than as alert.
DOC_GOOD = "#1f7a4d"     # (--best)
DOC_GOOD_BG = "#eaf6ef"  # (--bestbg)
DOC_WARN = "#b9740f"     # (--warn)
DOC_WARN_BG = "#fdf4e3"  # (--warnbg)
DOC_BAD = "#b03030"      # (--red)
DOC_BAD_BG = "#fdeeee"   # no reference equivalent; tinted from DOC_BAD

# PENDING is deliberately NOT one of the three above. Good/warn/bad are
# verdicts on a row; "still pending" is a statement about whose turn it is,
# and colouring it as a verdict is how a reader concludes an open quote has
# already gone wrong. So it takes an IDENTITY hue — the reference's wisteria,
# which is also DOC_SERIES[2].
#
# That shared value is a real, accepted collision: a carrier that hashes to
# series index 2 gets a dot the same colour as a pending marker. It is
# tolerable because the two never carry meaning alone — a carrier dot always
# sits beside the carrier's NAME (see the DOC_SERIES note below) and a pending
# marker always sits inside a pill that says PENDING. Named here rather than
# spelled DOC_SERIES[2] at each call site so the intent survives the next edit:
# three different purples used to mean "pending" across the dashboard alone.
DOC_PENDING = "#8e44ad"
DOC_PENDING_BG = "#f3ecf7"

# Neutral information — "quoted", "biz hours", a middling turnaround. Not a
# verdict either, so it stays out of good/warn/bad for the same reason
# DOC_PENDING does. Shares the reference's steel blue with DOC_SERIES[3] on
# the same accepted-collision terms as DOC_PENDING above.
DOC_INFO = "#2c5f8a"
DOC_INFO_BG = "#e8eff5"

# Both of the above exist so no CALL SITE ever spells DOC_SERIES[n]. An
# integer index into a carrier-identity tuple cannot say what it means: a
# reader cannot tell "the pending hue" from "whichever carrier sorted third",
# and repurposing the series — which is a palette-level decision — would
# silently repaint semantics that merely borrowed a value. Named tokens make
# the two independent. tests/test_document_restyle.py enforces it.

# The reference loads IBM Plex from a CDN. We deliberately do NOT: these
# documents ship as email bodies and email attachments, opened from Outlook,
# often offline and always behind OL's proxy. A local stack renders the same
# document the same way on every desk. Mono is what makes a column of
# figures readable, so it is the stack that matters most.
DOC_MONO_STACK = ("ui-monospace,'SF Mono','Cascadia Mono','Segoe UI Mono',"
                  "Menlo,Consolas,'Liberation Mono',monospace")
DOC_SANS_STACK = ("'Segoe UI',-apple-system,BlinkMacSystemFont,Inter,"
                  "Helvetica,Arial,sans-serif")

# Tabular figures: same advance width for every digit, so numbers line up
# column-over-column even outside a mono face.
DOC_TNUM = "font-variant-numeric:tabular-nums;font-feature-settings:'tnum' 1"


# ══ Document design tokens, part 2: ANNOTATION ═════════════════════════════
#
# Everything above is the reference's RESTRAINT — its paper, its hairlines,
# its quiet uppercase headers, its mono figures. #146 shipped that half and
# stopped there. Michael's read on 2026-08-05: "just boring", "too plain",
# and explicitly "for internal too" — so the dashboard, the staff email and
# the PDF all need what follows, not just the client email.
#
# COLOUR AS ANNOTATION vs COLOUR AS CHROME
# ────────────────────────────────────────
# This distinction is the entire point of this block. Read it before adding a
# hex below.
#
#   CHROME is colour spent on the CONTAINER: a saturated navy table-header
#   bar, a dark-red "losing lanes" bar, a gradient masthead, six solid KPI
#   blocks with white text. It paints the same pixels regardless of what the
#   data says. Change every number on the page and not one pixel of it moves.
#   #146 was right to delete all of it.
#
#   ANNOTATION is colour spent on the DATA: this row won its table, this quote
#   carries a caveat, this figure came from THAT carrier. It is a function of
#   the record, so if the data changes the colour moves with it. That makes it
#   readable — the hue is another column, not decoration.
#
# #146 removed the chrome AND the annotation, which is why what is left reads
# as grey. This block restores only the second kind.
#
# The reference is disciplined about this to the point of arithmetic: 16
# distinct colours across 229 lines; exactly THREE solid fills in the whole
# document (the red classification strip, the ink section-number chip, the one
# green LOWEST pill); zero box-shadows, zero gradients, zero transitions, zero
# coloured body text. Everything else carrying a hue is a pale tint ground, an
# 11px dot, or a 4px left edge.
#
# That restraint is what makes the annotation legible: on a page with no
# chrome, one tinted row IS the answer. So the check to run against any new
# surface is — if it needs a shadow or a fourth solid fill to read, the
# hierarchy is wrong somewhere else. Fix the hierarchy, don't add the colour.
#
# PROVENANCE TAGS, per token below:
#   [REF] read out of the 2026-07-22 reference document's :root / rules.
#   [EXT] a deliberate Hilmar extension with NO reference equivalent. The
#         reference compared five agents on one air shipment; Hilmar reports a
#         daily rate desk, which has states that document never had. Extensions
#         are labelled so nobody later "restores" them to a reference that
#         never contained them — the mistake DOC_BAD_BG (above) documents.

# ── Identity hues — which counterparty is this? ────────────────────────────
# DATA CONDITION: party identity. Which carrier (reference: which agent) a
# row, card, or legend entry belongs to. This is NOT status and must never be
# read as status. The reference is explicit about that: its identity green
# #2e7d5b is a DIFFERENT green from its status green #1f7a4d (DOC_GOOD),
# chosen so "this row is ADS" can never be misread as "this row won".
#
# The device's power comes from repetition, not from the colours themselves:
# the reference emits one party's hue in three places — the legend, that
# party's card header, and every table row belonging to it (20 dots over one
# document). That is what lets a reader follow a single carrier across four
# tables without re-reading its name. Use it the same way or don't use it.
#
# [REF] indices 0-3 are the reference's --mr / --ads / --es / --dt verbatim.
#       Only the case changed: the reference pasted them in uppercase, this
#       file is lowercase throughout and the hex-format test enforces that.
#       The values are unchanged.
# [EXT] index 4 is NOT the reference's --nax #b9740f. That value is
#       byte-identical to DOC_WARN, so a carrier's identity dot would render
#       in exactly the amber a "warn" badge uses for "usable, but read the
#       caveat" — identity misread as status, the one failure this set exists
#       to prevent. The reference could afford it (NAXCO was the sole
#       destination agent and never appeared beside a badge). Hilmar can't: a
#       carrier column sits next to a status column on every table we render.
#       Deep teal #0f6f76 replaces it — distinct from both greens, from the
#       steel blue, and from every status hue. The invariant is enforced by
#       test, not by memory: see test_document_restyle.py.
#
# The cycle REPEATS past five parties. That is deliberate and it is why the
# legend must always print the carrier NAME beside its dot (the reference
# legend prints "M+R Forwarding (Jason Hu, SHA)", never a bare dot). The dot
# is a tracking aid across a page; the name is the key.
DOC_SERIES = (
    "#c0392b",   # [REF] --mr   pomegranate red
    "#2e7d5b",   # [REF] --ads  forest green (identity green, NOT DOC_GOOD)
    "#8e44ad",   # [REF] --es   wisteria purple
    "#2c5f8a",   # [REF] --dt   steel blue
    "#0f6f76",   # [EXT] replaces --nax #b9740f, which collides with DOC_WARN
)

# Party with no name on the record. DATA CONDITION: "carrier not stated" —
# rendered muted so an absent party reads as absent rather than as a sixth
# carrier. Pairs with DOC_NULL for the cell text.
DOC_SERIES_UNKNOWN = DOC_MUTED

# Hand-assigned hues, which is how the reference did it (--mr, --ads, --es,
# --dt were assigned by a person, one per agent). Keys are _series_key()
# normalised names; values are indices into DOC_SERIES. doc_series_colour
# consults this FIRST and falls back to the stable hash below, so pinning is
# one decision read by all three renderers rather than three.
#
# EMPTY ON PURPOSE, and this is a gap, not a finished job: no production
# tracking data ships in this repo (it lives on the Cloud PC), so the real
# Hilmar carrier distribution could not be verified here. Pinning carrier
# names I have not seen in the data would be inventing them.
# NEXT STEP for whoever has the real file: take the top five carriers by row
# count, pin them to indices 0-4, and the five that matter stop colliding.
# Until then every carrier is hashed, and with five hues over more carriers
# than that, two WILL share a hue — call doc_series_collisions() to find out
# which before you build a legend.
DOC_SERIES_PINS: dict[str, int] = {}

# ── Status tints — badges, and the row that won ───────────────────────────
# Foreground/background pairs for the pill badge. Tint ground, matching darker
# text on top, NO border (the reference's badges have no border at all).
#
# Correcting the assumption that keeps coming back: "ok" does NOT mean "best".
# In the reference b-ok reads "Lowest all-in", "Complete", and "Only BRU
# quote" — i.e. it means "this record carries no disqualifying exception", and
# the LABEL carries the actual fact in one to three words. b-warn reads
# "Deferred only" and "High cartage" — "usable, but read the caveat", never
# "bad". Badges are not rare in the reference (5 badges over 5 cards); a badge
# with a vague label is the failure mode, not a badge that appears often.
#
# [REF] ok / warn are the reference's b-ok / b-warn pairs exactly.
# [EXT] bad — the reference has no failure state (nothing in a cost comparison
#       can be broken). Hilmar reports SLA breaches, stuck QC checks and
#       impossible states, which genuinely are. Keep it scarce: DOC_BAD is
#       otherwise the classification-strip colour, and the reference never lets
#       red touch a data surface.
# [EXT] neutral — "recorded, no judgement" (e.g. Pending). No new hex: it is
#       DOC_MUTED on DOC_TH_BG, so it stays quieter than every real signal.
DOC_BADGE_TONES = {
    "ok": (DOC_GOOD, DOC_GOOD_BG),
    "warn": (DOC_WARN, DOC_WARN_BG),
    "bad": (DOC_BAD, DOC_BAD_BG),
    "neutral": (DOC_MUTED, DOC_TH_BG),
}

# DATA CONDITION: cheapest / best option WITHIN ITS OWN TABLE. Scoped to the
# table it sits in, not to the document — the reference tints three rows
# across three tables, one of which is more expensive than every row in the
# table above it and is still correctly tinted, because it wins its own
# cohort. Deliberately the SAME value as DOC_GOOD_BG so the row tint and the
# "ok" badge read as one system rather than two greens.
# [REF] tr.best td{background:var(--bestbg)}
DOC_BEST_ROW_BG = DOC_GOOD_BG

# DATA CONDITION: the single global headline answer — the one row in the whole
# document that is the answer. The reference uses this pill EXACTLY ONCE; its
# scarcity is the device. Three rows get the tint, one gets the pill.
# [REF] .tag-best
DOC_TAG_BG = DOC_GOOD

# The one foreground used on all three solid fills. There are only three
# (classification strip, section chip, LOWEST pill) — if a fourth appears,
# the hierarchy is wrong.
# [REF] #fff on .ban / h2 .n / .tag-best
DOC_ON_SOLID = "#ffffff"

# DATA CONDITION: document-level handling classification / distribution
# restriction — who may see this, and what the numbers are NOT. Not a status
# and not an alert. This is the ONLY place the reference uses --red, and it is
# a solid fill with white text, never a tint. For Hilmar this is the
# internal-only strip on the dashboard, the staff email and the PDF.
# [REF] .ban  (alias of DOC_BAD so the two can never drift apart)
DOC_BAN_BG = DOC_BAD
DOC_BAN_FG = DOC_ON_SOLID

# DATA CONDITION: section sequence — sections are numbered so a reader can say
# "see 3". A 22x22 ink-filled rounded square holding a mono digit, sitting
# inline inside the h2. Second of the three solid fills.
# [REF] h2 .n
DOC_SECTION_CHIP_BG = DOC_INK
DOC_SECTION_CHIP_FG = DOC_ON_SOLID

# ── Callout edges — an analyst judgement the table can't state ────────────
# DATA CONDITION: a judgement about the data, carried by a 4px LEFT border on
# an otherwise plain white card. The asymmetry is the whole device: the card
# ground stays white and the text stays ink, the colour appears only in the
# edge. Never tint the body of a callout.
#
# Note the default: the reference's bare .co is WARN, and .good OVERRIDES it.
# So the default reading of a callout is "thing to watch", and green is the
# exception — observed ratio 1 good : 5 warn. Do not invert that.
# [REF] .co (warn) / .co.good (best)
# [EXT] bad — same reasoning as the "bad" badge tone. Rare by design.
DOC_CO_BORDER_WARN = DOC_WARN
DOC_CO_BORDER_GOOD = DOC_GOOD
DOC_CO_BORDER_BAD = DOC_BAD

# ── The measured scales ───────────────────────────────────────────────────
# Extracted mechanically from the reference, not eyeballed. Three renderers
# each guessing "about 11px" is how the last drift happened.
#
# TYPE: ten steps, weighted hard to the small end and full of half-pixels —
# much of why the reference reads as a printed document rather than a web page.
# There is nothing between 15px and 22px: below the h1 the hierarchy is carried
# by WEIGHT AND RULE, never by size. If a new heading wants 18px, it wants a
# rule instead.
DOC_TYPE = {
    "micro": "10.5px",    # badge, tag-best, card footnote
    "fine": "11px",       # classification strip, chip key, basis
    "small": "11.5px",    # card sub-line, grid th, method note, footer
    "chip": "12px",       # section-chip digit, routing codes
    "body": "12.5px",     # chip, legend, itemization table
    "base": "13px",       # sub-header, cohort heading, grid, callout
    "figure": "13.5px",   # chip value, totals row
    "name": "14.5px",     # card name
    "section": "15px",    # h2
    "title": "22px",      # h1
}

# RADIUS: six steps, each tied to an object class. tag-best is 4px and badge
# is 5px specifically so the two never read as the same object.
# NOTE for email: Word's engine ignores border-radius entirely, so every pill
# and dot below degrades to a square swatch in desktop Outlook. That is
# acceptable — the colour and the label still carry the meaning — but do not
# design something whose meaning depends on its being round.
DOC_RADIUS = {
    "tag": "4px",
    "badge": "5px",
    "chip": "5px",
    "ban": "6px",
    "card": "8px",
    "acard": "10px",
    "dot": "50%",
}

# TRACKING: three steps. The grid header is the QUIETEST thing on the page and
# the classification strip is the loudest; the gradient between them is
# deliberate.
DOC_TRACK = {
    "quiet": ".03em",     # grid th
    "key": ".04em",       # chip key
    "loud": ".05em",      # classification strip, cohort heading
}

# RULE WEIGHTS: exactly four, and each one means something different.
# hairline = structure; dashed = "this detaches from the arithmetic above it";
# total = "this is the sum"; section = "a new section starts here" (the
# heaviest rule in the document, under a 15px heading).
DOC_RULE_HAIRLINE = f"1px solid {DOC_LINE}"
DOC_RULE_DASHED = f"1px dashed {DOC_LINE}"
DOC_RULE_TOTAL = f"1.5px solid {DOC_INK}"
DOC_RULE_SECTION = f"2px solid {DOC_INK}"

# ── Content-level conventions ─────────────────────────────────────────────
# DATA CONDITION: "this field was not supplied" — asserted, never left blank.
# A reader must never have to wonder whether an empty cell means zero,
# unknown, or a rendering bug. [REF] &mdash; in two grid cells.
DOC_NULL = "&mdash;"

# DATA CONDITION: the single highest-severity item in a set of callouts.
# Prepended INSIDE the bolded lede, not as a separate icon element, so it
# escalates one item without introducing a sixth colour. Worth carrying over
# precisely because it is a character entity: it renders in Outlook, where
# CSS is unreliable and images are blocked. [REF] &#9888; used once.
DOC_WARN_GLYPH = "&#9888;"


# ── Pure helpers that emit INLINE style strings ───────────────────────────
# WHY INLINE, AND WHY HERE
# Desktop Outlook renders HTML with Word's engine: no CSS custom properties,
# no flex, no grid, and <style> blocks ignored except @media. So gen_email.py
# and gen_client_email.py cannot use a class for any of this — every token has
# to arrive as literal hex inside a style="" attribute. Three renderers each
# hand-rolling the same badge is exactly how the last drift happened, so the
# markup-level devices live here as functions rather than as three copies.
#
# CONTRACT, relied on by tests:
#   * pure — no I/O, no module state, no clock, no randomness;
#   * output contains NO "{" or "}", so a helper's result can never be
#     mistaken for an unrendered f-string placeholder by the email leak test;
#   * output contains no var(), no flex, no grid, no shorthand hex;
#   * no trailing semicolon, so callers can append (`f'{doc_num()};width:80px'`)
#     which matches the existing TH_STYLE / H2_STYLE convention in gen_email.

def _series_key(name: str | None) -> str:
    """Normalise a party name to its identity key.

    Case, accents, spacing and punctuation are stripped so "CMA CGM",
    "cma cgm" and "CMA-CGM" are one carrier. Distinct names stay distinct:
    "Maersk" and "Maersk Line" are two keys, because the tracker records them
    as two carriers and pretending otherwise would hide a data problem.
    """
    s = unicodedata.normalize("NFKD", str(name or ""))
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", "", s.casefold())


def doc_series_colour(name: str | None) -> str:
    """Return the stable identity hue for a carrier/party name.

    Same name -> same hue on every render, on every host, forever.

    WHY sha256 AND NOT hash(): Python randomises str hashing per process
    (PYTHONHASHSEED), and run_pipeline.py launches the dashboard, the PDF and
    the email as THREE SEPARATE processes. Built-in hash() would therefore
    give the same carrier three different colours inside one pipeline run, and
    a fourth set tomorrow. It would also look correct in a single-process
    test. sha256 of the normalised key is stable across processes, hosts and
    interpreter versions.

    Dict iteration order is not consulted anywhere in this function — the hue
    depends on the name alone, never on how many other carriers exist, what
    order they arrived in, or which of them changed. Adding a carrier never
    re-colours the others.

    With five hues and more than five carriers the cycle repeats: two carriers
    can share a hue. That is why the legend always prints the name.

    An empty / missing name returns DOC_SERIES_UNKNOWN (muted), not a hue —
    "no carrier recorded" is a data condition of its own.
    """
    key = _series_key(name)
    if not key:
        return DOC_SERIES_UNKNOWN
    pinned = DOC_SERIES_PINS.get(key)
    if pinned is not None:
        return DOC_SERIES[pinned % len(DOC_SERIES)]
    digest = hashlib.sha256(key.encode("utf-8")).digest()
    return DOC_SERIES[int.from_bytes(digest[:8], "big") % len(DOC_SERIES)]


def doc_series_collisions(names) -> dict[str, list[str]]:
    """Which parties in `names` share a hue. Returns {hue: [names]} for hues
    claimed by more than one party; empty dict means the legend is unambiguous.

    Five hues over more than five carriers must collide, so a legend built
    without checking can show two different carriers wearing the same dot —
    which quietly destroys the one thing the dot is for. Call this before
    rendering a legend: a non-empty result means either pin those carriers in
    DOC_SERIES_PINS or drop the dots from that surface.

    Output is fully sorted, so it is stable regardless of the order the caller
    supplies names in.
    """
    seen: dict[str, set[str]] = {}
    for n in names:
        label = str(n or "").strip()
        if not _series_key(label):
            continue
        seen.setdefault(doc_series_colour(label), set()).add(label)
    return {hue: sorted(v) for hue, v in sorted(seen.items()) if len(v) > 1}


def doc_dot(name: str | None, size: int = 11) -> str:
    """Inline style for the identity disc that precedes a party's name.

    The single highest-leverage device in the reference. Emit it in all three
    places for the same party — legend, card header, table cell — or it is
    just decoration. Degrades to a square swatch in desktop Outlook (no
    border-radius in Word's engine), which still reads as a colour key.
    """
    return (f"display:inline-block;width:{size}px;height:{size}px;"
            f"border-radius:{DOC_RADIUS['dot']};margin-right:6px;"
            f"vertical-align:middle;background-color:{doc_series_colour(name)}")


def doc_dot_html(name: str | None, size: int = 11) -> str:
    """The dot as a ready <span>. Carries no text — the caller prints the
    name beside it, always."""
    return f'<span style="{doc_dot(name, size)}"></span>'


def doc_badge(tone: str = "ok") -> str:
    """Inline style for a pill badge. `tone` is a key of DOC_BADGE_TONES.

    Raises ValueError on an unknown tone, deliberately: tones come from code,
    never from ingested data, so an unknown one is a programming error and
    silently rendering the wrong signal is worse than failing the render.
    """
    try:
        fg, bg = DOC_BADGE_TONES[tone]
    except KeyError:
        raise ValueError(
            f"unknown badge tone {tone!r}; valid tones are "
            f"{', '.join(sorted(DOC_BADGE_TONES))}") from None
    # display:inline-block is not in the reference (it styles a <span> that
    # CSS already lays out); Word's engine ignores padding on a pure inline
    # box, so without it the pill loses its ground in desktop Outlook.
    return (f"display:inline-block;font-size:{DOC_TYPE['micro']};padding:3px 9px;"
            f"border-radius:{DOC_RADIUS['badge']};font-weight:600;"
            f"white-space:nowrap;background-color:{bg};color:{fg}")


def doc_badge_html(label: str, tone: str = "ok") -> str:
    """A complete badge. The LABEL carries the fact ("2 pending >24h"), the
    tone carries only whether it qualifies or caveats — see DOC_BADGE_TONES.
    Keep labels to one to three words; escaped, so caller data is safe."""
    return f'<span style="{doc_badge(tone)}">{html.escape(str(label))}</span>'


def doc_tag_best(label: str = "LOWEST") -> str:
    """The solid pill marking the ONE global winner. Used once per document —
    if a second one appears on a page, the page no longer has an answer."""
    return (f'<span style="display:inline-block;background-color:{DOC_TAG_BG};'
            f"color:{DOC_ON_SOLID};font-size:{DOC_TYPE['micro']};padding:2px 7px;"
            f"border-radius:{DOC_RADIUS['tag']};font-weight:600;margin-left:6px\">"
            f'{html.escape(str(label))}</span>')


def doc_best_row() -> str:
    """Tint for the row that wins ITS OWN table.

    Apply to every <td> of the row, not to the <tr>: Word's engine does not
    reliably paint a background set on a table row. The reference's own CSS
    targets `tr.best td` for the same reason it renders correctly — the tint
    has to run edge to edge under the clipped corners.
    """
    return f"background-color:{DOC_BEST_ROW_BG}"


def doc_callout(tone: str = "warn") -> str:
    """Inline style for a callout card: 4px coloured LEFT edge, hairline on
    the other three sides, white ground, ink text. Default is "warn" because
    the reference's default callout is a thing to watch; "good" is the
    exception, not the norm. Open the body with a bolded claim, then the
    evidence, with the decisive figures re-bolded inside the sentence."""
    edge = {"warn": DOC_CO_BORDER_WARN, "good": DOC_CO_BORDER_GOOD,
            "bad": DOC_CO_BORDER_BAD}
    if tone not in edge:
        raise ValueError(f"unknown callout tone {tone!r}; valid tones are "
                         f"{', '.join(sorted(edge))}")
    return (f"background-color:{DOC_CARD};border:{DOC_RULE_HAIRLINE};"
            f"border-left:4px solid {edge[tone]};border-radius:{DOC_RADIUS['card']};"
            f"padding:12px 14px;font-size:{DOC_TYPE['base']};color:{DOC_INK}")


def doc_section_chip(number: int | str) -> str:
    """The dark mono chip carrying a section NUMBER, rendered inline inside
    the heading. Sections are numbered so a reader can say "see 3".

    The reference centres the digit with flex; this uses line-height instead
    so it survives Word's engine.
    """
    return (f'<span style="display:inline-block;background-color:{DOC_SECTION_CHIP_BG};'
            f"color:{DOC_SECTION_CHIP_FG};font-family:{DOC_MONO_STACK};"
            f"font-size:{DOC_TYPE['chip']};width:22px;height:22px;line-height:22px;"
            f"text-align:center;border-radius:{DOC_RADIUS['chip']};margin-right:8px\">"
            f'{html.escape(str(number))}</span>')


def doc_banner(text: str) -> str:
    """The solid classification strip: who may see this document, and what the
    numbers are NOT. One per document, above the h1. The only solid red on the
    page — it states handling, never status, and never touches data."""
    return (f'<div style="background-color:{DOC_BAN_BG};color:{DOC_BAN_FG};'
            f"font-size:{DOC_TYPE['fine']};font-weight:600;"
            f"letter-spacing:{DOC_TRACK['loud']};text-align:center;padding:5px;"
            f"border-radius:{DOC_RADIUS['ban']};margin-bottom:16px\">"
            f'{html.escape(str(text))}</div>')


def doc_basis() -> str:
    """Inline style for the derivation cell that sits immediately LEFT of an
    amount — "$0.10x8355 min35" beside "$835.50".

    The most repeated device in the reference (33 uses) and the one that makes
    a figure auditable at a glance. Two grammars in one column: a per-unit
    basis ("/bill", "/shpt", "flat") for flat charges, and an explicit
    multiplication with its minimum for computed ones. On a totals row the same
    cell carries RECONCILIATION instead — "(agent stated $1,275)".

    Emit the cell EMPTY rather than omitting it when there is nothing to
    derive, so the column holds its width.
    """
    return (f"color:{DOC_MUTED};font-size:{DOC_TYPE['fine']};"
            f"font-family:{DOC_MONO_STACK};padding-left:10px;text-align:right;"
            f"white-space:nowrap")


def doc_num(bold: bool = False) -> str:
    """Inline style for a numeric cell — mono, right-aligned, tabular.

    Set it on the <th> as well as the <td> so the label sits over its own
    decimals. `bold=True` marks the DECISION COLUMN — the one column the
    reader is actually comparing — on every row, not only on the winner; the
    row tint and the LOWEST pill then mark the winner within it.
    """
    weight = "font-weight:700;" if bold else ""
    return (f"text-align:right;font-family:{DOC_MONO_STACK};{weight}{DOC_TNUM}")


def doc_total_row() -> str:
    """Inline style for the cells of a totals row: the third rule weight
    (heavier than a hairline, lighter than a section rule), bolder and one
    step larger than the body. Pair it with a doc_basis() cell carrying the
    reconciliation against the source figure."""
    return (f"border-top:{DOC_RULE_TOTAL};padding-top:7px;font-weight:700;"
            f"font-size:{DOC_TYPE['figure']}")


def doc_card_footnote() -> str:
    """Inline style for the card footnote — what is NOT in the total, and what
    could still move it. The DASHED rule is the signal: it detaches the caveat
    from the arithmetic above in a way a solid rule would not. This is where
    the honest ugliness goes — exclusions, at-cost items, direct quotes from
    the counterparty, and risk."""
    return (f"font-size:{DOC_TYPE['micro']};color:{DOC_MUTED};margin-top:9px;"
            f"padding-top:8px;border-top:{DOC_RULE_DASHED}")


def doc_method_note() -> str:
    """Inline style for the note under a table: how the column above was
    computed, and what it excludes. This is doc_basis() one altitude up — the
    same auditability instinct at table level. The reference puts one under
    every table, and each states a formula PLUS an exclusion."""
    return f"font-size:{DOC_TYPE['small']};color:{DOC_MUTED};margin-top:7px"


def has_logo() -> bool:
    """True if any logo file is available on disk."""
    return LOGO_PNG.exists() or LOGO_SVG.exists()


def has_vector_logo() -> bool:
    """True if a true SVG vector source is available."""
    return LOGO_SVG.exists()


def _png_bytes() -> bytes | None:
    if LOGO_PNG.exists():
        return LOGO_PNG.read_bytes()
    return None


def _svg_text() -> str | None:
    if LOGO_SVG.exists():
        return LOGO_SVG.read_text(encoding="utf-8")
    return None


def logo_data_uri(prefer_svg: bool = True) -> str:
    """Return a data: URI for the logo. Empty string if no file.
    For email: use this as src= on an <img> tag — survives Outlook's
    external-image blocking because the bytes travel with the email."""
    if prefer_svg and LOGO_SVG.exists():
        svg = _svg_text()
        if svg:
            b64 = base64.b64encode(svg.encode("utf-8")).decode("ascii")
            return f"data:image/svg+xml;base64,{b64}"
    png = _png_bytes()
    if png:
        b64 = base64.b64encode(png).decode("ascii")
        return f"data:image/png;base64,{b64}"
    return ""


def logo_html(height: int = 36, alt: str = "Hilmar Ingredients") -> str:
    """Drop-in HTML tag for the logo using a data: URI.

    NOTE: data: URIs are BLOCKED by Outlook for HTML email bodies (security
    feature — prevents drive-by image content). For email use, prefer
    `logo_html_cid()` which uses a CID reference and pairs with the
    `cid:LOGO_CID` attachment added by outlook_send.

    This data-URI form is fine for:
      - HTML files opened directly in a browser (dashboard, audit)
      - PDFs (reportlab uses its own path via logo_reportlab_image)
    """
    uri = logo_data_uri()
    if not uri:
        return ""
    return (
        f'<img src="{uri}" alt="{alt}" '
        f'style="height:{height}px;width:auto;vertical-align:middle;display:inline-block" />'
    )


#: CID (Content-ID) for the logo when embedded as an email attachment.
#: outlook_send attaches the logo PNG with this CID and the HTML body
#: references it as <img src="cid:LOGO_CID"> — Outlook renders inline
#: regardless of external-image blocking.
LOGO_CID = "hilmar-logo"


def logo_html_cid(height: int = 36, alt: str = "Hilmar Ingredients") -> str:
    """Drop-in HTML tag for the logo using a CID reference.

    Pairs with outlook_send.py attaching the logo PNG with
    `contentId=LOGO_CID, isInline=true`. This is the format that survives
    Outlook's safe-senders / external-image / data-URI blocking — the
    image renders inline always because the bytes are part of the message.

    Returns empty string if no logo file exists (graceful fallback to
    text header).
    """
    if not has_logo():
        return ""
    return (
        f'<img src="cid:{LOGO_CID}" alt="{alt}" '
        f'style="height:{height}px;width:auto;vertical-align:middle;display:inline-block" />'
    )


def logo_png_path() -> Path | None:
    """Return the absolute path to the PNG logo file, or None.

    outlook_send uses this to attach the logo with content-disposition=inline
    + content-id=LOGO_CID for the cid: reference in logo_html_cid()."""
    if LOGO_PNG.exists():
        return LOGO_PNG
    return None


def logo_reportlab_image(width: float = 140):
    """Return a reportlab Image flowable, or None if no PNG.
    `width` is in points (1 inch = 72pt). Height auto-scales by aspect."""
    if not LOGO_PNG.exists():
        return None
    try:
        from reportlab.lib.utils import ImageReader
        from reportlab.platypus import Image
        # Read original dimensions to preserve aspect ratio
        ir = ImageReader(str(LOGO_PNG))
        ow, oh = ir.getSize()
        aspect = oh / ow if ow else 1.0
        height = width * aspect
        return Image(str(LOGO_PNG), width=width, height=height)
    except Exception:
        return None
