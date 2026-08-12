"""A win that no email can produce.

Michael, 2026-08-12: "everything she sent as a booking is a win ... assume
that each win was a quote request so just use the wins if you cannot find the
emails from lonny in my ol emails".

OL's booking recap is the system of record. A booking in it happened whether
or not the confirmation reached the mailbox this pipeline reads — and for the
Jul-Aug gap, entire threads went To: Lonny, Cc: the group and never arrived.
operator_corrections could only AMEND an existing row, which cannot express a
win with no row at all.

The danger this creates, and the guard for it: once the forwarding fix lets
the real confirmation through, ingest builds its own row for that MDOLX. Two
sources for one booking is exactly the double-counting that would make the
win rate wrong in the other direction, so a created row stands down the
moment a derived one exists.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for p in (str(ROOT / "scripts"), str(ROOT / "src")):
    if p not in sys.path:
        sys.path.insert(0, p)

import ingest as IN  # noqa: E402


def _corr(tmp_path, monkeypatch, **kw):
    import json
    body = {"request_id": kw.pop("request_id", "ol_261071"),
            "create": True,
            "set": kw.pop("set", {"status": "WIN", "mdolx_ref": "261071"}),
            "note": kw.pop("note", "from OL booking recap")}
    body.update(kw)
    p = tmp_path / "operator_corrections.json"
    p.write_text(json.dumps({"corrections": [body]}), encoding="utf-8")
    monkeypatch.setattr(IN, "CORRECTIONS_PATH", p, raising=False)


def test_a_booking_with_no_row_anywhere_becomes_a_win(tmp_path, monkeypatch):
    """THE case: MDOLX261071 exists in OL's recap and in no email we hold."""
    _corr(tmp_path, monkeypatch)
    rows: list[dict] = []
    IN.apply_operator_corrections(rows)
    assert len(rows) == 1
    r = rows[0]
    assert r["status"] == "WIN" and r["mdolx_ref"] == "261071"
    assert r["status_history"] and r["status_history"][-1]["to"] == "WIN", (
        "a created win with no transition entry is invisible to every "
        "win-event surface")


def test_it_stands_down_once_the_real_email_produces_a_row(tmp_path, monkeypatch):
    """Forwarding is fixed, so the confirmation will arrive and ingest will
    build its own row. The derived row wins — it has the email behind it —
    and the created one must not become a second copy of the same booking."""
    _corr(tmp_path, monkeypatch)
    rows = [{"request_id": "req_real", "status": "WIN", "mdolx_ref": "261071"}]
    IN.apply_operator_corrections(rows)
    assert len(rows) == 1, "the booking was counted twice"
    assert rows[0]["request_id"] == "req_real"


def test_a_secondary_mdolx_ref_also_blocks_the_duplicate(tmp_path, monkeypatch):
    """A row accumulates mdolx_refs_all; the primary is only the last linked
    one. Checking the primary alone would let a duplicate through."""
    _corr(tmp_path, monkeypatch)
    rows = [{"request_id": "req_real", "status": "WIN", "mdolx_ref": "260999",
             "mdolx_refs_all": ["261071"]}]
    IN.apply_operator_corrections(rows)
    assert len(rows) == 1


def test_creating_is_opt_in_not_the_default(tmp_path, monkeypatch):
    """Without create:true a correction for a missing row must still warn and
    skip — the old behaviour, which is right for a typo'd request_id."""
    import json
    p = tmp_path / "operator_corrections.json"
    p.write_text(json.dumps({"corrections": [
        {"request_id": "typo_id", "set": {"status": "WIN"}}]}), encoding="utf-8")
    monkeypatch.setattr(IN, "CORRECTIONS_PATH", p, raising=False)
    rows: list[dict] = []
    IN.apply_operator_corrections(rows)
    assert rows == [], "a correction with a bad id silently invented a row"


def test_the_created_row_carries_the_lane_when_one_is_known(tmp_path, monkeypatch):
    _corr(tmp_path, monkeypatch, set={
        "status": "WIN", "mdolx_ref": "261099", "destination": "Yokohama",
        "lane": "Oakland → Yokohama", "carrier_won": "ONE",
        "teu_requested": 2, "teu_won": 2})
    rows: list[dict] = []
    IN.apply_operator_corrections(rows)
    r = rows[0]
    assert r["lane"] == "Oakland → Yokohama" and r["carrier_won"] == "ONE"
    assert r["teu_won"] == 2


def test_an_unknown_lane_is_labelled_not_invented(tmp_path, monkeypatch):
    """MDOLX261071's recap row carries no POD. 'Lane unresolved' is the
    honest value and QC-015 keeps flagging it; guessing a lane to make the
    row look tidy is the fabrication this whole session removed."""
    _corr(tmp_path, monkeypatch)
    rows: list[dict] = []
    IN.apply_operator_corrections(rows)
    assert rows[0]["lane"] == "Lane unresolved"
    assert rows[0]["destination"] == "Unknown"


def test_re_running_does_not_append_twice(tmp_path, monkeypatch):
    """qc_selfheal re-applies the same corrections after ingest does. A
    second pass must recognise its own row rather than add another."""
    _corr(tmp_path, monkeypatch)
    rows: list[dict] = []
    IN.apply_operator_corrections(rows)
    IN.apply_operator_corrections(rows)
    assert len(rows) == 1, "the correction appended a duplicate on re-apply"
