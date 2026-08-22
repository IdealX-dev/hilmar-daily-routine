"""Targeted tests for the carrier_won inheritance / fallback logic in
ingest.apply_send_signals and the container-spec recovery in
apply_rate_responses. These were 0%-covered code paths surfaced by
QC-052 / run_audit_tests.py 2026-05-28 as the largest gap in
src/hilmar/ingest.py (the WIN-classification module).

WHY THESE MATTER

apply_send_signals decides which Q&L row turns into a WIN when Lonny
replies "send it" on a quote thread. carrier_won is the WIN's carrier
identity — without it, the audit's red-flag #1 (WIN missing carrier_won)
fires and the daily email shows "?" for the booked carrier. Three
fallback layers exist:

  1. Direct: carrier_won = carrier_quoted from the same row's rate
     response (covered by existing integration tests).
  2. Same-lane sibling: borrow from the most recent quoted row on the
     EXACT same canonical lane within 30 days. Catches off-channel
     rates where the rate-response email isn't in our corpus but a
     recent prior quote is. Drops missing-carrier WINs 6→1 in
     production (per the audit fix comment 2026-04-30).
  3. Substring-prefix sibling: borrow from a sibling on a sub-lane
     that shares the same prefix ("hcmc (cai mep)" ↔ "hcmc (cat lai)";
     "manila (north)" ↔ "manila (south)"). Last-resort, also 30-day
     window.

Container-spec recovery (apply_rate_responses) handles a separate
parser-gap case: when Lonny's outbound subject has no spec and the
preview is too short to carry one, the row landed with containers=None
and dragged TEU totals. The MBD rate-response body almost always
restates the spec ("for your 1x40HC to Bangkok…") so we mine it as a
best-effort backfill.
"""
from __future__ import annotations

from datetime import timezone

import pytest

from hilmar import ingest

UTC = timezone.utc


# ─────────────────────────────────────────────────────────────────────
# apply_send_signals — fallback carrier inheritance
# ─────────────────────────────────────────────────────────────────────

def _request(
    *, request_id: str, destination: str, request_ts: str,
    carrier_quoted: str | None = None, response_ts: str | None = None,
    mdolx_ref: str | None = None, teu_requested: int = 2,
) -> dict:
    return {
        "request_id": request_id,
        "destination": destination,
        "lane": f"Oakland → {destination}",
        "origin": "Oakland",
        "request_timestamp": request_ts,
        "response_timestamp": response_ts,
        "carrier_quoted": carrier_quoted,
        "mdolx_ref": mdolx_ref,
        "teu_requested": teu_requested,
        "status": "Q&L",
        "quoted": bool(carrier_quoted),
    }


def _lonny_send_reply(*, subject: str, sent: str) -> dict:
    return {
        "subject": subject,
        "sent": sent,
        "body_parsed": {"send_signal": True},
    }


def test_apply_send_signals_promotes_and_inherits_carrier_directly():
    """Baseline: a Q&L row with carrier_quoted set + a Lonny send reply
    targeting the same lane → row gets has_send=True and carrier_won
    inherited from carrier_quoted."""
    req = _request(
        request_id="req_1", destination="Yokohama",
        request_ts="2026-05-20T16:00:00+00:00",
        carrier_quoted="CMA CGM",
        response_ts="2026-05-20T18:00:00+00:00",
    )
    reply = _lonny_send_reply(
        subject="RE: Oakland to Yokohama 2x40HC",
        sent="2026-05-21T14:00:00+00:00",
    )
    promotions = ingest.apply_send_signals([req], [reply])
    assert promotions == 1
    assert req["has_send"] is True
    assert req["quoted"] is True
    assert req["carrier_won"] == "CMA CGM"
    assert req["teu_won"] == req["teu_requested"]
    assert any(e["source"] == "lonny_reply" for e in req["send_signal_events"])


