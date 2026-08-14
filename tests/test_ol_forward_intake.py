"""OL forwards a Hilmar quote with Lonny stripped off the header line.

THE DEFECT, 2026-08-13. Michael: "also ol quoted these... i sent you the two
fucking emails five times". The daily report for 2026-08-12 listed two NEW
REQUESTS FROM LONNY —

    Oakland -> HCMC (Cat Lai), 2x20', Lactose
    Oakland -> Algeciras, 1x40'HC, Protein

— and, for the same two lanes, an OL-USA RESPONSES section reading (0). OL had
in fact quoted both. Both quote emails are committed here:

    tests/fixtures/ol_quote_algeciras.eml
    tests/fixtures/ol_quote_hcmc_cat_lai.eml

They are FORWARDS. Each is `From: Linda.Echevarria@ol-usa.com
To: Michael.Deitchman@ol-usa.com` with NO Cc, and the forwarded original
inside is the shared booking mailbox quoting Lonny. classify()'s OL branch
requires LONNY_EMAIL in From/To/Cc, that test is false on a forward, and both
messages were dropped AT INTAKE — before ingest, before the report, before
anything could notice.

TWO GATES HAD TO OPEN, not one. Admitting them at classify() is necessary but
not sufficient: BP.RATE_RESPONSE_SUBJECT_RX is anchored on a literal "re:", so
"FW: Oakland to Algeciras" fails it, and ingest.counts_as_rate_response
re-derives that same regex over mbd_inbound rows. A fix that only opened
intake would have put both messages in mbd_inbound and left OL-USA RESPONSES
at (0) — the same bug one layer down, now with a fix in front of it. Both
gates are tested here, in that order.

WHAT THE FIX MAY NOT DO is admit other customers. NUMIDIA, Agri Dairy,
Hoogwegt, Erno Laszlo and Brisar ship out of the same Hilmar plant into the
same mailbox on the same lanes, and Lonny's subjects name no customer at all —
"Oakland to Algeciras" is textually identical whoever it is for. So the gate
is THREAD IDENTITY, not subject text, and the negative tests below are the
half of this file that matters most.
"""
from __future__ import annotations

import email
import sys
from email import policy
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import body_parser as BP  # noqa: E402
import ingest  # noqa: E402
import refresh_stage as RS  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures"
ALGECIRAS = FIXTURES / "ol_quote_algeciras.eml"
HCMC = FIXTURES / "ol_quote_hcmc_cat_lai.eml"


# ─────────────────────────────────────────────────────────────────────
# Fixture → Graph item
# ─────────────────────────────────────────────────────────────────────

def _addr_list(msg, header: str) -> list[dict]:
    raw = msg.get(header)
    if not raw:
        return []
    out = []
    for a in str(raw).split(","):
        a = a.strip()
        if "<" in a and ">" in a:
            a = a[a.index("<") + 1:a.index(">")]
        if "@" in a:
            out.append({"emailAddress": {"address": a}})
    return out


def graph_item(path: Path, *, conversation_id: str = "CONV-DEFAULT") -> dict:
    """Build the item Graph hands classify() — GRAPH_SELECT FIELDS ONLY.

    This is the load-bearing constraint of the whole fix and the reason this
    helper exists rather than a dict literal. classify() is called on raw
    list-call items (main(), the pass-1 loop). Bodies are fetched LATER and
    ONLY for items already admitted, so no body test can be the intake gate.
    `body` is therefore deliberately absent here: a rule that needs it will
    fail this test, which is exactly the feedback we want.
    """
    msg = email.message_from_bytes(path.read_bytes(), policy=policy.default)
    plain = ""
    for part in msg.walk():
        if part.get_content_type() == "text/plain":
            plain = part.get_content()
            break
    headers = []
    for name in ("In-Reply-To", "References", "Thread-Topic", "Message-ID"):
        if msg.get(name):
            headers.append({"name": name, "value": str(msg.get(name))})
    return {
        "id": f"AAMk-{path.stem}",
        "conversationId": conversation_id,
        "subject": str(msg.get("Subject") or ""),
        "from": {"emailAddress": {"address": str(msg.get("From")).split("<")[-1].strip("<> ")}},
        "toRecipients": _addr_list(msg, "To"),
        "ccRecipients": _addr_list(msg, "Cc"),
        # Graph truncates bodyPreview to ~255 chars. Real value, real cut.
        "bodyPreview": plain[:255],
        "receivedDateTime": "2026-08-12T20:57:00Z",
        "sentDateTime": "2026-08-12T20:57:00Z",
        "internetMessageId": str(msg.get("Message-ID") or "").strip(),
        "internetMessageHeaders": headers,
        "_src": "michael.deitchman@ol-usa.com",
    }


