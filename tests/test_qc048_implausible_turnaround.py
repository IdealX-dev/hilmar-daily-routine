"""QC-048: a turnaround above 40 business hours is a mis-pairing, not a slow OL.

Written 2026-08-19. This check has been in KNOWN_UNTESTED since it shipped,
and it became load-bearing the moment the sibling-date heal started stamping
borrowed response times: QC-048 is the ONLY thing that clears an implausible
turnaround once one is on disk, and it clears strictly above 40 biz-hours.

That threshold is why the borrowed-date bug reached the KPI. A copied minute
whose gap happened to fall UNDER 40 produced a plausible-looking sample
(measured: 6.95 biz-hours) that QC-048 was never going to touch, and it flowed
into summary.turnaround_avg_biz_hours, the carrier scoreboard gen_pdf sorts
by, and the insights baseline future fires compare against.

So the fix does not widen QC-048 — a real OL reply CAN take 4 hours and must
keep its sample. The borrowed row is scrubbed earlier in the same loop
iteration, and QC-048 keeps its >40 population exactly as-is for evidenced
rows. These tests pin both halves of that division of labour.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import core  # noqa: E402
import qc_selfheal as QS  # noqa: E402


def _row(**over):
    r = {"request_id": "req_1", "status": "LOSS", "quoted": True,
         "lane": "Oakland → Yokohama", "origin": "Oakland",
         "destination": "Yokohama", "ol_rate": 3010.0,
         "request_timestamp": "2026-08-10T14:00:00Z",
         "request_date": "2026-08-10",
         "response_timestamp": "2026-08-10T18:00:00Z",
         "status_history": []}
    r.update(over)
    return r


def _phase3(rows):
    log = QS.Log()
    QS.phase_3_entries(log, {"requests": rows})
    return log


def test_an_implausible_turnaround_is_cleared():
    """THE CHECK. >40 biz-hours means the response_timestamp was mis-paired —
    a stale reply from a later thread or a leaked booking time — not that OL
    took a week to answer."""
    r = _row(turnaround_biz_hours=96.0, turnaround_hours=120.0)
    _phase3([r])
    assert r["turnaround_biz_hours"] is None
    assert r["turnaround_hours"] is None


def test_a_plausible_turnaround_survives():
    """The threshold must not swallow real samples. A 4-hour reply is
    ordinary and its statistic is the point of the metric."""
    r = _row(turnaround_biz_hours=4.0, turnaround_hours=4.0)
    _phase3([r])
    assert r["turnaround_biz_hours"] == 4.0


def test_the_boundary_is_exclusive():
    """Exactly 40 is kept; the check clears strictly above it."""
    r = _row(turnaround_biz_hours=40.0, turnaround_hours=40.0)
    _phase3([r])
    assert r["turnaround_biz_hours"] == 40.0


def test_the_clear_is_reported_not_silent():
    """A statistic vanishing without a line in the audit is how a metric
    quietly becomes wrong."""
    r = _row(turnaround_biz_hours=96.0, turnaround_hours=120.0)
    log = _phase3([r])
    assert any("implausible turnaround" in f for f in log.fixes)


# ── the division of labour with the borrowed-date scrub ───────────────────

def test_qc048_is_not_what_protects_against_a_borrowed_date():
    """THE GAP THAT LET 6.95 BIZ-HOURS REACH THE KPI. A borrowed row under
    the threshold is invisible to QC-048 by design — it is cleared earlier,
    by the response_time_is_evidenced scrub, and this test exists so nobody
    "simplifies" the two into one."""
    borrowed = _row(turnaround_biz_hours=6.95, turnaround_hours=6.95,
                    response_time_source=core.BORROWED_RESPONSE_TIME)
    assert borrowed["turnaround_biz_hours"] <= 40, (
        "the fixture must sit UNDER QC-048's threshold or it proves nothing")
    _phase3([borrowed])
    assert borrowed["turnaround_biz_hours"] is None, (
        "a borrowed-date turnaround survived phase 3 — QC-048 will not catch "
        "it, because 6.95 is not > 40")
    assert borrowed["response_timestamp"] is not None, (
        "the DATE is evidence about which quote covered the lane and stays")
