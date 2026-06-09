"""QC governance — the mechanical enforcement of Michael's standing rule
(2026-06-09): "all qc's must be checked and self healed; all root issues
must be solved and not patched; this is a constant."

WHY THIS FILE EXISTS: the rule lived only as prose in CLAUDE.md §3 for a
month and silently rotted — by 2026-06-09 seven live checks (QC-043..048,
050) were missing from reports/QC-INDEX.md and three (QC-051/052/053) had
no Sentry-remediation routing. Prose rules don't enforce themselves. This
test does: it derives the QC inventory from the code at runtime and FAILS
CI when a check is added without its documentation, its remediation
routing, or a regression test. Adding a QC check without these now breaks
the build instead of quietly drifting.

Four invariants:
  INV-1  Every QC-NNN emitted by qc_selfheal.py has a row in QC-INDEX.md.
  INV-2  Every ACTIONS key in qc_actions_from_sentry.py is a real emitted
         check (no orphan remediation mappings).
  INV-3  Every QC-NNN documented in QC-INDEX.md is actually emitted
         (no phantom docs), except explicitly RETIRED checks.
  INV-4  Every emitted QC-NNN has a regression test somewhere in tests/,
         except a KNOWN_UNTESTED ratchet set that MAY ONLY SHRINK. A new
         check with no test fails here.

IDs are normalized to their numeric parent (QC-014a/QC-014b -> QC-014) so
a check documented under a sub-variant satisfies the parent and vice versa.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
QC_SELFHEAL = ROOT / "scripts" / "qc_selfheal.py"
QC_ACTIONS = ROOT / "scripts" / "qc_actions_from_sentry.py"
QC_INDEX = ROOT / "reports" / "QC-INDEX.md"
TESTS_DIR = ROOT / "tests"

# Real QC IDs are always 3-digit zero-padded (QC-001..QC-053), optionally
# with a sub-variant letter (QC-014a). The {3} guard avoids matching prose
# fragments like a bare "QC-04" inside a doc sentence.
_QC_RE = re.compile(r"QC-(\d{3})[a-z]?")
_QC_FULL_RE = re.compile(r"QC-\d{3}[a-z]?")


def _parent(qc_id: str) -> str:
    """QC-014a -> QC-014; QC-014 -> QC-014."""
    m = re.match(r"(QC-\d{3})", qc_id)
    return m.group(1) if m else qc_id


def _parents_in_text(text: str) -> set[str]:
    return {f"QC-{m.group(1)}" for m in _QC_RE.finditer(text)}


# ── Checks intentionally removed from the live engine. A retired check may
#    stay documented (with its retirement note) but must NOT be emitted.
RETIRED: frozenset[str] = frozenset({
    "QC-038",   # ol-quote-tracker reconciliation — retired 2026-05-21 (phantom drift)
})

# ── Test-coverage ratchet. These emitted checks have no dedicated
#    regression test YET. The set MAY ONLY SHRINK — adding an ID here fails
#    test_known_untested_only_shrinks. Every NEW QC check must ship with a
#    test (it cannot be added to this list). Work the backlog down over time.
KNOWN_UNTESTED: frozenset[str] = frozenset({
    "QC-009", "QC-010", "QC-012", "QC-013", "QC-016", "QC-018", "QC-019",
    "QC-020", "QC-022", "QC-023", "QC-024", "QC-025", "QC-026", "QC-030",
    "QC-032", "QC-033", "QC-034", "QC-035", "QC-036", "QC-043", "QC-044",
    "QC-046", "QC-047", "QC-048", "QC-050", "QC-051",
})
# Size at creation (2026-06-09): 26. The ratchet test asserts len never grows.
_KNOWN_UNTESTED_CEILING = 26


def emitted_checks() -> set[str]:
    """Parent QC IDs the live engine actually emits (PASS/WARN/ERROR/fix
    log lines all carry the QC-NNN tag)."""
    text = QC_SELFHEAL.read_text(encoding="utf-8")
    return _parents_in_text(text)


def documented_checks() -> set[str]:
    text = QC_INDEX.read_text(encoding="utf-8")
    # Drop obvious partials like a bare "QC-04" (no following digit boundary
    # issue) by requiring the full QC-\d+ token via _parent on full matches.
    return {_parent(m.group(0)) for m in _QC_FULL_RE.finditer(text)}


def actions_keys() -> set[str]:
    text = QC_ACTIONS.read_text(encoding="utf-8")
    # ACTIONS keys appear as quoted "QC-NNN": dict literals.
    return {_parent(m.group(1)) for m in re.finditer(r'"(QC-\d+[a-z]?)"\s*:', text)}


def _tested_check_ids() -> set[str]:
    out: set[str] = set()
    for p in TESTS_DIR.glob("test_*.py"):
        if p.name == "test_qc_governance.py":
            continue
        out |= _parents_in_text(p.read_text(encoding="utf-8", errors="ignore"))
    return out


# ── INV-1 ────────────────────────────────────────────────────────────────
def test_every_emitted_check_is_documented():
    emitted = emitted_checks()
    documented = documented_checks()
    missing = sorted(emitted - documented - RETIRED, key=lambda s: int(s[3:]))
    assert not missing, (
        "QC checks emitted by qc_selfheal.py but MISSING from "
        "reports/QC-INDEX.md (add a row for each — the index is the QC "
        f"source of truth):\n  {', '.join(missing)}"
    )


# ── INV-2 ────────────────────────────────────────────────────────────────
def test_no_orphan_actions_mappings():
    emitted = emitted_checks()
    orphans = sorted(actions_keys() - emitted - RETIRED, key=lambda s: int(s[3:]))
    assert not orphans, (
        "qc_actions_from_sentry.ACTIONS maps QC checks that the engine no "
        f"longer emits (stale routing — remove or fix):\n  {', '.join(orphans)}"
    )


# ── INV-3 ────────────────────────────────────────────────────────────────
def test_no_phantom_documented_checks():
    emitted = emitted_checks()
    phantom = sorted(documented_checks() - emitted - RETIRED, key=lambda s: int(s[3:]))
    assert not phantom, (
        "reports/QC-INDEX.md documents QC checks the engine does NOT emit "
        "(phantom docs — remove the row or mark RETIRED):\n  "
        f"{', '.join(phantom)}"
    )


# ── INV-4 ────────────────────────────────────────────────────────────────
def test_every_emitted_check_has_a_test_or_is_known_untested():
    emitted = emitted_checks()
    tested = _tested_check_ids()
    untested = emitted - tested - RETIRED
    new_gaps = sorted(untested - KNOWN_UNTESTED, key=lambda s: int(s[3:]))
    assert not new_gaps, (
        "QC checks with NO regression test in tests/ and NOT in the "
        "KNOWN_UNTESTED ratchet. Every QC check must ship with a test — "
        f"write one (preferred) or, only if truly deferred, this is a hard "
        f"stop:\n  {', '.join(new_gaps)}"
    )


def test_known_untested_only_shrinks():
    """The ratchet: the untested backlog may shrink, never grow. If you
    fixed a gap, remove it from KNOWN_UNTESTED and lower the ceiling."""
    assert len(KNOWN_UNTESTED) <= _KNOWN_UNTESTED_CEILING, (
        f"KNOWN_UNTESTED grew to {len(KNOWN_UNTESTED)} (ceiling "
        f"{_KNOWN_UNTESTED_CEILING}). New QC checks must ship WITH a test — "
        "do not add to this list. Lower the ceiling when you remove entries."
    )


def test_known_untested_has_no_stale_entries():
    """Keep the ratchet honest: every ID in KNOWN_UNTESTED must still be an
    emitted, currently-untested check. If a listed check got a test, drop it
    from the set (and lower the ceiling) so the backlog reflects reality."""
    emitted = emitted_checks()
    tested = _tested_check_ids()
    now_tested = sorted(KNOWN_UNTESTED & tested, key=lambda s: int(s[3:]))
    assert not now_tested, (
        "These are in KNOWN_UNTESTED but now HAVE tests — remove them from "
        f"the set and lower the ceiling:\n  {', '.join(now_tested)}"
    )
    not_emitted = sorted(KNOWN_UNTESTED - emitted - RETIRED, key=lambda s: int(s[3:]))
    assert not not_emitted, (
        "These are in KNOWN_UNTESTED but the engine no longer emits them — "
        f"remove from the set:\n  {', '.join(not_emitted)}"
    )
