"""AgriDairy moves must never count as Hilmar-the-client (stand_260821 leak).

Michael 2026-07-01: "this file is for agridairy not hilmar — it picks up from
Hilmar, not Hilmar the client. Only moves booked by Lonny are Hilmar the
client." AgriDairy ships product FROM the Hilmar plant, so "HILMAR" appears
in its bookings as the ORIGIN/supplier reference and the standalone-WIN gate
("HILMAR in subject") wrongly claimed stand_260821 — the same
Hilmar-as-supplier class as the 2026-05 Numidia leak.

Three layers, each locked here:
  1. 'agridairy' is an out_of_scope_reason marker (subject AND body AND
     preview, like numidia) → fresh intake drops these entirely, and the
     qc_selfheal PHASE-3 backstop purges stored ones by subject.
  2. operator corrections gain an `exclude: true` verb → removes a row from
     the data outright (a `set` override would still count it in Hilmar's
     wins/TEU). Idempotent: once gone, absence is silent, not a WARN.
  3. The tracked operator_corrections.json carries Michael's stand_260821
     verdict, so the row dies even if 'AgriDairy' appears only in the PDF.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import ingest  # noqa: E402
import qc_selfheal as q  # noqa: E402


# ── layer 1: the out-of-scope marker ──────────────────────────────────────
def test_agridairy_in_subject_is_out_of_scope():
    assert ingest.out_of_scope_reason(
        {"subject": "MDOLX260821_NEW BOOKING CONFIRMATION // AGRIDAIRY ex HILMAR"}
    ) == "agridairy"


def test_agridairy_in_body_or_preview_is_out_of_scope():
    assert ingest.out_of_scope_reason(
        {"subject": "MDOLX260821_NEW BOOKING // HILMAR",
         "text_body": "Shipper: Agri-Dairy LLC"}) == "agridairy"
    assert ingest.out_of_scope_reason(
        {"subject": "booking", "summary_preview": "AGRI DAIRY move"}) == "agridairy"


def test_hilmar_client_rows_are_not_swept_up():
    """A genuine Lonny/Hilmar row must not trip the new marker — including
    prose that merely says 'dairy' without the Agri prefix."""
    assert ingest.out_of_scope_reason(
        {"subject": "HILMAR Oakland to Yokohama RFQ",
         "text_body": "dairy products, 2x40'RF"}) is None


def test_phase3_backstop_purges_agridairy_subject_rows():
    """The stored-row backstop (qc_selfheal PHASE 3) purges an AgriDairy
    stand_* WIN by subject, same as Numidia rows."""
    row = {"request_id": "stand_999", "status": "WIN",
           "subject": "MDOLX999_NEW BOOKING // AGRIDAIRY ex HILMAR, CA"}
    data = {"requests": [row]}
    log = q.Log()
    q.phase_3_entries(log, data)
    assert not any(r.get("request_id") == "stand_999" for r in data["requests"])
    assert any("agridairy" in m for m in log.fixes), log.fixes


# ── layer 2: the exclude verb ─────────────────────────────────────────────
def _corrections_doc(tmp_path, monkeypatch, corrections):
    p = tmp_path / "operator_corrections.json"
    p.write_text(json.dumps({"corrections": corrections}), encoding="utf-8")
    monkeypatch.setattr(ingest, "CORRECTIONS_PATH", p)
    return p


def test_exclude_verb_removes_the_row(tmp_path, monkeypatch):
    _corrections_doc(tmp_path, monkeypatch, [
        {"request_id": "stand_260821", "exclude": True, "note": "AgriDairy"}])
    rows = [{"request_id": "stand_260821", "status": "WIN"},
            {"request_id": "req_keep", "status": "LOSS"}]
    applied = ingest.apply_operator_corrections(rows)
    assert applied == 1
    assert [r["request_id"] for r in rows] == ["req_keep"]


def test_exclude_is_idempotent_and_silent_when_gone(tmp_path, monkeypatch, capsys):
    """Once excluded, absence is the EXPECTED steady state — every later run
    must be a quiet no-op, not a 'no matching row' WARN."""
    _corrections_doc(tmp_path, monkeypatch, [
        {"request_id": "stand_260821", "exclude": True}])
    rows = [{"request_id": "req_keep", "status": "LOSS"}]
    applied = ingest.apply_operator_corrections(rows)
    out = capsys.readouterr().out
    assert applied == 0
    assert len(rows) == 1
    assert "no matching row" not in out


def test_set_corrections_still_work_alongside_exclude(tmp_path, monkeypatch):
    _corrections_doc(tmp_path, monkeypatch, [
        {"request_id": "stand_260821", "exclude": True},
        {"request_id": "req_fix", "set": {"status": "LOSS", "loss_reason": "SEND_NO_BOOKING"},
         "note": "audit"}])
    rows = [{"request_id": "stand_260821", "status": "WIN"},
            {"request_id": "req_fix", "status": "WIN"}]
    applied = ingest.apply_operator_corrections(rows)
    assert applied == 2
    assert rows[0]["request_id"] == "req_fix"
    assert rows[0]["status"] == "LOSS" and rows[0]["manual_locked"] is True


# ── layer 3: the authoritative verdict is on file ─────────────────────────
def test_stand_260821_exclusion_is_recorded():
    doc = json.loads((ROOT / "scripts" / "operator_corrections.json")
                     .read_text(encoding="utf-8"))
    entry = next((c for c in doc["corrections"]
                  if c.get("request_id") == "stand_260821"), None)
    assert entry is not None, "stand_260821 verdict missing from operator_corrections.json"
    assert entry.get("exclude") is True
    assert "AgriDairy" in entry.get("note", "")
