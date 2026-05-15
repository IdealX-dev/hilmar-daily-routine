#!/usr/bin/env python3
"""
upsert_one.py - CLI wrapper around fetch_bodies.upsert_body for the Phase 2
body-fetch loop. Reads HTML body from stdin so agent invocations don't have
to escape arbitrary HTML on the command line.

Usage:
  cat body.html | python scripts/upsert_one.py \
      --imid SJ0... --bucket lonny_outbound --uri "mail:///messages/..." \
      --subject "Oakland to Manila" \
      [--conv AAQk... --sender X@Y --sent 2026-04-30T... --received 2026-04-30T...]

Exit 0 = stored. Exit non-zero = stored nothing.
"""
from __future__ import annotations
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

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
    args = ap.parse_args()

    html = sys.stdin.read()
    if not html.strip():
        print(f"ERR {args.imid}: empty stdin", file=sys.stderr)
        return 2

    rec = FB.upsert_body(
        imid=args.imid,
        bucket=args.bucket,
        uri=args.uri,
        subject=args.subject,
        html_body=html,
        conversation_id=args.conv,
        sender_email=args.sender,
        sent_ts=args.sent,
        received_ts=args.received,
    )
    print(f"OK {args.imid} bucket={args.bucket} text_len={len(rec['text_body'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
