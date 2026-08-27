"""No scheduled fire may land before 6:00 AM ET.

Michael, 2026-08-27: "blast did not go out today", then "move cron earlier".
It had gone out — GitHub dropped the 12:07 UTC tick and started the run at
15:54 UTC, 3h47m late, so the email landed at 12:12 PM ET instead of ~8 AM.
Moving the cron earlier buys slack against that slip. Fine.

EXCEPT THERE IS A CLIFF AT 6 AM AND IT IS SILENT.

`core.report_business_day` applies a wee-hours rule:

    if hasattr(now_et, "hour") and now_et.hour < 6:
        today = today - timedelta(days=1)

Combined with `window=previous`, a fire before 6:00 AM ET reports the day
BEFORE the one it should. Measured before the move:

    05:07 ET Thu -> business day 2026-08-26, reports Tue 08-25   WRONG
    06:07 ET Thu -> business day 2026-08-27, reports Wed 08-26   right

A 5 AM cron would therefore skip one business day, every single day, with
nothing red anywhere — the report would simply always be one day stale and
Wednesday would never be reported at all. The rule exists for a good reason
(a 12:38 AM dispatch once reported an all-zero "Thu" and poisoned Thursday's
send flag, live failure run #76); it is only dangerous when a SCHEDULED fire
is put underneath it.

This test is the guard rail. It reads the crons out of the workflows and
fails if any of them converts to an ET wall-clock before the cutoff, in
either DST season.
"""
from __future__ import annotations

import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import core  # noqa: E402

ET = ZoneInfo("America/New_York")

#: The hour core.report_business_day rolls back below. Read from the source
#: rather than hardcoded, so moving the rule moves the guard with it.
CORE_SRC = (ROOT / "scripts" / "core.py").read_text(encoding="utf-8")


def _cutoff_hour() -> int:
    m = re.search(r'hasattr\(now_et, "hour"\) and now_et\.hour < (\d+)', CORE_SRC)
    assert m, (
        "could not find the wee-hours rule in core.report_business_day — if "
        "it moved, this guard must move with it, not silently pass")
    return int(m.group(1))


def _sending_workflows():
    """Workflows whose report day comes from core.report_business_day, and
    which are therefore subject to its wee-hours rollback.

    weekly.yml is DELIBERATELY EXEMPT and must stay out of this list. It
    derives its day from gen_weekly_summary._fire_day_et, which documents
    the exemption in its own docstring: "the weekly runs MONDAY ~5 AM ET
    (for the previous week). Unlike the evening daily fire, there is NO
    wee-hours rollback — 5 AM Monday IS Monday." Its 5:07 AM cron is correct
    for that rule, and this guard's first version failed it — which is why
    the exemption is asserted below rather than assumed.
    """
    wf = ROOT / ".github" / "workflows"
    return [wf / "daily.yml"]


def _crons(path: Path):
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8")
    # Only inside the `schedule:` block, and only uncommented lines.
    out = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        m = re.match(r'-\s*cron:\s*"([^"]+)"', stripped)
        if m:
            out.append(m.group(1))
    return out


#: A summer date (EDT, UTC-4) and a winter one (EST, UTC-5).
_EDT_DAY, _EST_DAY = date(2026, 7, 1), date(2026, 1, 15)


def _et_at(cron: str, on: date):
    """The ET wall-clock this UTC cron lands at on a given date, or None.

    Build a fresh UTC datetime per season and convert. Do NOT take one
    result and `.replace(month=...)` onto another date — ZoneInfo resolves
    the offset from the date, so replacing turns a 05:30 EST into a 05:30
    EDT and silently reports the wrong wall clock. That mistake is what this
    helper's first version made, and the suite caught it.
    """
    parts = cron.split()
    minute, hour = parts[0], parts[1]
    if not hour.isdigit() or not minute.isdigit():
        return None        # */N or a range — not a fixed daily fire
    utc = datetime(on.year, on.month, on.day,
                   int(hour), int(minute), tzinfo=ZoneInfo("UTC"))
    return utc.astimezone(ET)


def _seasonal_pairs(crons):
    """Match each cron to the season it is MEANT to fire in.

    The repo ships one cron per DST encoding and the schedule gate proceeds
    on exactly the one matching the current ET offset, so the EST-season
    member is deliberately an hour "wrong" in summer and vice versa. Judging
    every cron in both seasons would fail the healthy pair. Convention in
    these files: within a pair, the lower UTC hour is the EDT one.
    """
    fixed = [c for c in crons if _et_at(c, _EDT_DAY) is not None]
    fixed.sort(key=lambda c: (int(c.split()[1]), int(c.split()[0])))
    if len(fixed) == 2:
        return [(fixed[0], _EDT_DAY), (fixed[1], _EST_DAY)]
    # A lone cron has no DST twin — it must clear the cutoff year-round.
    return [(c, d) for c in fixed for d in (_EDT_DAY, _EST_DAY)]


