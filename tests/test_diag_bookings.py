"""A confirmed booking is a win. This finds the gate that disagreed.

2026-08-10, Michael: "if ol confirmed bookings with mdolx numbers it's a win.
what are you talking about."

That is the rule, and it is simpler than the model I was reasoning from. I had
been treating a booking as a win only once it MATCHED an RFQ and theorising
about why matching failed. ingest already agrees with Michael: an unmatched
booking becomes a standalone `stand_<mdolx>` WIN. So an MDOLX confirmation
yields a win either way — UNLESS one of three gates drops it first:

    out_of_scope_reason()      numidia / agridairy / trucking / recalled
    is_operational_subject()   FREE-TIME ISSUE, LOADING APPT, DRAFT RATED, …
    no MDOLX parsed            extract_mdolx() over subject and preview ONLY

Each gate was added to kill a real false positive, and each is a plausible
place for a real booking to die. The diagnostic does not pick a favourite —
it runs the real gates in the real order and reports which one fired.

What is pinned here is that it keeps using the PIPELINE's gates. A diagnostic
with its own copy of the rules would clear a booking the pipeline still eats,
which is the failure mode this repo has hit with classify(), with the imid
field name, and with the ET clock.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

SRC = (ROOT / "scripts" / "diag_bookings.py").read_text(encoding="utf-8")


def test_it_uses_the_pipelines_own_gates():
    """Not a reimplementation. If ingest tightens a gate, the diagnostic must
    tighten with it or it will clear a booking production still drops."""
    assert "IN.out_of_scope_reason(" in SRC
    assert "IN.is_operational_subject(" in SRC
    assert "IN.extract_mdolx(" in SRC
    assert "IN._OPERATIONAL_SUBJECT_HINTS" in SRC, (
        "the diagnostic names the operational gate but not WHICH hint fired")


def test_the_gates_run_in_ingests_order():
    """out_of_scope BEFORE operational BEFORE mdolx — the same order ingest
    applies. Report the first gate that fires, or the verdict names a gate
    that would not actually have been reached."""
    oos = SRC.index("IN.out_of_scope_reason(")
    ops = SRC.index("IN.is_operational_subject(subject)")
    mdx = SRC.index("IN.extract_mdolx(subject)")
    assert oos < ops < mdx, "the diagnostic checks the gates out of order"


def test_the_gate_chain_matches_ingest_on_real_subjects():
    """Behaviour, through the real predicates. These five shapes are the ones
    the gates exist for, plus the one that matters most: a genuine booking
    confirmation whose MDOLX is not in the subject."""
    import ingest as IN

    def verdict(subject: str) -> str:
        row = {"subject": subject, "summary_preview": ""}
        if IN.out_of_scope_reason(row):
            return f"out_of_scope:{IN.out_of_scope_reason(row)}"
        if IN.is_operational_subject(subject):
            return "operational"
        return "admitted" if IN.extract_mdolx(subject) else "no-mdolx"

    assert verdict("MDOLX260980 NEW BOOKING CONFIRMATION HILMAR 2X40'RF "
                   "Oakland to Yokohama") == "admitted"
    assert verdict("Re: MDOLX260469_DRAFT RATED FOR HILMAR") == "operational"
    assert verdict("MDOLX260062 FREE-TIME ISSUE") == "operational"
    assert verdict("MDOLX260558 // NUMIDIA // HILMAR -> ACAJUTLA") == "out_of_scope:numidia"
    # THE ONE THAT COSTS A WIN SILENTLY: a real confirmation, no MDOLX in the
    # subject. ingest reads subject + a 300-char preview and nothing else, so
    # a reference living in the body is invisible and the booking evaporates.
    assert verdict("NEW BOOKING CONFIRMATION HILMAR Oakland to Busan") == "no-mdolx"


def test_a_body_only_reference_is_reported_distinctly():
    """"No MDOLX anywhere" and "the MDOLX was on disk and nobody looked" are
    different bugs with different fixes. Collapsing them into one verdict is
    how a fixable loss reads as an unfixable one."""
    assert "no-mdolx-in-subject BUT body has" in SRC, (
        "a body-only MDOLX is reported as a plain no-mdolx drop")
    assert "bodies_by_imid" in SRC


def test_it_checks_for_the_standalone_row_not_just_a_match():
    """The whole correction: an unmatched booking should still be a WIN via
    stand_<mdolx>. A diagnostic that only looked for a MATCHED row would
    report a healthy standalone as missing and send the next person chasing
    the matcher — which is exactly the wrong turn I took."""
    assert 'f"stand_{mdolx}"' in SRC
    assert "mdolx_refs_all" in SRC, (
        "only the primary mdolx_ref is checked; a row carrying the reference "
        "in mdolx_refs_all would read as missing")


def test_it_distinguishes_a_gate_drop_from_a_post_gate_loss():
    """Three outcomes, three different owners: a gate is too tight (intake),
    a booking passed every gate and produced no row (ingest), or nothing was
    staged at all (the mailbox). The verdict must not blur them."""
    tail = SRC.split("_rule(\"verdict\")", 1)[-1]
    assert "dropped by a gate" in tail
    assert "admitted but NO win row" in tail
    assert "not reaching stage" in tail


def test_the_verdict_names_the_bookings_with_no_row():
    """A count is not a finding. 2026-08-10 this printed "admitted but NO win
    row: 4" for a whole session while the four MDOLX numbers sat ~300 lines
    up, above a 200-line mailbox scan and off the end of every log fetch — so
    the defect stayed unidentified despite being measured three times. The
    verdict block is the part that gets read; it has to carry the evidence."""
    tail = SRC.split("_rule(\"verdict\")", 1)[-1]
    assert "the ones with NO row" in tail, (
        "the verdict reports a COUNT of rowless bookings without naming them — "
        "the number is unactionable and the names scroll out of reach")
    assert "for d, mdolx, r in missing" in tail, (
        "nothing iterates `missing` in the verdict block, so no MDOLX is printed")


def test_it_is_read_only():
    """It pulls production state. It must not push, send, or edit."""
    import ast
    called = set()
    for node in ast.walk(ast.parse(SRC)):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            val = node.func.value
            if isinstance(val, ast.Name):
                called.add(f"{val.id}.{node.func.attr}")
    for forbidden in ("state_store.push", "state_store.backup",
                      "state_store.restore"):
        assert forbidden not in called, f"diag_bookings calls {forbidden}"
    assert "state_store.pull" in called
    for node in ast.walk(ast.parse(SRC)):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            assert node.func.attr not in ("write_text", "write_bytes"), (
                "diag_bookings writes a file")


def test_the_mailbox_scan_reports_why_the_bookings_query_missed():
    """Michael 2026-08-10: "yes they wind up in my mail because i'm part of the
    mbd ocean export group emails." So the confirmations arrive. The gap is
    between arriving and being staged.

    The bookings query is `from:MBD_OceanExportBookingShared AND
    subject:HILMAR`. Note which booking survived it in the first run: a NUMIDIA
    move, whose subject carries HILMAR only because Hilmar is the ORIGIN. A
    genuine Hilmar-client booking naming the lane instead would never match —
    the filter selecting FOR the moves the numidia gate then discards.

    So the scan must report BOTH halves of the query per message, not just
    whether the row was staged. "NOT-staged" alone does not say why.
    """
    assert "RS.MBD_BOOKING_EMAIL" in SRC, (
        "the scan does not check the sender half of the bookings query")
    assert '"HILMAR" in subj.upper()' in SRC, (
        "the scan does not check the subject:HILMAR half of the query")
    assert "q2-MISS" in SRC and "q2-match" in SRC
    assert "RS.classify(it)" in SRC, (
        "the scan judges staging by its own rule instead of classify()")


def test_the_mailbox_scan_can_be_skipped_offline():
    """The gate analysis works from stage alone. Requiring Graph would make
    the whole tool unusable when only the blob is reachable."""
    assert 'os.environ.get("DIAG_SKIP_GRAPH")' in SRC


def test_the_workflow_is_manual_and_installs_the_storage_sdk():
    wf = (ROOT / ".github" / "workflows" / "diag-bookings.yml").read_text(encoding="utf-8")
    assert "workflow_dispatch:" in wf and "schedule:" not in wf
    assert "azure-storage-blob" in wf
    assert "permissions:\n  contents: read" in wf
