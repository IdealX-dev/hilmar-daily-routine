"""A booking with no message of its own carries nothing parsed (QC-084), and
QC-039 grades cutoffs only where there was something to parse.

HILMAR-DAILY-TRACKER-8: "QC-039: parser accuracy 98.0% overall (weighted
98.8%); 2 non-critical field(s) below 95%: doc_cutoff=89.9%,
port_cutoff=89.3%" — 119 occurrences, 2026-05-21 → 2026-09-02, one WARN per
fire. Two defects in two layers and a receipt defect produced that line.

LAYER 1 — patch_carriers fabricated the cells. The 49 `ol_` wins that
ingest.apply_operator_corrections CREATES from OL's transaction report carry
`source_imids: []` (no email exists at all), a lane, and a request_timestamp
equal to a Jan–Apr SAILING date. PASS 2's sibling lookup fell through
conv → mdolx → LANE ("Oakland->Yokohama", first body wins), and its only
gate — core.quote_evidence_ok: OL sender, sent > request_timestamp — admits
ANY later OL quote on the lane. So a September grid was pasted onto a
January booking: erd, doc_cutoff, port_cutoff, vessel, ETD/ETA, ol_rate.
Rows are preserved-from-prior every fire, so the borrowed cells persisted.

LAYER 2 — parser_accuracy graded rows a parser could never populate.
erd/doc_cutoff/port_cutoff were applicable on EVERY WIN; the 2026-08-13
`ol_` fix reached only the predicates that call `_is_standalone`.

Layer 1 masked Layer 2: honest grading puts doc_cutoff near 67%; borrowing
lifted ≥34 rows to "populated" and landed it at 89.9% — just under the 90%
floor, so the alarm fired daily on numbers that were partly fiction. And the
RECEIPT printed the global 95% for fields whose applied floor is 90%, twice
prefixed ("QC-039: QC-039: ...") by sentry_setup.

Every fixture here is production-shaped: the `ol_` row is built through the
REAL create branch from the REAL correction in scripts/operator_corrections
.json, and the quote is the committed OL grid fixture with its lane words
swapped. Each protection has a test that goes red when it is removed.
"""
from __future__ import annotations

import email
import json
import sys
import types
from email import policy
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for p in (str(ROOT / "scripts"), str(ROOT / "src")):
    if p not in sys.path:
        sys.path.insert(0, p)

import body_parser as BP  # noqa: E402
import core as C  # noqa: E402
import ingest as IN  # noqa: E402
import patch_carriers as PC  # noqa: E402
import qc_selfheal as QS  # noqa: E402
import sentry_setup as SS  # noqa: E402

from hilmar import core as HC  # noqa: E402
from hilmar.parser_accuracy import compute_accuracy  # noqa: E402

QUOTE_SENT = "2026-08-20T17:44:45Z"          # a real OL quote, months after the sailing
QUOTE_IMID = "<other-shipment-quote-2026-08-20@ol-usa.com>"


# ── fixtures: the real correction, the real grid ────────────────────────────

def _real_create_correction() -> dict:
    doc = json.loads((ROOT / "scripts" / "operator_corrections.json").read_text(encoding="utf-8"))
    return next(c for c in doc["corrections"]
                if c.get("create") and c["set"].get("lane") == "Oakland → Yokohama")


def _corrections_file(tmp_path: Path, *corrections) -> Path:
    path = tmp_path / "operator_corrections.json"
    path.write_text(json.dumps({"corrections": list(corrections)}), encoding="utf-8")
    return path


def _ol_row(tmp_path: Path, monkeypatch, corr: dict | None = None) -> dict:
    """ol_252078 exactly as production builds it: the REAL create branch over
    the REAL correction. source_imids == [] by construction."""
    corr = corr or _real_create_correction()
    monkeypatch.setattr(IN, "CORRECTIONS_PATH", _corrections_file(tmp_path, corr))
    rows: list = []
    IN.apply_operator_corrections(rows)
    (row,) = rows
    assert row["request_id"] == "ol_252078" and row["source_imids"] == []
    return row


