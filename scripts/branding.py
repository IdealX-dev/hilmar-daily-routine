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
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
BRAND_DIR = ROOT / "assets" / "branding"

LOGO_PNG = BRAND_DIR / "hilmar-logo.png"
LOGO_SVG = BRAND_DIR / "hilmar-logo.svg"

# Hilmar brand colors — for headers, accents, etc.
HILMAR_BLUE = "#1a3d9c"
HILMAR_GREEN = "#76b82a"
HILMAR_NAVY = "#0a2350"


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


def logo_png_path() -> Optional[Path]:
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
        from reportlab.platypus import Image
        from reportlab.lib.utils import ImageReader
        # Read original dimensions to preserve aspect ratio
        ir = ImageReader(str(LOGO_PNG))
        ow, oh = ir.getSize()
        aspect = oh / ow if ow else 1.0
        height = width * aspect
        return Image(str(LOGO_PNG), width=width, height=height)
    except Exception:
        return None
