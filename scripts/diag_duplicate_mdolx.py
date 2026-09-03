"""diag_duplicate_mdolx.py — QC-069's duplicate_mdolx findings, mechanism by
mechanism, not guessed at.

QC-069 (`qc_selfheal.qc069_duplicate_shipment_rows`) is DETECT-ONLY on
purpose: "collapsing rows is destructive and the correct survivor depends on
which row carries the real request thread." Any heal that acts on its
findings without knowing WHY a ref landed on two rows risks deleting the
right row and keeping the wrong one — the exact failure this repo has
already shipped once (QC-083's re-ask absorb, and before that the phantom
100% win rate).

There are at least FOUR distinct ways one MDOLX ref ends up on two rows,
and each implies a different fix or none at all. (4) was added 2026-09-03
after the first run of this script named it; it accounts for the majority of
the live findings and none of (1)-(3) do:

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

  (4) OPERATOR CORRECTION vs LIVE MATCHER, disagreeing about which row.
      MEASURED 2026-09-03; this is the one that is actually firing.

      Eight corrections back-entered the MDOLX261026-261046 batch from OL's
      Aug-12 recap, each note reading "The confirmation never reached [this
      mailbox]". That was true when they were written and is not true now:
      the confirmations arrived 2026-08-13 20:04-20:21, one day AFTER the
      recap, and every fire since has staged them. So each fire does BOTH:

        link_bookings_to_requests  → writes mdolx_ref + mdolx_refs_all on
                                     the row IT scored best
        apply_operator_corrections → `row.update(changes)` overwrites
                                     mdolx_ref on the row the OPERATOR named

      They do not name the same row. And `row.update(changes)` touches
      mdolx_ref ONLY — the matcher's assignment survives untouched in the
      OTHER row's `mdolx_refs_all`, which QC-069 also scans. Nothing
      un-stamps it: CLAUDE.md's standing corollary, in one field.

      The fingerprint is exact and mechanical, so this script does not
      guess it — for each duplicated ref it asks whether some correction
      NAMES that ref, and whether the ref reaches each row as `mdolx_ref`
      or only via `mdolx_refs_all`. A correction-named ref sitting in one
      row's mdolx_ref and another row's mdolx_refs_all is case (4).

      The operator's row is the right one by the repo's own contract
      (operator_corrections.json is the single durable human override, and
      its source here is OL's system of record). The stale half is the
      matcher's, and it is what a heal should clear.

This prints, per duplicated ref: every row's request_id, status,
mdolx_ref/mdolx_refs_all, preserved_from_prior, and any status_history line
mentioning "Prior-build WIN restored" or "Absorbed" — the fingerprint that
tells (1) apart from (2) and (3).

READ-ONLY. Pulls state into a temp dir, prints. No writes to the blob, no
mutation of the working tree, no email. Prints request_id, status, dates,
lane, conversation_id, preserved_from_prior, and MDOLX refs — no subjects,
no bodies, no addresses. It also reads the COMMITTED
scripts/operator_corrections.json and prints each matching correction's
`source` field (e.g. "ol-booking-recap-2026-08-12"), which is a provenance
label already in the repository, not mailbox content.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import core as C  # noqa: E402  (path is set immediately above)


def _norm(ref) -> str:
    """One spelling for a ref, however it was stored.

    Refs reach rows as '261029', 'MDOLX261029' and '0261029' depending on
    which writer put them there, and a classifier that compares raw strings
    reports case (4) as case (3). qc069 already upper-cases; this also strips
    the prefix and leading zeros so the correction's spelling and the
    matcher's cannot disagree on identity alone.
    """
    t = str(ref or "").strip().upper()
    if t.startswith("MDOLX"):
        t = t[5:].strip()
    return t.lstrip("0") or t


def _corrections_by_ref() -> dict[str, list[dict]]:
    """{normalised mdolx_ref: [correction, ...]} from the COMMITTED file.

    Read from the repo, never from the pulled state — operator_corrections
    .json is source, not data, and the point of case (4) is what the file
    SAYS versus what the fire produced.
    """
    path = ROOT / "scripts" / "operator_corrections.json"
    out: dict[str, list[dict]] = {}
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"WARN: operator_corrections.json unreadable ({e}) — "
              "case (4) cannot be told apart from (1)-(3) this run")
        return out
    for c in doc.get("corrections", []):
        ref = _norm((c.get("set") or {}).get("mdolx_ref"))
        if ref:
            out.setdefault(ref, []).append(c)
    return out


def _classify(ref: str, rows_for_ref: list[dict], corrections: list[dict]) -> str:
    """Name the mechanism behind ONE duplicated ref, from the row fields alone.

    Deliberately conservative: every branch demands a fingerprint only that
    mechanism leaves, and anything else comes back UNCLASSIFIED rather than
    being forced into the nearest bucket. A heal is written against what this
    prints, so a wrong label here is worse than no label.
    """
    if not rows_for_ref:
        return "NO ROWS (stale finding)"

    primary = [r for r in rows_for_ref if _norm(r.get("mdolx_ref")) == ref]
    secondary = [r for r in rows_for_ref
                 if ref in {_norm(x) for x in (r.get("mdolx_refs_all") or [])}
                 and _norm(r.get("mdolx_ref")) != ref]

    # (4) A correction NAMES this ref and it is the mdolx_ref on exactly the
    #     row the correction names. The collision then takes one of two
    #     shapes, and they are SPLIT because they need different heals — a
    #     single "(4)" label would hide that half of them cannot be fixed by
    #     clearing a field.
    named = {c.get("request_id") for c in corrections}
    owned = [r for r in primary if r.get("request_id") in named]
    if owned:
        # core.has_no_rfq_chain, not a bare stand_ test. Both stand_ (the
        # matcher's own unmatched-booking row) and ol_ (the 49 rows backfilled
        # from OL's export) are rows with no RFQ thread behind them, and
        # either can hold a ref a correction also claims. A bare stand_ check
        # here would report the ol_ shape as UNCLASSIFIED — the exact miss
        # tests/test_no_rfq_chain_predicate.py exists to stop repeating.
        stands = [r for r in rows_for_ref if C.has_no_rfq_chain(r)]
        if secondary:
            # 4a: the stale copy is a LIST ENTRY on another row. Clearing it
            # removes no row and no evidence — the correction's row keeps the
            # ref, the other row keeps its own.
            return ("(4a) OPERATOR CORRECTION vs MATCHER, stale list entry — "
                    f"correction owns {owned[0].get('request_id')}; stale on "
                    + ", ".join(f"{r.get('request_id')}.mdolx_refs_all"
                                for r in secondary))
        if stands:
            # 4b: the matcher could not place the booking and emitted a whole
            # standalone WIN row for it. Both rows claim the ref outright, so
            # the shipment is counted twice. Resolving it means REMOVING a
            # win row, which is destructive and is not this script's call.
            return ("(4b) OPERATOR CORRECTION vs MATCHER, orphan standalone — "
                    f"correction owns {owned[0].get('request_id')}; "
                    + ", ".join(r.get("request_id") for r in stands)
                    + " is the same shipment counted again")

    if any(r.get("preserved_from_prior") for r in rows_for_ref):
        return "(1) CARRY-FORWARD, STALE — a row is preserved_from_prior"

    if any(C.has_no_rfq_chain(r) for r in rows_for_ref):
        return ("(2) STANDALONE, NEVER LINKED — a row with no RFQ chain "
                "(stand_/ol_) is present")

    if len(primary) > 1:
        return "(3) TWO ROWS BOTH CLAIM IT AS mdolx_ref — genuinely ambiguous"

    return "UNCLASSIFIED — none of (1)-(4) left its fingerprint"


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

    corrections = _corrections_by_ref()
    print(f"operator corrections naming an mdolx_ref: {len(corrections)}\n")

    by_id = {r.get("request_id"): r for r in rows}
    tally: dict[str, int] = {}
    for _kind, ref, ids in dup_refs:
        nref = _norm(ref)
        present = [by_id[rid] for rid in ids if by_id.get(rid) is not None]
        verdict = _classify(nref, present, corrections.get(nref) or [])
        tally[verdict.split(" —")[0]] = tally.get(verdict.split(" —")[0], 0) + 1

        print(f"=== MDOLX{ref} — {len(ids)} row(s): {', '.join(ids)} ===")
        print(f"  MECHANISM: {verdict}")
        for c in corrections.get(nref) or []:
            print(f"  CORRECTION: {c.get('request_id')} "
                  f"source={c.get('source')!r}")
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

    # The whole point: how many of the real findings each mechanism explains.
    # A heal is scoped from this line, not from the one pair someone read.
    print("── mechanism tally ─────────────────────────────────────────────")
    for k, n in sorted(tally.items(), key=lambda kv: -kv[1]):
        print(f"  {n:>3}  {k}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
