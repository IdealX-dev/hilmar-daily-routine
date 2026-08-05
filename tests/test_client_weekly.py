"""The client weekly rollup (scripts/gen_client_weekly.py) and its guardrails.

Michael 2026-08-05, on whether Lonny gets tallied reports with all the data:
he did not — a body-only daily and nothing weekly. This is the weekly, in the
shape Michael picked: week at a glance · your bookings · quotes still open ·
upcoming cutoffs · 4-week volume trend.

The tests that matter here are the NEGATIVE ones. This artifact is one config
flag away from reaching a customer, and the failure that costs something is
not a broken table — it is a correct-looking email that hands Hilmar our
success rate, our lost-quote count, or our carrier ranking. So the leak
scanner runs on the RENDERED body (it catches a marker arriving through data
as readily as one typed into a template), it runs per-marker rather than as
one lump, and it is joined by a test asserting the rollup is not empty —
because a renderer that produced nothing would pass every leak check ever
written.
"""
from __future__ import annotations

import json
import re
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import gen_client_weekly as GCW  # noqa: E402
import qc_selfheal as QC  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "golden_day.json"
# The golden fixture's own span.
START, END = date(2026, 4, 2), date(2026, 4, 14)


@pytest.fixture(scope="module")
def data() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def cfg() -> dict:
    return json.loads((ROOT / "config.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def body(data, cfg) -> str:
    return GCW.build_body(data, cfg, START, END)


# ── the line this file exists to hold ────────────────────────────────────────

def test_body_is_free_of_internal_analytics(body):
    """The hard guarantee, via the SAME scanner that guards the client daily."""
    assert QC.qc065_internal_leaks(body) == []


@pytest.mark.parametrize("marker", QC.QC065_INTERNAL_MARKERS)
def test_each_internal_marker_individually(marker, body):
    """Per-marker so a failure names WHICH one leaked. A single assertion over
    the list tells you something leaked and leaves you to find it."""
    assert marker not in body.lower(), f"client weekly leaked {marker!r}"


def test_the_rollup_is_not_empty(body):
    """The test that keeps every assertion above honest. A renderer that
    returned "" passes every leak check ever written."""
    assert len(body) > 4000
    for heading in ("Week at a glance", "Your bookings this week",
                    "Quotes still open", "Volume"):
        assert heading in body, f"missing section: {heading}"


def test_no_unresolved_placeholder_reaches_the_client(body):
    """Same failure mode that put "{B.DOC_WARN}" into a staff email body on
    2026-08-05 with a 2458-green suite."""
    leaks = [x for x in set(re.findall(r"\{[A-Za-z_][A-Za-z_0-9.]*\}", body))
             if x.startswith(("{B.", "{DOC_", "{TH_", "{EMAIL_", "{HEADER_", "{STRIPE_"))]
    assert not leaks, f"unresolved placeholders: {leaks}"


def test_the_volume_trend_carries_no_performance_rate(data):
    """Volume is the CLIENT's number — what Hilmar shipped. A rate over the
    same four weeks would be OL's report card."""
    trend = GCW.volume_trend(data, END)
    assert len(trend) == GCW.TREND_WEEKS
    for wk in trend:
        assert set(wk) == {"label", "requests", "teu", "bookings", "teu_booked"}, (
            f"the trend grew a key that is not volume: {sorted(wk)}")


def test_the_trend_reads_oldest_first(data):
    """Row order IS the time axis in a table with no chart.

    Asserted against the weeks actually expected, not against a self-derived
    sort — a test that sorts the output and compares it to itself passes for
    any output at all.
    """
    from datetime import timedelta
    mon = END - timedelta(days=END.weekday())
    want = [GCW.range_label(mon - timedelta(days=7 * i),
                            mon - timedelta(days=7 * i) + timedelta(days=4))
            for i in range(GCW.TREND_WEEKS - 1, -1, -1)]
    assert [w["label"] for w in GCW.volume_trend(data, END)] == want


# ── windowing: the distinction the daily got wrong ───────────────────────────

def test_open_quotes_are_current_state_not_windowed(data, cfg):
    """A quote delivered before the reported week is STILL open. Hiding it
    because it is old would hide the only rows needing Lonny to act — the
    inverse of the quiet-day bug, and the same misunderstanding."""
    old = {
        "request_id": "old-open", "request_date": "2026-01-05",
        "status": "PENDING", "quoted": True,
        "lane": "Oakland → Shanghai", "destination": "Shanghai, CN",
        "ol_rate": 4874.0, "teu_requested": 2,
        "response_timestamp": "2026-01-06T10:00:00Z",
    }
    d = {**data, "requests": [*data["requests"], old]}
    s = GCW.client_sections(d, START, END)
    assert any(r.get("request_id") == "old-open" for r in s["open_quotes"]), (
        "an open quote from before the period vanished from the rollup")
    assert not any(r.get("request_id") == "old-open" for r in s["requests"]), (
        "an out-of-period row leaked into the week's request count")


def test_requests_and_bookings_are_windowed(data):
    s = GCW.client_sections(data, date(2026, 4, 2), date(2026, 4, 3))
    for r in s["requests"] + s["bookings"]:
        d = r.get("request_date") or r.get("date")
        assert "2026-04-02" <= d[:10] <= "2026-04-03", d


def test_bookings_are_status_form_agnostic(data):
    """Same trap as the dashboard: a STRICT-form row must count identically."""
    strict = []
    for r in data["requests"]:
        r = dict(r)
        if r.get("status") == "LOSS":
            r["status"] = "Q&L" if r.get("quoted") else "NQ"
        strict.append(r)
    a = GCW.client_sections(data, START, END)
    b = GCW.client_sections({**data, "requests": strict}, START, END)
    assert len(a["bookings"]) == len(b["bookings"])
    assert len(a["requests"]) == len(b["requests"])


# ── period labels ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("start,end,want", [
    (date(2026, 7, 27), date(2026, 7, 31), "Jul 27–31, 2026"),
    (date(2026, 7, 27), date(2026, 8, 4), "Jul 27–Aug 4, 2026"),
    (date(2026, 12, 28), date(2027, 1, 1), "Dec 28, 2026–Jan 1, 2027"),
])
def test_range_label(start, end, want):
    """The cross-month case is here because its first draft rendered
    "Jul 27–4, 2026", which reads as a typo rather than a date."""
    assert GCW.range_label(start, end) == want


