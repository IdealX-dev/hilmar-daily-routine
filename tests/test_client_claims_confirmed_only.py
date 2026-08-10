"""We do not tell the customer a shipment is booked unless it is.

2026-08-10, Michael: "data missing.. you sent lonny we won no shipment last
week."

He is right, and the claim was made under headings that promised exactly what
we could not support:

    gen_client_email  "Booked shipments — upcoming and in transit"
                      "Your confirmed bookings that have not yet reached..."
    gen_client_email  "Bookings confirmed"
                      "Shipments confirmed on <day>."
    gen_client_weekly bookings / teu_booked / active_shipments

Every one selected on `status == "WIN"` alone. A row flips to WIN on a
send-signal — Lonny saying "please send" — and only becomes a real booking
when OL issues an MDOLX confirmation. Between those two moments the row is a
WIN with no booking behind it, and both templates rendered the reference cell
as `mdolx_ref or "Confirmation to follow"`, which tells the customer a
confirmation is coming when nothing says it is.

WE ALREADY KNEW. QC-049 has flagged these rows internally at ERROR severity
since 2026-05 — "UNCONFIRMED — flipped to WIN on a send-signal with no MDOLX
booking confirmation linked". The internal audit reported them; the
client-facing renderers never asked. One fact, two readers, and the one
talking to the customer held the wrong half.

These tests pin the ASYMMETRY, which is the actual rule:
  - internal reporting may use is_win. A send-signal win is a real business
    signal and our own KPIs should count it.
  - anything leaving the building for Hilmar must use is_confirmed_win.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

CLIENT_MODULES = ("gen_client_email.py", "gen_client_weekly.py")


def _confirmed():
    return {
        "request_id": "req_confirmed", "status": "WIN",
        "mdolx_ref": "MDOLX260980", "lane": "Oakland → Yokohama",
        "origin": "Oakland", "destination": "Yokohama",
        "request_date": "2026-08-04", "carrier_won": "ONE",
        "etd_offered": "2026-08-20", "eta_offered": "2026-09-12",
        "containers": "2x40RF", "teu_won": 4,
    }


def _unconfirmed():
    """The row that produced the complaint: WIN on a send-signal, no MDOLX."""
    return {
        "request_id": "req_unconfirmed", "status": "WIN",
        "lane": "Oakland → Busan", "origin": "Oakland", "destination": "Busan",
        "request_date": "2026-08-05", "carrier_won": "MSC",
        "etd_offered": "2026-08-22", "eta_offered": "2026-09-15",
        "containers": "1x40RF", "teu_won": 2,
    }


def test_the_predicate_requires_a_booking_reference():
    import core
    assert core.is_confirmed_win(_confirmed())
    assert not core.is_confirmed_win(_unconfirmed())
    # both storage spellings of the reference count
    assert core.is_confirmed_win({"status": "WIN", "mdolx_refs_all": ["MDOLX1"]})
    # empty is not a reference — `or` on a falsy value must not pass it
    assert not core.is_confirmed_win({"status": "WIN", "mdolx_ref": ""})
    assert not core.is_confirmed_win({"status": "WIN", "mdolx_refs_all": []})
    # and it must never be broader than is_win
    assert not core.is_confirmed_win({"status": "PENDING", "mdolx_ref": "MDOLX1"})
    assert core.is_confirmed_win(None) is False


def test_it_matches_QC_049s_definition_exactly():
    """QC-049 decides which wins the AUDIT calls unconfirmed. If the two
    definitions drift, the audit reports a number the client email
    contradicts — which is how this defect survived since May."""
    import core
    qc = (ROOT / "scripts" / "qc_selfheal.py").read_text(encoding="utf-8")
    assert 'not r.get("mdolx_ref") and not r.get("mdolx_refs_all")' in qc, (
        "QC-049's unconfirmed-win definition moved — core.is_confirmed_win "
        "must move with it or the audit and the client email will disagree")
    for row in (_confirmed(), _unconfirmed()):
        qc_unconfirmed = not (row.get("mdolx_ref") or row.get("mdolx_refs_all"))
        assert core.is_confirmed_win(row) is (not qc_unconfirmed)


def test_no_client_module_selects_bookings_on_status_alone():
    """The whole defect in one line. `is_win` and `status == "WIN"` are both
    fine internally and both wrong here."""
    import ast
    offenders = []
    for name in CLIENT_MODULES:
        src = (ROOT / "scripts" / name).read_text(encoding="utf-8")
        if "core.is_win(" in src:
            offenders.append(f"{name}: uses core.is_win for a client claim")
        # AST for the literal, so the prose above and in the modules' own
        # docstrings cannot trip it — that has produced a false guard seven
        # times in this repo.
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.Compare) and isinstance(node.left, ast.Call):
                fn = node.left.func
                attr = getattr(fn, "attr", None)
                arg0 = node.left.args[0] if node.left.args else None
                lit = getattr(node.comparators[0], "value", None) if node.comparators else None
                if (attr == "get" and lit == "WIN"
                        and isinstance(arg0, ast.Constant) and arg0.value == "status"):
                    offenders.append(f"{name}:{node.lineno}: raw status == 'WIN'")
    assert not offenders, (
        "client-facing code claims a booking from status alone:\n  "
        + "\n  ".join(offenders))


def test_the_promise_is_gone():
    """"Confirmation to follow" printed beside a row with no reference is an
    assurance nothing supports. With confirmed-only selection the cell can no
    longer be empty, so the string has no reason to exist."""
    for name in CLIENT_MODULES:
        src = (ROOT / "scripts" / name).read_text(encoding="utf-8")
        code = "\n".join(
            ln for ln in src.splitlines() if not ln.lstrip().startswith("#"))
        assert "Confirmation to follow" not in code, (
            f"{name} still promises a confirmation it cannot evidence")


def test_an_unconfirmed_win_never_reaches_the_weekly():
    """Behaviour, not source. Build a week containing one confirmed and one
    unconfirmed win and assert only the confirmed one is counted."""
    from datetime import date

    import gen_client_weekly as W
    data = {"requests": [_confirmed(), _unconfirmed()]}
    s = W.client_sections(data, date(2026, 8, 3), date(2026, 8, 7))
    ids = [r["request_id"] for r in s["bookings"]]
    assert ids == ["req_confirmed"], (
        f"the weekly reported an unconfirmed win as a booking: {ids}")

    trend = W.volume_trend(data, date(2026, 8, 7), weeks=1)
    assert trend[-1]["bookings"] == 1, (
        f"the trend counted {trend[-1]['bookings']} bookings; only one is "
        f"confirmed")
    assert trend[-1]["teu_booked"] == 4, (
        "TEU booked includes the unconfirmed row's tonnage")

    active = W.active_shipments(data, date(2026, 8, 7))
    assert [r["request_id"] for r in active] == ["req_confirmed"]


def test_an_unconfirmed_win_never_reaches_the_daily_active_list():
    """The daily's "Your confirmed bookings" section, asserted through the
    real selector rather than by reading it."""
    from datetime import date

    import gen_client_email as CE
    data = {"requests": [_confirmed(), _unconfirmed()]}
    rows = CE._active_shipments(data, date(2026, 8, 7))
    ids = [r["request_id"] for r in rows]
    assert "req_unconfirmed" not in ids, (
        f"an unconfirmed win is in the daily's confirmed-bookings list: {ids}")


def test_internal_reporting_still_counts_send_signal_wins():
    """The asymmetry is deliberate and must not be 'tidied' into consistency.
    A send-signal win is a real business signal — the STAFF email and the KPIs
    should keep counting it. Only the customer-facing claim needs the
    confirmation. Narrowing the internal number would understate the desk's
    actual performance."""
    src = (ROOT / "scripts" / "gen_email.py").read_text(encoding="utf-8")
    assert "core.is_win(" in src, (
        "the staff email stopped counting send-signal wins — that is an "
        "internal KPI, not a claim to Hilmar")
