"""Close the QC-056/QC-002 carrier gap the 2026-07-01 diagnostics exposed.

The carrier-extraction diagnostics showed the stuck rows fall into two
classes, and each gets its own precise fix:

1. SIBLING-RECOVERABLE — the stuck $3,076 Yokohama rows sit next to $3,076
   Yokohama rows attributed CMA CGM: same OL quote line landed in two emails
   and only one parsed its carrier cell. _carrier_from_lane_rate_sibling
   backfills from that sibling. It deliberately does NOT infer from vessel
   names: alliance slot-sharing means a Yang Ming quote can sail on a
   ONE-named vessel (the live $425 Busan row), so a vessel prefix can
   mislabel the QUOTING carrier — a sibling's parsed carrier cannot. Any
   disagreement between siblings → stay blank rather than guess wrong.

2. CONTRADICTORY PROSE — rows that now carry an OL rate but whose
   reason_detail still reads "OL-USA never responded with a quote" (written
   when the row looked NQ; ingest never overwrites a set reason_detail).
   phase_3 now rewrites the text (and a stale NO_RESPONSE loss_reason) to
   the aged-Q&L truth.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import qc_selfheal as q  # noqa: E402


def _row(rid="req_a", lane="Oakland → Yokohama", rate=3076.0, carrier=None,
         date="2026-06-24", **over):
    base = {"request_id": rid, "subject": "HILMAR Oakland to Yokohama RFQ",
            "status": "LOSS", "quoted": True, "lane": lane, "ol_rate": rate,
            "carrier_quoted": carrier, "request_date": date}
    base.update(over)
    return base


# ── the sibling helper ────────────────────────────────────────────────────
def test_sibling_with_same_lane_and_rate_backfills():
    stuck = _row(rid="req_stuck")
    sib = _row(rid="req_sib", carrier="CMA CGM", date="2026-06-20")
    assert q._carrier_from_lane_rate_sibling(stuck, [stuck, sib]) == "CMA CGM"


def test_disagreeing_siblings_stay_blank():
    """Two siblings, two carriers → ambiguous → None (never guess)."""
    stuck = _row(rid="req_stuck")
    s1 = _row(rid="s1", carrier="CMA CGM")
    s2 = _row(rid="s2", carrier="ONE")
    assert q._carrier_from_lane_rate_sibling(stuck, [stuck, s1, s2]) is None


def test_rate_mismatch_is_not_a_sibling():
    stuck = _row(rid="req_stuck", rate=3076.0)
    near = _row(rid="near", carrier="CMA CGM", rate=3100.0)
    assert q._carrier_from_lane_rate_sibling(stuck, [stuck, near]) is None


def test_different_lane_is_not_a_sibling():
    stuck = _row(rid="req_stuck")
    other = _row(rid="other", carrier="CMA CGM", lane="Oakland → Osaka")
    assert q._carrier_from_lane_rate_sibling(stuck, [stuck, other]) is None


def test_sibling_outside_window_is_ignored():
    stuck = _row(rid="req_stuck", date="2026-06-24")
    old = _row(rid="old", carrier="CMA CGM", date="2026-03-01")
    assert q._carrier_from_lane_rate_sibling(stuck, [stuck, old]) is None


def test_missing_dates_still_match_on_lane_and_rate():
    stuck = _row(rid="req_stuck", date=None)
    sib = _row(rid="sib", carrier="CMA CGM", date=None)
    assert q._carrier_from_lane_rate_sibling(stuck, [stuck, sib]) == "CMA CGM"


def test_helper_never_raises_on_garbage():
    assert q._carrier_from_lane_rate_sibling({}, [None, {}, {"lane": 3}]) is None


# ── QC-056 integration: the stuck row gets the sibling's carrier ──────────
def _base_data(requests):
    return {"version": "2", "requests": requests,
            "summary": {"wins": 0, "quoted_lost": 0, "not_quoted": 0,
                        "pending_hilmar": 0, "win_rate": 0.0, "quote_rate": 0.0,
                        "teu_requested": 0, "teu_won": 0, "teu_quoted_lost": 0,
                        "teu_not_quoted": 0, "teu_pending": 0, "total_entries": 0}}


def test_qc056_backfills_from_sibling(monkeypatch):
    monkeypatch.setenv("HILMAR_QC_NO_PIP", "1")
    stuck = _row(rid="req_stuck")           # rate, no carrier, empty scan fields
    sib = _row(rid="req_sib", carrier="CMA CGM", date="2026-06-20")
    log = q.Log()
    q.phase_6_rules(log, _base_data([stuck, sib]))
    assert stuck["carrier_quoted"] == "CMA CGM"
    assert any("QC-056" in m and "sibling" in m for m in log.fixes), log.fixes


def test_qc056_stuck_without_sibling_still_diagnosed(monkeypatch):
    """No sibling → still stuck, still WARNs, still emits a diagnostic —
    the backfill must not swallow the honest leftover."""
    monkeypatch.setenv("HILMAR_QC_NO_PIP", "1")
    stuck = _row(rid="req_stuck")
    log = q.Log()
    q.phase_6_rules(log, _base_data([stuck]))
    assert stuck.get("carrier_quoted") is None
    assert any("QC-056" in m for m in log.warnings), log.warnings
    assert any(d.get("check") == "QC-056" for d in log.carrier_diag)


# ── phase-3: contradictory 'never responded' prose on a rated row ─────────
def test_phase3_rewrites_never_responded_on_rated_row():
    r = _row(rid="req_stale", carrier="CMA CGM", quoted=False,
             loss_reason="NO_RESPONSE",
             reason_detail="OL-USA never responded with a quote")
    data = {"requests": [r]}
    log = q.Log()
    q.phase_3_entries(log, data)
    survivors = [x for x in data["requests"] if x.get("request_id") == "req_stale"]
    assert survivors, "row was unexpectedly dropped by phase_3 cleanup"
    s = survivors[0]
    assert "never responded" not in s["reason_detail"]
    assert "assumed aged" in s["reason_detail"]
    assert s["loss_reason"] == "OTHER"
    assert any("never responded" in m for m in log.fixes), log.fixes


def test_phase3_leaves_genuine_nq_prose_alone():
    """A row with NO rate keeps its truthful 'never responded' prose."""
    r = _row(rid="req_nq", rate=None, carrier=None, quoted=False,
             loss_reason="NO_RESPONSE",
             reason_detail="OL-USA never responded with a quote")
    data = {"requests": [r]}
    q.phase_3_entries(q.Log(), data)
    survivors = [x for x in data["requests"] if x.get("request_id") == "req_nq"]
    assert survivors
    assert survivors[0]["reason_detail"] == "OL-USA never responded with a quote"
    assert survivors[0]["loss_reason"] == "NO_RESPONSE"
