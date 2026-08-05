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


# ── it ships GATED OFF ───────────────────────────────────────────────────────

def test_send_is_disabled_in_shipped_config(cfg):
    """Lonny receives nothing until a person flips this, having read a
    rendered week. The client daily took a day to earn it."""
    assert cfg["client_weekly"]["enabled"] is False, (
        "the client weekly was enabled without an explicit go-live decision")


def test_no_pipeline_step_sends_the_client_weekly():
    """The gate is a config flag AND the absence of a send. A flag alone is
    half a stop — that lesson is written into daily.yml about the pause flag,
    and it applies here: nothing should be one boolean away from mailing a
    customer."""
    src = (ROOT / "scripts" / "run_pipeline.py").read_text(encoding="utf-8")
    assert "gen_client_weekly.py" in src, "the rollup stopped being built"
    for wf in ("daily.yml", "weekly.yml"):
        text = (ROOT / ".github" / "workflows" / wf).read_text(encoding="utf-8")
        assert "client-weekly" not in text, (
            f"{wf} gained a send path for the gated client weekly")


def test_the_recipients_match_the_client_daily(cfg):
    """Two client artifacts, one approved audience. A second list is a second
    thing to get wrong."""
    assert cfg["client_weekly"]["to"] == cfg["client_report"]["to"]
    assert cfg["client_weekly"]["cc"] == cfg["client_report"]["cc"]