def _first_reference(path: Path) -> str:
    """The ROOT of the thread — Lonny's own message.

    [Inference, not measurement] Every References chain here mixes
    *.namprd22.prod.outlook.com ids with OL-internal *.prod.exchangelabs.com
    ids, and namprd22 is the Hilmar tenant. The FIRST reference is the thread
    root, and the deepest quoted block in both bodies is
    `From: Lonny Upfold ... Subject: <bare lane>` — so the root is Lonny's
    request. The test only needs A staged Lonny imid that the forward's chain
    contains; if that inference were wrong the References path would simply
    not fire and the conversationId path (tested separately) still carries
    the fix.
    """
    msg = email.message_from_bytes(path.read_bytes(), policy=policy.default)
    return (msg.get("References") or "").split()[0]


ALL_FIXTURES = pytest.mark.parametrize("path", [ALGECIRAS, HCMC],
                                       ids=["algeciras", "hcmc_cat_lai"])


# ─────────────────────────────────────────────────────────────────────
# The measurements the fix is built on — pinned so they cannot rot
# ─────────────────────────────────────────────────────────────────────

@ALL_FIXTURES
def test_lonny_is_not_on_the_header_line(path):
    """The whole reason the old rule failed. If a future fixture DOES carry
    Lonny in From/To/Cc, the pre-existing OL branch handles it and this file
    is testing something that no longer exists."""
    it = graph_item(path)
    assert RS.LONNY_EMAIL.lower() not in RS._addresses(it)


@ALL_FIXTURES
def test_lonny_is_not_reachable_from_the_body_preview(path):
    """Measured: the first occurrence of Lonny's address in text/plain is at
    byte 8678 (algeciras) / 4303 (hcmc) — behind Linda's signature and OL's
    standing carrier advisory. Graph's bodyPreview is ~255 chars. A
    "lupfold is in the preview" rule cannot work and must not be proposed
    again."""
    it = graph_item(path)
    assert "lupfold@hilmaringredients.com" not in (it["bodyPreview"] or "").lower()


@ALL_FIXTURES
def test_the_shared_rate_response_regex_rejects_a_forward(path):
    """Gate 2's failure mode, pinned. RATE_RESPONSE_SUBJECT_RX starts at a
    literal "re:". If this ever starts passing, the local LANE_SUBJECT_RX is
    redundant and someone widened the shared regex — which silently
    reclassifies every historical mbd_inbound row in BOTH trees."""
    it = graph_item(path)
    assert not BP.RATE_RESPONSE_SUBJECT_RX.match(it["subject"])


# ─────────────────────────────────────────────────────────────────────
# BEFORE / AFTER
# ─────────────────────────────────────────────────────────────────────

@ALL_FIXTURES
def test_before_no_anchors_the_forward_is_still_dropped(path):
    """classify() with no anchor set is the OLD behaviour, unchanged. This is
    the "before" half of the fix and it must stay true: the new branch is
    additive and opt-in, so nothing about the pure sender rules moved."""
    assert RS.classify(graph_item(path)) is None


