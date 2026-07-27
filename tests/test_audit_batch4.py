"""Data audit batch 4 — the last four priority findings, each reproduced first.

  [1]  Booking→request matching was decided by STAGE-FILE ORDER. The header
       chain (In-Reply-To/References) picked the first row it encountered, so
       when Lonny REUSED a thread the outcome depended on row ordering:

         stage holds NEW first -> booking landed on req_new
         stage holds OLD first -> booking landed on req_old

       Same inputs, same day, opposite business outcome. The new unanswered
       RFQ got stamped WIN with a booking for equipment it never asked for and
       vanished from PENDING OL, while the genuinely quoted row sat open.

  [4]  A quoted row whose response_timestamp was missing or unparseable went
       straight to LOSS/OTHER with ZERO aging — "assumed aged". That is what
       patch_carriers produces when it recovers a rate from a sibling thread
       or a booking PDF. An RFQ sent THIS MORNING was reported to staff and to
       the client as a loss, counted against win rate, dropped from the chase
       lists, and absent from every pending bucket.

  [3]  The additive carry-forward APPENDED a prior WIN beside the row the
       fresh stage had already rebuilt under the same request_id — two rows
       and double the TEU for one shipment, the id both PENDING and WIN, and
       phase_4 arbitrating by counting non-empty fields.

  [9]  The "Won — <day>" KPI tile and the What-Happened block counted wins by
       DIFFERENT rules, so one email said "0 wins" in the strip and "1 wins"
       eight inches below, with a green PENDING → WIN pill under it.
"""
from __future__ import annotations

import importlib.util
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
for p in (str(ROOT / "src"), str(SCRIPTS)):
    if p not in sys.path:
        sys.path.insert(0, p)

import core  # noqa: E402

UTC = timezone.utc


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


qc = _load(SCRIPTS / "qc_selfheal.py", "qc_selfheal_batch4")
ingest = _load(SCRIPTS / "ingest.py", "ingest_batch4")
gen_email = _load(SCRIPTS / "gen_email.py", "gen_email_batch4")
hilmar_core = _load(ROOT / "src" / "hilmar" / "core.py", "hilmar_core_batch4")


# ── [1] the match is decided by evidence, not file order ────────────────────

def _reused_thread():
    """RFQ_old (2x40'HC, quoted) and a NEW unanswered RFQ (1x20'DV) in ONE
    thread. OL books the OLD move; References carry both imids."""
    old = {"request_id": "req_old", "status": "PENDING", "quoted": True,
           "destination": "HCMC", "origin": "Oakland", "lane": "Oakland → HCMC",
           "containers": "2x40'HC", "container_count": 2, "teu_requested": 4,
           "request_timestamp": "2026-07-20T16:00:00Z", "request_date": "2026-07-20",
           "carrier_quoted": "ONE", "source_imids": ["<A>"]}
    new = {"request_id": "req_new", "status": "PENDING", "quoted": False,
           "destination": "HCMC", "origin": "Oakland", "lane": "Oakland → HCMC",
           "containers": "1x20'DV", "container_count": 1, "teu_requested": 1,
           "request_timestamp": "2026-07-22T22:00:00Z", "request_date": "2026-07-22",
           "source_imids": ["<C>"]}
    booking = {"260999": {
        "subject": "MDOLX260999 // HILMAR 2X40'HC Oakland to HCMC // ONE: EBKG1",
        "sent": "2026-07-23T01:00:00Z", "in_reply_to": "<B>",
        "references": ["<A>", "<B>", "<C>"]}}
    return old, new, booking


def _winner(rows, booking):
    updated, _ = ingest.link_bookings_to_requests(rows, dict(booking))
    return [r["request_id"] for r in updated if r.get("mdolx_ref")]


def test_booking_match_is_independent_of_stage_order():
    """THE defect: same inputs, different row order, different outcome."""
    old, new, booking = _reused_thread()
    a = _winner([dict(new), dict(old)], booking)
    b = _winner([dict(old), dict(new)], booking)
    assert a == b, f"stage order changed the outcome: {a} vs {b}"


def test_booking_lands_on_the_request_whose_equipment_it_names():
    """The booking subject says 2X40'HC. That is the ask it settles."""
    old, new, booking = _reused_thread()
    assert _winner([dict(new), dict(old)], booking) == ["req_old"]


