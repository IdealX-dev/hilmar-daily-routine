"""One port is one lane, however it was spelled.

`aggregate_lanes` and `compute_lane_winning_medians` keyed on the raw
"Oakland → X" DISPLAY string. The note above `core.PORT_LOCODES` named that as
the reason the Yokohama split starved its own winning median in 2026-08 — and
#230 fixed the JPYOK spelling at PARSE time and left the keying alone. So the
cause outlived the symptom.

THIS IS NOT HYPOTHETICAL. QC-083's first real fire (2026-08-31) named two
live pairs, and the second one is a lane-spelling split in production data:

    req_34213cc401395756  superseded re-ask of  req_e54685b379d8c950
        Oakland → HCMC (Cat Lai)          vs        Oakland → HCMC

THE MERGE IS AN OPERATOR RULING, NOT AN INFERENCE. `canonical_port_key`'s own
docstring calls itself "a MATCHING key, not a display value", built for
booking→request linking — so reusing it to bucket a REPORTING aggregate needed
a decision. Michael, 2026-08-31: *"no they are all hcmc with two different
terminal requests in ho chi minh"*. Cat Lai and Cai Mep are two terminals of
ONE lane, priced as one. `_PORT_ALIASES` already said so for matching
("Lonny asks for 'HCMC'; OL confirms whichever terminal the vessel calls");
this confirms it for pricing.

WHY IT MATTERS BEYOND TIDINESS: `PRICE_GAP_MIN_LANE_WINS` is 3. A lane with
four wins split 2/2 across two spellings produces NO median at all, and every
Q&L loss on it falls from PRICE to UNDIFFERENTIATED — "we lost, the data
doesn't tell us why" — on the lane group Hilmar ships most.

HALF THIS FILE ASSERTS THINGS MUST STAY SEPARATE. A bucketer that merged
everything would satisfy every merge test ever written.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for p in (str(ROOT / "src"), str(ROOT / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)

import core  # noqa: E402

from hilmar import core as HC  # noqa: E402

TREES = [pytest.param(core, id="scripts"), pytest.param(HC, id="hilmar")]


def _wins(dest, n=2, rate=3000.0, origin="Oakland"):
    return [{"status": "WIN", "origin": origin, "destination": dest,
             "lane": f"{origin} → {dest}", "teu_requested": 2, "teu_won": 2,
             "mdolx_ref": f"{dest}-{i}", "ol_rate": rate} for i in range(n)]


# ── the live pair, from QC-083's first real fire ──────────────────────────

@pytest.mark.parametrize("mod", TREES)
def test_the_hcmc_pair_qc083_found_is_one_lane(mod):
    rows = _wins("HCMC") + _wins("HCMC (Cat Lai)")
    lanes = mod.aggregate_lanes(rows)
    assert len(lanes) == 1, f"one port, two spellings, {len(lanes)} lanes: {sorted(lanes)}"
    only = next(iter(lanes.values()))
    assert only["requests"] == 4 and only["wins"] == 4


@pytest.mark.parametrize("mod", TREES)
@pytest.mark.parametrize("a,b", [
    ("HCMC", "Ho Chi Minh"),
    ("HCMC", "Cat Lai"),
    ("Cat Lai", "Cai Mep"),
    ("HCMC", "HCMC (Cat Lai)"),
    ("Busan", "Port Busan"),
    ("Lat Krabang", "Lat Krab"),
    ("Yokohama", "Jpyok"),
])
def test_spellings_of_one_port_bucket_together(mod, a, b):
    assert len(mod.aggregate_lanes(_wins(a) + _wins(b))) == 1, (
        f"{a!r} and {b!r} are one port and must be one lane")


# ── the point: the median stops starving ──────────────────────────────────

@pytest.mark.parametrize("mod", TREES)
def test_a_split_spelling_no_longer_starves_the_winning_median(mod):
    """PRICE_GAP_MIN_LANE_WINS is 3. Two wins per spelling is four on the
    lane — enough — but only if they land in one bucket."""
    rows = _wins("HCMC", rate=3000.0) + _wins("Cat Lai", rate=3200.0)
    med = mod.compute_lane_winning_medians(rows)
    assert med, "four wins on one lane still produce no median"
    # Reachable by EITHER spelling, because decide_status looks up by the
    # row's own raw lane string.
    assert med.get("Oakland → HCMC") == med.get("Oakland → Cat Lai") is not None


@pytest.mark.parametrize("mod", TREES)
def test_decide_status_can_still_reach_the_median_it_was_given(mod):
    """THE DRIFT TRAP. Bucketing one side canonically and looking up the other
    side raw returns None silently — which reads as "no lane history" and
    drops every Q&L on the lane from PRICE to UNDIFFERENTIATED. Same wrong
    answer, arrived at a new way."""
    rows = _wins("HCMC", n=2, rate=3000.0) + _wins("Cat Lai", n=2, rate=3000.0)
    med = mod.compute_lane_winning_medians(rows)
    d = mod.decide_status(
        has_send=False, mdolx_ref=None, response_timestamp="2026-01-05T10:00:00Z",
        quoted=True, etd_fit_days=0, request_timestamp="2026-01-04T10:00:00Z",
        ol_rate=9000.0, lane="Oakland → Cat Lai", lane_winning_median=med)
    assert d.loss_reason == "PRICE", (
        f"a rate 3x the lane median did not classify as PRICE ({d.loss_reason}) "
        f"— the median lookup missed")


@pytest.mark.parametrize("mod", TREES)
def test_a_display_keyed_dict_from_another_caller_still_works(mod):
    """The raw-first lookup keeps hand-built dicts working — no caller had to
    change when the bucketing did."""
    d = mod.decide_status(
        has_send=False, mdolx_ref=None, response_timestamp="2026-01-05T10:00:00Z",
        quoted=True, etd_fit_days=0, request_timestamp="2026-01-04T10:00:00Z",
        ol_rate=9000.0, lane="Oakland → Yokohama",
        lane_winning_median={"Oakland → Yokohama": 3000.0})
    assert d.loss_reason == "PRICE"


# ── IT MUST DISCRIMINATE ──────────────────────────────────────────────────

@pytest.mark.parametrize("mod", TREES)
@pytest.mark.parametrize("a,b", [
    ("Yokohama", "Tokyo"),
    ("Yokohama", "Osaka"),
    ("HCMC", "Busan"),
    ("Shanghai", "Ningbo"),
    ("Kobe", "Tokyo"),
])
def test_different_ports_stay_different_lanes(mod, a, b):
    assert len(mod.aggregate_lanes(_wins(a) + _wins(b))) == 2, (
        f"{a!r} and {b!r} are different ports and were merged")


@pytest.mark.parametrize("mod", TREES)
def test_a_different_origin_is_a_different_lane(mod):
    rows = _wins("Tokyo", origin="Oakland") + _wins("Tokyo", origin="Seattle")
    assert len(mod.aggregate_lanes(rows)) == 2


@pytest.mark.parametrize("mod", TREES)
def test_two_unresolvable_lanes_do_not_merge_into_a_third_thing_silently(mod):
    """canonical_port_key returns "unknown" for what it cannot resolve. Two
    such rows DO bucket together — they are equally unattributable — but the
    displayed lane must still be a spelling the rows actually carried, never
    the word "unknown"."""
    lanes = mod.aggregate_lanes(_wins("Zzz Nowhere") + _wins("Qqq Elsewhere"))
    for key in lanes:
        assert "unknown" not in key.lower(), (
            f"the bucket id leaked into the display: {key!r}")


# ── display is chosen, and chosen deterministically ───────────────────────

@pytest.mark.parametrize("mod", TREES)
def test_the_display_spelling_is_one_the_rows_actually_carried(mod):
    lanes = mod.aggregate_lanes(_wins("HCMC", n=3) + _wins("Cat Lai", n=1))
    key = next(iter(lanes))
    assert key in ("Oakland → HCMC", "Oakland → Cat Lai")
    assert key == "Oakland → HCMC", "the more common spelling should win"


@pytest.mark.parametrize("mod", TREES)
def test_the_display_is_stable_across_identical_inputs(mod):
    """A display that flipped between fires would make the dashboard and the
    PDF disagree about the same lane on the same day."""
    rows = _wins("Cat Lai", n=2) + _wins("HCMC", n=2)      # a deliberate tie
    first = sorted(mod.aggregate_lanes(rows))
    for _ in range(5):
        assert sorted(mod.aggregate_lanes(rows)) == first


def test_both_trees_agree_on_every_bucket_in_this_file():
    for a, b in [("HCMC", "Cat Lai"), ("Yokohama", "Jpyok"),
                 ("Yokohama", "Tokyo"), ("Busan", "Port Busan")]:
        rows = _wins(a) + _wins(b)
        assert sorted(core.aggregate_lanes(rows)) == sorted(HC.aggregate_lanes(rows)), (
            f"the trees disagree about {a!r} vs {b!r}")
        assert core.canonical_lane_id(f"Oakland → {a}") == HC.canonical_lane_id(f"Oakland → {a}")
