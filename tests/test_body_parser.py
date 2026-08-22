"""Tests for hilmar.body_parser — subject + body field extraction.

These cover the parser entry points used by ingest:
parse_subject_lane, parse_eta_requested / etd_offered / eta_offered /
origin_cutoff, parse_vessel, parse_transshipment, parse_rate_table,
parse_send_signal, html_to_text.

The parsers are intentionally permissive — these tests just lock in
the happy paths that ingest depends on.
"""
from __future__ import annotations

from hilmar import body_parser as BP
from hilmar import core as C

# ── parse_subject_lane ─────────────────────────────────────────────────


def test_subject_lane_oakland_to_shanghai():
    origin, dest = BP.parse_subject_lane("Oakland to Shanghai")
    assert origin and origin.lower() == "oakland"
    assert dest and dest.lower() == "shanghai"


# ── parse_container_spec_from_subject ──────────────────────────────────


def test_parse_container_spec_from_subject_uppercase_x():
    s = "MDOLX260420_UPDATED ETA BOOKING CONFIRMATION// HILMAR 1X20'DV Oakland to Bangkok// ONE: RICGE7217600"
    spec = BP.parse_container_spec_from_subject(s)
    assert spec is not None
    cc, teu = C.parse_teu(spec)
    assert cc == 1 and teu == 1


def test_parse_container_spec_from_subject_lowercase_reefer():
    s = "Re: MDOLX260460_BOOKING CONFIRMATION// HILMAR 4X40'RF Oakland to Tokyo// CMA: NAM8400958"
    spec = BP.parse_container_spec_from_subject(s)
    assert spec is not None
    cc, teu = C.parse_teu(spec)
    assert cc == 4 and teu == 8


def test_parse_container_spec_from_subject_no_match():
    """Free-time-issue / ops follow-ups don't carry a fresh container
    spec in the subject — return None rather than guessing."""
    s = "RE: MDOLX260062_ *FREE-TIME ISSUE - MSC BKG # EBKG14800694 // HILMAR"
    assert BP.parse_container_spec_from_subject(s) is None


def test_parse_container_spec_from_subject_empty():
    assert BP.parse_container_spec_from_subject("") is None
    assert BP.parse_container_spec_from_subject(None) is None


def test_subject_lane_handles_re_prefix():
    origin, dest = BP.parse_subject_lane("RE: Oakland to Singapore (12)")
    assert dest and dest.lower() == "singapore"


def test_subject_lane_returns_none_for_garbage():
    origin, dest = BP.parse_subject_lane("totally unrelated nonsense")
    assert origin is None or dest is None


def test_subject_lane_handles_empty():
    origin, dest = BP.parse_subject_lane("")
    assert origin is None and dest is None


def test_subject_lane_hilmar_is_customer_not_origin():
    """Booking confirmation subjects say 'HILMAR Oakland to Tokyo' where
    HILMAR is the customer reference and Oakland is the actual port-of-
    loading. Pre-fix _scan_for_origin had 'Hilmar' / 'Hilmar, CA' in
    _KNOWN_ORIGINS, picked HILMAR as origin (first hit by index), and
    produced lane labels like 'Hilmar → Tokyo' that split the lane
    bucket from 'Oakland → Tokyo' in the carrier scoreboard. Origin
    must resolve to 'Oakland' on these subjects."""
    cases = [
        "MDOLX260433_UPDATED PORT AND DOC CUT BOOKING CONFIRMATION// HILMAR 3X40'RF Oakland to Tokyo// CMA: NAM8433582",
        "Re: MDOLX260460_BOOKING CONFIRMATION// HILMAR 4X40'RF Oakland to Tokyo// CMA: NA",
        "MDOLX260420_UPDATED ETA BOOKING CONFIRMATION// HILMAR 1X20'DV Oakland to Bangkok",
    ]
    for s in cases:
        origin, dest = BP.parse_subject_lane(s)
        assert origin and origin.lower() == "oakland", (
            f"expected origin=Oakland for {s!r}, got {origin!r}"
        )
        assert dest and dest.lower() in ("tokyo", "bangkok"), (
            f"unexpected dest {dest!r} for {s!r}"
        )


