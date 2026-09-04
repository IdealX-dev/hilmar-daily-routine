"""QC-019 must measure the state that SHIPS, not the state before the heals.

HILMAR-DAILY-TRACKER-3 fired 75 times between 2026-05-17 and 2026-09-04:

    QC-019: 1 status-change(s) on 2026-09-03 have no carrier — parser missed
    extraction. Rows: 20:52:29 Oakland → Busan

Sentry's Seer diagnosed it as "body_parser.py fails to extract the carrier
from the pipe-table or prose". That is REFUTED by execution. Reproduced with
the row shape from production fire 33864188808 — a stand_ WIN carrying
ol_rate 2600, lane "Oakland → Busan" and vessel_voyage "HMM PROMISE 0091W" —
driving the real phase_6_rules():

    🔴 ERROR: QC-019: ... have no carrier — parser missed extraction
    🔧 FIX:   QC-056: backfilled carrier from row text — Oakland → Busan=HMM
    carrier_quoted after the pass: HMM

One pass, both lines. QC-019 sat ~166 source lines ABOVE QC-056, so it read
the row before the heal that fixes it and reported a defect the same run
repaired. Seventy-five ERRORs about a value the run already had — and the
second Seer misdiagnosis this month (it also blamed the tracking rows for the
180s QC timeout that measurement pinned on reprocessing 4,696 cached bodies).

The check is NOT downgraded. Michael set QC-019 to ERROR on 2026-05-13
("status change of pending to quoted with no carrier and no rate") and a row
that is still carrier-less after every recovery attempt is still an error.
Only WHEN it looks changed, plus wording that matches what it now measures.
"""
from __future__ import annotations

import contextlib
import io
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import core  # noqa: E402
import qc_selfheal as q  # noqa: E402


def _report_iso() -> str:
    """The window QC-019 keys off — core.report_business_day, the same source
    gen_email._report_date uses. Anchoring to it rather than to `now` keeps
    this test from passing or failing on the hour the suite runs."""
    return core.report_business_day(datetime.now(core.ET).date()).isoformat()


def _run(vessel_voyage: str):
    """Drive the real phase_6_rules over one status-changed WIN."""
    row = {
        "request_id": "stand_261134", "status": "WIN", "quoted": True,
        "lane": "Oakland → Busan", "destination": "Busan", "origin": "Oakland",
        "ol_rate": 2600, "teu_requested": 2, "teu_won": 2,
        "vessel_voyage": vessel_voyage,
        "carrier_quoted": None, "carrier_won": None,
        "status_history": [
            {"at": f"{_report_iso()}T20:52:29Z", "from": "PENDING", "to": "WIN"}],
    }
    data = {
        "version": "2", "requests": [row],
        "summary": {
            "wins": 1, "quoted_lost": 0, "not_quoted": 0, "pending_hilmar": 0,
            "win_rate": 0.0, "quote_rate": 0.0, "teu_requested": 2, "teu_won": 2,
            "teu_quoted_lost": 0, "teu_not_quoted": 0, "teu_pending": 0,
            "total_entries": 1,
        },
    }
    log = q.Log()
    with contextlib.redirect_stdout(io.StringIO()):
        q.phase_6_rules(log, data)
    return log, row


def test_qc019_is_silent_when_the_same_pass_recovers_the_carrier():
    """THE production false positive. Fails against the pre-move tree: there
    QC-019 read the row before QC-056 healed it and errored anyway."""
    log, row = _run("HMM PROMISE 0091W")
    assert row["carrier_quoted"] == "HMM", (
        "fixture is wrong — QC-056 must be able to recover this carrier, "
        "otherwise the test proves nothing about ordering")
    assert not [m for m in log.errors if "QC-019" in m], (
        "QC-019 reported a carrier gap the same pass had already filled")


def test_qc019_still_errors_when_no_source_holds_a_carrier():
    """The move must not soften the check. A genuinely blank row is still an
    ERROR, at the severity Michael set on 2026-05-13."""
    log, row = _run("TBD")
    errs = [m for m in log.errors if "QC-019" in m]
    assert errs, "a row that ships with no carrier anywhere must still fail"
    assert row["carrier_quoted"] is None
    assert "no carrier in ANY source" in errs[0], (
        "the message must state what it now measures — 'parser missed "
        "extraction' is what sent Seer to the wrong file")


def test_qc019_names_the_row_not_just_a_timestamp():
    """The 2026-08-27 production event read `Rows: 16:59:21 Lane unresolved`
    — a time and a placeholder, with no way to find the row it meant."""
    log, _ = _run("TBD")
    errs = [m for m in log.errors if "QC-019" in m]
    assert errs and "stand_261134" in errs[0]


def test_qc019_is_positioned_after_the_carrier_heals():
    """STRUCTURAL GUARD. The behaviour above depends entirely on source
    order, which a later edit could innocently undo — the block reads fine in
    either position. Pin it: QC-019 must appear after QC-056's heal.
    """
    src = (ROOT / "scripts" / "qc_selfheal.py").read_text(encoding="utf-8")
    qc056 = src.index("QC-056: backfilled carrier from row text")
    qc019 = src.index(
        "# QC-019: status-change rows on the report date must have carrier_quoted.")
    assert qc019 > qc056, (
        "QC-019 has been moved back above QC-056 — it will resume reporting "
        "carrier gaps that the same pass repairs (75 Sentry events, "
        "HILMAR-DAILY-TRACKER-3)")
