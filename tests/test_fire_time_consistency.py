"""Fire-time consistency guard — the daily fire and its monitors must agree.

Root-cause regression test for the 2026-06-25 reconciliation. The daily fire
time is declared in FOUR places that have to encode the SAME wall-clock ET
time, or monitoring false-alerts:

  1. Cloud-PC Task Scheduler trigger  — deploy/setup_cloudpc.ps1   (`-At 6:07pm`)
  2. Sentry cron monitor              — scripts/sentry_setup.py    (`7 18 * * 1-5`, tz America/New_York)
  3. GitHub schedule                  — .github/workflows/daily.yml (`7 22`/`7 23 * * 1-5`, the two DST-season UTC crons)
  4. Liveness backstops               — .github/workflows/liveness.yml (must run AFTER the fire)

Before this guard, the box silently fired at 10 AM while the Sentry monitor
expected 6:07 PM — so Sentry paged a false "missed check-in" every weekday.
This test parses the files textually (no project imports, no sentry_sdk
dependency) and fails CI if surfaces 1–3 disagree, or if a liveness tick
would race the fire.

To change the fire time, update all four surfaces AND `FIRE_ET_HOUR` /
`FIRE_ET_MINUTE` below — the test makes that a single, deliberate edit.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# The one canonical fire time, in Eastern Time.
FIRE_ET_HOUR = 18      # 6 PM ET
FIRE_ET_MINUTE = 7     # :07 — an off-:00 minute, matched by the box + the monitor

# ET → UTC offsets by DST season (ET is behind UTC).
EDT_OFFSET = 4         # Mar–Nov: ET = UTC-4
EST_OFFSET = 5         # Nov–Mar: ET = UTC-5


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


# --- Surface 1: Cloud-PC Task Scheduler trigger -----------------------------

def _ps_trigger_hm() -> tuple[int, int]:
    text = _read("deploy/setup_cloudpc.ps1")
    m = re.search(r"-At\s+(\d{1,2}):(\d{2})\s*([AaPp][Mm])", text)
    assert m, "no `-At H:MM(am|pm)` trigger found in setup_cloudpc.ps1"
    hour, minute, ampm = int(m.group(1)), int(m.group(2)), m.group(3).lower()
    if ampm == "pm" and hour != 12:
        hour += 12
    elif ampm == "am" and hour == 12:
        hour = 0
    return hour, minute


def test_cloud_pc_trigger_is_fire_time():
    assert _ps_trigger_hm() == (FIRE_ET_HOUR, FIRE_ET_MINUTE), (
        "Cloud-PC Task Scheduler trigger (setup_cloudpc.ps1 -At) does not match "
        f"the canonical {FIRE_ET_HOUR:02d}:{FIRE_ET_MINUTE:02d} ET fire time."
    )


# --- Surface 2: Sentry cron monitor -----------------------------------------

def _sentry_cron_and_tz() -> tuple[int, int, str]:
    text = _read("scripts/sentry_setup.py")
    m = re.search(r'"type":\s*"crontab",\s*"value":\s*"([^"]+)"', text)
    assert m, "no crontab schedule value found in sentry_setup.py _MONITOR_CONFIG"
    minute, hour = (int(x) for x in m.group(1).split()[:2])
    tzm = re.search(r'"timezone":\s*"([^"]+)"', text)
    assert tzm, "no timezone found in sentry_setup.py _MONITOR_CONFIG"
    return minute, hour, tzm.group(1)


def test_sentry_monitor_is_fire_time_in_eastern():
    minute, hour, tz = _sentry_cron_and_tz()
    assert tz == "America/New_York", f"Sentry monitor tz must be America/New_York, got {tz!r}"
    assert (hour, minute) == (FIRE_ET_HOUR, FIRE_ET_MINUTE), (
        "Sentry cron monitor schedule does not match the canonical "
        f"{FIRE_ET_HOUR:02d}:{FIRE_ET_MINUTE:02d} ET fire time."
    )


# --- Surface 3: GitHub daily.yml schedule (UTC, both DST seasons) ------------

def test_daily_yml_crons_map_to_fire_time_both_seasons():
    weekday = [(mi, hr) for (mi, hr, dow) in _crons(_read(".github/workflows/daily.yml")) if dow == "1-5"]
    assert weekday, "no weekday (`* * 1-5`) cron found in daily.yml schedule"
    # Every minute must be the canonical fire minute.
    assert all(mi == FIRE_ET_MINUTE for mi, _ in weekday), (
        f"daily.yml crons must all fire at minute :{FIRE_ET_MINUTE:02d}, got {sorted(weekday)}"
    )
    # The two UTC hours must be the EDT and EST encodings of the same ET hour.
    utc_hours = {hr for _, hr in weekday}
    expected = {(FIRE_ET_HOUR + EDT_OFFSET) % 24, (FIRE_ET_HOUR + EST_OFFSET) % 24}
    assert utc_hours == expected, (
        f"daily.yml UTC cron hours {utc_hours} do not encode {FIRE_ET_HOUR}:00 ET "
        f"across both DST seasons (expected {expected} = ET+4 and ET+5)."
    )


# --- Cross-surface: the box, the monitor, and GitHub all agree --------------

def test_all_surfaces_agree_on_one_fire_time():
    ps_hm = _ps_trigger_hm()
    s_min, s_hour, _ = _sentry_cron_and_tz()
    assert ps_hm == (s_hour, s_min), (
        f"Cloud-PC trigger {ps_hm} != Sentry monitor {(s_hour, s_min)} (ET). "
        "The box's check-in must land in the monitor's expected slot."
    )


# --- Surface 4: liveness backstops must run AFTER the fire, not race it ------

def test_liveness_backstops_run_after_the_fire():
    weekday_utc_hours = [
        hr for (_, hr, dow) in _crons(_read(".github/workflows/liveness.yml"))
        # liveness ticks span into the next UTC day (dow 2-6); both seasons count.
        if dow in ("1-5", "2-6")
    ]
    assert weekday_utc_hours, "no weekday liveness cron found"
    fire_utc_edt = (FIRE_ET_HOUR + EDT_OFFSET) % 24  # 22 for 6 PM ET
    # Every liveness tick must be at or after the EDT fire hour on the same UTC
    # day, OR in the early-UTC-morning next-day window (hour < fire) — i.e. it
    # must never land in the pre-fire afternoon-UTC window that would dispatch a
    # duplicate before the scheduled fire has had its chance.
    for hr in weekday_utc_hours:
        in_evening = hr >= fire_utc_edt          # same-day, after the fire
        in_next_day_early = hr < EDT_OFFSET      # wee hours UTC = previous ET evening/night
        assert in_evening or in_next_day_early, (
            f"liveness cron at {hr}:00 UTC could fire BEFORE the {FIRE_ET_HOUR}:00 ET "
            "scheduled fire and dispatch a duplicate; move it later."
        )
