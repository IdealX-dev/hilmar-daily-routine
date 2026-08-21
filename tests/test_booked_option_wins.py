"""Michael's ruling: "the booked one when there is a booking."

2026-08-21. An OL reply offers several sailings on several steamship lines.
The parser's headline rule picks the LOWEST rate on the lane — the best price
OL put on the table, and the honest answer while Lonny's decision is still
open. But once a booking confirmation exists the guessing is over: the option
Hilmar booked IS the transaction, and reporting a cheaper one it declined
would be as wrong as the row-order rule both of these replaced.

The bar for "there is a booking" is core.is_confirmed_win — a WIN with an
MDOLX reference, the same bar every client-facing claim clears (QC-049). A
send-signal WIN with no confirmation is not a booking.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

import core  # noqa: E402
import qc_selfheal as QS  # noqa: E402

# The real 2026-08-19 Oakland->Xingang reply: two lines, two prices.
OPTIONS = [
    {"ol_rate": 810.0, "carrier_quoted": "ONE", "vessel_voyage": "HMM TURQUOISE 011W",
     "etd_offered": "9-Sep-26", "eta_offered": "2-Oct-26",
     "transshipment": "VIA PUSAN"},
    {"ol_rate": 675.0, "carrier_quoted": "CMA CGM", "vessel_voyage": "EVER LEGACY 0TBOGW1MA",
     "etd_offered": "7-Sep-26", "eta_offered": "22-Sep-26",
     "transshipment": "DIRECT"},
]


def _row(**over):
    """A quote sitting on the parser's headline option (the cheaper CMA one)."""
    r = {
        "request_id": "req_xingang", "lane": "Oakland → Xingang",
        "destination": "Xingang", "status": "LOSS", "quoted": True,
        "ol_rate": 675.0, "carrier_quoted": "CMA CGM",
        "vessel_voyage": "EVER LEGACY 0TBOGW1MA",
        "etd_offered": "7-Sep-26", "eta_offered": "22-Sep-26",
        "transshipment": "DIRECT",
        "rate_options": [dict(o) for o in OPTIONS],
    }
    r.update(over)
    return r


def _booked_on_one(**over):
    base = {"status": "WIN", "carrier_won": "ONE", "mdolx_ref": "MDOLX260587"}
    base.update(over)
    return _row(**base)


def test_an_open_quote_keeps_the_best_rate_offered():
    r = _row()
    assert core.snap_quote_to_booked_option(r) is None
    assert r["ol_rate"] == 675.0
    assert "rate_option_source" not in r


def test_a_booked_quote_moves_onto_the_booked_option():
    r = _booked_on_one()
    assert core.snap_quote_to_booked_option(r) == 810.0
    assert r["ol_rate"] == 810.0
    assert r["carrier_quoted"] == "ONE"
    assert r["rate_option_source"] == core.BOOKED_RATE_OPTION


def test_the_schedule_moves_with_the_rate():
    """A quote may never pair one sailing's price with another's schedule."""
    r = _booked_on_one()
    core.snap_quote_to_booked_option(r)
    assert r["vessel_voyage"] == "HMM TURQUOISE 011W"
    assert r["etd_offered"] == "9-Sep-26"
    assert r["eta_offered"] == "2-Oct-26"
    assert r["transshipment"] == "VIA PUSAN"


def test_a_schedule_from_a_better_source_is_left_alone():
    """A WIN's ETA may already come from the booking PDF, which beats the rate
    sheet. Only a field still holding the LEAVING option's value is rewritten."""
    r = _booked_on_one(eta_offered="2026-10-04")   # PDF-sourced, matches no option
    core.snap_quote_to_booked_option(r)
    assert r["ol_rate"] == 810.0
    assert r["eta_offered"] == "2026-10-04", (
        "The booking PDF's ETA was overwritten with the rate sheet's.")
    assert r["vessel_voyage"] == "HMM TURQUOISE 011W"


def test_a_send_signal_win_with_no_confirmation_is_not_a_booking():
    r = _row(status="WIN", carrier_won="ONE")      # no mdolx_ref
    assert core.snap_quote_to_booked_option(r) is None
    assert r["ol_rate"] == 675.0


def test_a_booking_on_a_carrier_ol_never_offered_moves_nothing():
    r = _booked_on_one(carrier_won="Maersk")
    assert core.snap_quote_to_booked_option(r) is None
    assert r["ol_rate"] == 675.0


def test_a_single_option_quote_is_untouched():
    r = _booked_on_one(rate_options=None)
    assert core.snap_quote_to_booked_option(r) is None
    assert r["ol_rate"] == 675.0


def test_it_is_idempotent_across_the_two_qc_passes():
    """qc_selfheal runs TWICE per fire and phase 3 runs in both."""
    r = _booked_on_one()
    first = core.snap_quote_to_booked_option(r)
    second = core.snap_quote_to_booked_option(r)
    assert (first, second) == (810.0, None)
    assert r["ol_rate"] == 810.0
    assert r["eta_offered"] == "2-Oct-26"


def test_the_heal_runs_in_phase_3():
    data = {"requests": [_booked_on_one()]}
    log = QS.Log()
    QS.phase_3_entries(log, data)
    assert data["requests"][0]["ol_rate"] == 810.0
    assert any("booked" in m for m in log.fixes), log.fixes


def test_qc079_does_not_call_the_booked_option_an_error(capsys):
    log = QS.Log()
    QS.phase_6_rules(log, {"requests": [
        _booked_on_one(ol_rate=810.0, carrier_quoted="ONE",
                       rate_option_source=core.BOOKED_RATE_OPTION)]})
    out = capsys.readouterr().out
    assert "best offered" not in out, (
        f"QC-079 flagged a deliberately-booked option as a defect:\n{out}")
    assert "on the option Hilmar booked" in out


def test_qc079_still_errors_when_the_marker_is_forged():
    """The exemption has to be earned: the marker alone is not enough, the row
    must really carry a booking confirmation."""
    log = QS.Log()
    QS.phase_6_rules(log, {"requests": [
        _row(ol_rate=810.0, carrier_quoted="ONE",
             rate_option_source=core.BOOKED_RATE_OPTION)]})
    text = "\n".join(log.errors)
    assert "best offered $675" in text, text
