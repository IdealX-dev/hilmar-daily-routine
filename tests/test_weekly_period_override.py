"""The weekly summary must be able to report an EXPLICIT period.

Regression for the 2026-08-04 backfill (Michael: "you failed in your
backfill.. lots more work happened this week including today").

What happened: the daily fires were down 2026-07-28 → 2026-08-04, so no report
covered that stretch. The recovery run was `gen_weekly_summary.py --force`,
which is hard-anchored to `today - 7 days` → the previous Mon-Fri. Dispatched
on Tuesday 2026-08-04 that anchors to Jul 27–31 and silently drops Aug 3 and
Aug 4 — including the day Michael was looking at. The generator reported a
week; the outage was longer than a week; nothing said so.

The fix is `--start/--end`: report the window that was actually missed. The
default (no flags) path must stay byte-for-byte what the Monday cron produces,
because that is the run nobody watches.
"""
from __future__ import annotations

import json
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import gen_weekly_summary as GWS  # noqa: E402

UTC = timezone.utc

# The real dispatch instant: Tuesday 2026-08-04. `--force` here anchors to
# Jul 27–31; the outage ran Jul 28 → Aug 4.
TUESDAY_2026_08_04 = datetime(2026, 8, 4, 17, 0, tzinfo=UTC)

WORKFLOW = ROOT / ".github" / "workflows" / "weekly.yml"


def _row(rid, day, status="WIN", carrier="MSC", teu=2):
    r = {"request_id": rid, "request_date": day, "status": status,
         "quoted": status != "NQ", "carrier_quoted": carrier,
         "teu_requested": teu}
    if status == "WIN":
        r["carrier_won"] = carrier
        r["teu_won"] = teu
        r["win_date"] = day
    return r


def _run(tmp_path, monkeypatch, rows, argv):
    data = tmp_path / "tracking-data-v2.json"
    data.write_text(json.dumps({"requests": rows}), encoding="utf-8")
    monkeypatch.setattr(GWS, "DATA", data)
    monkeypatch.setattr(GWS, "REPORTS", tmp_path / "reports")
    rc = GWS.main(argv, now=TUESDAY_2026_08_04)
    return rc, tmp_path / "reports"


# ── _explicit_period: the window arithmetic ────────────────────────────────

def test_explicit_period_spans_start_through_end_inclusive():
    s, e, ps, pe = GWS._explicit_period("2026-07-27", "2026-08-04")
    assert (s, e) == (date(2026, 7, 27), date(2026, 8, 4))


def test_baseline_is_the_same_length_window_immediately_before():
    # 9 calendar days Jul 27 → Aug 4, so the baseline is the 9 days ending
    # Jul 26. Comparing a 9-day window against a 5-day Mon-Fri would print a
    # delta manufactured by the window length, not by the business.
    s, e, ps, pe = GWS._explicit_period("2026-07-27", "2026-08-04")
    assert pe == date(2026, 7, 26), "baseline must end the day before start"
    assert (e - s).days == (pe - ps).days, "baseline must match the period's length"
    assert ps == date(2026, 7, 18)


def test_single_day_period_is_legal():
    s, e, ps, pe = GWS._explicit_period("2026-08-04", "2026-08-04")
    assert s == e == date(2026, 8, 4)
    assert ps == pe == date(2026, 8, 3)


@pytest.mark.parametrize("start,end", [
    ("2026-07-27", ""),
    ("", "2026-08-04"),
])
def test_start_and_end_must_be_given_together(start, end):
    with pytest.raises(ValueError, match="together"):
        GWS._explicit_period(start, end)


def test_end_before_start_is_rejected():
    with pytest.raises(ValueError, match="before"):
        GWS._explicit_period("2026-08-04", "2026-07-27")


@pytest.mark.parametrize("bad", ["08/04/2026", "2026-8-4x", "yesterday", "20260804"])
def test_non_iso_dates_are_rejected(bad):
    with pytest.raises(ValueError, match="ISO"):
        GWS._explicit_period(bad, "2026-08-04")


# ── _range_label: a caption that is actually a date range ──────────────────

