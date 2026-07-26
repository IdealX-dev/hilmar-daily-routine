"""Data audit batch 2 — three confirmed defects, each proved before it was fixed.

  [6]  core.decide_status returned has_send=False on SEND_NO_BOOKING, erasing
       the only record that Lonny accepted. qc_selfheal writes the decision
       back onto the row, so the NEXT pass re-read has_send=False, fell through
       to the quote-aging branch and relabelled the loss UNDIFFERENTIATED —
       "we lost, cause unknown". Unrecoverable, and it deleted the
       OL-dropped-the-ball signal from the loss mix and carrier scorecards.
       A SECOND site did the same thing: qc_selfheal cleared has_send on every
       LOSS, which would have wiped the flag straight back out.

  [7/10] request_date had THREE producers on THREE clocks (ingest: UTC,
       merge_ingest: raw ts[:10] UTC slice, qc_selfheal heal: PT) while every
       reader buckets by the ET business day. A Friday 5:30 PM PT RFQ stored
       as 2026-07-25 (Saturday) appeared in NO day's New Requests, KPI tile or
       reconciliation on any day, ever — while still counting in the period
       totals, so day tiles and period tiles disagreed by exactly those rows.

  [12] status was assigned directly instead of through record_transition, so
       status_history — the field schema.json declares as THE transition
       record — ended at {"to": "WIN"} on a row reading status="LOSS", and
       teu_won stayed >0 on a shipment that was never booked.

Each defect has a test that FAILS on the pre-fix code, plus the QC check that
catches a regression on live data (QC-071, QC-072).
"""
from __future__ import annotations

import importlib.util
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


qc = _load(SCRIPTS / "qc_selfheal.py", "qc_selfheal_batch2")
ingest = _load(SCRIPTS / "ingest.py", "ingest_batch2")
gen_email = _load(SCRIPTS / "gen_email.py", "gen_email_batch2")
hilmar_core = _load(ROOT / "src" / "hilmar" / "core.py", "hilmar_core_batch2")
# Package-relative imports inside src/hilmar — import it normally, not by path.
from hilmar import ingest as hilmar_ingest  # noqa: E402

# ── [6] has_send is evidence, not state ─────────────────────────────────────

_SEND_ARGS = dict(
    quoted=True, has_send=True, mdolx_ref=None,
    response_timestamp="2026-07-13T15:00:00Z",
    request_timestamp="2026-07-13T14:00:00Z",
    etd_fit_days=None,
)
_STALE_NOW = datetime(2026, 7, 20, 17, 0, tzinfo=UTC)   # Monday, well past 48 biz-h


def test_send_no_booking_keeps_has_send():
    """SEND_NO_BOOKING *means* the send happened. has_send must say so."""
    d = core.decide_status(now=_STALE_NOW, **_SEND_ARGS)
    assert d.status == "LOSS"
    assert d.loss_reason == "SEND_NO_BOOKING"
    assert d.has_send is True


def test_send_no_booking_is_stable_across_passes():
    """THE regression this exists for: qc_selfheal writes decision.has_send
    back onto the row, so pass 2 re-reads it. With has_send=False the row
    relabelled itself UNDIFFERENTIATED and the OL-service-failure signal was
    gone for good."""
    d1 = core.decide_status(now=_STALE_NOW, **_SEND_ARGS)
    args2 = dict(_SEND_ARGS, has_send=d1.has_send, quoted=d1.quoted)
    d2 = core.decide_status(now=_STALE_NOW, **args2)
    assert d2.loss_reason == "SEND_NO_BOOKING", (
        "the send evidence was destroyed and the loss lost its cause")
    d3 = core.decide_status(now=_STALE_NOW,
                            **dict(_SEND_ARGS, has_send=d2.has_send, quoted=d2.quoted))
    assert d3.loss_reason == "SEND_NO_BOOKING"


