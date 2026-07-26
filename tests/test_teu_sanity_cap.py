"""Per-row TEU sanity ceiling — core.teu_implausible / parse_teu / QC-070.

THE FAILURE (2026-07-26): a reference number in a subject line ("PO 4451440")
parsed as 44,514 x 40' = 89,028 TEU on ONE row. Every volume figure in the
daily email, dashboard, PDF and lane rollup is a SUM over rows, so that single
row rewrote the whole day's numbers — and a wrong-but-huge number is invisible
until a human reads the report and disbelieves it.

The regex was hardened the same day (tests/test_parse_teu_hardening.py). These
tests cover the SECOND line of defence, which is what makes the hardening
non-load-bearing: a per-row ceiling that (1) makes parse_teu refuse to return
an impossible number and (2) makes QC-070 error on any stored row above it,
whoever wrote it. A regex regression can then cost a zero, never an 89,028.

Both known real defects are asserted directly: 89,028 TEU (PO reference) and
200 TEU ("quote 10040"), plus the full set of real Hilmar spellings, which
must stay untouched.
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


qc = _load(SCRIPTS / "qc_selfheal.py", "qc_selfheal_teu_cap")


# ── core.teu_implausible ────────────────────────────────────────────────────

@pytest.mark.parametrize("count,teu", [
    (0, 0),                                     # empty row
    (1, 2),                                     # 1x40'
    (6, 12),                                    # largest real sample on record
    (core.MAX_ROW_CONTAINERS, core.MAX_ROW_TEU),  # exactly at the ceiling
])
def test_plausible_volumes_pass(count, teu):
    assert core.teu_implausible(count, teu) is None


@pytest.mark.parametrize("count,teu", [
    (core.MAX_ROW_CONTAINERS + 1, 2),           # one container over
    (1, core.MAX_ROW_TEU + 1),                  # one TEU over
    (44514, 89028),                             # THE 2026-07-26 defect
    (100, 200),                                 # the "quote 10040" misread
    (-1, 0),                                    # negatives are never real
    (0, -5),
])
def test_implausible_volumes_are_named(count, teu):
    why = core.teu_implausible(count, teu)
    assert why, f"({count}, {teu}) should be refused"
    # The reason has to be readable in a log line by whoever gets paged.
    assert isinstance(why, str) and len(why) > 10


def test_ceiling_is_far_above_real_business():
    """Guard the calibration itself, not just the comparison.

    The ceiling only works if it is unreachable by real quotes. Largest real
    Hilmar sample on record is 6x40'RF = 12 TEU; if someone ever tunes the
    constant down toward that, this fails and forces the conversation.
    """
    assert core.MAX_ROW_TEU >= 50
    assert core.MAX_ROW_CONTAINERS >= 25


# ── parse_teu refuses rather than returns garbage ───────────────────────────

def test_po_reference_cannot_produce_a_volume():
    """The exact 2026-07-26 poison string, end to end."""
    assert core.parse_teu("RFQ PO 4451440 Jakarta") == (0, 0)


def test_synthetic_over_ceiling_parse_is_refused():
    """A REAL-shaped parse above the ceiling is still refused.

    "999 x 40'HC" is well-formed — the regex reads it correctly as 999
    containers. It is the ceiling, not the regex, that must stop it, which is
    precisely the case a future regex regression falls into.
    """
    assert core.parse_teu("999 x 40'HC") == (0, 0)


def test_reverse_phrasing_over_ceiling_is_refused():
    assert core.parse_teu("40'HC x 800") == (0, 0)


@pytest.mark.parametrize("text,expected", [
    ("1x40HC", (1, 2)),
    ("2x40HC", (2, 4)),
    ("1-40' HC", (1, 2)),
    ("1X20'DV", (1, 1)),
    ("2-20'", (2, 2)),
    ("4X40'RF", (4, 8)),
    ("6x40'RF", (6, 12)),          # largest real sample
    ("3×20'DV + 1×40'HC", (4, 5)),
    ("40'HC x 2", (2, 4)),         # reverse phrasing OL and Lonny both use
])
def test_real_spellings_are_untouched_by_the_cap(text, expected):
    assert core.parse_teu(text) == expected


def test_both_core_trees_agree_on_the_ceiling():
    """scripts/core.py runs in production; src/hilmar/core.py is what CI
    covers. A ceiling that exists in only one tree is not a ceiling."""
    hilmar_core = _load(ROOT / "src" / "hilmar" / "core.py", "hilmar_core_teu_cap")
    assert hilmar_core.MAX_ROW_TEU == core.MAX_ROW_TEU
    assert hilmar_core.MAX_ROW_CONTAINERS == core.MAX_ROW_CONTAINERS
    assert hilmar_core.parse_teu("RFQ PO 4451440 Jakarta") == (0, 0)
    assert hilmar_core.parse_teu("999 x 40'HC") == (0, 0)
    assert hilmar_core.parse_teu("2x40HC") == (2, 4)


# ── QC-070 ──────────────────────────────────────────────────────────────────

def test_qc070_clean_rows_produce_no_findings():
    rows = [
        {"request_id": "R1", "containers": "2x40HC",
         "container_count": 2, "teu_requested": 4, "status": "PENDING"},
        {"request_id": "R2", "containers": "1x20'DV",
         "container_count": 1, "teu_requested": 1, "teu_won": 1, "status": "WIN"},
    ]
    assert qc.qc070_teu_sanity(rows) == []


def test_qc070_catches_and_heals_a_stored_over_count():
    """A poisoned number ALREADY in the dataset — written by an older build,
    a carry-forward, or a hand edit — never reaches a report."""
    row = {"request_id": "R9", "containers": "2x40HC",
           "container_count": 44514, "teu_requested": 89028, "status": "PENDING"}
    found = qc.qc070_teu_sanity([row])

    fields = {d.split("=")[0] for _, shape, d in found if shape == "over-count"}
    assert fields == {"teu_requested", "container_count"}
    # Healed from the row's OWN containers text, not from the poisoned value.
    assert row["teu_requested"] == 4
    assert row["container_count"] == 2


def test_qc070_heal_can_be_disabled_for_reporting():
    row = {"request_id": "R9", "containers": "2x40HC",
           "container_count": 44514, "teu_requested": 89028, "status": "PENDING"}
    found = qc.qc070_teu_sanity([row], heal=False)
    assert found
    assert row["teu_requested"] == 89028, "heal=False must not mutate"


def test_qc070_heals_teu_won_to_zero_on_a_non_win():
    """teu_won is win evidence. A LOSS row carrying one is contradictory, so
    the heal clears it rather than copying the requested volume across."""
    row = {"request_id": "R8", "containers": "1x40HC", "container_count": 1,
           "teu_requested": 2, "teu_won": 9999, "status": "LOSS"}
    found = qc.qc070_teu_sanity([row])
    assert any(d.startswith("teu_won=") for _, _, d in found)
    assert row["teu_won"] == 0


def test_qc070_heals_teu_won_from_containers_on_a_win():
    row = {"request_id": "R7", "containers": "3x40'RF", "container_count": 3,
           "teu_requested": 6, "teu_won": 9999, "status": "WIN"}
    qc.qc070_teu_sanity([row])
    assert row["teu_won"] == 6


def test_qc070_flags_container_text_that_parses_to_nothing():
    """Shape (b): the row plainly describes equipment but yields 0 TEU —
    either a parser gap or a parse refused as implausible. DETECT-ONLY,
    because healing it would mean inventing a volume."""
    row = {"request_id": "R5", "containers": "two forty foot reefers",
           "container_count": 0, "teu_requested": 0, "status": "PENDING"}
    found = qc.qc070_teu_sanity([row])
    assert [s for _, s, _ in found] == ["unparsed"]
    assert row["teu_requested"] == 0, "shape (b) must never be healed"


def test_qc070_refused_over_ceiling_parse_surfaces_as_unparsed():
    """The two defences meet: parse_teu refuses "999 x 40'HC" (returns 0), and
    QC-070 then reports the row rather than letting a silent zero pass as a
    genuinely empty request."""
    row = {"request_id": "R6", "containers": "999 x 40'HC",
           "container_count": 0, "teu_requested": 0, "status": "PENDING"}
    found = qc.qc070_teu_sanity([row])
    assert [s for _, s, _ in found] == ["unparsed"]


def test_qc070_ignores_rows_with_no_container_text():
    """An RFQ with no equipment line yet is normal, not a defect."""
    rows = [{"request_id": "R4", "containers": "", "teu_requested": 0,
             "status": "PENDING"},
            {"request_id": "R3", "teu_requested": 0, "status": "PENDING"}]
    assert qc.qc070_teu_sanity(rows) == []


def test_qc070_survives_junk_field_types():
    """The daily fire must not die on a bad row. Booleans in particular are
    ints in Python and must not be read as volumes."""
    rows = [
        {"request_id": "R2", "containers": "1x40HC", "teu_requested": None},
        {"request_id": "R1", "containers": "1x40HC", "teu_requested": "89028"},
        {"request_id": "R0", "containers": "1x40HC", "teu_requested": True},
    ]
    assert qc.qc070_teu_sanity(rows) == []


def test_qc070_handles_empty_and_none_input():
    assert qc.qc070_teu_sanity([]) == []
    assert qc.qc070_teu_sanity(None) == []
