"""A mailbox that returns nothing is a broken read, not a quiet week.

2026-08-14. Reads of MBD_OceanExportBookingShared had been failing from the
moment they were switched on:

    403 ErrorAccessDenied  "Access is denied. Check credentials and try again."
    404 ErrorItemNotFound  "Default folder AllItems not found."

Every failure was caught per-query and logged as a warning the run then walked
straight past. On 08-13 that was invisible, because /me was read alongside it
and supplied 27,500 messages — so the fire looked healthy and the shared
mailbox looked live. It was never live.

On 08-14, with HILMAR_READ_SHARED_ONLY on, the shared mailbox was the ONLY
target. The sweep returned 0, staged 0, printed "Nothing new to stage", and
the job went green. The tracker was blind and nothing said so.

This is the exact shape the repo has already paid for: an empty report and a
quiet day are indistinguishable downstream, and "Lonny sent nothing" read
identical to "we cannot see the mailbox" for a week in July.

So a zero-yield mailbox is now an ERROR at the point of the read — per
mailbox, because with two targets one dead mailbox is exactly what the
aggregate hides.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

SRC = (ROOT / "scripts" / "refresh_stage.py").read_text(encoding="utf-8")


def test_a_zero_yield_mailbox_is_an_error_not_a_warning():
    assert '::error::refresh_stage: mailbox' in SRC, (
        "a mailbox returning nothing must be an ERROR — a warning is what the "
        "failing shared reads already produced, and the run walked past them")


def test_the_check_is_per_mailbox():
    """With two targets, an aggregate count hides one dead mailbox entirely —
    which is precisely what happened on 2026-08-13."""
    i = SRC.index("::error::refresh_stage: mailbox")
    window = SRC[max(0, i - 700):i]
    assert "for _label, _base, _ in targets:" in window, (
        "the zero-yield check must iterate the targets, not just test the "
        "grand total")
    assert "per_src.get(_label, 0) == 0" in window


def test_per_src_is_computed_even_for_a_single_target():
    """The counter used to be built only when len(targets) > 1, which is the
    single-mailbox case this bug arrived in."""
    i = SRC.index("per_src: Counter = Counter(")
    before = SRC[max(0, i - 200):i]
    assert "if len(targets) > 1:" not in before.split("\n")[-1], (
        "per_src is still gated on having more than one target, so a single "
        "dead mailbox computes no count and raises nothing")


def test_a_totally_empty_sweep_says_the_report_is_stale():
    assert "NO mailbox returned any message" in SRC
    assert "cannot show anything that arrived since" in SRC, (
        "the operator has to be told the report reflects only previously "
        "staged mail — otherwise it reads as a real, current, quiet day")


def test_the_shared_only_switch_is_off_in_the_fire():
    """It was set true at 10:20 and the 10:41 fire swept zero. Until the
    mailbox is genuinely readable, /me is the only working source."""
    import yaml
    wf = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "daily.yml").read_text(encoding="utf-8"))
    env = wf["jobs"]["production-fire"]["env"]
    assert str(env.get("HILMAR_READ_SHARED_ONLY")).lower() == "false", (
        "reading the shared mailbox alone stages ZERO messages — it 403s/404s")
