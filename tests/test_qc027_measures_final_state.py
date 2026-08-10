"""QC-027 graded the rows before the heals repaired them — and before the
scrub broke them.

2026-08-10, Michael on the daily Sentry page: "you have to fix this.. it used
to work.. don't know what you did."

Nothing broke the carriers. QC-027's measurement sat ~1200 lines up inside
phase_6_rules, ahead of two heals that write the very fields it grades:

    QC-027  measures carrier_quoted, pol, pod, vessel_voyage, …   (line ~3900)
    QC-056  BACKFILLS carrier_quoted from row text, then from a
            same-lane same-rate sibling                            (line ~4290)
    QC-064  NULLS garbage out of carrier_quoted / pol / pod /
            vessel_voyage and five other client-visible fields     (line ~4717)

Four of QC-027's seven graded fields are written by QC-064; one is written by
QC-056. So "Carrier=87% (ERROR <90%)" described a state that did not survive
its own run: it counted as MISSING every carrier QC-056 was about to restore,
and counted as PRESENT every value QC-064 was about to blank. Both errors, in
both directions, every single day.

This is the FOURTH instance of the shape in this one phase — QC-039 (2026-07-27,
which withheld a business day's client report), batch-5 #15's persisted
aggregates, QC-075's stale summary, now QC-027. The rule QC-039's banner states
is the rule tested here: A GATE MEASURES THE FINAL STATE OF THE ROWS, AFTER
EVERY MUTATING HEAL. Heals stay early — QC-027's own pol/pod derivation is
deliberately left in place up top, because it WRITES fields QC-064 later
scrubs, and a heal that runs after the scrub ships a derived value nothing
checked.

The structural test is an AST walk, not a substring scan: QC-064 writes through
a loop variable (`r[_f] = None`), which no text search for `r["carrier_quoted"]`
can see. That is the exact blind spot that let the first QC-039 fix ship
half-done.
"""
from __future__ import annotations

import ast
import contextlib
import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
for p in (str(SCRIPTS), str(ROOT / "src")):
    if p not in sys.path:
        sys.path.insert(0, p)

import core  # noqa: E402
import qc_selfheal as QC  # noqa: E402

GRADED = {f for f, _label in QC.QC027_FIELDS}

# The statement that begins QC-027's measurement. Anchored on the real
# identifier rather than a comment, so moving the comment can't fool the test.
MEASURE_ANCHOR = "_active27"


def _phase6():
    src = (SCRIPTS / "qc_selfheal.py").read_text(encoding="utf-8")
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.FunctionDef) and n.name == "phase_6_rules")
    return src, fn


def _measurement_lineno(fn) -> int:
    """Line where QC-027's measurement starts reading rows."""
    for node in ast.walk(fn):
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id == MEASURE_ANCHOR):
            return node.lineno
    raise AssertionError(
        f"QC-027's measurement anchor {MEASURE_ANCHOR!r} is gone from "
        "phase_6_rules — if it was renamed, update this guard; if the check "
        "was deleted, the completeness alarm is gone")


def _graded_field_writes(fn):
    """Every `something[<field>] = ...` inside phase_6_rules that could land on
    a field QC-027 grades.

    A literal key is checked against GRADED. A NON-literal key (`r[_f] = None`,
    QC-064's loop) is treated as a possible hit — conservative on purpose: the
    alternative is a guard that goes quiet the moment someone writes through a
    variable, which is how this defect survived its first fix.
    """
    out = []
    for node in ast.walk(fn):
        targets = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
            targets = [node.target]
        for t in targets:
            if not isinstance(t, ast.Subscript):
                continue
            key = t.slice
            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                if key.value in GRADED:
                    out.append((node.lineno, key.value))
            elif isinstance(key, (ast.Name, ast.Attribute)):
                out.append((node.lineno, f"<variable key: {ast.unparse(key)}>"))
    return out


# ── structural: the measurement runs last ───────────────────────────────────

def test_no_graded_field_is_written_after_qc027_measures():
    """THE FIX. Every write to a QC-027-graded field inside phase_6_rules must
    happen BEFORE the measurement, or the number reported is a state that no
    longer exists when the run ends."""
    _src, fn = _phase6()
    measure = _measurement_lineno(fn)
    late = [(ln, f) for ln, f in _graded_field_writes(fn) if ln > measure]
    detail = "\n  ".join(f"line {ln}: {f}" for ln, f in late)
    assert not late, (
        f"QC-027 measures at line {measure}, but these writes to graded fields "
        f"happen AFTER it:\n  {detail}\nA gate measures the FINAL state of the "
        "rows, after every mutating heal.")