def test_a_request_sent_after_the_booking_can_never_win_it():
    """The guard that stops a brand-new RFQ swallowing an older move's
    booking: an ask Lonny sent AFTER the booking cannot be what it fulfils."""
    old, new, booking = _reused_thread()
    new["request_timestamp"] = "2026-07-24T00:00:00Z"   # after the booking
    updated, _ = ingest.link_bookings_to_requests([dict(new)], dict(booking))
    assert [r for r in updated if r.get("mdolx_ref")] == []


def test_chain_match_still_wins_over_lane_heuristics():
    """The header chain must remain the strongest signal — it is now a filter
    rather than a decision, but a chain member still beats a lane-only
    candidate on a subject-drifted lane."""
    old, _new, booking = _reused_thread()
    off_lane = dict(old, request_id="req_offlane", destination="Cat Lai",
                    lane="Oakland → Cat Lai", source_imids=["<A>"])
    updated, standalones = ingest.link_bookings_to_requests([off_lane], dict(booking))
    assert standalones == []
    assert updated[0]["mdolx_ref"] == "260999"


def test_ambiguous_thread_is_flagged_not_hidden():
    """A reused thread means a human may need to confirm which ask this
    booking settles. Never resolve it silently."""
    old, new, booking = _reused_thread()
    updated, _ = ingest.link_bookings_to_requests([dict(old), dict(new)], booking)
    won = [r for r in updated if r.get("mdolx_ref")][0]
    assert "in_reply_to/references" in won["_booking_match_via"]
    assert "1 of 2" in won["_booking_match_via"]


# ── [4] never age on absence ────────────────────────────────────────────────

_THIS_MORNING = "2026-07-27T14:00:00Z"
_TWO_HOURS_LATER = datetime(2026, 7, 27, 16, 0, tzinfo=UTC)


def test_quoted_row_with_no_timestamp_stays_pending_inside_the_window():
    """THE defect: a live morning quote reported as a LOSS to staff AND the
    client, with zero aging."""
    for ts in (None, "garbage", ""):
        d = core.decide_status(
            quoted=True, has_send=False, mdolx_ref=None, response_timestamp=ts,
            etd_fit_days=None, request_timestamp=_THIS_MORNING,
            now=_TWO_HOURS_LATER)
        assert d.status == "PENDING", f"response_timestamp={ts!r} buried a live quote"
        assert d.loss_reason == "NO_RESPONSE_TS"


def test_it_still_becomes_a_loss_once_the_request_clock_expires():
    """Held, not exempt. Once Lonny's own clock runs out it IS a loss."""
    d = core.decide_status(
        quoted=True, has_send=False, mdolx_ref=None, response_timestamp=None,
        etd_fit_days=None, request_timestamp="2026-07-01T14:00:00Z",
        now=_TWO_HOURS_LATER)
    assert d.status == "LOSS"
    assert d.loss_reason == "NO_RESPONSE_TS"


def test_with_no_request_clock_either_it_ages_as_before():
    """Nothing to fall back on — preserve the legacy outcome rather than
    holding a row PENDING forever."""
    d = core.decide_status(
        quoted=True, has_send=False, mdolx_ref=None, response_timestamp=None,
        etd_fit_days=None, now=_TWO_HOURS_LATER)
    assert d.status == "LOSS"
    assert d.loss_reason == "NO_RESPONSE_TS"


def test_a_quoted_row_is_never_no_response():
    """The older invariant this branch must not break."""
    d = core.decide_status(
        quoted=True, has_send=False, mdolx_ref=None, response_timestamp=None,
        etd_fit_days=None, request_timestamp=_THIS_MORNING, now=_TWO_HOURS_LATER)
    assert d.loss_reason != "NO_RESPONSE"


def test_never_age_on_absence_parity_across_trees():
    kw = dict(quoted=True, has_send=False, mdolx_ref=None, response_timestamp=None,
              etd_fit_days=None, request_timestamp=_THIS_MORNING, now=_TWO_HOURS_LATER)
    assert hilmar_core.decide_status(**kw).loss_reason == "NO_RESPONSE_TS"


def test_no_response_ts_is_a_declared_loss_reason_in_both_trees():
    assert "NO_RESPONSE_TS" in core.LOSS_REASONS
    assert "NO_RESPONSE_TS" in hilmar_core.LOSS_REASONS