def test_send_no_booking_promotes_to_win_when_a_late_mdolx_lands():
    """The consequence of keeping the evidence: Lonny accepted and OL did
    eventually book, so the row is a WIN. With has_send=False it would have
    landed in the MDOLX_NO_SEND anomaly bucket instead."""
    d = core.decide_status(now=_STALE_NOW, **dict(_SEND_ARGS, mdolx_ref="MDX-9"))
    assert d.status == "WIN"


def test_send_no_booking_has_send_parity_across_trees():
    d = hilmar_core.decide_status(now=_STALE_NOW, **_SEND_ARGS)
    assert d.loss_reason == "SEND_NO_BOOKING"
    assert d.has_send is True


def test_mdolx_without_send_reports_has_send_false():
    """The mirror boundary — and a SECOND live defect found while fixing the
    first. The branch is literally `has_mdolx and not has_send`, yet
    scripts/core.py returned has_send=True (src/hilmar already returned False:
    a production-only split, exactly what the parity tests exist to catch).
    Not cosmetic — see the next test."""
    d = core.decide_status(
        has_send=False, mdolx_ref="MDX-2", response_timestamp="2026-04-10T15:00:00Z",
        quoted=True, etd_fit_days=0)
    assert d.loss_reason == "MDOLX_NO_SEND"
    assert d.has_send is False


def test_mdolx_no_send_anomaly_does_not_self_promote_to_win():
    """THE consequence. qc_selfheal writes the decision back onto the row, so
    pass 2 re-read has_send=True alongside the MDOLX and took the WIN branch:
    an anomaly explicitly held for ops review silently became a WIN one fire
    later, with no send signal and nobody looking."""
    args = dict(mdolx_ref="MDX-2", response_timestamp="2026-04-10T15:00:00Z",
                quoted=True, etd_fit_days=0)
    d1 = core.decide_status(has_send=False, **args)
    d2 = core.decide_status(has_send=d1.has_send, **args)
    assert (d2.status, d2.loss_reason) == ("PENDING", "MDOLX_NO_SEND")
    assert hilmar_core.decide_status(has_send=d1.has_send, **args).loss_reason \
        == "MDOLX_NO_SEND"


def _heal_row(row):
    log = qc.Log()
    qc.phase_3_entries(log, {"requests": [row]})
    return row


def test_qc_heal_no_longer_wipes_has_send_on_send_no_booking():
    """The second site. Fixing decide_status alone was not enough: this heal
    cleared has_send on EVERY loss, wiping the flag straight back out."""
    row = {"request_id": "R1", "status": "LOSS", "loss_reason": "SEND_NO_BOOKING",
           "quoted": True, "has_send": True, "request_timestamp": "2026-07-13T14:00:00Z",
           "response_timestamp": "2026-07-13T15:00:00Z", "containers": "1x40HC"}
    _heal_row(row)
    assert row["has_send"] is True
    assert row["loss_reason"] == "SEND_NO_BOOKING"


def test_has_send_on_a_loss_means_send_no_booking_and_nothing_else():
    """The invariant the exemption is narrowed to. After healing, a LOSS row
    carries has_send=True IF AND ONLY IF its reason is SEND_NO_BOOKING —
    has_send on a price loss or an unanswered RFQ genuinely is contradictory
    (Lonny cannot accept a quote we lost, or one OL never sent), and that
    clearing behaviour is deliberate and stays."""
    rows = [
        # aged send, never booked → SEND_NO_BOOKING, evidence kept
        {"request_id": "S1", "status": "LOSS", "loss_reason": "SEND_NO_BOOKING",
         "quoted": True, "has_send": True, "containers": "1x40HC",
         "request_timestamp": "2026-05-01T14:00:00Z",
         "response_timestamp": "2026-05-01T15:00:00Z"},
        # OL never answered → NO_RESPONSE, a stray has_send is contradictory
        {"request_id": "S2", "status": "LOSS", "loss_reason": "NO_RESPONSE",
         "quoted": False, "has_send": True, "containers": "1x40HC",
         "request_timestamp": "2026-05-01T14:00:00Z"},
    ]
    log = qc.Log()
    qc.phase_3_entries(log, {"requests": rows})
    for r in rows:
        if (r.get("status") or "").upper() == "LOSS":
            assert bool(r.get("has_send")) == (r.get("loss_reason") == "SEND_NO_BOOKING"), (
                f"{r['request_id']}: has_send={r.get('has_send')} on "
                f"LOSS/{r.get('loss_reason')}")


