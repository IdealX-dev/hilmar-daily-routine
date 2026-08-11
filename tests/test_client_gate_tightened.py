"""Full tightening of the Hilmar client gate — Michael, 2026-08-10.

I recommended leaving it loose. Michael: "i want full tightening." His call,
and the work is here. What follows is how it was tightened WITHOUT the failure
mode I flagged, because that risk was real and does not go away by being
overruled: a stricter client gate fails by making a real win quietly stop
existing.

THE AMBIGUITY. Hilmar Ingredients is physically in HILMAR, CALIFORNIA, so
"HILMAR" is both the customer tag and the origin city. `"HILMAR" in subject`
cannot tell "// HILMAR" from "Hilmar, CA to La Guaira, Venezuela". That is
exactly how stand_260821 leaked — Agri Dairy cargo loading at the Hilmar plant,
flagged by Michael 2026-07-01 ("only moves booked by Lonny are Hilmar the
client") and patched then with a subject/body regex for "agri dairy".

WHAT TIGHTENING IS *NOT*. Requiring a "// HILMAR" tag. A genuine Hilmar move
can describe the lane and never name the customer, so that rule would drop real
wins — the concern I raised, and the reason it is not the design.

WHAT IT IS, three parts:

  1. hilmar_signal() classifies the mention: 'tag' | 'origin_city' | None.
     Any tag-shaped mention wins outright.
  2. out_of_scope_mdolx() makes the check THREAD-LEVEL. One MDOLX is one
     shipment, so it has one paying customer; if any message carrying that
     number names a different customer, the number is theirs. Per-row
     filtering could never see that — it caught the Agri Dairy sibling only
     when its text happened to be quoted into a fetched body.
  3. The other-customer list now covers the customers actually in this
     mailbox (Hoogwegt, Erno Laszlo, Brisar), sourced from senders observed in
     the live stage, not invented.

AND THE ESCAPE HATCH THAT KEEPS IT SAFE: an explicit "// HILMAR" tag overrides
the thread verdict. Hilmar and another customer can share a thread — same
plant, same week — and a stated customer tag is not something to discard on a
sibling's say-so. Only the ambiguous origin-city-only rows defer.

Every thread-level drop is printed with its MDOLX, reason and subject. A
booking count that falls with no line saying why is indistinguishable from the
pipeline breaking.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for p in (str(ROOT / "scripts"), str(ROOT / "src")):
    if p not in sys.path:
        sys.path.insert(0, p)

import ingest as IN  # noqa: E402

# Verbatim production strings (diag-bookings runs 3-6, 2026-08-10).
LANE_ONLY = ("RE: Hilmar, CA to La Guaira, Venezuela - S38083 / "
             "MDOLX260821 - Puerto Cabello. / EBKG17621387")
AGRI_SIBLING = ("RE: MDOLX260821_Load appointment needed for 1 x 40' HC  / "
                "Agri Dairy Vendor Reference PO00-26002163 / 93348")
TAGGED = ("MDOLX260769_ *NEW BOOKING CONFIRMATION // HILMAR - Oakland to "
          "Osaka - 3X40'RF // CMA BKG # NAM8482648")
HOOGWEGT = ("RE: MDOLX260789_// HOOGWEGT - Chicago to Jakarta - 6x40' - "
            "S210086014609-001 // ONE LINE BKG # 12345")


def _row(subject, sent="2026-07-13T14:29:47Z", imid="<x>", preview="", body=""):
    return {"bucket": "mbd_inbound", "subject": subject, "sent": sent,
            "received": sent, "imid": imid, "summary_preview": preview,
            "text_body": body}


# ── 1. the signal classifier ────────────────────────────────────────────────

def test_a_customer_tag_reads_as_a_tag():
    assert IN.hilmar_signal(TAGGED) == "tag"
    assert IN.hilmar_signal("MDOLX1_ BOOKING // HILMAR // MSC") == "tag"
    assert IN.hilmar_signal("HILMAR INGREDIENTS - Oakland to Busan") == "tag"


def test_an_origin_city_reads_as_a_city():
    assert IN.hilmar_signal(LANE_ONLY) == "origin_city"
    assert IN.hilmar_signal("Hilmar, CA to Osaka") == "origin_city"
    assert IN.hilmar_signal("Hilmar CA to Osaka") == "origin_city"
    assert IN.hilmar_signal("from Hilmar, California to Kobe") == "origin_city"


def test_a_tag_anywhere_beats_a_city_mention():
    """A subject naming both is unambiguously ours."""
    assert IN.hilmar_signal("Hilmar, CA to Osaka // HILMAR - 2X40'RF") == "tag"


def test_no_mention_is_none():
    assert IN.hilmar_signal(HOOGWEGT) is None
    assert IN.hilmar_signal("Oakland to Qingdao 14x40'HC") is None
    assert IN.hilmar_signal("") is None
    assert IN.hilmar_signal(None) is None


# ── 2. thread-level exclusion ───────────────────────────────────────────────

def test_a_sibling_naming_another_customer_condemns_the_whole_mdolx():
    """THE leak, reproduced. The booking subjects are clean; only the Jul 22
    sibling says Agri Dairy — and per-row filtering never connects them."""
    rows = [_row(LANE_ONLY), _row(AGRI_SIBLING)]
    excluded = IN.out_of_scope_mdolx([r for r in rows
                                      if IN.out_of_scope_reason(r)])
    assert excluded.get("260821") == "agridairy", (
        "the Agri Dairy sibling did not condemn MDOLX260821 — the thread-level "
        "signal is not being derived")

    got = IN.collect_bookings([_row(LANE_ONLY)], excluded_mdolx=excluded)
    assert got == {}, (
        "MDOLX260821 still becomes a Hilmar booking; this is the stand_260821 "
        "leak Michael flagged on 2026-07-01")


def test_the_same_booking_is_admitted_when_no_sibling_condemns_it():
    """Non-vacuity, and the thing that makes the rule safe: the identical
    lane-only subject IS a Hilmar booking when nothing says otherwise."""
    got = IN.collect_bookings([_row(LANE_ONLY)], excluded_mdolx={})
    assert "260821" in got, (
        "a lane-only Hilmar subject is now dropped even with a clean thread — "
        "that is the real-win loss this design exists to avoid")


def test_an_explicit_tag_survives_a_condemned_thread():
    """THE ESCAPE HATCH. Hilmar and another customer can share a thread; a
    stated customer tag is not discarded on a sibling's say-so."""
    tagged_in_bad_thread = ("MDOLX260821_ *NEW BOOKING CONFIRMATION // HILMAR - "
                            "Hilmar, CA to Osaka - 2X40'RF // MSC BKG # 999")
    got = IN.collect_bookings([_row(tagged_in_bad_thread)],
                              excluded_mdolx={"260821": "agridairy"})
    assert "260821" in got, (
        "an explicit // HILMAR tag was overridden by a sibling — a real, "
        "clearly-labelled Hilmar win just disappeared")


