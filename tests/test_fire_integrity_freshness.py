"""The integrity gate must tell a FRESH send apart from an idempotency-suppressed
no-send.

Regression for 2026-07-20 ("no daily tracker went out yesterday"): Monday's fire
found Saturday's sent-2026-07-17.flag (the stray weekend fire had already shipped
Friday's report), sent nothing, and yet assert_fire_integrity printed
"✅ fresh report shipped". The flag EXISTING is not proof THIS fire shipped —
send_freshness checks whether the flag records a send dated today.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "assert_fire_integrity", ROOT / "deploy" / "assert_fire_integrity.py")
AFI = importlib.util.module_from_spec(_spec)
sys.modules["assert_fire_integrity"] = AFI
_spec.loader.exec_module(AFI)


def _flag(rep: Path, report_day: str, sent_dates: list[str]):
    body = "".join(
        f"Sent {d} 09:45 ET req=abc-{i} to=9 recipient(s)\n"
        for i, d in enumerate(sent_dates))
    (rep / f"sent-{report_day}.flag").write_text(body or "sent", encoding="utf-8")


def test_fresh_when_flag_has_a_send_dated_today(tmp_path):
    # today's fire reports today (calendar==report for this direct check) and
    # wrote a flag entry dated today.
    _flag(tmp_path, "2026-07-21", ["2026-07-21"])
    status, detail = AFI.send_freshness("2026-07-21", reports=tmp_path)
    assert status == "fresh", detail


def test_suppressed_when_flag_is_from_an_earlier_fire(tmp_path):
    # THE bug: report day 2026-07-17 flag was written 2026-07-18 (the stray
    # Saturday fire). A Monday fire (2026-07-20) reporting Friday finds it and
    # ships nothing — must read as suppressed, not fresh.
    _flag(tmp_path, "2026-07-20", ["2026-07-18"])   # flag under the report-day name
    # _flag_day maps calendar 2026-07-20 → report day; force the direct path by
    # also naming the calendar-day flag so the loop finds it.
    status, detail = AFI.send_freshness("2026-07-20", reports=tmp_path)
    assert status == "suppressed", detail
    assert "2026-07-18" in detail and "2026-07-20" in detail


def test_absent_when_no_flag(tmp_path):
    status, _ = AFI.send_freshness("2026-07-21", reports=tmp_path)
    assert status == "absent"


def test_flag_entry_dates_parses_appended_lines(tmp_path):
    f = tmp_path / "sent-x.flag"
    f.write_text(
        "Sent 2026-07-18 09:45 ET req=a to=9 recipient(s)\n"
        "Sent 2026-07-20 10:33 ET req=b to=9 recipient(s)\n",
        encoding="utf-8")
    assert AFI._flag_entry_dates(f) == ["2026-07-18", "2026-07-20"]


def test_check_integrity_contract_unchanged(tmp_path):
    # send_freshness is additive — check_integrity still passes on a bare flag.
    rep = tmp_path / "reports"; rep.mkdir()
    sec = tmp_path / "secrets"; sec.mkdir()
    for n in ("email-subject.txt", "email-body.html", "hilmar-report.pdf"):
        (rep / n).write_text("x", encoding="utf-8")
    today = AFI._et_today()
    (rep / f"sent-{today}.flag").write_text("sent", encoding="utf-8")
    assert AFI.check_integrity(pipeline_rc=0, today=today, reports=rep, secrets=sec) == []
