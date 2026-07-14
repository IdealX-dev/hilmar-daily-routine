"""Tests for the 2026-06-04 PENDING window change.

Per Michael's restated rule:
  - Normal biz week: 24 wall-hours from OL response → LOSS if no reply.
  - Friday/weekend quote: not LOSS until Tuesday 18:00 ET.

Locks the new behavior + verifies the audit's red flag uses the same
predicate as the state machine (so the audit can't disagree with the
data it's reporting on).
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import core  # noqa: E402
import gen_improvements_report as gir  # noqa: E402

UTC = timezone.utc


# ─────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────

def test_pending_window_hours_is_24():
    """The constant Michael restated 2026-06-04. Was 48 before that."""
    assert core.PENDING_WINDOW_HOURS == 24


def test_is_business_stale_default_hours_is_24():
    """The default ``hours`` arg to is_business_stale matches the policy."""
    import inspect
    sig = inspect.signature(core.is_business_stale)
    assert sig.parameters["hours"].default == 24


# ─────────────────────────────────────────────────────────────────────
# Normal weekday — 24h cutoff
# ─────────────────────────────────────────────────────────────────────

def test_wednesday_quote_inside_24h_not_stale():
    wed = datetime(2026, 4, 22, 13, 0, tzinfo=UTC)
    now = wed + timedelta(hours=23)
    assert core.is_business_stale(wed, now) is False


def test_wednesday_quote_past_24h_is_stale():
    """The whole point of the 24h rule. Was 48h before 2026-06-04."""
    wed = datetime(2026, 4, 22, 13, 0, tzinfo=UTC)
    now = wed + timedelta(hours=25)
    assert core.is_business_stale(wed, now) is True


def test_tuesday_quote_28h_later_is_stale():
    """If the prior 48h rule were still in force this would be False."""
    tue = datetime(2026, 4, 21, 12, 0, tzinfo=UTC)
    thu = datetime(2026, 4, 22, 16, 0, tzinfo=UTC)  # 28h later
    assert core.is_business_stale(tue, thu) is True


# ─────────────────────────────────────────────────────────────────────
# Friday / weekend carve-out — deadline is Tuesday 18:00 ET
# ─────────────────────────────────────────────────────────────────────

def test_friday_quote_not_stale_monday_morning():
    fri = datetime(2026, 4, 24, 20, 0, tzinfo=UTC)  # Fri 16:00 ET
    mon_am = datetime(2026, 4, 27, 18, 0, tzinfo=UTC)  # Mon 14:00 ET
    assert core.is_business_stale(fri, mon_am) is False


def test_friday_quote_not_stale_monday_evening():
    """Lonny gets the FULL Monday + Tuesday window."""
    fri = datetime(2026, 4, 24, 20, 0, tzinfo=UTC)
    mon_pm = datetime(2026, 4, 27, 23, 0, tzinfo=UTC)  # Mon 19:00 ET
    assert core.is_business_stale(fri, mon_pm) is False


def test_friday_quote_not_stale_tuesday_afternoon():
    """Still PENDING through Tuesday afternoon, before the 18:00 ET cutoff."""
    fri = datetime(2026, 4, 24, 20, 0, tzinfo=UTC)
    tue_pm = datetime(2026, 4, 28, 21, 0, tzinfo=UTC)  # Tue 17:00 ET
    assert core.is_business_stale(fri, tue_pm) is False


def test_friday_quote_stale_tuesday_evening():
    """The 'by Tuesday' deadline kicks in at Tuesday 18:00 ET."""
    fri = datetime(2026, 4, 24, 20, 0, tzinfo=UTC)
    tue_pm = datetime(2026, 4, 28, 23, 0, tzinfo=UTC)  # Tue 19:00 ET
    assert core.is_business_stale(fri, tue_pm) is True


def test_saturday_quote_not_stale_tuesday_afternoon():
    """Saturday quotes also benefit from the carve-out (deadline = Tue 18 ET)."""
    sat = datetime(2026, 4, 25, 14, 0, tzinfo=UTC)
    tue_afternoon = datetime(2026, 4, 28, 21, 0, tzinfo=UTC)  # Tue 17:00 ET
    assert core.is_business_stale(sat, tue_afternoon) is False


def test_saturday_quote_stale_tuesday_evening():
    sat = datetime(2026, 4, 25, 14, 0, tzinfo=UTC)
    tue_evening = datetime(2026, 4, 28, 23, 0, tzinfo=UTC)  # Tue 19:00 ET
    assert core.is_business_stale(sat, tue_evening) is True


def test_sunday_quote_not_stale_tuesday_afternoon():
    sun = datetime(2026, 4, 26, 14, 0, tzinfo=UTC)
    tue_afternoon = datetime(2026, 4, 28, 21, 0, tzinfo=UTC)
    assert core.is_business_stale(sun, tue_afternoon) is False


# ─────────────────────────────────────────────────────────────────────
# Audit's PENDING red flag uses is_business_stale, not wall-clock >24h
# ─────────────────────────────────────────────────────────────────────

def _row(rid, status, resp_offset_hours, now):
    return {
        "request_id": rid,
        "status": status,
        "lane": "Oakland → Test",
        "response_timestamp": (now - timedelta(hours=resp_offset_hours)).isoformat(),
    }


def test_audit_pending_red_flag_aligns_with_state_machine_normal_day(monkeypatch):
    """Non-Friday quote past 48 CLOCK hours (Michael 2026-07-14) → state
    machine says Q&L, audit red flag must agree. 50h > 48h."""
    # Pin "now" to a Thursday afternoon; the quote 50h earlier is a Tuesday.
    fake_now = datetime(2026, 4, 23, 16, 0, tzinfo=UTC)
    rows = [_row("r-stale", "PENDING", 50, fake_now)]
    data = {"requests": rows}
    monkeypatch.setattr(
        gir, "datetime",
        type("_D", (), {
            "now": staticmethod(lambda tz=None: fake_now if tz is UTC else fake_now),
            # passthrough other classmethods we don't use
        }),
    )
    flags = gir.collect_red_flags(data, qc={}, drift={})
    pending_flags = [f for f in flags if "Pending past" in f.get("title", "")]
    assert len(pending_flags) == 1
    assert "r-stale" in pending_flags[0]["title"]


def test_audit_pending_red_flag_respects_friday_carve_out(monkeypatch):
    """Friday quote gets 72 CLOCK hours (Michael 2026-07-14). Viewed Monday
    MORNING (~66h) it must stay quiet — still inside the 72h window — so the
    audit red flag agrees with the state machine (which left it PENDING)."""
    fri_quote = datetime(2026, 4, 24, 20, 0, tzinfo=UTC)   # Fri 16:00 ET
    mon_morning = datetime(2026, 4, 27, 14, 0, tzinfo=UTC)  # Mon 10:00 ET (~66h)
    rows = [{
        "request_id": "r-fri",
        "status": "PENDING",
        "lane": "Oakland → Test",
        "response_timestamp": fri_quote.isoformat(),
    }]
    monkeypatch.setattr(
        gir, "datetime",
        type("_D", (), {
            "now": staticmethod(lambda tz=None: mon_morning),
        }),
    )
    flags = gir.collect_red_flags({"requests": rows}, qc={}, drift={})
    pending_flags = [f for f in flags if "Pending past" in f.get("title", "")]
    assert not pending_flags, (
        "Friday quote viewed Monday morning (~66h) must NOT be red-flagged — "
        "still within the 72h Friday window per Michael 2026-07-14."
    )


def test_audit_pending_red_flag_fires_after_friday_deadline(monkeypatch):
    """Same Friday quote, viewed AFTER Tuesday 18 ET — IS red-flagged."""
    fri_quote = datetime(2026, 4, 24, 20, 0, tzinfo=UTC)
    tue_evening = datetime(2026, 4, 28, 23, 0, tzinfo=UTC)  # Tue 19:00 ET
    rows = [{
        "request_id": "r-fri-stale",
        "status": "PENDING",
        "lane": "Oakland → Test",
        "response_timestamp": fri_quote.isoformat(),
    }]
    monkeypatch.setattr(
        gir, "datetime",
        type("_D", (), {
            "now": staticmethod(lambda tz=None: tue_evening),
        }),
    )
    flags = gir.collect_red_flags({"requests": rows}, qc={}, drift={})
    pending_flags = [f for f in flags if "Pending past" in f.get("title", "")]
    assert len(pending_flags) == 1


# ─────────────────────────────────────────────────────────────────────
# QC-052 dedupe — dedicated red flag suppresses the generic QC-error row
# ─────────────────────────────────────────────────────────────────────

def test_qc052_not_double_surfaced_when_dedicated_flag_fires(tmp_path, monkeypatch):
    """Today's audit (2026-06-03) showed 'Daily test/coverage routine FAILED'
    AND a separate 'QC-052 ERROR' row carrying the same info. Both refer to
    the same underlying problem. After the dedupe, only the dedicated
    (richer) flag appears."""
    monkeypatch.setattr(gir, "REPORTS", tmp_path)
    # Seed the test-result.json that drives the dedicated 'Daily test/coverage
    # routine FAILED' red flag.
    import json
    (tmp_path / "test-result.json").write_text(json.dumps({
        "status": "FAIL",
        "tests_ok": False,
        "coverage_ok": True,
        "counts": {"failed": 0, "error": 5},
        "total_coverage": None,
        "gate": 85.0,
        "errors": [],
        "error_type_buckets": [{"error_type": "UnknownError", "count": 5}],
        "collection_error": True,
        "pytest_output_path": "reports/pytest-output.txt",
    }))
    qc = {
        "status": "HAS_ERRORS",
        "fixes": 0, "warnings": 0, "errors": 1,
        "error_details": ["QC-052: daily test/coverage routine FAILED — 0 failed / 5 error"],
        "warning_details": [],
        "counts": {"total": 186},
    }
    flags = gir.collect_red_flags({"requests": []}, qc, {})
    dedicated = [f for f in flags if "Daily test/coverage" in f["title"]]
    generic_qc052 = [f for f in flags if f["title"].startswith("QC-052")]
    assert len(dedicated) == 1, "Dedicated red flag must fire"
    assert len(generic_qc052) == 0, (
        "Generic QC-052 ERROR row must be suppressed when the dedicated "
        "test/coverage red flag is already present."
    )


def test_qc052_still_surfaces_when_no_dedicated_flag(tmp_path, monkeypatch):
    """If somehow QC-052 fires but test-result.json is missing or PASS,
    the generic QC-error row must NOT be silently dropped — that would
    be a regression worse than the dupe."""
    monkeypatch.setattr(gir, "REPORTS", tmp_path)
    # No test-result.json on disk
    qc = {
        "status": "HAS_ERRORS",
        "fixes": 0, "warnings": 0, "errors": 1,
        "error_details": ["QC-052: pytest-cov not importable"],
        "warning_details": [],
        "counts": {"total": 186},
    }
    flags = gir.collect_red_flags({"requests": []}, qc, {})
    qc052 = [f for f in flags if f["title"].startswith("QC-052")]
    assert len(qc052) == 1, "Generic QC-052 row must surface when no dedicated flag exists"
