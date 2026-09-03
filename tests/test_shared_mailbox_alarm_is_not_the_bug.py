"""The shared mailbox's date-sweep failure was mistaken for an open bug —
by this repo's own next session, on 2026-09-03.

CHANGELOG, 2026-08-14, "THE SHARED MAILBOX IS CLOSED, PERMANENTLY": Full
Access was the only route to a folder store behind
`MBD_OceanExportBookingShared`, Graph 404s every endpoint without it, and
Michael said "ol won't grant more access." `refresh_stage.SHARED_MAILBOX`'s
own comment says the same and adds why it costs nothing: Michael is on the
ops distribution, so Hilmar mail reaches `/me` anyway.

None of that stopped a fresh read of one `::error::` line —

    ::error::refresh_stage: [MBD_OceanExportBookingShared@ol-usa.com] date
    sweep FAILED: ... — falling back to $search only, which is known to
    drop recent mail

— from being reported as active, unrecovered data loss, and a whole
folder-enumeration mechanism being designed, reviewed, fixed, and then
deleted before it shipped, once `diag_shared_mailbox.py` was actually run
and showed every endpoint 404ing, not just the one being replaced.

The bug was never in the code. It was that a KNOWN, PERMANENT, unfixable
condition was printed exactly like a NEW one, every single fire, forever.
`::error::` on a dead end that cannot be fixed trains the next reader — a
future session, or this one on a later day — to go looking for a fix that
cannot exist.

So: the known-dead signature on the known-dead mailbox gets a plain line
that says so and points at the history. Anything else — a different
mailbox, or this one failing a NEW way — still has to be loud, because that
IS an open question and someone should look.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = (ROOT / "scripts" / "refresh_stage.py").read_text(encoding="utf-8")


def _sweep_except_block() -> str:
    i = SRC.index("for mbox, base, mtoken in targets:")
    j = SRC.index("_swept_total = len(all_items)")
    assert i < j
    return SRC[i:j]


def _if_and_else_branches(block: str) -> tuple[str, str]:
    """Split the except-block on its `if mbox == SHARED_MAILBOX...` /
    `else:` structure so each branch can be checked independently — the
    whole point being that ONLY the known-dead branch may be quiet."""
    i = block.index('if mbox == SHARED_MAILBOX and "AllItems" in str(e):')
    else_i = block.index("else:", i)
    continue_i = block.index("continue", else_i)
    return block[i:else_i], block[else_i:continue_i]


def test_the_known_dead_signature_is_quiet_not_an_error():
    block = _sweep_except_block()
    assert 'mbox == SHARED_MAILBOX and "AllItems" in str(e)' in block, (
        "the known-dead branch must key on BOTH the mailbox and the exact "
        "AllItems signature — the discriminator that separates 'nothing can "
        "be done' from 'something just broke'")
    if_branch, _else_branch = _if_and_else_branches(block)
    assert "::error::" not in if_branch, (
        "the known, permanent, unfixable condition is still shouting — "
        "exactly what sent a fresh session chasing a fix that cannot exist")
    assert "known" in if_branch.lower()


def test_the_quiet_line_points_at_the_history_not_just_the_symptom():
    """A quiet line that only says 'skipped' is worse than a loud one — it
    gives nobody anywhere to go. It has to name where the story lives."""
    block = _sweep_except_block()
    if_branch, _ = _if_and_else_branches(block)
    assert "SHARED_MAILBOX" in if_branch


def test_any_other_failure_still_cries_out():
    """THE DISCRIMINATOR, proven the other direction. A different mailbox, or
    this one failing a way that is NOT the confirmed-dead signature, must
    keep the original loud path — this is still an open question."""
    block = _sweep_except_block()
    _if_branch, else_branch = _if_and_else_branches(block)
    assert "::error::" in else_branch
    assert "date sweep FAILED" in else_branch


def test_the_quiet_branch_still_falls_back_to_search_like_the_loud_one_did():
    """Only the ANNOUNCEMENT changes. The known-dead mailbox still gets a
    $search attempt for it (queries run per-target below, unconditionally),
    and the loop still moves to the next target — `continue`, not `return`,
    not `raise`."""
    block = _sweep_except_block()
    i = block.index('mbox == SHARED_MAILBOX and "AllItems" in str(e)')
    after = block[i:]
    assert "continue" in after
    # Exactly one `continue` serves both branches (shared indentation below
    # the if/else), not a second one hidden inside the quiet branch that
    # would skip something the loud branch still does.
    assert after.count("continue") == 1
