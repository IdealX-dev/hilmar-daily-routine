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
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPORTS = ROOT / "reports"
ARTIFACT = REPORTS / "test-result.json"
COVERAGE_JSON = REPORTS / "coverage.json"


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
    """
    if (ROOT / "tests").is_dir() and (ROOT / "src" / "hilmar").is_dir():
        return ROOT
    sibling = ROOT / "hilmar-daily-routine"
    if (sibling / "tests").is_dir() and (sibling / "src" / "hilmar").is_dir():
        return sibling
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
        print(f"⏭️  test-result.json: SKIPPED — no tests/+src/hilmar/ found")
        return 0  # observer: never block the pipeline

    # Run the suite with a JSON coverage report we can parse for per-module %.
    # --cov-fail-under=0 here so pytest's own exit code reflects ONLY test
    # pass/fail; the coverage-gate decision is made below against `gate` so we
    # can report the exact margin instead of a bare non-zero exit.
    # Coverage JSON goes to the test_root's reports/ so the file ends up
    # next to where pytest writes it; we read it back below regardless of
    # whether REPORTS (under ROOT) is the same dir.
    coverage_json = test_root / "reports" / "coverage.json"
    cmd = [
        sys.executable, "-m", "pytest", "-q", "--no-header",
        "--cov=hilmar",
        f"--cov-report=json:{coverage_json}",
        "--cov-fail-under=0",
    ]
    proc = subprocess.run(cmd, cwd=str(test_root), capture_output=True, text=True)
    stdout = proc.stdout + "\n" + proc.stderr
    if not args.quiet:
        print(stdout[-4000:])

    counts = _parse_counts(stdout)
    tests_ok = proc.returncode == 0 and counts["failed"] == 0 and counts["error"] == 0

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

    # Overall status: FAIL if a test failed OR coverage is below the gate.
    if not tests_ok or not coverage_ok:
        status = "FAIL"
    else:
        status = "PASS"

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
        "generated_at": now,
    }
    _write(artifact)

    print(
        f"{'✅' if status == 'PASS' else '❌'} test-result.json: {status} — "
        f"{counts['passed']} passed / {counts['failed']} failed / "
        f"{counts['error']} error · coverage {total_cov}% (gate {gate}%)"
    )
    if untested:
        print(f"   ⚠️  untested modules (0%): {', '.join(untested)}")
    return 0  # observer: never block the pipeline


if __name__ == "__main__":
    sys.exit(main())
