"""The weekly tiles must add up, and no rate may exceed 100%.

Michael, 2026-08-24, on the Aug 17-21 summary:
    "how are there 16 requests with 9 wins and 10 losses   that would be
     19 requests"
and on the 4-week trend, where Aug 10-14 read 12 requests / 17 wins / 175%:
    "how more wins then requests"
    "that's unusual and sounds like bad parse"

Both were right, and they were two different defects wearing one symptom.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import core  # noqa: E402
import gen_weekly_summary as GWS  # noqa: E402


def _won(rid, refs=("A",), teu=4, booked="2026-08-19T12:00:00+00:00"):
    return {"request_id": rid, "status": "WIN", "quoted": True,
            "mdolx_ref": refs[0], "mdolx_refs_all": list(refs),
            "teu_won": teu, "teu_requested": teu,
            "booking_timestamp": booked,
            "request_timestamp": "2026-08-18T12:00:00+00:00",
            "request_date": "2026-08-18",
            "status_history": [{"at": booked, "from": "PENDING", "to": "WIN"}]}


def _lost(rid):
    return {"request_id": rid, "status": "LOSS", "quoted": True, "teu_requested": 2,
            "request_timestamp": "2026-08-18T12:00:00+00:00",
            "request_date": "2026-08-18", "status_history": []}


WEEK = ("2026-08-17", "2026-08-21")


def _week(rows):
    from datetime import date
    s, e = date(2026, 8, 17), date(2026, 8, 21)
    return GWS.analyze_week(GWS._filter_rows(rows, s, e),
                            GWS._filter_wins(rows, s, e))


# ── The arithmetic Michael did in his head ─────────────────────────────────

def test_the_parts_sum_to_the_total():
    m = _week([_won("w1"), _won("w2"), _lost("l1"), _lost("l2")])
    assert m["total"] == m["wins"] + m["ql"] + m["nq"] + m["pending"]
    assert m["total"] == 4


def test_wins_can_never_exceed_requests():
    """The trend table read 12 requests against 17 wins."""
    m = _week([_won(f"w{i}") for i in range(17)] + [_lost("l1")])
    assert m["wins"] <= m["total"], (m["wins"], m["total"])


def test_no_rate_can_exceed_one_hundred_percent():
    """125.0% quote rate and 175.0% before it."""
    for rows in ([_won("w1")], [_won("w1"), _lost("l1")],
                 [_won(f"w{i}") for i in range(9)] + [_lost(f"l{i}") for i in range(10)]):
        m = _week(rows)
        assert 0 <= m["win_rate"] <= 100, m
        assert 0 <= m["quote_rate"] <= 100, m


def test_the_reported_week_now_reconciles():
    """9 wins and 10 losses is 19 shipments, and the tile says 19 — not 16."""
    m = _week([_won(f"w{i}") for i in range(9)] + [_lost(f"l{i}") for i in range(10)])
    assert (m["wins"], m["ql"], m["total"]) == (9, 10, 19)
    assert m["win_rate"] == round(9 / 19 * 100, 1)


# ── One quote, several bookings ────────────────────────────────────────────

def test_three_bookings_on_one_quote_are_three_wins():
    """Michael: "no it would be three requests to three wins"."""
    m = _week([_won("w1", refs=("A", "B", "C"))])
    assert m["wins"] == 3
    assert m["total"] == 3


def test_booking_count_is_never_zero_for_a_real_win():
    assert core.booking_count({"status": "WIN"}) == 1
    assert core.booking_count({"status": "LOSS", "quoted": True}) == 0


# ── The bad parse: a booking dated when we were told, not when it happened ──

def test_a_back_entered_booking_keeps_its_real_date():
    """18 corrections stamped →WIN at fire time on 2026-08-13, putting
    Jan-Apr bookings into the week of Aug 10-14."""
    r = {"status": "WIN", "booking_timestamp": "2026-01-08T12:00:00+00:00",
         "status_history": [{"at": "2026-08-13T14:00:00+00:00",
                             "from": "PENDING", "to": "WIN"}]}
    assert core.win_event_date(r) == "2026-01-08"


def test_a_normal_win_still_uses_its_transition():
    r = {"status": "WIN",
         "status_history": [{"at": "2026-08-20T14:00:00+00:00",
                             "from": "PENDING", "to": "WIN"}]}
    assert core.win_event_date(r) == "2026-08-20"


def test_the_applier_no_longer_stamps_a_correction_with_today():
    src = (ROOT / "scripts" / "ingest.py").read_text(encoding="utf-8")
    i = src.index('new_status = changes.get("status", prior_status)')
    block = src[i:i + 1400]
    assert '"at": _at' in block
    assert '"at": C.now_utc().isoformat()' not in block, (
        "a back-entered booking is being dated to the fire, not to when it "
        "was booked")


# ── QC-080: the check that watches for the re-dating from outside ──────────

def _qc(rows, capsys):
    import qc_selfheal as QS
    QS.phase_6_rules(QS.Log(), {"requests": rows})
    return capsys.readouterr().out


def test_qc080_flags_a_one_day_win_cluster(capsys):
    """54 of 60 wins on one calendar day is not a busy Tuesday."""
    flush = [dict(_won(f"f{i}"), booking_timestamp=None,
                  status_history=[{"at": "2026-08-13T14:00:00+00:00",
                                   "from": "PENDING", "to": "WIN"}])
             for i in range(24)]
    spread = [_won(f"s{i}", booked=f"2026-0{1 + i % 6}-1{i % 9}T12:00:00+00:00")
              for i in range(10)]
    out = _qc(flush + spread, capsys)
    assert "QC-080" in out
    assert "2026-08-13" in out
    assert "signature of a re-dating bug" in out


def test_qc080_quiet_when_wins_are_spread(capsys):
    spread = [_won(f"s{i}", booked=f"2026-0{1 + i % 6}-1{i % 9}T12:00:00+00:00")
              for i in range(30)]
    out = _qc(spread, capsys)
    assert "QC-080" in out
    assert "no re-dating cluster" in out