def test_apply_send_signals_same_lane_sibling_carrier_fallback():
    """Target row has NO carrier_quoted. A sibling on the EXACT same
    canonical lane, quoted within 30 days before the send, supplies
    carrier_won (and back-fills carrier_quoted)."""
    target = _request(
        request_id="req_target", destination="Yokohama",
        request_ts="2026-05-20T16:00:00+00:00",
        carrier_quoted=None,                # <-- missing
    )
    sibling = _request(
        request_id="req_sib", destination="Yokohama",
        request_ts="2026-05-05T16:00:00+00:00",
        carrier_quoted="ONE",
        response_ts="2026-05-05T18:00:00+00:00",
    )
    reply = _lonny_send_reply(
        subject="RE: Oakland to Yokohama 1x40HC",
        sent="2026-05-21T14:00:00+00:00",
    )
    promotions = ingest.apply_send_signals([target, sibling], [reply])
    assert promotions == 1
    assert target["carrier_won"] == "ONE"
    assert target["carrier_quoted"] == "ONE"   # back-fills the input field too


def test_apply_send_signals_sibling_fallback_skips_after_send_timestamp():
    """A sibling whose rate response is AFTER the Lonny send must not
    supply the carrier — it can't have been the rate Lonny was responding
    to."""
    target = _request(
        request_id="req_target", destination="Yokohama",
        request_ts="2026-05-20T16:00:00+00:00",
        carrier_quoted=None,
    )
    future_sibling = _request(
        request_id="req_future", destination="Yokohama",
        request_ts="2026-05-22T16:00:00+00:00",     # AFTER the send
        carrier_quoted="MSC",
        response_ts="2026-05-22T18:00:00+00:00",
    )
    reply = _lonny_send_reply(
        subject="RE: Oakland to Yokohama 1x40HC",
        sent="2026-05-21T14:00:00+00:00",
    )
    ingest.apply_send_signals([target, future_sibling], [reply])
    assert target.get("carrier_won") is None


def test_apply_send_signals_sibling_fallback_respects_30_day_window():
    """A sibling more than 30 days before the send cannot back-fill."""
    target = _request(
        request_id="req_target", destination="Yokohama",
        request_ts="2026-05-15T16:00:00+00:00",
        carrier_quoted=None,
    )
    ancient_sibling = _request(
        request_id="req_old", destination="Yokohama",
        request_ts="2026-03-01T16:00:00+00:00",       # > 30 days before send
        carrier_quoted="OOCL",
        response_ts="2026-03-01T18:00:00+00:00",
    )
    reply = _lonny_send_reply(
        subject="RE: Oakland to Yokohama 1x40HC",
        sent="2026-05-21T14:00:00+00:00",
    )
    ingest.apply_send_signals([target, ancient_sibling], [reply])
    assert target.get("carrier_won") is None


def test_apply_send_signals_substring_prefix_sibling_fallback():
    """Last-resort prefix fallback: target on 'HCMC (Cai Mep)' with no
    carrier and no same-lane sibling; sibling on 'HCMC (Cat Lai)'
    (same 'hcmc' prefix) within 30 days supplies the carrier."""
    target = _request(
        request_id="req_target", destination="HCMC (Cai Mep)",
        request_ts="2026-05-20T16:00:00+00:00",
        carrier_quoted=None,
    )
    prefix_sibling = _request(
        request_id="req_prefix", destination="HCMC (Cat Lai)",
        request_ts="2026-05-10T16:00:00+00:00",
        carrier_quoted="ONE",
        response_ts="2026-05-10T18:00:00+00:00",
    )
    reply = _lonny_send_reply(
        subject="RE: Oakland to HCMC (Cai Mep) 1x40HC",
        sent="2026-05-21T14:00:00+00:00",
    )
    promotions = ingest.apply_send_signals([target, prefix_sibling], [reply])
    assert promotions == 1
    assert target["carrier_won"] == "ONE"
    assert target["carrier_quoted"] == "ONE"