def test_the_guard_can_actually_see_qc064s_loop_write():
    """Non-vacuity for the walk itself. QC-064 nulls through a loop variable;
    if the collector could not see that, the test above would pass on a file
    where the nulling ran after the measurement."""
    _src, fn = _phase6()
    writes = _graded_field_writes(fn)
    assert any("variable key" in f for _ln, f in writes), (
        "the AST walk found no variable-key writes — QC-064's `r[_f] = None` "
        "is exactly that shape, so the collector is blind and this guard is "
        "worthless")
    assert any(f == "carrier_quoted" for _ln, f in writes), (
        "no literal carrier_quoted write found — QC-056's backfill is the "
        "heal this ordering exists to respect")


def test_qc056_still_heals_before_qc027_grades_carrier():
    """The specific pair, stated directly, so a failure names the cause."""
    src, fn = _phase6()
    seg = ast.get_source_segment(src, fn) or ""
    heal = seg.find('r["carrier_quoted"] = _car')
    measure = seg.find(MEASURE_ANCHOR)
    assert heal != -1, "QC-056's carrier backfill is gone"
    assert measure != -1, "QC-027's measurement is gone"
    assert heal < measure, (
        "QC-027 grades carrier_quoted before QC-056 backfills it — every "
        "healable row counts as a miss, which is the 87% Michael was paged for")


