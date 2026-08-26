"""Tests for the loss-reason mix render in scripts/gen_email.py.

Added 2026-05-31 alongside the renderer. Locks the contract that the
daily email's "Why We Lost" section:
  - is OMITTED entirely when there are no losses (so newly-deployed
    environments don't ship an empty section to the 10-recipient list);
  - renders the 30d and 60d windows with the expected labels and counts;
  - uses Outlook-safe HTML only (no flexbox, no SVG, no linear-gradient
    — QC-045 lessons);
  - shows the actionable_mix tags so the "what to push next" call is
    visible even on phone-mobile clients.
"""
from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
SRC = ROOT / "src"
for p in (str(SRC), str(SCRIPTS)):
    if p not in sys.path:
        sys.path.insert(0, p)

spec = importlib.util.spec_from_file_location("ge_under_test", SCRIPTS / "gen_email.py")
GE = importlib.util.module_from_spec(spec)
sys.modules["ge_under_test"] = GE
spec.loader.exec_module(GE)

# Load scripts/core.py for the actionable_mix sanity-check assertion
# below (UNDIFFERENTIATED bucketing).
core_spec = importlib.util.spec_from_file_location("scripts_core_lrmix", SCRIPTS / "core.py")
core_mod = importlib.util.module_from_spec(core_spec)
sys.modules["scripts_core_lrmix"] = core_mod
core_spec.loader.exec_module(core_mod)

UTC = timezone.utc


def _ts(days_ago: int) -> str:
    return (datetime.now(UTC) - timedelta(days=days_ago)).isoformat()


def _data_with_losses() -> dict:
    """Synthetic data with a realistic loss mix across 30/60-day windows."""
    return {
        "requests": [
            # Inside 30d
            {"status": "LOSS", "loss_reason": "PRICE",       "response_timestamp": _ts(2)},
            {"status": "LOSS", "loss_reason": "PRICE",       "response_timestamp": _ts(5)},
            {"status": "LOSS", "loss_reason": "PRICE",       "response_timestamp": _ts(15)},
            {"status": "LOSS", "loss_reason": "ETD_MISS",    "response_timestamp": _ts(8)},
            {"status": "LOSS", "loss_reason": "NO_RESPONSE", "response_timestamp": _ts(12)},
            {"status": "LOSS", "loss_reason": "SEND_NO_BOOKING", "response_timestamp": _ts(20)},
            # Inside 60d but outside 30d
            {"status": "LOSS", "loss_reason": "ETD_MISS",    "response_timestamp": _ts(40)},
            {"status": "LOSS", "loss_reason": "PRICE",       "response_timestamp": _ts(45)},
            # Outside 60d — should be ignored by both windows
            {"status": "LOSS", "loss_reason": "PRICE",       "response_timestamp": _ts(90)},
            # Not losses
            {"status": "WIN", "carrier_won": "MSC"},
            {"status": "PENDING"},
        ]
    }


# ── empty state: omit section entirely ──────────────────────────────────

def test_no_requests_returns_empty_string():
    assert GE._loss_reason_mix_html({}) == ""
    assert GE._loss_reason_mix_html({"requests": []}) == ""


def test_no_losses_returns_empty_string():
    """All wins + pendings — no loss_reasons to chart. The section must
    NOT render at all (don't ship an empty 'Why We Lost' header)."""
    data = {"requests": [
        {"status": "WIN", "carrier_won": "MSC"},
        {"status": "PENDING"},
    ]}
    assert GE._loss_reason_mix_html(data) == ""


# ── happy path: section + content ───────────────────────────────────────

def test_section_header_and_30d_window_render():
    out = GE._loss_reason_mix_html(_data_with_losses())
    assert "Why We Lost" in out
    assert "Loss-Reason Mix" in out
    assert "Last 30 days" in out
    # 30d window: 6 losses
    assert "6 losses" in out


def test_60d_window_renders():
    out = GE._loss_reason_mix_html(_data_with_losses())
    assert "Last 60 days" in out
    # 60d window: 8 losses (90-day-old one excluded)
    assert "8 losses" in out


