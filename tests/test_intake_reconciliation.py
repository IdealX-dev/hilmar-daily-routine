"""QC-057 — staged Lonny RFQ silently dropped at intake.

ingest.build_requests drops any lonny_outbound email whose subject (and body)
yields no parseable destination ("if not destination: skipped_ops += 1;
continue"), bumping a counter it never logs — so a real rate request can vanish
from the report with NO alarm. That intake blind spot is exactly what hid the
2026-06-24 "Busan Korea from Dalhart" miss for a week (no check reconciled
staged RFQs vs built rows). QC-057 reconciles the two and surfaces the dropped
subjects: WARN for 1-2, ERROR at >=3 (systemic parser regression).

These tests drive the real _intake_reconciliation helper and the real
phase_6_rules() (with a monkeypatched stage) so the QC-057 log path is covered.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import ingest  # noqa: E402
import qc_selfheal as q  # noqa: E402

# A genuinely unparseable real-RFQ subject — NOT the Busan/Korea one, which
# PR #57 now parses. Uses a lane shape parse_subject_lane still can't resolve
# (no "to", no "from", no recognizable origin token) so the drop is real.
_UNPARSEABLE = "Pricing needed asap for the usual move pls"


def _row(bucket, subject, imid="i0"):
    return {"bucket": bucket, "subject": subject, "imid": imid,
            "summary_preview": "2-40 HC reefer"}


def test_helper_flags_only_the_silently_dropped_rfq():
    stage = [
        _row("lonny_outbound", "Oakland to Yokohama 2x40HC", "i1"),       # resolves -> kept
        _row("lonny_outbound", "Updated Cheese Rates Busan Korea from Dalhart", "i2"),  # PR#57 resolves
        _row("lonny_outbound", "RE: FREE-TIME ISSUE MDOLX260062", "i3"),  # operational -> not counted
        _row("lonny_outbound", "FTL Modesto CA to Sturgis MI", "i4"),     # out-of-scope (trucking)
        _row("lonny_outbound", _UNPARSEABLE, "i5"),                       # real RFQ, no dest -> DROPPED
        _row("lonny_reply", _UNPARSEABLE, "i6"),                          # wrong bucket -> ignored
    ]
    expected, dropped = ingest_reconcile(stage)
    # Only the three genuine rate asks count as "expected" (i1, i2, i5);
    # operational + out-of-scope + the reply are excluded.
    assert expected == 3, (expected, dropped)
    assert dropped == [_UNPARSEABLE], dropped


def ingest_reconcile(stage, bodies=None):
    return q._intake_reconciliation(stage, bodies or {})


def test_body_destination_rescues_an_unparseable_subject():
    # A subject that won't parse but whose fetched body resolves the lane must
    # NOT be reported as dropped (mirrors ingest's `or parsed.destination`).
    stage = [_row("lonny_outbound", _UNPARSEABLE, "i7")]
    bodies = {"i7": {"parsed": {"destination": "Busan"}, "text_body": ""}}
    expected, dropped = ingest_reconcile(stage, bodies)
    assert expected == 1
    assert dropped == [], dropped


def _fired(monkeypatch, tmp_path, stage, bodies=None):
    # QC-057 gates on ingest.STAGE_PATH.exists(); point it at a real temp file
    # and patch the loaders to return the fixture instead of reading disk.
    fake = tmp_path / "stage_emails.txt"
    fake.write_text("", encoding="utf-8")
    monkeypatch.setattr(ingest, "STAGE_PATH", fake)
    monkeypatch.setattr(ingest, "load_stage", lambda: stage)
    monkeypatch.setattr(ingest, "load_bodies_index", lambda: bodies or {})
    log = q.Log()
    data = {"version": "2", "requests": [],
            "summary": {"wins": 0, "quoted_lost": 0, "not_quoted": 0,
                        "pending_hilmar": 0, "win_rate": 0.0, "quote_rate": 0.0,
                        "teu_requested": 0, "teu_won": 0, "teu_quoted_lost": 0,
                        "teu_not_quoted": 0, "teu_pending": 0, "total_entries": 0}}
    q.phase_6_rules(log, data)
    return log.warnings, log.errors


def test_qc057_warns_on_single_drop(monkeypatch, tmp_path):
    warns, errors = _fired(monkeypatch, tmp_path,
                           [_row("lonny_outbound", _UNPARSEABLE, "i8")])
    assert any("QC-057" in m for m in warns), warns
    assert not any("QC-057" in m for m in errors), errors


def test_qc057_errors_when_systemic(monkeypatch, tmp_path):
    stage = [_row("lonny_outbound", f"{_UNPARSEABLE} {n}", f"i{n}") for n in range(3)]
    warns, errors = _fired(monkeypatch, tmp_path, stage)
    assert any("QC-057" in m for m in errors), errors


def test_qc057_silent_when_all_resolve(monkeypatch, tmp_path):
    stage = [_row("lonny_outbound", "Oakland to Yokohama", "i9"),
             _row("lonny_outbound", "Chicago to HCMC", "i10")]
    warns, errors = _fired(monkeypatch, tmp_path, stage)
    assert not any("QC-057" in m for m in warns + errors), (warns, errors)


# ── QC-057-DIAG: scrubbed body diagnostics for dropped RFQs ──────────
# QC-057 names WHICH subjects dropped; the root fix (a parser extension)
# needs the lane-bearing body text the parser failed on. The diag helper
# surfaces it, PII-scrubbed, in the run log + audit email.

def test_drop_diag_surfaces_lane_bearing_body_lines():
    stage = [_row("lonny_outbound", "REEFER NEEDS", "d1")]
    bodies = {"d1": {"parsed": {}, "text_body": (
        "Hi team,\n"
        "Need 5x40 HC reefers to Singapore, ETD week of 8/1\n"
        "Also 2x40 to Algeciras with 14d free time\n"
        "Thanks, Lonny\n"
        "lupfold@hilmaringredients.com\n")}}
    diags = q._intake_drop_diag(stage, bodies)
    assert len(diags) == 1
    d = diags[0]
    assert d["subject"] == "REEFER NEEDS"
    assert d["has_body"] is True
    assert "Singapore" in d["snippet"]
    assert "Algeciras" in d["snippet"]
    # PII scrubbed: the email address must not survive into the snippet.
    assert "lupfold@hilmaringredients.com" not in d["snippet"]
    # Non-lane chatter is filtered out by the line hints.
    assert "Hi team" not in d["snippet"]


def test_drop_diag_skips_resolved_and_out_of_scope_rows():
    stage = [
        _row("lonny_outbound", "Oakland to Yokohama 2x40HC", "d2"),  # resolves
        _row("lonny_outbound", "FTL Modesto CA to Sturgis MI", "d3"),  # trucking
        _row("lonny_reply", _UNPARSEABLE, "d4"),                     # wrong bucket
        _row("lonny_outbound", _UNPARSEABLE, "d5"),                  # the drop
    ]
    diags = q._intake_drop_diag(stage, {})
    assert [d["subject"] for d in diags] == [_UNPARSEABLE]
    assert diags[0]["has_body"] is False
    assert "no body cached" in diags[0]["snippet"]


def test_qc057_diag_lines_reach_the_log(monkeypatch, tmp_path):
    bodies = {"i8": {"parsed": {}, "text_body": "3x40 HC reefers to Osaka pls"}}
    warns, errors = _fired(monkeypatch, tmp_path,
                           [_row("lonny_outbound", _UNPARSEABLE, "i8")],
                           bodies)
    diag = [m for m in warns if "QC-057-DIAG" in m]
    assert diag, warns
    assert "Osaka" in diag[0]