@ALL_FIXTURES
def test_after_conversation_id_admits_the_forward_as_a_rate_response(path):
    """The load-bearing path. conversationId is a first-class Graph property,
    it is in GRAPH_SELECT, and build_stage_record has persisted it on every
    staged row since 2026-06-25 — so the anchor for Lonny's Aug-12 request
    exists on the real stage file.

    conversationId is SYNTHESIZED here: a .eml carries no Graph
    conversationId at all (Exchange derives it from Thread-Index), so no
    fixture can supply one. What the fixture supplies is everything else —
    sender, subject, absent Lonny, real headers."""
    it = graph_item(path, conversation_id="CONV-LONNY-THREAD")
    threads = RS.LonnyThreads(conv_ids={"CONV-LONNY-THREAD"})
    assert RS.classify(it, threads) == "mbd_rate_response"


@ALL_FIXTURES
def test_after_the_real_references_chain_admits_the_forward(path):
    """The OR path, driven entirely by the fixture's own References header —
    no synthesis. Stage one Lonny-sent message under the thread root's id and
    the forward links to it. Kept as an OR and never an AND: this repo has
    never measured whether Graph returns internetMessageHeaders on a
    COLLECTION $select as opposed to a single-message GET, so the fix must
    not depend on it."""
    it = graph_item(path, conversation_id="CONV-SOMETHING-ELSE")
    threads = RS.LonnyThreads(imids={_first_reference(path)})
    assert RS.classify(it, threads) == "mbd_rate_response"


@ALL_FIXTURES
def test_gate_two_the_staged_row_counts_as_an_ol_response(path):
    """The layer that would have swallowed an intake-only fix.

    ingest.counts_as_rate_response re-derives RATE_RESPONSE_SUBJECT_RX over
    mbd_inbound rows, and this subject fails it (pinned above). Bucketing the
    forward as mbd_rate_response short-circuits that re-derivation, so
    OL-USA RESPONSES actually moves off (0). Asserted through the real
    build_stage_record, not a hand-written row, so a change to the record
    shape breaks this test rather than the report."""
    it = graph_item(path, conversation_id="CONV-LONNY-THREAD")
    threads = RS.LonnyThreads(conv_ids={"CONV-LONNY-THREAD"})
    rec = RS.build_stage_record(it, RS.classify(it, threads))
    assert rec["bucket"] == "mbd_rate_response"
    assert ingest.counts_as_rate_response(rec) is True
    assert rec["conversation_id"] == "CONV-LONNY-THREAD"


@ALL_FIXTURES
def test_an_intake_only_fix_would_not_have_been_enough(path):
    """Proves the claim above rather than asserting it: the SAME message
    bucketed mbd_inbound — what a naive intake fix produces — still does not
    count as an OL response."""
    it = graph_item(path)
    rec = RS.build_stage_record(it, "mbd_inbound")
    assert ingest.counts_as_rate_response(rec) is False


# ─────────────────────────────────────────────────────────────────────
# NEGATIVES — the half that keeps other customers out
# ─────────────────────────────────────────────────────────────────────

def test_a_numidia_forward_is_still_refused():
    """THE REQUIRED NEGATIVE. NUMIDIA ships out of the same Hilmar plant, on
    the same lane, and their mail lands in the same mailbox. This forward is
    identical to the Hilmar one in EVERY field the classifier can see except
    the thread: same OL sender, same recipient, same lane-shaped subject,
    same shape of References. It is a different Exchange conversation, and
    that is the only thing standing between Hilmar's report and another
    client's freight."""
    it = graph_item(ALGECIRAS, conversation_id="CONV-NUMIDIA-THREAD")
    it["subject"] = "FW: Oakland to Algeciras"
    it["internetMessageHeaders"] = [
        {"name": "References",
         "value": "<numidia-root@PH0PR22MB9999.namprd22.prod.outlook.com> "
                  "<numidia-2@DS0PR01MB111111.prod.exchangelabs.com>"},
    ]
    threads = RS.LonnyThreads(conv_ids={"CONV-LONNY-THREAD"},
                              imids={_first_reference(ALGECIRAS)})
    assert RS.classify(it, threads) is None


