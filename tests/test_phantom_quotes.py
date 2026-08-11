"""The machine that manufactured two weeks of losses out of Lonny's own email.

2026-08-11, Michael on the weekly table: "this is absurd.. your data is
consistently wrong." Measured on live state (diag-weekly runs 1-3):

  - mbd_rate_response staged per ET day: ZERO Aug 3 through Aug 11, across two
    fires that ran WITH the Aug-7 classify fix. Reno's quote replies match
    NEITHER Graph query — q1 needs Lonny ON the message, q2 needs the shared
    mailbox as sender — so classify never even saw them. Intake, not
    classification.
  - yet all 25 W31/W32 requests read quoted=1 LOSS with
    response_timestamp == request date, SAME DAY, to the row. Zero staged
    replies, twenty-five "quotes".

The fabrication chain, each step individually defensible as "recovery":
Lonny re-uses Outlook threads, so his new ask quotes the PREVIOUS rate sheet
below it. The heals read bodies by source_imids — which on a rebuilt request
row is the ask itself — and mined a carrier, then reconciled quoted=True, then
recovered last cycle's rate, then stamped the ask's own send time as the
response. Real OL replies used to overwrite all of it; when they stopped being
staged (~Jul 24), the fabricated quotes became the only quotes, and the weekly
table showed Lonny losing everything.

THE RULE, one home (core.quote_evidence_ok), both stamp sites: a message may
evidence an OL quote only if OL WROTE it (@ol-usa.com sender; missing sender
fails CLOSED — an undated quote QC-077 flags honestly beats a dated
fabrication) and it POSTDATES the ask (resp <= req is QC-066's impossible
ordering).

Plus the third defect from the same investigation: ingest's prior-WIN restore
stamped its →WIN transition at NOW on every fire, so eight April wins re-dated
to "this week" daily, forever (diag-weekly run 3: win_event=2026-08-11 on
request_date=2026-04-*, reason "Prior-build WIN restored").
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for p in (str(ROOT / "scripts"), str(ROOT / "src")):
    if p not in sys.path:
        sys.path.insert(0, p)

import core  # noqa: E402
import ingest as IN  # noqa: E402
import qc_selfheal as QC  # noqa: E402
import refresh_stage as RS  # noqa: E402

ASK_TS = "2026-08-03T14:00:00Z"
REPLY_TS = "2026-08-03T18:30:00Z"


# ── the predicate ───────────────────────────────────────────────────────────

def test_a_real_ol_reply_after_the_ask_is_evidence():
    assert core.quote_evidence_ok("Reno.Gurusinghe@ol-usa.com", REPLY_TS, ASK_TS)


def test_lonnys_own_email_is_never_quote_evidence():
    """The ask quotes the previous rate sheet below it. Mining it is how a
    same-day phantom quote is born."""
    assert not core.quote_evidence_ok("lonny.x@hilmar.com", ASK_TS, ASK_TS)


def test_a_message_at_or_before_the_ask_is_never_evidence():
    """resp <= req is QC-066's impossible ordering — a reply cannot predate
    the question it answers."""
    assert not core.quote_evidence_ok("Reno.Gurusinghe@ol-usa.com", ASK_TS, ASK_TS)
    assert not core.quote_evidence_ok("Reno.Gurusinghe@ol-usa.com",
                                      "2026-08-01T10:00:00Z", ASK_TS)


def test_a_missing_sender_fails_closed():
    """A stamp that cannot prove authorship is a guess. An undated quote that
    QC-077 flags honestly beats a dated fabrication."""
    assert not core.quote_evidence_ok(None, REPLY_TS, ASK_TS)
    assert not core.quote_evidence_ok("", REPLY_TS, ASK_TS)


def test_a_row_without_a_request_time_skips_the_ordering_half():
    """Standalones and legacy rows carry no request_timestamp; authorship
    alone must still admit a genuine OL message."""
    assert core.quote_evidence_ok("Reno.Gurusinghe@ol-usa.com", REPLY_TS, None)


# ── the stamp sites enforce it ──────────────────────────────────────────────

def _row(**kw):
    base = {"request_id": "r1", "request_timestamp": ASK_TS,
            "source_imids": ["ask@hilmar"], "status_history": []}
    base.update(kw)
    return base


def test_qc_stamp_refuses_the_asks_own_send_time():
    """THE phantom, reproduced: the only body on the row is Lonny's ask."""
    r = _row()
    bodies = {"ask@hilmar": {"sender_email": "lonny.x@hilmar.com",
                             "sent_ts": ASK_TS, "text_body": "old rates below"}}
    assert QC._stamp_response_time_from_bodies(r, bodies) is False
    assert r.get("response_timestamp") is None, (
        "the ask's own send time was stamped as the OL response — this is the "
        "resp==req machine that filled W31/W32 with phantom Q&L")