def test_apply_send_signals_prefix_fallback_outside_30_days_does_not_apply():
    target = _request(
        request_id="req_target", destination="Manila (North)",
        request_ts="2026-05-15T16:00:00+00:00",
        carrier_quoted=None,
    )
    far_sibling = _request(
        request_id="req_far", destination="Manila (South)",
        request_ts="2026-02-01T16:00:00+00:00",      # > 30 days before send
        carrier_quoted="HMM",
        response_ts="2026-02-01T18:00:00+00:00",
    )
    reply = _lonny_send_reply(
        subject="RE: Oakland to Manila (North) 1x40HC",
        sent="2026-05-21T14:00:00+00:00",
    )
    ingest.apply_send_signals([target, far_sibling], [reply])
    assert target.get("carrier_won") is None


def test_apply_send_signals_skips_rows_already_bound_to_mdolx():
    """Rows with an MDOLX ref are already booking-confirmed —
    finalize_status will WIN them; apply_send_signals must skip."""
    already_bound = _request(
        request_id="req_bound", destination="Tokyo",
        request_ts="2026-05-20T16:00:00+00:00",
        carrier_quoted="CMA CGM",
        mdolx_ref="MDOLX12345",                # <-- already linked
    )
    reply = _lonny_send_reply(
        subject="RE: Oakland to Tokyo 1x40HC",
        sent="2026-05-21T14:00:00+00:00",
    )
    promotions = ingest.apply_send_signals([already_bound], [reply])
    assert promotions == 0
    assert "has_send" not in already_bound  # untouched
    assert "carrier_won" not in already_bound


def test_apply_send_signals_no_match_when_request_after_send():
    """The Q&L row was created after Lonny's send — can't be the row
    she was replying to."""
    too_recent = _request(
        request_id="req_recent", destination="Osaka",
        request_ts="2026-05-22T16:00:00+00:00",    # AFTER the send
        carrier_quoted="CMA CGM",
    )
    reply = _lonny_send_reply(
        subject="RE: Oakland to Osaka 1x40HC",
        sent="2026-05-21T14:00:00+00:00",
    )
    promotions = ingest.apply_send_signals([too_recent], [reply])
    assert promotions == 0
    assert too_recent.get("has_send") is None


def test_apply_send_signals_no_match_when_request_older_than_5_days():
    """The 'best' candidate must be within 5 days of Lonny's send."""
    too_old = _request(
        request_id="req_old", destination="Busan",
        request_ts="2026-05-10T16:00:00+00:00",    # > 5 days before send
        carrier_quoted="CMA CGM",
    )
    reply = _lonny_send_reply(
        subject="RE: Oakland to Busan 1x40HC",
        sent="2026-05-21T14:00:00+00:00",
    )
    promotions = ingest.apply_send_signals([too_old], [reply])
    assert promotions == 0
    assert too_old.get("has_send") is None


def test_apply_send_signals_picks_most_recent_among_multiple_candidates():
    """When multiple rows on the same lane are eligible, the MOST RECENT
    one before the send wins."""
    older = _request(
        request_id="req_older", destination="Singapore",
        request_ts="2026-05-18T10:00:00+00:00",
        carrier_quoted="MSC",
    )
    newer = _request(
        request_id="req_newer", destination="Singapore",
        request_ts="2026-05-20T10:00:00+00:00",      # more recent
        carrier_quoted="CMA CGM",
    )
    reply = _lonny_send_reply(
        subject="RE: Oakland to Singapore 1x40HC",
        sent="2026-05-21T14:00:00+00:00",
    )
    promotions = ingest.apply_send_signals([older, newer], [reply])
    assert promotions == 1
    assert newer.get("has_send") is True
    assert older.get("has_send") is None


def test_apply_send_signals_ignores_replies_without_send_signal():
    """A Lonny reply without a parsed send_signal must not promote anything."""
    req = _request(
        request_id="req_1", destination="Yokohama",
        request_ts="2026-05-20T16:00:00+00:00",
        carrier_quoted="CMA CGM",
    )
    reply = {
        "subject": "RE: Oakland to Yokohama",
        "sent": "2026-05-21T14:00:00+00:00",
        "body_parsed": {"send_signal": False},   # <-- not a send
    }
    promotions = ingest.apply_send_signals([req], [reply])
    assert promotions == 0