# ── html_to_text ───────────────────────────────────────────────────────


def test_html_to_text_basic():
    out = BP.html_to_text("<p>Hello <b>world</b></p>")
    assert "Hello" in out
    assert "world" in out
    assert "<" not in out


def test_html_to_text_handles_none():
    assert BP.html_to_text(None) == ""


def test_html_to_text_collapses_whitespace():
    out = BP.html_to_text("<div>line1</div>\n\n\n<div>line2</div>")
    assert "line1" in out and "line2" in out


# ── parse_eta_requested / etd_offered / eta_offered / origin_cutoff ────


def test_a_departure_ask_is_not_a_requested_arrival():
    """"Need to ship by" states a DEPARTURE, so it belongs to etd_requested.

    This test used to assert the opposite — that parse_eta_requested picks up
    "need to ship/sail/load/depart" — and it was pinning the defect. Michael,
    2026-08-21: "compare like with like only". eta_requested is differenced
    against OL's offered ARRIVAL to decide ETD_MISS, so filing a sail-by date
    there made the computed "miss" the ocean transit time, and every
    cutoff-style RFQ that lost was stamped "missed the requested ETD".
    """
    text = "Need to ship by 5/15/2026"
    assert BP.parse_eta_requested(text) is None, (
        "a departure ask must not populate the requested ARRIVAL")
    assert BP.parse_etd_requested(text) == "2026-05-15"


def test_a_real_arrival_ask_still_parses():
    """The other half — removing the departure anchors must not blind the
    arrival side, which is the leg Lonny states most often."""
    assert BP.parse_eta_requested("Oakland to Shanghai\nETA 9/15/2026") == "2026-09-15"
    assert BP.parse_eta_requested("Deliver by 6/1/2026") == "2026-06-01"


def test_parse_etd_offered_finds_etd():
    text = "ETD 4/15/2026 from Oakland on MSC OSCAR"
    got = BP.parse_etd_offered(text)
    assert got, f"expected ETD parsed, got {got!r}"


def test_parse_origin_cutoff_with_anchor():
    text = "Origin cutoff: 4/12/2026"
    got = BP.parse_origin_cutoff(text)
    assert got, f"expected origin_cutoff parsed, got {got!r}"


def test_parse_eta_offered_returns_none_when_absent():
    text = "totally unrelated text without dates"
    assert BP.parse_eta_offered(text) is None


# ── parse_vessel ───────────────────────────────────────────────────────


def test_parse_vessel_returns_none_for_carrier_only_line():
    # A bare "Vessel: MSC" without a specific ship / voyage falls back to
    # None — that's by design (carrier name alone isn't a vessel).
    assert BP.parse_vessel("Vessel: MSC") is None


def test_parse_vessel_returns_none_when_absent():
    assert BP.parse_vessel("rate quote attached, no vessel info") is None


def test_parse_vessel_handles_empty_input():
    assert BP.parse_vessel("") is None
    assert BP.parse_vessel(None) is None  # type: ignore[arg-type]


# ── parse_transshipment ────────────────────────────────────────────────


def test_parse_transshipment_singapore():
    out = BP.parse_transshipment("Transshipment: Singapore")
    assert out and "singapore" in out.lower()


def test_parse_transshipment_direct():
    out = BP.parse_transshipment("Transshipment: Direct")
    assert out and "direct" in out.lower()


def test_parse_transshipment_none_when_absent():
    assert BP.parse_transshipment("just a rate body") is None


# ── parse_rate_table ───────────────────────────────────────────────────


def test_parse_rate_table_returns_dict_with_carrier_and_rate():
    text = (
        "Carrier: MSC\n"
        "OL rate: $2400\n"
        "ETD: 4/15/2026\n"
        "ETA: 5/15/2026\n"
        "Vessel: MSC OSCAR / 012E\n"
        "Transshipment: Direct\n"
    )
    rt = BP.parse_rate_table(text)
    assert rt
    # Carriers are extracted from `_find_carrier`; the parser may or may not
    # populate every field, but at minimum we expect SOMETHING parseable.
    keys = set(rt.keys())
    expected_some_overlap = keys & {
        "carrier_quoted", "ol_rate", "etd", "eta", "vessel_voyage", "transshipment",
    }
    assert expected_some_overlap, f"unexpected rate_table shape: {rt!r}"


