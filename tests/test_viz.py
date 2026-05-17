"""
Unit tests for scripts/viz.py — shared visualization helpers.

Tests the contract each helper guarantees:
  - empty/invalid input → safe empty string (never raises)
  - valid input → well-formed HTML/SVG output
  - heatmap colors are in valid red→yellow→green progression
  - sparkline SVG has correct point count

Per Michael 2026-05-14: best-practices batch (autonomous).
Standing rule: every new code pattern ships with tests.
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import viz as V  # noqa: E402


# ─────────────────────────────────────────────────────────────────────
# sparkline_svg
# ─────────────────────────────────────────────────────────────────────

def test_sparkline_empty_returns_empty_string():
    assert V.sparkline_svg([]) == ""
    assert V.sparkline_svg(None) == ""


def test_sparkline_single_point_returns_empty():
    # Need at least 2 points to draw a line
    assert V.sparkline_svg([5]) == ""


def test_sparkline_two_points_renders_svg():
    out = V.sparkline_svg([1, 2])
    assert out.startswith("<svg")
    assert 'viewBox="0 0 80 20"' in out
    assert "<polyline" in out
    assert "<polygon" in out  # filled area


def test_sparkline_custom_dimensions():
    out = V.sparkline_svg([1, 2, 3], width=100, height=30)
    assert 'width="100"' in out
    assert 'height="30"' in out
    assert 'viewBox="0 0 100 30"' in out


def test_sparkline_handles_none_values():
    out = V.sparkline_svg([1, None, 3, None, 5])
    assert out.startswith("<svg")
    # Should drop the Nones and render 3 valid points
    polyline = re.search(r'<polyline points="([^"]+)"', out)
    assert polyline
    coords = polyline.group(1).split()
    assert len(coords) == 3


def test_sparkline_constant_values():
    # All same values shouldn't divide by zero
    out = V.sparkline_svg([5, 5, 5, 5])
    assert out.startswith("<svg")


# ─────────────────────────────────────────────────────────────────────
# trend_arrow
# ─────────────────────────────────────────────────────────────────────

def test_trend_arrow_no_change_neutral():
    out = V.trend_arrow(10, 10)
    assert "▬" in out
    assert "0" in out


def test_trend_arrow_up_good_when_direction_up():
    out = V.trend_arrow(15, 10, good_direction="up")
    assert "▲" in out
    assert "#16a34a" in out  # green = good


def test_trend_arrow_up_bad_when_direction_down():
    # Rate cost going up = bad
    out = V.trend_arrow(150, 100, good_direction="down")
    assert "▲" in out
    assert "#dc2626" in out  # red = bad


def test_trend_arrow_handles_non_numeric():
    assert V.trend_arrow(None, 5) == ""
    assert V.trend_arrow(5, None) == ""
    assert V.trend_arrow("ten", 5) == ""


# ─────────────────────────────────────────────────────────────────────
# heatmap_color
# ─────────────────────────────────────────────────────────────────────

def test_heatmap_none_is_transparent():
    assert V.heatmap_color(None) == "transparent"


def test_heatmap_low_is_red_high_is_green_good_high():
    # 0 → red-ish, 100 → green-ish
    low = V.heatmap_color(0, vmin=0, vmax=100, mode="good_high")
    high = V.heatmap_color(100, vmin=0, vmax=100, mode="good_high")
    # Parse rgb(r,g,b) and verify red > green at low, green > red at high
    def _rgb(s):
        m = re.match(r"rgb\((\d+),(\d+),(\d+)\)", s)
        return tuple(int(x) for x in m.groups())
    r_low, g_low, _ = _rgb(low)
    r_high, g_high, _ = _rgb(high)
    assert r_low > g_low  # red dominates at 0
    assert g_high > r_high or g_high == r_high  # green at top


def test_heatmap_good_low_inverts():
    # In good_low mode, 0 should be green (good), 100 should be red (bad)
    low = V.heatmap_color(0, vmin=0, vmax=100, mode="good_low")
    high = V.heatmap_color(100, vmin=0, vmax=100, mode="good_low")
    def _rgb(s):
        m = re.match(r"rgb\((\d+),(\d+),(\d+)\)", s)
        return tuple(int(x) for x in m.groups())
    r_low, g_low, _ = _rgb(low)
    r_high, g_high, _ = _rgb(high)
    assert g_low >= r_low  # 0 = good = green-ish
    assert r_high > g_high  # 100 = bad = red


def test_heatmap_clamps_out_of_range():
    # Values outside vmin..vmax should clamp, not crash
    V.heatmap_color(-50, vmin=0, vmax=100)  # below
    V.heatmap_color(500, vmin=0, vmax=100)  # above


# ─────────────────────────────────────────────────────────────────────
# bar_cell
# ─────────────────────────────────────────────────────────────────────

def test_bar_cell_empty_when_no_data():
    assert V.bar_cell(None, 100) == "—"
    assert V.bar_cell(50, None) == "—"
    assert V.bar_cell(50, 0) == "—"


def test_bar_cell_renders_proportional_width():
    out = V.bar_cell(50, 100, label="50")
    assert "50.0%" in out  # 50/100 = 50%
    assert "<div" in out


def test_bar_cell_caps_at_100_pct():
    # Value > max shouldn't render > 100% width
    out = V.bar_cell(150, 100)
    assert "100.0%" in out  # capped


def test_bar_cell_custom_label():
    out = V.bar_cell(50, 100, label="custom")
    assert "custom" in out


# ─────────────────────────────────────────────────────────────────────
# status_pill
# ─────────────────────────────────────────────────────────────────────

def test_status_pill_known_statuses():
    for status in ["WIN", "LOSS", "PENDING", "QUOTED", "OK", "WARN", "ERROR", "CLEAN"]:
        out = V.status_pill(status)
        assert "<span" in out
        assert status in out


def test_status_pill_unknown_falls_back_to_neutral():
    out = V.status_pill("MYSTERY")
    assert "<span" in out
    assert "MYSTERY" in out
    # Default neutral grey palette
    assert "#f1f5f9" in out


def test_status_pill_case_insensitive():
    out = V.status_pill("win")
    assert "WIN" in out  # normalized to upper


def test_status_pill_empty_input():
    # Empty/None should still produce some output (don't crash)
    out = V.status_pill("")
    assert "<span" in out


# ─────────────────────────────────────────────────────────────────────
# section_header
# ─────────────────────────────────────────────────────────────────────

def test_section_header_renders():
    out = V.section_header("Test Title", subtitle="Test Subtitle", icon="🔥")
    assert "Test Title" in out
    assert "Test Subtitle" in out
    assert "🔥" in out
    assert "linear-gradient" in out


def test_section_header_no_subtitle():
    out = V.section_header("Title Only", icon="")
    assert "Title Only" in out


if __name__ == "__main__":
    # Run all test_ functions in this module
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
    print(f"\n{passed} passed, {failed} failed of {len(funcs)} viz tests")
    sys.exit(0 if failed == 0 else 1)
