"""Tests for scripts/auto_chase_pending.py.

This script soft-nudges Lonny on PENDING quotes ≥24h old. Daily-fired
on the Cloud PC via a wrapper. The audit on 2026-05-31 found ZERO test
coverage — and the script gates send by ET hour, which means a wrong
schedule produces a silent daily no-op. These tests lock the gating
logic so a schedule change can be made safely.

Run order matters: tests use module-level imports so a fresh import
of the script happens once per session.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

spec = importlib.util.spec_from_file_location("ac_under_test", SCRIPTS / "auto_chase_pending.py")
AC = importlib.util.module_from_spec(spec)
sys.modules["ac_under_test"] = AC
spec.loader.exec_module(AC)

UTC = timezone.utc


# ── _find_overdue_pending: min_age + status gating ─────────────────────

def test_find_overdue_pending_picks_only_pending_status():
    now = datetime.now(UTC)
    data = {"requests": [
        {"request_id": "r1", "status": "WIN",     "response_timestamp": (now - timedelta(hours=48)).isoformat()},
        {"request_id": "r2", "status": "PENDING", "response_timestamp": (now - timedelta(hours=48)).isoformat()},
        {"request_id": "r3", "status": "LOSS",    "response_timestamp": (now - timedelta(hours=48)).isoformat()},
        {"request_id": "r4", "status": "Q&L",     "response_timestamp": (now - timedelta(hours=48)).isoformat()},
    ]}
    rows = AC._find_overdue_pending(data, min_age_hours=24)
    assert [r["request_id"] for r in rows] == ["r2"]


def test_find_overdue_pending_filters_under_min_age():
    now = datetime.now(UTC)
    data = {"requests": [
        {"request_id": "fresh", "status": "PENDING",
         "response_timestamp": (now - timedelta(hours=10)).isoformat()},
        {"request_id": "stale", "status": "PENDING",
         "response_timestamp": (now - timedelta(hours=30)).isoformat()},
    ]}
    rows = AC._find_overdue_pending(data, min_age_hours=24)
    assert [r["request_id"] for r in rows] == ["stale"]


def test_find_overdue_pending_skips_rows_with_no_response_timestamp():
    data = {"requests": [
        {"request_id": "no_ts", "status": "PENDING"},
        {"request_id": "bad_ts", "status": "PENDING", "response_timestamp": "not-iso"},
    ]}
    rows = AC._find_overdue_pending(data, min_age_hours=24)
    assert rows == []


def test_find_overdue_pending_sorted_oldest_first():
    now = datetime.now(UTC)
    data = {"requests": [
        {"request_id": "younger", "status": "PENDING",
         "response_timestamp": (now - timedelta(hours=30)).isoformat()},
        {"request_id": "older", "status": "PENDING",
         "response_timestamp": (now - timedelta(hours=72)).isoformat()},
        {"request_id": "oldest", "status": "PENDING",
         "response_timestamp": (now - timedelta(hours=120)).isoformat()},
    ]}
    rows = AC._find_overdue_pending(data, min_age_hours=24)
    assert [r["request_id"] for r in rows] == ["oldest", "older", "younger"]


# ── _already_chased_today: idempotency flag ─────────────────────────────

def test_already_chased_today_returns_false_when_flag_missing(tmp_path):
    flag = tmp_path / "chase-sent-2026-05-31.flag"  # doesn't exist
    assert AC._already_chased_today({"request_id": "abc"}, flag) is False


def test_already_chased_today_detects_already_sent(tmp_path):
    flag = tmp_path / "chase-sent-2026-05-31.flag"
    flag.write_text("2026-05-31T16:30:00 req=req_HEX_ABC lane=Oakland-HCMC sent_id=xyz\n")
    assert AC._already_chased_today({"request_id": "req_HEX_ABC"}, flag) is True
    assert AC._already_chased_today({"request_id": "req_HEX_XYZ"}, flag) is False


def test_record_chase_appends_to_flag(tmp_path):
    flag = tmp_path / "chase-sent.flag"
    AC._record_chase({"request_id": "r1", "lane": "L1"}, "sent_abc", flag)
    AC._record_chase({"request_id": "r2", "lane": "L2"}, "sent_def", flag)
    lines = flag.read_text().strip().splitlines()
    assert len(lines) == 2
    assert "req=r1" in lines[0]
    assert "req=r2" in lines[1]


# ── _build_chase_email: subject + body sanity ───────────────────────────

def test_build_chase_email_contains_lane_carrier_rate():
    r = {
        "request_id": "r1",
        "lane": "Oakland → HCMC",
        "carrier_quoted": "MSC",
        "ol_rate": 2450,
        "containers": "1x40HC",
        "etd_offered": "2026-06-15",
        "_age_hours": 30.0,
    }
    subject, body = AC._build_chase_email(r)
    assert "Oakland → HCMC" in subject
    assert "MSC" in body
    assert "$2,450" in body
    assert "1x40HC" in body


def test_build_chase_email_handles_missing_rate_gracefully():
    r = {
        "request_id": "r1",
        "lane": "Oakland → Yokohama",
        "carrier_quoted": None,
        "ol_rate": None,
        "containers": "?",
        "etd_offered": None,
        "_age_hours": 50.0,
    }
    subject, body = AC._build_chase_email(r)
    assert "(rate TBD)" in body
    assert "(carrier TBD)" in body


# ── Config defaults (the schedule-fix safety net) ───────────────────────

def test_config_in_repo_has_evening_time_gate():
    """Lock the 4 PM ET time gate in committed config — this is what
    forces auto_chase to run on a separate evening schedule. If someone
    bumps it to 9 (matching the daily fire) without thinking through
    Lonny's PT timezone, this test catches it."""
    cfg = json.loads((ROOT / "config.json").read_text())
    ac = cfg.get("auto_chase", {})
    assert ac.get("enabled") is True, "auto_chase must stay enabled"
    assert ac.get("earliest_send_hour_et", 0) >= 16, (
        "earliest_send_hour_et must be >= 16 (4 PM ET). Lonny is on PT; "
        "earlier nudges arrive before he's had his coffee. Bumping this "
        "requires a deliberate policy decision."
    )
    assert ac.get("max_per_day", 99) <= 5, (
        "max_per_day cap protects Lonny from a chase storm. Stay <= 5."
    )