def test_apply_send_signals_ignores_replies_without_parseable_destination():
    """A reply whose subject doesn't carry a lane can't be matched."""
    req = _request(
        request_id="req_1", destination="Yokohama",
        request_ts="2026-05-20T16:00:00+00:00",
        carrier_quoted="CMA CGM",
    )
    reply = {
        "subject": "RE: confirm please",   # no lane
        "sent": "2026-05-21T14:00:00+00:00",
        "body_parsed": {"send_signal": True},
    }
    promotions = ingest.apply_send_signals([req], [reply])
    assert promotions == 0


def test_apply_send_signals_ignores_replies_with_unparseable_sent_ts():
    req = _request(
        request_id="req_1", destination="Yokohama",
        request_ts="2026-05-20T16:00:00+00:00",
        carrier_quoted="CMA CGM",
    )
    reply = {
        "subject": "RE: Oakland to Yokohama 1x40HC",
        "sent": "not-an-iso-timestamp",     # parse failure
        "body_parsed": {"send_signal": True},
    }
    promotions = ingest.apply_send_signals([req], [reply])
    assert promotions == 0


# ─────────────────────────────────────────────────────────────────────
# apply_rate_responses — container-spec recovery from the response body
# ─────────────────────────────────────────────────────────────────────

def _rate_response(*, subject: str, sent: str, text_body: str,
                   carrier: str | None = None, ol_rate: int | None = None) -> dict:
    rate_table = {}
    if carrier:
        rate_table["carrier_quoted"] = carrier
    if ol_rate is not None:
        rate_table["ol_rate"] = ol_rate
    return {
        "subject": subject,
        "sent": sent,
        "text_body": text_body,
        "body_parsed": {"rate_table": rate_table},
    }


def test_apply_rate_responses_recovers_container_spec_from_body():
    """Lonny's outbound had no container spec (containers=None,
    teu_requested=0); the MBD rate-response body restates it
    ('for your 1x40HC to Bangkok'). apply_rate_responses mines it."""
    req = {
        "request_id": "req_spec",
        "destination": "Bangkok",
        "lane": "Oakland → Bangkok",
        "origin": "Oakland",
        "request_timestamp": "2026-05-20T16:00:00+00:00",
        "containers": None,            # <-- missing
        "container_count": None,
        "teu_requested": 0,
        "quoted": False,
    }
    rr = _rate_response(
        subject="RE: Oakland to Bangkok",
        sent="2026-05-20T18:00:00+00:00",
        text_body="Hi Lonny, here is the rate for your 1x40HC to Bangkok via CMA CGM at $3,500.",
        carrier="CMA CGM", ol_rate=3500,
    )
    quoted = ingest.apply_rate_responses([req], [rr])
    assert quoted == 1
    assert req["quoted"] is True
    assert req["containers"] == "1x40HC"
    assert req["container_count"] == 1
    assert req["teu_requested"] == 2          # 1x40' == 2 TEU
    assert req["carrier_quoted"] == "CMA CGM"
    assert req["ol_rate"] == 3500


def test_apply_rate_responses_does_not_overwrite_existing_container_spec():
    """Recovery only fills when containers is missing — a populated spec
    must be left alone even if the body restates a different one."""
    req = {
        "request_id": "req_keep",
        "destination": "Bangkok",
        "lane": "Oakland → Bangkok",
        "origin": "Oakland",
        "request_timestamp": "2026-05-20T16:00:00+00:00",
        "containers": "2x20DV",        # <-- already populated
        "container_count": 2,
        "teu_requested": 2,
        "quoted": False,
    }
    rr = _rate_response(
        subject="RE: Oakland to Bangkok",
        sent="2026-05-20T18:00:00+00:00",
        text_body="Rate for your 1x40HC to Bangkok via ONE.",
        carrier="ONE",
    )
    ingest.apply_rate_responses([req], [rr])
    assert req["containers"] == "2x20DV"       # untouched
    assert req["container_count"] == 2
    assert req["teu_requested"] == 2


