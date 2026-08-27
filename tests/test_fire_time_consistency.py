"""Fire-time consistency guard — the daily fire and its monitors must agree.

Root-cause regression test for the 2026-06-25 reconciliation. The fire time is
declared in several places that must encode the SAME wall-clock ET time, or
monitoring false-alerts. Before this guard, the box silently fired at 10 AM
while the Sentry monitor expected 6:07 PM — Sentry paged a false "missed
check-in" every weekday.

The schedule (Michael 2026-07-21: "get rid of the recaps and just do daily at
8am est for the day before") is now ONE fire, no wrap-up, no weekend:

  • MORNING — 6:30 AM ET, Mon-Fri (reports the PRIOR business day; Mon→Fri,
             Tue→Mon, Wed→Tue, Thu→Wed, Fri→Thu)

Surfaces checked (parsed textually — no project imports, no sentry_sdk dep):
  1. Cloud-PC Task Scheduler trigger — deploy/setup_cloudpc.ps1  (-At)
  2. Sentry cron monitor            — scripts/sentry_setup.py    (Mon-Fri 8 AM)
  3. GitHub schedule                — .github/workflows/daily.yml (2 DST crons)
  4. Liveness backstops             — .github/workflows/liveness.yml (each tick
     must run AFTER the fire)

To change the fire time, update all surfaces AND the constant below.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# The canonical fire time, in Eastern Time. ONE fire, Mon-Fri (dow 1-5).
MORNING_ET = (6, 30)    # 6:30 AM ET, Mon-Fri (was 8:07 until 2026-08-27)
FRIDAY_WRAPUP = (16, 30)  # the RETIRED 4:30 PM wrap-up — must NOT appear anywhere

# ET → UTC offsets by DST season (ET is behind UTC).
EDT_OFFSET = 4          # Mar–Nov: ET = UTC-4
EST_OFFSET = 5          # Nov–Mar: ET = UTC-5


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _crons(text: str) -> list[tuple[int, int, str]]:
    """Extract (minute, hour, dow) from every `cron: "..."` 5-field entry."""
    out = []
    for raw in re.findall(r"""cron:\s*["']([^"']+)["']""", text):
        fields = raw.split()
        if len(fields) == 5 and fields[0].isdigit() and fields[1].isdigit():
            out.append((int(fields[0]), int(fields[1]), fields[4]))
    return out


def _utc_hour(et_hour: int, offset: int) -> int:
    return (et_hour + offset) % 24


# --- Surface 1: Cloud-PC Task Scheduler trigger -----------------------------

def _ps_triggers_hm() -> set[tuple[int, int]]:
    """Every `-At H:MM(am|pm)` trigger in setup_cloudpc.ps1, as 24h (hour, min)."""
    text = _read("deploy/setup_cloudpc.ps1")
    out = set()
    for h, m, ampm in re.findall(r"-At\s+(\d{1,2}):(\d{2})\s*([AaPp][Mm])", text):
        hour, minute, ampm = int(h), int(m), ampm.lower()
        if ampm == "pm" and hour != 12:
            hour += 12
        elif ampm == "am" and hour == 12:
            hour = 0
        out.add((hour, minute))
    return out


def test_cloud_pc_has_only_the_morning_fire_trigger():
    triggers = _ps_triggers_hm()
    assert MORNING_ET in triggers, (
        f"Cloud-PC is missing the Mon-Fri {MORNING_ET[0]:02d}:{MORNING_ET[1]:02d} "
        f"ET trigger; found {sorted(triggers)}"
    )
    assert FRIDAY_WRAPUP not in triggers, (
        "the Friday 4:30 PM wrap-up trigger is retired — it must not be on the box"
    )


# --- Surface 2: Sentry cron monitor (Mon-Fri 8 AM) --------------------------

def _sentry_cron_and_tz() -> tuple[int, int, str, str]:
    text = _read("scripts/sentry_setup.py")
    m = re.search(r'"type":\s*"crontab",\s*"value":\s*"([^"]+)"', text)
    assert m, "no crontab schedule value found in sentry_setup.py _MONITOR_CONFIG"
    fields = m.group(1).split()
    minute, hour, dow = int(fields[0]), int(fields[1]), fields[4]
    tzm = re.search(r'"timezone":\s*"([^"]+)"', text)
    assert tzm, "no timezone found in sentry_setup.py _MONITOR_CONFIG"
    return minute, hour, dow, tzm.group(1)


def test_sentry_monitor_is_the_morning_fire_mon_fri():
    minute, hour, dow, tz = _sentry_cron_and_tz()
    assert tz == "America/New_York", f"Sentry monitor tz must be America/New_York, got {tz!r}"
    assert (hour, minute) == MORNING_ET, (
        "Sentry cron monitor must match the morning fire "
        f"{MORNING_ET[0]:02d}:{MORNING_ET[1]:02d} ET; got {hour:02d}:{minute:02d}."
    )
    assert dow == "1-5", (
        f"Sentry monitor must be Mon-Fri (dow 1-5) — the daily fire runs every "
        f"weekday morning. Got dow={dow!r}."
    )


# --- Surface 3: GitHub daily.yml schedule (UTC, both DST seasons) ------------

def test_daily_yml_encodes_the_morning_fire_both_seasons_and_no_wrapup():
    crons = _crons(_read(".github/workflows/daily.yml"))
    # Morning: Mon-Fri (dow 1-5) at the UTC hours of 8:07 AM ET, both seasons.
    morning = {(mi, hr) for (mi, hr, dow) in crons if dow == "1-5"}
    assert morning, "no Mon-Fri (dow 1-5) cron found in daily.yml"
    assert all(mi == MORNING_ET[1] for mi, _ in morning), (
        f"morning crons must fire at minute :{MORNING_ET[1]:02d}, got {sorted(morning)}"
    )
    assert {hr for _, hr in morning} == {
        _utc_hour(MORNING_ET[0], EDT_OFFSET), _utc_hour(MORNING_ET[0], EST_OFFSET)
    }, f"morning UTC hours {sorted(h for _, h in morning)} don't encode 8 AM ET both seasons"

    # The retired Friday wrap-up (dow 5) must be GONE.
    friday = [(mi, hr) for (mi, hr, dow) in crons if dow == "5"]
    assert not friday, f"Friday wrap-up crons must be removed; found {sorted(friday)}"


# --- Cross-surface: the box trigger == the Sentry monitor -------------------

def test_cloud_pc_trigger_matches_sentry_monitor():
    s_min, s_hour, _, _ = _sentry_cron_and_tz()
    assert MORNING_ET in _ps_triggers_hm(), "Cloud-PC morning trigger missing"
    assert (s_hour, s_min) == MORNING_ET, (
        f"Sentry monitor {(s_hour, s_min)} must equal the fire {MORNING_ET} "
        "so the box's check-in lands in the monitor's expected slot."
    )


# --- Weekly exec summary: Monday 5:07 AM ET, internal-only ------------------

WEEKLY_ET = (5, 7)   # 5:07 AM ET, Monday (dow 1)


def test_weekly_yml_fires_monday_5am_et_both_seasons():
    crons = _crons(_read(".github/workflows/weekly.yml"))
    monday = {(mi, hr) for (mi, hr, dow) in crons if dow == "1"}
    assert monday, "no Monday (dow 1) cron in weekly.yml"
    assert all(mi == WEEKLY_ET[1] for mi, _ in monday), (
        f"weekly crons must fire at minute :{WEEKLY_ET[1]:02d}, got {sorted(monday)}"
    )
    assert {hr for _, hr in monday} == {
        _utc_hour(WEEKLY_ET[0], EDT_OFFSET), _utc_hour(WEEKLY_ET[0], EST_OFFSET)
    }, f"weekly UTC hours {sorted(h for _, h in monday)} don't encode 5 AM ET both seasons"


def test_weekly_summary_never_targets_the_client():
    """The exec summary is internal analytics — it must go to the staff list
    (--to-from-config) or Michael only, NEVER a client recipient (QC-065
    boundary)."""
    text = _read(".github/workflows/weekly.yml")
    assert "--to-from-config" in text, "weekly must send to the staff distribution"
    assert "lupfold" not in text and "client-email" not in text and "client_report" not in text, (
        "weekly.yml must never reference client recipients or the client email"
    )


# --- Surface 4: liveness backstops must run AFTER the fire -------------------

def test_liveness_backstops_run_after_the_fire_and_no_weekend():
    crons = _crons(_read(".github/workflows/liveness.yml"))
    morning_fire_utc = max(
        _utc_hour(MORNING_ET[0], EDT_OFFSET), _utc_hour(MORNING_ET[0], EST_OFFSET)
    )
    mon_fri = [hr for (_, hr, dow) in crons if dow == "1-5"]
    assert mon_fri, "no Mon-Fri liveness cron found"
    for hr in mon_fri:
        assert hr >= morning_fire_utc, (
            f"Mon-Fri liveness cron at {hr}:00 UTC races the morning fire "
            f"({morning_fire_utc}:00 UTC latest); move it later."
        )
    # No Friday-only (dow 5) evening backstop — the wrap-up is retired.
    fri = [hr for (_, hr, dow) in crons if dow == "5"]
    assert not fri, f"Friday-evening liveness backstop must be removed; found {fri}"
