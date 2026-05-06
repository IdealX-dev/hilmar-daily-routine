#!/usr/bin/env python3
"""
batch_upsert_fetched.py - Take a directory of MCP-fetched JSON responses
(scripts/_fetched_apr30/*.json) and upsert each into stage_emails_bodies.jsonl.

Each JSON file is the raw MCP read_resource response. Filename pattern:
  <safe_imid>.json  where safe_imid is imid with [<>@.] -> '_'.

We cross-reference scripts/_missing.json (the bucket+uri+imid catalog) so we
know which bucket each fetch belongs to.

Idempotent — safe to re-run.
"""
from __future__ import annotations
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import fetch_bodies as FB  # noqa: E402

FETCHED = ROOT / "scripts" / "_fetched_apr30"
MISSING = ROOT / "scripts" / "_missing.json"


def safe(imid: str) -> str:
    s = imid
    for c in "<>@.,/":
        s = s.replace(c, "_")
    return s


def main() -> int:
    if not MISSING.exists():
        print(f"ERR: {MISSING} not found", file=sys.stderr)
        return 1
    catalog = json.loads(MISSING.read_text(encoding="utf-8"))
    by_safe = {safe(r["imid"]): r for r in catalog}

    files = sorted(FETCHED.glob("*.json"))
    print(f"Found {len(files)} fetched files to upsert")

    n_ok = 0
    n_skip = 0
    n_err = 0
    for fp in files:
        try:
            mcp = json.loads(fp.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"  ERR  {fp.name}: unreadable JSON: {e}", file=sys.stderr)
            n_err += 1
            continue

        # Find the catalog entry by file stem (safe imid)
        cat = by_safe.get(fp.stem)
        if not cat:
            # Try alternate: match by internetMessageId
            iim = (mcp.get("internetMessageId") or "").strip("<>")
            for c in catalog:
                if c["imid"].strip("<>") == iim:
                    cat = c
                    break
        if not cat:
            print(f"  SKIP {fp.name}: not in _missing catalog", file=sys.stderr)
            n_skip += 1
            continue

        body = mcp.get("body") or {}
        html = body.get("content") or mcp.get("bodyPreview") or ""
        sender = (mcp.get("sender") or {}).get("address")

        FB.upsert_body(
            imid=cat["imid"],
            bucket=cat["bucket"],
            uri=cat["uri"],
            subject=mcp.get("subject") or cat.get("subject", ""),
            html_body=html,
            conversation_id=mcp.get("conversationId"),
            sender_email=sender,
            sent_ts=mcp.get("sentDateTime"),
            received_ts=mcp.get("receivedDateTime"),
        )
        n_ok += 1
        print(f"  OK   {cat['bucket']:18s} {cat['imid'][:50]} text_len={len(html)}")

    print()
    print(f"Upserted: {n_ok}  Skipped: {n_skip}  Errors: {n_err}")
    s = FB.status()
    print(f"\nPost-upsert: {s['total_fetched']}/{s['total_staged']}")
    for b, n in sorted(s["by_bucket"].items()):
        pct = 100.0 * n["fetched"] / n["staged"] if n["staged"] else 0.0
        print(f"  {b:22s}  {n['fetched']:3d}/{n['staged']:3d}  ({pct:5.1f}%)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
