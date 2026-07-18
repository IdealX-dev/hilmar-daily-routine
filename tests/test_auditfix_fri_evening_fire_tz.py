"""Four proven defects from production run 29174327034 (the Friday-evening
~8:50 PM ET fire, whose UTC clock had already rolled into Saturday).

CALENDAR NOTE — the dates below are pinned to the proleptic Gregorian
calendar Python uses (July 4 2026 is a Saturday, so Friday that week is
2026-07-10 and 2026-07-11/12 are the weekend). The scenario shapes locked
here are the ones the incident exposed: an evening-ET fire whose UTC date
has already rolled forward, the midnight–6 AM ET wee-hours rollback, and
the weekend→Friday roll.

  1. Client email subject dated the wrong day — build_subject/build_body now
     derive the report day from ONE injectable ET instant through
     gen_email._report_date → core.report_business_day, exactly like the
     staff email (never a second wall-clock read, never a UTC date).
  2. Weekly summary skipped a Friday-evening fire — the Friday gate is now
     evaluated on the ET fire day (wee-hours aware), never the runner's
     UTC/local date. --force keeps its override.
  3. patch_carriers: the literal "Unknown" POD placeholder is treated as
     absent; PASS 2b + the LANE-DIAG breadcrumb run even when the row's
     bodies parse to nothing (the old `if not parsed: continue`
     short-circuit suppressed both); pdf_parser stops emitting the literal
     "Unknown" at the source.
  4. Client reply-speed metric counts the PACIFIC business window
     (Michael 2026-07-11: "lonny is uswc and we are usec") —
     core.biz_hours_between_pt, byte-mirrored in src/hilmar/core.py per
     QC-040, while biz_hours_between (ET) stays the staff desk SLA.
"""
from __future__ import annotations

import inspect
import json
import sys
from datetime import datetime, timezone
from datetime import time as dtime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

import core  # noqa: E402
import gen_client_email as gce  # noqa: E402
import gen_email as GE  # noqa: E402
import gen_weekly_summary as GWS  # noqa: E402
import patch_carriers as PC  # noqa: E402
import pdf_parser as PDF  # noqa: E402

from hilmar import core as lib_core  # noqa: E402

UTC = timezone.utc

#: Friday 2026-07-10, 8:53 PM ET — the UTC stamp is ALREADY Saturday Jul 11.
FRI_EVENING_UTC_SHIFTED = datetime(2026, 7, 11, 0, 53, tzinfo=UTC)
#: Friday 2026-07-10, 00:40 ET (04:40Z) — the wee-hours case.
FRI_WEE_HOURS = datetime(2026, 7, 10, 4, 40, tzinfo=UTC)
#: Saturday 2026-07-11, 8:53 PM ET (00:53Z Sunday) — a true weekend fire.
SAT_EVENING = datetime(2026, 7, 12, 0, 53, tzinfo=UTC)


# ── 1. client subject/body report day ─────────────────────────────────────

def test_client_subject_dates_the_et_business_day_not_the_utc_date():
    """Friday-evening fire: the UTC calendar day (Saturday Jul 11) must
    never leak into the subject — the report day is the ET Friday."""
    subj = gce.build_subject({}, {}, now=FRI_EVENING_UTC_SHIFTED)
    assert "(Jul 10, 2026)" in subj
    assert "Jul 11" not in subj


def test_client_subject_wee_hours_reports_prior_business_day():
    """00:40 ET Friday is Thursday's very-late fire → subject says Thu."""
    subj = gce.build_subject({}, {}, now=FRI_WEE_HOURS)
    assert "(Jul 9, 2026)" in subj


def test_client_subject_weekend_fire_rolls_to_friday():
    subj = gce.build_subject({}, {}, now=SAT_EVENING)
    assert "(Jul 10, 2026)" in subj


@pytest.mark.parametrize("now", [FRI_EVENING_UTC_SHIFTED, FRI_WEE_HOURS, SAT_EVENING])
def test_client_report_day_derives_exactly_like_the_staff_email(now):
    """Subject AND body must carry the staff email's report day for the
    same instant — gen_email._report_date with a real ET now."""
    staff_day = GE._report_date(now.astimezone(core.ET))
    short = GE._fmt_date(datetime.combine(staff_day, datetime.min.time()),
                         "%b %-d, %Y")
    long = GE._fmt_date(datetime.combine(staff_day, datetime.min.time()),
                        "%A, %B %-d, %Y")
    assert short in gce.build_subject({}, {}, now=now)
    assert long in gce.build_body({"requests": []}, {}, now=now)