def test_the_old_behaviour_is_still_reachable_for_comparison():
    """Passing no exclusions reproduces the pre-tightening gate, which is what
    the blast-radius diagnostic diffs against. Without this, 'what did the
    tightening change' is unanswerable on real data."""
    got = IN.collect_bookings([_row(LANE_ONLY)])
    assert "260821" in got


# ── 3. the widened customer list ────────────────────────────────────────────

def test_the_other_mailbox_customers_are_recognised():
    """Sourced from senders seen in the live stage 2026-08-10, not invented."""
    assert IN.out_of_scope_reason(_row(HOOGWEGT)) == "other_client"
    assert IN.out_of_scope_reason(
        _row("RE: MDOLX261050_ // Erno Laszlo - VTM Sachets")) == "other_client"
    assert IN.out_of_scope_reason(
        _row("RE: MDOLX261050_ x", body="claza@brisar.com wrote:")) == "other_client"


def test_the_widened_list_does_not_fire_on_ports_or_carriers():
    """Word-bounded on purpose: a customer name that matched a substring of a
    port or carrier would silently delete real Hilmar business."""
    for subject in (
        "MDOLX1_ BOOKING // HILMAR - Oakland to Rotterdam // ONE",
        "MDOLX2_ BOOKING // HILMAR - Oakland to Busan // HMM",
        "MDOLX3_ BOOKING // HILMAR - Houston to Brisbane // MSC",
        "MDOLX4_ BOOKING // HILMAR - Oakland to Yokohama // CMA CGM",
    ):
        assert IN.out_of_scope_reason(_row(subject)) is None, subject


