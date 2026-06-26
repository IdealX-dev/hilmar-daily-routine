"""The Cloud-PC daily task must be registered to survive a logged-off session.

Root-cause guard for the 2026-06 ~2-week SILENT miss. The daily task was
registered INTERACTIVE ("run only when the user is logged on"), so once the RDP
session stopped staying logged on (~June 11) Task Scheduler skipped 10 straight
fires while still reporting LastTaskResult 0 -- and the reports went out by hand
each morning, masking the outage. QC/self-heal/the env-drift sentinel could not
catch it: they all run *inside* a fire, and a task that never fires runs no code
to check itself.

This test locks `setup_cloudpc.ps1`'s task registration to the config that
survived the incident, so that fragile shape can never silently return via a
later edit:
  - an **S4U principal** -> runs whether or not the user is logged on
  - `-StartWhenAvailable` + `-WakeToRun` -> catch up / wake a sleeping box
  - the Register call actually *passes* the principal (S4U is dead config if not)
  - the trigger is the canonical 6:07 PM evening fire

It does NOT prove the box fires at runtime -- a task that never runs cannot
self-report; catching a true no-fire is liveness.yml's job (GitHub-side,
independent of the box). This guard guarantees the *configuration the operator
deploys* is the resilient one.
"""
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parent.parent
PS1 = (ROOT / "deploy" / "setup_cloudpc.ps1").read_text(encoding="utf-8")
REGISTER_CALLS = re.findall(r"Register-ScheduledTask[^\n]*", PS1)


def test_creates_an_s4u_principal():
    assert re.search(r"New-ScheduledTaskPrincipal[^\n]*-LogonType\s+S4U", PS1), (
        "setup_cloudpc.ps1 must create an S4U principal so the fire runs whether "
        "or not the user is logged on (the interactive default caused the 2026-06 "
        "silent miss)."
    )


def test_register_call_passes_the_principal():
    assert REGISTER_CALLS, "expected at least one Register-ScheduledTask call"
    assert any(re.search(r"-Principal\s+\$principal", c) for c in REGISTER_CALLS), (
        "Register-ScheduledTask must pass -Principal $principal, or the S4U "
        "principal is dead config and the task falls back to interactive."
    )


def test_no_register_call_omits_the_principal():
    # Guard against a refactor that re-introduces an interactive registration
    # (the exact 2026-06 regression shape). EVERY Register call must be S4U.
    for c in REGISTER_CALLS:
        assert "-Principal" in c, (
            "every Register-ScheduledTask must pass -Principal -- an interactive "
            f"(principal-less) registration is what silently missed 10 fires: {c}"
        )


def test_settings_are_resilient():
    for flag in ("-StartWhenAvailable", "-WakeToRun"):
        assert flag in PS1, f"task settings must include {flag} (sleep/missed-run resilience)"


def test_trigger_is_the_canonical_evening_fire():
    assert re.search(r"New-ScheduledTaskTrigger[^\n]*-At\s+6:07pm", PS1), (
        "the task trigger must be -At 6:07pm (the canonical evening fire time; "
        "see tests/test_fire_time_consistency.py)."
    )