def test_week_bounds_is_the_previous_completed_week():
    """Monday–Friday of the week that ENDED, not the one in progress."""
    start, end = GCW.week_bounds(date(2026, 8, 3))     # a Monday
    assert (start, end) == (date(2026, 7, 27), date(2026, 7, 31))
    start, end = GCW.week_bounds(date(2026, 8, 6))     # mid-week
    assert (start, end) == (date(2026, 7, 27), date(2026, 7, 31))


def test_subject_names_the_period(cfg):
    subj = GCW.build_subject(date(2026, 7, 27), date(2026, 8, 4), cfg)
    assert "Jul 27–Aug 4, 2026" in subj
    assert QC.qc065_internal_leaks(subj) == []


# ── the gate ────────────────────────────────────────────────────────────────
#
# These two tests used to assert "enabled is False" and "no send path exists",
# which was true and useful for exactly one day. Michael enabled it on
# 2026-08-05 ("flip client weekly") and both went red — not because anything
# broke, but because they pinned an OPERATIONAL STATE the operator is entitled
# to change. That is the same defect as the cron test that went red the
# morning he said resume, and the fix is the same: assert the INVARIANT that
# has to hold in both states, so the test survives the decision.

WEEKLY_YML = ROOT / ".github" / "workflows" / "weekly.yml"