# ── 2. weekly summary MONDAY gate (2026-07-16: Monday 5 AM ET, prev week) ──
# Week of 2026-07-13: Mon=07-13, Tue=07-14 … Fri=07-17, Sat=07-18, Sun=07-19.

def test_weekly_gate_runs_on_monday_morning_et():
    """Monday ~5 AM ET fires the weekly (for the previous week)."""
    # 2026-07-13 09:07 UTC = 5:07 AM EDT Monday.
    assert GWS.should_generate(now=datetime(2026, 7, 13, 9, 7, tzinfo=UTC)) is True


def test_weekly_gate_wee_hours_monday_is_NOT_rolled_back_to_sunday():
    """A legitimate 5 AM Monday fire must NOT roll back to Sunday (the old
    <6 AM wee-hours rule is gone for the weekly). 01:00 ET Monday = Monday."""
    # 2026-07-13 05:00 UTC = 1:00 AM EDT Monday.
    assert GWS.should_generate(now=datetime(2026, 7, 13, 5, 0, tzinfo=UTC)) is True


def test_weekly_gate_skips_a_non_monday_et():
    # 2026-07-15 (Wednesday) afternoon ET.
    assert GWS.should_generate(now=datetime(2026, 7, 15, 18, 0, tzinfo=UTC)) is False


def test_weekly_gate_friday_skips_and_force_overrides():
    fri = datetime(2026, 7, 17, 18, 0, tzinfo=UTC)   # Friday ET
    assert GWS.should_generate(now=fri) is False
    assert GWS.should_generate(now=fri, force=True) is True


def test_weekly_main_skip_message_names_the_et_day(capsys):
    rc = GWS.main([], now=datetime(2026, 7, 15, 18, 0, tzinfo=UTC))  # Wed ET
    assert rc == 0
    out = capsys.readouterr().out
    assert "Wednesday" in out and "skipping" in out


def test_weekly_main_force_generates_even_off_monday(tmp_path, monkeypatch):
    data = tmp_path / "tracking-data-v2.json"
    data.write_text(json.dumps({"requests": []}), encoding="utf-8")
    monkeypatch.setattr(GWS, "DATA", data)
    monkeypatch.setattr(GWS, "REPORTS", tmp_path / "reports")
    rc = GWS.main(["--force"], now=datetime(2026, 7, 15, 18, 0, tzinfo=UTC))
    assert rc == 0
    assert (tmp_path / "reports" / "weekly-summary.html").exists()


# ── 3. POD "Unknown" literal + PASS 2b short-circuit ──────────────────────

@pytest.mark.parametrize("placeholder", ["Unknown", "unknown", "UNKNOWN",
                                         "  Unknown  ", "", None])
def test_dest_from_pod_treats_unknown_literal_as_absent(placeholder):
    assert PC._dest_from_pod(placeholder) is None


def test_dest_from_pod_still_maps_real_ports():
    assert PC._dest_from_pod("TOKYO,JAPAN") == "Tokyo"
    assert PC._dest_from_pod("Yokohama") == "Yokohama"


def test_dest_from_row_pod_falls_through_unknown_to_parsed_aliases():
    """A row that stored the placeholder recovers from the parse's
    POD-shaped fields; 'pol' is the ORIGIN and never a candidate."""
    assert PC._dest_from_row_pod(
        {"pod": "Unknown"}, {"port_of_discharge": "YOKOHAMA,JAPAN"}) == "Yokohama"
    assert PC._dest_from_row_pod({"pod": "Unknown"}, {"pol": "Tokyo"}) is None
    assert PC._dest_from_row_pod({}, {}) is None


def _summary_stub():
    return {"wins": 0, "quoted_lost": 0, "not_quoted": 0, "pending_hilmar": 0,
            "win_rate": 0.0, "quote_rate": 0.0, "teu_requested": 0,
            "teu_won": 0, "teu_quoted_lost": 0, "teu_not_quoted": 0,
            "teu_pending": 0, "total_entries": 2}


