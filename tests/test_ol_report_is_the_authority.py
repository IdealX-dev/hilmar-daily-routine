"""OL's 2026 transaction report is the authority on what Hilmar booked.

Michael, 2026-08-13: "THE REPORT I UPLOADED EARLIER IS THE REPORT TO VERIFY
AND USE." And on the row that started it: "MDOLX260928 IS NOT ON THE
TRANSACTION REPORT I SENT YOU FOR HILMAR."

The reconciliation found ten wins the tracker held and that report does not,
and they turned out to be three different faults wearing the same face:

  NUMIDIA        260387 260388 260407 260486 260487 260928
                 A different customer whose cargo loads at the Hilmar plant.
                 Michael: "NUMIDIA IS NOT HILMAR.. THAT'S WHEN HILMAR IS
                 USED AS A LOCATION." Hilmar is a town in California as well
                 as the client, and the pipeline could not tell them apart.
  CANCELLED      260895
                 "*CANCELED BOOKING CONFIRMATION" counted as a win.
  UNCONFIRMED    260772 260905 260963
                 Real HILMAR-tagged mail, absent from OL's own book. Worth a
                 query to OL, excluded meanwhile because the report rules.

260928 is the one worth remembering: the operational-subject gate listed
"LOADING APPT" and OL wrote "LOAD APPTS". "LOADING" is not a prefix of
"LOAD ", so the string never matched, and a drayage leg from the town of
Hilmar to the Port of Oakland became a WIN on the lane "Oakland → Oakland".
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for p in (str(ROOT / "scripts"), str(ROOT / "src")):
    if p not in sys.path:
        sys.path.insert(0, p)

import ingest as IN  # noqa: E402

REPORT = ROOT / "data" / "ol-transaction-report-2026.json"
CORRECTIONS = ROOT / "scripts" / "operator_corrections.json"

#: Every MDOLX the reconciliation found in the tracker and not in OL's book
#: (diag-reconcile run 31701602704, diag-find run 31703548619).
UNLISTED = {"260387", "260388", "260407", "260486", "260487", "260928",
            "260895", "260772", "260905", "260963"}


def _corrections():
    return json.loads(CORRECTIONS.read_text(encoding="utf-8"))["corrections"]


# ── the gate that let a drayage leg become a win ─────────────────────────

def test_load_appts_is_recognised_as_operational():
    """The exact subject that produced MDOLX260928."""
    assert IN.is_operational_subject(
        "MDOLX260928_LOAD APPTS // 00+080435// 2X40'RF HILMAR -> OAKLAND")


def test_loading_appt_still_is_too():
    """The older spelling must keep working — the fix ADDS a string, it does
    not replace one, because OL writes both."""
    assert IN.is_operational_subject(
        "MDOLX260357_ *NEED TO SCHEDULE LOADING APPT // 1X40'RF")


def test_loading_is_not_a_prefix_of_load_space():
    """The reason the gap existed at all, stated as an assertion so nobody
    'simplifies' the two strings back into one."""
    assert "LOAD APPT" not in "LOADING APPT"
    assert "LOADING APPT" not in "LOAD APPTS"


def test_a_real_booking_confirmation_is_not_operational():
    """The gate must not start eating the mail it exists to protect."""
    for subject in [
        "MDOLX260963_NEW BOOKING CONFIRMATION// HILMAR 2X20'DV Oakland to HCMC",
        "MDOLX260772_NEW BOOKING CONFIRMATION// HILMAR 1X40'RF Oakland to Yokohama",
        "Oakland to Manila (North) / MDOLX261070 / ONE BKG # RICGAZ641400",
    ]:
        assert not IN.is_operational_subject(subject), subject


# ── Numidia: Hilmar the town, not Hilmar the client ──────────────────────

def test_numidia_mail_is_out_of_scope():
    """out_of_scope_reason already knew about Numidia; these subjects prove
    the rule fires on the real ones rather than a paraphrase."""
    for subject in [
        "MDOLX260388_ *REVISED BOOKING CONFIRMATION // NUMIDIA - 00+073690 / "
        "Hilmar, CA to Jakarta",
        "MDOLX260387_ *ROLLED BOOKING CONFIRMATION // NUMIDIA - 00+074173 / "
        "Hilmar, CA to Busan",
        "RE: MDOLX260487_ *NEW BOOKING REQUEST // NUMIDIA - 00+075061/ Port "
        "Oakland to Port Busan",
    ]:
        assert IN.out_of_scope_reason({"subject": subject}) == "numidia", subject


def test_hilmar_comma_ca_is_read_as_the_town():
    """The classifier that makes the distinction possible at all."""
    assert IN.hilmar_signal("Hilmar, CA to Jakarta") == "origin_city"
    assert IN.hilmar_signal("// HILMAR 2X20'DV Oakland to HCMC") == "tag"


# ── the tracker's win set vs OL's book ───────────────────────────────────

def test_every_unlisted_win_is_excluded():
    """Michael: "THE REPORT I UPLOADED EARLIER IS THE REPORT TO VERIFY AND
    USE." A win the report does not carry does not stay in the tracker."""
    excluded = {c["request_id"] for c in _corrections() if c.get("exclude")}
    by_ref = {}
    for c in _corrections():
        if c.get("exclude"):
            note = c.get("note") or ""
            for ref in UNLISTED:
                if ref in note:
                    by_ref[ref] = c["request_id"]
    missing = UNLISTED - set(by_ref)
    assert not missing, f"still counted as Hilmar wins: {sorted(missing)}"
    assert excluded  # sanity: the exclusions are really in the file


