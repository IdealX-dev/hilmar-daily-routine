"""The turnaround clock is back on, and the mechanism to stop it survives.

Michael 2026-08-13, once the shared mailbox came online: "turnaround clock
should be fine now that you see the shard box yourself".

MEASURED BEFORE FLIPPING (diag-blob 31736160870, stored state): of 288 rows
carrying both a request and a response time, ZERO have a response before the
ask, and 8 (2.8%) exceed 30 days. Those 8 are April asks paired to June/July
replies — Lonny re-using an Outlook thread — and QC-021 already clears
turnaround above 40 biz-hours, so they never reach an average.

WHAT THIS FILE PROTECTS. core.TIMING_VALID_FROM is retired to "" rather than
deleted, because the falsy branch is what makes it a one-line switch in BOTH
directions. If someone later deletes the constant, or a renderer starts
reading it unguarded, stopping the clock again means rebuilding the mechanism
under pressure — which is exactly the situation it was built in.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import core  # noqa: E402
import gen_email as GE  # noqa: E402


def test_the_clock_is_on():
    assert core.TIMING_VALID_FROM == ""


def test_every_sample_counts_while_the_clock_is_on():
    """timing_is_valid must not exclude history now that the floor is gone."""
    for ts in ("2026-04-02T21:04:28Z", "2026-06-19T13:25:59Z",
               "2026-08-13T20:57:02Z", None, ""):
        assert core.timing_is_valid(ts) is True, ts


def test_the_reset_banner_is_gone_everywhere():
    assert core.timing_reset_note() == ""
    assert core.timing_reset_note(short=True) == ""
    assert GE._timing_reset_banner(288, 0) == ""


def test_the_switch_still_works_in_the_other_direction(monkeypatch):
    """Setting a date must re-arm the whole mechanism — that is the reason the
    constant is emptied rather than deleted."""
    monkeypatch.setattr(core, "TIMING_VALID_FROM", "2026-08-13")
    assert core.timing_is_valid("2026-04-02T21:04:28Z") is False
    assert core.timing_is_valid("2026-08-13T20:57:02Z") is True
    assert core.timing_reset_note() != ""
    assert core.timing_reset_note(short=True) != ""


def test_the_summary_no_longer_advertises_a_floor():
    out = core.aggregate_summary([])
    assert out.get("turnaround_valid_from") in ("", None)
