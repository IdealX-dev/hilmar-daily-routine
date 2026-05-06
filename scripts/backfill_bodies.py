#!/usr/bin/env python3
"""
backfill_bodies.py - One-off Phase 2 backfill.

Reads existing on-disk body fetches from scripts/email_bodies/ and
scripts/new_bodies/ and pushes them into stage_emails_bodies.jsonl
via fetch_bodies.upsert_body().

Why: Phase 2 of HANDOFF-TO-CODE-2026-04-30.md requires body backfill on
~188 staged rows; ~83 are already on disk in two legacy formats. Pulling
those in first lets the MCP read_resource loop only fetch the actual gap.

Match keys (in priority order):
  1. internetMessageId stripped to bare form (drop <>, drop @host)
  2. Outlook resource id (only present in email_bodies/*)
"""
from __future__ import annotations
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import fetch_bodies as FB  # noqa: E402


def strip_imid(raw: str | None) -> str | None:
    if not raw:
        return None
    s = raw.strip()
    if s.startswith("<") and s.endswith(">"):
        s = s[1:-1]
    s = s.split("@", 1)[0]
    return s or None


def index_staged() -> tuple[dict[str, dict], dict[str, dict]]:
    by_imid: dict[str, dict] = {}
    by_id: dict[str, dict] = {}
    for r in FB.load_staged():
        imid = r.get("imid")
        if imid:
            by_imid[imid] = r
        rid = r.get("id")
        if rid:
            by_id[rid] = r
    return by_imid, by_id


def find_staged(file_data: dict, by_imid: dict, by_id: dict) -> dict | None:
    imid = strip_imid(file_data.get("internetMessageId"))
    if imid and imid in by_imid:
        return by_imid[imid]
    rid = file_data.get("id")
    if rid and rid in by_id:
        return by_id[rid]
    return None


def html_or_text(file_data: dict) -> str:
    """Return whatever body content is available, prefer HTML."""
    body = file_data.get("body")
    if isinstance(body, dict):
        ct = (body.get("contentType") or "").lower()
        content = body.get("content") or ""
        if ct == "html":
            return content
        if ct == "text":
            # Wrap as html-ish so html_to_text() returns it cleanly
            return content
        return content
    text = file_data.get("body_text")
    if text:
        return text
    return ""


def main():
    by_imid, by_id = index_staged()
    print(f"Staged rows: {len(by_imid)} by imid, {len(by_id)} by id")

    matched, skipped_no_match, skipped_no_body = 0, 0, 0
    bucket_counts: dict[str, int] = {}

    files = sorted((ROOT / "scripts" / "email_bodies").glob("*.json"))
    files += sorted((ROOT / "scripts" / "new_bodies").glob("*.json"))
    print(f"On-disk body files: {len(files)}")

    for fp in files:
        try:
            data = json.loads(fp.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"  WARN unreadable {fp.name}: {e}")
            continue

        body_blob = html_or_text(data)
        if not body_blob:
            skipped_no_body += 1
            continue

        staged = find_staged(data, by_imid, by_id)
        if not staged:
            skipped_no_match += 1
            continue

        bucket = staged.get("bucket")
        imid = staged.get("imid")
        if not imid or not bucket:
            skipped_no_match += 1
            continue

        sender = data.get("sender")
        if isinstance(sender, dict):
            sender_email = sender.get("address")
        elif isinstance(data.get("from"), dict):
            sender_email = data["from"].get("address") or data["from"].get("email")
        else:
            sender_email = None

        FB.upsert_body(
            imid=imid,
            bucket=bucket,
            uri=staged.get("uri", ""),
            subject=data.get("subject") or staged.get("subject", ""),
            html_body=body_blob,
            conversation_id=data.get("conversationId"),
            sender_email=sender_email,
            sent_ts=data.get("sentDateTime") or data.get("sent"),
            received_ts=data.get("receivedDateTime"),
        )
        matched += 1
        bucket_counts[bucket] = bucket_counts.get(bucket, 0) + 1

    print()
    print(f"Matched & upserted: {matched}")
    print(f"  by bucket: {bucket_counts}")
    print(f"Skipped (no match):  {skipped_no_match}")
    print(f"Skipped (no body):   {skipped_no_body}")

    s = FB.status()
    print()
    print(f"Post-backfill status:  {s['total_fetched']}/{s['total_staged']}")
    for b, n in sorted(s["by_bucket"].items()):
        pct = 100.0 * n["fetched"] / n["staged"] if n["staged"] else 0.0
        print(f"  {b:22s}  {n['fetched']:3d}/{n['staged']:3d}  ({pct:5.1f}%)")


if __name__ == "__main__":
    main()
