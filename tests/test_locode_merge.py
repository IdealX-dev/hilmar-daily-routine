"""JPYOK is Yokohama — the UN/LOCODE merge, and the guardrails around it.

Michael, 2026-08-27, confirming the external fact himself: "JPYOK and Yokohama
are same JPYOK is the UN LOC code for Yokohama" / "makes no sense and for you
to fix".

WHAT WAS BROKEN. `body_parser._norm` Title-Cases any all-caps token longer than
three characters, so the code JPYOK became the port name "Jpyok".
`ingest.title_case_destination` did the same thing independently, and the
standalone-booking path stored a raw table-cell POD with no normalizer at all —
three spellings of one port. `aggregate_lanes` and
`compute_lane_winning_medians` both key on the raw "Oakland -> X" display
string, so Yokohama — 44 of the 134 bookings in OL's 2026 transaction report,
the largest lane in the book — split, and the split starved the lane winning
median below PRICE_GAP_MIN_LANE_WINS, flipping that lane's Q&L losses from
PRICE to UNDIFFERENTIATED.

WHAT MUST NOT BREAK. A bare five-letter rule would eat BUSAN, OSAKA, TOKYO,
GENOA, HAIFA and LAGOS — every one a real port in this corpus. The normalizer
is TABLE-GATED: only codes in core.PORT_LOCODES resolve, everything else keeps
its raw text and trips QC-015. Most of this file exists to hold that line.
"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

import body_parser as SBP  # noqa: E402
import core as core  # noqa: E402
import ingest as ingest  # noqa: E402

from hilmar import body_parser as HBP  # noqa: E402
from hilmar import core as hcore  # noqa: E402

# ── the merge, at every entry point that can write a destination ──────────

def test_body_parser_norm_merges_the_code_in_both_trees():
    """Entry point 1: scripts/body_parser.py _norm — the Title-Caser that
    manufactured "Jpyok". Feeds parse_subject_lane (every return path) and
    _prose_lane, which is what fetch_bodies stores as parsed["destination"]."""
    for bp in (SBP, HBP):
        assert bp._norm("JPYOK") == "Yokohama"


def test_subject_lane_merges_the_code_in_both_trees():
    subject = "MDOLX261200_ NEW BOOKING // HILMAR 2X40'RF Oakland to JPYOK // CMA: NAM8664237"
    for bp in (SBP, HBP):
        assert bp.parse_subject_lane(subject) == ("Oakland", "Yokohama")


def test_table_cell_pod_is_resolved_not_stored_raw():
    """Entry point 2: the rate-table cell copy. It never called _norm, so a
    Port of Discharge column reading JPYOK landed verbatim — the third
    spelling, neither "Jpyok" nor "Yokohama"."""
    body = (
        "Please see the option below.\n"
        "POL | POD | ETD | RATE | CARRIER\n"
        "USOAK | JPYOK | 12-Sep-26 | $3,200 | CMA CGM\n"
    )
    for bp in (SBP, HBP):
        out = bp.parse_rate_table(body)
        assert out.get("pod") == "Yokohama", out


def test_ingest_title_case_destination_merges_the_code():
    """Entry point 3: ingest.title_case_destination — the SECOND, independent
    Title-Caser, applied to every request row built from a subject."""
    assert ingest.title_case_destination("JPYOK") == "Yokohama"
    # And the already-damaged spelling on disk, so the fix reaches stored rows.
    assert ingest.title_case_destination("Jpyok") == "Yokohama"


def test_canonical_port_key_collapses_the_code_onto_the_port():
    """core.canonical_port_key is THE matching key — booking->request linking,
    rate-response attachment, QC duplicate detection all route through it. A
    code that slipped past every write-side normalizer must still match here,
    and the key is LOWERCASED, so resolve_locode's display spelling and the
    key cannot disagree."""
    for c in (core, hcore):
        assert c.canonical_port_key("JPYOK") == c.canonical_port_key("Yokohama") == "yokohama"
        assert c.canonical_port_key("Jpyok") == "yokohama"
        assert c.same_port("JPYOK", "Yokohama") is True
    # Still NOT the same port as its neighbour. The merge must not become a
    # licence to collapse distinct Japanese calls.
    assert core.same_port("JPYOK", "Tokyo") is False


def test_trade_region_reaches_rows_written_before_the_fix():
    """A carried-forward prior WIN is copied verbatim and never rebuilt (see
    ingest's additive merge), so "Jpyok" can persist on disk after the parser
    is fixed. It must not keep colouring the dashboard's Unmapped bucket."""
    assert core.trade_region_for("Jpyok") == "Far East"
    assert core.trade_region_for("JPYOK") == "Far East"


# ── the guardrail: an unlisted five-letter token is NEVER touched ──────────

#: Every one of these is a REAL port in this corpus and exactly five letters.
#: A shape-based rule eats all of them. Named explicitly so the failure message
#: says which port a careless normalizer just destroyed.
_FIVE_LETTER_REAL_PORTS = [
    "BUSAN", "OSAKA", "TOKYO", "GENOA", "HAIFA", "LAGOS",
    "Busan", "Osaka", "Tokyo", "Genoa", "Haifa", "Lagos",
]


@pytest.mark.parametrize("name", _FIVE_LETTER_REAL_PORTS)
def test_five_letter_real_ports_are_never_merged(name):
    assert core.resolve_locode(name) is None, (
        f"{name} is a real port, not a UN/LOCODE. resolve_locode must be "
        f"TABLE-GATED — if this fires, a shape rule crept in and it is "
        f"silently merging real business onto the wrong lane."
    )
    # And the parser leaves them exactly where they were before the table
    # existed: Title-Cased if they arrived all-caps, otherwise untouched.
    assert SBP._norm(name) == (name.title() if name.isupper() else name)


def test_no_known_destination_is_swallowed_by_the_locode_table():
    """The curated corpus is what the whole system displays and aggregates. Not
    one entry may resolve to something else."""
    swallowed = {d: core.resolve_locode(d) for d in SBP.KNOWN_DESTINATIONS
                 if core.resolve_locode(d)}
    assert not swallowed, f"PORT_LOCODES is eating real corpus ports: {swallowed}"


def test_no_trade_region_key_is_swallowed_by_the_locode_table():
    swallowed = {k: core.resolve_locode(k) for k in core._TRADE_REGION_MAP
                 if core.resolve_locode(k)}
    assert not swallowed, f"PORT_LOCODES is eating trade-region keys: {swallowed}"


def test_no_real_booked_port_is_swallowed_by_the_locode_table():
    """The strongest version of the guard: run it over the ACTUAL ports this
    book has shipped to, out of OL's own 2026 transaction report."""
    rows = json.loads((ROOT / "data" / "ol-transaction-report-2026.json")
                      .read_text(encoding="utf-8"))
    pods = {(r.get("pod") or "").strip() for r in rows} - {""}
    assert pods, "transaction report carries no PODs — fixture moved?"
    swallowed = {p: core.resolve_locode(p) for p in pods if core.resolve_locode(p)}
    assert not swallowed, f"PORT_LOCODES is eating ports OL actually booked: {swallowed}"


def test_resolve_locode_refuses_everything_that_is_not_a_listed_code():
    for junk in (None, "", "   ", "JP", "JPYOKO", "JP-YOK", "JPY0K", 5, ["JPYOK"]):
        assert core.resolve_locode(junk) is None, junk


# ── governance: the table cannot drift away from the corpus ───────────────

def test_every_locode_value_is_a_real_corpus_port():
    """A PORT_LOCODES value must be a spelling the rest of the system already
    knows — otherwise the merge trades an unmapped CODE for an unmapped NAME
    and QC-015 goes right on complaining. Mirrors the discipline
    tests/test_auditfix_qc057_dest_recovery.py applies to KNOWN_DESTINATIONS."""
    for code, name in core.PORT_LOCODES.items():
        assert name in SBP.KNOWN_DESTINATIONS, (
            f"{code} -> {name!r} is not in body_parser.KNOWN_DESTINATIONS. Add "
            f"the port to the corpus and the trade-region map first.")
        assert core.trade_region_for(name) != "Unmapped", (
            f"{code} -> {name!r} has no trade region — the merge would just "
            f"move the QC-015 warning, not clear it.")


def test_locode_keys_have_locode_shape():
    for code in core.PORT_LOCODES:
        assert len(code) == 5 and code.isalpha() and code.isupper(), (
            f"{code!r} is not a UN/LOCODE (2-letter country + 3-letter "
            f"locality, upper-case). resolve_locode will never match it.")


def test_locode_table_is_identical_across_trees():
    """PORT_LOCODES is a CLAUDE.md paired surface. Drift here means the two
    trees disagree about which physical port a booking landed on —
    test_core_parity's constants walk also covers it; asserted directly so the
    failure names the actual cause."""
    assert core.PORT_LOCODES == hcore.PORT_LOCODES
    for code in core.PORT_LOCODES:
        assert core.resolve_locode(code) == hcore.resolve_locode(code)


# ── why the migration exists: the stored key moves ────────────────────────

def test_merging_the_destination_changes_the_request_id():
    """THE REASON THIS SHIP NEEDS A MIGRATION, pinned as arithmetic.

    core.request_id hashes the destination, so renaming "Jpyok" to "Yokohama"
    re-keys the row. Any operator_corrections.json entry keyed to the old hash
    then matches nothing — and ingest.apply_operator_corrections only PRINTS a
    WARN for that, so a human verdict would be dropped in silence. QC-082 is
    the alarm; scripts/migrate_locode_rekey.py is the repair."""
    imid, ts = "<AAA@ol-usa.com>", "2026-08-20T17:04:00Z"
    old = core.request_id(imid, ts, "Jpyok")
    new = core.request_id(imid, ts, "Yokohama")
    assert old != new, (
        "request_id no longer depends on destination — if that is deliberate, "
        "the LOCODE migration is unnecessary and should be retired.")
    # The raw and Title-Cased spellings always hashed the SAME (request_id
    # lowercases), so the code/parser split was a DISPLAY and AGGREGATION
    # split, not an id split. Exactly one id moves per affected row.
    assert core.request_id(imid, ts, "JPYOK") == old


def test_locode_split_would_fragment_the_lane_rollup():
    """The damage, measured on core's own aggregation. aggregate_lanes keys on
    the raw display string, so before the merge one port produced two lanes."""
    def _rows(dest):
        return [{"status": "WIN", "origin": "Oakland", "destination": dest,
                 "lane": f"Oakland → {dest}", "teu_requested": 2, "teu_won": 2,
                 "ol_rate": 3000.0} for _ in range(2)]
    split = core.aggregate_lanes(_rows("Jpyok") + _rows("Yokohama"))
    assert len(split) == 2, "fixture no longer demonstrates the split"
    merged_dest = ingest.title_case_destination("Jpyok")
    merged = core.aggregate_lanes(_rows(merged_dest) + _rows("Yokohama"))
    assert len(merged) == 1, merged
    assert next(iter(merged)) == "Oakland → Yokohama"
    # And with 4 wins on one lane the winning median exists, where 2+2 split
    # left both halves under PRICE_GAP_MIN_LANE_WINS and unbenchmarked.
    assert core.compute_lane_winning_medians(_rows(merged_dest) + _rows("Yokohama"))
    assert not core.compute_lane_winning_medians(_rows("Jpyok") + _rows("Yokohama"))