def test_nothing_after_phase_6_writes_a_graded_field_either():
    """The guard above is scoped to ONE function. main() keeps going after it —
    _recompute_aggregates, _trade_region_reconciliation, phase_7_save — and a
    graded-field write in any of those would put the measurement back in the
    past without touching phase_6_rules at all. Measured, and pinned: today
    all three write only summary dicts."""
    src = (SCRIPTS / "qc_selfheal.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    offenders = []
    for name in ("_recompute_aggregates", "_trade_region_reconciliation",
                 "phase_7_save"):
        fn = next((n for n in ast.walk(tree)
                   if isinstance(n, ast.FunctionDef) and n.name == name), None)
        assert fn is not None, f"{name}() is gone — main()'s tail changed shape"
        for ln, f in _graded_field_writes(fn):
            offenders.append(f"{name}() line {ln}: {f}")
    assert not offenders, (
        "these run AFTER phase_6_rules and write fields QC-027 grades, so the "
        "measurement is stale again:\n  " + "\n  ".join(offenders))


def test_the_polpod_heal_stays_ahead_of_the_scrub():
    """The half that must NOT move. QC-027's pol/pod derivation WRITES fields
    QC-064 scrubs; dragging it down with the measurement would put a derived
    value in the client email with nothing checking it."""
    src, fn = _phase6()
    seg = ast.get_source_segment(src, fn) or ""
    heal = seg.find('r["pol"] = _p')
    scrub = seg.find("r[_f] = None")
    assert heal != -1, "QC-027's POL/POD self-heal is gone"
    assert scrub != -1, "QC-064's nulling is gone"
    assert heal < scrub, (
        "the POL/POD heal now runs after QC-064's scrub — derived ports would "
        "reach the client email unchecked")


# ── behavioural: both directions of the error ───────────────────────────────

def _reachable_row(rid, carrier="CMA CGM", vessel="EVER GIVEN 021E", rate=2400.0):
    """A row inside QC-027's denominator: live status, a response we timed, and
    a rate-response trace. Every graded field but Carrier is populated, so the
    Carrier percentage is the only thing that can move."""
    now = core.now_utc()
    return {
        "request_id": rid, "status": "LOSS", "loss_reason": "PRICE",
        "quoted": True, "ol_rate": rate, "carrier_quoted": carrier,
        "origin": "Oakland", "destination": "Busan", "lane": "Oakland → Busan",
        "pol": "Oakland", "pod": "Busan",
        "etd_offered": "2026-07-01", "eta_offered": "2026-07-25",
        "vessel_voyage": vessel,
        "teu_requested": 4, "container_count": 2,
        "request_date": core.et_date_of(now),
        "request_timestamp": now.isoformat(),
        "response_timestamp": now.isoformat(),
        "source_imids": [f"<{rid}@ol>"], "status_history": [],
    }


def _run_phase6(rows):
    data = {"requests": rows, "summary": {}, "lane_summary": {},
            "carrier_summary": {}}
    log = QC.Log()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        QC.phase_5_summaries(log, data)
        QC.phase_6_rules(log, data)
    # log.ok() only PRINTS — there is no `oks` list — so the pass line is
    # recoverable from stdout and nowhere else.
    log.stdout = buf.getvalue()
    return log, data


def _qc027_lines(log):
    return [ln for ln in log.stdout.splitlines() if "QC-027:" in ln]


def test_a_row_qc056_can_heal_is_not_counted_as_a_missing_carrier():
    """Direction one — the false alarm. Two of ten rows have a rate and no
    carrier, but name one in vessel_voyage, so QC-056 backfills both. Graded
    before the heal that is 8/10 = 80%, an ERROR page. Graded after, it is
    10/10."""
    rows = [_reachable_row(f"ok{i}") for i in range(8)]
    rows += [_reachable_row("heal1", carrier=None, vessel="MAERSK SEALAND 123"),
             _reachable_row("heal2", carrier=None, vessel="MAERSK ESSEX 44W")]
    log, _data = _run_phase6(rows)

    carrier_errors = [m for m in getattr(log, "errors", [])
                      if "QC-027:" in m and "Carrier" in m]
    assert not carrier_errors, (
        "QC-027 still reports a Carrier shortfall on rows QC-056 healed in the "
        f"same run: {carrier_errors}")


def test_a_carrier_qc064_nulls_is_counted_as_missing():
    """Direction two, and the worse one — the number that reads GREEN on data
    already blanked. A phone fragment in carrier_quoted is garbage QC-064
    nulls. Graded before the scrub it counts as present; graded after, it is
    the miss it really is."""
    rows = [_reachable_row(f"ok{i}") for i in range(6)]
    rows += [_reachable_row(f"junk{i}", carrier="209-656") for i in range(4)]
    log, data = _run_phase6(rows)

    nulled = [r for r in data["requests"] if not r.get("carrier_quoted")]
    assert len(nulled) == 4, (
        "QC-064 did not null the phone-fragment carriers — the premise of this "
        f"test is gone (nulled {len(nulled)})")
    carrier_errors = [m for m in getattr(log, "errors", [])
                      if "QC-027:" in m and "Carrier" in m]
    assert carrier_errors, (
        "six of ten rows have a carrier after the scrub — 60% — and QC-027 did "
        "not report it. The gate is reading pre-scrub values and would call a "
        "blanked client email complete")


def test_qc027_still_reports_on_a_clean_dataset():
    """Nothing about the move may silence the check itself."""
    rows = [_reachable_row(f"ok{i}") for i in range(10)]
    log, _data = _run_phase6(rows)
    assert _qc027_lines(log), "QC-027 emitted nothing at all — the check is dead"
    assert not [m for m in getattr(log, "errors", []) if "QC-027:" in m], (
        "a fully populated dataset tripped QC-027")


# ── the denominator has exactly one definition ──────────────────────────────

def test_the_check_and_its_helpers_share_one_denominator():
    """The QC and any diagnostic must select the same rows. A re-typed list
    comprehension in a diag script answers questions about a set nobody is
    measuring."""
    src, fn = _phase6()
    seg = ast.get_source_segment(src, fn) or ""
    assert "qc027_active_rows(requests)" in seg, (
        "phase_6_rules no longer calls qc027_active_rows — the denominator has "
        "been re-inlined and can now drift from every tool that reports on it")
    assert seg.count("qc027_is_reachable(") >= 2, (
        "the reachable/PDF-only split is no longer computed from "
        "qc027_is_reachable")


def test_reachability_is_the_documented_three_signals():
    """Pinned because the denominator IS the finding: a completeness ratio
    moves when its denominator moves, and this predicate is the denominator."""
    assert QC.qc027_is_reachable({"etd_offered": "2026-07-01"})
    assert QC.qc027_is_reachable({"vessel_voyage": "EVER GIVEN 021E"})
    assert QC.qc027_is_reachable({"ol_rate": 2400.0})
    assert not QC.qc027_is_reachable({})
    assert not QC.qc027_is_reachable({"carrier_quoted": "CMA CGM"}), (
        "a carrier alone now makes a row reachable — it would enter its own "
        "denominator and the Carrier percentage could never fall")


def test_active_requires_both_a_live_status_and_a_timed_response():
    now = core.now_utc().isoformat()
    rows = [
        {"status": "WIN", "response_timestamp": now},
        {"status": "LOSS", "response_timestamp": now},
        {"status": "PENDING", "response_timestamp": now},
        {"status": "WIN", "response_timestamp": None},      # standalone booking
        {"status": "ARCHIVED", "response_timestamp": now},
    ]
    got = QC.qc027_active_rows(rows)
    assert len(got) == 3
    assert all(r.get("response_timestamp") for r in got)


def test_pdf_only_and_reachable_are_a_clean_partition():
    """They used to be split with `r not in _reachable`, an equality scan over
    dicts: two rows with identical content collapsed, and the halves could
    overlap or lose a row. Now both sides call the same predicate."""
    rows = [{"etd_offered": "2026-07-01"}, {}, {"etd_offered": "2026-07-01"}]
    reachable = [r for r in rows if QC.qc027_is_reachable(r)]
    pdf_only = [r for r in rows if not QC.qc027_is_reachable(r)]
    assert len(reachable) + len(pdf_only) == len(rows)
    assert len(reachable) == 2 and len(pdf_only) == 1
