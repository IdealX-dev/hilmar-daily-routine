"""Michael, 2026-08-12: "ol responded to everything."

The weekly table said W31 13 requests / 0 quoted and W32 12 / 1, while 455
mbd_rate_response records sat in stage. Both cannot be true, so the defect is
in MATCHING. This diagnostic's contract is the one every prior diagnostic in
this session broke and paid for: OBSERVE the real matcher, never re-implement
it, and never write.

The trace hook is the mechanism — apply_rate_responses reports its own
decisions — so what the diagnostic prints is by construction what production
did, not a second opinion that happens to agree today.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
for p in (str(SCRIPTS), str(ROOT / "src")):
    if p not in sys.path:
        sys.path.insert(0, p)

import ingest as IN  # noqa: E402

SRC = (SCRIPTS / "diag_matching.py").read_text(encoding="utf-8")

ASK = "2026-07-29T15:00:00Z"
REPLY = "2026-07-29T19:00:00Z"


def _ask(dest="Yokohama", ts=ASK, rid="req_1", **kw):
    r = {"request_id": rid, "request_timestamp": ts, "destination": dest,
         "origin": "Oakland", "status": "PENDING", "status_history": []}
    r.update(kw)
    return r


def _reply(dest="Yokohama", sent=REPLY, **kw):
    rr = {"bucket": "mbd_rate_response", "destination": dest, "sent": sent,
          "subject": f"RE: Oakland to {dest}", "body_parsed": {}}
    rr.update(kw)
    return rr


# ── the trace hook must not change what production does ────────────────────

def test_tracing_is_optional_and_production_passes_nothing():
    """A diagnostic hook that alters the matcher is a defect generator. The
    fire calls apply_rate_responses(requests, rate_rsps) — two args."""
    ing = (SCRIPTS / "ingest.py").read_text(encoding="utf-8")
    assert "quoted = apply_rate_responses(requests, rate_rsps)" in ing, (
        "main() now passes a trace — production must run untraced")


def test_the_same_inputs_match_identically_with_and_without_a_trace():
    """Behavioural, not textual: the observation must be free."""
    reqs_a, reqs_b = [_ask()], [_ask()]
    rsps = [_reply()]
    n_a = IN.apply_rate_responses(reqs_a, list(rsps))
    seen = []
    n_b = IN.apply_rate_responses(
        reqs_b, list(rsps),
        trace=lambda rr, outcome, d: seen.append(outcome))
    assert n_a == n_b == 1
    assert reqs_a[0]["quoted"] == reqs_b[0]["quoted"] is True
    assert reqs_a[0]["response_timestamp"] == reqs_b[0]["response_timestamp"]
    assert "matched" in seen, "a successful match emitted no trace event"


# ── every way a reply can die must be observable ───────────────────────────

def test_a_reply_with_no_destination_is_reported_not_swallowed():
    """`continue` on a falsy destination is the quietest death in the matcher
    — 455 replies could vanish here and the log would say nothing."""
    seen = {}
    IN.apply_rate_responses(
        [_ask()], [_reply(destination=None, subject="Rates as requested")],
        trace=lambda rr, o, d: seen.setdefault(o, d))
    assert "no_destination" in seen, (
        "a reply whose subject yields no lane died silently — the single "
        "most likely mass-failure mode is invisible")
    assert seen["no_destination"].get("subject") == "Rates as requested", (
        "the event carries no subject, so the fix would be written against "
        "an imagined mail shape")


def test_a_reply_that_matches_nothing_reports_why_each_candidate_lost():
    """'No candidate' is not a diagnosis. already_quoted / ask_after_reply /
    outside_14d are different bugs with different fixes."""
    seen = {}
    # The only ask on this lane was already quoted by an earlier reply.
    reqs = [_ask(quoted=True)]
    IN.apply_rate_responses(
        reqs, [_reply(sent="2026-07-30T19:00:00Z")],
        trace=lambda rr, o, d: seen.setdefault(o, d))
    assert "no_candidate_matched" in seen
    assert seen["no_candidate_matched"]["reasons"].get("already_quoted") == 1


def test_an_ask_that_postdates_the_reply_is_told_apart_from_a_stale_one():
    seen = {}
    IN.apply_rate_responses(
        [_ask(ts="2026-08-05T15:00:00Z")], [_reply(sent="2026-07-29T19:00:00Z")],
        trace=lambda rr, o, d: seen.setdefault(o, d))
    assert seen["no_candidate_matched"]["reasons"].get("ask_after_reply") == 1
    seen.clear()
    IN.apply_rate_responses(
        [_ask(ts="2026-06-01T15:00:00Z")], [_reply(sent="2026-07-29T19:00:00Z")],
        trace=lambda rr, o, d: seen.setdefault(o, d))
    assert seen["no_candidate_matched"]["reasons"].get("outside_14d") == 1


# ── the diagnostic replays production, it does not model it ────────────────

def test_it_traces_the_real_matcher_rather_than_re_deriving_matches():
    assert "IN.apply_rate_responses(" in SRC and "trace=" in SRC, (
        "the diagnostic no longer observes the real matcher — whatever it "
        "reports is a second opinion about a pipeline nobody runs")


def test_it_attaches_bodies_through_ingests_own_join():
    """attach_bodies is the ONLY place conversation_id reaches a stage row,
    and conversation_id is the matcher's strongest signal. A diagnostic that
    skips it under-reports matches and blames the wrong component."""
    assert "IN.attach_bodies(rows)" in SRC
    assert "def attach_bodies" in (SCRIPTS / "ingest.py").read_text(encoding="utf-8")


def test_it_applies_the_same_client_gate_as_main():
    """main() drops out-of-scope rows BEFORE the bucket split — that is why
    the fire log says 378 rate responses over a 455-record stage file. A
    diagnostic on the unfiltered superset cannot reconcile with the fire."""
    assert "IN.out_of_scope_reason(r)" in SRC, (
        "the client gate is not replayed — counts will not reconcile with "
        "the fire log and every conclusion inherits the gap")


def test_it_proves_or_refutes_recoverability_from_stage():
    """The decisive question: does an unquoted ask have an OL reply naming its
    lane inside the window? If yes, OL is not silent and the matcher is at
    fault — that is the finding Michael's statement demands be tested."""
    assert "same-lane" in SRC and "recoverable" in SRC
    assert "C.same_port(" in SRC, (
        "lane comparison bypasses core.same_port — a fourth opinion about "
        "which ports are the same lane")


def test_it_is_read_only():
    """AST, not grep — a mention in the docstring is not a call."""
    called = set()
    for node in ast.walk(ast.parse(SRC)):
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Attribute):
                called.add(f.attr)
            elif isinstance(f, ast.Name):
                called.add(f.id)
    for forbidden in ("push", "save_data", "send", "send_email", "write_text",
                      "save_data_validated", "backup"):
        assert forbidden not in called, f"diag_matching calls {forbidden}()"


def test_the_workflow_is_manual_and_installs_the_storage_sdk():
    wf = (ROOT / ".github/workflows/diag-matching.yml").read_text(encoding="utf-8")
    assert "workflow_dispatch:" in wf
    assert "schedule:" not in wf, "a diagnostic must not fire on a schedule"
    assert "contents: read" in wf
    assert "azure-storage-blob" in wf
    assert "scripts/diag_matching.py" in wf