def test_no_response_ts_is_in_the_persisted_schema():
    """A status the schema rejects cannot be written to disk."""
    import json
    schema = json.loads((ROOT / "schema.json").read_text(encoding="utf-8"))
    found = [v for v in json.dumps(schema).split('"') if v == "NO_RESPONSE_TS"]
    assert found, "NO_RESPONSE_TS missing from schema.json loss_reason enum"


# ── [3] carry-forward reconciles by id ──────────────────────────────────────

def _rebuilt_and_prior():
    rebuilt = {"request_id": "req_3f12", "status": "PENDING", "quoted": False,
               "destination": "Yokohama", "lane": "Oakland → Yokohama",
               "containers": "2x40'RF", "container_count": 2, "teu_requested": 4,
               "request_timestamp": "2026-06-01T15:00:00Z"}
    prior = {"request_id": "req_3f12", "status": "WIN", "quoted": True,
             "has_send": True, "destination": "Yokohama", "mdolx_ref": "260500",
             "carrier_won": "CMA CGM", "teu_won": 4, "teu_requested": 4,
             "request_timestamp": "2026-06-01T15:00:00Z"}
    return rebuilt, prior


def test_prior_win_merges_into_the_rebuilt_row_not_beside_it():
    """THE defect: one shipment stored as two rows under one id, 8 TEU for a
    4-TEU move, the id both PENDING and WIN."""
    rebuilt, prior = _rebuilt_and_prior()
    rows = [rebuilt]
    ingest._merge_prior_win_into(rows[0], prior, "2026-07-27T00:00:00Z")
    assert len(rows) == 1
    assert rows[0]["status"] == "WIN"
    assert rows[0]["teu_won"] == 4


def test_the_merge_restores_the_win_evidence():
    """An mdolx-backed WIN beats an evidence-free rebuilt PENDING. A status
    contradiction is never resolved by counting non-empty fields."""
    rebuilt, prior = _rebuilt_and_prior()
    ingest._merge_prior_win_into(rebuilt, prior, "2026-07-27T00:00:00Z")
    assert rebuilt["mdolx_ref"] == "260500"
    assert rebuilt["carrier_won"] == "CMA CGM"
    assert rebuilt["quoted"] is True and rebuilt["has_send"] is True
    assert rebuilt["loss_reason"] is None
    assert rebuilt["preserved_from_prior"] is True


def test_the_merge_records_the_transition():
    """QC-072's invariant — the row and its audit trail must agree."""
    rebuilt, prior = _rebuilt_and_prior()
    ingest._merge_prior_win_into(rebuilt, prior, "2026-07-27T00:00:00Z")
    assert rebuilt["status_history"][-1]["to"] == "WIN"
    assert "260500" in rebuilt["status_history"][-1]["reason"]


def test_the_merge_does_not_clobber_fresher_values():
    """Evidence fills gaps; it does not freeze stale display fields the fresh
    ingest re-derives correctly."""
    rebuilt, prior = _rebuilt_and_prior()
    rebuilt["carrier_won"] = "MSC"          # fresher signal already present
    ingest._merge_prior_win_into(rebuilt, prior, "2026-07-27T00:00:00Z")
    assert rebuilt["carrier_won"] == "MSC"


def test_the_merge_unions_all_booking_refs():
    rebuilt, prior = _rebuilt_and_prior()
    rebuilt["mdolx_refs_all"] = ["260111"]
    prior["mdolx_refs_all"] = ["260500", "260222"]
    ingest._merge_prior_win_into(rebuilt, prior, "2026-07-27T00:00:00Z")
    assert rebuilt["mdolx_refs_all"] == ["260111", "260222", "260500"]


def test_the_merge_is_idempotent():
    rebuilt, prior = _rebuilt_and_prior()
    ingest._merge_prior_win_into(rebuilt, prior, "2026-07-27T00:00:00Z")
    n = len(rebuilt["status_history"])
    ingest._merge_prior_win_into(rebuilt, prior, "2026-07-27T00:00:00Z")
    assert len(rebuilt["status_history"]) == n


# ── [9] one definition of "a win today" ─────────────────────────────────────

