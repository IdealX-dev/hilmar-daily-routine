"""QC-069 — one shipment must never be stored as two rows.

Operator-reported defect #2: a lane showing as WON and still PENDING in the
same report. Root shape confirmed by the 2026-07-26 data audit: when OL's
booking confirmation names the terminal ("Cat Lai") and Lonny's RFQ names the
city ("HCMC"), link_bookings_to_requests cannot match them — because
canonical_lane_key is only .lower() — so it emits a standalone stand_<mdolx>
WIN beside the untouched PENDING request. TEU is double counted and the
orphaned PENDING copy later ages into a LOSS claiming OL never quoted a move
OL actually booked.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import qc_selfheal as q  # noqa: E402


def _row(rid, status, dest, containers="1-40' HC", **kw):
    base = {"request_id": rid, "status": status, "destination": dest,
            "containers": containers, "request_date": "2026-07-22"}
    base.update(kw)
    return base


def test_alias_split_booking_is_caught():
    """THE defect: RFQ says HCMC, OL's confirmation says Cat Lai."""
    rows = [
        _row("req_abc", "PENDING", "HCMC (Cat Lai)", quoted=True),
        _row("stand_260999", "WIN", "Cat Lai", mdolx_ref="260999"),
    ]
    out = q.qc069_duplicate_shipment_rows(rows)
    kinds = {k for k, _, _ in out}
    assert "open_row_shadowed_by_win" in kinds
    ids = [i for k, _, i in out if k == "open_row_shadowed_by_win"][0]
    assert ids == ["req_abc", "stand_260999"]


def test_same_booking_ref_on_two_rows_is_caught():
    rows = [
        _row("a", "PENDING", "Osaka", "2-20'", mdolx_ref="260999"),
        _row("b", "WIN", "Osaka", "2-20'", mdolx_ref="260999"),
    ]
    out = q.qc069_duplicate_shipment_rows(rows)
    assert ("duplicate_mdolx", "260999", ["a", "b"]) in out


def test_mdolx_refs_all_is_included_in_the_ref_scan():
    rows = [
        _row("a", "WIN", "Osaka", "2-20'", mdolx_ref="260999"),
        _row("b", "WIN", "Kobe", "2-20'", mdolx_refs_all=["260999"]),
    ]
    out = q.qc069_duplicate_shipment_rows(rows)
    assert any(k == "duplicate_mdolx" for k, _, _ in out)


def test_clean_dataset_is_silent():
    rows = [
        _row("r1", "WIN", "Osaka", "2-20'", mdolx_ref="260111"),
        _row("r2", "PENDING", "Xingang", "1-40' HC", quoted=False),
    ]
    assert q.qc069_duplicate_shipment_rows(rows) == []


def test_win_predating_the_request_is_a_different_deal():
    """A won move from last week must not shadow a brand-new ask on the same
    lane — recurring lanes are normal business, not duplication."""
    rows = [
        _row("old_win", "WIN", "Osaka", "2-20'", mdolx_ref="260001",
             request_date="2026-07-01", booking_timestamp="2026-07-01T18:00:00Z"),
        _row("new_ask", "PENDING", "Osaka", "2-20'", quoted=False,
             request_date="2026-07-22", request_timestamp="2026-07-22T15:00:00Z"),
    ]
    assert q.qc069_duplicate_shipment_rows(rows) == []


def test_different_equipment_on_the_same_lane_is_not_a_duplicate():
    rows = [
        _row("w", "WIN", "HCMC (Cat Lai)", "2-20'", mdolx_ref="260002"),
        _row("p", "PENDING", "HCMC (Cat Lai)", "1-40' HC", quoted=True),
    ]
    assert q.qc069_duplicate_shipment_rows(rows) == []


def test_unknown_destination_never_matches():
    rows = [
        _row("w", "WIN", "Unknown", "1-40' HC", mdolx_ref="260003"),
        _row("p", "PENDING", "Unknown", "1-40' HC", quoted=True),
    ]
    assert q.qc069_duplicate_shipment_rows(rows) == []