def test_pass2b_diagnoses_and_recovers_even_when_bodies_parse_to_nothing(
        tmp_path, monkeypatch, capsys):
    """The run-29174327034 gap: stand_260905 stayed unresolved with NO
    LANE-DIAG line because `if not parsed: continue` skipped PASS 2b for
    rows whose bodies parse to nothing. Both rows here have no parseable
    sources at all — the placeholder row must still emit LANE-DIAG, and a
    row whose own pod is mappable must still recover its lane."""
    rows = [
        {"request_id": "stand_260905", "status": "WIN", "quoted": True,
         "carrier_won": "ONE", "destination": "Unknown",
         "lane": "Lane unresolved", "pod": "Unknown", "source_imids": []},
        {"request_id": "stand_260906", "status": "WIN", "quoted": True,
         "carrier_won": "ONE", "destination": "Unknown",
         "lane": "Lane unresolved", "pod": "YOKOHAMA,JAPAN",
         "source_imids": []},
    ]
    data_path = tmp_path / "tracking-data-v2.json"
    data_path.write_text(json.dumps(
        {"version": "2", "requests": rows, "summary": _summary_stub()}),
        encoding="utf-8")
    monkeypatch.setattr(PC, "ROOT", tmp_path)  # no stage files → parsed == {}
    monkeypatch.setattr(PC.C, "load_config",
                        lambda *a, **k: {"paths": {"data": str(data_path)}})
    PC.main()
    out = capsys.readouterr().out
    assert "LANE-DIAG stand_260905" in out, out
    assert "pod=Unknown" in out
    assert "Oakland → Yokohama" in out          # PASS 2b recovery ran
    saved = {r["request_id"]: r
             for r in json.loads(data_path.read_text(encoding="utf-8"))["requests"]}
    assert saved["stand_260906"]["destination"] == "Yokohama"
    assert saved["stand_260906"]["lane"] == "Oakland → Yokohama"
    assert saved["stand_260905"]["destination"] == "Unknown"  # stays honest


_PDF_TEXT = """BOOKING CONFIRMATION MDOLX260905
Place of Receipt Port of Loading 7/20/2026 Vessel and Voyage No.
OAKLAND ONE OLYMPUS / 080W
Port of Discharge 8/2/2026 Place of Delivery
{pod_cell}
"""


def _parse_synthetic(monkeypatch, tmp_path, text):
    monkeypatch.setattr(PDF, "_PDFPLUMBER_OK", True)
    monkeypatch.setattr(PDF, "_extract_pdf_text", lambda p: text)
    f = tmp_path / "synthetic.pdf"
    f.write_bytes(b"%PDF-1.4")
    return PDF.parse_booking_pdf(f, allow_llm=False)


def test_pdf_parser_never_emits_literal_unknown_pod(monkeypatch, tmp_path):
    out = _parse_synthetic(monkeypatch, tmp_path,
                           _PDF_TEXT.format(pod_cell="UNKNOWN"))
    assert "pod" not in out
    assert "Unknown" not in [v for v in out.values() if isinstance(v, str)]
    # The rest of the parse still lands — the guard drops ONLY the placeholder.
    assert out.get("pol") == "Oakland"
    assert out.get("etd_offered") == "2026-07-20"
    assert out.get("vessel_voyage") == "ONE OLYMPUS 080W"


def test_pdf_parser_real_pod_still_extracts(monkeypatch, tmp_path):
    out = _parse_synthetic(
        monkeypatch, tmp_path,
        _PDF_TEXT.format(pod_cell="YOKOHAMA,JAPAN Closing Date: 7/15/2026 16:00"))
    assert out.get("pod") == "Yokohama"
    assert out.get("eta_offered") == "2026-08-02"


# ── 4. PT-window reply metric (cross-coast) ───────────────────────────────

#: Wed 2026-07-08 4:30 PM PT (7:30 PM ET) → Thu 5:45 AM PT (8:45 AM ET).
REQ = datetime(2026, 7, 8, 23, 30, tzinfo=UTC)
RESP = datetime(2026, 7, 9, 12, 45, tzinfo=UTC)


def test_et_window_cross_coast_overnight_value():
    """Derivation against the REAL constants (BIZ_START 8:30, BIZ_END 17:30):
    Wed 7:30 PM ET is after the ET close → 0h Wednesday; Thursday credits
    open 8:30 → 8:45 AM ET = 0.25h. Total ET-window = 0.25."""
    assert dtime(8, 30) == core.BIZ_START and dtime(17, 30) == core.BIZ_END
    assert core.biz_hours_between(REQ, RESP) == 0.25


