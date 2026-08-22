"""The time system: which clock a number came from, and which leg it measured.

Michael, 2026-08-21, on the delivered Aug 20 report: "pending hilmar two
sections different number of open   fix time system with proper tools".

Investigating it turned up FOUR separate defects and one non-defect, and the
non-defect matters most, so it is pinned first: PENDING_HILMAR_LOSS_HOURS is
24 because Michael set it to 24 (0c73c4b, superseding his own earlier 48).
Three Aug-20 quotes at 26-29h were aged to Quoted & Lost CORRECTLY. Any future
session that "fixes" that constant is reverting an operator decision.
"""
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

import body_parser as BP  # noqa: E402
import core  # noqa: E402
import gen_email as GE  # noqa: E402

from hilmar import body_parser as HBP  # noqa: E402
from hilmar import core as HC  # noqa: E402

# ── 0. The operator decision, pinned so nobody "fixes" it back ──────────────

def test_the_pending_hilmar_window_is_michaels_24_hours():
    assert core.PENDING_HILMAR_LOSS_HOURS == 24, (
        "Michael set 24 in 0c73c4b (2026-07-26), explicitly superseding the "
        "48 he asked for on 2026-07-14. This is an operator decision, not a "
        "tunable — if it is being changed, he changed it.")
    assert core.PENDING_HILMAR_LOSS_HOURS_FRIDAY == 72


def test_a_26_hour_old_quote_is_correctly_quoted_and_lost():
    """The exact rows from the delivered report. This is NOT a bug."""
    quote = datetime(2026, 8, 20, 19, 33, tzinfo=timezone.utc)   # Thu 15:33 ET
    now = datetime(2026, 8, 21, 21, 45, tzinfo=timezone.utc)     # Fri 17:45 ET
    assert (now - quote).total_seconds() / 3600 > 24
    assert core.pending_hilmar_stale(quote, now) is True


def test_the_same_quote_is_still_pending_at_the_scheduled_fire():
    """...and at 8 AM the next morning it is NOT stale — which is why the
    normal cadence never showed Michael this and an off-hours re-fire did."""
    quote = datetime(2026, 8, 20, 19, 33, tzinfo=timezone.utc)
    at_8am = datetime(2026, 8, 21, 12, 7, tzinfo=timezone.utc)    # 08:07 ET
    assert core.pending_hilmar_stale(quote, at_8am) is False


# ── 1. Like for like: a cutoff ask is not a requested arrival ───────────────

def test_departure_language_no_longer_populates_the_arrival_ask():
    ref = date(2026, 8, 20)
    for txt in ("Cutoff 8/28", "Need to sail by 8/25", "Ship by 8/25",
                "Load by 8/25", "Sailing by 8/25"):
        body = "Oakland to Osaka\n" + txt
        assert BP.parse_eta_requested(body, ref_date=ref) is None, txt
        assert BP.parse_etd_requested(body, ref_date=ref) is not None, txt


def test_a_real_arrival_ask_is_untouched():
    ref = date(2026, 8, 20)
    body = "10-20's\nOakland to Shanghai\nETA 9/15 send your closest"
    assert BP.parse_eta_requested(body, ref_date=ref) == "2026-09-15"


def test_requested_fit_refuses_to_cross_the_legs():
    """THE DEFECT. A cutoff differenced against an arrival measures the ocean
    crossing: "Cutoff 8/28" vs OL's ETA 30-Sep-26 came out as 33 days and
    cleared the 5-day ETD_MISS gate every single time."""
    crossed = {"etd_requested": "2026-08-28", "eta_offered": "30-Sep-26"}
    assert core.requested_fit_days(crossed) == (None, None)

    arrival = {"eta_requested": "2026-09-15", "eta_offered": "10-Oct-26"}
    assert core.requested_fit_days(arrival) == (25, "arrival")

    departure = {"etd_requested": "2026-08-28", "etd_offered": "6-Sep-26"}
    assert core.requested_fit_days(departure) == (9, "departure")


def test_requested_fit_never_guesses_from_free_text():
    """requested_dates is prose with no stated leg ("Cutoff next week or the
    following"). The old code fed it in as an arrival ask."""
    assert core.requested_fit_days(
        {"requested_dates": "Cutoff next week", "eta_offered": "30-Sep-26"}
    ) == (None, None)


def test_both_trees_agree_on_the_fit():
    row = {"eta_requested": "2026-09-15", "eta_offered": "10-Oct-26"}
    assert core.requested_fit_days(row) == HC.requested_fit_days(row)


def test_etd_miss_threshold_is_named_not_spelled():
    assert core.ETD_MISS_DAYS == HC.ETD_MISS_DAYS == 5
    src = (ROOT / "scripts" / "core.py").read_text(encoding="utf-8")
    assert "etd_fit_days >= ETD_MISS_DAYS" in src
    assert "etd_fit_days >= 5" not in src


# ── 2. A year-less date means the NEXT one when it has already passed ───────

