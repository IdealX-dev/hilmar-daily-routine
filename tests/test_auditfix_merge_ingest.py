"""Regression test for the merge_ingest.py audit fix.

Audit finding: scripts/merge_ingest.py formatted PT/ET times with the
Unix-only `%-I` strftime token (lines 89/91), which CLAUDE.md §8 forbids
because it raises ValueError on the Windows Cloud PC. The fix routes both
lines through a module-local `_fmt_time` helper that maps `%-I`->`%#I`
on win32, mirroring the established gen_email._fmt_date pattern.

These tests fail against the pre-fix source (bare `.strftime("%-I...")`
with no helper) and pass against the fixed source.
"""
from __future__ import annotations

import importlib.util
from datetime import datetime, timezone
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
MERGE_INGEST_SRC = SCRIPTS / "merge_ingest.py"


def _load_merge_ingest():
    """Load scripts/merge_ingest.py as a module (it imports sibling `core`)."""
    import sys

    sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location("merge_ingest", MERGE_INGEST_SRC)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_no_bare_dash_strftime_tokens_in_source():
    """The bare Unix-only `.strftime("%-I...")` calls must be gone; the
    `%-I` tokens may only survive inside the win32-mapping helper."""
    src = MERGE_INGEST_SRC.read_text(encoding="utf-8")
    # The pre-fix code called .strftime("%-I:...") directly on a datetime.
    assert '.strftime("%-I' not in src, (
        "merge_ingest.py still calls .strftime() directly with a Unix-only "
        "%-I token — must route through the platform-aware helper"
    )
    assert '.strftime("%-d' not in src
    assert '.strftime("%-m' not in src
    assert '.strftime("%-H' not in src


def test_fmt_time_helper_exists():
    mod = _load_merge_ingest()
    assert hasattr(mod, "_fmt_time"), "merge_ingest must expose _fmt_time helper"


def test_fmt_time_maps_dash_tokens_on_win32(monkeypatch):
    """On win32 the helper must translate `%-I` -> `%#I` (and the other
    dash tokens) before calling strftime, so msvcrt never sees `%-I`."""
    mod = _load_merge_ingest()
    captured = {}

    class _FakeDT:
        def strftime(self, fmt):
            captured["fmt"] = fmt
            return "STRFTIME_OK"

    monkeypatch.setattr(mod.sys, "platform", "win32")
    out = mod._fmt_time(_FakeDT(), "%-I:%M %p PT")
    assert out == "STRFTIME_OK"
    assert "%-I" not in captured["fmt"], "win32 branch left a Unix-only token in the format"
    assert "%#I" in captured["fmt"]


def test_fmt_time_passthrough_on_unix(monkeypatch):
    """On non-win32 the dash tokens are preserved (real strftime supports them)."""
    mod = _load_merge_ingest()
    captured = {}

    class _FakeDT:
        def strftime(self, fmt):
            captured["fmt"] = fmt
            return "OK"

    monkeypatch.setattr(mod.sys, "platform", "linux")
    mod._fmt_time(_FakeDT(), "%-I:%M %p PT")
    assert captured["fmt"] == "%-I:%M %p PT"


def test_fmt_time_real_format_produces_no_leading_zero_hour():
    """End-to-end: a real datetime formats without a ValueError and without a
    zero-padded hour (the user-visible reason the dash token was used)."""
    mod = _load_merge_ingest()
    dt = datetime(2026, 6, 26, 9, 5, tzinfo=timezone.utc)
    out = mod._fmt_time(dt, "%-I:%M %p")
    # On this Linux env the dash token yields a non-padded hour.
    assert out.startswith("9:05"), out
