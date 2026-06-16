"""Tests for core.report_business_day — the single source of truth for which
business day the daily email REPORTS ON.

The fire moved to ~6 PM ET (2026-06-16, Michael "move this to end of every
day"), so the default window is "current": a weekday reports ITSELF (the
now-complete Pacific business day) and a weekend rolls back to Friday. The
old 10 AM ET morning behavior survives behind window="previous".

report_business_day is shared logic that MUST be byte-identical between
scripts/core.py and src/hilmar/core.py (QC-040 + test_core_parity guard the
pair). These tests exercise BOTH trees and assert they agree.
"""
from __future__ import annotations

import importlib.util
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
SRC = ROOT / "src"
for _p in (str(SRC), str(SCRIPTS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


scripts_core = _load(SCRIPTS / "core.py", "scripts_core_rbd")
hilmar_core = _load(SRC / "hilmar" / "core.py", "hilmar_core_rbd")

# Week of 2026-06-01: Mon=06-01, Tue=06-02, Wed=06-03, Thu=06-04, Fri=06-05,
# Sat=06-06, Sun=06-07.
_CURRENT_CASES = [
    (date(2026, 6, 1), date(2026, 6, 1)),    # Mon -> Mon (today)
    (date(2026, 6, 2), date(2026, 6, 2)),    # Tue -> Tue
    (date(2026, 6, 3), date(2026, 6, 3)),    # Wed -> Wed
    (date(2026, 6, 4), date(2026, 6, 4)),    # Thu -> Thu
    (date(2026, 6, 5), date(2026, 6, 5)),    # Fri -> Fri
    (date(2026, 6, 6), date(2026, 6, 5)),    # Sat -> Fri
    (date(2026, 6, 7), date(2026, 6, 5)),    # Sun -> Fri
]

_PREVIOUS_CASES = [
    (date(2026, 6, 1), date(2026, 5, 29)),   # Mon -> last Friday
    (date(2026, 6, 2), date(2026, 6, 1)),    # Tue -> Mon
    (date(2026, 6, 3), date(2026, 6, 2)),    # Wed -> Tue
    (date(2026, 6, 4), date(2026, 6, 3)),    # Thu -> Wed
    (date(2026, 6, 5), date(2026, 6, 4)),    # Fri -> Thu
    (date(2026, 6, 6), date(2026, 6, 5)),    # Sat -> Fri
    (date(2026, 6, 7), date(2026, 6, 5)),    # Sun -> Fri
]


@pytest.mark.parametrize("mod", [scripts_core, hilmar_core])
@pytest.mark.parametrize("today,expected", _CURRENT_CASES)
def test_current_window(mod, today, expected):
    assert mod.report_business_day(today, window="current") == expected


@pytest.mark.parametrize("mod", [scripts_core, hilmar_core])
@pytest.mark.parametrize("today,expected", _PREVIOUS_CASES)
def test_previous_window(mod, today, expected):
    assert mod.report_business_day(today, window="previous") == expected


@pytest.mark.parametrize("mod", [scripts_core, hilmar_core])
def test_default_window_is_current(mod):
    """REPORT_WINDOW defaults to "current" (no HILMAR_REPORT_WINDOW override
    in the test env), so a bare call equals the current-window result."""
    assert mod.REPORT_WINDOW == "current"
    for today, expected in _CURRENT_CASES:
        assert mod.report_business_day(today) == expected


@pytest.mark.parametrize("today,expected", _CURRENT_CASES)
def test_both_trees_agree_current(today, expected):
    a = scripts_core.report_business_day(today, window="current")
    b = hilmar_core.report_business_day(today, window="current")
    assert a == b == expected, f"report_business_day drift (current): scripts={a} hilmar={b}"


@pytest.mark.parametrize("today,expected", _PREVIOUS_CASES)
def test_both_trees_agree_previous(today, expected):
    a = scripts_core.report_business_day(today, window="previous")
    b = hilmar_core.report_business_day(today, window="previous")
    assert a == b == expected, f"report_business_day drift (previous): scripts={a} hilmar={b}"


@pytest.mark.parametrize("mod", [scripts_core, hilmar_core])
def test_accepts_aware_datetime(mod):
    """now_et may be a datetime (uses its .date()), not just a date — a Tuesday
    evening fire at 6 PM ET still reports that Tuesday."""
    from datetime import datetime
    dt = datetime(2026, 6, 2, 18, 0, tzinfo=mod.ET)
    assert mod.report_business_day(dt, window="current") == date(2026, 6, 2)