# ── [7/10] one clock: ET ────────────────────────────────────────────────────

# Friday 2026-07-24, 5:30 PM PT = 8:30 PM ET = 2026-07-25T00:30Z.
_FRI_EVENING = "2026-07-25T00:30:00Z"


def test_et_date_of_uses_et_not_utc():
    assert core.et_date_of(_FRI_EVENING) == "2026-07-24"
    assert core.et_date_of(core.parse_iso(_FRI_EVENING)) == "2026-07-24"


def test_et_date_of_passes_date_only_strings_through():
    """A date-only string carries no timezone. Re-reading it as midnight UTC
    would shift it a day — in exactly the direction this helper prevents."""
    assert core.et_date_of("2026-07-24") == "2026-07-24"


def test_et_date_of_returns_none_rather_than_inventing_a_date():
    for junk in (None, "", "not a date", 42, []):
        assert core.et_date_of(junk) is None


def test_et_date_of_parity_across_trees():
    assert hilmar_core.et_date_of(_FRI_EVENING) == core.et_date_of(_FRI_EVENING)
    assert hilmar_core.et_date_of("2026-07-24") == "2026-07-24"


def test_qc_heal_migrates_a_row_stored_on_the_wrong_clock():
    """Filling-only was why the timezone fix could not stand alone: every row
    ALREADY stored with a UTC or PT date would have kept its wrong day
    forever. The heal recomputes every pass, so it is its own migration."""
    row = {"request_id": "R3", "status": "PENDING", "quoted": False,
           "request_timestamp": _FRI_EVENING, "request_date": "2026-07-25",
           "date": "2026-07-25", "containers": "1x40HC"}
    _heal_row(row)
    assert row["request_date"] == "2026-07-24"
    assert row["date"] == "2026-07-24", "the legacy mirror readers fall back to"


def test_friday_evening_request_lands_on_the_friday_report():
    """The business consequence. Stored as Saturday it was invisible to every
    report day — no fire ever reports a Saturday."""
    row = {"request_id": "R4", "status": "PENDING", "quoted": False,
           "request_timestamp": _FRI_EVENING, "request_date": "2026-07-25",
           "containers": "1x40HC"}
    _heal_row(row)
    counts = {}
    for rd in ("2026-07-23", "2026-07-24", "2026-07-27"):
        s = gen_email._today_summary([row], report_date=date.fromisoformat(rd))
        counts[rd] = s.get("total", s.get("requests"))
    assert counts["2026-07-24"] == 1, "must land on the Friday it was sent"
    assert counts["2026-07-23"] == 0 and counts["2026-07-27"] == 0


def test_heal_leaves_rows_with_no_timestamp_alone():
    """We correct dates from the authoritative timestamp. We never invent one."""
    row = {"request_id": "R5", "status": "PENDING", "quoted": False,
           "request_date": "2026-07-20", "containers": "1x40HC"}
    _heal_row(row)
    assert row["request_date"] == "2026-07-20"


def test_qc071_flags_a_row_the_heal_did_not_reach():
    bad = {"request_id": "R6", "request_timestamp": _FRI_EVENING,
           "request_date": "2026-07-25"}
    assert qc.qc071_request_date_clock([bad]) == [("R6", "2026-07-25", "2026-07-24")]


