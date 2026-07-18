"""Fire-time consistency guard — the daily fire and its monitors must agree.

Root-cause regression test for the 2026-06-25 reconciliation. The fire time is
declared in several places that must encode the SAME wall-clock ET times, or
monitoring false-alerts. Before this guard, the box silently fired at 10 AM
while the Sentry monitor expected 6:07 PM — Sentry paged a false "missed
check-in" every weekday.

The schedule (Michael 2026-07-16: "monday through thursday at 8am; friday at
430pm est … no weekend emails") is now TWO fire times, no weekend:

  • MORNING  — 8:07 AM ET, Mon-Thu (reports the prior business day)
  • FRIDAY   — 4:30 PM ET, Friday (reports Friday itself; feeds the Monday
              5 AM weekly)

Surfaces checked (parsed textually — no project imports, no sentry_sdk dep):
  1. Cloud-PC Task Scheduler triggers — deploy/setup_cloudpc.ps1  (both -At)
  2. Sentry cron monitor             — scripts/sentry_setup.py    (Mon-Thu 8 AM;
     Friday is intentionally covered by liveness, not this single-crontab monitor)
  3. GitHub schedule                 — .github/workflows/daily.yml (2 DST crons
     per fire time)
  4. Liveness backstops              — .github/workflows/liveness.yml (each tick
     must run AFTER its day's fire)

To change a fire time, update all surfaces AND the constants below.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# The canonical fire times, in Eastern Time.
MORNING_ET = (8, 7)     # 8:07 AM ET, Mon-Thu (dow 1-4)
FRIDAY_ET = (16, 30)    # 4:30 PM ET, Friday (dow 5)

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


# --- Surface 1: Cloud-PC Task Scheduler triggers ----------------------------

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


def test_cloud_pc_has_both_fire_triggers():
    triggers = _ps_triggers_hm()
    assert MORNING_ET in triggers, (
        f"Cloud-PC is missing the Mon-Thu {MORNING_ET[0]:02d}:{MORNING_ET[1]:02d} "
        f"ET trigger; found {sorted(triggers)}"
    )
    assert FRIDAY_ET in triggers, (
        f"Cloud-PC is missing the Friday {FRIDAY_ET[0]:02d}:{FRIDAY_ET[1]:02d} "
        f"ET trigger; found {sorted(triggers)}"
    )


# --- Surface 2: Sentry cron monitor (Mon-Thu 8 AM) --------------------------

def _sentry_cron_and_tz() -> tuple[int, int, str, str]:
    text = _read("scripts/sentry_setup.py")
    m = re.search(r'"type":\s*"crontab",\s*"value":\s*"([^"]+)"', text)
    assert m, "no crontab schedule value found in sentry_setup.py _MONITOR_CONFIG"
    fields = m.group(1).split()
    minute, hour, dow = int(fields[0]), int(fields[1]), fields[4]
    tzm = re.search(r'"timezone":\s*"([^"]+)"', text)
    assert tzm, "no timezone found in sentry_setup.py _MONITOR_CONFIG"
    return minute, hour, dow, tzm.group(1)


def test_sentry_monitor_is_the_morning_fire_mon_thu():
    minute, hour, dow, tz = _sentry_cron_and_tz()
    assert tz == "America/New_York", f"Sentry monitor tz must be America/New_York, got {tz!r}"
    assert (hour, minute) == MORNING_ET, (
        "Sentry cron monitor must match the morning fire "
        f"{MORNING_ET[0]:02d}:{MORNING_ET[1]:02d} ET; got {hour:02d}:{minute:02d}."
    )
    assert dow == "1-4", (
        f"Sentry monitor must be Mon-Thu (dow 1-4); Friday is covered by "
        f"liveness, not the cron monitor. Got dow={dow!r}."
    )


# --- Surface 3: GitHub daily.yml schedule (UTC, both DST seasons) ------------

def test_daily_yml_crons_encode_both_fire_times_both_seasons():
    crons = _crons(_read(".github/workflows/daily.yml"))
    # Morning: Mon-Thu (dow 1-4) at the UTC hours of 8:07 AM ET.
    morning = {(mi, hr) for (mi, hr, dow) in crons if dow == "1-4"}
    assert morning, "no Mon-Thu (dow 1-4) cron found in daily.yml"
    assert all(mi == MORNING_ET[1] for mi, _ in morning), (
        f"morning crons must fire at minute :{MORNING_ET[1]:02d}, got {sorted(morning)}"
    )
    assert {hr for _, hr in morning} == {
        _utc_hour(MORNING_ET[0], EDT_OFFSET), _utc_hour(MORNING_ET[0], EST_OFFSET)
    }, f"morning UTC hours {sorted(h for _, h in morning)} don't encode 8 AM ET both seasons"

    # Friday: dow 5 at the UTC hours of 4:30 PM ET.
    friday = {(mi, hr) for (mi, hr, dow) in crons if dow == "5"}
    assert friday, "no Friday (dow 5) cron found in daily.yml"
    assert all(mi == FRIDAY_ET[1] for mi, _ in friday), (
        f"Friday crons must fire at minute :{FRIDAY_ET[1]:02d}, got {sorted(friday)}"
    )
    assert {hr for _, hr in friday} == {
        _utc_hour(FRIDAY_ET[0], EDT_OFFSET), _utc_hour(FRIDAY_ET[0], EST_OFFSET)
    }, f"Friday UTC hours {sorted(h for _, h in friday)} don't encode 4:30 PM ET both seasons"


# --- Cross-surface: the box morning trigger == the Sentry monitor -----------

def test_cloud_pc_morning_trigger_matches_sentry_monitor():
    s_min, s_hour, _, _ = _sentry_cron_and_tz()
    assert MORNING_ET in _ps_triggers_hm(), "Cloud-PC morning trigger missing"
    assert (s_hour, s_min) == MORNING_ET, (
        f"Sentry monitor {(s_hour, s_min)} must equal the morning fire {MORNING_ET} "
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


# --- Surface 4: liveness backstops must run AFTER each day's fire ------------

def test_liveness_backstops_run_after_each_fire():
    crons = _crons(_read(".github/workflows/liveness.yml"))
    # Mon-Thu backstops must be after the morning fire (both DST encodings).
    morning_fire_utc = max(
        _utc_hour(MORNING_ET[0], EDT_OFFSET), _utc_hour(MORNING_ET[0], EST_OFFSET)
    )
    mon_thu = [hr for (_, hr, dow) in crons if dow == "1-4"]
    assert mon_thu, "no Mon-Thu liveness cron found"
    for hr in mon_thu:
        assert hr >= morning_fire_utc, (
            f"Mon-Thu liveness cron at {hr}:00 UTC races the morning fire "
            f"({morning_fire_utc}:00 UTC latest); move it later."
        )
    # Friday backstop must be after the 4:30 PM fire (both DST encodings).
    friday_fire_utc = max(
        _utc_hour(FRIDAY_ET[0], EDT_OFFSET), _utc_hour(FRIDAY_ET[0], EST_OFFSET)
    )
    fri = [hr for (_, hr, dow) in crons if dow == "5"]
    assert fri, "no Friday liveness cron found"
    for hr in fri:
        assert hr >= friday_fire_utc, (
            f"Friday liveness cron at {hr}:00 UTC races the 4:30 PM fire "
            f"({friday_fire_utc}:00 UTC latest); move it later."
        )
