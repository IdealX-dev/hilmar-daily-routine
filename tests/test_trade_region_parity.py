"""Trade-region cross-tree divergence is DELIBERATE and LOCKED (finding [12]).

scripts/core.trade_region_for and src/hilmar/core.trade_region implement
destination->region with intentionally DIFFERENT taxonomies:
  - production (scripts): COARSE trade-lane buckets ("SE Asia", "Far East",
    "Europe", "Central America", ...) with the sentinel "Unmapped". This is what
    ships in the client PDF/email and satisfies Michael's "nothing is ever
    Unmapped" extend-the-map rule.
  - library (src/hilmar): FINE country buckets ("Thailand", "Japan", "China",
    "Korea", ...) with the sentinel "Other".

Picking the single canonical taxonomy is a product/migration decision for the
operator (it changes either client-facing PDF vocabulary or the library's
render/tests), so this is NOT converged here. Instead these tests:
  1. lock the deliberate SENTINEL split so neither tree silently adopts the
     other's (which would mask a real mapping gap), and
  2. enforce production's "nothing the operator added is Unmapped" rule on the
     destinations the audit flagged.
This keeps the divergence VISIBLE and non-wideanable until the taxonomy decision
is made, rather than leaving it as silent, unguarded drift.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

import core as SC  # noqa: E402  scripts/core.py

from hilmar import core as HC  # noqa: E402

# Destinations the audit flagged as production-mapped but "Other" in the library.
_OPERATOR_ADDED = ["Dublin", "Acajutla", "Caucedo", "Pasir Gudang",
                   "Lat Krabang", "Sturgis"]
_COMMON = ["Bangkok", "Yokohama", "Shanghai", "Busan", "Rotterdam"]


def test_functions_are_deliberately_separate():
    """The two trees expose differently-named functions on purpose (coarse vs
    fine taxonomy). If they ever unify, replace this divergence lock with a
    real parity assertion."""
    assert hasattr(SC, "trade_region_for") and not hasattr(SC, "trade_region")
    assert hasattr(HC, "trade_region") and not hasattr(HC, "trade_region_for")


def test_production_never_returns_the_library_sentinel():
    """Production's sentinel is 'Unmapped'. It must never emit 'Other' (the
    library's sentinel) — that would mean a tree silently adopted the other's
    taxonomy and the extend-the-map rule stopped being enforceable."""
    for d in _OPERATOR_ADDED + _COMMON + ["Mars", None, ""]:
        assert SC.trade_region_for(d) != "Other"


def test_library_never_returns_the_production_sentinel():
    for d in _OPERATOR_ADDED + _COMMON + ["Mars", None, ""]:
        assert HC.trade_region(d) != "Unmapped"


def test_production_maps_every_operator_added_destination():
    """Michael's rule: 'nothing is ever Unmapped'. Every destination the
    operator explicitly added must resolve to a real coarse bucket."""
    unmapped = [d for d in _OPERATOR_ADDED if SC.trade_region_for(d) == "Unmapped"]
    assert not unmapped, (
        f"production trade_region_for left operator-added destinations Unmapped: "
        f"{unmapped} — extend scripts/core._TRADE_REGION_MAP"
    )


def test_production_truly_unknown_is_unmapped_not_other():
    assert SC.trade_region_for("Totally Fake Port 9000") == "Unmapped"
    assert SC.trade_region_for(None) == "Unmapped"


# ── country-qualified destinations resolve to the bare port's region ─────────
# Michael 2026-08-05, on a dashboard where all five rows sat under Unmapped:
# "unmapped shouldn't exist". Every flagged destination — Shanghai, Busan,
# Qingdao, Yokohama — was already in the map. The LOOKUP was dropping them: it
# tried the whole string, then the part before "(", so "Shanghai, CN" missed on
# both. The standing "Unmapped means extend the map" rule then aimed every
# previous look at the one thing that was already correct.

def test_country_qualified_destinations_resolve_like_the_bare_port():
    """The exact four from the 2026-08-05 dashboard."""
    for qualified, bare in [("Shanghai, CN", "Shanghai"), ("Busan, KR", "Busan"),
                            ("Qingdao, CN", "Qingdao"), ("Yokohama, JP", "Yokohama")]:
        assert SC.trade_region_for(qualified) == SC.trade_region_for(bare) != "Unmapped"


def test_every_mapped_port_survives_a_country_suffix():
    """The property the four cases are instances of, asserted over the WHOLE
    map so the next port to arrive comma-qualified is covered before it ships.
    A per-destination test only ever proves the destinations someone already
    saw fail."""
    broken = [
        port for port in SC._TRADE_REGION_MAP
        if SC.trade_region_for(f"{port}, XX") != SC._TRADE_REGION_MAP[port]
    ]
    assert not broken, (
        f"{len(broken)} mapped port(s) go Unmapped once a country suffix is "
        f"appended: {broken[:10]}"
    )


def test_a_country_suffix_cannot_invent_a_region():
    """Peeling comma segments must only ever match a key that is genuinely in
    the map. It must not infer a region from the country code — otherwise
    'Unmapped' stops meaning 'extend the map' and starts meaning 'we guessed'."""
    assert SC.trade_region_for("Totally Fake Port 9000, CN") == "Unmapped"
    assert SC.trade_region_for(", CN") == "Unmapped"
    assert SC.trade_region_for("CN") == "Unmapped"


def test_paren_qualified_keys_still_beat_the_bare_port():
    """'Manzanillo (Panama)' is its own key because a bare 'Manzanillo' is a
    different port on a different coast. Trying the whole string first is what
    stops the comma/paren peeling from collapsing them."""
    assert SC.trade_region_for("Manzanillo (Panama)") == "Central America"
    assert SC.trade_region_for("Manzanillo (Panama), PA") == "Central America"
    assert SC.trade_region_for("HCMC (Cat Lai)") == "SE Asia"
