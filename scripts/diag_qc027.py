"""diag_qc027.py — is the Carrier percentage a broken parser, or a broken ruler?

2026-08-10, Michael on the daily Sentry page: "you have to fix this.. it used
to work.. don't know what you did."

QC-027's measurement ran ~1200 lines ahead of two heals that write the fields
it grades — QC-056 backfills carrier_quoted, QC-064 nulls garbage out of it —
so the reported number described a state that did not survive its own run.
That ordering defect is fixed and guarded by
tests/test_qc027_measures_final_state.py.

What a unit test CANNOT say is what the real number becomes. 87% was measured
on 329 live rows; whether the heals lift those particular rows over 90% is a
fact about the data, not about the code. This prints both readings on the real
dataset — the one the old code sent to Sentry, and the one the fixed code
sends — and then names every row still missing a carrier, with the reason it
is still missing.

The residual list is the part that matters. A row with an ol_rate and no
carrier is QC-056's target and should be near-zero; a row reachable only by
etd_offered or vessel_voyage is in QC-027's denominator but OUTSIDE QC-056's
(which only ever looks at rows with a rate), so no heal will ever touch it —
that gap is measured here rather than argued about.

WHAT THIS CANNOT TELL YOU — read before trusting the BEFORE column.

Run 1 (2026-08-10, 329 reachable rows) came back with BEFORE == AFTER on all
seven fields, and Carrier at 97.0%, not the 87% that was paged. That is not a
contradiction, it is the shape of the data: QC-056's backfills PERSIST to
tracking-data, so by the time any later run reads the stored state, the repairs
are already in it and there is nothing left to heal. The BEFORE column is
"what the old ruler would say about TODAY's rows" — it is NOT a reconstruction
of what shipped on the day of the alert, because those rows no longer exist in
that condition. Only a run that ingests fresh unhealed rows can show the two
columns diverge.

So use this to answer "where does the data stand NOW, and which rows are still
blank" — which it answers exactly. Do not use it to reconstruct a past page.

READS ONLY. Pulls state (see diag_day for why into the repo root) and runs the
QC in memory on a deep copy. No blob write, no send, no mutation of the stored
tracking data.

Usage
    python3 scripts/diag_qc027.py
    DIAG_LIST=40 python3 scripts/diag_qc027.py     # show more residual rows
"""
from __future__ import annotations

import contextlib
import copy
import io
import json
import os
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))


def _rule(title: str) -> None:
    print(f"\n{'─' * 4} {title} {'─' * max(0, 62 - len(title))}")


def _short(s, n: int) -> str:
    s = str(s or "").replace("\n", " ").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def measure(rows, QC) -> tuple[int, int, dict]:
    """QC-027's own arithmetic, through QC-027's own predicates.

    Returns (n_reachable, n_pdf_only, {field: (present, pct)}). Deliberately
    calls qc027_active_rows / qc027_is_reachable rather than re-typing the
    comprehensions — a diagnostic with its own idea of the denominator answers
    questions about a set nobody is measuring.
    """
    active = QC.qc027_active_rows(rows)
    reachable = [r for r in active if QC.qc027_is_reachable(r)]
    pdf_only = [r for r in active if not QC.qc027_is_reachable(r)]
    stats = {}
    for fld, label in QC.QC027_FIELDS:
        present = sum(1 for r in reachable if r.get(fld))
        pct = present * 100 / len(reachable) if reachable else 0.0
        stats[label] = (present, pct)
    return len(reachable), len(pdf_only), stats


def _verdict(pct: float) -> str:
    return "ERROR" if pct < 90 else "WARN " if pct < 95 else "ok   "


