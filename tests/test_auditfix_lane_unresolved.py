"""Zero 'Lane unresolved' in the client-visible daily sections (2026-07-09).

The Jul 9 staff email showed 'Lane unresolved' rows in New Requests AND
OL-USA Responses (Michael: "there should be zero lane unresolved"). Both
were the SAME failure: an unmatched booking confirmation whose SUBJECT had
no lane shape became a standalone WIN with destination Unknown, and stand_*
rows were rendering in sections they don't belong to.

Three fixes, locked here:
  1. The standalone builder falls back to the booking BODY's parsed
     destination (then POD) before giving up — the body had parsed fine
     (product/temp were visible in the very same row).
  2. stand_* rows no longer render in New Requests / OL Responses (they are
     neither a Lonny ask nor a rate quote); they surface in STATUS CHANGES
     via a real PENDING→WIN history entry.
  3. QC-015 ERRORs when a TODAY-dated row would render 'Lane unresolved'
     (the "your qc isn't working" gap — the old check only counted the
     historical unmapped tail).
Plus the lane tables now carry the Offered (# · TEU) denominator (Michael:
"shows percentages but doesn't show total teus and shipments up for offer").
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import core  # noqa: E402
import gen_email as GE  # noqa: E402
import ingest  # noqa: E402
import qc_selfheal as q  # noqa: E402


def _booking(subject, body_parsed=None, sent="2026-07-09T21:42:00Z"):
    return {"sent": sent, "subject": subject, "body_parsed": body_parsed or {},
            "source_imid": "im1", "source_id": "id1",
            "source_bucket": "mbd_inbound", "body_signer": "Ryan Gordon"}


# ── 1. builder falls back to the body's destination / POD ─────────────────
def test_standalone_uses_body_destination_when_subject_has_no_lane():
    _, stands = ingest.link_bookings_to_requests(
        [], {"260900": _booking("MDOLX260900_NEW BOOKING CONFIRMATION // HILMAR chilled",
                                {"destination": "Tokyo", "product": "Cheese"})})
    assert len(stands) == 1
    assert stands[0]["destination"] == "Tokyo"
    assert stands[0]["lane"] == "Oakland → Tokyo"


def test_standalone_uses_body_pod_as_second_fallback():
    _, stands = ingest.link_bookings_to_requests(
        [], {"260901": _booking("MDOLX260901_NEW BOOKING // HILMAR",
                                {"pod": "Yokohama"})})
    assert stands[0]["destination"] == "Yokohama"
    assert stands[0]["lane"] == "Oakland → Yokohama"


def test_standalone_still_honest_when_nothing_has_a_port():
    _, stands = ingest.link_bookings_to_requests(
        [], {"260902": _booking("MDOLX260902_NEW BOOKING // HILMAR", {})})
    assert stands[0]["destination"] == "Unknown"
    assert stands[0]["lane"] == "Lane unresolved"


def test_standalone_gets_a_status_history_entry():
    """The PENDING→WIN entry is what surfaces a new standalone in the daily
    STATUS CHANGES section — its correct home."""
    _, stands = ingest.link_bookings_to_requests(
        [], {"260903": _booking("MDOLX260903_NEW BOOKING // HILMAR",
                                {"destination": "Busan"})})
    h = stands[0]["status_history"]
    assert len(h) == 1 and h[0]["from"] == "PENDING" and h[0]["to"] == "WIN"
    assert "260903" in h[0]["reason"]


# ── 2. stand_* rows leave New Requests / OL Responses ─────────────────────
def test_today_events_exclude_standalones_from_request_and_response_tables():
    from datetime import date
    today = "2026-07-09"
    stand = {"request_id": "stand_260900", "status": "WIN",
             "request_date": today, "response_timestamp": f"{today}T21:42:00Z",
             "status_history": [{"at": f"{today}T21:42:00Z",
                                 "from": "PENDING", "to": "WIN",
                                 "reason": "MDOLX260900 standalone booking confirmation"}]}
    real = {"request_id": "req_abc", "status": "PENDING",
            "request_date": today, "response_timestamp": None,
            "status_history": []}
    new_req, ol_resp, status_ch, _pend = GE._today_events(
        {"requests": [stand, real]}, date(2026, 7, 9))
    assert stand not in new_req and stand not in ol_resp
    assert real in new_req
    # ...but the standalone's WIN still shows in Status Changes.
    assert any(r is stand for r, _h in status_ch)


# ── 3. QC-015 screams on a TODAY-dated unresolved lane ─────────────────────
def test_qc015_errors_on_fresh_unresolved_lane(monkeypatch):
    monkeypatch.setenv("HILMAR_QC_NO_PIP", "1")
    today = core.report_business_day(datetime.now(core.ET)).isoformat()
    row = {"request_id": "stand_260904", "status": "WIN",
           "destination": "Unknown", "lane": "Lane unresolved",
           "request_date": today, "quoted": True}
    data = {"version": "2", "requests": [row],
            "summary": {"wins": 0, "quoted_lost": 0, "not_quoted": 0,
                        "pending_hilmar": 0, "win_rate": 0.0, "quote_rate": 0.0,
                        "teu_requested": 0, "teu_won": 0, "teu_quoted_lost": 0,
                        "teu_not_quoted": 0, "teu_pending": 0, "total_entries": 0}}
    log = q.Log()
    q.phase_6_rules(log, data)
    assert any("QC-015" in m and "Lane unresolved" in m for m in log.errors), log.errors


def test_qc015_stays_quiet_for_old_unresolved_rows(monkeypatch):
    """The historical Unknown tail keeps its tolerance — only TODAY-dated
    rows (which render in the daily sections) escalate to ERROR."""
    monkeypatch.setenv("HILMAR_QC_NO_PIP", "1")
    row = {"request_id": "stand_old", "status": "WIN",
           "destination": "Unknown", "lane": "Lane unresolved",
           "request_date": "2026-05-06", "quoted": True}
    data = {"version": "2", "requests": [row],
            "summary": {"wins": 0, "quoted_lost": 0, "not_quoted": 0,
                        "pending_hilmar": 0, "win_rate": 0.0, "quote_rate": 0.0,
                        "teu_requested": 0, "teu_won": 0, "teu_quoted_lost": 0,
                        "teu_not_quoted": 0, "teu_pending": 0, "total_entries": 0}}
    log = q.Log()
    q.phase_6_rules(log, data)
    assert not any("QC-015" in m for m in log.errors), log.errors


# ── 4. lane tables carry the Offered denominator ───────────────────────────
def test_lane_buckets_accumulate_total_teu_offered():
    data = {"requests": [
        {"lane": "Oakland → Tokyo", "status": "WIN", "teu_requested": 8, "teu_won": 8},
        {"lane": "Oakland → Tokyo", "status": "LOSS", "quoted": True,
         "loss_reason": "PRICE", "teu_requested": 4},
        {"lane": "Oakland → Tokyo", "status": "PENDING", "teu_requested": 2},
    ]}
    b = GE._build_lane_buckets(data)["Oakland → Tokyo"]
    assert b["total"] == 3
    assert b["teu_req"] == 14


def test_lane_tables_render_offered_column():
    src = (ROOT / "scripts" / "gen_email.py").read_text(encoding="utf-8")
    assert src.count("Offered (# · TEU)") >= 4  # header + row micro-label × 2 tables


# ── 5. FIX 1 (2026-07-14, run 29292014093): phase_3 heals poisoned literals ─
def test_phase3_heals_poisoned_pod_destination_origin_to_none():
    """A row persisted with the LITERAL 'Unknown'/'N/A'/… in pod/destination/
    origin has that field REMOVED at entry-heal, BEFORE lane derivation —
    killing the recurring 'pod=Unknown re-derives unresolved every fire' drift
    at the source. A real port is never touched.

    Changed 2026-07-27 from `= None` to `pop()`. Setting the key to None left
    it present-but-null, so every downstream `r.get("origin", "Oakland")`
    default was bypassed (`.get` only substitutes when the key is ABSENT) and
    the value rendered as the literal string "None" — the client PDF's Lane
    Performance table shipped a row labelled "None → Tokyo", strictly worse
    than the "Unknown → Tokyo" this heal replaced."""
    data = {"requests": [
        {"request_id": "r-poison", "subject": "HILMAR rate request",
         "pod": "Unknown", "destination": "unknown", "origin": "N/A",
         "status": "PENDING"},
        {"request_id": "r-clean", "subject": "HILMAR rate request",
         "pod": "Yokohama", "destination": "Tokyo", "origin": "Oakland",
         "status": "PENDING"},
    ]}
    log = q.Log()
    q.phase_3_entries(log, data)
    poison = next(r for r in data["requests"] if r["request_id"] == "r-poison")
    # Absent, not None — so `.get(key, default)` engages downstream.
    assert "pod" not in poison
    assert "destination" not in poison
    assert "origin" not in poison
    assert poison.get("origin", "Oakland") == "Oakland"
    assert any("poisoned placeholder" in f and "r-poison" in f for f in log.fixes)
    clean = next(r for r in data["requests"] if r["request_id"] == "r-clean")
    assert clean["destination"] == "Tokyo" and clean["pod"] == "Yokohama"


def test_phase3_placeholder_heal_mirrored_in_src_hilmar():
    """QC-040 spirit: the paired src/hilmar/qc.phase_3_entries heals the same
    placeholder literals so the two trees can't drift."""
    sys.path.insert(0, str(ROOT / "src"))
    from hilmar import qc as hq
    data = {"requests": [
        {"request_id": "r-poison", "subject": "HILMAR rate request",
         "pod": "TBD", "destination": "None", "origin": "—", "status": "PENDING"},
    ]}
    log = hq.Log()
    hq.phase_3_entries(log, data)
    r0 = data["requests"][0]
    assert r0["pod"] is None and r0["destination"] is None and r0["origin"] is None
    assert any("poisoned placeholder" in f for f in log.fixes)