def _grid_text() -> str:
    """The committed OL grid (Oakland→Algeciras), lane words swapped to the
    ol_ row's lane, as fetch_bodies stores it."""
    with open(ROOT / "tests" / "fixtures" / "ol_quote_algeciras.eml", "rb") as fh:
        msg = email.message_from_binary_file(fh, policy=policy.default)
    html = next(p.get_content() for p in msg.walk() if p.get_content_type() == "text/html")
    html = html.replace("ALGECIRAS", "YOKOHAMA").replace("Algeciras", "Yokohama")
    return BP.html_to_text(html)


def _stage(tmp_path: Path, monkeypatch, bodies: list[dict]) -> None:
    (tmp_path / "scripts").mkdir(exist_ok=True)
    (tmp_path / "scripts" / "stage_emails_bodies.txt").write_text(
        "".join(json.dumps(b) + "\n" for b in bodies), encoding="utf-8")
    monkeypatch.setattr(PC, "ROOT", tmp_path)


def _same_lane_quote(**over) -> dict:
    rec = {"imid": QUOTE_IMID, "bucket": "mbd_rate_response",
           "subject": "RE: Oakland to Yokohama 1x40'HC - HILMAR",
           "sender_email": "linda.echevarria@ol-usa.com",
           "sent_ts": QUOTE_SENT, "text_body": _grid_text()}
    rec.update(over)
    return rec


def _run_patch_carriers(tmp_path: Path, monkeypatch, rows: list[dict], capsys) -> tuple[dict, str]:
    data_path = tmp_path / "tracking-data-v2.json"
    summary = {"wins": 1, "quoted_lost": 0, "not_quoted": 0, "pending_hilmar": 0,
               "win_rate": 100.0, "quote_rate": 100.0, "teu_requested": 1,
               "teu_won": 1, "teu_quoted_lost": 0, "teu_not_quoted": 0,
               "teu_pending": 0, "total_entries": len(rows)}
    data_path.write_text(json.dumps({"version": "2", "requests": rows, "summary": summary}),
                         encoding="utf-8")
    monkeypatch.setattr(PC.C, "load_config",
                        lambda *a, **k: {"paths": {"data": str(data_path)}})
    PC.main()
    out = capsys.readouterr().out
    saved = {r["request_id"]: r
             for r in json.loads(data_path.read_text(encoding="utf-8"))["requests"]}
    return saved, out


def _borrowed(row: dict) -> list[str]:
    return [f for f in C.SOURCE_ONLY_FIELDS if row.get(f) not in (None, "", [], {})]


# ── the predicate and the field list ────────────────────────────────────────

def test_the_predicate_reads_the_rows_own_evidence_list():
    assert C.has_own_source({"source_imids": ["<ask@hilmar>"]}) is True
    assert C.has_own_source({"source_imids": []}) is False
    assert C.has_own_source({}) is False
    assert C.has_own_source(None) is False


def test_the_two_cores_agree_on_it():
    """parser_accuracy reads src/hilmar/core; the fire reads scripts/core.
    One predicate in two trees is how the 2026-08-13 fix reached one surface."""
    assert C.SOURCE_ONLY_FIELDS == HC.SOURCE_ONLY_FIELDS
    for row in ({"source_imids": ["x"]}, {"source_imids": []}, {}, None):
        assert C.has_own_source(row) == HC.has_own_source(row)


def test_source_only_fields_and_backfill_keys_partition_each_other():
    """The heal reads core.SOURCE_ONLY_FIELDS; the writer reads
    patch_carriers.BACKFILL_KEYS. A key added to the writer without being
    classified — parsed-only, or derived-from-the-row — goes red here, so
    the two lists cannot drift apart silently."""
    backfill = set(PC.BACKFILL_KEYS)
    derived = set(PC.ROW_DERIVED_KEYS)
    assert derived <= backfill
    assert backfill - derived == set(C.SOURCE_ONLY_FIELDS) - {"ol_rate"}
    assert "ol_rate" in C.SOURCE_ONLY_FIELDS      # written by PASS 2 outside BACKFILL_KEYS
    # Other heals derive these from the row itself (QC-027 POL/POD, the
    # phase-3 TEU recompute); un-stamping them would start a heal war.
    assert {"pol", "pod", "container_count", "teu_requested", "containers"} == derived