def test_parse_rate_table_empty_text_returns_falsy():
    rt = BP.parse_rate_table("")
    assert not rt


# ── parse_send_signal ──────────────────────────────────────────────────


def test_parse_send_signal_send_please():
    assert BP.parse_send_signal("Send please") is True


def test_parse_send_signal_book():
    assert BP.parse_send_signal("Please book this") is True


def test_parse_send_signal_negative():
    assert BP.parse_send_signal("Hi, just checking on the rate") is False


def test_parse_send_signal_handles_empty():
    assert BP.parse_send_signal("") is False
    assert BP.parse_send_signal(None) is False  # type: ignore[arg-type]


# ── MBD column-layout table parser (PR #15 — closes the learning loop) ──


# Real production body taken from parser_misses.jsonl after PR #12 went
# live (LLM had to extract it because regex couldn't). The PR #15 column
# parser must extract these fields without LLM help.
_MBD_TABLE_BODY = """Hi Lonny,

Please see option below.

POL
POD
Container Size
Vessel
Voyage
ERD
Doc Cut
Port Cut
ETD
ETA
RATE
CARRIER
TRANSSHIPMENT
ORIGIN FREE TIME
DESTINATION FREE TIME

Oakland
HCMH
5x20'DV
WAN HAI A05
W105
30-Apr-26
4-May-26
5-May-26
8-May-26
10-Jun-26
$420
ONE
DIRECT VIA CAI MEP
14 DETENTION + 6 DEMURRAGE FREE DAYS
14 DETENTION + 14 DEMURRAGE FREE DAYS

*Please note that ERD, cut-off, ETS, and ETA are estimates and may change."""


def test_mbd_column_table_extracts_rate_carrier_dates():
    """Real production body that LLM had to extract on PR #12. After
    PR #15's column parser, regex extracts everything natively."""
    out = BP.parse_mbd_rate_columns(_MBD_TABLE_BODY)
    assert out is not None
    assert out.get("ol_rate") == 420.0
    assert out.get("carrier_quoted") == "ONE"
    assert out.get("etd") == "2026-05-08"
    assert out.get("eta") == "2026-06-10"
    assert "WAN HAI A05" in (out.get("vessel_voyage") or "")
    assert out.get("transshipment") == "DIRECT VIA CAI MEP"


def test_parse_rate_table_uses_column_parser_first():
    """parse_rate_table() integration: column parser fills high-confidence
    fields, prose parsers fill any remaining gaps. Carrier 'ONE' should
    come through normalize_carrier intact."""
    out = BP.parse_rate_table(_MBD_TABLE_BODY)
    assert out["ol_rate"] == 420.0
    assert out["carrier_quoted"] == "ONE"  # normalize_carrier preserves canonical
    assert out["etd"] == "2026-05-08"
    assert out["eta"] == "2026-06-10"


def test_parse_mbd_rate_columns_returns_none_on_random_email():
    """Defensive: an email that doesn't have the labels block must
    return None, not invent values from random text."""
    out = BP.parse_mbd_rate_columns(
        "Hi Lonny, can you ask MSC for a quote on Oakland to Tokyo? Thanks."
    )
    assert out is None