def test_loss_reason_labels_rendered():
    """Top reasons must appear in the body — not raw enum names but
    the friendlier display labels from _REASON_META."""
    out = GE._loss_reason_mix_html(_data_with_losses())
    assert "Price (rate-driven)" in out
    assert "ETD missed" in out
    # Apostrophe in "OL didn't respond" gets HTML-escaped by _esc — match
    # the escaped form (either &#x27; or &#39; or raw, defensively).
    assert ("OL didn&#x27;t respond" in out
            or "OL didn&#39;t respond" in out
            or "OL didn't respond" in out)
    # 30d top reason is PRICE with 3 losses (of 6 total) — count visible.
    assert "3 &middot; 50%" in out or "3 · 50%" in out


def test_actionable_mix_tags_visible():
    out = GE._loss_reason_mix_html(_data_with_losses())
    # Each actionable bucket label appears at least once.
    assert "Push carriers" in out
    assert "Push ops" in out
    assert "Push OL" in out


# ── Outlook safety (QC-045 lessons) ─────────────────────────────────────

def test_no_linear_gradient_in_output():
    """QC-045: Outlook strips linear-gradient backgrounds. Solid colors
    only. This test catches a future contributor adding a gradient."""
    out = GE._loss_reason_mix_html(_data_with_losses())
    assert "linear-gradient" not in out.lower()


def test_no_flexbox_or_svg():
    """Outlook ignores display:flex and most SVG. Stay on tables."""
    out = GE._loss_reason_mix_html(_data_with_losses())
    assert "display:flex" not in out.lower()
    assert "<svg" not in out.lower()


def test_no_data_uri():
    """QC-042 guard: no data: URIs in the email HTML."""
    out = GE._loss_reason_mix_html(_data_with_losses())
    assert "data:" not in out.lower()


def test_lonely_no_response_only_still_renders_section():
    """If ALL losses in window are NO_RESPONSE (an "OL didn't quote at
    all" week), the chart must still render — push-OL is exactly the
    insight Michael wants surfaced."""
    data = {"requests": [
        {"status": "LOSS", "loss_reason": "NO_RESPONSE", "response_timestamp": _ts(2)},
        {"status": "LOSS", "loss_reason": "NO_RESPONSE", "response_timestamp": _ts(5)},
    ]}
    out = GE._loss_reason_mix_html(data)
    assert "Why We Lost" in out
    # Apostrophe escaped by _esc — match either form.
    assert ("OL didn&#x27;t respond" in out
            or "OL didn&#39;t respond" in out
            or "OL didn't respond" in out)
    assert "Push OL" in out


def test_bar_value_label_sits_outside_the_colored_bar_and_never_wraps():
    """2026-06-25 'terrible formatting': at small percentages the bar was too
    thin to hold its 'N · M%' label, which wrapped onto two lines inside the
    fill. The value must now live in a separate white-space:nowrap cell to the
    RIGHT of the bar, and the colored bar must be a pure fill (no text)."""
    import re
    # A deliberately lopsided mix → a ~8% bar, the wrap case.
    data = {"requests":
            [{"status": "LOSS", "loss_reason": "PRICE", "response_timestamp": _ts(2)}]
            + [{"status": "LOSS", "loss_reason": "UNDIFFERENTIATED", "response_timestamp": _ts(3)}
               for _ in range(12)]}
    out = GE._loss_reason_mix_html(data)

    # Every "count · pct%" value cell is nowrap and carries NO background
    # (i.e. it is NOT the colored bar).
    value_cells = re.findall(r'<td style="([^"]*)"[^>]*>(\d+ &middot; \d+%)</td>', out)
    assert value_cells, "no value cells found"
    for style, _val in value_cells:
        assert "white-space:nowrap" in style, style
        assert "background:" not in style, style

    # Every colored bar cell is a pure fill — its content is just a spacer,
    # never the value text (which is what wrapped).
    bar_cells = re.findall(r'<td style="[^"]*background:[^"]*">(.*?)</td>', out)
    assert bar_cells, "no colored bar cells found"
    for content in bar_cells:
        assert content.strip() in ("&nbsp;", ""), content