def test_a_numidia_named_subject_is_refused_even_inside_a_lonny_thread():
    """Belt and braces on the other axis. If someone replies into a Lonny
    thread about NUMIDIA freight, the subject no longer matches the bare lane
    shape and the message stays out of stage. (ingest.out_of_scope_reason is
    the backstop below this, not a substitute for it.)"""
    it = graph_item(ALGECIRAS, conversation_id="CONV-LONNY-THREAD")
    it["subject"] = "FW: NUMIDIA - Oakland to Algeciras"
    threads = RS.LonnyThreads(conv_ids={"CONV-LONNY-THREAD"})
    assert RS.classify(it, threads) is None


def test_a_lonny_thread_without_a_lane_subject_is_refused():
    """No general mbd_inbound path on this rule. OL chatter inside a Hilmar
    thread — "FW: out of office", an internal note — is not a quote and does
    not enter stage. Forwarded booking confirmations come in through the
    hilmar-bookings query and the MBD_BOOKING_EMAIL branch instead."""
    threads = RS.LonnyThreads(conv_ids={"CONV-LONNY-THREAD"})
    for subject in ("FW: out of office", "FW: carrier advisory — please read",
                    "RE: pricing meeting Thursday", ""):
        it = graph_item(ALGECIRAS, conversation_id="CONV-LONNY-THREAD")
        it["subject"] = subject
        assert RS.classify(it, threads) is None, subject


def test_our_own_outbound_is_refused_even_inside_a_lonny_thread():
    """SELF_SENDER is the mailbox this tracker reads AND sends from. The
    pre-existing OL branch can afford to admit it because that branch also
    requires Lonny to be a named participant; this one has no such check, so
    ingesting our own mail as an OL quote would be a closed loop."""
    it = graph_item(ALGECIRAS, conversation_id="CONV-LONNY-THREAD")
    it["from"] = {"emailAddress": {"address": RS.SELF_SENDER}}
    threads = RS.LonnyThreads(conv_ids={"CONV-LONNY-THREAD"})
    assert RS.classify(it, threads) is None


def test_a_non_ol_sender_in_a_lonny_thread_is_refused():
    """The thread anchor is necessary, not sufficient. Lonny's threads carry
    Hilmar staff, carriers and truckers; only OL quotes OL's rates."""
    it = graph_item(ALGECIRAS, conversation_id="CONV-LONNY-THREAD")
    it["from"] = {"emailAddress": {"address": "someone@hilmaringredients.com"}}
    threads = RS.LonnyThreads(conv_ids={"CONV-LONNY-THREAD"})
    assert RS.classify(it, threads) is None


def test_empty_anchor_set_admits_nothing():
    """First run, or a stage file with no Lonny rows yet. An empty anchor set
    must fail closed, never open."""
    it = graph_item(ALGECIRAS, conversation_id="CONV-LONNY-THREAD")
    assert RS.classify(it, RS.LonnyThreads()) is None


# ─────────────────────────────────────────────────────────────────────
# LANE_SUBJECT_RX
# ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("subject,expected", [
    ("FW: Oakland to Algeciras", True),
    ("FW: Oakland to HCMC (Cat Lai)", True),
    ("Re: Oakland to Algeciras", True),          # superset of the shared regex
    ("Fwd: RE: Dalhart, TX to Caucedo", True),   # prefix CHAIN, non-Oakland origin
    # Reno's real subject — "Rates" is not an origin. Pinned by
    # test_refresh_stage_drops too; she is admitted by sender, never subject.
    ("Re: Rates to a few destinations for a study", False),
    ("FW: Hilmar, CA to La Guaira", False),      # Hilmar is a TOWN, not an origin
    ("FW: NUMIDIA - Oakland to Algeciras", False),
    ("Oakland to Algeciras", False),             # Lonny's own request shape
    ("FTL Modesto to Sturgis MI", False),        # no prefix; domestic trucking
    ("OL-USA — Daily Shipment Update for Hilmar Ingredients (activity for Aug 6, 2026)", False),
])
def test_lane_subject_rx(subject, expected):
    assert bool(RS.LANE_SUBJECT_RX.match(subject)) is expected