def test_parse_mbd_rate_columns_handles_optional_origin_free_time():
    """Some MBD emails skip ORIGIN FREE TIME (only DESTINATION FREE TIME).
    The parser must align values positionally even with one fewer label."""
    body = """POL
POD
Container Size
Vessel
Voyage
ERD
Doc Cut
Port Cut
ETD
ETA
RATE
CARRIER
TRANSSHIPMENT
DESTINATION FREE TIME

Oakland
HCMC
2 X 20'DV
WAN HAI A01
W017
21-Apr-26
24-Apr-26
27-Apr-26
30-Apr-26
3-Jun-26
$450.00
ONE LINE
DIRECT VIA CAI MEP
14 DETENTION + 14 DEMURRAGE FREE DAYS"""
    out = BP.parse_mbd_rate_columns(body)
    assert out is not None
    assert out["ol_rate"] == 450.0
    assert out["carrier_quoted"] == "ONE LINE"
    assert out["etd"] == "2026-04-30"
    assert out["eta"] == "2026-06-03"


def test_parse_mbd_table_date_handles_dd_mon_yy_format():
    """MBD uses DD-Mon-YY format ('21-Apr-26'). Parser must produce
    ISO date strings."""
    assert BP._parse_table_date("21-Apr-26") == "2026-04-21"
    assert BP._parse_table_date("8-May-26") == "2026-05-08"
    assert BP._parse_table_date("10-Jun-2026") == "2026-06-10"
    # ISO passes through unchanged
    assert BP._parse_table_date("2026-04-21") == "2026-04-21"
    # Garbage returns None
    assert BP._parse_table_date("not-a-date") is None
    assert BP._parse_table_date("") is None


# ── parse_signer (PR #18 — individual at MBD shared mailbox) ──


def test_parse_signer_simple_best_regards():
    text = """Hi Lonny,

Please see option below.

[rate table content]

Best regards,
Caren Tobel
OL-USA Ocean Export
"""
    assert BP.parse_signer(text) == "Caren Tobel"


def test_parse_signer_thanks_then_name():
    text = """Hi Lonny,
Rate is $540.

Thanks,
John Smith
"""
    assert BP.parse_signer(text) == "John Smith"


def test_parse_signer_skips_company_and_title_lines():
    """Negative tokens (titles, company names, contact info) must not
    be picked up as the signer name."""
    text = """Best regards,

OL-USA
Ocean Export Booking Team
www.ol-usa.com
"""
    # "OL-USA" is a negative token; "Ocean Export Booking Team" is too;
    # parser should walk past them and find nothing → None.
    assert BP.parse_signer(text) is None


def test_parse_signer_returns_none_when_no_closing():
    """An email without any greeting closing block returns None."""
    assert BP.parse_signer("Just a body. No closing. No signature.") is None
    assert BP.parse_signer("") is None
    assert BP.parse_signer(None) is None  # type: ignore[arg-type]


def test_parse_signer_handles_hyphenated_name():
    text = """Thanks,
Mary-Anne O'Brien
"""
    assert BP.parse_signer(text) == "Mary-Anne O'Brien"


def test_parse_signer_with_middle_initial():
    text = """Best,
John Q. Public
"""
    out = BP.parse_signer(text)
    assert out and "John" in out and "Public" in out


# ─────────────────────────────────────────────────────────────────────
# Parser-gap fixes — 2026-05-19 (per Michael "no field should be empty ever")
# ─────────────────────────────────────────────────────────────────────

def test_parse_product_labeled():
    """Lonny's standard pattern: 'Product Lactose' / 'product: cheese'."""
    assert BP.parse_product("Product Lactose") == "Lactose"
    assert BP.parse_product("product: cheese") == "Cheese"
    assert BP.parse_product("Product is Skim Milk Powder") == "Skim Milk Powder"


def test_parse_product_dictionary_fallback():
    """Commodity word anywhere in body without explicit 'Product' label."""
    assert BP.parse_product("Need rate for whey shipment") == "Whey"
    assert BP.parse_product("WPC 80 to Yokohama") == "WPC 80"


def test_parse_product_returns_none_when_no_commodity():
    assert BP.parse_product("Hi Lonny, please send rates") is None
    assert BP.parse_product("") is None
    assert BP.parse_product(None) is None


def test_parse_temperature_numeric_celsius():
    assert BP.parse_temperature("Reefer at -2C, sailing next week") == "-2C"
    assert BP.parse_temperature("Set at +2C") == "2C"


