"""OL writes a sail date two ways. The client's report only understood one.

2026-08-10, chasing QC-027's ETA at 93.3% — the only field left under 95%
after Carrier was resolved. Both forms are in the live data, on rows created
within weeks of each other (diag-bookings run 3):

    stand_260769   etd=22-Apr-26   eta=26-May-26      <- d-Mmm-yy
    req_5d2685f3…  etd=1-Jul-26    eta=2026-07-25     <- ISO

and THREE different parsers were reading that one field:

    share_intel.py:255        _parse_loose_date   internal feed   loose
    gen_client_email.py:326   _iso_date           LONNY'S EMAIL   strict
    gen_client_weekly.py:184  _iso_date           LONNY'S WEEKLY  strict

`_iso_date` is `strptime(s[:10], "%Y-%m-%d")`. So a `26-May-26` ETA is truthy
for QC-027 — the field IS populated, it counts toward the 93.3% — and invisible
to "Currently in transit", which drops any row whose ETA will not parse. The
internal intel feed saw those shipments; the customer's report did not.

Michael's rule for that section, 2026-07-22: "for current shipments only those
with eta's that haven't happened yet." A row with a real, future ETA written
`26-Aug-26` obeyed that rule and was dropped anyway.

Two more ways the same field was lost, both fixed here:

  * ingest.py:1255 assigned eta_offered UNCONDITIONALLY. Lonny re-uses Outlook
    threads, so a second rate response carrying no ETA nulled a good one. The
    correct shape was already two lines below on pol/pod.
  * patch_carriers.py:633 — the gate deciding whether to go looking for
    missing fields named etd/vessel/rate and omitted eta. Every one of the 22
    ETA-missing rows carries etd+vessel+rate, so every one failed the gate and
    never triggered the sibling lookup. Gradeable, but unreachable.
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for p in (str(ROOT / "scripts"), str(ROOT / "src")):
    if p not in sys.path:
        sys.path.insert(0, p)

import core  # noqa: E402

# Verbatim from the production rows above.
ETA_DMY = "26-May-26"
ETA_ISO = "2026-07-25"
ETD_DMY = "22-Apr-26"


# ── the predicate ───────────────────────────────────────────────────────────

def test_both_production_forms_parse_to_the_same_kind_of_answer():
    """THE defect, with the two real strings that sat in one dataset."""
    assert core.offered_date(ETA_DMY) == date(2026, 5, 26)
    assert core.offered_date(ETA_ISO) == date(2026, 7, 25)
    assert core.offered_date(ETD_DMY) == date(2026, 4, 22)


def test_the_forms_the_third_parser_could_read_are_not_lost():
    """share_intel's private parser handled non-zero-padded M/D/YY, and the
    others did not. Folding three parsers into one must not drop what any of
    them could already do."""
    assert core.offered_date("7/25/2026") == date(2026, 7, 25)
    assert core.offered_date("7/5/26") == date(2026, 7, 5)
    assert core.offered_date("1-Jul-26") == date(2026, 7, 1)


def test_a_yearless_cell_is_none_not_a_1900_date():
    """strptime defaults "%b %d" to 1900. That is a FABRICATED date, not a
    parse — and a 1900 sail date sorts to the front of the client's transit
    table. The cell genuinely does not say which year."""
    assert core.offered_date("Jul 25") is None
    assert core.offered_date("Jul 25", fallback_year=2026) == date(2026, 7, 25)


def test_junk_and_empties_are_none():
    for bad in (None, "", "   ", "garbage", "TBD", "see attached"):
        assert core.offered_date(bad) is None, f"{bad!r} parsed to a date"


# ── the consequence, end to end ─────────────────────────────────────────────

def _win(rid, eta, etd=ETD_DMY, req="2026-08-05"):
    return {
        "request_id": rid, "status": "WIN", "quoted": True,
        "mdolx_ref": "260900", "mdolx_refs_all": ["260900"],
        "origin": "Oakland", "destination": "Yokohama",
        "lane": "Oakland → Yokohama",
        "carrier_won": "CMA CGM", "carrier_quoted": "CMA CGM",
        "ol_rate": 3150.0, "vessel_voyage": "EVER GIVEN 021E",
        "etd_offered": etd, "eta_offered": eta,
        "request_date": req, "request_timestamp": f"{req}T12:00:00Z",
        "response_timestamp": f"{req}T15:00:00Z",
        "teu_won": 4, "container_count": 2, "status_history": [],
        "source_imids": [f"<{rid}@ol>"],
    }


def test_a_future_eta_in_the_dmy_form_reaches_the_client_transit_table():
    """The customer-visible consequence. Both rows are current shipments by
    Michael's rule; before the fix only the ISO one appeared."""
    import gen_client_email as G
    report_date = date(2026, 8, 10)
    rows = [_win("iso", "2026-09-15"), _win("dmy", "15-Sep-26")]
    got = G._active_shipments({"requests": rows}, report_date)
    ids = {r["request_id"] for r in got}
    assert "dmy" in ids, (
        "a real, future ETA written 15-Sep-26 is still missing from the "
        "client's current-shipments table")
    assert ids == {"iso", "dmy"}


