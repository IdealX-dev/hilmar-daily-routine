"""Design-system regression guards (added 2026-06-13 visual overhaul).

The email/dashboard/PDF had drifted into three different navies and the
brand green went unused; amber meant three different things. These pin the
unified outcome so a future edit can't silently regress it. They render the
real email from a synthetic dataset (no network) and assert on the output.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import branding as B  # noqa: E402
import core  # noqa: E402
import gen_email as ge  # noqa: E402


def _data():
    now_et = datetime.now(timezone.utc).astimezone(core.ET)
    rd = ge._report_date(now_et)

    def ts(days, h):
        d = datetime.combine(rd, datetime.min.time(), tzinfo=core.ET) + timedelta(days=days, hours=h)
        return d.astimezone(timezone.utc).isoformat()

    reqs = [
        {"request_id": "w", "status": "WIN", "lane": "Dalhart → Caucedo",
         "origin": "Dalhart, TX", "destination": "Caucedo, DO", "teu_requested": 1,
         "containers": "1-20'", "product": "Protein", "quoted": True,
         "carrier_quoted": "CMA CGM", "ol_rate": 2845.0, "request_date": rd.isoformat(),
         "request_timestamp": ts(0, 9), "response_timestamp": ts(0, 12)},
        {"request_id": "nq", "status": "LOSS", "lane": "Oakland → Sydney",
         "origin": "Oakland, CA", "destination": "Sydney, AU", "teu_requested": 2,
         "containers": "1-40' HC", "product": "Cheese", "quoted": False,
         "loss_reason": "NO_RESPONSE", "request_date": (rd - timedelta(days=3)).isoformat(),
         "request_timestamp": ts(-3, 9)},
        {"request_id": "po", "status": "PENDING", "lane": "Dalhart → Hamburg",
         "origin": "Dalhart, TX", "destination": "Hamburg, DE", "teu_requested": 2,
         "containers": "1-40' HC", "product": "Protein", "quoted": False,
         "request_date": rd.isoformat(), "request_timestamp": ts(0, 9)},
    ]
    return {"version": "2", "requests": reqs, "date_range": "2026-04-01 – now",
            "summary": core.aggregate_summary(reqs)}


def test_brand_navy_unified_no_legacy_one_off():
    body = ge.build_body(_data(), core.load_config(str(ROOT / "config.json")))
    # the legacy one-off table navy is gone; the brand navy is present
    assert "#1e3a5f" not in body
    assert B.HILMAR_NAVY in body


def test_verdict_strip_and_preheader_present():
    body = ge.build_body(_data(), core.load_config(str(ROOT / "config.json")))
    assert "mso-hide:all" in body                     # hidden preheader
    # compact verdict strip — colored numbers with tiny labels
    assert "pend OL" in body and "pend Hilmar" in body
    assert "win rate" in body


def test_nq_uses_neutral_slate_not_amber():
    body = ge.build_body(_data(), core.load_config(str(ROOT / "config.json")))
    # The NQ section header must be slate, not the old amber #d97706.
    # Find the NQ heading and check its inline color.
    idx = body.find("Not Quoted — Last")
    assert idx > 0
    head = body[body.rfind("<h2", 0, idx):idx]
    assert "#64748b" in head, "NQ heading should be neutral slate"
    assert "#d97706" not in head, "NQ heading must not be amber (reserved for Pending-OL)"


def test_theme_tokens_exist_and_distinct():
    T = B.THEME
    # the four-way semantic split that un-overloads amber
    assert T["pending_ol"] == "#d97706"   # amber = waiting on OL
    assert T["nq"] == "#64748b"           # slate = no contest
    assert T["pending"] != T["pending_ol"] != T["nq"]
    assert B.STATUS_COLORS["WIN"][0] == T["win"]
