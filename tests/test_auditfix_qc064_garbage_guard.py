"""QC-064 — garbage in client-visible display fields.

Defense-in-depth for the "absolutely wrong info" class the operator flagged:
a phone fragment, a raw message-id, or the OL responder-mailbox name leaking
into a display field (carrier/origin/destination/lane/pol/pod/vessel_voyage/
transshipment) that ships straight into the client email + PDF. The check
SELF-HEALS by nulling the field and logs a QC-064 fix; a clean row is left
untouched and emits QC-064 ok. WARN-class — never an ERROR gate, so a false
positive can't block the client email.

Drives the real phase_6_rules log path (matching test_env_integrity_checks.py's
sys.path/header style) and the focused qc064_garbage_reason helper directly.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import qc_selfheal as q  # noqa: E402


def _data_with(requests):
    return {"version": "2", "requests": requests,
            "summary": {"wins": 0, "quoted_lost": 0, "not_quoted": 0,
                        "pending_hilmar": 0, "win_rate": 0.0, "quote_rate": 0.0,
                        "teu_requested": 0, "teu_won": 0, "teu_quoted_lost": 0,
                        "teu_not_quoted": 0, "teu_pending": 0, "total_entries": 0}}


def _row(request_id="req_DEAD", status="PENDING", **fields):
    base = {"request_id": request_id, "status": status, "quoted": False}
    base.update(fields)
    return base


def _run(monkeypatch, requests):
    monkeypatch.setenv("HILMAR_QC_NO_PIP", "1")
    log = q.Log()
    q.phase_6_rules(log, _data_with(requests))
    return log


# ── the focused helper: each garbage shape, plus clean values ────────────
def test_helper_flags_raw_message_id():
    assert q.qc064_garbage_reason("<CADEADBEEF@mail.ol-usa.com>")


def test_helper_flags_exchange_msgid_shard():
    # An Exchange msg-id shard with the MB anchor: uppercase/digits run + MB +
    # trailing alnum. Conservative — needs the MB anchor + length.
    assert q.qc064_garbage_reason("AAAA1234MB99ZZ")


def test_helper_flags_email_or_mailbox():
    assert q.qc064_garbage_reason("MBD_OceanExportBookingShared@ol-usa.com")
    assert q.qc064_garbage_reason("Ocean Export Booking Shared Mailbox")
    assert q.qc064_garbage_reason("Shared Mailbox")


def test_helper_flags_phone_fragment():
    assert q.qc064_garbage_reason("Call 209-826")
    assert q.qc064_garbage_reason("555-1234")


def test_helper_leaves_clean_values_untouched():
    for clean in ("CMA CGM", "Maersk", "Los Angeles", "Shanghai",
                  "USLAX", "CNSHA", "Hapag-Lloyd", "Singapore",
                  "EVER GIVEN 0FW3E", "Busan", None, "", "N/A"):
        assert q.qc064_garbage_reason(clean) is None, clean


# ── phase_6_rules: each garbage type is nulled + a QC-064 fix is logged ───
def test_phase6_nulls_message_id_in_carrier(monkeypatch):
    r = _row(carrier_quoted="<CADEADBEEF@mail.ol-usa.com>", status="LOSS",
             quoted=True)
    log = _run(monkeypatch, [r])
    assert r["carrier_quoted"] is None
    assert any("QC-064" in m and "carrier_quoted" in m for m in log.fixes), log.fixes


def test_phase6_nulls_mailbox_in_origin(monkeypatch):
    r = _row(origin="MBD_OceanExportBookingShared@ol-usa.com")
    log = _run(monkeypatch, [r])
    assert r["origin"] is None
    assert any("QC-064" in m and "origin" in m for m in log.fixes), log.fixes


def test_phase6_nulls_phone_fragment_in_destination(monkeypatch):
    r = _row(destination="call 209-826")
    log = _run(monkeypatch, [r])
    assert r["destination"] is None
    assert any("QC-064" in m and "destination" in m for m in log.fixes), log.fixes


def test_phase6_nulls_msgid_shard_in_vessel(monkeypatch):
    r = _row(vessel_voyage="AAAA1234MB99ZZ")
    log = _run(monkeypatch, [r])
    assert r["vessel_voyage"] is None
    assert any("QC-064" in m and "vessel_voyage" in m for m in log.fixes), log.fixes


def test_phase6_scans_every_display_field(monkeypatch):
    # One garbage value per display field — all must be nulled in one pass.
    bad = "<x@y.com>"
    r = _row(**{f: bad for f in q.QC064_DISPLAY_FIELDS}, status="LOSS", quoted=True)
    log = _run(monkeypatch, [r])
    for f in q.QC064_DISPLAY_FIELDS:
        assert r[f] is None, f
    # One fix per offending field.
    qc064_fixes = [m for m in log.fixes if "QC-064" in m]
    assert len(qc064_fixes) == len(q.QC064_DISPLAY_FIELDS), qc064_fixes


# ── a clean row is untouched and emits QC-064 ok (printed, not logged) ────
def test_phase6_clean_row_untouched_and_ok(monkeypatch, capsys):
    r = _row(carrier_quoted="CMA CGM", origin="Los Angeles",
             destination="Shanghai", lane="Los Angeles → Shanghai",
             pol="USLAX", pod="CNSHA", vessel_voyage="EVER GIVEN 0FW3E",
             transshipment="Singapore", status="LOSS", quoted=True)
    log = _run(monkeypatch, [r])
    # Nothing scrubbed.
    assert r["carrier_quoted"] == "CMA CGM"
    assert r["origin"] == "Los Angeles"
    assert not any("QC-064" in m for m in log.fixes), log.fixes
    # ok() prints rather than logging — assert on captured stdout.
    out = capsys.readouterr().out
    assert "QC-064: no garbage tokens in display fields" in out


def test_phase6_is_warn_class_not_error_gate(monkeypatch):
    # A garbage value must NEVER raise a QC-064 ERROR (it would block the
    # client email). The self-heal nulls it; no QC-064 error is emitted.
    r = _row(carrier_quoted="<x@y.com>", status="LOSS", quoted=True)
    log = _run(monkeypatch, [r])
    assert not any("QC-064" in m for m in log.errors), log.errors
