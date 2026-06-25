#!/usr/bin/env python3
"""
reprocess_bodies.py - Re-run html_to_text + _parse_all on every cached body in
stage_emails_bodies.txt, writing the refreshed text_body / parsed back.

WHY THIS RUNS EVERY FIRE (2026-06-24): the daily fetch (refresh_stage) parses
each email ONCE, at fetch time, and caches the result. ingest then consumes
that CACHE. So a body_parser fix only reaches NEWLY-fetched mail — the
back-catalog already in the window stays stale until its cache is refreshed.
That is the "break in data flow" behind the manual re-ingest: upstream data
(the raw stored body) is fine, but the downstream representation (parsed
fields -> report) is stale. reprocess() re-derives downstream from upstream so
any parser improvement self-applies to the whole window. Wired as a pipeline
step BEFORE ingest, and verified by QC-059 (data-flow integrity).

Idempotent and ATOMIC (temp file + os.replace) — safe to run on every fire.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import body_parser as BP  # noqa: E402
import fetch_bodies as FB  # noqa: E402


# 2026-05-19: stage files renamed .jsonl → .txt 2026-05-06 (so SharePoint
# indexes them). Resolve to the .txt file when present, fall back to the
# legacy .jsonl for back-compat with older boxes.
def _resolve(p: Path) -> Path:
    txt = p.with_suffix(".txt")
    legacy = p.with_suffix(".jsonl")
    return txt if txt.exists() or not legacy.exists() else legacy
BODIES = _resolve(ROOT / "scripts" / "stage_emails_bodies")


def _material(parsed: dict) -> tuple:
    """The downstream-facing projection of a parse. Two parses are
    'equivalent' for data-flow purposes iff these match — so a difference here
    is a real break (a field the current parser fills that the cache missed,
    or a changed value). Keep in sync with what ingest/the report consume."""
    p = parsed or {}
    rt = p.get("rate_table") or {}
    return (
        p.get("destination"), p.get("origin"),
        rt.get("carrier_quoted"), rt.get("ol_rate"), rt.get("pol"), rt.get("pod"),
        p.get("vessel_voyage"), p.get("transshipment"),
        p.get("ol_responder_signer"), p.get("etd_offered"), p.get("eta_offered"),
    )


def reprocess(*, write: bool = True) -> dict:
    """Re-parse every cached body and report what changed vs the stored parse.

    write=False  → DETECT ONLY (QC-059 dry-run): nothing is written.
    write=True   → backfill the refreshed parse ATOMICALLY.

    Returns a stats dict: {present, total, changed, delta_carrier, delta_rate,
    delta_dest, delta_signer, delta_vessel, wrote}. `present` is False when no
    bodies file exists (ephemeral runner / pre-fetch) — callers skip cleanly.
    """
    stats = {"present": False, "total": 0, "changed": 0, "delta_carrier": 0,
             "delta_rate": 0, "delta_dest": 0, "delta_signer": 0,
             "delta_vessel": 0, "wrote": False}
    if not BODIES.exists():
        return stats
    stats["present"] = True
    rows = []
    for line in BODIES.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        rec = json.loads(line)
        old = rec.get("parsed") or {}
        text = BP.html_to_text(rec.get("html_body") or "")
        new = FB._parse_all(text, rec.get("subject", ""), rec.get("bucket", ""),
                            sent_ts=rec.get("sent_ts"))
        old_rt = old.get("rate_table") or {}
        new_rt = new.get("rate_table") or {}
        if not old_rt.get("carrier_quoted") and new_rt.get("carrier_quoted"):
            stats["delta_carrier"] += 1
        if not old_rt.get("ol_rate") and new_rt.get("ol_rate"):
            stats["delta_rate"] += 1
        if not old.get("destination") and new.get("destination"):
            stats["delta_dest"] += 1
        if not old.get("ol_responder_signer") and new.get("ol_responder_signer"):
            stats["delta_signer"] += 1
        if not old.get("vessel_voyage") and new.get("vessel_voyage"):
            stats["delta_vessel"] += 1
        if _material(old) != _material(new):
            stats["changed"] += 1
        rec["text_body"] = text
        rec["parsed"] = new
        rows.append(rec)
    stats["total"] = len(rows)
    if write and rows:
        tmp = BODIES.with_suffix(BODIES.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        os.replace(tmp, BODIES)   # atomic — never leaves a half-written cache
        stats["wrote"] = True
    return stats


def main() -> int:
    dry = "--dry" in sys.argv or "--check" in sys.argv
    stats = reprocess(write=not dry)
    if not stats["present"]:
        print(f"ERR: {BODIES} not found", file=sys.stderr)
        return 1
    verb = "Would refresh" if dry else "Reprocessed"
    print(f"{verb} {stats['total']} bodies "
          f"({stats['changed']} changed){' [dry-run]' if dry else ''}")
    print(f"  Newly populated rate.carrier: +{stats['delta_carrier']}")
    print(f"  Newly populated rate.rate:    +{stats['delta_rate']}")
    print(f"  Newly populated destination:  +{stats['delta_dest']}")
    print(f"  Newly populated signer:       +{stats['delta_signer']}")
    print(f"  Newly populated vessel:       +{stats['delta_vessel']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