@pytest.mark.parametrize("start,end,expect", [
    # A Mon-Fri week never leaves its month — the form that already shipped.
    (date(2026, 7, 27), date(2026, 7, 31), "Jul 27–31, 2026"),
    # The cross-month case the old one-line label got wrong: it dropped the
    # end month unconditionally and rendered "Jul 27–4, 2026".
    (date(2026, 7, 27), date(2026, 8, 4), "Jul 27–Aug 4, 2026"),
    (date(2026, 12, 28), date(2027, 1, 1), "Dec 28, 2026–Jan 1, 2027"),
    (date(2026, 8, 4), date(2026, 8, 4), "Aug 4–4, 2026"),
])
def test_range_label(start, end, expect):
    assert GWS._range_label(start, end) == expect


# ── main(): the defect this option exists to fix ───────────────────────────

def test_force_alone_misses_the_tail_of_the_outage(tmp_path, monkeypatch):
    """Pins the BUG so the fix can be shown to close it.

    Not an aspiration — this is the behavior that shipped Michael a report
    covering one day of a six-day gap.
    """
    rows = [_row("a", "2026-07-29"), _row("b", "2026-08-03"), _row("c", "2026-08-04")]
    rc, reports = _run(tmp_path, monkeypatch, rows, ["--force"])
    assert rc == 0
    html = (reports / "weekly-summary.html").read_text(encoding="utf-8")
    # Jul 27–31 window: 1 of the 3 rows.
    assert re.search(r'class="val">1</span><span class="lbl">Requests', html), (
        "the previous-week anchor should see only the Jul 29 row — if this "
        "fails the anchor moved and this test's premise is stale"
    )


def test_explicit_period_covers_the_whole_outage_including_today(tmp_path, monkeypatch):
    rows = [_row("a", "2026-07-29"), _row("b", "2026-08-03"), _row("c", "2026-08-04")]
    rc, reports = _run(tmp_path, monkeypatch, rows,
                       ["--start", "2026-07-27", "--end", "2026-08-04"])
    assert rc == 0
    html = (reports / "weekly-summary.html").read_text(encoding="utf-8")
    assert re.search(r'class="val">3</span><span class="lbl">Requests', html), (
        "all three days of the outage window must be counted, Aug 4 included"
    )


def test_explicit_period_generates_on_a_non_monday_without_force(tmp_path, monkeypatch):
    # Aug 4 2026 is a Tuesday. An explicit period IS the request; requiring
    # --force as well would let a wrong weekday drop a recovery run that was
    # asked for by date.
    assert TUESDAY_2026_08_04.astimezone(GWS.core.ET).weekday() != 0
    rc, reports = _run(tmp_path, monkeypatch, [_row("a", "2026-08-04")],
                       ["--start", "2026-08-04", "--end", "2026-08-04"])
    assert rc == 0
    assert (reports / "weekly-summary.html").exists()


def test_no_flags_on_a_tuesday_still_skips(tmp_path, monkeypatch):
    """The Monday-only gate is untouched for the ordinary path."""
    rc, reports = _run(tmp_path, monkeypatch, [_row("a", "2026-08-04")], [])
    assert rc == 0
    assert not (reports / "weekly-summary.html").exists()


@pytest.mark.parametrize("argv,expect_rc", [
    (["--start", "2026-07-27"], 2),
    (["--end", "2026-08-04"], 2),
    (["--start", "2026-08-04", "--end", "2026-07-27"], 2),
    (["--start", "not-a-date", "--end", "2026-08-04"], 2),
])
def test_bad_period_exits_2_and_writes_nothing(tmp_path, monkeypatch, argv, expect_rc):
    """Exit 2 fails the workflow step, and the send step reads files this
    step must write — so a typo'd date sends nothing rather than sending the
    wrong window."""
    rc, reports = _run(tmp_path, monkeypatch, [_row("a", "2026-08-04")], argv)
    assert rc == expect_rc
    assert not (reports / "weekly-summary.html").exists()
    assert not (reports / "weekly-subject.txt").exists()


# ── the subject line ───────────────────────────────────────────────────────

def test_default_subject_matches_what_the_shell_used_to_build(tmp_path, monkeypatch):
    """weekly.yml built this in shell as
       LABEL=$(date -u -d 'last monday -7 days' +'%b %-d')
       SUBJECT="Hilmar — Weekly Executive Summary (week of $LABEL)"
    Moving it into Python must not change the string: the cross-host mailbox
    guard dedupes on the subject, so a changed subject silently changes
    idempotency behavior for the scheduled run."""
    rc, reports = _run(tmp_path, monkeypatch, [_row("a", "2026-07-29")], ["--force"])
    assert rc == 0
    subject = (reports / "weekly-subject.txt").read_text(encoding="utf-8")
    assert subject == "Hilmar — Weekly Executive Summary (week of Jul 27)"