def test_the_client_send_requires_both_the_flag_and_a_full_run():
    """Reaching Lonny takes TWO conditions, and neither alone is enough. A
    test dispatch must never reach a customer no matter how the flag sits."""
    text = WEEKLY_YML.read_text(encoding="utf-8")
    assert 'client_weekly' in text and 'client-weekly.html' in text, (
        "weekly.yml no longer sends the client rollup at all")
    assert '[ "$ENABLED" = "true" ] && [ "$SEND_TO" = "full" ]' in text, (
        "the client-weekly send is no longer gated on BOTH client_weekly."
        "enabled AND send_to=full")


def test_the_disabled_path_reaches_only_the_sample_address():
    """The else branch must be a labeled sample to sample_to, never the
    client. Labeled for two reasons: Michael mistook an unlabeled client
    sample for a gutted staff report on 2026-07-11, and an unprefixed sample
    shares the real subject, which would let the mailbox guard suppress the
    real send later."""
    text = WEEKLY_YML.read_text(encoding="utf-8")
    after = text.split('client_weekly.enabled=true', 1)[-1]
    assert "$SAMPLE_TO" in after and "--verification" in after
    assert "[SAMPLE - internal preview, Lonny does NOT receive this]" in after


def test_the_rollup_is_built_before_it_is_sent():
    """weekly.yml had no build step for the client rollup — only run_pipeline
    did, and run_pipeline runs in daily.yml. Enabling the send without adding
    the build would have shipped whatever stale file the runner had, or
    nothing."""
    text = WEEKLY_YML.read_text(encoding="utf-8")
    build = text.index("gen_client_weekly.py")
    send = text.index("client-weekly-subject.txt")
    assert build < send, "the client rollup is sent before it is built"


def test_the_client_weekly_owns_its_own_idempotency_flag():
    """Sharing weekly-sent would let the staff send consume the client send's
    guard, or the reverse — the exact collision that blocked 2026-07-30."""
    text = WEEKLY_YML.read_text(encoding="utf-8")
    assert "--flag-name client-weekly-sent" in text
    # and it must actually be persisted, comma-joined: a second bare word
    # would land as a positional and be dropped, so every Monday would look
    # like the first.
    assert "--only weekly-sent,client-weekly-sent" in text


def test_recipients_are_the_approved_pair_whenever_it_is_enabled(cfg):
    """The conditional invariant. While disabled a wrong address is merely
    wrong; the moment the flag flips it is live, which is why QC-065 checks
    recipients in BOTH states."""
    cw = cfg["client_weekly"]
    assert cw["to"] == cfg["client_report"]["to"], "two client artifacts, one audience"
    assert cw["cc"] == cfg["client_report"]["cc"]
    if cw["enabled"]:
        assert [a.lower() for a in cw["to"]] == [a.lower() for a in QC.QC065_APPROVED_TO]
        assert [a.lower() for a in cw["cc"]] == [a.lower() for a in QC.QC065_APPROVED_CC]


def test_qc065_covers_the_weekly_not_just_the_daily(cfg):
    """One definition of "safe client artifact", applied to both. A second
    inline copy for the weekly is the mistake this repo spent 2026-08-05
    undoing — five spellings of one rate predicate, two vocabularies for one
    status."""
    bad = {**cfg, "client_weekly": {**cfg["client_weekly"],
                                    "enabled": True,
                                    "to": ["someone.else@example.com"]}}
    problems = QC.qc065_check_client_block(
        bad, "client_weekly", ROOT / "does-not-exist.html")
    assert problems, "QC-065 accepted an unapproved recipient on the client weekly"
    assert any("client_weekly" in p for p in problems)


def test_qc065_rejects_a_staff_address_on_a_client_artifact(cfg):
    """The worst shape: the internal distribution receiving the client's own
    report, or the client receiving the staff list's."""
    staff = cfg["distribution"]["full_list"][0]
    bad = {**cfg, "client_weekly": {**cfg["client_weekly"], "to": [staff]}}
    problems = QC.qc065_check_client_block(
        bad, "client_weekly", ROOT / "does-not-exist.html")
    assert any("full_list" in p for p in problems)