def test_apply_rate_responses_no_match_leaves_rows_untouched():
    """A rate response on a lane with no matching open request promotes
    nothing."""
    req = {
        "request_id": "req_other",
        "destination": "Yokohama",
        "lane": "Oakland → Yokohama",
        "origin": "Oakland",
        "request_timestamp": "2026-05-20T16:00:00+00:00",
        "quoted": False,
    }
    rr = _rate_response(
        subject="RE: Oakland to Osaka",        # different lane
        sent="2026-05-20T18:00:00+00:00",
        text_body="Rate for your 1x40HC to Osaka.",
        carrier="MSC",
    )
    quoted = ingest.apply_rate_responses([req], [rr])
    assert quoted == 0
    assert req["quoted"] is False


# ─────────────────────────────────────────────────────────────────────
# guess_teu_from_preview — equipment-code normalization
# ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("preview,exp_count,exp_canonical", [
    ("2-40' HC to Yokohama", 2, "2-40' HC"),
    # 'HC' wins the ordered alternation over 'Reefer', so 'HC Reefer' → 'HC'.
    ("1-40' HC Reefer", 1, "1-40' HC"),
    # A bare 'Reefer' (no HC prefix) hits the REEF-normalization branch.
    ("1-40' Reefer", 1, "1-40' HC Reefer"),
    ("3-20' Oakland", 3, "3-20'"),
    ("2-40' Flex container", 2, "2-40' Flex"),
])
def test_guess_teu_from_preview_variants(preview, exp_count, exp_canonical):
    count, teu, canonical = ingest.guess_teu_from_preview(preview)
    assert count == exp_count
    assert canonical == exp_canonical
    assert teu > 0


def test_guess_teu_from_preview_empty_returns_zeros():
    assert ingest.guess_teu_from_preview(None) == (0, 0, None)
    assert ingest.guess_teu_from_preview("") == (0, 0, None)


def test_guess_teu_from_preview_no_spec_returns_no_canonical():
    count, teu, canonical = ingest.guess_teu_from_preview("just some prose, no spec")
    assert canonical is None


# ─────────────────────────────────────────────────────────────────────
# _etd_fit_days — ETA delta
# ─────────────────────────────────────────────────────────────────────

def test_etd_fit_days_computes_delta():
    """Now via core.requested_fit_days — ingest._etd_fit_days was retired
    2026-08-21. It parsed with fromisoformat against fields holding OL's raw
    cell text ("30-Sep-26"), so it returned None on every table-parsed quote
    while these ISO-only fixtures kept it looking healthy."""
    import core as C
    assert C.requested_fit_days(
        {"eta_requested": "2026-05-10", "eta_offered": "2026-05-13"}) == (3, "arrival")
    assert C.requested_fit_days(
        {"eta_requested": "2026-05-10", "eta_offered": "2026-05-08"}) == (-2, "arrival")


def test_etd_fit_days_handles_the_raw_cell_text_production_actually_stores():
    """The case the retired helper could never do — and the reason it was dead."""
    import core as C
    assert C.requested_fit_days(
        {"eta_requested": "2026-09-15", "eta_offered": "10-Oct-26"}) == (25, "arrival")


def test_etd_fit_days_none_on_missing_or_bad_input():
    import core as C
    assert C.requested_fit_days({"eta_offered": "2026-05-13"}) == (None, None)
    assert C.requested_fit_days({"eta_requested": "2026-05-10"}) == (None, None)
    assert C.requested_fit_days(
        {"eta_requested": "garbage", "eta_offered": "2026-05-13"}) == (None, None)


def test_etd_fit_days_refuses_to_cross_the_legs():
    """A cutoff ask against an arrival offer measures the ocean crossing."""
    import core as C
    assert C.requested_fit_days(
        {"etd_requested": "2026-08-28", "eta_offered": "30-Sep-26"}) == (None, None)

