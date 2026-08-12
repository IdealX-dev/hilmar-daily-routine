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


# ── the mining sites enforce it too (2026-08-12, the Aug-12 staff email) ────
#
# aa39f16 guarded the TIMESTAMP stamps but not the CARRIER/RATE mining, so the
# very first fire on the fix printed "PATCH PND req_73be1541f11b -> Yang Ming
# @ $797" (and CMA CGM $725, ONE $505) — three fresh Aug-11 asks "quoted" out
# of Lonny's own thread text, undated, straight into the email the CEO read.
# QC-077 counted 49 such rows. The rate a function returns must clear the same
# bar as the time it would be stamped with.

LONNY_RATE_SHEET = (
    "POL | POD | Carrier | Rate | ETD | ETA\n"
    "Oakland | Manila (North) | Yang Ming | $797 | 15-Jul | 8-Aug\n"
)


def _with_body_maps(monkeypatch, sender_by_imid, sent_by_imid):
    import patch_carriers as PC
    monkeypatch.setattr(PC, "_SENDER_BY_IMID", dict(sender_by_imid))
    monkeypatch.setattr(PC, "_SENT_BY_IMID", dict(sent_by_imid))
    return PC


def test_pass1_mining_refuses_the_ask_body(monkeypatch):
    """THE Aug-12 fabrication, reproduced: the only body on the rebuilt row is
    Lonny's ask, carrying the previous rate sheet quoted below it."""
    PC = _with_body_maps(monkeypatch,
                         {"ask@hilmar": "lonny.x@hilmar.com"},
                         {"ask@hilmar": ASK_TS})
    parsed = PC._discover_full_quote_from_bodies(
        ["ask@hilmar"], {"ask@hilmar": LONNY_RATE_SHEET}, ASK_TS)
    assert parsed == {}, (
        f"mined {parsed.get('carrier_quoted')} @ {parsed.get('ol_rate')} out "
        "of Lonny's own email — the PATCH PND machine still runs")


def test_pass1_mining_accepts_a_genuine_ol_reply(monkeypatch):
    """The half that must not regress: a real OL reply still yields the quote."""
    PC = _with_body_maps(monkeypatch,
                         {"reply@ol": "Reno.Gurusinghe@ol-usa.com"},
                         {"reply@ol": REPLY_TS})
    parsed = PC._discover_full_quote_from_bodies(
        ["reply@ol"], {"reply@ol": LONNY_RATE_SHEET}, ASK_TS)
    assert parsed.get("carrier_quoted") == "Yang Ming"
    assert parsed.get("_src_imid") == "reply@ol"


def test_pass1_mining_skips_the_ask_and_keeps_looking(monkeypatch):
    """A row with both bodies must yield the OL reply, not first-hit-wins on
    the ask."""
    PC = _with_body_maps(monkeypatch,
                         {"ask@hilmar": "lonny.x@hilmar.com",
                          "reply@ol": "linda.echevarria@ol-usa.com"},
                         {"ask@hilmar": ASK_TS, "reply@ol": REPLY_TS})
    parsed = PC._discover_full_quote_from_bodies(
        ["ask@hilmar", "reply@ol"],
        {"ask@hilmar": LONNY_RATE_SHEET, "reply@ol": LONNY_RATE_SHEET},
        ASK_TS)
    assert parsed.get("_src_imid") == "reply@ol"


def test_sibling_lookup_refuses_last_cycles_rate_sheet():
    """Lonny re-uses threads, so the conv-id join finds LAST cycle's rate
    response — sent before this ask existed. Same phantom, side door."""
    import patch_carriers as PC
    stale = {"body": LONNY_RATE_SHEET, "sender": "Reno.Gurusinghe@ol-usa.com",
             "sent": "2026-07-15T10:00:00Z"}
    row = {"conversation_id": "conv1", "request_timestamp": ASK_TS}
    assert PC._find_related_rate_response(row, {("conv", "conv1"): stale}) is None, (
        "a rate response sent 19 days BEFORE the ask was accepted as this "
        "cycle's quote")


def test_sibling_lookup_accepts_a_post_ask_ol_response():
    import patch_carriers as PC
    fresh = {"body": LONNY_RATE_SHEET, "sender": "Reno.Gurusinghe@ol-usa.com",
             "sent": REPLY_TS}
    row = {"conversation_id": "conv1", "request_timestamp": ASK_TS}
    assert PC._find_related_rate_response(
        row, {("conv", "conv1"): fresh}) == LONNY_RATE_SHEET


def test_the_ungated_mining_wrapper_stays_deleted():
    """_discover_carrier_from_bodies had zero callers and duplicated the
    mining loop WITHOUT the gate — dead code holding the fabrication path
    open for the next caller to find."""
    src = (ROOT / "scripts/patch_carriers.py").read_text(encoding="utf-8")
    assert "def _discover_carrier_from_bodies" not in src


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


def test_a_win_to_win_correction_touch_is_not_a_win_event():
    """The last survivor of the rolling-win defect (diag on run 31611357523):
    the operator-corrections applier re-runs every fire (the rebuild wipes
    its fields), and until 2026-08-12 appended a fire-time WIN→WIN entry each
    time. A correction touching a row that already IS a win is not the win
    happening — stand_260905's booking is Jul 9, not "today", every day."""
    r = {"status": "WIN", "status_history": [
        {"at": "2026-07-09T21:42:21Z", "from": "PENDING", "to": "WIN",
         "reason": "MDOLX260905 standalone booking confirmation"},
        {"at": "2026-08-12T15:17:27+00:00", "from": "WIN", "to": "WIN",
         "reason": "Operator correction: MDOLX260905 (OOCL booking …)"},
    ]}
    assert core.win_event_date(r) == "2026-07-09", (
        f"win_event={core.win_event_date(r)} — the WIN→WIN correction touch "
        "re-dated the booking to the fire day")


def test_the_corrections_applier_writes_no_entry_without_a_transition(monkeypatch, tmp_path):
    """Kill it at the source too: re-applying a correction whose status the
    row already holds must not grow status_history — that append is what
    manufactured the daily WIN→WIN entries."""
    corr = {"corrections": [{"request_id": "stand_x",
                             "set": {"status": "WIN", "carrier_won": "OOCL"},
                             "note": "confirm booking"}]}
    p = tmp_path / "operator_corrections.json"
    p.write_text(__import__("json").dumps(corr), encoding="utf-8")
    monkeypatch.setattr(IN, "CORRECTIONS_PATH", p, raising=False)
    row = {"request_id": "stand_x", "status": "WIN", "carrier_won": None,
           "status_history": [{"at": "2026-07-09T21:42:21Z",
                               "from": "PENDING", "to": "WIN",
                               "reason": "booking confirmed"}]}
    IN.apply_operator_corrections([row])
    assert row["carrier_won"] == "OOCL", "the correction itself must still apply"
    assert len(row["status_history"]) == 1, (
        "a WIN→WIN history entry was appended for a correction that changed "
        "no status — the daily re-dating machine, reborn")


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