# ── LAYER 1: the writer refuses a source-less row ───────────────────────────

def test_a_source_less_booking_takes_nothing_from_a_same_lane_quote(tmp_path, monkeypatch, capsys):
    """THE production question, on the real code path. ol_252078 sailed
    2026-01-08; the staged quote is a September Oakland→Yokohama grid that
    passes quote_evidence_ok (OL sender, sent > sailing date). Before the
    fix PC.main() wrote 13 field backfills and ol_rate=4938 onto the January
    booking and QC-039 graded it populated."""
    row = _ol_row(tmp_path, monkeypatch)
    quote = _same_lane_quote()
    assert C.quote_evidence_ok(quote["sender_email"], quote["sent_ts"],
                               row["request_timestamp"]), (
        "the fixture must be one the evidence gate ADMITS — the refusal under "
        "test is the source guard, not quote_evidence_ok")
    _stage(tmp_path, monkeypatch, [quote])
    saved, out = _run_patch_carriers(tmp_path, monkeypatch, [row], capsys)
    after = saved["ol_252078"]
    assert _borrowed(after) == [], (
        f"a booking with no message of its own inherited another shipment's "
        f"grid through the lane join: {_borrowed(after)}")
    assert after.get("ol_rate") is None and after["quoted"] is True
    assert "0 rate patches" in out or "Nothing to patch" in out, out
    # What the row legitimately holds is untouched.
    assert after["carrier_won"] == "CMA CGM SA" and after["mdolx_ref"] == "252078"


def test_the_sibling_lookup_itself_returns_nothing_for_a_source_less_row(tmp_path, monkeypatch):
    """Unit form of the guard, all three joins: even an mdolx/conv hit is
    refused, because the QC-084 heal cannot tell a joined cell from a
    borrowed one and would clear it again every pass."""
    row = _ol_row(tmp_path, monkeypatch)
    rec = {"body": _grid_text(), "sender": "linda.echevarria@ol-usa.com", "sent": QUOTE_SENT}
    by_thread = {("lane", "oakland->yokohama"): rec, ("mdolx", "252078"): rec,
                 ("conv", "conv-1"): rec}
    row["conversation_id"] = "conv-1"
    assert PC._find_related_rate_response(row, by_thread) is None
    # The same joins DO serve a row that has a source of its own.
    chain = dict(row, request_id="req_chain", source_imids=["<conf@ol-usa.com>"])
    assert PC._find_related_rate_response(chain, by_thread) == rec["body"]


def test_a_row_with_its_own_source_still_inherits_the_sibling_grid(tmp_path, monkeypatch, capsys):
    """THE NEGATIVE DIRECTION. The fix must be no wider than the defect: a
    booking-confirmation row whose own body is signature-only (the data is
    in the PDF) still inherits ETD/vessel/cutoffs from the same-lane rate
    response — the 2026-05-13 behaviour every chain WIN depends on."""
    conf_imid = "<booking-conf@ol-usa.com>"
    row = {"request_id": "req_chain_win", "status": "WIN", "quoted": True,
           "origin": "Oakland", "destination": "Yokohama",
           "lane": "Oakland → Yokohama", "mdolx_ref": "261199",
           "carrier_won": "CMA CGM", "carrier_quoted": "CMA CGM",
           "request_timestamp": "2026-08-01T15:00:00Z", "request_date": "2026-08-01",
           "source_imids": [conf_imid], "status_history": []}
    conf = {"imid": conf_imid, "bucket": "mbd_booking",
            "subject": "NEW BOOKING CONFIRMATION MDOLX261199 // HILMAR",
            "sender_email": "reno.gurusinghe@ol-usa.com",
            "sent_ts": "2026-08-02T10:00:00Z",
            "text_body": "Booking confirmed, please see the attached confirmation.\n\nThanks,\nReno"}
    _stage(tmp_path, monkeypatch, [conf, _same_lane_quote()])
    saved, _ = _run_patch_carriers(tmp_path, monkeypatch, [row], capsys)
    after = saved["req_chain_win"]
    assert after.get("vessel_voyage") and after.get("etd_offered") and after.get("doc_cutoff"), (
        "a row WITH its own source lost the same-lane enrichment — the guard "
        f"is wider than the defect: {after}")