def test_pt_window_cross_coast_overnight_value():
    """PT window: Wed 4:30 → 5:30 PM PT = 1.0h; Thu 5:45 AM PT is before
    the PT open → 0h Thursday. Total PT-window = 1.0 — Lonny's actual
    experienced wait."""
    assert core.biz_hours_between_pt(REQ, RESP) == 1.0


def test_pt_window_none_guards_match_et_semantics():
    assert core.biz_hours_between_pt(None, RESP) is None
    assert core.biz_hours_between_pt(RESP, REQ) is None  # end <= start


def test_pt_window_parity_and_byte_parity_across_trees():
    """QC-040: core.py is PAIRED — behavior AND source must match."""
    assert (lib_core.biz_hours_between_pt(REQ, RESP)
            == core.biz_hours_between_pt(REQ, RESP) == 1.0)
    assert (lib_core.biz_hours_between(REQ, RESP)
            == core.biz_hours_between(REQ, RESP) == 0.25)
    for fn in ("_biz_hours_between_window", "biz_hours_between",
               "biz_hours_between_pt"):
        assert inspect.getsource(getattr(core, fn)) == \
            inspect.getsource(getattr(lib_core, fn)), f"{fn} drifted between trees"


def test_et_callers_semantics_unchanged_by_refactor():
    """biz_hours_between still counts the ET desk window exactly as before
    the shared-loop refactor (mid-day ET sanity: 10 AM → 12 PM = 2.0h)."""
    a = datetime(2026, 7, 9, 14, 0, tzinfo=UTC)   # 10:00 AM ET Thu
    b = datetime(2026, 7, 9, 16, 0, tzinfo=UTC)   # 12:00 PM ET Thu
    assert core.biz_hours_between(a, b) == 2.0


def test_same_day_share_pt_calendar_day():
    overnight = {"request_timestamp": "2026-07-08T23:30:00Z",
                 "response_timestamp": "2026-07-09T12:45:00Z", "ol_rate": 3000.0}
    midday = {"request_timestamp": "2026-07-09T17:00:00Z",     # 10:00 AM PT
              "response_timestamp": "2026-07-09T19:00:00Z",    # 12:00 PM PT
              "ol_rate": 3100.0}
    avg, same, n = gce._pt_reply_stats([overnight, midday])
    assert (same, n) == (1, 2)                    # overnight pair ≠ same PT day
    assert avg == pytest.approx((1.0 + 2.0) / 2)  # PT hours: 1.0 and 2.0
    _avg, same2, n2 = gce._pt_reply_stats([midday])
    assert (same2, n2) == (1, 1)                  # mid-day pair IS same PT day
    assert gce._pt_reply_stats([{"ol_rate": 2900.0}]) is None  # no timestamps


def test_client_narrative_renders_pt_metric_from_timestamps():
    """Deterministic render: Thu 2026-07-09 evening fire; two quotes today.
    The stored ET metric (99.0) must not leak into the narrative."""
    now = datetime(2026, 7, 9, 22, 0, tzinfo=UTC)   # Thu Jul 9, 6:00 PM ET
    rows = [
        {"request_id": "r-a", "status": "PENDING", "quoted": True,
         "request_date": "2026-07-08",
         "request_timestamp": "2026-07-08T23:30:00Z",
         "response_timestamp": "2026-07-09T12:45:00Z",
         "ol_rate": 3000.0, "carrier_quoted": "MSC",
         "turnaround_biz_hours": 99.0,   # ET desk metric — must NOT drive this
         "lane": "Oakland → Tokyo", "teu_requested": 2, "status_history": []},
        {"request_id": "r-b", "status": "PENDING", "quoted": True,
         "request_date": "2026-07-09",
         "request_timestamp": "2026-07-09T17:00:00Z",
         "response_timestamp": "2026-07-09T19:00:00Z",
         "ol_rate": 3100.0, "carrier_quoted": "ONE",
         "lane": "Oakland → Busan", "teu_requested": 2, "status_history": []},
    ]
    html = gce.build_body({"requests": rows}, {}, now=now)
    assert "1 of 2 the same business day" in html
    assert "(average 1.5 business hours, Pacific)" in html
    assert "(average 99" not in html
    # All-same-day phrasing when every pair lands on one PT calendar day.
    html2 = gce.build_body({"requests": [rows[1]]}, {}, now=now)
    assert "all the same business day (average 2.0 business hours, Pacific)" in html2