def test_qc071_clean_on_correct_rows():
    good = {"request_id": "R7", "request_timestamp": _FRI_EVENING,
            "request_date": "2026-07-24"}
    no_ts = {"request_id": "R8", "request_date": "2026-07-24"}
    assert qc.qc071_request_date_clock([good, no_ts]) == []
    assert qc.qc071_request_date_clock([]) == []
    assert qc.qc071_request_date_clock(None) == []


# ── [12] the row and its audit trail must agree ─────────────────────────────

def _aged_win_row():
    return {"request_id": "R9", "status": "WIN", "quoted": True, "has_send": True,
            "teu_requested": 2, "teu_won": 2,
            "request_timestamp": "2026-06-01T15:00:00Z", "request_date": "2026-06-01",
            "response_timestamp": "2026-06-01T16:00:00Z",
            "status_history": [{"at": "2026-06-02T00:00:00Z", "from": "PENDING",
                                "to": "WIN", "reason": "Lonny replied Send"}]}


def test_age_requests_records_the_transition():
    r = _aged_win_row()
    ingest.age_requests([r], now=_STALE_NOW)
    assert r["status"] == "LOSS"
    assert r["status_history"][-1]["to"] == "LOSS", (
        "history still claimed WIN — every history-based reader called it won")
    assert r["status_history"][-1]["from"] == "WIN"


def test_leaving_win_clears_the_booked_volume():
    r = _aged_win_row()
    ingest.age_requests([r], now=_STALE_NOW)
    assert r["status"] != "WIN"
    assert r["teu_won"] == 0, "counted booked volume for a shipment never booked"


def test_age_requests_does_not_append_history_when_status_is_unchanged():
    """record_transition is a no-op on an unchanged status — re-running the
    fire must not grow the log."""
    r = _aged_win_row()
    ingest.age_requests([r], now=_STALE_NOW)
    n = len(r["status_history"])
    ingest.age_requests([r], now=_STALE_NOW)
    assert len(r["status_history"]) == n


def test_merge_unions_status_history_instead_of_keeping_the_stale_one():
    """status is recomputed on merge but history was preserved, so the merged
    row asserted one outcome in `status` and a different one in the log."""
    old = [{"request_id": "R10", "status": "WIN",
            "status_history": [{"at": "2026-06-02T00:00:00Z", "from": "PENDING",
                                "to": "WIN", "reason": "send"}]}]
    fresh = [{"request_id": "R10", "status": "LOSS",
              "status_history": [{"at": "2026-06-10T00:00:00Z", "from": "WIN",
                                  "to": "LOSS", "reason": "no MDOLX"}]}]
    merged = hilmar_ingest.merge_idempotent(old, fresh)[0]
    assert merged["status"] == "LOSS"
    assert len(merged["status_history"]) == 2, "the earlier transition was dropped"
    assert merged["status_history"][-1]["to"] == "LOSS"


def test_merge_status_history_is_idempotent():
    """Re-ingesting the same window must not duplicate transitions."""
    h = [{"at": "2026-06-02T00:00:00Z", "from": "PENDING", "to": "WIN", "reason": "s"}]
    rows = [{"request_id": "R11", "status": "WIN", "status_history": list(h)}]
    merged = hilmar_ingest.merge_idempotent(rows, [dict(rows[0])])[0]
    assert len(merged["status_history"]) == 1


def test_merge_status_history_does_not_grow_on_re_derived_transitions():
    """The regression an existing ingest test caught: a fresh run re-derives
    the SAME transition with a new timestamp, so deduping on `at` let every
    daily fire append another copy of it — unbounded growth."""
    old = [{"request_id": "R21", "status": "WIN", "status_history": [
        {"at": "2026-06-02T00:00:00Z", "from": None, "to": "WIN", "reason": "send+mdolx"}]}]
    row = old
    for i in range(5):
        fresh = [{"request_id": "R21", "status": "WIN", "status_history": [
            {"at": f"2026-06-0{i + 3}T00:00:00Z", "from": None, "to": "WIN",
             "reason": "send+mdolx"}]}]
        row = hilmar_ingest.merge_idempotent(row, fresh)
    assert len(row[0]["status_history"]) == 1
    assert row[0]["status_history"][0]["at"] == "2026-06-02T00:00:00Z", "keep the earliest"