def test_a_past_eta_is_still_excluded_in_both_forms():
    """The half that must NOT regress. Michael 2026-07-22: "only those with
    eta's that haven't happened yet." Parsing MORE dates must not start
    showing shipments that already arrived."""
    import gen_client_email as G
    report_date = date(2026, 8, 10)
    rows = [_win("old_iso", "2026-06-01"), _win("old_dmy", "1-Jun-26")]
    assert G._active_shipments({"requests": rows}, report_date) == []


def test_the_weekly_reads_the_same_forms_as_the_daily():
    """Two client reports disagreeing about which shipments are live is the
    same defect one layer up."""
    import gen_client_weekly as W
    src = (ROOT / "scripts/gen_client_weekly.py").read_text(encoding="utf-8")
    assert "core.offered_date(r.get(\"eta_offered\"))" in src, (
        "the weekly still parses ETA with the strict ISO reader")
    assert W is not None


# ── one predicate, not three ────────────────────────────────────────────────

def test_the_client_renderers_do_not_date_parse_offered_dates_themselves():
    """A completeness number is only as good as the reader behind it. Any
    renderer that re-implements this will drift from QC-027 again."""
    for name in ("gen_client_email.py", "gen_client_weekly.py"):
        src = (ROOT / "scripts" / name).read_text(encoding="utf-8")
        for field in ("eta_offered", "etd_offered"):
            assert f'_iso_date(r.get("{field}"))' not in src, (
                f"{name} still reads {field} through the strict ISO parser")


def test_share_intel_delegates_rather_than_keeping_its_own_patterns():
    src = (ROOT / "scripts/share_intel.py").read_text(encoding="utf-8")
    assert "core.offered_date(s)" in src
    assert '"%d-%b-%y"' not in src, (
        "share_intel still carries its own format table — a third reader of "
        "the same field is how this started")


def test_transit_math_still_works_through_the_shared_reader():
    """share_intel's transit-days was the one reader that WORKED. Routing it
    through core must not break it."""
    import share_intel as SI
    assert SI._transit_days({"etd_offered": "1-Jul-26",
                             "eta_offered": "2026-07-25"}) == 24
    assert SI._transit_days({"etd_offered": "22-Apr-26",
                             "eta_offered": "26-May-26"}) == 34
    assert SI._transit_days({"etd_offered": None, "eta_offered": ETA_ISO}) is None


# ── the two write-side losses ───────────────────────────────────────────────

def test_a_later_rate_response_without_an_eta_does_not_null_a_good_one():
    """ingest.py:1255 was unconditional, and Lonny re-uses Outlook threads."""
    src = (ROOT / "scripts/ingest.py").read_text(encoding="utf-8")
    i = src.find('best["eta_offered"] =')
    assert i != -1, "the ETA assignment is gone"
    stmt = src[i:i + 220]
    assert 'best.get("eta_offered")' in stmt, (
        "the ETA assignment does not fall back to the value already on the "
        "row — a second rate response with no ETA still nulls a good one")


def test_a_stated_eta_still_wins_over_the_preserved_one():
    """Preserving must not freeze the first ETA forever: a revised sailing
    has to be able to replace it."""
    src = (ROOT / "scripts/ingest.py").read_text(encoding="utf-8")
    i = src.find('best["eta_offered"] =')
    stmt = src[i:i + 220]
    assert stmt.find('rt.get("eta")') < stmt.find('best.get("eta_offered")'), (
        "the row's existing ETA is consulted BEFORE the new email's — a "
        "revised sail date could never land")


def test_the_backfill_gate_can_fire_for_a_missing_eta():
    """patch_carriers could always WRITE eta_offered; the gate that decides
    whether to go looking omitted it, and every ETA-missing row carries
    etd+vessel+rate — so the gate never fired for exactly the rows that
    needed it."""
    src = (ROOT / "scripts/patch_carriers.py").read_text(encoding="utf-8")
    i = src.find("needs_fields = not all(")
    assert i != -1, "the sibling-lookup gate is gone"
    assert "eta_offered" in src[i:i + 260], (
        "eta_offered is still absent from the sibling-lookup trigger")
