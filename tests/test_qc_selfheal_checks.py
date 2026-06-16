"""Direct regression tests for individual checks in scripts/qc_selfheal.py.

Pays down the KNOWN_UNTESTED ratchet in test_qc_governance.py. Each test
drives the real phase_6_rules() with a crafted data dict and asserts the
target check FIRES when it should and stays silent when it shouldn't —
genuine behavior, not a string-mention.

Scope: the checks here all operate on the passed `data`/`requests` dict
(deterministically testable in-container). Checks that read real repo files
or hit Sentry/git stay in the ratchet until a fixture harness exists for
them. (Do not name those check IDs in this file's prose — the governance
scan counts any QC-NNN mention as coverage, so naming one would falsely
mark it tested.)
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import core  # noqa: E402
import qc_selfheal as q  # noqa: E402


def _report_iso() -> str:
    """Mirror the report-date math the date-dependent checks use: the ~6 PM ET
    evening fire reports on TODAY's now-complete business day (weekends roll
    back to Friday). Single source of truth: core.report_business_day."""
    return core.report_business_day(datetime.now(core.ET).date()).isoformat()


def _base_data(requests=None, summary=None) -> dict:
    s = {
        "wins": 0, "quoted_lost": 0, "not_quoted": 0, "pending_hilmar": 0,
        "win_rate": 0.0, "quote_rate": 0.0, "teu_requested": 0, "teu_won": 0,
        "teu_quoted_lost": 0, "teu_not_quoted": 0, "teu_pending": 0,
        "total_entries": 0,
    }
    if summary:
        s.update(summary)
    return {"version": "2", "requests": requests or [], "summary": s}


def _fired(data: dict) -> list[str]:
    """Run phase_6_rules; return the WARN+ERROR messages (log.ok lines are
    not failures, so a check that passes simply won't appear here)."""
    log = q.Log()
    q.phase_6_rules(log, data)
    return log.warnings + log.errors


def _has(msgs: list[str], tag: str) -> bool:
    return any(tag in m for m in msgs)


# ── QC-010 — preserved-from-prior WIN growth ──────────────────────────────
def test_qc010_fires_above_threshold():
    reqs = [{"request_id": f"r{i}", "status": "WIN", "preserved_from_prior": True}
            for i in range(11)]  # threshold is 10
    assert _has(_fired(_base_data(reqs)), "QC-010")


def test_qc010_silent_within_threshold():
    reqs = [{"request_id": f"r{i}", "status": "WIN", "preserved_from_prior": True}
            for i in range(3)]
    assert not _has(_fired(_base_data(reqs)), "QC-010")


# ── QC-030 — transit-time pair (ETD+ETA) coverage ─────────────────────────
def _active(rid, **kw):
    return {"request_id": rid, "status": "LOSS", "quoted": True,
            "response_timestamp": "2026-05-01T10:00:00Z", **kw}


def test_qc030_fires_when_coverage_low():
    # 2 eligible rows, neither has both ETD+ETA → 0% < 70% → ERROR
    reqs = [_active("r1"), _active("r2")]
    assert _has(_fired(_base_data(reqs)), "QC-030")


def test_qc030_silent_when_coverage_full():
    reqs = [_active("r1", etd_offered="2026-05-10", eta_offered="2026-05-20"),
            _active("r2", etd_offered="2026-05-11", eta_offered="2026-05-21")]
    assert not _has(_fired(_base_data(reqs)), "QC-030")


# ── QC-018 — day-row math reconciliation ──────────────────────────────────
def test_qc018_fires_on_unclassified_status():
    # A row on the report date with a status that isn't W/QL/NQ/P means
    # total (1) != sum of the four buckets (0).
    reqs = [{"request_id": "r1", "status": "QUOTED", "request_date": _report_iso()}]
    assert _has(_fired(_base_data(reqs)), "QC-018")


def test_qc018_silent_when_buckets_reconcile():
    reqs = [{"request_id": "r1", "status": "WIN", "request_date": _report_iso()},
            {"request_id": "r2", "status": "PENDING", "request_date": _report_iso()}]
    assert not _has(_fired(_base_data(reqs)), "QC-018")


# ── QC-019 — status-change rows must carry a carrier ──────────────────────
def test_qc019_fires_when_status_change_has_no_carrier():
    reqs = [{
        "request_id": "r1", "status": "LOSS", "quoted": True, "lane": "Oakland → Tokyo",
        "status_history": [{"at": f"{_report_iso()}T13:00:00Z", "from": "PENDING", "to": "LOSS"}],
        # no carrier_quoted / carrier_won
    }]
    assert _has(_fired(_base_data(reqs)), "QC-019")


def test_qc019_silent_when_carrier_present():
    reqs = [{
        "request_id": "r1", "status": "LOSS", "quoted": True, "lane": "Oakland → Tokyo",
        "carrier_quoted": "CMA CGM",
        "status_history": [{"at": f"{_report_iso()}T13:00:00Z", "from": "PENDING", "to": "LOSS"}],
    }]
    assert not _has(_fired(_base_data(reqs)), "QC-019")


# ── QC-020b — NQ aggregate must equal raw NO_RESPONSE count ────────────────
def test_qc020b_fires_when_aggregate_drifts():
    reqs = [{"request_id": "r1", "status": "LOSS", "loss_reason": "NO_RESPONSE"},
            {"request_id": "r2", "status": "LOSS", "loss_reason": "NO_RESPONSE"}]
    # summary claims 1 but raw NO_RESPONSE count is 2 → display window leaked
    data = _base_data(reqs, summary={"not_quoted": 1})
    assert _has(_fired(data), "QC-020b")


def test_qc020b_silent_when_aggregate_matches():
    reqs = [{"request_id": "r1", "status": "LOSS", "loss_reason": "NO_RESPONSE"},
            {"request_id": "r2", "status": "LOSS", "loss_reason": "NO_RESPONSE"}]
    data = _base_data(reqs, summary={"not_quoted": 2})
    assert not _has(_fired(data), "QC-020b")


# ── QC-034 — tracking-data shape validity ─────────────────────────────────
def test_qc034_fires_on_invalid_status_enum():
    reqs = [{"request_id": "r1", "status": "TOTALLY_BOGUS", "quoted": True}]
    assert _has(_fired(_base_data(reqs)), "QC-034")


def test_qc034_silent_on_valid_shape():
    reqs = [{"request_id": "r1", "status": "WIN", "quoted": True}]
    assert not _has(_fired(_base_data(reqs)), "QC-034")


# ── QC-056 — OL rate quoted but carrier missing (Manila $797, 2026-06-15) ──
def test_qc056_heals_carrier_from_row_text():
    # Rate present, carrier blank, but the vessel string names the carrier.
    # The self-heal must backfill it from the row's own text — no WARN left.
    reqs = [{"request_id": "r1", "status": "Q&L", "quoted": True,
             "lane": "Oakland → Manila", "ol_rate": 797.0,
             "vessel_voyage": "HMM RUBY 012W"}]
    data = _base_data(reqs)
    log = q.Log()
    q.phase_6_rules(log, data)
    assert data["requests"][0].get("carrier_quoted") == "HMM"
    assert not _has(log.warnings + log.errors, "QC-056")


def test_qc056_warns_when_no_carrier_anywhere():
    # Rate present, carrier blank, and nothing on the row names a carrier →
    # un-healable, so QC-056 must surface it (WARN) rather than ship a blank.
    reqs = [{"request_id": "r1", "status": "Q&L", "quoted": True,
             "lane": "Oakland → Manila", "ol_rate": 797.0}]
    assert _has(_fired(_base_data(reqs)), "QC-056")


def test_qc056_silent_when_carrier_present():
    reqs = [{"request_id": "r1", "status": "Q&L", "quoted": True,
             "lane": "Oakland → Manila", "ol_rate": 797.0,
             "carrier_quoted": "MSC"}]
    assert not _has(_fired(_base_data(reqs)), "QC-056")


def test_qc056_silent_when_no_rate():
    # No rate → no carrier expected yet; QC-056 must not fire.
    reqs = [{"request_id": "r1", "status": "PENDING", "quoted": False}]
    assert not _has(_fired(_base_data(reqs)), "QC-056")