def test_merge_status_history_keeps_a_genuine_repeat_transition():
    """Adjacency, not global uniqueness. A row that really goes WIN → LOSS →
    WIN keeps both WIN entries, so [-1]["to"] stays the true current state —
    the invariant QC-072 checks."""
    hist = [
        {"at": "2026-06-01T00:00:00Z", "from": "PENDING", "to": "WIN", "reason": "send"},
        {"at": "2026-06-05T00:00:00Z", "from": "WIN", "to": "LOSS", "reason": "no MDOLX"},
        {"at": "2026-06-09T00:00:00Z", "from": "LOSS", "to": "WIN", "reason": "send"},
    ]
    out = hilmar_ingest._merge_status_history(hist, [])
    assert [e["to"] for e in out] == ["WIN", "LOSS", "WIN"]


def test_merge_status_history_orders_by_time():
    """[-1] must be the LATEST transition — every reader indexes it that way."""
    old = [{"request_id": "R12", "status": "LOSS",
            "status_history": [{"at": "2026-06-10T00:00:00Z", "from": "WIN",
                                "to": "LOSS", "reason": "b"}]}]
    fresh = [{"request_id": "R12", "status": "LOSS",
              "status_history": [{"at": "2026-06-02T00:00:00Z", "from": "PENDING",
                                  "to": "WIN", "reason": "a"}]}]
    merged = hilmar_ingest.merge_idempotent(old, fresh)[0]
    assert [e["to"] for e in merged["status_history"]] == ["WIN", "LOSS"]


def test_merge_status_history_keeps_undated_entries():
    """A transition we cannot date still happened. Sort it last, never drop it."""
    out = hilmar_ingest._merge_status_history(
        [{"to": "WIN"}], [{"at": "2026-06-02T00:00:00Z", "to": "LOSS"}])
    assert len(out) == 2
    assert out[-1]["to"] == "WIN"


def test_qc072_catches_a_history_contradiction():
    row = {"request_id": "R13", "status": "LOSS",
           "status_history": [{"at": "2026-06-02T00:00:00Z", "to": "WIN"}]}
    found = qc.qc072_history_contradicts_status([row])
    assert [k for _, k, _ in found] == ["history-contradiction"]


def test_qc072_catches_stale_teu_won():
    row = {"request_id": "R14", "status": "LOSS", "teu_won": 2}
    found = qc.qc072_history_contradicts_status([row])
    assert [k for _, k, _ in found] == ["stale-teu-won"]


def test_qc072_clean_on_consistent_rows():
    rows = [
        {"request_id": "R15", "status": "WIN", "teu_won": 2,
         "status_history": [{"at": "2026-06-02T00:00:00Z", "to": "WIN"}]},
        {"request_id": "R16", "status": "LOSS", "teu_won": 0,
         "status_history": [{"at": "2026-06-02T00:00:00Z", "to": "LOSS"}]},
        {"request_id": "R17", "status": "PENDING"},          # no history yet
    ]
    assert qc.qc072_history_contradicts_status(rows) == []
    assert qc.qc072_history_contradicts_status([]) == []
    assert qc.qc072_history_contradicts_status(None) == []


def test_qc072_survives_malformed_history():
    """The daily fire must not die on a bad row."""
    rows = [{"request_id": "R18", "status": "WIN", "status_history": "not a list"},
            {"request_id": "R19", "status": "WIN", "status_history": ["junk"]},
            {"request_id": "R20", "status": "LOSS", "teu_won": None}]
    qc.qc072_history_contradicts_status(rows)   # must not raise