def test_catchup_subject_names_the_actual_period(tmp_path, monkeypatch):
    rc, reports = _run(tmp_path, monkeypatch, [_row("a", "2026-07-29")],
                       ["--start", "2026-07-27", "--end", "2026-08-04"])
    assert rc == 0
    subject = (reports / "weekly-subject.txt").read_text(encoding="utf-8")
    assert subject == "Hilmar — Catch-Up Executive Summary (Jul 27–Aug 4, 2026)"
    assert "Weekly Executive Summary" not in subject, (
        "a catch-up must NOT reuse the weekly subject — same subject means the "
        "mailbox guard treats one as a duplicate of the other"
    )


def test_subject_is_written_every_run_the_html_is(tmp_path, monkeypatch):
    for argv in (["--force"], ["--start", "2026-07-27", "--end", "2026-08-04"]):
        rc, reports = _run(tmp_path, monkeypatch, [_row("a", "2026-07-29")], argv)
        assert rc == 0
        assert (reports / "weekly-summary.html").exists()
        assert (reports / "weekly-subject.txt").read_text(encoding="utf-8").strip(), (
            f"{argv} produced HTML but no subject — the send step would have "
            f"nothing to title it with"
        )


# ── the rendered caption must not contradict the numbers ───────────────────

def test_catchup_html_does_not_call_itself_the_previous_week(tmp_path, monkeypatch):
    rc, reports = _run(tmp_path, monkeypatch, [_row("a", "2026-08-04")],
                       ["--start", "2026-07-27", "--end", "2026-08-04"])
    html = (reports / "weekly-summary.html").read_text(encoding="utf-8")
    assert "Previous week:" not in html
    assert "Reporting period:" in html
    assert "Jul 27" in html and "Aug 4" in html
    # The deltas have to say what they are measured against, or a reader
    # assumes "vs last week" and misreads a 9-day-vs-9-day comparison.
    assert "vs the preceding" in html


def test_default_html_still_says_previous_week(tmp_path, monkeypatch):
    rc, reports = _run(tmp_path, monkeypatch, [_row("a", "2026-07-29")], ["--force"])
    html = (reports / "weekly-summary.html").read_text(encoding="utf-8")
    assert "Previous week:" in html
    assert "Week at a glance" in html
    assert "vs the preceding" not in html, (
        "the scheduled Monday run must render exactly as it did before"
    )


# ── the workflow wiring (helper tested, wiring not — 3x in one day) ────────

def test_workflow_passes_the_period_inputs_through():
    wf = WORKFLOW.read_text(encoding="utf-8")
    assert "PERIOD_START: ${{ github.event.inputs.start }}" in wf
    assert "PERIOD_END: ${{ github.event.inputs.end }}" in wf
    assert '--start "$PERIOD_START" --end "$PERIOD_END"' in wf, (
        "the generator gained --start/--end but the workflow never passes them"
    )


def test_workflow_declares_both_dispatch_inputs():
    wf = WORKFLOW.read_text(encoding="utf-8")
    for name in ("start:", "end:"):
        assert f"      {name}\n" in wf, f"workflow_dispatch input {name!r} missing"


def test_workflow_no_longer_builds_the_subject_in_shell():
    wf = WORKFLOW.read_text(encoding="utf-8")
    assert "last monday -7 days" not in wf, (
        "two clocks, one header: shell date math here can disagree with the "
        "period the HTML covers"
    )
    assert 'SUBJECT="Hilmar' not in wf


def test_workflow_refuses_to_send_without_a_subject_file():
    wf = WORKFLOW.read_text(encoding="utf-8")
    assert "-s reports/weekly-subject.txt" in wf, (
        "the subject now comes from the generator; the send step must fail "
        "closed if it is missing rather than mailing an untitled summary"
    )


def test_workflow_still_defaults_to_the_previous_week_path():
    wf = WORKFLOW.read_text(encoding="utf-8")
    assert "gen_weekly_summary.py --force" in wf, (
        "with no start/end the scheduled Monday run must take the unchanged path"
    )