def test_parse_temperature_numeric_fahrenheit():
    assert BP.parse_temperature("Cargo at 34F") == "34F"


def test_parse_temperature_rejects_false_positives():
    """234 FCL must not be read as 34F; '10-14 days free' must not match."""
    assert BP.parse_temperature("234 FCL containers") is None
    assert BP.parse_temperature("10-14 days free demurrage") is None
    # 'days free' tail-rejection
    assert BP.parse_temperature("5 days free combined") is None


def test_parse_temperature_keywords():
    assert BP.parse_temperature("Frozen cargo, no genset") == "Frozen"
    assert BP.parse_temperature("Chilled product") == "Chilled"


def test_parse_etd_requested_explicit_date():
    """Lonny writes 'cutoff week of 4/27' → extract 2026-04-27."""
    out = BP.parse_etd_requested("Cutoff week of 4/27, please advise")
    assert out == "2026-04-27", f"got {out!r}"


def test_parse_etd_requested_returns_none_when_relative():
    """'next week' has no concrete date — must return None, not guess."""
    assert BP.parse_etd_requested("Cutoff next week or the following") is None


def test_parse_requested_dates_captures_lonny_phrase():
    """Returns the raw phrase (anchor + tail), not an ISO date."""
    out = BP.parse_requested_dates("Cutoff week of 4/27, send both")
    assert out is not None
    assert "Cutoff" in out and "4/27" in out


def test_parse_requested_dates_returns_none_on_no_anchor():
    assert BP.parse_requested_dates("Just checking on the rate") is None
    assert BP.parse_requested_dates("") is None


def test_parse_lonny_notes_returns_body_minus_signature():
    body = """1-20' Oakland to Manila. Cutoff next week. Product Lactose.
Thanks,
Lonny Upfold
Logistics Coordinator
Hilmar Ingredients"""
    out = BP.parse_lonny_notes(body)
    assert out is not None
    assert "Manila" in out
    assert "Lonny Upfold" not in out  # signature stripped
    assert "Logistics Coordinator" not in out


def test_parse_lonny_notes_strips_outlook_quote_chain():
    body = """Need rate for Oakland to HCMC.

From: MBD Ocean Export Booking
Sent: Monday
Subject: Old rate"""
    out = BP.parse_lonny_notes(body)
    assert out and "HCMC" in out
    assert "From:" not in out


def test_parse_lonny_notes_returns_none_on_empty():
    assert BP.parse_lonny_notes("") is None
    assert BP.parse_lonny_notes(None) is None


def test_parse_rate_expiry_valid_through():
    assert BP.parse_rate_expiry("This rate is valid through 5/31") == "5/31"
    assert BP.parse_rate_expiry("valid until June 15") == "June 15"


def test_parse_rate_expiry_expires_pattern():
    assert BP.parse_rate_expiry("Rate expires 6/15/26") == "6/15/26"


def test_parse_rate_expiry_returns_none_when_absent():
    assert BP.parse_rate_expiry("Hi Lonny, here is the rate.") is None


def test_parse_rate_table_surfaces_erd_origin_free_dest_free():
    """The MBD column parser now exposes erd, origin_free_time, and
    dest_free_time — previously buried in the cells dict but never returned."""
    body = """POL
POD
Container Size
Vessel
Voyage
ERD
Doc Cut
Port Cut
ETD
ETA
RATE
CARRIER
TRANSSHIPMENT
ORIGIN FREE TIME
DESTINATION FREE TIME

Oakland
HCMC
5x40'DV
WAN HAI A05
W105
30-Apr-26
4-May-26
5-May-26
8-May-26
10-Jun-26
$420
ONE
DIRECT VIA CAI MEP
14 DETENTION + 6 DEMURRAGE FREE DAYS
14 DETENTION + 14 DEMURRAGE FREE DAYS"""
    out = BP.parse_rate_table(body)
    assert out["erd"] == "2026-04-30", f"erd not surfaced: {out!r}"
    assert "DETENTION + 6" in (out.get("origin_free_time") or "")
    assert "DETENTION + 14" in (out.get("dest_free_time") or "")
