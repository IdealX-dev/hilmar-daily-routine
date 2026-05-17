"""
Architectural invariants — fail loudly if structural rules are violated.

These tests don't exercise behavior; they enforce design rules that are
easy to break in a 1-line edit and cause whole categories of bugs.

Ported 2026-05-17 from the dormant `hilmar-tracker` repo (which ran on a
Linux C3 VM before the Cloud PC pivot on 2026-05-06). Original source:
github.com/IdealX-dev/hilmar-tracker tests/test_invariants.py.

Path adaptations: `src/hilmar/` → `scripts/`, `qc.py` → `qc_selfheal.py`.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


# ─────────────────────────────────────────────────────────────────────
# Phase A — single-writer invariant for derived aggregates
# ─────────────────────────────────────────────────────────────────────

# Aggregate fields that MUST only be assigned in qc_selfheal.py's Phase 5
# (Aggregate rebuild). Pre-Phase-A these were also written by ingest, which
# produced drift the moment qc_selfheal mutated requests between the two
# writes. Phase A made qc_selfheal the sole writer; this test enforces it.
#
# Naming note (hilmar-daily-routine vs the dormant hilmar-tracker):
# Current codebase uses `lane_summary` / `carrier_summary` (dict, lane→stats),
# not `lanes` / `carriers` (list). gen_dashboard.py, gen_pdf.py, and
# gen_rate_intelligence.py all read `data["lane_summary"]` directly.
_AGGREGATE_FIELDS = ("summary", "lane_summary", "carrier_summary")

# Files exempt from the rule. qc_selfheal.py is the daily-pipeline writer.
# link_mdolx_wins.py is a manual backfill utility that explicitly recomputes
# both aggregates from raw `requests` data — same algorithm as qc_selfheal,
# used when reattributing historical WINs to their MDOLX refs. Both writers
# use the canonical core.aggregate_* functions, so they can't drift apart.
_ALLOWED_WRITERS = {"qc_selfheal.py", "link_mdolx_wins.py"}


def _all_python_sources() -> list[Path]:
    return sorted(p for p in SCRIPTS.rglob("*.py") if "__pycache__" not in p.parts)


def test_only_qc_writes_summary_lanes_carriers():
    """No file other than qc_selfheal.py may contain `data["summary"] = ...`,
    `data["lanes"] = ...`, or `data["carriers"] = ...`. Catches the next
    accidental ingest-time aggregate write before it ships."""
    violations: list[str] = []
    for path in _all_python_sources():
        if path.name in _ALLOWED_WRITERS:
            continue
        text = path.read_text(encoding="utf-8")
        for line_no, line in enumerate(text.splitlines(), 1):
            for field in _AGGREGATE_FIELDS:
                # Match `<anything>["<field>"] = ...` — covers data, doc,
                # tracking, tracking_data, etc.
                if re.search(rf'\["{field}"\]\s*=\s*[^=]', line):
                    violations.append(
                        f'{path.relative_to(SCRIPTS.parent)}:{line_no}: '
                        f'aggregate write outside qc_selfheal.py — {line.strip()!r}'
                    )
    assert not violations, (
        "Phase A invariant violation — only qc_selfheal.py Phase 5 "
        "may write summary/lanes/carriers:\n  " + "\n  ".join(violations)
    )


def test_qc_writes_all_three_aggregates():
    """Symmetric check: qc_selfheal.py must in fact write all three
    aggregates. If a future refactor accidentally drops one (e.g. forgets
    to refresh `lanes` after touching Phase 5), the email scoreboard goes
    stale silently. This test catches the omission."""
    qc_path = SCRIPTS / "qc_selfheal.py"
    qc_text = qc_path.read_text(encoding="utf-8")
    for field in _AGGREGATE_FIELDS:
        pattern = rf'data\["{field}"\]\s*=\s*'
        assert re.search(pattern, qc_text), (
            f'qc_selfheal.py is missing the assignment to data["{field}"] — '
            "Phase 5 must remain the sole writer of all three aggregates."
        )


# ─────────────────────────────────────────────────────────────────────
# Dead-field rule — these fields have no consumer and must not be
# written anywhere. Re-introducing them is almost certainly a mistake
# (a fresh writer with no reader = the same scaffolding cruft Phase A
# cleaned up).
# ─────────────────────────────────────────────────────────────────────

_DEAD_FIELDS = (
    # Legacy names from the dormant hilmar-tracker repo's schema. Adding a
    # writer here without a reader is the same scaffolding-cruft pattern
    # Phase A removed.
    "lanes",             # OLD name (list form) — current uses lane_summary (dict)
    "carriers",          # OLD name (list form) — current uses carrier_summary (dict)
    "escalations_sent",  # schema-only, no live consumer
    "mdolx_bookings",    # schema-only, no live consumer
)


def test_dead_aggregate_fields_have_no_writer():
    """No file may write `data["lane_summary"]`, `data["escalations_sent"]`,
    or `data["mdolx_bookings"]` — these have no readers anywhere in
    scripts/, and adding a writer without a reader produces the exact
    pattern Phase A removed."""
    violations: list[str] = []
    for path in _all_python_sources():
        text = path.read_text(encoding="utf-8")
        for line_no, line in enumerate(text.splitlines(), 1):
            for field in _DEAD_FIELDS:
                if re.search(rf'\["{field}"\]\s*=\s*[^=]', line):
                    violations.append(
                        f'{path.relative_to(SCRIPTS.parent)}:{line_no}: '
                        f'write to dead field {field!r} — {line.strip()!r}'
                    )
    assert not violations, (
        "Dead-field rule violation:\n  " + "\n  ".join(violations)
    )


if __name__ == "__main__":
    import inspect
    mod = sys.modules[__name__]
    funcs = [(n, f) for n, f in inspect.getmembers(mod, inspect.isfunction)
             if n.startswith("test_")]
    passed = failed = 0
    for name, fn in funcs:
        try:
            fn()
            passed += 1
        except AssertionError as e:
            failed += 1
            print(f"  FAIL {name}: {e}")
        except Exception as e:
            failed += 1
            print(f"  ERROR {name}: {type(e).__name__}: {e}")
    print(f"\n{passed} passed, {failed} failed of {len(funcs)} invariant tests")
    sys.exit(0 if failed == 0 else 1)
