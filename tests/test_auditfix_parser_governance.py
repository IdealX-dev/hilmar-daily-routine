"""Parser-accuracy gate governance (audit findings [27], [28]).

[28] PER_FIELD_THRESHOLDS relaxes the 95% gate for several sparse fields
     (mdolx_ref 0.80, dest_free_time 0.85, etc.). CLAUDE.md rule #3 forbids
     lowering a gate to make a red check pass, so these standing overrides must
     be a SHRINK-ONLY exception set: the count of below-gate fields may only
     fall (as parser fixes / backfills retire overrides), never grow. This
     ratchet freezes that — adding a new sub-gate field fails CI, forcing a
     deliberate parser fix instead of another quiet relaxation.

[27] QC-039 re-derives its gate decision by hand from critical_failing /
     overall_rate instead of reading compute_accuracy()['pass']. Pin the
     contract pass == (overall_rate >= threshold AND no critical field failing)
     so the library's canonical definition and the pipeline's enforced one
     cannot silently diverge.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from hilmar import parser_accuracy as pa  # noqa: E402

# ── [28] shrink-only ratchet on the gate relaxation ──────────────────────

# The number of fields whose per-field threshold sits BELOW the 0.95 gate, as
# of 2026-06-26. This is a RATCHET: it may be LOWERED when a parser fix /
# backfill lets a field meet 0.95, but it may NEVER be raised. A new sub-gate
# override is a quiet gate relaxation (fireable per CLAUDE.md rule #3) — fix the
# parser instead.
MAX_FIELDS_BELOW_GATE = 7


def _fields_below_gate():
    return {f: t for f, t in pa.PER_FIELD_THRESHOLDS.items()
            if t < pa.ACCURACY_THRESHOLD}


def test_below_gate_override_count_is_shrink_only():
    below = _fields_below_gate()
    assert len(below) <= MAX_FIELDS_BELOW_GATE, (
        f"PER_FIELD_THRESHOLDS now relaxes {len(below)} fields below the "
        f"{pa.ACCURACY_THRESHOLD:.0%} gate ({sorted(below)}), up from the "
        f"{MAX_FIELDS_BELOW_GATE} ceiling. This is a one-way ratchet — do NOT "
        f"add a new below-gate override to make a red check pass (CLAUDE.md "
        f"rule #3). Fix the parser / backfill the field instead. If a field "
        f"was legitimately RETIRED to 0.95, LOWER this ceiling to match."
    )


def test_no_override_is_absurdly_low():
    """A floor on the floor: even the relaxed fields must stay defensible.
    Nothing should drop below 0.75 without a hard conversation."""
    for field, thr in pa.PER_FIELD_THRESHOLDS.items():
        assert thr >= 0.75, (
            f"{field} per-field threshold {thr} is below 0.75 — that is not a "
            f"gate anymore. Fix the parser, don't floor it out."
        )


def test_overrides_never_exceed_the_gate():
    """A per-field override above the gate would be a silent TIGHTENING that the
    overall gate doesn't reflect — overrides exist to relax, not to raise."""
    for field, thr in pa.PER_FIELD_THRESHOLDS.items():
        assert thr <= pa.ACCURACY_THRESHOLD, (
            f"{field} override {thr} exceeds the {pa.ACCURACY_THRESHOLD} gate"
        )


# ── [27] pin the pass contract so QC-039 can't drift from the library ─────

def _pass_should_be(res):
    return res["overall_rate"] >= res["threshold"] and not res["critical_failing"]


def test_pass_equals_overall_and_no_critical_failing():
    cases = [
        # all-populated WIN → pass
        [{"status": "WIN", "quoted": True, "mdolx_ref": "MDOLX1",
          "lane": "Oakland-Tokyo", "container_count": 2, "teu_requested": 4,
          "carrier_quoted": "CMA", "carrier_won": "CMA", "ol_rate": 2000,
          "origin": "Oakland", "destination": "Tokyo", "etd_offered": "2026-06-01"}],
        # critical field missing → fail
        [{"status": "WIN", "quoted": True, "lane": "Oakland-Tokyo",
          "container_count": 2, "teu_requested": 4, "ol_rate": 2000}],
        # empty → vacuously pass
        [],
    ]
    for reqs in cases:
        res = pa.compute_accuracy(reqs)
        assert res["pass"] == _pass_should_be(res), (
            f"compute_accuracy['pass'] diverged from "
            f"(overall>=threshold AND no critical failing) for {reqs!r}: {res}"
        )