def test_qc_stamp_accepts_a_genuine_ol_reply():
    """The half that must not regress: real recovery still works."""
    r = _row(source_imids=["ask@hilmar", "reply@ol"])
    bodies = {
        "ask@hilmar": {"sender_email": "lonny.x@hilmar.com", "sent_ts": ASK_TS},
        "reply@ol": {"sender_email": "Reno.Gurusinghe@ol-usa.com",
                     "sent_ts": REPLY_TS},
    }
    assert QC._stamp_response_time_from_bodies(r, bodies) is True
    assert r["response_timestamp"] == REPLY_TS


def test_qc_rate_mining_skips_the_ask_body():
    """_heal_missing_rate mined last cycle's rate out of the ask's quoted
    text. With the guard, the ask body is never parsed at all."""
    r = _row(status="LOSS", quoted=True, carrier_quoted="CMA CGM", ol_rate=None)
    bodies = {"ask@hilmar": {
        "sender_email": "lonny.x@hilmar.com", "sent_ts": ASK_TS,
        "text_body": "Oakland | Yokohama | CMA CGM | $3,150 | 15-Jul | 8-Aug"}}
    log = QC.Log.__new__(QC.Log)
    log.fixes, log.warnings, log.errors = [], [], []
    QC._heal_missing_rate(log, "r1", r, bodies)
    assert r.get("ol_rate") is None, (
        "a rate was mined out of Lonny's own email — last cycle's price is "
        "now this cycle's phantom quote")


def test_patch_carriers_stamp_is_guarded_by_the_same_rule():
    """Two stamp sites, one predicate. A guard on one side only moves the
    fabrication to the other."""
    import patch_carriers as PC
    src = (ROOT / "scripts/patch_carriers.py").read_text(encoding="utf-8")
    assert "quote_evidence_ok" in src, (
        "patch_carriers._stamp_response_time does not consult the shared "
        "predicate — the phantom machine still runs through PASS 2")
    PC._SENT_BY_IMID["ask@hilmar"] = ASK_TS
    PC._SENDER_BY_IMID["ask@hilmar"] = "lonny.x@hilmar.com"
    try:
        r = _row()
        assert PC._stamp_response_time(r, {"_src_imid": "ask@hilmar"}) is False
        assert r.get("response_timestamp") is None
        PC._SENT_BY_IMID["reply@ol"] = REPLY_TS
        PC._SENDER_BY_IMID["reply@ol"] = "linda.echevarria@ol-usa.com"
        r2 = _row()
        assert PC._stamp_response_time(r2, {"_src_imid": "reply@ol"}) is True
        assert r2["response_timestamp"] == REPLY_TS
    finally:
        for k in ("ask@hilmar", "reply@ol"):
            PC._SENT_BY_IMID.pop(k, None)
            PC._SENDER_BY_IMID.pop(k, None)


# ── the intake hole ─────────────────────────────────────────────────────────

def test_the_quote_only_senders_are_fetched_not_just_kept():
    """The Aug 7 classify fix was necessary but not sufficient: classify can
    only keep what a query fetched, and neither q1 nor q2 can reach a Reno
    reply that drops Lonny. Measured: mbd_rate_response ZERO Aug 3-11 through
    two fires running the classify fix."""
    queries = dict(RS.graph_queries())
    q3 = queries.get("ol-quote-senders")
    assert q3, "the ol-quote-senders query is gone — quote intake has no path again"
    for s in RS.OL_QUOTE_ONLY_SENDERS:
        assert f"from:{s}" in q3, (
            f"{s} is admitted by classify but fetched by no query — the two "
            "ends of the pipe have drifted apart again")


