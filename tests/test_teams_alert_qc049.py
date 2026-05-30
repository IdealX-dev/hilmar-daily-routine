"""Tests for the QC-049 unconfirmed-WIN auto-notify added to teams_alert.

QC-049 surfaces unconfirmed WINs in the daily audit but for ~10 days there
was no mechanism to push these to whoever owns the booking-team handoff —
they just sat. Per Michael 2026-05-28 ("do all 7-9"), each unconfirmed
WIN >=7d old now generates ONE alert per week (de-duped) via the existing
teams_alert framework."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import teams_alert as TA  # noqa: E402


def _unconfirmed_win(*, request_id: str, lane: str, request_date: str) -> dict:
    return {
        "request_id": request_id,
        "status": "WIN",
        "lane": lane,
        "request_date": request_date,
        "mdolx_ref": None,
        "mdolx_refs_all": None,
        "has_send": True,
        "carrier_won": "CMA CGM",
    }


def test_unconfirmed_win_old_enough_generates_alert(tmp_path, monkeypatch):
    monkeypatch.setattr(TA, "REPORTS", tmp_path)  # isolate _was_alerted state
    old_date = (datetime.now(timezone.utc) - timedelta(days=14)).date().isoformat()
    data = {"requests": [_unconfirmed_win(
        request_id="req_aged", lane="Oakland → Yokohama", request_date=old_date,
    )]}
    cfg = {"events": ["qc049_unconfirmed_win"]}
    events = TA.detect_events(data, cfg)
    qc049 = [e for e in events if e["type"] == "qc049_unconfirmed_win"]
    assert len(qc049) == 1
    assert "Yokohama" in qc049[0]["title"]
    assert "14d" in qc049[0]["title"] or "14 d" in qc049[0]["title"]


def test_unconfirmed_win_recent_is_suppressed(tmp_path, monkeypatch):
    """A WIN that flipped 3 days ago is still within normal booking-
    confirmation lag — must NOT fire the alert."""
    monkeypatch.setattr(TA, "REPORTS", tmp_path)
    recent = (datetime.now(timezone.utc) - timedelta(days=3)).date().isoformat()
    data = {"requests": [_unconfirmed_win(
        request_id="req_fresh", lane="Oakland → Tokyo", request_date=recent,
    )]}
    cfg = {"events": ["qc049_unconfirmed_win"]}
    events = TA.detect_events(data, cfg)
    assert [e for e in events if e["type"] == "qc049_unconfirmed_win"] == []


def test_confirmed_win_with_mdolx_ref_does_not_alert(tmp_path, monkeypatch):
    """The MDOLX booking IS linked — nothing to review."""
    monkeypatch.setattr(TA, "REPORTS", tmp_path)
    row = _unconfirmed_win(
        request_id="req_ok", lane="Oakland → Osaka",
        request_date=(datetime.now(timezone.utc) - timedelta(days=20)).date().isoformat(),
    )
    row["mdolx_ref"] = "MDOLX260999"
    data = {"requests": [row]}
    cfg = {"events": ["qc049_unconfirmed_win"]}
    assert [e for e in TA.detect_events(data, cfg) if e["type"] == "qc049_unconfirmed_win"] == []


def test_event_disabled_in_config_does_not_fire(tmp_path, monkeypatch):
    """If the operator hasn't opted in via cfg.events, stay quiet."""
    monkeypatch.setattr(TA, "REPORTS", tmp_path)
    old = (datetime.now(timezone.utc) - timedelta(days=14)).date().isoformat()
    data = {"requests": [_unconfirmed_win(
        request_id="req_a", lane="Oakland → Yokohama", request_date=old,
    )]}
    cfg = {"events": []}  # event not enabled
    assert [e for e in TA.detect_events(data, cfg) if e["type"] == "qc049_unconfirmed_win"] == []


def test_alert_dedup_within_same_week(tmp_path, monkeypatch):
    """Run detect_events twice in a row in the same week — second run must
    not double-alert. The de-dup key is request_id + ISO week."""
    monkeypatch.setattr(TA, "REPORTS", tmp_path)
    old = (datetime.now(timezone.utc) - timedelta(days=14)).date().isoformat()
    data = {"requests": [_unconfirmed_win(
        request_id="req_a", lane="Oakland → Yokohama", request_date=old,
    )]}
    cfg = {"events": ["qc049_unconfirmed_win"]}
    first = TA.detect_events(data, cfg)
    assert len(first) == 1
    # Record the alert as sent.
    TA._record_alert(first[0]["key"])
    # Re-run — must not fire again.
    second = TA.detect_events(data, cfg)
    assert [e for e in second if e["type"] == "qc049_unconfirmed_win"] == []


def test_multiple_unconfirmed_wins_each_alert_once(tmp_path, monkeypatch):
    """Each unconfirmed WIN gets its own alert (one per request_id)."""
    monkeypatch.setattr(TA, "REPORTS", tmp_path)
    old = (datetime.now(timezone.utc) - timedelta(days=14)).date().isoformat()
    data = {"requests": [
        _unconfirmed_win(request_id=f"req_{i}", lane=f"Oakland → Lane{i}", request_date=old)
        for i in range(3)
    ]}
    cfg = {"events": ["qc049_unconfirmed_win"]}
    events = TA.detect_events(data, cfg)
    qc049 = [e for e in events if e["type"] == "qc049_unconfirmed_win"]
    assert len(qc049) == 3
    keys = {e["key"] for e in qc049}
    assert len(keys) == 3  # all distinct