def test_lane_subject_rx_is_local_to_refresh_stage():
    """It must NOT be the shared BP regex. Widening that one changes
    ingest.counts_as_rate_response's re-derivation over every historical
    mbd_inbound row in both trees — a silent reclassification of shipped
    history that no migration covers."""
    assert RS.LANE_SUBJECT_RX is not BP.RATE_RESPONSE_SUBJECT_RX
    assert RS.LANE_SUBJECT_RX.pattern != BP.RATE_RESPONSE_SUBJECT_RX.pattern


def test_lane_subject_rx_is_built_from_the_shared_origin_list():
    """One origin list, not N regexes. Dalhart was the 2026-06-11 miss; a new
    Hilmar site must extend BP.KNOWN_ORIGINS and reach this pattern for free."""
    for origin in BP.KNOWN_ORIGINS:
        assert RS.LANE_SUBJECT_RX.match(f"FW: {origin} to Somewhere"), origin


# ─────────────────────────────────────────────────────────────────────
# Anchors + ordering
# ─────────────────────────────────────────────────────────────────────

def test_only_lonny_sent_buckets_may_seed_an_anchor():
    """An anchor asserts "Hilmar's buyer is in this conversation". A message
    OL sent proves nothing of the kind — seeding from mbd_* would let one
    admitted forward vouch for the next one and walk the classifier into
    whatever thread it landed in."""
    assert set(RS.LONNY_BUCKETS) == {"lonny_outbound", "lonny_reply"}


def test_stage_anchors_are_loaded_only_from_lonny_rows(tmp_path, monkeypatch):
    """load_existing_stage_threads reads the stage FILE because a forward can
    arrive on a later fire than the request it answers — Lonny asks Monday,
    OL forwards Wednesday. In-run anchors alone would only ever link the two
    when they share a sweep."""
    stage = tmp_path / "stage_emails.txt"
    stage.write_text(
        '{"bucket":"lonny_outbound","conversation_id":"CONV-A","imid":"<lonny-1>"}\n'
        '{"bucket":"lonny_reply","conversation_id":"CONV-B","imid":"<lonny-2>"}\n'
        '{"bucket":"mbd_inbound","conversation_id":"CONV-OL","imid":"<ol-1>"}\n'
        '{"bucket":"mbd_rate_response","conversation_id":"CONV-OL2","imid":"<ol-2>"}\n'
        'not json at all\n',
        encoding="utf-8")
    monkeypatch.setattr(RS, "STAGE_PATH", stage)
    threads = RS.load_existing_stage_threads()
    assert threads.conv_ids == {"CONV-A", "CONV-B"}
    assert threads.imids == {"<lonny-1>", "<lonny-2>"}


def test_missing_stage_file_yields_an_empty_anchor_set(tmp_path, monkeypatch):
    monkeypatch.setattr(RS, "STAGE_PATH", tmp_path / "nope.txt")
    assert not RS.load_existing_stage_threads()