def test_the_original_two_queries_are_unchanged():
    """Adding intake must not disturb what already worked."""
    queries = dict(RS.graph_queries())
    assert queries["lonny-flow"] == f"from:{RS.LONNY_EMAIL} OR to:{RS.LONNY_EMAIL}"
    assert queries["hilmar-bookings"] == (
        f"from:{RS.MBD_BOOKING_EMAIL} AND subject:HILMAR")
    assert "subject:HILMAR" in queries["hilmar-bookings"], (
        "the NUMIDIA body-match guard (2026-05-05) has been dropped")


def test_classify_and_the_query_share_one_sender_list():
    """One list, two consumers — built FROM OL_QUOTE_ONLY_SENDERS so adding a
    sender extends fetch and keep together, or neither."""
    src = (ROOT / "scripts/refresh_stage.py").read_text(encoding="utf-8")
    i = src.find("def graph_queries")
    assert "OL_QUOTE_ONLY_SENDERS" in src[i:i + 2200], (
        "q3 hardcodes addresses instead of deriving from the classify set")


# ── the rolling win dates ───────────────────────────────────────────────────

def test_a_restored_win_keeps_its_original_date():
    """Eight April wins carried win_event=THIS WEEK because the restore
    stamped at=now on every fire. The original →WIN transition must survive
    the rebuild verbatim."""
    prior = {"request_id": "req_x", "status": "WIN", "mdolx_ref": "260388",
             "booking_timestamp": "2026-04-17T21:20:31Z",
             "status_history": [{"at": "2026-04-17T21:20:31Z",
                                 "from": "PENDING", "to": "WIN",
                                 "reason": "MDOLX260388 booking confirmed"}]}
    rebuilt = {"request_id": "req_x", "status": "PENDING",
               "request_timestamp": "2026-04-07T15:00:00Z", "status_history": []}
    IN._merge_prior_win_into(rebuilt, prior, "2026-08-11T13:00:00+00:00")
    assert rebuilt["status"] == "WIN"
    assert core.win_event_date(rebuilt) == "2026-04-17", (
        f"the restored win re-dated to {core.win_event_date(rebuilt)} — every "
        "win-event surface will bucket an April booking into the current week")


def test_a_poisoned_prior_history_self_heals_to_booking_evidence():
    """Rows already rolled by the old code carry ONLY 'Prior-build WIN
    restored' entries dated at fire times. Those are excluded as a date
    source, so the restore falls back to the booking time — the row heals
    instead of carrying the poison forward."""
    prior = {"request_id": "req_y", "status": "WIN", "mdolx_ref": "260407",
             "booking_timestamp": "2026-04-17T21:55:29Z",
             "status_history": [{"at": "2026-08-11T13:37:28+00:00",
                                 "from": "PENDING", "to": "WIN",
                                 "reason": "Prior-build WIN restored (MDOLX260407) — x"}]}
    rebuilt = {"request_id": "req_y", "status": "PENDING",
               "request_timestamp": "2026-04-09T15:00:00Z", "status_history": []}
    IN._merge_prior_win_into(rebuilt, prior, "2026-08-11T13:00:00+00:00")
    assert rebuilt["status"] == "WIN"
    assert core.win_event_date(rebuilt) == "2026-04-17", (
        "the poisoned fire-time date was carried forward instead of falling "
        "back to the booking evidence")


def test_a_prior_with_no_evidence_at_all_still_restores():
    """Never lose the win itself — dating it imperfectly beats dropping it."""
    prior = {"request_id": "req_z", "status": "WIN", "mdolx_ref": None,
             "status_history": []}
    rebuilt = {"request_id": "req_z", "status": "PENDING",
               "request_timestamp": "2026-04-02T15:00:00Z", "status_history": []}
    IN._merge_prior_win_into(rebuilt, prior, "2026-08-11T13:00:00+00:00")
    assert rebuilt["status"] == "WIN"
    assert core.win_event_date(rebuilt) == "2026-04-02", (
        "with no booking or response evidence the restore should date from "
        "the ask, not from the fire clock")
