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
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BRAND_DIR = ROOT / "assets" / "branding"

LOGO_PNG = BRAND_DIR / "hilmar-logo.png"
LOGO_SVG = BRAND_DIR / "hilmar-logo.svg"

# Hilmar brand colors — for headers, accents, etc.
HILMAR_BLUE = "#1a3d9c"
HILMAR_GREEN = "#76b82a"
HILMAR_NAVY = "#0a2350"

# ─────────────────────────────────────────────────────────────────────
# THEME — the single design-token source of truth (added 2026-06-13 per
# Michael "the entire visual design ... improvements for beauty style and
# function"). Before this, three surfaces had drifted into three different
# navies (#1e3a5f email, #0f172a PDF, brand #0a2350 declared-but-unused)
# and the brand GREEN was never used at all. Every generator (gen_email,
# gen_dashboard, gen_pdf, gen_carrier_scorecard_pdf, gen_improvements_report)
# now reads from here, so the email, dashboard, and PDF look like siblings
# and a palette change happens in ONE place.
#
# Two layers, deliberately separated:
#   CHROME  = brand identity (headers, structural accents) — Hilmar navy/blue/green
#   STATUS  = semantic state (win/loss/etc.) — kept on the well-known
#             Tailwind hues operators already read fluently
#
# STATUS note (the amber un-overload): amber used to mean THREE things —
# NQ, slow-turnaround, AND pending. With the 2026-06-12 pending split,
# amber now means exactly "waiting on OL"; NQ moves to a neutral slate
# ("no contest happened" reads as neutral, not warning), and slow
# turnaround keeps red. One color, one meaning.
THEME = {
    # Chrome
    "brand_navy":   HILMAR_NAVY,    # #0a2350 — primary header fill
    "brand_blue":   HILMAR_BLUE,    # #1a3d9c — header gradient terminus, links
    "brand_green":  HILMAR_GREEN,   # #76b82a — brand accent (rules, the "live" dot)
    "header_grad_from": HILMAR_NAVY,
    "header_grad_to":   HILMAR_BLUE,
    # Status (semantic)
    "win":          "#059669",
    "win_border":   "#10b981",
    "loss":         "#dc2626",
    "loss_border":  "#ef4444",
    "pending":      "#7c3aed",      # Pending Hilmar (chase Lonny)
    "pending_border": "#a855f7",
    "pending_ol":   "#d97706",      # Pending OL quote (chase OL) — amber, now unambiguous
    "pending_ol_border": "#f59e0b",
    "nq":           "#64748b",      # Not Quoted — neutral slate (no contest happened)
    "nq_border":    "#94a3b8",
    # Turnaround speed
    "ta_fast":      "#059669",
    "ta_medium":    "#2563eb",
    "ta_slow":      "#dc2626",
    # Neutrals (Tailwind slate scale — the connective tissue)
    "ink":          "#0f172a",
    "ink_soft":     "#1e293b",
    "muted":        "#475569",
    "muted_soft":   "#64748b",
    "faint":        "#94a3b8",
    "rule":         "#e2e8f0",
    "rule_soft":    "#f1f5f9",
    "surface":      "#ffffff",
    "canvas":       "#f5f7fa",
}

#: Status enum → (text color, left-border accent). One lookup so email,
#: dashboard, and PDF tag a WIN/LOSS/NQ/PENDING row identically.
STATUS_COLORS = {
    "WIN":          (THEME["win"], THEME["win_border"]),
    "LOSS":         (THEME["loss"], THEME["loss_border"]),
    "Q&L":          (THEME["loss"], THEME["loss_border"]),
    "NQ":           (THEME["nq"], THEME["nq_border"]),
    "PENDING":      (THEME["pending"], THEME["pending_border"]),
    "PENDING_OL":   (THEME["pending_ol"], THEME["pending_ol_border"]),
    "PENDING_HILMAR": (THEME["pending"], THEME["pending_border"]),
}


def header_gradient_css() -> str:
    """The standard header fill: a solid brand-navy declaration FIRST (Outlook
    strips linear-gradient — QC-045 enforces the fallback), then the gradient
    for clients that honor it. Always emit both, in this order."""
    return (f"background-color:{THEME['header_grad_from']};"
            f"background:linear-gradient(135deg,{THEME['header_grad_from']} 0%,"
            f"{THEME['header_grad_to']} 100%)")


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