def test_anchors_do_not_depend_on_arrival_order():
    """THE ORDERING TRAP. The date sweep is newest-first, so Linda's 20:57
    forward is visited BEFORE Lonny's 13:05 request that it answers. classify
    must therefore run twice — once to fix the anchors on sender alone, once
    over the residual. A single pass would drop the forward again.

    This asserts the property the two-pass loop provides: a Lonny row seen
    AFTER the forward still vouches for it."""
    forward = graph_item(ALGECIRAS, conversation_id="CONV-LONNY-THREAD")
    lonny = {
        "id": "AAMk-lonny",
        "conversationId": "CONV-LONNY-THREAD",
        "subject": "Oakland to Algeciras",
        "from": {"emailAddress": {"address": RS.LONNY_EMAIL}},
        "toRecipients": [{"emailAddress": {"address": RS.MBD_BOOKING_EMAIL}}],
        "internetMessageId": "<lonny-orig>",
        "receivedDateTime": "2026-08-12T20:05:00Z",
    }
    newest_first = [forward, lonny]          # the order the sweep really gives

    threads = RS.LonnyThreads()
    decided = [[it, RS.classify(it)] for it in newest_first]
    for pair in decided:
        if pair[1] in RS.LONNY_BUCKETS:
            threads.add_item(pair[0])
    for pair in decided:
        if pair[1] is None:
            pair[1] = RS.classify(pair[0], threads)

    assert decided[1][1] == "lonny_outbound"
    assert decided[0][1] == "mbd_rate_response", (
        "the forward was visited before its own anchor and stayed dropped — "
        "the intake loop collapsed back to a single pass")


def test_pass_two_cannot_override_a_sender_rule():
    """Pass 2 re-decides only rows pass 1 left as None. If it could revisit a
    decided row, an anchor could relabel Lonny's own request as an OL rate
    response and the tracker would quote itself."""
    lonny = {
        "id": "x", "conversationId": "CONV-LONNY-THREAD",
        "subject": "FW: Oakland to Algeciras",
        "from": {"emailAddress": {"address": RS.LONNY_EMAIL}},
        "internetMessageId": "<lonny-fw>",
    }
    assert RS.classify(lonny) == "lonny_reply"
    threads = RS.LonnyThreads(conv_ids={"CONV-LONNY-THREAD"})
    assert RS.classify(lonny, threads) == "lonny_reply"


def test_links_tolerates_the_leading_whitespace_real_headers_carry():
    """Real header values arrive with folding whitespace — a literal tab or
    space before the id. _extract_thread_headers strips it; if that ever
    stops, every References match silently goes to zero and the fix reverts to
    conversationId only, with nothing failing.

    2026-08-14: the whitespace is INJECTED rather than read off the fixture.
    The old version asserted the .eml's own In-Reply-To still carried its
    leading tab, which is version-dependent — Python 3.12's email parsing
    strips folding whitespace that 3.11 preserves, so the self-check passed
    locally (3.11) and failed on CI (3.12) with identical code. The case
    under test was never "does the parser preserve tabs"; it is "does links()
    survive them when they arrive"."""
    it = graph_item(ALGECIRAS, conversation_id="CONV-OTHER")
    clean = next(h["value"] for h in it["internetMessageHeaders"]
                 if h["name"] == "In-Reply-To").strip()
    assert clean.startswith("<"), "fixture lost its In-Reply-To entirely"
    for h in it["internetMessageHeaders"]:
        if h["name"] == "In-Reply-To":
            h["value"] = "\t " + clean
    assert RS.LonnyThreads(imids={clean}).links(it) is True


# ─────────────────────────────────────────────────────────────────────
# End to end through the REAL main() loop
# ─────────────────────────────────────────────────────────────────────

def _run_main(monkeypatch, capsys, tmp_path, items, stage_lines=""):
    """Drive refresh_stage.main() --dry over a fake Graph, returning stdout.

    The tests above assert the RULE; this one asserts the WIRING. They are
    different failures: classify() can be perfectly correct while main() never
    passes it an anchor set, and that combination reproduces the original bug
    with a green unit suite in front of it.
    """
    stage = tmp_path / "stage_emails.txt"
    stage.write_text(stage_lines, encoding="utf-8")
    monkeypatch.setattr(RS, "STAGE_PATH", stage)
    monkeypatch.setattr(RS, "BODIES_PATH", tmp_path / "bodies.txt")
    monkeypatch.setattr(RS, "get_token", lambda: "tok")
    monkeypatch.setattr(RS, "read_targets",
                        lambda _t: [("michael.deitchman@ol-usa.com", "base", "tok")])
    monkeypatch.setattr(RS, "list_messages_since",
                        lambda *a, **k: list(items))
    monkeypatch.setattr(RS, "search_messages", lambda *a, **k: [])
    monkeypatch.setattr(sys, "argv",
                        ["refresh_stage.py", "--since", "2026-08-01", "--dry"])
    assert RS.main() == 0
    return capsys.readouterr().out


