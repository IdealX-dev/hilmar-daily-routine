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
import re
from pathlib import Path

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


def test_triggers_are_the_canonical_fire_times():
    # 2026-07-21: ONE trigger, no weekend — a single fire Mon-Fri 6:30 AM ET that
    # reports the prior business day. No wrap-up. See
    # tests/test_fire_time_consistency.py for the cross-surface guard.
    assert re.search(
        r"New-ScheduledTaskTrigger[^\n]*-DaysOfWeek\s+Monday,Tuesday,Wednesday,Thursday,Friday[^\n]*-At\s+6:30am",
        PS1,
    ), "missing the Mon-Fri 6:30am fire trigger"
    assert "4:30pm" not in PS1, "the Friday 4:30pm wrap-up trigger is retired — remove it"


def test_execution_time_limit_exceeds_worst_case_pipeline():
    """Audit finding [33]: the old 15-min cap could SIGTERM the run mid-fire
    (the pipeline's per-step timeouts alone sum to ~25 min worst case). The
    limit must comfortably exceed that, matching daily.yml's 50-min budget."""
    m = re.search(r"-ExecutionTimeLimit\s*\(New-TimeSpan\s+-Minutes\s+(\d+)\)", PS1)
    assert m, "setup_cloudpc.ps1 must set an explicit -ExecutionTimeLimit"
    assert int(m.group(1)) >= 40, (
        f"ExecutionTimeLimit is {m.group(1)} min -- too short for the ~25-min "
        "worst-case pipeline + send/audit chain; must be >=40 (daily.yml uses 50)."
    )


def test_no_auto_restart_of_the_email_wrapper():
    """Audit finding [33]: auto-restart is unsafe for an email-sending wrapper.
    A restart after a mid-send kill re-runs outlook_send before the sent-flag
    exists and double-mails all 10 recipients. RestartCount must be 0, and
    RestartInterval (which only applies when RestartCount>0) must be gone."""
    m = re.search(r"-RestartCount\s+(\d+)", PS1)
    assert m, "expected an explicit -RestartCount"
    assert int(m.group(1)) == 0, (
        f"-RestartCount is {m.group(1)}; must be 0 -- restarting an email wrapper "
        "mid-send risks a duplicate client email."
    )
    assert "-RestartInterval" not in PS1, (
        "-RestartInterval must be removed when RestartCount is 0 (it only applies "
        "when RestartCount > 0)."
    )
