"""The wee-hours report-day rule + report-day-keyed send flags (run #76).

Live failure (2026-07-02, GitHub run #76): a manual production-fire dispatch
at 12:38 AM ET Thursday reported an ALL-ZERO "Thu Jul 2" to the full
distribution — the calendar day had just started and was empty — and wrote
Thursday's sent-flag + poisoned the mailbox guard, blocking the REAL
Thursday-evening send before Thursday even happened.

The fix, keyed to one semantic: the fire is an EVENING fire, so
  1. core.report_business_day: a run between midnight and 6 AM ET reports
     the business day that just ENDED (paired: scripts + src/hilmar).
  2. outlook_send keys the idempotency flags to the REPORT day
     (_flag_date), so a night fire dedupes against the evening it belongs
     to instead of blocking the next one.
  3. assert_fire_integrity accepts the send proof under the report-day name.
  4. state_store syncs both the calendar-day and report-day flag names.
Also from the same email: QC-064 now scans the Equipment/containers cell
(the "209-656" phone fragment was client-visible).
"""
import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "deploy"))

import core as scripts_core  # noqa: E402
import outlook_send as os_send  # noqa: E402
import qc_selfheal as q  # noqa: E402
import state_store as ss  # noqa: E402

from hilmar import core as lib_core  # noqa: E402

ET = ZoneInfo("America/New_York")


def _et(y, m, d, hh, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=ET)


# ── 1. the wee-hours rule, both trees ─────────────────────────────────────
def test_after_midnight_reports_the_evening_that_just_ended():
    # The literal run-#76 case: 12:38 AM ET Thu Jul 2 → report Wed Jul 1.
    for core in (scripts_core, lib_core):
        assert core.report_business_day(_et(2026, 7, 2, 0, 38), window="current") == date(2026, 7, 1)


def test_evening_fire_still_reports_today():
    for core in (scripts_core, lib_core):
        assert core.report_business_day(_et(2026, 7, 2, 18, 7), window="current") == date(2026, 7, 2)


def test_early_monday_reports_friday():
    # Mon 00:30 → Sun (hour rule) → Fri (weekend mapping).
    for core in (scripts_core, lib_core):
        assert core.report_business_day(_et(2026, 7, 6, 0, 30), window="current") == date(2026, 7, 3)


def test_early_saturday_reports_friday():
    for core in (scripts_core, lib_core):
        assert core.report_business_day(_et(2026, 7, 4, 0, 30), window="current") == date(2026, 7, 3)


def test_date_only_input_is_untouched():
    """Explicit report dates (tests, fixtures) carry no time — no hour rule."""
    for core in (scripts_core, lib_core):
        assert core.report_business_day(date(2026, 7, 2), window="current") == date(2026, 7, 2)


# ── 2. flags keyed to the report day ──────────────────────────────────────
def test_flag_date_maps_night_fire_to_previous_evening():
    assert os_send._flag_date(_et(2026, 7, 2, 0, 38)) == "2026-07-01"
    assert os_send._flag_date(_et(2026, 7, 2, 19, 0)) == "2026-07-02"


# ── 3. integrity proof accepted under the report-day name ─────────────────
def test_integrity_accepts_report_day_flag(tmp_path):
    import assert_fire_integrity as AFI
    today = AFI._et_today()
    rep = tmp_path / "reports"
    rep.mkdir()
    for name in ("email-subject.txt", "email-body.html", "hilmar-report.pdf"):
        (rep / name).write_text("x", encoding="utf-8")
    # Proof written under the REPORT-day keying (what outlook_send writes).
    (rep / f"sent-{AFI._flag_day(today)}.flag").write_text("sent", encoding="utf-8")
    sec = tmp_path / "secrets"
    sec.mkdir()
    (sec / "token-cache.json").write_text("{}", encoding="utf-8")
    assert AFI.check_integrity(pipeline_rc=0, today=today, reports=rep, secrets=sec) == []


def test_flag_day_passthrough_for_injected_dates():
    import assert_fire_integrity as AFI
    # A non-"now" injected date is used verbatim — hermetic tests stay stable.
    assert AFI._flag_day("2020-01-01") == "2020-01-01"


# ── 4. state sync covers both flag names ──────────────────────────────────
def test_state_paths_include_report_day_flags(monkeypatch):
    monkeypatch.setattr(scripts_core, "report_business_day",
                        lambda now: date(2026, 7, 1))
    paths = ss.state_paths("2026-07-02")
    for d in ("2026-07-01", "2026-07-02"):
        assert f"reports/sent-{d}.flag" in paths
        assert f"reports/improvements-sent-{d}.flag" in paths


def test_state_paths_no_duplicates_when_days_match(monkeypatch):
    monkeypatch.setattr(scripts_core, "report_business_day",
                        lambda now: date(2026, 7, 2))
    paths = ss.state_paths("2026-07-02")
    assert paths.count("reports/sent-2026-07-02.flag") == 1


# ── 5. QC-064 covers the Equipment cell ("209-656" was client-visible) ────
def test_qc064_scans_containers_field():
    assert "containers" in q.QC064_DISPLAY_FIELDS
    assert q.qc064_garbage_reason("209-656")


def test_qc064_leaves_real_equipment_strings_alone():
    for ok in ("2-40'RF", "1-20'DV + 2-40'HC", "3-20'DV", "— (manual review)"):
        assert q.qc064_garbage_reason(ok) is None, ok


def test_phase6_nulls_phone_fragment_in_containers(monkeypatch):
    monkeypatch.setenv("HILMAR_QC_NO_PIP", "1")
    r = {"request_id": "req_x", "status": "LOSS", "quoted": True,
         "containers": "209-656"}
    data = {"version": "2", "requests": [r],
            "summary": {"wins": 0, "quoted_lost": 0, "not_quoted": 0,
                        "pending_hilmar": 0, "win_rate": 0.0, "quote_rate": 0.0,
                        "teu_requested": 0, "teu_won": 0, "teu_quoted_lost": 0,
                        "teu_not_quoted": 0, "teu_pending": 0, "total_entries": 0}}
    log = q.Log()
    q.phase_6_rules(log, data)
    assert r["containers"] is None
    assert any("QC-064" in m and "containers" in m for m in log.fixes), log.fixes
