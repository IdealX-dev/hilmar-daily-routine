"""Operator docs must not instruct anyone to use a machine that is gone.

2026-08-07. Michael: "the drift from when it worked to now [is] absurd."

He was right, and the worst instance was the worst possible place. RUNBOOK.md
— the file you open BECAUSE something is already broken — opened with:

    ## Daily fire (6:07 PM ET weekdays)
    **Trigger**: Cloud PC CPC-micha-E552L Windows Task Scheduler
    **If no emails by 6:30 PM ET**:
    1. RDP into Cloud PC via windows.cloud.microsoft

Every line of that is false. The fire moved to GitHub Actions at 8:07 AM ET in
the 2026-06 cutover and the Cloud PC was deliberately retired. The token
re-auth procedure pointed at the same dead machine, which meant the documented
recovery for an expired credential was impossible to perform.

THE DISTINCTION THIS FILE ENFORCES, because it is not "delete every mention":
  - NARRATIVE about the Cloud PC is valuable and stays. Comments explaining
    why state_store exists, why the wrapper is shaped as it is, why the
    scope list is short — that is the reasoning record, and deleting it is
    how the next person rebuilds a bad idea.
  - IMPERATIVE steps telling a reader to RDP in, open Task Scheduler, or run
    the wrapper are traps. Those must be labelled [HISTORY] or removed.

So this checks the imperative forms only, and only in the files an operator
actually opens under pressure.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

#: Files an operator reaches for. Not the whole repo — CHANGELOG and code
#: comments are narrative by nature and must stay free to describe history.
OPERATOR_DOCS = ("RUNBOOK.md", "README.md", "docs/PASSOFF.md")

#: Imperative Cloud-PC steps. Each is something a reader could TRY and fail.
TRAPS = (
    r"RDP into (the )?Cloud PC",
    r"Open Cloud PC RDP",
    r"Select `?CPC-micha-\w+`? to RDP",
    r"Double-click `?deploy.run_daily_laptop\.cmd`?",
    r"fire manually from MBD-TRAVEL",
)


def _lines(path: Path) -> list[tuple[int, str]]:
    return list(enumerate(path.read_text(encoding="utf-8").splitlines(), 1))


#: A STEP is a numbered or bulleted markdown list item. That is the whole
#: distinction this file rests on, and it took two attempts to get right: the
#: first version flagged any line CONTAINING a trap phrase, which matched the
#: prose added in this very commit explaining that the old step was removed
#: ("The old procedure here was \"Open Cloud PC RDP\"…"). Correct docs, red
#: test — the seventh time this session that an identifier in prose was
#: indistinguishable from one in code.
#:
#: Describing a dead machine is fine and often necessary. INSTRUCTING someone
#: to use one is the trap. Only a list item instructs.
_STEP = re.compile(r"^\s*(\d+\.|[-*+])\s+\S")


def _is_step(line: str) -> bool:
    return bool(_STEP.match(line))


def _is_marked_history(text: str, idx: int) -> bool:
    """A step is acceptable when its section is labelled as history."""
    lines = text.splitlines()
    if "[HISTORY" in lines[idx - 1]:
        return True
    for j in range(idx - 1, -1, -1):
        prev = lines[j]
        if prev.startswith("#"):
            return "[HISTORY" in prev
        if "[HISTORY" in prev:
            return True
    return False


@pytest.mark.parametrize("doc", OPERATOR_DOCS)
def test_no_live_instruction_points_at_the_retired_cloud_pc(doc):
    """The machine is gone. An unlabelled step telling you to log into it is
    a dead end discovered at the worst possible moment."""
    path = ROOT / doc
    if not path.exists():
        pytest.skip(f"{doc} not present")
    text = path.read_text(encoding="utf-8")
    offenders = []
    for n, line in _lines(path):
        if not _is_step(line):
            continue  # prose may describe the dead machine; steps may not use it
        for pat in TRAPS:
            if re.search(pat, line, re.I) and not _is_marked_history(text, n):
                offenders.append(f"{doc}:{n}: {line.strip()[:90]}")
    assert not offenders, (
        "live instructions point at the retired Cloud PC — label the section "
        "[HISTORY] or rewrite it for GitHub Actions:\n  " + "\n  ".join(offenders))


def test_the_runbook_states_the_real_fire_time():
    """It said 6:07 PM ET — the pre-cutover schedule — while the fire ran at
    8:07 AM. An operator waiting until 6:30 PM to worry would be ten hours
    late, which is most of a business day of missed quotes."""
    rb = (ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
    head = rb.split("## [HISTORY", 1)[0]
    assert "8:07 AM ET" in head, "the runbook does not state the real fire time"
    assert "6:07 PM ET weekdays" not in head, (
        "the runbook still leads with the pre-cutover schedule")

    daily = (ROOT / ".github" / "workflows" / "daily.yml").read_text(encoding="utf-8")
    assert 'cron: "7 12 * * 1-5"' in daily, (
        "the fire schedule changed — RUNBOOK.md's stated time is now wrong "
        "again, and this test is the only thing that will tell you")


def test_the_runbook_warns_up_front_that_the_machine_is_gone():
    """Buried caveats do not survive a 3 AM read. The first screen has to say
    it, because that is what someone skims when the report did not arrive."""
    rb = (ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
    first_screen = rb[:1600]
    assert "CLOUD PC IS GONE" in first_screen.upper()


def test_the_token_recovery_procedure_is_performable():
    """The documented fix for an expired credential was "Open Cloud PC RDP".
    With the machine retired that made the recovery path for the pipeline's
    single most critical secret impossible — and nobody would have found out
    until the day it expired."""
    rb = (ROOT / "RUNBOOK.md").read_text(encoding="utf-8")
    msal = rb.split("## Failure mode: MSAL silent token refresh failed", 1)
    assert len(msal) == 2, "the MSAL recovery section is gone"
    section = msal[1].split("\n## ", 1)[0]
    assert "auth-refresh.yml" in section or "Re-seed the Graph token" in section, (
        "the MSAL recovery section does not point at the workflow that "
        "actually performs it")
    assert (ROOT / ".github" / "workflows" / "auth-refresh.yml").exists(), (
        "RUNBOOK points at auth-refresh.yml but the workflow is missing")


def test_the_completed_cutover_plan_does_not_read_as_pending():
    """docs/MOVE-OFF-CLOUDPC.md is a finished plan written in the imperative.
    Its step 2 — "Seed state once, on the Cloud PC" — is the instruction that
    sent me looking for a machine that does not exist."""
    doc = (ROOT / "docs" / "MOVE-OFF-CLOUDPC.md").read_text(encoding="utf-8")
    assert "DONE" in doc[:900].upper(), (
        "the cutover plan does not declare itself complete, so its steps read "
        "as work to do")
