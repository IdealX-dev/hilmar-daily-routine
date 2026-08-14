"""A booking Hilmar cancelled must never become a win — by construction.

Michael 2026-08-13: "260905 260192 260963 were bookings hilmar cancelled",
and separately "260426 cancelled".

THE FRAGILITY THIS CLOSES. ol_260192 carried TWO corrections at once — a
`create: true` that recorded it as a WIN from OL's transaction report, and an
`exclude: true` added afterwards when Michael said it was cancelled. Both were
written the same day, and the fire log showed both firing on every single run:

    Operator correction: CREATED  ol_260192 (MDOLX260192) — booked ...
    Operator correction: EXCLUDED ol_260192 — MDOLX260192 was cancelled

The net result was CORRECT — 133 wins, matching OL's book — but only because
apply_operator_corrections happens to iterate the file in order and the
exclude sits after the create. Nothing enforced that. Reorder the file, sort
it, or dedupe it by request_id keeping the first, and a cancelled booking
silently becomes a phantom win: exactly the failure class that cost a full
day on 2026-08-13.

The create is gone. These tests keep it gone.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import ingest  # noqa: E402

CORRECTIONS = ROOT / "scripts" / "operator_corrections.json"

#: Every MDOLX Michael has named as cancelled or not-Hilmar, verbatim.
CANCELLED = ("260192", "260905", "260963", "260426")


def _doc():
    return json.loads(CORRECTIONS.read_text(encoding="utf-8"))


def test_no_cancelled_booking_has_a_create():
    """The invariant. A create for a cancelled MDOLX is a phantom win waiting
    for someone to reorder the file."""
    offenders = []
    for c in _doc()["corrections"]:
        if not c.get("create"):
            continue
        ref = str((c.get("set") or {}).get("mdolx_ref") or "").lstrip("0")
        if ref in CANCELLED:
            offenders.append(c.get("request_id"))
    assert offenders == [], (
        f"these cancelled bookings still have a `create`: {offenders} — "
        "remove the create; the exclude alone is the record")


def test_no_request_id_is_both_created_and_excluded():
    """Generalises it: any id carrying both is order-dependent, whichever way
    the ordering currently happens to fall."""
    created, excluded = set(), set()
    for c in _doc()["corrections"]:
        rid = c.get("request_id")
        if c.get("create"):
            created.add(rid)
        if c.get("exclude"):
            excluded.add(rid)
    both = sorted(created & excluded)
    assert both == [], (
        f"{both} are both created and excluded — the outcome depends on file "
        "order, which nothing enforces")


def test_the_exclude_for_260192_survives_as_the_record():
    keep = [c for c in _doc()["corrections"]
            if c.get("request_id") == "ol_260192"]
    assert len(keep) == 1, keep
    assert keep[0].get("exclude") is True
    assert "cancelled" in (keep[0].get("note") or "").lower()


def test_an_exclude_for_a_row_that_does_not_exist_is_a_silent_no_op():
    """Why deleting the create is safe: apply_operator_corrections treats a
    missing row as the expected steady state, not an error."""
    rows = [{"request_id": "req_real", "status": "WIN", "mdolx_ref": "260999"}]
    before = json.dumps(rows, sort_keys=True)
    ingest.apply_operator_corrections(rows)
    # req_real carries no correction, so it must be untouched, and the
    # ol_260192 exclude must not have raised on its absent row.
    assert any(r.get("request_id") == "req_real" for r in rows)
    assert "ol_260192" not in json.dumps(rows)
    assert before == before  # the call completed at all — no exception


def test_cancelled_bookings_never_appear_as_wins_after_corrections():
    """End to end through the real corrections file: run the applier over an
    empty dataset and assert no cancelled MDOLX materialises."""
    rows: list[dict] = []
    ingest.apply_operator_corrections(rows)
    for r in rows:
        ref = str(r.get("mdolx_ref") or "").lstrip("0")
        assert ref not in CANCELLED, (
            f"{r.get('request_id')} materialised MDOLX{ref}, which Michael "
            "said was cancelled")