def _reversed_win_row():
    """Promoted to WIN on Jul 22 by a send-signal, later aged away."""
    return {"request_id": "req_x", "status": "LOSS", "loss_reason": "SEND_NO_BOOKING",
            "quoted": True, "has_send": True, "lane": "Oakland → HCMC",
            "destination": "HCMC", "containers": "1x40HC", "container_count": 1,
            "teu_requested": 2, "request_date": "2026-07-22",
            "request_timestamp": "2026-07-22T15:00:00Z",
            "status_history": [{"at": "2026-07-22T18:00:00Z", "from": "PENDING",
                                "to": "WIN", "reason": "Lonny replied Send"}]}


def _render(rows, rd=date(2026, 7, 22)):
    new_req, ol_resp, status_ch, pending = gen_email._today_events({"requests": rows}, rd)
    summary = gen_email._today_summary(rows, report_date=rd)
    block = gen_email._today_block_html("Wed Jul 22", new_req, ol_resp, status_ch, pending)
    m = re.search(r"(\d+) wins · (\d+) status changes", block)
    return summary, block, int(m.group(1))


def test_kpi_tile_and_what_happened_agree_on_a_reversed_win():
    """THE defect: "Won — Wed Jul 22: 0" in the KPI strip and "· 1 wins ·"
    eight inches below, in the same email."""
    summary, _block, block_wins = _render([_reversed_win_row()])
    assert summary["wins"] == block_wins == 0


def test_a_reversed_win_is_not_rendered_as_a_win():
    """The transition is still shown — it happened — but it must not read as
    a win, or the table contradicts the count above it."""
    _summary, block, _ = _render([_reversed_win_row()])
    assert "REVERSED" in block
    assert "SEND_NO_BOOKING" in block


def test_a_real_win_still_counts_on_both_surfaces():
    """Guard the fix: a win that STUCK must be counted, not suppressed."""
    row = _reversed_win_row()
    row["status"] = "WIN"
    row["loss_reason"] = None
    row["teu_won"] = 2
    summary, block, block_wins = _render([row])
    assert summary["wins"] == block_wins == 1
    assert "REVERSED" not in block


def test_win_landed_is_the_single_shared_rule():
    r_win = {"status": "WIN"}
    r_gone = {"status": "LOSS"}
    assert gen_email._win_landed(r_win, {"to": "WIN"}) is True
    assert gen_email._win_landed(r_gone, {"to": "WIN"}) is False
    assert gen_email._win_landed(r_win, {"to": "PENDING"}) is False


# ── QC-074 ──────────────────────────────────────────────────────────────────

def test_qc074_catches_a_duplicate_request_id():
    rows = [{"request_id": "req_a", "status": "PENDING"},
            {"request_id": "req_a", "status": "WIN", "mdolx_ref": "260500"}]
    found = qc.qc074_win_evidence_consistency(rows)
    assert any(s == "error" and "share this request_id" in d for _, s, d in found)


def test_qc074_catches_a_booking_ref_on_a_loss():
    rows = [{"request_id": "req_b", "status": "LOSS", "loss_reason": "PRICE",
             "mdolx_ref": "260500"}]
    found = qc.qc074_win_evidence_consistency(rows)
    assert any(s == "error" and "260500" in d for _, s, d in found)


def test_qc074_exempts_the_mdolx_no_send_anomaly():
    """That state is DEFINED as holding a booking ref without a win, pending
    ops review. Flagging it would cry wolf every fire."""
    rows = [{"request_id": "req_c", "status": "PENDING",
             "loss_reason": "MDOLX_NO_SEND", "mdolx_ref": "260500"}]
    assert qc.qc074_win_evidence_consistency(rows) == []


def test_qc074_warns_on_a_win_with_no_evidence():
    rows = [{"request_id": "req_d", "status": "WIN"}]
    assert [s for _, s, _ in qc.qc074_win_evidence_consistency(rows)] == ["warn"]


def test_qc074_clean_on_healthy_rows():
    rows = [{"request_id": "req_e", "status": "WIN", "mdolx_ref": "260500",
             "has_send": True},
            {"request_id": "req_f", "status": "LOSS", "loss_reason": "PRICE",
             "quoted": True},
            {"request_id": "req_g", "status": "PENDING"}]
    assert qc.qc074_win_evidence_consistency(rows) == []
    assert qc.qc074_win_evidence_consistency([]) == []
    assert qc.qc074_win_evidence_consistency(None) == []