def test_a_source_less_booking_takes_nothing_from_a_pdf_indexed_by_its_mdolx(tmp_path, monkeypatch, capsys):
    """The second guarded site. The booking-PDF cross-reference joins by
    MDOLX, and an ol_ row carries one — but a row recorded as "no email
    exists at all" has no attachment of its own either, and the QC-084 heal
    would clear a PDF-filled cell again every pass. The PDF layer is stubbed
    at the two seams patch_carriers calls; the row is production's."""
    row = _ol_row(tmp_path, monkeypatch)
    _stage(tmp_path, monkeypatch, [])
    monkeypatch.setattr(PC, "_PDF_OK", True)
    monkeypatch.setattr(PC, "_index_pdfs_by_mdolx", lambda: {"252078": tmp_path / "252078.pdf"})
    monkeypatch.setattr(PC, "PDF", types.SimpleNamespace(
        parse_booking_pdf=lambda _p: {"erd": "1-Sep-26", "doc_cutoff": "3-Sep-26",
                                      "port_cutoff": "4-Sep-26"}), raising=False)
    saved, _ = _run_patch_carriers(tmp_path, monkeypatch, [row], capsys)
    assert _borrowed(saved["ol_252078"]) == [], _borrowed(saved["ol_252078"])


# ── the un-stamp: what earlier fires wrote, and what stays ──────────────────

def _preserved_ol_row(tmp_path, monkeypatch, corr=None) -> dict:
    """ol_252078 as the 2026-09-02 production dataset held it: preserved
    from a prior fire that had already borrowed the September grid."""
    row = _ol_row(tmp_path, monkeypatch, corr)
    row.update({"ol_rate": 4938.0, "etd_offered": "7-Sep-26", "eta_offered": "24-Oct-26",
                "vessel_voyage": "NYK METEOR 0CLNCE1MA", "erd": "1-Sep-26",
                "doc_cutoff": "3-Sep-26", "port_cutoff": "4-Sep-26",
                "dest_free_time": "7 COMBINED FREE DAYS", "container_size": "1x40'HC",
                "transshipment": "LE HAVRE",
                # Legitimately derived by other heals / named by OL's export:
                "pol": "Oakland", "pod": "Yokohama"})
    return row


def test_qc084_names_exactly_the_borrowed_cells(tmp_path, monkeypatch):
    row = _preserved_ol_row(tmp_path, monkeypatch)
    found = QS.qc084_fabricated_source_fields([row], corrections_path=IN.CORRECTIONS_PATH)
    assert found == [("ol_252078", [
        "ol_rate", "etd_offered", "eta_offered", "vessel_voyage", "transshipment",
        "container_size", "doc_cutoff", "port_cutoff", "erd", "dest_free_time"])]


def test_a_cell_the_operator_set_is_not_fabricated(tmp_path, monkeypatch):
    """A human may write a rate onto a recovered booking; a parser cannot.
    The correction's own `set` keys are exempt, the rest are still named."""
    corr = json.loads(json.dumps(_real_create_correction()))
    corr["set"]["ol_rate"] = 4100.0
    row = _preserved_ol_row(tmp_path, monkeypatch, corr)
    (rid, fields), = QS.qc084_fabricated_source_fields([row], corrections_path=IN.CORRECTIONS_PATH)
    assert rid == "ol_252078"
    assert "ol_rate" not in fields and "doc_cutoff" in fields


