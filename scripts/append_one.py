#!/usr/bin/env python3
"""
append_one.py - Parallel-safe sibling of upsert_one.py.

Reads HTML body from stdin and APPENDS one record to the file specified
by --out (default: scripts/_shards/_default.jsonl). No load+modify+write,
no dedup against existing records — pure append. Use when multiple agents
write concurrently to disjoint imid slices; merge afterward with
merge_shards.py to get the final stage_emails_bodies.jsonl.

Usage:
  python scripts/upsert_one.py < body.html  # for serial (in-place upsert)
  python scripts/append_one.py --out scripts/_shards/a.jsonl < body.html
"""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import body_parser as BP  # noqa: E402
import fetch_bodies as FB  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--imid", required=True)
    ap.add_argument("--bucket", required=True)
    ap.add_argument("--uri", required=True)
    ap.add_argument("--subject", default="")
    ap.add_argument("--conv", default=None)
    ap.add_argument("--sender", default=None)
    ap.add_argument("--sent", default=None)
    ap.add_argument("--received", default=None)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    html = sys.stdin.read()
    if not html.strip():
        print(f"ERR {args.imid}: empty stdin", file=sys.stderr)
        return 2

    text = BP.html_to_text(html)
    parsed = FB._parse_all(text, args.subject, args.bucket)

    rec = {
        "imid": args.imid,
        "bucket": args.bucket,
        "uri": args.uri,
        "subject": args.subject,
        "conversation_id": args.conv,
        "sender_email": args.sender,
        "sent_ts": args.sent,
        "received_ts": args.received,
        "html_body": html,
        "text_body": text,
        "parsed": parsed,
        "fetched_at": FB._now_iso(),
    }

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    print(f"OK {args.imid} bucket={args.bucket} text_len={len(text)} -> {out_path.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