def test_undifferentiated_renders_with_distinct_label():
    """UNDIFFERENTIATED — the new (2026-06-02) "lost but rate was
    competitive" bucket — must render with the explicit "needs
    investigation" label so Michael sees it as a research signal, not
    just another rate-driven row."""
    data = {"requests": [
        {"status": "LOSS", "loss_reason": "UNDIFFERENTIATED", "response_timestamp": _ts(2)},
        {"status": "LOSS", "loss_reason": "UNDIFFERENTIATED", "response_timestamp": _ts(5)},
    ]}
    out = GE._loss_reason_mix_html(data)
    assert "Undifferentiated" in out
    # Label must say "needs investigation" so the action implication is
    # explicit — UNDIFFERENTIATED is the operator's signal to dig into
    # the email thread, not just another rate-driven bar.
    assert "needs investigation" in out


def test_undifferentiated_does_not_inflate_push_carriers_tag():
    """The "Push carriers" actionable tag must NOT pick up UNDIFFERENTIATED
    counts. That was the bug this whole PR existed to fix.

    NB the preamble text mentions "Push carriers" inline as a banner
    description. We assert the action-TAG isn't rendered (which embeds
    a count like "Push carriers — rate-driven: 3 (100%)") rather than
    checking for the bare phrase."""
    data = {"requests": [
        {"status": "LOSS", "loss_reason": "UNDIFFERENTIATED", "response_timestamp": _ts(2)},
        {"status": "LOSS", "loss_reason": "UNDIFFERENTIATED", "response_timestamp": _ts(5)},
        {"status": "LOSS", "loss_reason": "UNDIFFERENTIATED", "response_timestamp": _ts(10)},
    ]}
    out = GE._loss_reason_mix_html(data)
    # The action-TAG label "Push carriers — rate-driven" only renders
    # when rate_driven count > 0. With ALL UNDIFFERENTIATED, rate_driven
    # is 0 so the tag is suppressed entirely.
    assert "Push carriers &mdash; rate-driven" not in out
    assert "Push carriers — rate-driven" not in out
    # The "Other" actionable bucket gets all 3 UNDIFFERENTIATED rows.
    am_30 = core_mod.aggregate_loss_reasons(data["requests"], window_days=30)
    assert am_30["actionable_mix"]["rate_driven"] == 0
    assert am_30["actionable_mix"]["other"] == 3


# ── WHERE THIS SECTION LIVES NOW ────────────────────────────────────────
#
# 2026-08-26: removed from the daily email in the design proof, on the
# stated grounds that it "already exists in the attached dashboard HTML and
# the 6-page PDF". It did not. gen_dashboard and gen_pdf had never
# referenced it, so for the hours between that change and its restoration
# the "why we lost" breakdown was rendered by NOTHING — and every test in
# this file kept passing, because they all call the renderer directly and
# none of them asked whether anybody calls it.
#
# These two close that gap: one pins the caller, one pins the render.

def test_the_dashboard_calls_this_renderer():
    import gen_dashboard
    src = Path(gen_dashboard.__file__).read_text(encoding="utf-8")
    assert "_loss_reason_mix_html" in src, (
        "nothing renders the loss-reason mix — it is in the daily email's "
        "moved-out list, so the dashboard has to be its home")


def test_the_dashboard_actually_renders_it_with_losses_present():
    import json

    import core as core_prod
    import gen_dashboard
    cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    ts = datetime.now(timezone.utc) - timedelta(days=5)
    iso, day = ts.isoformat(), ts.date().isoformat()

    def _row(**kw):
        return {"request_date": day, "date": day, "request_timestamp": iso,
                "response_timestamp": iso, "origin": "Oakland", **kw}

    rows = [
        _row(status="LOSS", quoted=True, loss_reason="PRICE",
             destination="Osaka", lane="Oakland → Osaka", teu_requested=2,
             carrier_quoted="ONE", ol_rate=3400.0),
        _row(status="LOSS", quoted=True, loss_reason="ETD_MISS",
             destination="Kobe", lane="Oakland → Kobe", teu_requested=2,
             carrier_quoted="CMA CGM", ol_rate=3100.0),
    ]
    data = {"version": cfg["version"], "requests": rows,
            "summary": core_prod.aggregate_summary(rows),
            "last_updated": core_prod.now_utc().isoformat()}
    html = gen_dashboard.render(cfg, data)
    assert 'id="sec-loss-reasons"' in html
    assert "Price (rate-driven)" in html
    assert "ETD missed" in html
    # The section must not unbalance the tab it was inserted into.
    assert html.count("<div") == html.count("</div>")
