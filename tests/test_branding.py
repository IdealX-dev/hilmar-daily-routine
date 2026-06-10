"""
Unit tests for scripts/branding.py — Hilmar logo + brand color helpers.

Verifies:
  - has_logo() / has_vector_logo() detect files correctly
  - logo_data_uri() produces valid base64 data URIs
  - logo_html() produces valid <img> tag with embedded data URI
  - SVG preferred over PNG when both exist
  - All helpers no-op gracefully when files missing
  - Brand color constants are valid hex
"""
import re
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import branding as B  # noqa: E402


def test_brand_colors_are_valid_hex():
    """All exposed brand color constants must be valid hex codes."""
    for name in ("HILMAR_BLUE", "HILMAR_GREEN", "HILMAR_NAVY"):
        val = getattr(B, name)
        assert re.match(r"^#[0-9a-fA-F]{6}$", val), f"{name} = {val!r} not valid hex"


def test_has_logo_detects_actual_file():
    """If the real logo is in place, has_logo() returns True."""
    # This is the prod file — should exist
    if B.LOGO_PNG.exists() or B.LOGO_SVG.exists():
        assert B.has_logo() is True
    else:
        assert B.has_logo() is False


def test_data_uri_starts_with_data_scheme():
    """If logo is present, data URI starts with data: scheme."""
    if not B.has_logo():
        return  # skip if no logo present
    uri = B.logo_data_uri()
    assert uri.startswith("data:image/")
    assert ";base64," in uri


def test_data_uri_prefers_svg():
    """When both SVG and PNG exist, SVG wins."""
    if B.has_vector_logo() and B.LOGO_PNG.exists():
        uri = B.logo_data_uri(prefer_svg=True)
        assert "svg+xml" in uri
        # Force PNG
        uri_png = B.logo_data_uri(prefer_svg=False)
        assert "png" in uri_png


def test_logo_html_renders_img_tag():
    """logo_html returns a complete <img> tag with src + alt + height."""
    if not B.has_logo():
        return
    html = B.logo_html(height=40, alt="Test Alt")
    assert "<img" in html
    assert "src=\"data:image" in html
    assert "alt=\"Test Alt\"" in html
    assert "height:40px" in html


def test_logo_html_empty_when_no_file():
    """When no logo file exists, logo_html returns empty string (no broken img)."""
    with patch.object(B, "LOGO_PNG", Path("/nonexistent/path.png")), \
         patch.object(B, "LOGO_SVG", Path("/nonexistent/path.svg")):
        html = B.logo_html()
        assert html == ""


def test_data_uri_empty_when_no_file():
    """Missing files → empty string, never raises."""
    with patch.object(B, "LOGO_PNG", Path("/nonexistent/path.png")), \
         patch.object(B, "LOGO_SVG", Path("/nonexistent/path.svg")):
        assert B.logo_data_uri() == ""


def test_logo_reportlab_image_none_when_no_file():
    """Missing PNG → None, never raises."""
    with patch.object(B, "LOGO_PNG", Path("/nonexistent/path.png")):
        result = B.logo_reportlab_image(width=140)
        assert result is None


def test_logo_reportlab_image_preserves_aspect_ratio():
    """When PNG present, the Image flowable preserves source aspect ratio."""
    if not B.LOGO_PNG.exists():
        return
    img = B.logo_reportlab_image(width=200)
    if img is None:
        return  # reportlab not installed in this env
    # height should be proportional to source aspect
    # we don't know exact aspect but it should be > 0 and reasonable
    assert hasattr(img, "drawWidth")
    assert img.drawWidth == 200
    assert img.drawHeight > 0
    assert img.drawHeight < 400  # not absurdly tall


def test_brand_paths_under_assets_branding():
    """Logo paths must be under the canonical assets/branding folder
    so the schema is predictable across devices."""
    assert str(B.LOGO_PNG).endswith(str(Path("assets") / "branding" / "hilmar-logo.png"))
    assert str(B.LOGO_SVG).endswith(str(Path("assets") / "branding" / "hilmar-logo.svg"))


if __name__ == "__main__":
    import inspect
    mod = sys.modules[__name__]
    funcs = [(n, f) for n, f in inspect.getmembers(mod, inspect.isfunction)
             if n.startswith("test_")]
    passed = failed = 0
    for name, fn in funcs:
        try:
            fn()
            passed += 1
        except AssertionError as e:
            failed += 1
            print(f"  FAIL {name}: {e}")
        except Exception as e:
            failed += 1
            print(f"  ERROR {name}: {type(e).__name__}: {e}")
    print(f"\n{passed} passed, {failed} failed of {len(funcs)} branding tests")
    sys.exit(0 if failed == 0 else 1)
