"""The diagnostic that separates a broken parser from a broken ruler.

QC-027's ordering defect is fixed and guarded by
tests/test_qc027_measures_final_state.py. What that guard cannot say is what
the number becomes on the real 300-odd rows — that is a fact about the data.
diag_qc027 measures it, so the answer to "is it actually fixed" is a printout
and not a hope.

The one thing this diagnostic must never do is hold its own idea of QC-027's
denominator. A completeness percentage is a ratio; a tool that re-types the
comprehension is reporting on a set nobody is measuring, and would "explain"
a number that the check never produced. So it calls the check's own
predicates, and that binding is tested here by planting a rename.
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

import diag_qc027 as D  # noqa: E402
import qc_selfheal as QC  # noqa: E402

SRC = (SCRIPTS / "diag_qc027.py").read_text(encoding="utf-8")


def _row(rid="r1", **kw):
    base = {"request_id": rid, "status": "LOSS", "response_timestamp": "2026-07-01T12:00:00Z",
            "etd_offered": "2026-07-05", "eta_offered": "2026-07-25",
            "vessel_voyage": "EVER GIVEN 021E", "ol_rate": 2400.0,
            "carrier_quoted": "CMA CGM", "pol": "Oakland", "pod": "Busan",
            "request_date": "2026-07-01"}
    base.update(kw)
    return base


# ── it reports on the set the check actually grades ─────────────────────────

def test_it_uses_the_checks_own_denominator():
    """No private comprehension. If the diag selected its own rows it could
    report 100% on a set QC-027 never looks at."""
    assert "qc027_active_rows" in SRC
    assert "qc027_is_reachable" in SRC
    assert "QC027_FIELDS" in SRC


def test_measure_matches_the_check_on_the_denominator():
    rows = [
        _row("a"),                                   # reachable
        _row("b", etd_offered=None, vessel_voyage=None, ol_rate=None),  # PDF-only
        _row("c", response_timestamp=None),          # not active at all
        _row("d", status="ARCHIVED"),                # not active at all
    ]
    n_reach, n_pdf, stats = D.measure(rows, QC)
    assert n_reach == 1, f"expected 1 reachable row, got {n_reach}"
    assert n_pdf == 1, f"expected 1 PDF-only row, got {n_pdf}"
    assert set(stats) == {lbl for _f, lbl in QC.QC027_FIELDS}


def test_the_binding_to_the_check_is_real_not_incidental():
    """Non-vacuity, by planting the failure. Rename the predicate the check
    exports and `measure` must break — if it sails through, it is carrying a
    private copy of the rule."""
    import pytest
    original = QC.qc027_is_reachable
    try:
        del QC.qc027_is_reachable
        with pytest.raises(AttributeError):
            D.measure([_row("a")], QC)
    finally:
        QC.qc027_is_reachable = original
    # and it works again once restored — the test left nothing broken
    assert D.measure([_row("a")], QC)[0] == 1


def test_percentages_are_computed_over_reachable_rows_only():
    """The PDF-only rows are excluded from the denominator — that exclusion is
    the whole reason the check tolerates booking-confirmation WINs."""
    rows = [_row("a"), _row("b", carrier_quoted=None),
            _row("pdf", etd_offered=None, vessel_voyage=None, ol_rate=None,
                 carrier_quoted=None)]
    n_reach, n_pdf, stats = D.measure(rows, QC)
    assert (n_reach, n_pdf) == (2, 1)
    present, pct = stats["Carrier"]
    assert (present, round(pct)) == (1, 50), (
        "the PDF-only row leaked into the Carrier denominator")


def test_an_empty_dataset_does_not_divide_by_zero():
    n_reach, n_pdf, stats = D.measure([], QC)
    assert (n_reach, n_pdf) == (0, 0)
    assert all(v == (0, 0.0) for v in stats.values())


# ── the verdict banding matches the check's own thresholds ──────────────────

def test_verdict_bands_match_qc027():
    assert D._verdict(89.9).strip() == "ERROR"
    assert D._verdict(90.0).strip() == "WARN"
    assert D._verdict(94.9).strip() == "WARN"
    assert D._verdict(95.0).strip() == "ok"
    assert D._verdict(100.0).strip() == "ok"


# ── read-only ───────────────────────────────────────────────────────────────

def test_it_never_writes_state():
    """A diagnostic that pushes is not a diagnostic. Walks the AST rather than
    grepping, so a mention inside the module docstring cannot pass for a call
    and a real call cannot hide behind an alias."""
    tree = ast.parse(SRC)
    called = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Attribute):
                called.add(f.attr)
            elif isinstance(f, ast.Name):
                called.add(f.id)
    for forbidden in ("push", "save_data", "send", "send_email", "write_text"):
        assert forbidden not in called, (
            f"diag_qc027 calls {forbidden}() — it must only read")


def test_the_residual_analysis_is_not_hardwired_to_one_field():
    """A residual analysis hardwired to carrier_quoted can only ever answer the
    question that was already answered. With Carrier resolved at 97.0%, ETA is
    the only field left under 95% (307/329) and this section could not look at
    it."""
    assert "DIAG_FIELD" in SRC, "the analysed field is not selectable"
    tail = SRC.split("who is still missing", 1)[-1]
    assert 'r.get(field)' in tail, (
        "the miss list still reads a hardcoded field name rather than the "
        "selected one")


def test_an_unknown_field_is_refused_rather_than_silently_reporting_zero():
    """`not r.get("etaoffered")` is True for every row, so a typo would report
    100% of rows missing a field that does not exist — a confident, wrong
    answer. Refuse instead."""
    valid = {f for f, _lbl in QC.QC027_FIELDS}
    assert "eta_offered" in valid and "carrier_quoted" in valid
    assert "is not a QC-027 field" in SRC, (
        "an unrecognised DIAG_FIELD is not rejected; a typo would produce a "
        "full-looking report about nothing")


def test_the_workflow_exposes_the_field_selector():
    wf = (ROOT / ".github/workflows/diag-qc027.yml").read_text(encoding="utf-8")
    assert "DIAG_FIELD" in wf, "the workflow cannot select a field"
    assert "eta_offered" in wf, (
        "eta_offered is not offered as a choice — it is the field currently "
        "under 95% and the reason this selector exists")


def test_a_zero_body_result_is_diagnosed_not_just_reported():
    """Run 3 printed "0/8 row(s) had a cached body" against an index of 1263
    records. Two causes look identical from there and need opposite fixes:
    the bodies aged out, or the two key spaces never meet — and if it is the
    latter, every body-based heal in qc_selfheal is a no-op on every row.

    This session has already been burned three times by "one fact, two
    storages, and the reader holds the other one". A diagnostic that reports
    the symptom without the whole-dataset hit rate cannot tell them apart."""
    assert "bodies-index reachability" in SRC, (
        "a zero-body result is reported with no way to tell age-out from a "
        "key-shape mismatch")
    assert "sample bodies-index keys" in SRC, (
        "the two key spaces are never shown side by side, so a mismatch "
        "cannot be seen even when it is the cause")
    # and it must state a verdict in BOTH directions, not only the alarming one
    assert "That is not age-out" in SRC
    assert "genuinely absent" in SRC


def test_the_body_region_is_scrubbed_before_it_reaches_a_log():
    """This text goes into a CI log and is quoted into session notes. It goes
    through the same PII scrub qc_selfheal._carrier_diag_snippet uses."""
    assert "_scrub_string" in SRC, (
        "body text is printed without the scrub the carrier snippet applies")


def test_the_body_region_anchors_on_the_rows_own_etd():
    """All 22 ETA misses carry an ETD, so the two dates sit in the SAME table
    row — the ETD value names the exact line whose sibling cell went missing.
    Keyword hints alone would show six unrelated date lines."""
    rows = [{"source_imids": ["<a>"], "etd_offered": "2026-06-19"}]
    bodies = {"<a>": {"text_body": "\n".join([
        "Dear team, please see below",
        "MAERSK SEALAND 123 / 2026-06-19 / Oakland / Yokohama / $3,150",
        "unrelated line about invoices",
    ])}}
    got = D._field_region(rows[0], bodies, "eta_offered")
    assert any("2026-06-19" in line for line in got), (
        f"the row's own ETD did not anchor the region: {got}")


def test_the_body_region_is_empty_rather_than_wrong_when_no_body_is_cached():
    """A missing body must read as missing. Inventing a region from the row's
    own fields would show the parser text that never existed."""
    assert D._field_region({"source_imids": ["<gone>"]}, {}, "eta_offered") == []
    assert D._field_region({}, {}, "eta_offered") == []


def test_the_body_dump_is_opt_in():
    """Default output stays compact; the bodies are a second, deliberate ask."""
    assert "DIAG_BODIES" in SRC
    wf = (ROOT / ".github/workflows/diag-qc027.yml").read_text(encoding="utf-8")
    assert "DIAG_BODIES" in wf
    assert 'default: "0"' in wf, "the body dump defaults to ON"


def test_there_is_one_html_flattener_not_two():
    """Two flatteners drift, and then they disagree about what the parser
    'saw' — which is the only thing this diagnostic exists to report."""
    assert "from diag_bookings import _plain" in SRC, (
        "diag_qc027 has grown its own HTML-to-text conversion")


def test_it_documents_that_it_cannot_replay_the_alert_day():
    """Run 1 came back BEFORE == AFTER with Carrier at 97%, not the 87% that
    was paged — because QC-056's backfills persist, so the stored state a later
    run reads is already repaired. Anyone reading the BEFORE column as "what
    shipped that day" draws the wrong conclusion, so the limitation is written
    down and kept written down."""
    doc = D.__doc__ or ""
    assert "PERSIST" in doc or "persist" in doc, (
        "the docstring no longer explains that heals persist into the stored "
        "state — the BEFORE column reads as a replay of the alert day, which "
        "it is not")
    assert "NOT a reconstruction" in doc or "not a replay" in SRC


def test_it_runs_the_phase_on_a_copy():
    """phase_6_rules MUTATES rows. Grading the stored object in place would
    leave healed values in a file the diag has no business editing."""
    assert "copy.deepcopy(stored)" in SRC, (
        "the QC phase is being run on the stored data itself, not a copy")


# ── the workflow can actually run it ────────────────────────────────────────

def test_the_workflow_is_manual_and_installs_the_storage_sdk():
    wf = (ROOT / ".github/workflows/diag-qc027.yml").read_text(encoding="utf-8")
    assert "workflow_dispatch:" in wf
    assert "schedule:" not in wf, "a diagnostic must not fire on a schedule"
    assert "contents: read" in wf
    # requirements.txt deliberately omits azure-storage-blob; a workflow that
    # installs only requirements.txt cannot pull state. See tests/test_diag_day.
    assert "azure-storage-blob" in wf
    assert "scripts/diag_qc027.py" in wf