def test_the_pre_existing_exclusions_still_fire():
    """Tightening must not disturb the rules that already worked."""
    assert IN.out_of_scope_reason(_row("RE: NUMIDIA - 00+080007")) == "numidia"
    assert IN.out_of_scope_reason(_row(AGRI_SIBLING)) == "agridairy"
    assert IN.out_of_scope_reason(_row("FTL Modesto CA to Sturgis MI")) == "trucking"
    assert IN.out_of_scope_reason(_row("Recall: MDOLX1_ booking")) == "recalled"


# ── 4. it can never be silent ───────────────────────────────────────────────

def test_every_thread_level_drop_is_reported():
    """A booking count that falls with no line saying why is indistinguishable
    from the pipeline breaking."""
    seen = []
    IN.collect_bookings([_row(LANE_ONLY)],
                        excluded_mdolx={"260821": "agridairy"},
                        log_excluded=lambda m, why, subj: seen.append((m, why, subj)))
    assert len(seen) == 1
    mdolx, why, subj = seen[0]
    assert (mdolx, why) == ("260821", "agridairy")
    assert "La Guaira" in subj, "the drop is reported without the evidence"


def test_production_passes_both_the_exclusions_and_a_logger():
    """The wiring, asserted — the rule is worthless if main() never hands it
    the thread verdict, and unreviewable if it never hands it a logger."""
    src = (ROOT / "scripts/ingest.py").read_text(encoding="utf-8")
    i = src.find("bookings = collect_bookings(")
    assert i != -1, "the production call site is gone"
    call = src[i:i + 200]
    assert "excluded_mdolx=" in call, "main() never passes the thread verdict"
    assert "log_excluded=" in call, "thread-level drops are silent in production"


def test_the_diagnostic_attaches_bodies_like_production_does():
    """out_of_scope_reason reads text_body, and ingest.main attaches bodies
    BEFORE it filters. Without that, the diagnostic returns None where
    production returns 'agridairy' — which is exactly how it reported
    MDOLX260821 as "admitted by every gate" for a thread the pipeline had been
    excluding correctly since July."""
    src = (ROOT / "scripts/diag_bookings.py").read_text(encoding="utf-8")
    assert 'r["text_body"] = body' in src, (
        "the diagnostic runs the gates without bodies, so its verdicts are "
        "weaker than production's and it will invent 'admitted' rows again")


def test_the_blast_radius_is_measured_on_the_rows_production_would_see():
    """ingest.main removes out-of-scope rows and THEN calls collect_bookings,
    so a row whose own subject says NUMIDIA never reaches the collector in
    production. Run 7 compared against unfiltered rows and reported 4 lost
    bookings, two of which name NUMIDIA in their own subject — they were never
    in production's "before", so counting them overstates the change. A
    blast-radius number measured in a different order than the pipeline runs
    is not a blast-radius number."""
    src = (ROOT / "scripts/diag_bookings.py").read_text(encoding="utf-8")
    assert "_kept_rows = [r for r in rows if not IN.out_of_scope_reason(r)]" in src, (
        "the comparison does not pre-filter, so it measures a 'before' the "
        "pipeline never had")
    assert "IN.collect_bookings(_kept_rows)" in src, (
        "the OLD-gate side still runs over unfiltered rows")


def test_the_diagnostic_measures_the_blast_radius():
    """The tightening ships with its diff against real staged mail measured,
    not asserted safe. A stricter client gate fails by making a real win
    quietly stop existing."""
    src = (ROOT / "scripts/diag_bookings.py").read_text(encoding="utf-8")
    assert "collect_bookings(_kept_rows, excluded_mdolx=" in src, (
        "nothing runs the NEW gate, so there is nothing to diff")
    assert "collect_bookings(_kept_rows)" in src, (
        "nothing runs the OLD gate, so there is nothing to diff against")
    assert "hilmar_signal(subj)" in src, (
        "the dropped list does not say whether each drop was a tag or a "
        "city mention — the one fact that says if it was safe")


def test_the_thread_verdict_is_built_before_the_rows_are_filtered():
    """The out-of-scope siblings are REMOVED from `rows` by the per-row filter.
    Deriving the thread verdict after that point would always find nothing —
    a rule that looks right and does nothing."""
    src = (ROOT / "scripts/ingest.py").read_text(encoding="utf-8")
    build = src.find("_excluded_mdolx = out_of_scope_mdolx(_dropped_rows)")
    reassign = src.find("rows = _kept_rows")
    assert build != -1, "the thread verdict is not derived from the dropped rows"
    assert build < reassign, (
        "the thread verdict is built after `rows` is replaced by the kept set, "
        "so the dropped siblings are already gone and it will always be empty")
