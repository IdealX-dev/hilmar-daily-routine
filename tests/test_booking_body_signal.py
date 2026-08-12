"""OL stopped putting HILMAR in the booking subject, and we stopped counting wins.

2026-08-12. Michael sent Linda Echevarria's reply with two real bookings
attached, plus OL's own recap of 35 Hilmar bookings Jun 1 - Aug 12. Measured
on the two .eml files, verbatim:

  "MDOLX260963_NEW BOOKING CONFIRMATION// HILMAR 2X20'DV Oakland to HCMC..."
      subject has HILMAR: True    body has HILMAR: True
  "RE: Oakland to Manila (North) / MDOLX261070 / ONE BKG # RICGAZ641400"
      subject has HILMAR: FALSE   body has HILMAR: True  (7x "HILMAR INGREDIENTS")

Same desk, same customer, different subject convention. collect_bookings
gated on hilmar_signal(SUBJECT) alone — the "full tightening" Michael asked
for on 2026-08-10 — so every new-format booking was discarded. 15 of the 35
bookings in OL's recap (MDOLX261025-261072) never reached the tracker.

diag_find is what separated this from the delivery question it was mistaken
for: the Manila message was STAGED, bucket=mbd_inbound, body fetched (9224
chars), and "261070: NO tracking row carries this ref". Present, read, and
thrown away here.

The body is the weaker signal ON PURPOSE. Only a subject tag overrides the
thread-level exclusion, so a forwarded digest that happens to mention Hilmar
still cannot claim another customer's MDOLX.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for p in (str(ROOT / "scripts"), str(ROOT / "src")):
    if p not in sys.path:
        sys.path.insert(0, p)

import ingest as IN  # noqa: E402

# The real subjects, from the two messages Michael forwarded.
NEW_FORMAT = "RE: Oakland to Manila (North) / MDOLX261070 / ONE BKG # RICGAZ641400"
OLD_FORMAT = ("MDOLX260963_NEW BOOKING CONFIRMATION// HILMAR 2X20'DV "
              "Oakland to HCMC (Cat Lai)// ONE: RICGZ5629700")
BODY_WITH_CUSTOMER = (
    "Booking confirmation\nShipper: HILMAR INGREDIENTS\n"
    "lupfold@hilmaringredients.com\nPOL Oakland POD Manila (North)\n")


def _row(subject, body="", bucket="mbd_inbound", **kw):
    r = {"bucket": bucket, "subject": subject, "text_body": body,
         "summary_preview": "", "sent": "2026-08-12T14:25:35Z"}
    r.update(kw)
    return r


def test_the_new_format_booking_is_counted():
    """THE regression: no HILMAR in the subject, seven in the body."""
    got = IN.collect_bookings([_row(NEW_FORMAT, BODY_WITH_CUSTOMER)])
    assert "261070" in got, (
        "MDOLX261070 was dropped — OL's current booking subject carries no "
        "HILMAR tag, so a subject-only gate loses every win they send")


def test_the_old_format_still_works():
    """Nothing about the path that was working may change."""
    got = IN.collect_bookings([_row(OLD_FORMAT, BODY_WITH_CUSTOMER)])
    assert "260963" in got


def test_a_row_with_hilmar_nowhere_is_still_dropped():
    """The gate is widened to the body, not removed. Another customer's
    booking in the same mailbox must still be refused."""
    got = IN.collect_bookings([_row(
        "RE: Quote Lubbock TX to Port Klang / MDOLX261099 / CMA",
        "Shipper: NUMIDIA BV\nPOL Oakland POD Port Klang\n")])
    assert got == {}, f"a non-Hilmar booking was admitted: {sorted(got)}"


def test_a_body_signal_defers_to_the_thread_verdict():
    """One MDOLX is one shipment, so it has one paying customer. A body
    mention is corroboration, never an override — otherwise a forwarded
    digest could claim another customer's number."""
    got = IN.collect_bookings(
        [_row(NEW_FORMAT, BODY_WITH_CUSTOMER)],
        excluded_mdolx={"261070": "numidia"})
    assert got == {}, (
        "a body-signalled row overrode the thread-level exclusion — that is "
        "the stand_260821 Agri Dairy leak coming back")


def test_a_subject_tag_still_overrides_the_thread_verdict():
    """Unchanged: Hilmar and another customer can share a thread, and an
    explicit customer tag is not discarded on a sibling's say-so."""
    got = IN.collect_bookings(
        [_row(OLD_FORMAT, BODY_WITH_CUSTOMER)],
        excluded_mdolx={"260963": "numidia"})
    assert "260963" in got


def test_the_body_is_read_from_the_field_attach_bodies_populates():
    """attach_bodies writes text_body and runs before collect_bookings in
    main(). Reading any other field would silently see nothing."""
    assert "text_body" in (ROOT / "scripts/ingest.py").read_text(encoding="utf-8")
    got = IN.collect_bookings([_row(NEW_FORMAT, "", text_body=BODY_WITH_CUSTOMER)])
    assert "261070" in got


def test_hilmar_signal_still_distinguishes_the_town_from_the_customer():
    """The origin-city case is why this is a classifier and not a substring
    test; widening to the body must not blur it."""
    assert IN.hilmar_signal("Hilmar, CA to La Guaira") == "origin_city"
    assert IN.hilmar_signal("// HILMAR") == "tag"
    assert IN.hilmar_signal("Oakland to Manila") is None
