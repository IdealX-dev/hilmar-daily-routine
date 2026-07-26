"""Data audit batch 3 — the standalone-WIN cluster (findings 2, 11, 22).

ONE root cause behind all three, reproduced on main before anything was
touched: the only lane key in the system was `.strip().lower()`, so "HCMC"
and "Cat Lai" were different lanes. When Lonny's RFQ said "Oakland to HCMC"
and OL's booking confirmation said "Oakland to Cat Lai", the booking could not
be linked, so `link_bookings_to_requests` fabricated a `stand_<mdolx>` WIN row
beside the real request:

    req_abc       lane=Oakland → HCMC     status=PENDING  teu_requested=2
    stand_260999  lane=Oakland → Cat Lai  status=WIN      teu_won=2

One shipment, two rows. TEU double counted, a phantom lane in Lane
Performance, and 24h later the orphaned PENDING copy ages into a LOSS
reporting that OL never quoted a move OL had actually booked.

Three layers, each tested here:
  PREVENT  core.canonical_port_key collapses aliases on BOTH sides, so the
           booking links to the real row and no standalone is created.
  DETECT   QC-069 missed this exact pair (verified: it returned []) because
           its alias set only split a parenthetical and it compared container
           spellings as raw strings. Both gaps closed.
  CONTAIN  Standalone rows can no longer carry a degenerate "Oakland →
           Oakland" lane or a fabricated response_timestamp; QC-073 errors if
           one ever does.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
for p in (str(ROOT / "src"), str(SCRIPTS)):
    if p not in sys.path:
        sys.path.insert(0, p)

import core  # noqa: E402


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


qc = _load(SCRIPTS / "qc_selfheal.py", "qc_selfheal_batch3")
ingest = _load(SCRIPTS / "ingest.py", "ingest_batch3")
hilmar_core = _load(ROOT / "src" / "hilmar" / "core.py", "hilmar_core_batch3")


# ── PREVENT: one canonical key per physical destination ─────────────────────

@pytest.mark.parametrize("name,expected", [
    ("HCMC", "hcmc"),
    ("Cat Lai", "hcmc"),
    ("Cai Mep", "hcmc"),
    ("Ho Chi Minh", "hcmc"),
    ("Ho Chi Minh City", "hcmc"),
    ("Saigon", "hcmc"),
    ("HCMC (Cat Lai)", "hcmc"),          # head resolves
    ("Vietnam (Cat Lai)", "hcmc"),       # parenthetical resolves
    ("  cat lai  ", "hcmc"),             # whitespace / case
    ("Manila (North)", "manila"),
    ("Manila South", "manila"),
    ("Port Busan", "busan"),
    ("Lat Krab", "lat krabang"),
    ("Ladkrabang", "lat krabang"),
    ("HongKong", "hong kong"),
])
def test_aliases_collapse_to_one_key(name, expected):
    assert core.canonical_port_key(name) == expected


@pytest.mark.parametrize("a,b", [
    ("Bangkok", "Laem Chabang"),   # distinct ports, distinct rates
    ("Tokyo", "Yokohama"),
    ("Oakland", "HCMC"),
    ("Hamburg", "Rotterdam"),
])
def test_distinct_ports_are_not_merged(a, b):
    """The map is deliberately conservative. Over-merging would cross-match
    real separate business — a worse failure than the one being fixed."""
    assert core.canonical_port_key(a) != core.canonical_port_key(b)


def test_unknown_names_pass_through_unchanged():
    """Strict refinement of the old `.strip().lower()`: it can only merge
    names the map lists, never split ones that used to match."""
    assert core.canonical_port_key("Novorossiysk") == "novorossiysk"
    assert core.canonical_port_key("  Hamburg ") == "hamburg"


def test_empty_and_none_are_unknown():
    for junk in (None, "", "   "):
        assert core.canonical_port_key(junk) == "unknown"


def test_canonical_port_key_parity_across_trees():
    for name in ("Cat Lai", "HCMC (Cat Lai)", "Oakland", "Manila (North)", None, ""):
        assert hilmar_core.canonical_port_key(name) == core.canonical_port_key(name)


def test_ingest_lane_key_uses_the_canonical_map():
    assert ingest.canonical_lane_key("Cat Lai") == ingest.canonical_lane_key("HCMC")


# ── PREVENT: the booking links instead of fabricating a second row ──────────

_RFQ = {
    "request_id": "req_abc", "status": "PENDING", "quoted": False,
    "origin": "Oakland", "destination": "HCMC", "lane": "Oakland → HCMC",
    "request_timestamp": "2026-07-22T16:00:00Z", "request_date": "2026-07-22",
    "containers": "1x40HC", "container_count": 1, "teu_requested": 2,
    "source_imids": ["<lonny-rfq-1>"],
}
_BOOKING = {"260999": {
    "subject": "MDOLX260999 // HILMAR 1X40'HC Oakland to Cat Lai // ONE: EBKG12345",
    "sent": "2026-07-22T21:00:00Z", "in_reply_to": "", "references": []}}


def test_alias_booking_links_to_the_real_request():
    """THE defect. Pre-fix this produced two rows for one shipment."""
    updated, standalones = ingest.link_bookings_to_requests([dict(_RFQ)], dict(_BOOKING))
    assert standalones == [], "a standalone WIN was fabricated for a linkable booking"
    assert len(updated) == 1
    row = updated[0]
    assert row["request_id"] == "req_abc", "linked to the real request, not a stand_ row"
    assert row["status"] == "WIN"
    assert row["mdolx_ref"] == "260999"
    assert row["lane"] == "Oakland → HCMC", "keeps Lonny's lane, not the terminal name"


def test_teu_is_counted_once():
    """The measurable consequence: pre-fix, teu_requested=2 AND teu_won=2 sat
    on two different rows for the same 2 TEU."""
    updated, standalones = ingest.link_bookings_to_requests([dict(_RFQ)], dict(_BOOKING))
    rows = list(updated) + list(standalones)
    assert len(rows) == 1
    assert sum(r.get("teu_won") or 0 for r in rows) == 2


def test_a_booking_with_no_matching_request_still_becomes_a_standalone():
    """The alias fix must not suppress genuine standalones — a booking whose
    RFQ predates the window is exactly what stand_ rows exist for."""
    _, standalones = ingest.link_bookings_to_requests([], dict(_BOOKING))
    assert len(standalones) == 1
    assert standalones[0]["request_id"] == "stand_260999"
    assert standalones[0]["status"] == "WIN"


def test_a_booking_on_a_different_lane_does_not_steal_a_request():
    """Guard against over-matching: Hamburg must not absorb an HCMC booking."""
    other = dict(_RFQ, request_id="req_hamburg", destination="Hamburg",
                 lane="Oakland → Hamburg")
    updated, standalones = ingest.link_bookings_to_requests([other], dict(_BOOKING))
    assert updated[0]["status"] == "PENDING", "unrelated RFQ was consumed"
    assert len(standalones) == 1


# ── CONTAIN: standalone rows carry no invented values ───────────────────────

_RETURN_LEG = {"260888": {"subject": "HILMAR 1X40'HC to Oakland",
                          "sent": "2026-03-10T18:00:00Z",
                          "in_reply_to": "", "references": []}}


def test_degenerate_lane_is_not_written():
    """Michael's reported defect #3: a re-forwarded confirmation naming only
    one port produced "Oakland → Oakland", which then appeared in Lane
    Performance as a real trade lane."""
    _, standalones = ingest.link_bookings_to_requests([], dict(_RETURN_LEG))
    row = standalones[0]
    assert row["destination"] == "Unknown"
    assert row["lane"] == "Lane unresolved"
    assert core.canonical_port_key(row["origin"]) != core.canonical_port_key(row["destination"])


def test_standalone_does_not_fabricate_a_rate_response():
    """The matched path documents this rule explicitly ("response_timestamp
    stays None to signal we never saw a rate response"); the standalone path
    contradicted it and wrote the booking time, making the row claim an OL
    quote that never happened and corrupting turnaround metrics."""
    _, standalones = ingest.link_bookings_to_requests([], dict(_RETURN_LEG))
    row = standalones[0]
    assert row["response_timestamp"] is None
    assert row["booking_timestamp"] == "2026-03-10T18:00:00Z", "chronology preserved"


def test_fixed_constructor_produces_no_qc073_errors():
    _, standalones = ingest.link_bookings_to_requests([], dict(_RETURN_LEG))
    errors = [f for f in qc.qc073_standalone_booking_hygiene(standalones)
              if f[1] == "error"]
    assert errors == []


# ── DETECT: QC-069 backstop + QC-073 ────────────────────────────────────────

def _legacy_pair():
    """The pair exactly as an OLDER build would have written it."""
    return [
        {"request_id": "req_abc", "status": "PENDING", "destination": "HCMC",
         "containers": "1x40HC", "request_timestamp": "2026-07-22T16:00:00Z",
         "teu_requested": 2},
        {"request_id": "stand_260999", "status": "WIN", "destination": "Cat Lai",
         "containers": "1X40'HC", "booking_timestamp": "2026-07-22T21:00:00Z",
         "mdolx_ref": "260999", "teu_won": 2, "origin": "Oakland"},
    ]


def test_qc069_now_catches_the_pair_it_was_written_for():
    """Verified 2026-07-26: qc069 returned [] on this exact pair. Its alias set
    only split a parenthetical ("HCMC (Cat Lai)"), so two rows saying plain
    "HCMC" and plain "Cat Lai" produced disjoint sets."""
    found = qc.qc069_duplicate_shipment_rows(_legacy_pair())
    kinds = [k for k, _, _ in found]
    assert "open_row_shadowed_by_win" in kinds
    ids = [i for k, _, i in found if k == "open_row_shadowed_by_win"][0]
    assert ids == ["req_abc", "stand_260999"]


def test_qc069_matches_container_specs_across_spellings():
    """The second gap: Lonny writes "1x40HC", OL writes "1X40'HC". Folding
    case leaves those different strings, so the equipment half of the check
    never fired. Compare parsed (count, TEU) instead."""
    rows = _legacy_pair()
    rows[1]["containers"] = "1 x 40' HC"
    assert qc.qc069_duplicate_shipment_rows(rows)


def test_qc069_does_not_pair_different_equipment():
    rows = _legacy_pair()
    rows[1]["containers"] = "3x20'DV"
    assert [k for k, _, _ in qc.qc069_duplicate_shipment_rows(rows)
            if k == "open_row_shadowed_by_win"] == []


def test_qc069_does_not_pair_different_lanes():
    rows = _legacy_pair()
    rows[1]["destination"] = "Hamburg"
    assert [k for k, _, _ in qc.qc069_duplicate_shipment_rows(rows)
            if k == "open_row_shadowed_by_win"] == []


def test_qc073_flags_a_degenerate_lane():
    found = qc.qc073_standalone_booking_hygiene(
        [{"request_id": "stand_1", "status": "WIN",
          "origin": "Oakland", "destination": "Oakland"}])
    assert any(s == "error" and "degenerate lane" in d for _, s, d in found)


def test_qc073_flags_a_fabricated_rate_response():
    found = qc.qc073_standalone_booking_hygiene(
        [{"request_id": "stand_2", "status": "WIN", "origin": "Oakland",
          "destination": "HCMC", "response_timestamp": "2026-07-22T21:00:00Z",
          "ol_rate": None, "carrier_won": "ONE"}])
    assert [(s, "response_timestamp" in d) for _, s, d in found] == [("error", True)]


def test_qc073_warns_on_an_unattributable_standalone_win():
    found = qc.qc073_standalone_booking_hygiene(
        [{"request_id": "stand_3", "status": "WIN", "origin": "Oakland",
          "destination": "HCMC"}])
    assert [s for _, s, _ in found] == ["warn"]


def test_qc073_leaves_healthy_rows_alone():
    rows = [
        {"request_id": "req_ok", "status": "WIN", "origin": "Oakland",
         "destination": "HCMC", "carrier_won": "ONE", "ol_rate": "2400",
         "response_timestamp": "2026-07-22T17:00:00Z"},
        {"request_id": "stand_ok", "status": "WIN", "origin": "Oakland",
         "destination": "Cat Lai", "carrier_won": "MSC"},
    ]
    assert qc.qc073_standalone_booking_hygiene(rows) == []
    assert qc.qc073_standalone_booking_hygiene([]) == []
    assert qc.qc073_standalone_booking_hygiene(None) == []


def test_qc073_does_not_flag_a_normal_row_missing_a_rate():
    """A matched request with a real OL response but no parsed rate is a
    RESPONSE_NO_RATE case, not a fabricated booking timestamp. Only stand_
    rows are judged on that shape."""
    found = qc.qc073_standalone_booking_hygiene(
        [{"request_id": "req_x", "status": "LOSS", "origin": "Oakland",
          "destination": "HCMC", "response_timestamp": "2026-07-22T17:00:00Z",
          "ol_rate": None}])
    assert found == []