# ── 6. FIX 3: QC-015 ERRORs on ANY unresolved row that WOULD render ────────
def _qc015_data(row):
    return {"version": "2", "requests": [row],
            "summary": {"wins": 0, "quoted_lost": 0, "not_quoted": 0,
                        "pending_hilmar": 0, "win_rate": 0.0, "quote_rate": 0.0,
                        "teu_requested": 0, "teu_won": 0, "teu_quoted_lost": 0,
                        "teu_not_quoted": 0, "teu_pending": 0, "total_entries": 0}}


def test_qc015_errors_on_win_inside_active_window_not_today(monkeypatch):
    """A WIN dated 5 days back (inside the client email's 14-day active-
    shipments window) with an unresolved lane ERRORs even though it is NOT
    today-dated — the old check only caught today-dated rows and otherwise
    printed GREEN 'within tolerance' while the row shipped to Lonny."""
    monkeypatch.setenv("HILMAR_QC_NO_PIP", "1")
    d5 = (core.report_business_day(datetime.now(core.ET))
          - timedelta(days=5)).isoformat()
    row = {"request_id": "stand_win5", "status": "WIN", "destination": None,
           "lane": "Lane unresolved", "request_date": d5,
           "response_timestamp": f"{d5}T18:00:00Z", "quoted": True}
    log = q.Log()
    q.phase_6_rules(log, _qc015_data(row))
    assert any("QC-015" in m and "Lane unresolved" in m for m in log.errors), log.errors