def test_no_sending_cron_fires_before_the_wee_hours_cutoff():
    cutoff = _cutoff_hour()
    offenders = []
    for wf in _sending_workflows():
        for cron, day in _seasonal_pairs(_crons(wf)):
            et = _et_at(cron, day)
            if et and et.hour < cutoff:
                offenders.append(
                    f"{wf.name}: cron {cron!r} lands {et:%H:%M} ET "
                    f"({et.tzname()}), before the {cutoff}:00 cutoff")
    assert not offenders, (
        "a scheduled fire lands in the wee-hours window, where "
        "core.report_business_day rolls the report back a business day — it "
        "would report the wrong day every day, silently:\n  "
        + "\n  ".join(offenders))


def test_the_cutoff_actually_bites_where_this_guard_says_it_does():
    """Prove the cliff is real, so the guard above is not cargo-culted."""
    cutoff = _cutoff_hour()
    thu = datetime(2026, 8, 27, cutoff - 1, 7, tzinfo=ET)   # just below
    ok = datetime(2026, 8, 27, cutoff, 7, tzinfo=ET)        # just above
    below = core.report_business_day(thu, window="previous")
    above = core.report_business_day(ok, window="previous")
    assert below != above, (
        "the wee-hours rule no longer changes the report day at this hour — "
        "re-derive the cutoff before trusting the guard")
    assert below == above - timedelta(days=1), (
        f"below the cutoff reports {below}, above reports {above}; the guard "
        f"assumes exactly one business day of difference")


def test_the_daily_fire_still_reports_the_prior_business_day():
    """The point of moving the cron was slack, not a different report day."""
    daily = _crons(ROOT / ".github" / "workflows" / "daily.yml")
    for cron, _season in _seasonal_pairs(daily):
        # Evaluate on the real dates, in EDT (August is EDT either way).
        thu = _et_at(cron, date(2026, 8, 27))
        mon = _et_at(cron, date(2026, 8, 31))
        if thu is None:
            continue
        # Only the season-matched tick proceeds; the other is gated out, so
        # judge each cron on the days it would actually run.
        if thu.hour < _cutoff_hour():
            continue
        assert core.report_business_day(thu, window="previous") == \
            date(2026, 8, 26), f"cron {cron!r} at {thu:%H:%M %Z} (Thu)"
        assert core.report_business_day(mon, window="previous") == \
            date(2026, 8, 28), f"cron {cron!r} at {mon:%H:%M %Z} (Mon)"


def test_the_guard_catches_a_planted_early_cron():
    """It must discriminate, or it is decoration."""
    cutoff = _cutoff_hour()
    # 09:30 UTC is 05:30 EDT — one hour under the cliff.
    planted = [_et_at("30 9 * * 1-5", d) for d in (_EDT_DAY, _EST_DAY)]
    assert all(planted), "probe cron did not parse"
    assert any(et.hour < cutoff for et in planted), (
        "a 09:30 UTC cron should land below the cutoff in EDT; if it does "
        "not, _et_hours is wrong and the real guard proves nothing")


def test_the_weeklys_exemption_is_real_and_not_just_asserted():
    """weekly.yml fires at 5:07 AM ET, inside the window this file guards.
    That is only safe while the weekly keeps its OWN day rule. If it is ever
    routed through core.report_business_day, the exemption silently becomes
    wrong and every Monday summary reports the week before the week before.
    """
    src = (ROOT / "scripts" / "gen_weekly_summary.py").read_text(encoding="utf-8")
    assert "def _fire_day_et" in src, (
        "gen_weekly_summary lost _fire_day_et — its 5 AM cron is now "
        "unguarded; either restore the rule or move the cron past the cutoff")
    assert "report_business_day" not in src, (
        "gen_weekly_summary now uses core.report_business_day, whose "
        "wee-hours rollback its 5:07 AM ET cron sits underneath. Add "
        "weekly.yml back to _sending_workflows() and move its cron.")
    # And prove the rule behaves: 5 AM Monday must stay Monday.
    import gen_weekly_summary as W
    five_am_mon = datetime(2026, 8, 31, 9, 7, tzinfo=ZoneInfo("UTC"))  # 05:07 ET
    assert W._fire_day_et(five_am_mon) == date(2026, 8, 31), (
        "a 5 AM Monday fire no longer resolves to Monday")
