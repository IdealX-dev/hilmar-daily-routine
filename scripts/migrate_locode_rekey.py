#!/usr/bin/env python3
"""Re-key the operator corrections that the UN/LOCODE merge moves.

WHY THIS EXISTS. `core.request_id` hashes the destination
(scripts/core.py: sha1(conversationId | ts[:16] | destination.lower())). The
LOCODE merge renames a stored destination — "Jpyok" becomes "Yokohama" — so
every affected row is re-keyed on the next fire. `operator_corrections.json` is
matched BY request_id (ingest.apply_operator_corrections), and a key that
matches nothing only prints a WARN and continues. So a human verdict — the one
piece of durable state this system has, per CLAUDE.md "rebuild, don't merge" —
would be dropped in silence.

CLAUDE.md §3: schema/config changes are migrations — scripted, reversible,
logged. This is that script.

  --dry-run  (DEFAULT)  Report, write nothing. Exits non-zero with ::error::
                        when anything needs doing, so the workflow step goes
                        red instead of green-in-zero-seconds.
  --apply               Re-key scripts/operator_corrections.json in place,
                        recording each entry's prior key as
                        `superseded_request_id` (that is the reverse map).
  --revert              Undo an --apply using those recorded keys.

The file is version-controlled, so --apply lands as a reviewable diff and
`git revert` is the outer undo. --revert exists for the case where the diff has
already been committed and pushed and the fastest safe move is a scripted
inverse rather than a hand edit.

STORED ROWS ARE DELIBERATELY NOT REWRITTEN. Every fire rebuilds rows from the
staged mail (reprocess_bodies re-parses the cached bodies BEFORE ingest), so a
rebuilt row picks up the merged spelling on its own. The one class that is NOT
rebuilt is a carried-forward prior WIN, which ingest copies verbatim — and
those are handled by reading through `core.resolve_locode` in
`canonical_port_key` and `trade_region_for` rather than by stamping the file,
because a stamped value is one more thing nothing can un-stamp.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import core as C  # noqa: E402
import state_store  # noqa: E402
from ingest import title_case_destination  # noqa: E402

CORRECTIONS = ROOT / "scripts" / "operator_corrections.json"


def _err(msg: str) -> None:
    """A diagnostic that cannot fail loudly is worse than none (2026-08-20)."""
    print(f"::error::{msg}")


def merged_destination(dest) -> str | None:
    """What the NEXT fire will store for this row's destination.

    Runs the row's stored value back through the SAME normalizer the ingest
    path uses (ingest.title_case_destination, which now consults
    core.resolve_locode first), rather than re-deriving a rule here. Two
    spellings of the merge rule is how the last one shipped wrong.
    """
    if not dest or dest == "Unknown":
        return None
    merged = title_case_destination(dest)
    return merged if merged != dest else None


def plan(rows: list[dict], corrections: list[dict]) -> dict:
    """Everything the migration would do, computed and returned — no writes.

    Returns {"moves": [...], "affected": [...], "unverifiable": [...],
             "already_stale": [...]}.
    """
    moves, unverifiable = [], []
    for r in rows:
        old_dest = r.get("destination")
        new_dest = merged_destination(old_dest)
        if not new_dest:
            continue
        old_rid = r.get("request_id") or ""
        imid = (r.get("source_imids") or [None])[0]
        ts = r.get("request_timestamp")
        entry = {
            "request_id": old_rid,
            "old_destination": old_dest,
            "new_destination": new_dest,
            "mdolx_ref": r.get("mdolx_ref"),
            "status": r.get("status"),
        }
        # Only `req_*` ids hash the destination. `stand_<mdolx>` and `ol_<ref>`
        # are derived from the booking number and do not move — their rows
        # still get the new spelling, but their corrections stay valid.
        if not old_rid.startswith("req_"):
            entry["new_request_id"] = old_rid
            entry["note"] = "id not destination-derived — display only"
            moves.append(entry)
            continue
        # SELF-CONSISTENCY. Recompute the OLD id from the row's own inputs. If
        # it does not reproduce the stored id, this row's id was not derived
        # from these fields (a carried-forward row, or a destination a later
        # heal rewrote without re-keying) and the NEW id computed the same way
        # would be a guess. Refuse it; a human decides.
        if C.request_id(imid, ts, old_dest) != old_rid:
            entry["reason"] = "stored request_id is not reproducible from this row"
            unverifiable.append(entry)
            continue
        entry["new_request_id"] = C.request_id(imid, ts, new_dest)
        moves.append(entry)

    by_old = {m["request_id"]: m for m in moves if m.get("new_request_id")}
    live_ids = {r.get("request_id") for r in rows}
    affected, already_stale = [], []
    for corr in corrections:
        rid = corr.get("request_id")
        if rid in by_old and by_old[rid]["new_request_id"] != rid:
            affected.append({**by_old[rid], "note": corr.get("note", "")[:120]})
        elif rid not in live_ids and not corr.get("create") and not corr.get("exclude"):
            # PRE-EXISTING staleness, not caused by this migration. Reported
            # separately so the migration is never blamed for it — and so it
            # is finally visible at all (QC-082 is the standing detector).
            already_stale.append({"request_id": rid, "note": corr.get("note", "")[:120]})
    return {"moves": moves, "affected": affected,
            "unverifiable": unverifiable, "already_stale": already_stale}


def apply_rekey(doc: dict, affected: list[dict]) -> int:
    by_old = {a["request_id"]: a for a in affected}
    changed = 0
    for corr in doc.get("corrections", []):
        hit = by_old.get(corr.get("request_id"))
        if not hit:
            continue
        corr["superseded_request_id"] = corr["request_id"]
        corr["request_id"] = hit["new_request_id"]
        corr["superseded_reason"] = (
            f"UN/LOCODE merge {hit['old_destination']} -> "
            f"{hit['new_destination']} re-keyed this row (core.request_id "
            f"hashes the destination)"
        )
        changed += 1
    return changed


def revert_rekey(doc: dict) -> int:
    changed = 0
    for corr in doc.get("corrections", []):
        prior = corr.pop("superseded_request_id", None)
        if not prior:
            continue
        corr.pop("superseded_reason", None)
        corr["request_id"] = prior
        changed += 1
    return changed


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--apply", action="store_true", help="write the re-keyed corrections")
    g.add_argument("--revert", action="store_true", help="undo an --apply")
    ap.add_argument("--tracking", default=None,
                    help="tracking-data-v2.json to read (default: pull from blob)")
    args = ap.parse_args(argv)

    doc = json.loads(CORRECTIONS.read_text(encoding="utf-8"))
    corrections = doc.get("corrections", [])

    if args.revert:
        n = revert_rekey(doc)
        CORRECTIONS.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n",
                               encoding="utf-8")
        print(f"REVERTED {n} correction key(s) to their pre-migration ids")
        return 0

    if args.tracking:
        src = Path(args.tracking)
    else:
        # Nothing meaningful runs locally — the real rows live in blob.
        state_store.pull(root=ROOT)
        src = ROOT / "tracking-data-v2.json"
    if not src.exists():
        _err(f"no tracking data at {src} — cannot compute the plan")
        return 2
    rows = json.loads(src.read_text(encoding="utf-8")).get("requests", [])
    print(f"Read {len(rows)} rows from {src} and {len(corrections)} corrections")

    p = plan(rows, corrections)
    for m in p["moves"]:
        print(f"  ROW  {m['request_id']}  {m['old_destination']!r} -> "
              f"{m['new_destination']!r}  id -> {m.get('new_request_id', '?')}")
    for a in p["affected"]:
        print(f"  CORR {a['request_id']} -> {a['new_request_id']}  ({a['note']})")
    for u in p["unverifiable"]:
        _err(f"UNVERIFIABLE {u['request_id']} ({u['old_destination']!r}): {u['reason']}")
    for s in p["already_stale"]:
        _err(f"ALREADY STALE before this migration: {s['request_id']} ({s['note']})")

    print(f"\nrows renamed={len(p['moves'])} corrections to re-key="
          f"{len(p['affected'])} unverifiable={len(p['unverifiable'])} "
          f"pre-existing stale={len(p['already_stale'])}")

    if not args.apply:
        # Non-zero when there is work to do, so the workflow step is RED and
        # somebody looks at it. Green-and-silent is the failure mode this
        # project has already paid for.
        return 1 if (p["affected"] or p["unverifiable"] or p["already_stale"]) else 0

    if p["unverifiable"]:
        _err("refusing to --apply while any row's id is unreproducible — "
             "resolve those first")
        return 2
    n = apply_rekey(doc, p["affected"])
    CORRECTIONS.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n",
                           encoding="utf-8")
    print(f"APPLIED — re-keyed {n} correction(s); prior ids recorded in "
          f"superseded_request_id (that is the reverse map)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