def test_a_december_ask_for_january_means_next_january():
    got = BP.parse_eta_requested("Oakland to Osaka\nETA 1/15",
                                 ref_date=date(2026, 12, 10))
    assert got == "2027-01-15", (
        "A December RFQ asking for 15 Jan resolved into the past, so OL's "
        "perfectly good January arrival measured ~365 days late.")


def test_a_slightly_stale_date_is_left_alone():
    """Lonny does restate a cutoff that just passed; rolling it a full year
    forward would be a worse lie than leaving it."""
    assert BP.parse_eta_requested("Oakland to Osaka\nETA 8/18",
                                  ref_date=date(2026, 8, 20)) == "2026-08-18"


def test_an_explicit_year_always_wins():
    assert BP.parse_eta_requested("Oakland to Osaka\nETA 1/15/27",
                                  ref_date=date(2026, 12, 10)) == "2027-01-15"


def _code_only(path):
    """Executable lines of a module, comments stripped.

    Scanning raw source would match the COMMENT that explains a fix and call
    it a regression — which is what the first version of the test below did.
    """
    import io
    import tokenize
    src = Path(path).read_text(encoding="utf-8")
    drop = {t.start[0] for t in tokenize.generate_tokens(io.StringIO(src).readline)
            if t.type == tokenize.COMMENT}
    return "\n".join(ln for i, ln in enumerate(src.splitlines(), 1)
                      if i not in drop)


def test_the_fallback_year_comes_from_the_email_not_the_run_clock():
    src = _code_only(ROOT / "scripts" / "body_parser.py")
    assert "datetime.utcnow().year" not in src, (
        "the bare-date year must come from ref_date (the message's send "
        "date); the run clock made the same body parse differently in "
        "December and January, and reprocess_bodies re-derives every fire")
    assert BP.parse_eta_requested("ETA 1/15", ref_date=date(2026, 12, 10)) == \
        HBP.parse_eta_requested("ETA 1/15", ref_date=date(2026, 12, 10))


# ── 3. The report says WHICH MOMENT each section describes ─────────────────

def _row(rid, lane, status, on_day, quoted=True):
    sent = datetime(on_day.year, on_day.month, on_day.day, 14, 0,
                    tzinfo=timezone.utc)
    return {"request_id": rid, "lane": lane, "origin": "Oakland",
            "destination": lane.split("→")[-1].strip(),
            "containers": "2-20'", "teu_requested": 2,
            "status": status, "quoted": quoted,
            "carrier_quoted": "ONE", "ol_rate": 555.0,
            "ol_responder_signer": "Maria Machado",
            "request_timestamp": (sent - timedelta(hours=5)).isoformat(),
            "response_timestamp": sent.isoformat(),
            "status_history": [{"at": sent.isoformat(), "from": "PENDING",
                                "to": "QUOTED", "reason": "MBD rate response"}]}


def _report_html(rows):
    return GE.build_body({"requests": rows}, {})


def _report_day():
    return GE._report_date(datetime.now(timezone.utc).astimezone(core.ET))


def test_the_pending_sections_say_they_are_live():
    html = _report_html([_row("r1", "Oakland → Durban", "PENDING", _report_day())])
    assert "Open right now — as of" in html, (
        "PENDING OL/HILMAR are current state at render time while the box "
        "around them is a day's history — the report has to say so")


def test_the_report_reconciles_rows_that_left_the_pending_list():
    """Michael's exact picture: three lanes shown moving INTO Pending Hilmar,
    one unrelated row actually pending."""
    day = _report_day()
    rows = [_row("r1", "Oakland → Osaka", "LOSS", day),
            _row("r2", "Oakland → Keelung", "LOSS", day),
            _row("r3", "Oakland → Shanghai", "LOSS", day),
            _row("r4", "Oakland → Durban", "PENDING", day)]
    html = _report_html(rows)
    m = re.search(r"(\d+) quote\(s\) shown in STATUS CHANGES", html)
    assert m, "no reconciliation line — the two counts contradict in silence"
    assert m.group(1) == "3"
    for lane in ("Oakland → Osaka", "Oakland → Keelung", "Oakland → Shanghai"):
        assert lane in html
    assert f"{core.PENDING_HILMAR_LOSS_HOURS}h decision window" in html


def test_no_reconciliation_line_when_nothing_left_the_list():
    html = _report_html([_row("r4", "Oakland → Durban", "PENDING", _report_day())])
    assert "shown in STATUS CHANGES" not in html


def test_the_reconciliation_quotes_the_constant_not_a_literal():
    src = (ROOT / "scripts" / "gen_email.py").read_text(encoding="utf-8")
    assert "core.PENDING_HILMAR_LOSS_HOURS" in src


# ── 4. One vocabulary on screen ────────────────────────────────────────────

