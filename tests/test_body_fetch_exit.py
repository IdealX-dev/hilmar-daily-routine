"""One unfetchable message must not kill the fire.

2026-08-12: the first verification fire after the phantom-quote fixes died in
refresh_stage — 46 of 47 bodies fetched, ONE Graph GET failed (an Evergreen
"GRI ALERT" blast), `return 0 if body_failures == 0 else 1` exited 1 under
bash -e, and the pipeline never ran. The old rule was also a permanent trap:
a fetch-failed message never lands in the bodies file, so it retries every
run — a message deleted from the mailbox would fail every fire forever.

The rule now: exit non-zero ONLY when fetching is DEAD (failures and not one
success — broken auth / no network). A partial failure warns per-message and
proceeds; the staged record keeps its retry.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for p in (str(ROOT / "scripts"), str(ROOT / "src")):
    if p not in sys.path:
        sys.path.insert(0, p)

import refresh_stage as RS  # noqa: E402


def test_one_failed_body_among_many_does_not_kill_the_fire():
    """THE 2026-08-12 verification-fire death, as arithmetic."""
    assert RS.body_fetch_exit_code(body_count=46, body_failures=1) == 0


def test_all_failures_and_no_successes_is_fatal():
    """Zero successes with failures present is the broken-auth / no-network
    signature — the one case where dying loudly beats proceeding blind."""
    assert RS.body_fetch_exit_code(body_count=0, body_failures=5) == 1
    assert RS.body_fetch_exit_code(body_count=0, body_failures=1) == 1


def test_quiet_day_is_clean():
    assert RS.body_fetch_exit_code(body_count=0, body_failures=0) == 0
    assert RS.body_fetch_exit_code(body_count=10, body_failures=0) == 0


def test_main_returns_the_shared_rule_not_its_own():
    """main() must delegate — a hand-rolled comparison at the return site is
    how the any-failure-fatal rule got in unreviewed the first time."""
    src = (ROOT / "scripts" / "refresh_stage.py").read_text(encoding="utf-8")
    assert "return body_fetch_exit_code(body_count, body_failures)" in src
    assert "return 0 if body_failures == 0 else 1" not in src