def test_a_stand_booking_with_its_confirmation_is_never_touched(tmp_path, monkeypatch):
    """stand_ rows keep the confirmation email (and its PDF) that built them;
    their cutoffs are parsed, not borrowed."""
    stand = {"request_id": "stand_260905", "status": "WIN", "quoted": True,
             "source_imids": ["<conf@ol-usa.com>"], "erd": "1-Sep-26",
             "doc_cutoff": "3-Sep-26", "port_cutoff": "4-Sep-26", "ol_rate": 3100.0}
    assert QS.qc084_fabricated_source_fields([stand], corrections_path=tmp_path / "none.json") == []


def test_the_phase3_heal_clears_the_borrowed_cells_and_only_those(tmp_path, monkeypatch):
    """The un-stamp on the real phase-3 path, right after the corrections
    backstop, logged by request_id. Idempotent: a second pass finds nothing."""
    row = _preserved_ol_row(tmp_path, monkeypatch)
    monkeypatch.setattr(QS, "_CORRECTIONS_PATH", IN.CORRECTIONS_PATH)
    log = QS.Log()
    QS.phase_3_entries(log, {"requests": [row]})
    assert _borrowed(row) == [], f"borrowed cells survived the phase-3 heal: {_borrowed(row)}"
    assert row["quoted"] is True and row["carrier_won"] == "CMA CGM SA"
    assert row["pol"] == "Oakland" and row["pod"] == "Yokohama"
    assert row["containers"] and row["container_count"] == 1 and row["teu_requested"] == 2
    fixes = [m for m in log.fixes if m.startswith("QC-084: ol_252078: cleared")]
    assert len(fixes) == 1 and "doc_cutoff" in fixes[0] and "ol_rate" in fixes[0], log.fixes
    log2 = QS.Log()
    QS.phase_3_entries(log2, {"requests": [row]})
    assert not [m for m in log2.fixes if m.startswith("QC-084:")], "the heal is not idempotent"


# ── the detector: a survivor after the scrub is a WARN ──────────────────────

def _phase6(rows: list[dict]) -> QS.Log:
    data = {"version": "2", "requests": rows,
            "summary": {"wins": 0, "quoted_lost": 0, "not_quoted": 0, "pending_hilmar": 0,
                        "win_rate": 0.0, "quote_rate": 0.0, "teu_requested": 0,
                        "teu_won": 0, "teu_quoted_lost": 0, "teu_not_quoted": 0,
                        "teu_pending": 0, "total_entries": 0}}
    log = QS.Log()
    QS.phase_6_rules(log, data)
    return log


def test_qc084_reports_a_survivor_by_request_id(tmp_path, monkeypatch):
    """Phase 6 does not run the phase-3 heal, so a still-borrowed row here IS
    the "writer after the scrub" the check exists for."""
    row = _preserved_ol_row(tmp_path, monkeypatch)
    monkeypatch.setattr(QS, "_CORRECTIONS_PATH", IN.CORRECTIONS_PATH)
    warns = [m for m in _phase6([row]).warnings if m.startswith("QC-084:")]
    assert len(warns) == 1 and "ol_252078: ol_rate" in warns[0] and "doc_cutoff" in warns[0], warns


def test_qc084_is_quiet_on_a_clean_source_less_row(tmp_path, monkeypatch):
    row = _ol_row(tmp_path, monkeypatch)
    monkeypatch.setattr(QS, "_CORRECTIONS_PATH", IN.CORRECTIONS_PATH)
    log = _phase6([row])
    assert not [m for m in log.warnings + log.errors if m.startswith("QC-084:")]


# ── LAYER 2: QC-039 grades only what a parser could have populated ──────────

def test_qc039_does_not_grade_cutoffs_on_a_row_with_nothing_to_parse(tmp_path, monkeypatch):
    row = _ol_row(tmp_path, monkeypatch)
    stats = compute_accuracy([row])["field_stats"]
    for f in ("erd", "doc_cutoff", "port_cutoff"):
        assert stats[f]["n_a"] is True, (
            f"{f} graded on an export-recovered booking that has no email and "
            f"no PDF by construction: {stats[f]}")
    # ...and, as before, not on rate/ETD/ETA either (the 2026-08-13 half).
    assert stats["ol_rate"]["n_a"] is True


