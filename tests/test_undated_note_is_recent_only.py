"""The undated-quote alarm covers what can still be acted on, not history.

Michael 2026-08-13, on a report banner reading 16: "also these 16 what? all
that truly matters at end of days is the wins and losses. turnaround is
secondary for the past moves.. so clear this error".

TWO THINGS WERE WRONG WITH IT.

1. THE WORDING. It said those rows "cannot be dated and are NOT COUNTED
   above". That meant "absent from the dated OL-USA RESPONSES table", but it
   reads as "missing from the totals" — so it invited a reconciliation of
   numbers that were never wrong. Every one of the 16 IS counted in wins,
   losses, TEU and every lane rollup. The only missing field is WHEN OL sent
   the quote.

2. THE SCOPE. Measured on stored state (diag-blob 31790544681) the 16 were
   10 WINs and 6 losses, all resolved months ago. A quote time on a move that
   already closed feeds turnaround and nothing else, and turnaround on
   history is not actionable. Erroring on it every fire trains the reader to
   skip the audit — which is how the count reached 41 unnoticed once already.

The detector is NOT deleted. The audit still states the backlog as a fact.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import core  # noqa: E402
import gen_email as GE  # noqa: E402

NOW = datetime.now(timezone.utc)


def _iso(dt):
    return dt.isoformat().replace("+00:00", "Z")


def _undated(rid, days_ago, **over):
    r = {"request_id": rid, "status": "LOSS", "quoted": True,
         "lane": "Oakland → Algeciras", "origin": "Oakland",
         "destination": "Algeciras", "ol_rate": 4938.0,
         "carrier_quoted": "CMA CGM", "response_timestamp": None,
         "request_timestamp": _iso(NOW - timedelta(days=days_ago))}
    r.update(over)
    return r


def test_a_months_old_undated_quote_is_not_current():
    assert core.undated_quote_is_current(_undated("old", 120)) is False


def test_a_quote_from_this_week_is_current():
    assert core.undated_quote_is_current(_undated("new", 3)) is True


def test_a_row_with_no_anchor_counts_as_current():
    """An undateable row that is ALSO unanchored is a data defect. Defaulting
    it to 'old' would hide the shape most worth seeing."""
    r = _undated("no_anchor", 1)
    r.pop("request_timestamp")
    assert core.undated_quote_is_current(r) is True


def test_the_report_note_ignores_the_historical_backlog():
    rows = [_undated(f"old_{i}", 120) for i in range(16)]
    assert GE.undated_quotes({"requests": rows}) == []
    assert GE._undated_quotes_note([]) == ""


def test_the_report_note_still_fires_for_a_current_gap():
    rows = [_undated("old", 120), _undated("fresh", 2)]
    got = GE.undated_quotes({"requests": rows})
    assert [r["request_id"] for r in got] == ["fresh"]


def test_the_note_no_longer_claims_they_are_uncounted():
    """The misleading half. These rows ARE in the win/loss totals."""
    note = GE._undated_quotes_note([_undated("fresh", 2)])
    assert "not counted above" not in note
    assert "still counted in the win/loss totals" in note.lower()


def test_the_audit_reports_the_backlog_as_a_fact_not_an_error(capsys):
    """Log.ok only PRINTS — there is no list to inspect — so the OK line is
    read off stdout. The error list is a real attribute."""
    import qc_selfheal as QS
    log = QS.Log()
    QS.phase_6_rules(log, {"requests": [_undated(f"old_{i}", 120)
                                        for i in range(16)]})
    errs = [e for e in log.warnings if "QC-077" in e]
    assert errs == [], f"historical backlog still raised as an error: {errs}"
    out = capsys.readouterr().out
    assert "QC-077: 16 historical quote(s)" in out, out[-800:]
    assert "Accepted backlog" in out


def test_a_current_gap_is_still_reported():
    """The detector must not go silent — that is how it reached 41 unnoticed.

    SEVERITY LOWERED 2026-08-19, substance unchanged. Michael, on the report
    banner this check backs: "this error shouldn't exist / just clear it."
    Every one of these rows is counted in wins, losses and TEU; only the send
    TIME is missing. So it is a recorded WARNING, not an error — and warn(),
    not ok(), because Log.ok merely prints and never reaches qc-result.json,
    which would delete the count from the audit."""
    import qc_selfheal as QS
    log = QS.Log()
    QS.phase_6_rules(log, {"requests": [_undated("fresh", 2)]})
    assert any("QC-077" in w for w in log.warnings), (
        "QC-077 stopped reporting a current undated quote entirely")
    assert not any("QC-077" in e for e in log.errors), (
        "QC-077 is raising an ERROR again on a known, accepted gap")


# ── the banner is gone from the report ────────────────────────────────────

def test_the_report_body_no_longer_renders_the_undated_banner():
    """Michael, 2026-08-19, on "⚠️ 1 recent quote has a rate or carrier but no
    response time…": "this error shouldn't exist / just clear it."

    Second time he has asked. On 2026-08-13 the same instruction bought a
    14-day recency filter instead of the removal he wanted. The banner's own
    text says the row IS counted in the win/loss totals — so the only thing
    missing is WHEN OL quoted, which is turnaround detail on a report whose
    job is wins and losses.

    The FUNCTION stays and QC-077 stays: a silent detector is how the count
    reached 41 unnoticed, and the audit still records it. Only the render is
    gone."""
    src = (ROOT / "scripts" / "gen_email.py").read_text(encoding="utf-8")
    code = "\n".join(ln for ln in src.splitlines()
                     if not ln.lstrip().startswith("#"))
    assert "{_undated_quotes_note(undated_quotes)}" not in code, (
        "the undated-quote banner is being rendered into the report body "
        "again — Michael has asked for it gone twice")


def test_the_detector_itself_is_not_deleted():
    """Removing the banner must not become removing the check. The count is
    what let anyone notice the gap had reached 41 in the first place."""
    import gen_email as GE
    assert callable(GE._undated_quotes_note)
    assert callable(GE.undated_quotes)
