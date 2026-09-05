"""The 95% parser-accuracy gate (QC-039) is a HARD client-ship block (Option A).

Root-cause regression test for the CRITICAL audit finding: CLAUDE.md rule #2
says sub-95% parser accuracy BLOCKS the pipeline, but the code shipped anyway
(qc_selfheal returned 0 on a post-patch ERROR; run_pipeline treated QC as
non-blocking). The fix:
  - qc_selfheal.main() returns QC039_GATE_BLOCK_RC when the POST-PATCH run logs a
    QC-039 ERROR (and fires the out-of-band alarm), and 0 in pre-patch.
  - run_pipeline treats THAT exit code from the post-patch QC step as
    client-blocking (aborts before the email is built/sent).

This locks the gate decision and the cross-module exit-code agreement so the
"95% hard gate" can never silently go decorative again.
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import qc_selfheal as Q  # noqa: E402

# The shape the ERROR branch EMITS since 2026-09-05 (each critical field names
# its own floor — "ol_rate=0/1 (0.0%, floor 95%)", never "below 95%"). The gate
# keys on "QC-039" plus the FAILED TO EVALUATE marker, so the wording is not
# load-bearing here; the fixture follows production so it cannot drift into a
# line nothing emits. The rendered line itself is proven against the gate in
# tests/test_qc084_sourceless_booking_borrows_nothing.py.
_MEASURED_MISS = ("QC-039: parser accuracy 80.0% (weighted 82.0%) with 2 CRITICAL "
                  "field(s) below floor: ol_rate=40/50 (80.0%, floor 95%), "
                  "carrier_won=41/50 (82.0%, floor 95%)")


def _const_in(rel: str) -> int:
    src = (ROOT / rel).read_text(encoding="utf-8")
    m = re.search(r"QC039_GATE_BLOCK_RC\s*=\s*(\d+)", src)
    assert m, f"QC039_GATE_BLOCK_RC not defined in {rel}"
    return int(m.group(1))


def test_gate_code_matches_across_modules():
    qc = _const_in("scripts/qc_selfheal.py")
    rp = _const_in("scripts/run_pipeline.py")
    assert qc == rp == Q.QC039_GATE_BLOCK_RC == 39, (
        "qc_selfheal and run_pipeline must agree on the gate exit code"
    )


def test_post_patch_qc039_measured_miss_blocks_the_ship():
    rc = Q._gate_exit_code([_MEASURED_MISS], pre_patch=False)
    assert rc == Q.QC039_GATE_BLOCK_RC, "a MEASURED sub-95% miss must hard-block the ship"


def test_post_patch_qc039_eval_failure_does_NOT_block():
    """A gate that could not EVALUATE (missing dep / src not deployed) screams +
    HAS_ERRORS for visibility but must NOT block — a deploy gap is not sub-95%
    data, and blocking the client email over it is a self-inflicted outage. This
    is the "No module named 'hilmar'" case seen on the box."""
    rc = Q._gate_exit_code(["QC-039: parser-accuracy gate FAILED TO EVALUATE (failing closed): No module named 'hilmar'"],
                           pre_patch=False)
    assert rc == 0, "an un-evaluable gate must scream-but-ship, not block"


def test_block_vs_uneval_partition():
    errs = [
        _MEASURED_MISS,
        "QC-039: parser-accuracy gate FAILED TO EVALUATE (failing closed): boom",
    ]
    assert len(Q._qc039_block_errors(errs)) == 1
    assert len(Q._qc039_uneval_errors(errs)) == 1
    # A run with ONLY an eval-failure must not block; add a real miss and it does.
    assert Q._gate_exit_code(errs[1:], pre_patch=False) == 0
    assert Q._gate_exit_code(errs, pre_patch=False) == Q.QC039_GATE_BLOCK_RC


def test_pre_patch_never_blocks():
    rc = Q._gate_exit_code([_MEASURED_MISS], pre_patch=True)
    assert rc == 0, "pre-patch is advisory and must never block the ship"


def test_non_qc039_errors_do_not_trigger_the_gate():
    rc = Q._gate_exit_code(["QC-032: no backup target is fresh", "QC-049: WIN missing MDOLX"],
                           pre_patch=False)
    assert rc == 0, "only the QC-039 accuracy gate is client-blocking under Option A"


def test_clean_run_does_not_block():
    assert Q._gate_exit_code([], pre_patch=False) == 0


def test_run_pipeline_treats_the_gate_code_as_blocking():
    # Structural lock: run_pipeline must (a) compare the post-patch QC step's rc
    # to QC039_GATE_BLOCK_RC and (b) reclassify it into blocking_failures.
    src = (ROOT / "scripts" / "run_pipeline.py").read_text(encoding="utf-8")
    assert "QC039_GATE_BLOCK_RC" in src
    assert re.search(r'name == "QC self-heal \(post-patch\)".*QC039_GATE_BLOCK_RC', src), (
        "run_pipeline must gate specifically on the post-patch QC step + the gate code"
    )
    assert "gate_blocked" in src and "blocking_failures.append" in src