def test_qc039_still_grades_cutoffs_on_a_booking_that_has_its_confirmation():
    """The other direction: a stand_ WIN keeps its confirmation email, so a
    missing cutoff there is a real parser miss and must count."""
    stand = {"request_id": "stand_260905", "status": "WIN", "quoted": True,
             "source_imids": ["<conf@ol-usa.com>"], "carrier_won": "ONE",
             "mdolx_ref": "260905", "erd": "1-Sep-26"}
    stats = compute_accuracy([stand])["field_stats"]
    assert stats["erd"] == {"applicable": 1, "populated": 1, "rate": 1.0, "n_a": False}
    assert stats["doc_cutoff"]["applicable"] == 1 and stats["doc_cutoff"]["populated"] == 0


# ── the receipt: each field's OWN floor, prefixed once ──────────────────────

def test_the_qc039_receipt_names_the_floor_that_fired(tmp_path, monkeypatch):
    """doc_cutoff's applied floor is 90% (PER_FIELD_THRESHOLDS); the line
    used to say "below 95%". Rendered through the real phase-6 gate."""
    row = {"request_id": "req_chain_win", "status": "WIN", "quoted": True,
           "origin": "Oakland", "destination": "Yokohama", "lane": "Oakland → Yokohama",
           "containers": "1x40HC", "container_count": 1, "teu_requested": 2,
           "request_date": "2026-08-01", "request_timestamp": "2026-08-01T15:00:00Z",
           "carrier_quoted": "CMA CGM", "carrier_won": "CMA CGM", "mdolx_ref": "261199",
           "ol_rate": 4938.0, "etd_offered": "7-Sep-26", "eta_offered": "24-Oct-26",
           "dest_free_time": "7 days", "product": "milk powder", "lonny_notes": "rush",
           "source_imids": ["<conf@ol-usa.com>"], "erd": "1-Sep-26", "port_cutoff": "4-Sep-26",
           "doc_cutoff": None}
    monkeypatch.setattr(QS, "_CORRECTIONS_PATH", tmp_path / "none.json")
    lines = [m for m in _phase6([row]).warnings if m.startswith("QC-039:")]
    assert len(lines) == 1, lines
    assert "doc_cutoff=0.0% (floor 90%)" in lines[0], lines[0]
    assert "below 95%" not in lines[0]


def test_the_sentry_message_carries_the_check_tag_exactly_once(monkeypatch):
    captured: list = []
    fake = types.SimpleNamespace(
        set_tag=lambda *a, **k: None,
        capture_message=lambda msg, **k: captured.append((msg, k.get("level"))))
    monkeypatch.setitem(sys.modules, "sentry_sdk", fake)
    monkeypatch.setattr(SS, "_INITIALIZED", True)
    SS.capture_qc_warning("QC-039", "QC-039: parser accuracy 98.0% overall")
    SS.capture_qc_error("QC-039", "QC-039: parser accuracy 80.0% with 1 CRITICAL field(s) below floor")
    SS.capture_qc_warning("QC-084", "bare summary with no tag")
    assert captured == [
        ("QC-039: parser accuracy 98.0% overall", "warning"),
        ("QC-039: parser accuracy 80.0% with 1 CRITICAL field(s) below floor", "error"),
        ("QC-084: bare summary with no tag", "warning"),
    ]
    assert SS.qc_event_message("QC-039", "QC-039: x") == "QC-039: x"
    assert SS.qc_event_message("QC-039", "x") == "QC-039: x"


@pytest.mark.parametrize("rid", ["ol_252078", "stand_260905", "req_c499ccd17e8763ff"])
def test_the_predicate_is_about_evidence_not_the_id_prefix(rid):
    """`stand_`, `ol_` and `req_` rows are told apart by what they carry, not
    by name — a stand_ row with its confirmation has a source; an ol_ row
    never does."""
    assert C.has_own_source({"request_id": rid, "source_imids": ["<m>"]}) is True
    assert C.has_own_source({"request_id": rid, "source_imids": []}) is False