def test_a_lost_row_never_renders_the_raw_storage_enum():
    """Production stores LEGACY form (LOSS + quoted); every count in the same
    email says Q&L. The pill said LOSS."""
    pill = GE._status_change_pill("LOSS", {"status": "LOSS", "quoted": True},
                                  "QUOTED")
    assert "LOSS" not in pill, pill
    assert "Q&amp;L" in pill or "Q&L" in pill, pill


def test_a_strict_form_row_renders_the_same_way():
    legacy = GE._status_change_pill("LOSS", {"status": "LOSS", "quoted": True}, "QUOTED")
    strict = GE._status_change_pill("Q&L", {"status": "Q&L", "quoted": True}, "QUOTED")
    assert legacy == strict, "the two storage forms must render identically"


# ── 5. What the two code-review passes caught, pinned so it cannot return ──

def test_the_roll_forward_never_redates_a_quoted_chain():
    """A reply carries the whole thread. A re-ping sent in August must not
    re-date Lonny's May "ETA 6/1" against an August clock — that put the ask
    a year out and fed ~-290d into avg_etd_fit_days."""
    body = ("Any update on this?\n\n"
            "From: Lonny Upfold\nSent: Monday, May 4, 2026\n\n"
            "Oakland to Osaka\nETA 6/1 confirm please\n")
    assert BP.parse_eta_requested(body, ref_date=date(2026, 8, 20)) == "2026-06-01"
    assert HBP.parse_eta_requested(body, ref_date=date(2026, 8, 20)) == "2026-06-01"


def test_both_legs_take_the_year_from_the_message():
    """Fixing only the REQUESTED side turned a year-boundary wash into a
    ~360-day fabricated miss: before, both sides shared the same wrong year
    and cancelled."""
    dec = date(2026, 12, 10)
    ask = BP.parse_eta_requested("Oakland to X\nETA 1/15", ref_date=dec)
    offer = BP.parse_eta_offered("we can offer ETA 1/20", ref_date=dec)
    assert ask == "2027-01-15" and offer == "2027-01-20"
    assert core.requested_fit_days(
        {"eta_requested": ask, "eta_offered": offer}) == (5, "arrival")


def test_the_offered_cell_reader_is_the_same_in_both_trees():
    """The library tree returned None for "9-30-26" — a form OL really sends
    — while production read it fine."""
    row = {"eta_requested": "2026-09-15", "eta_offered": "9-30-26"}
    assert core.requested_fit_days(row) == (15, "arrival")
    assert HC.requested_fit_days(row) == core.requested_fit_days(row)


def test_a_transition_never_renders_the_same_value_at_both_ends():
    """Passing the row's CURRENT status as both ends made a stored LOSS→WIN
    render "WIN → WIN", erasing the very change the section exists to show."""
    row = {"status": "WIN", "quoted": True}
    frm = GE._status_change_pill("LOSS", row, "WIN")
    to = GE._status_change_pill("WIN", row, "LOSS")
    strip = lambda h: re.sub(r"<[^>]+>", "", h).strip()  # noqa: E731
    assert strip(frm) != strip(to), (frm, to)
    assert "WIN" in strip(to)


def test_a_loss_pill_is_red_not_the_grey_unknown_fallback():
    """"Q&L"/"NQ" are display words, not palette keys — passing them as the
    pill's STATUS turned every loss grey."""
    pill = GE._status_change_pill("LOSS", {"status": "LOSS", "quoted": True}, "QUOTED")
    assert "#f1f5f9" not in pill, "fell through to the grey unknown-status pill"


def test_the_reconciliation_never_calls_a_booking_a_loss():
    day = _report_day()
    won = _row("w1", "Oakland → Keelung", "WIN", day)
    won["mdolx_ref"] = "MDOLX260999"
    html = _report_html([won, _row("p1", "Oakland → Durban", "PENDING", day)])
    assert "booked" in html
    assert "Quoted &amp; Lost (Oakland → Keelung)" not in html


def test_a_locked_row_keeps_its_operator_value():
    """operator_corrections.json is the only durable human state here. An
    unconditional recompute must never outrank it."""
    import qc_selfheal as QS
    locked = {"request_id": "r_locked", "manual_locked": True, "quoted": True,
              "status": "LOSS", "etd_fit_days": 99, "etd_fit_basis": "arrival",
              "lane": "Oakland → Osaka", "eta_requested": "2026-09-15",
              "eta_offered": "10-Oct-26", "status_history": []}
    QS.phase_3_entries(QS.Log(), {"requests": [locked]})
    assert locked["etd_fit_days"] == 99


def test_a_cutoff_only_rfq_still_shows_its_date_to_the_reader():
    """The like-for-like split moved cutoff asks into etd_requested; a
    renderer reading only eta_requested prints "no ETA on request" for every
    one of them."""
    src = (ROOT / "scripts" / "gen_email.py").read_text(encoding="utf-8")
    assert "r.get('eta_requested') or r.get('etd_requested')" in src
    dash = (ROOT / "scripts" / "gen_dashboard.py").read_text(encoding="utf-8")
    assert 'r.get("eta_requested") or r.get("etd_requested")' in dash