def main() -> int:
    _rule("state store")
    import state_store
    print(f"pulling state into {ROOT} (overwrites local copies)")
    try:
        pulled = state_store.pull(root=ROOT)
    except Exception as e:
        print(f"pull FAILED: {type(e).__name__}: {e}")
        return 2
    print(f"pulled {len(pulled)} file(s)")

    data_path = ROOT / "tracking-data-v2.json"
    if not data_path.exists():
        print("::error::tracking-data-v2.json not in the store")
        return 2
    stored = json.loads(data_path.read_text(encoding="utf-8"))
    rows = stored.get("requests") or []
    print(f"tracking-data: {len(rows)} requests")

    import qc_selfheal as QC

    # BEFORE — exactly what the old code sent to Sentry: the stored rows,
    # graded with no heal having run.
    n_reach_b, n_pdf_b, before = measure(rows, QC)

    # AFTER — the same phase the fire runs, on a deep copy so nothing here can
    # touch the stored state even in memory.
    _rule("running phase_5_summaries + phase_6_rules on a COPY")
    work = copy.deepcopy(stored)
    log = QC.Log()
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            QC.phase_5_summaries(log, work)
            QC.phase_6_rules(log, work)
    except Exception as e:
        print(f"phase run FAILED: {type(e).__name__}: {e}")
        return 2
    healed = work.get("requests") or []
    fixes = [f for f in log.fixes if "QC-056" in f or "QC-064" in f or "QC-027" in f]
    print(f"{len(log.fixes)} fix(es) applied; {len(fixes)} touch QC-027's fields")
    for f in fixes[:15]:
        print(f"    {_short(f, 108)}")
    if len(fixes) > 15:
        print(f"    … and {len(fixes) - 15} more")

    n_reach_a, n_pdf_a, after = measure(healed, QC)

    _rule("QC-027 completeness — as measured before vs after the heals")
    print(f"reachable rows: {n_reach_b} before / {n_reach_a} after   "
          f"(PDF-only excluded: {n_pdf_b} / {n_pdf_a})\n")
    print(f"  {'field':<14} {'BEFORE (old ruler)':>24}   {'AFTER (fixed ruler)':>22}")
    for _fld, label in QC.QC027_FIELDS:
        pb, pctb = before[label]
        pa, pcta = after[label]
        moved = "" if abs(pcta - pctb) < 0.05 else f"   {pcta - pctb:+.0f} pts"
        print(f"  {label:<14} {_verdict(pctb)} {pb:>4}/{n_reach_b} {pctb:>5.1f}%   "
              f"{_verdict(pcta)} {pa:>4}/{n_reach_a} {pcta:>5.1f}%{moved}")

    if all(abs(after[lbl][1] - before[lbl][1]) < 0.05 for _f, lbl in QC.QC027_FIELDS):
        print("\n  (BEFORE == AFTER: the stored state is ALREADY healed — QC-056's "
              "backfills persist, so a later run has nothing left to repair. This "
              "is today's truth, not a replay of the alert day. See the module "
              "docstring.)")

    still_error = [lbl for _f, lbl in QC.QC027_FIELDS if after[lbl][1] < 90]
    print()
    if still_error:
        print(f">>> STILL ERROR after the heals: {', '.join(still_error)} — the "
              "ordering was not the whole story; see the residual list below.")
    else:
        print(">>> No field below 90% once the heals have run. The ERROR page was "
              "the ruler, not the data.")

    # ── who is still missing a carrier, and why ────────────────────────────
    active = QC.qc027_active_rows(healed)
    reachable = [r for r in active if QC.qc027_is_reachable(r)]
    misses = [r for r in reachable if not r.get("carrier_quoted")]
    _rule(f"rows still missing carrier_quoted: {len(misses)}")

    # THE question behind "it used to work": are these new rows, or old ones?
    by_month = Counter(str(r.get("request_date") or "?")[:7] for r in misses)
    all_by_month = Counter(str(r.get("request_date") or "?")[:7] for r in reachable)
    print("  by request month (misses / reachable in that month):")
    for m in sorted(all_by_month):
        if by_month.get(m):
            print(f"    {m}   {by_month[m]:>3} / {all_by_month[m]:<4} "
                  f"({by_month[m] * 100 / all_by_month[m]:.0f}% of the month)")

    # Why each is unhealed. QC-056 only ever looks at rows WITH a rate, so a
    # row reachable by ETD or vessel alone is in the denominator of the check
    # and outside the reach of the heal.
    reasons = Counter()
    for r in misses:
        if r.get("ol_rate"):
            reasons["has a rate — QC-056's target, parser found no carrier"] += 1
        elif r.get("etd_offered") or r.get("vessel_voyage"):
            reasons["no rate — reachable by ETD/vessel, OUTSIDE QC-056's scope"] += 1
        else:
            reasons["unclassified"] += 1
    print("\n  why they are still blank:")
    for why, n in reasons.most_common():
        print(f"    {n:>4}  {why}")

    limit = int(os.environ.get("DIAG_LIST", "25") or 25)
    print(f"\n  first {min(limit, len(misses))}:")
    for r in misses[:limit]:
        sig = "+".join(k for k, v in (("etd", r.get("etd_offered")),
                                      ("vessel", r.get("vessel_voyage")),
                                      ("rate", r.get("ol_rate"))) if v)
        print(f"    {str(r.get('request_id')):<26} {str(r.get('status')):<8} "
              f"{str(r.get('request_date') or '?'):<11} via={sig:<18} "
              f"{_short(r.get('lane'), 32)}")
    if len(misses) > limit:
        print(f"    … and {len(misses) - limit} more (raise DIAG_LIST)")

    print("\nNOTHING WAS WRITTEN. The stored tracking data is untouched.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
