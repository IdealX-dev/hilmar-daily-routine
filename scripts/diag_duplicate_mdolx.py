"""diag_duplicate_mdolx.py — QC-069's duplicate_mdolx findings, mechanism by
mechanism, not guessed at.

QC-069 (`qc_selfheal.qc069_duplicate_shipment_rows`) is DETECT-ONLY on
purpose: "collapsing rows is destructive and the correct survivor depends on
which row carries the real request thread." Any heal that acts on its
findings without knowing WHY a ref landed on two rows risks deleting the
right row and keeping the wrong one — the exact failure this repo has
already shipped once (QC-083's re-ask absorb, and before that the phantom
100% win rate).

There are at least three distinct ways one MDOLX ref ends up on two rows,
and each implies a different fix or none at all:

  (1) CARRY-FORWARD, STALE. ingest._merge_prior_win_into unions
      `mdolx_refs_all` from a PRIOR fire's persisted WIN into a row this
      fire's fresh rebuild could not reproduce as a WIN — stamping a
      `status_history` entry "Prior-build WIN restored (MDOLXnnnnn)". If a
      LATER fire's fresh `link_bookings_to_requests` correctly matches the
      same ref to a DIFFERENT row, nothing ever un-stamps the first row's
      copy: rule 3's corollary in CLAUDE.md, "nothing un-stamps a bad
      value." This is the case a heal could plausibly resolve — the row
      with LIVE evidence this fire is knowable.

  (2) STANDALONE, NEVER LINKED. link_bookings_to_requests could not match
      the booking to any request row (the RFQ says one lane spelling, OL's
      confirmation another) and emitted a `stand_<mdolx>` WIN beside the
      still-open request row. QC-069's own docstring names this shape
      explicitly. No heal without the request thread — this needs the lane
      match, not a duplicate-ref rule.

  (3) TWO REAL BOOKINGS, in which case QC-069 should not be firing at all
      (distinct MDOLX refs are distinct shipments) — but two DIFFERENT
      confirmation emails carrying the SAME ref (a forward, a resend) could
      independently match two rows in ONE fire. Genuinely ambiguous; no
      heal should touch it.

This prints, per duplicated ref: every row's request_id, status,
mdolx_ref/mdolx_refs_all, preserved_from_prior, and any status_history line
mentioning "Prior-build WIN restored" or "Absorbed" — the fingerprint that
tells (1) apart from (2) and (3).

READ-ONLY. Pulls state into a temp dir, prints. No writes to the blob, no
mutation of the working tree, no email. Prints request_id, status, dates,
lane, conversation_id, preserved_from_prior, and MDOLX refs — no subjects,
no bodies, no addresses.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))


def _row_detail(r: dict) -> str:
    lines = [
        f"  {r.get('request_id')}  status={r.get('status')} "
        f"request_date={r.get('request_date')} "
        f"preserved_from_prior={r.get('preserved_from_prior', False)}",
        f"      lane={r.get('lane')!r}  conversation_id={r.get('conversation_id')}",
        f"      mdolx_ref={r.get('mdolx_ref')!r}  "
        f"mdolx_refs_all={r.get('mdolx_refs_all')!r}",
    ]
    hist = r.get("status_history") or []
    fingerprints = [h for h in hist if isinstance(h, dict) and
                    ("Prior-build WIN restored" in str(h.get("reason") or h.get("note") or h)
                     or "Absorbed" in str(h.get("reason") or h.get("note") or h))]
    if not fingerprints:
        # status_history entries are not always {reason:...} shaped everywhere
        # in this codebase; fall back to a raw string search per entry.
        fingerprints = [h for h in hist
                        if "Prior-build WIN restored" in str(h) or "Absorbed" in str(h)]
    for h in fingerprints:
        lines.append(f"      HISTORY: {h}")
    notes = r.get("merge_notes") or []
    for n in notes:
        lines.append(f"      MERGE_NOTE: {n}")
    return "\n".join(lines)


def main() -> int:
    import state_store

    tmp = Path(tempfile.mkdtemp())
    try:
        pulled = state_store.pull(root=tmp)
    except Exception as e:
        print(f"::error::diag_duplicate_mdolx: pull FAILED: {type(e).__name__}: {e}")
        return 2
    print(f"pulled {len(pulled)} file(s) into a temp dir: {', '.join(pulled)}")

    data_path = tmp / "tracking-data-v2.json"
    if not data_path.exists():
        print("::error::diag_duplicate_mdolx: tracking-data-v2.json not in the store")
        return 2
    data = json.loads(data_path.read_text(encoding="utf-8"))
    rows = data.get("requests", [])
    print(f"tracking-data-v2.json: {len(rows)} requests\n")

    import qc_selfheal as QC

    findings = QC.qc069_duplicate_shipment_rows(rows)
    dup_refs = [(kind, key, ids) for kind, key, ids in findings
                if kind == "duplicate_mdolx"]
    print(f"QC-069 duplicate_mdolx findings: {len(dup_refs)}\n")

    by_id = {r.get("request_id"): r for r in rows}
    for _kind, ref, ids in dup_refs:
        print(f"=== MDOLX{ref} — {len(ids)} row(s): {', '.join(ids)} ===")
        for rid in ids:
            r = by_id.get(rid)
            if r is None:
                print(f"  {rid}  >>> NOT FOUND in current rows (stale id?)")
                continue
            print(_row_detail(r))
        print()

    if not dup_refs:
        print("Nothing to trace — QC-069 found no duplicate_mdolx this run.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