def test_qc015_errors_on_healed_none_destination_today(monkeypatch):
    """After FIX 1 nulls the poisoned 'Unknown', destination is None; the
    today-dated unresolved WIN must still ERROR (destination-None counts as
    unresolved, not just the literal 'Unknown')."""
    monkeypatch.setenv("HILMAR_QC_NO_PIP", "1")
    today = core.report_business_day(datetime.now(core.ET)).isoformat()
    row = {"request_id": "stand_none", "status": "WIN", "destination": None,
           "lane": "Lane unresolved", "request_date": today, "quoted": True}
    log = q.Log()
    q.phase_6_rules(log, _qc015_data(row))
    assert any("QC-015" in m for m in log.errors), log.errors


def test_qc015_win_outside_window_stays_quiet(monkeypatch):
    """A WIN 40 days back is a non-rendered historical-tail row — QC-015 keeps
    its count tolerance and does NOT ERROR."""
    monkeypatch.setenv("HILMAR_QC_NO_PIP", "1")
    d40 = (core.report_business_day(datetime.now(core.ET))
           - timedelta(days=40)).isoformat()
    row = {"request_id": "stand_old40", "status": "WIN", "destination": "Unknown",
           "lane": "Lane unresolved", "request_date": d40, "quoted": True}
    log = q.Log()
    q.phase_6_rules(log, _qc015_data(row))
    assert not any("QC-015" in m for m in log.errors), log.errors