def test_no_excluded_ref_is_also_backfilled_as_a_win():
    """Excluding a row and creating a win for the same booking would cancel
    out into a silent re-add — the two mechanisms must not overlap."""
    created = {str(c.get("set", {}).get("mdolx_ref") or "")
               for c in _corrections() if c.get("create")}
    assert not (created & UNLISTED), sorted(created & UNLISTED)


def test_every_exclusion_says_why_and_cites_the_report():
    """A bare exclusion is indistinguishable from data loss six months on."""
    for c in _corrections():
        if not c.get("exclude"):
            continue
        note = c.get("note") or ""
        assert len(note) > 60, f"{c['request_id']}: no reason given"


def test_the_report_and_the_tracker_cannot_disagree_about_a_cancelled_row():
    """260895 was a *CANCELED BOOKING CONFIRMATION*. OL's export carries a
    cancelled flag and drops those, so a cancelled row counted as a win is
    exactly the kind of disagreement the reconciliation exists to surface."""
    refs = {r["mdolx"] for r in json.loads(REPORT.read_text(encoding="utf-8"))}
    assert "260895" not in refs
    notes = " ".join(c.get("note") or "" for c in _corrections()
                     if c.get("exclude"))
    assert "CANCELED" in notes or "CANCELLED" in notes


def test_the_report_still_holds_the_bookings_that_were_backfilled():
    """Guards the opposite mistake: an exclusion sweep that also removed the
    54 recovered wins would leave the tracker emptier than OL's book."""
    refs = {r["mdolx"] for r in json.loads(REPORT.read_text(encoding="utf-8"))}
    created = {str(c.get("set", {}).get("mdolx_ref") or "")
               for c in _corrections() if c.get("create")}
    created.discard("261071")          # from Linda's recap, not this report
    assert created <= refs, sorted(created - refs)


# ── one carrier, one name ────────────────────────────────────────────────

def test_ols_legal_carrier_names_collapse_to_the_trade_names():
    """The customer transaction report names carriers as LEGAL entities.
    Without these aliases ONE appears twice — 38 bookings as "ONE" and 19
    backfilled as "OCEAN NETWORK EXPRESS PTE, LTD" — and every carrier
    rollup splits one carrier in two, which defeats the point of the
    backfill ("so we can see complete and total volumes booked on lanes")."""
    import core as C
    for raw, want in [
        ("OCEAN NETWORK EXPRESS PTE, LTD", "ONE"),
        ("CMA CGM SA", "CMA CGM"),
        ("MEDITERRANEAN SHIPPING LINES", "MSC"),
        ("HAPAG-LLOYD AMERICA", "Hapag-Lloyd"),
        ("EVERGREEN SHIPPING AGENCY (AMERICA)", "Evergreen"),
        ("HYUNDAI MERCHANT MARINE INC.", "HMM"),
    ]:
        assert C.normalize_carrier(raw) == want, raw