def test_main_admits_the_forward_when_lonnys_request_is_in_the_same_sweep(
        monkeypatch, capsys, tmp_path):
    """Both messages arrive on one fire, NEWEST FIRST — the order the date
    sweep really produces. This is the 2026-08-12 scenario end to end."""
    forward = graph_item(ALGECIRAS, conversation_id="CONV-LONNY-THREAD")
    lonny = {
        "id": "AAMk-lonny",
        "conversationId": "CONV-LONNY-THREAD",
        "subject": "Oakland to Algeciras",
        "from": {"emailAddress": {"address": RS.LONNY_EMAIL}},
        "toRecipients": [{"emailAddress": {"address": RS.MBD_BOOKING_EMAIL}}],
        "internetMessageId": "<lonny-orig>",
        "receivedDateTime": "2026-08-12T20:05:00Z",
        "bodyPreview": "Please quote Oakland to Algeciras, 1x40HC protein.",
    }
    out = _run_main(monkeypatch, capsys, tmp_path, [forward, lonny])
    assert "mbd_rate_response (1)" in out or "mbd_rate_response" in out
    assert "ADMITTED by Lonny-thread linkage: 1" in out
    assert "FW: Oakland to Algeciras" in out


def test_main_admits_the_forward_against_a_previously_staged_request(
        monkeypatch, capsys, tmp_path):
    """The commoner case: Lonny asked on an earlier fire and is already in the
    stage file, OL forwards days later. Anchors have to come off DISK, not
    just out of this run."""
    forward = graph_item(ALGECIRAS, conversation_id="CONV-FROM-DISK")
    out = _run_main(
        monkeypatch, capsys, tmp_path, [forward],
        stage_lines='{"bucket":"lonny_outbound","conversation_id":"CONV-FROM-DISK",'
                    '"imid":"<lonny-orig>","id":"old-1"}\n')
    assert "ADMITTED by Lonny-thread linkage: 1" in out


def test_main_still_drops_a_numidia_forward(monkeypatch, capsys, tmp_path):
    """The negative, end to end. Same sender, same lane subject, different
    conversation — and Lonny's real thread IS staged, so the anchor set is
    non-empty and the refusal is the rule working, not an empty set."""
    numidia = graph_item(ALGECIRAS, conversation_id="CONV-NUMIDIA")
    numidia["id"] = "AAMk-numidia"
    numidia["internetMessageId"] = "<numidia-fw>"
    numidia["internetMessageHeaders"] = [
        {"name": "References", "value": "<numidia-root@x.namprd22.prod.outlook.com>"}]
    out = _run_main(
        monkeypatch, capsys, tmp_path, [numidia],
        stage_lines='{"bucket":"lonny_outbound","conversation_id":"CONV-LONNY-THREAD",'
                    '"imid":"<lonny-orig>","id":"old-1"}\n')
    assert "ADMITTED by Lonny-thread linkage" not in out
    assert "NEW staged records: 0" in out
    assert "DROPPED as unclassified" in out


def test_main_reports_the_anchor_count_even_when_nothing_is_admitted(
        monkeypatch, capsys, tmp_path):
    """The fire's log has to be able to answer "did it have anchors at all?"
    without a re-run. An empty anchor set and a correct refusal look identical
    from the outside otherwise — that ambiguity is what let the original drop
    hide behind the word "unclassified" for a week."""
    out = _run_main(monkeypatch, capsys, tmp_path, [graph_item(ALGECIRAS)])
    assert "Lonny thread anchors:" in out
