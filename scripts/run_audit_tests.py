"""
run_audit_tests.py — Daily test + coverage routine for the Hilmar pipeline.

Runs the pytest suite under coverage and writes a machine-readable artifact
(reports/test-result.json) that the daily audit (gen_improvements_report.py)
and QC-052 (qc_selfheal.py) read to surface code-health in the systems audit.

WHY THIS EXISTS
  Per Michael 2026-05-28 ("a complete audit ... daily ... checking that every
  line of code has testing on it and successful ... must be in routines").
  The pipeline already had a 587-test pytest suite + an 85% coverage gate in
  pyproject.toml, but the suite ran NOWHERE in the daily routine — so the daily
  audit was blind to whether the shipped code was green. This wires the suite
  into the daily fire and exposes the result to the audit.

DESIGN — OBSERVER, NOT GATEKEEPER
  This script ALWAYS exits 0. It never blocks the daily client email, even if
  tests regress: the priority of the 10 AM fire is the client deliverable, and
  the systems audit (idealx.us only) is where a regression must be loud. The
  severity decision lives in QC-052 + collect_red_flags, which read the
  artifact this writes:
    - status FAIL (a test failed)          -> audit RED FLAG + QC-052 ERROR
    - coverage below the pyproject gate    -> audit RED FLAG + QC-052 ERROR
    - status SKIPPED (pytest unavailable)  -> audit observation + QC-052 WARN
    - modules at 0% / below floor          -> audit observation (learning loop)

  When dev deps aren't installed (e.g. a Cloud PC that only has runtime deps),
  the artifact records status="SKIPPED" with the reason so the audit can tell
  Michael to install the dev extras rather than silently reporting "all green".

USAGE
  python3 scripts/run_audit_tests.py            # run suite, write artifact
  python3 scripts/run_audit_tests.py --quiet     # suppress pytest stdout
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPORTS = ROOT / "reports"
ARTIFACT = REPORTS / "test-result.json"
COVERAGE_JSON = REPORTS / "coverage.json"
# Per-test stdout/stderr capture — when QC-052 reports failures, the audit
# email points the operator here for the full traceback. Written each fire,
# overwritten by the next. Added 2026-06-01 after the Cloud PC reported
# "0 failed, 22 error; coverage None%" with no diagnostic detail.
PYTEST_OUTPUT = REPORTS / "pytest-output.txt"

# How many per-failure excerpts to keep in the artifact. The audit email
# truncates again to ~5; we keep a few extra so QC-052 has signal even when
# the audit clips.
MAX_ERRORS_IN_ARTIFACT = 25
# Cap each traceback excerpt to keep test-result.json bounded — the full
# pytest output is always available in PYTEST_OUTPUT for the operator.
MAX_TRACEBACK_LINES = 4


def _test_root() -> Path | None:
    """Locate the directory that contains tests/ + src/hilmar/ + pyproject.toml.

    Two layouts to handle:

    1. **Development / CI** — this script lives at the repo root's
       ``scripts/``, with ``tests/`` and ``src/hilmar/`` as siblings.
       ROOT itself IS the test root.

    2. **Cloud PC production** — the wrapper xcopies ``scripts/*.py`` from
       the git checkout (``PROJECT HILMAR/hilmar-daily-routine/scripts/``)
       to a parallel ``PROJECT HILMAR/scripts/``. So when this script
       runs, ROOT = ``PROJECT HILMAR/`` — which has NO ``tests/`` and NO
       ``src/`` (only runtime data + the scripts copy). The actual git
       checkout (with tests + src + pyproject) is at
       ``ROOT/hilmar-daily-routine/``. Detect that and point pytest at
       the right place.

    Returns the test root directory if found, or ``None`` if neither
    layout matches — caller writes SKIPPED in that case rather than
    bombing pytest with a "no tests collected" error.

    Order matters: the canonical git checkout (``ROOT/hilmar-daily-routine``)
    is preferred FIRST. On the Cloud PC there can ALSO be a stale tests/+src/
    copy sitting directly in ROOT from an older layout — collecting THAT
    alongside the checkout gives two test modules with the same basename and
    pytest aborts with "import file mismatch" (the 2026-06-25 fire's 3
    collection errors). Preferring the real checkout sidesteps the stale copy.
    """
    sibling = ROOT / "hilmar-daily-routine"
    if (sibling / "tests").is_dir() and (sibling / "src" / "hilmar").is_dir():
        return sibling
    if (ROOT / "tests").is_dir() and (ROOT / "src" / "hilmar").is_dir():
        return ROOT
    return None


# A per-module floor below which we surface the module in the audit as an
# under-tested learning target. The global gate lives in pyproject; this is
# the "every line of code has testing" signal Michael asked for — it names
# the specific modules dragging the bottom (e.g. parser_accuracy.py at 0%).
MODULE_FLOOR = 80.0


def _read_gate_from_pyproject() -> float:
    """Read --cov-fail-under from pyproject.toml addopts. Fallback 85.
    Reads from the test root's pyproject (not ROOT's) so the Cloud PC
    layout reads the correct gate."""
    test_root = _test_root() or ROOT
    pp = test_root / "pyproject.toml"
    try:
        txt = pp.read_text(encoding="utf-8")
        m = re.search(r"--cov-fail-under[=\s]+(\d+(?:\.\d+)?)", txt)
        if m:
            return float(m.group(1))
    except Exception:
        pass
    return 85.0


def _write(artifact: dict) -> None:
    REPORTS.mkdir(parents=True, exist_ok=True)
    ARTIFACT.write_text(json.dumps(artifact, indent=2), encoding="utf-8")


def _pytest_available() -> tuple[bool, str]:
    try:
        import pytest  # noqa: F401
        import pytest_cov  # noqa: F401
        return True, ""
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def _parse_counts(stdout: str) -> dict:
    """Pull passed/failed/error/skipped counts from the pytest summary line."""
    counts = {"passed": 0, "failed": 0, "error": 0, "skipped": 0}
    for key in counts:
        m = re.search(rf"(\d+)\s+{key}", stdout)
        if m:
            counts[key] = int(m.group(1))
    return counts


# Pytest's "short test summary info" block is the most reliable place to
# extract per-test diagnostics across pytest versions: one line per failed/
# errored test in the form:
#   FAILED tests/test_x.py::test_foo - AssertionError: x != y
#   ERROR tests/test_x.py - ModuleNotFoundError: No module named 'foo'
_SUMMARY_LINE_RE = re.compile(
    r"^(FAILED|ERROR)\s+([^\s]+(?:::[^\s]+)?)(?:\s+-\s+(.*))?$"
)
# A typed exception always shows up as "TypeName: message" once we strip a
# leading "E   " marker pytest adds in --tb=short output. Used both to
# bucket and to recognize the "short message" payload.
_EXC_RE = re.compile(
    r"^([A-Z][A-Za-z_0-9.]*Error|Exception|Warning|TimeoutError|KeyboardInterrupt)(?::\s*(.*))?$"
)


def _classify_error_type(short_msg: str) -> str:
    """Bucket a short error message into a typed exception name.

    Falls back to ``UnknownError`` when pytest's summary line doesn't carry a
    typed exception (rare — happens for some plugin hook crashes). Used by
    both the artifact's ``error_type_buckets`` array and the QC-052 audit
    text so the operator sees ``12x ModuleNotFoundError`` instead of just
    ``22 error``.
    """
    if not short_msg:
        return "UnknownError"
    s = short_msg.strip()
    m = _EXC_RE.match(s)
    if m:
        return m.group(1)
    # Collection failures may have prose preamble; pull the typed name out.
    m2 = re.search(r"\b([A-Z][A-Za-z_0-9.]*Error)\b", s)
    if m2:
        return m2.group(1)
    return "UnknownError"


def _parse_failures(stdout: str, *, max_errors: int = MAX_ERRORS_IN_ARTIFACT) -> list[dict]:
    """Extract per-test failure/error diagnostics from pytest's output.

    Parses two regions of pytest's text output:

    1. **Short test summary info** — one line per failure/error, gives us
       nodeid + a typed short message. This is what pytest emits with
       ``-rfE`` (or ``-ra`` which the suite uses by default).
    2. **FAILURES / ERRORS sections** — multi-line tracebacks per test.
       We pick the first ~4 lines of each as the "traceback excerpt" so
       the audit email has enough context to triage without dumping the
       full stack.

    Returns at most ``max_errors`` entries to bound test-result.json size.
    The full output is always preserved in PYTEST_OUTPUT for the operator.

    No new pytest plugin / dependency — text parsing only. Cost of fighting
    pytest version drift is contained and locked in by tests.
    """
    failures: list[dict] = []

    # Pass 1 — short summary block. Format is stable across recent pytest:
    #   ========== short test summary info ==========
    #   FAILED tests/x.py::test_y - AssertionError: ...
    #   ERROR  tests/x.py - ModuleNotFoundError: ...
    summary_idx = stdout.find("short test summary info")
    summary_block = stdout[summary_idx:] if summary_idx >= 0 else stdout
    for raw in summary_block.splitlines():
        line = raw.strip()
        m = _SUMMARY_LINE_RE.match(line)
        if not m:
            continue
        phase_word = m.group(1)
        nodeid = m.group(2)
        short_msg = (m.group(3) or "").strip()
        failures.append({
            "nodeid": nodeid,
            "phase": "collect" if phase_word == "ERROR" else "call",
            "error_type": _classify_error_type(short_msg),
            "short_message": short_msg[:300],  # bound per-entry size
            "traceback_excerpt": [],
        })
        if len(failures) >= max_errors:
            break

    # Pass 2 — try to attach a short traceback excerpt for each. Look in
    # the FAILURES / ERRORS sections by header (pytest underlines test
    # names with ``___`` so each test's traceback starts predictably).
    if failures:
        sections = re.split(r"^=+\s+(FAILURES|ERRORS)\s+=+\s*$", stdout, flags=re.MULTILINE)
        body = ""
        for i in range(1, len(sections), 2):
            if i + 1 < len(sections):
                body += "\n" + sections[i + 1]
        if body:
            # Each per-test block starts with a header line of underscores
            # surrounding the test name. We capture the body until the next
            # header or the end of the body.
            header_re = re.compile(r"^_{3,}\s+(.+?)\s+_{3,}\s*$", re.MULTILINE)
            matches = list(header_re.finditer(body))
            for idx, mat in enumerate(matches):
                name = mat.group(1).strip()
                start = mat.end()
                end = matches[idx + 1].start() if idx + 1 < len(matches) else len(body)
                tb_lines = [
                    ln.rstrip()
                    for ln in body[start:end].splitlines()
                    if ln.strip()
                ][:MAX_TRACEBACK_LINES]
                # Match this traceback to a failure by suffix on the nodeid.
                for f in failures:
                    if f["nodeid"].endswith("::" + name) or f["nodeid"].endswith(name):
                        if not f["traceback_excerpt"]:
                            f["traceback_excerpt"] = tb_lines
                        break

    return failures


def _bucket_error_types(failures: list[dict]) -> list[dict]:
    """Aggregate failures into an ordered ``[{error_type, count}, ...]`` list.

    Sorted by descending count then alphabetical so the audit email's "most
    common error type" line is deterministic across re-runs of the same set.
    """
    if not failures:
        return []
    c = Counter(f.get("error_type") or "UnknownError" for f in failures)
    return [
        {"error_type": et, "count": n}
        for et, n in sorted(c.items(), key=lambda kv: (-kv[1], kv[0]))
    ]


def _detect_collection_error(stdout: str, failures: list[dict]) -> bool:
    """True when pytest hit collection errors (modules failed to import).

    Two signals: the explicit ``Interrupted: N error during collection`` line
    pytest prints when collection is fatal, OR a majority of the captured
    failures are in the ``collect`` phase. Used to set ``collection_error``
    in the artifact so the audit email can lead with the right diagnosis
    ("modules failed to import" vs "tests ran but failed").
    """
    if re.search(r"Interrupted:\s+\d+\s+error[s]?\s+during\s+collection", stdout):
        return True
    return bool(failures and sum(1 for f in failures if f.get("phase") == "collect") >= max(1, len(failures) // 2))


# Collection errors of these types mean the HOST can't import a test
# dependency — an ENVIRONMENT gap, not a code regression.
_IMPORT_ERR_TYPES = frozenset({"ModuleNotFoundError", "ImportError"})


def _classify_outcome(counts: dict, failures: list[dict],
                      total_cov: float | None, gate: float) -> dict:
    """Decide PASS / FAIL / SKIPPED, separating a CODE failure from an
    ENVIRONMENT-incomplete run.

    A collection ImportError/ModuleNotFoundError means this host lacks a
    test-only dependency (e.g. the Cloud PC installs bare runtime deps, so
    ``jinja2`` — used only by ``src/hilmar/render.py``, never the production
    ``scripts/`` — is absent). That is NOT a code regression and must not
    red-flag the audit; the authoritative suite runs in CI. What DOES fail:
      - a call-phase test failure (real regression),
      - a non-import collection error (e.g. a syntax error in a test),
      - an error count we could not positively classify (stay conservative),
      - coverage below the gate (only when coverage was actually measured).

    Returns {status, env_incomplete, missing_modules, reason}.
    """
    call_failures = [f for f in failures if f.get("phase") == "call"]
    collect_failures = [f for f in failures if f.get("phase") == "collect"]
    import_errors = [f for f in collect_failures
                     if f.get("error_type") in _IMPORT_ERR_TYPES]
    real_collect = [f for f in collect_failures
                    if f.get("error_type") not in _IMPORT_ERR_TYPES]

    missing: list[str] = []
    for f in import_errors:
        m = re.search(r"No module named ['\"]([\w.]+)['\"]", f.get("short_message", ""))
        if m:
            missing.append(m.group(1))
    missing = sorted(set(missing))

    # Coverage fails the gate when measured-and-below, OR when tests actually
    # ran but coverage couldn't be measured at all (preserves the old
    # conservative behavior). A SKIPPED env run (0 passed) gets no coverage
    # penalty — there was nothing to measure.
    coverage_fail = (
        (total_cov is not None and total_cov < gate)
        or (total_cov is None and int(counts.get("passed", 0)) > 0)
    )
    # Any error we could NOT classify as a missing-dep import error is treated
    # as a real problem — never mask an unknown break.
    unclassified = max(0, int(counts.get("error", 0)) - len(collect_failures))
    real_failure = (
        int(counts.get("failed", 0)) > 0
        or bool(call_failures)
        or bool(real_collect)
        or unclassified > 0
        or coverage_fail
    )
    env_incomplete = bool(import_errors) and not real_failure

    if real_failure:
        status = "FAIL"
    elif env_incomplete and int(counts.get("passed", 0)) == 0:
        status = "SKIPPED"   # nothing could run in this env
    else:
        status = "PASS"

    reason = None
    if env_incomplete:
        mods = ", ".join(missing) if missing else "test-only dependencies"
        reason = (
            f"{len(import_errors)} test module(s) could not be imported on this "
            f"host (missing {mods}) — library/test-only deps NOT used by the "
            f"production pipeline (it runs scripts/, not src/hilmar). The "
            f"authoritative suite runs in CI; install dev extras to run it here: "
            f"pip install -e '.[dev]'."
        )
    return {"status": status, "env_incomplete": env_incomplete,
            "missing_modules": missing, "reason": reason}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quiet", action="store_true", help="Suppress pytest stdout")
    args = ap.parse_args()

    gate = _read_gate_from_pyproject()
    now = datetime.now(timezone.utc).isoformat()

    available, reason = _pytest_available()
    if not available:
        artifact = {
            "status": "SKIPPED",
            "reason": (
                "pytest / pytest-cov not importable in this environment — "
                f"{reason}. Install dev deps: pip install -e '.[dev]'. Until "
                "then the daily audit cannot verify code health."
            ),
            "generated_at": now,
            "gate": gate,
        }
        _write(artifact)
        print(f"⏭️  test-result.json: SKIPPED — {reason}")
        return 0  # observer: never block the pipeline

    # Locate tests/ + src/hilmar/. On Cloud PC production these live one
    # level deeper than ROOT (in hilmar-daily-routine/). Without this,
    # pytest gets a cwd with no tests + no `hilmar` package and emits a
    # wall of collection errors — exactly the QC-052 failure mode
    # observed in production on the 2026-05-30 manual fire.
    test_root = _test_root()
    if test_root is None:
        artifact = {
            "status": "SKIPPED",
            "reason": (
                "Could not locate tests/ + src/hilmar/ next to this script "
                f"(checked {ROOT} and {ROOT / 'hilmar-daily-routine'}). The "
                "test routine needs the git checkout layout to discover "
                "tests — verify the wrapper's git pull is current."
            ),
            "generated_at": now,
            "gate": gate,
        }
        _write(artifact)
        print("⏭️  test-result.json: SKIPPED — no tests/+src/hilmar/ found")
        return 0  # observer: never block the pipeline

    # Run the suite with a JSON coverage report we can parse for per-module %.
    # --cov-fail-under=0 here so pytest's own exit code reflects ONLY test
    # pass/fail; the coverage-gate decision is made below against `gate` so we
    # can report the exact margin instead of a bare non-zero exit.
    # Coverage JSON goes to the test_root's reports/ so the file ends up
    # next to where pytest writes it; we read it back below regardless of
    # whether REPORTS (under ROOT) is the same dir.
    coverage_json = test_root / "reports" / "coverage.json"
    # ``-rfE`` forces pytest to emit the per-test "FAILED ..." / "ERROR ..."
    # lines in the short summary block — _parse_failures() reads those to
    # produce per-test diagnostics so the audit email isn't blind.
    # ``--tb=short`` keeps the FAILURES section compact enough to scrape a
    # few-line excerpt per test without explosion on a 22-error fire.
    cmd = [
        sys.executable, "-m", "pytest", "-q", "--no-header",
        "-rfE",
        "--tb=short",
        # Collect ONLY the checkout's tests — never a stale sibling copy at a
        # parent dir (the source of the "import file mismatch" collisions).
        str(test_root / "tests"),
        # A couple of missing-dep import errors must NOT abort the whole
        # session (default pytest behavior is "Interrupted: N errors during
        # collection" → 0 tests run). Run everything collectable; the missing
        # modules surface as classified collection errors below.
        "--continue-on-collection-errors",
        "--cov=hilmar",
        f"--cov-report=json:{coverage_json}",
        "--cov-fail-under=0",
    ]
    proc = subprocess.run(cmd, cwd=str(test_root), capture_output=True, text=True)
    stdout = proc.stdout + "\n" + proc.stderr
    if not args.quiet:
        print(stdout[-4000:])

    # Persist the raw output so the audit email can point the operator at it
    # without us trying to cram a 22-traceback wall into test-result.json.
    try:
        REPORTS.mkdir(parents=True, exist_ok=True)
        PYTEST_OUTPUT.write_text(stdout, encoding="utf-8")
    except Exception as _e:  # pragma: no cover - defensive
        print(f"⚠️  could not write {PYTEST_OUTPUT}: {_e}")

    counts = _parse_counts(stdout)
    failures = _parse_failures(stdout)
    error_buckets = _bucket_error_types(failures)
    collection_error = _detect_collection_error(stdout, failures)

    total_cov = None
    modules_below_floor: list[dict] = []
    untested: list[str] = []
    try:
        cov = json.loads(coverage_json.read_text(encoding="utf-8"))
        total_cov = round(float(cov["totals"]["percent_covered"]), 2)
        for path, fdata in (cov.get("files") or {}).items():
            pct = round(float(fdata["summary"]["percent_covered"]), 2)
            mod = Path(path).name
            if pct == 0.0:
                untested.append(mod)
            if pct < MODULE_FLOOR:
                modules_below_floor.append({"module": mod, "coverage": pct})
        modules_below_floor.sort(key=lambda m: m["coverage"])
    except Exception as e:
        print(f"⚠️  could not parse {coverage_json}: {e}")

    coverage_ok = total_cov is not None and total_cov >= gate

    # Overall status: FAIL on a real code regression or coverage below gate;
    # a missing test-only dependency is an ENVIRONMENT gap (SKIPPED/PASS), not
    # a FAIL — see _classify_outcome. tests_ok is retained for the artifact.
    outcome = _classify_outcome(counts, failures, total_cov, gate)
    status = outcome["status"]
    # Align tests_ok with the classified outcome (was a raw "0 errors" flag,
    # which mislabeled an env-incomplete run as not-ok).
    tests_ok = status == "PASS"

    # pytest_output_path: serialize as a repo-relative POSIX path when the
    # output landed under ROOT; otherwise fall back to the absolute path.
    # Using POSIX form keeps the audit email readable across Windows/Linux.
    try:
        if PYTEST_OUTPUT.is_relative_to(ROOT):
            output_ref = PYTEST_OUTPUT.relative_to(ROOT).as_posix()
        else:  # pragma: no cover - defensive
            output_ref = str(PYTEST_OUTPUT)
    except (AttributeError, ValueError):  # pragma: no cover - py<3.9 / cross-drive
        output_ref = str(PYTEST_OUTPUT)

    artifact = {
        "status": status,
        "tests_ok": tests_ok,
        "coverage_ok": coverage_ok,
        "counts": counts,
        "total_coverage": total_cov,
        "gate": gate,
        "coverage_margin": (round(total_cov - gate, 2) if total_cov is not None else None),
        "module_floor": MODULE_FLOOR,
        "modules_below_floor": modules_below_floor,
        "untested_modules": untested,
        # NEW 2026-06-01: per-test diagnostics so the daily audit isn't blind
        # when the suite breaks. Old consumers that didn't know these fields
        # ignore them — backward compatible by addition, not rename.
        "errors": failures,
        "error_type_buckets": error_buckets,
        "collection_error": collection_error,
        # NEW 2026-06-25: distinguish a missing test-only dependency (this
        # host can't import it) from a code regression, so the audit doesn't
        # red-flag an incomplete Cloud-PC env as broken code.
        "env_incomplete": outcome["env_incomplete"],
        "missing_modules": outcome["missing_modules"],
        "reason": outcome["reason"],
        "pytest_output_path": output_ref,
        "generated_at": now,
    }
    _write(artifact)

    _icon = {"PASS": "✅", "SKIPPED": "⏭️"}.get(status, "❌")
    print(
        f"{_icon} test-result.json: {status} — "
        f"{counts['passed']} passed / {counts['failed']} failed / "
        f"{counts['error']} error · coverage {total_cov}% (gate {gate}%)"
    )
    if outcome["env_incomplete"]:
        print(f"   ⏭️  environment incomplete (not a code failure): {outcome['reason']}")
    if untested:
        print(f"   ⚠️  untested modules (0%): {', '.join(untested)}")
    return 0  # observer: never block the pipeline


if __name__ == "__main__":
    sys.exit(main())