def test_every_carrier_in_the_export_normalises_to_something_known():
    """A carrier spelling this file has never seen passes through unchanged
    and quietly becomes its own row in the rollup. Better to fail here."""
    import core as C
    rows = json.loads(REPORT.read_text(encoding="utf-8"))
    canon = set(C.CARRIER_ALIASES.values())
    unknown = {r["carrier"] for r in rows
               if r.get("carrier") and C.normalize_carrier(r["carrier"]) not in canon}
    assert not unknown, f"carrier spellings with no canonical form: {sorted(unknown)}"


def test_the_two_cores_agree_on_the_new_aliases():
    import core as C

    from hilmar import core as HC
    assert C.CARRIER_ALIASES == HC.CARRIER_ALIASES


def test_the_stored_export_carries_the_richer_customer_report_fields():
    """Michael sent customertransactionreport_20260813093333.xls: "this is
    another report for only client hilmar". Same 134 bookings, but it also
    names the consignee, the vessel and the carrier's booking number."""
    rows = json.loads(REPORT.read_text(encoding="utf-8"))
    assert len(rows) == 134
    withc = [r for r in rows if r.get("consignee")]
    assert len(withc) > 100, "the consignee column did not survive extraction"
    assert any(r.get("booking_no") for r in rows)
    assert any(r.get("vessel") for r in rows)


def test_the_destination_agent_is_not_recorded_as_a_place():
    """The report's "destinationoffice" column holds OL's destination AGENT
    (QUANTERM LOGISTICS VIETNAM), and it matched the "destination" alias.
    A freight agent recorded as a place of delivery is a lane that does not
    exist."""
    rows = json.loads(REPORT.read_text(encoding="utf-8"))
    agenty = [r["mdolx"] for r in rows
              if "LOGISTICS" in (r.get("final_destination") or "").upper()
              or "QUANTERM" in (r.get("final_destination") or "").upper()]
    assert not agenty, f"an agent office became a destination: {agenty}"


# ── what a backfilled win can and cannot be graded on ────────────────────

def test_a_backfilled_win_is_treated_as_having_no_rate_chain():
    """QC-039 blocked the 2026-08-13 fire at 92.3%. Cause: _is_standalone
    matched only the 'stand_' prefix, so the 49 rows recovered from OL's
    export — which have NO email at all — were graded as missing ol_rate,
    etd_offered, eta_offered, dest_free_time, product and lonny_notes.
    Every one of those requires a message the row does not have.

    This is not a way to excuse a parser miss: the row qualifies only
    because there is nothing to parse."""
    from hilmar import parser_accuracy as PA
    row = {"request_id": "ol_252071", "status": "WIN", "quoted": True}
    assert PA._is_standalone(row) is True
    assert PA._is_chain_quoted(row) is False
    assert PA.FIELD_REQUIREMENTS["ol_rate"](row) is False
    assert PA.FIELD_REQUIREMENTS["product"](row) is False


def test_the_stand_prefix_still_qualifies():
    from hilmar import parser_accuracy as PA
    assert PA._is_standalone({"request_id": "stand_260905"}) is True


def test_an_ordinary_request_is_still_graded_on_its_rate():
    """The exemption must not widen to rows that DO have a chain — those
    are where a real parser miss shows up."""
    from hilmar import parser_accuracy as PA
    row = {"request_id": "req_abc123", "status": "LOSS", "quoted": True}
    assert PA._is_standalone(row) is False
    assert PA.FIELD_REQUIREMENTS["ol_rate"](row) is True


def test_container_count_is_graded_on_every_row_including_backfills():
    """It is NOT exempted — it is populated from OL's equipment column.
    Exempting it would have hidden real missing volume data."""
    from hilmar import parser_accuracy as PA
    assert PA.FIELD_REQUIREMENTS["container_count"](
        {"request_id": "ol_252071", "status": "WIN"}) is True
