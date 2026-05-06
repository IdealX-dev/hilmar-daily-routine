"""refresh_stage.py — Fetch new Hilmar-relevant emails via Microsoft Graph
and append to stage_emails.jsonl + stage_emails_bodies.jsonl.

Replaces what the Cowork-side `outlook_email_search` MCP previously did. Now
the laptop's Task Scheduler can run end-to-end without Claude Code being open.

Auth: reuses the MSAL token cache that outlook_send.py already established
(secrets/token-cache.bin). Silent refresh only — never prompts device-code
from a non-interactive context.

Search strategy: TWO Graph $search queries to make sure we catch both sides
of the conversation:

  Q1 (rate-request flow):
    "from:lupfold@hilmaringredients.com" OR "to:lupfold@hilmaringredients.com"
  Q2 (booking-confirmation flow — closes the structural gap where MDOLX
      bookings go to carriers, with Lonny only on CC):
    "from:MBD_OceanExportBookingShared@ol-usa.com AND HILMAR"

We $search rather than $filter because Graph rejects toRecipients lambdas
combined with $filter clauses (production VM ate this in 2026-04-27 — see
hilmar-tracker/src/hilmar/graph_client.py:404-451 for the post-mortem).

Bucket classification (sender + subject heuristics, from empirical
inspection of the 188-record corpus produced by the Cowork MCP):

  sender = lupfold@hilmaringredients.com:
    subject ^(Re|Fw|Fwd):  → lonny_reply
    else                   → lonny_outbound

  sender = MBD_OceanExportBookingShared@ol-usa.com:
    subject ^Re:\\s*Oakland to  → mbd_rate_response
    else                        → mbd_inbound

  any other sender → drop (excluded mailboxes, off-topic, etc.)

Output is append-only — dedupe key is the Graph message `id` for stage_emails
and `imid` (internetMessageId) for stage_emails_bodies. Existing records are
never rewritten by this tool; ingest.py's idempotency depends on stable ids.

Usage:
  python scripts/refresh_stage.py                    # full run, last 14 days
  python scripts/refresh_stage.py --days-back 30
  python scripts/refresh_stage.py --since 2026-04-01
  python scripts/refresh_stage.py --dry              # show what'd be added, don't write
  python scripts/refresh_stage.py --verbose          # per-message classification trace
  python scripts/refresh_stage.py --no-bodies        # stage only, skip body fetch (debug)
"""
from __future__ import annotations
import argparse
import json
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import msal
import requests

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import outlook_send as OS  # noqa: E402  reuse auth + token cache
import fetch_bodies as FB  # noqa: E402  reuse upsert_body / _parse_all

GRAPH = "https://graph.microsoft.com/v1.0"
STAGE_PATH = SCRIPTS / "stage_emails.jsonl"
BODIES_PATH = SCRIPTS / "stage_emails_bodies.jsonl"

LONNY_EMAIL = "lupfold@hilmaringredients.com"
MBD_BOOKING_EMAIL = "MBD_OceanExportBookingShared@ol-usa.com"

# Drops — never stage from these senders even if they appear in the search
EXCLUDED_SENDERS = {
    "MBD_Export_Pricing@ol-usa.com",
    "caren.tobel@ol-usa.com",
    # michael.deitchman@idealx.us is also excluded per ingest_scope.mailboxes_excluded,
    # but those won't appear here because we auth as @ol-usa.com — different mailbox.
}

REPLY_PREFIX = re.compile(r"^\s*(re|fw|fwd)\s*:", re.I)
RATE_RESPONSE_SUBJECT = re.compile(r"^\s*re\s*:\s*oakland\s+to\s+", re.I)


# ─────────────────────────────────────────────────────────────────────
# Auth — reuse outlook_send's token cache
# ─────────────────────────────────────────────────────────────────────

def get_token_silent() -> str:
    """Acquire a Graph token via silent refresh. Bail if cache is missing/expired."""
    cache = OS._load_cache()
    app = msal.PublicClientApplication(
        OS.CLIENT_ID,
        authority=f"https://login.microsoftonline.com/{OS.TENANT}",
        token_cache=cache,
    )
    accounts = app.get_accounts()
    if not accounts:
        raise SystemExit(
            "ERROR: no cached MSAL account. Run `python scripts/outlook_send.py auth` "
            "interactively once to seed the token cache."
        )
    result = app.acquire_token_silent(OS.SCOPES, account=accounts[0])
    if not result or "access_token" not in result:
        raise SystemExit(
            "ERROR: silent token refresh failed. Refresh token may have expired "
            "(>90 days idle). Run `python scripts/outlook_send.py auth` to re-authenticate."
        )
    OS._save_cache(cache)
    return result["access_token"]


# ─────────────────────────────────────────────────────────────────────
# HTTP helpers
# ─────────────────────────────────────────────────────────────────────

RETRYABLE_STATUS = {429, 503, 504}
RETRY_BACKOFFS_S = (2.0, 4.0, 8.0)


def graph_get(token: str, url: str, params: dict | None = None) -> dict:
    """GET with retry on 429/503/504."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }
    for attempt in range(len(RETRY_BACKOFFS_S) + 1):
        r = requests.get(url, params=params, headers=headers, timeout=60)
        if r.status_code in RETRYABLE_STATUS and attempt < len(RETRY_BACKOFFS_S):
            wait = RETRY_BACKOFFS_S[attempt]
            ra = r.headers.get("Retry-After")
            if ra:
                try:
                    wait = float(ra)
                except ValueError:
                    pass
            time.sleep(wait)
            continue
        if not (200 <= r.status_code < 300):
            raise RuntimeError(f"Graph GET {url} -> {r.status_code}: {r.text[:500]}")
        return r.json()
    raise RuntimeError("unreachable")


# ─────────────────────────────────────────────────────────────────────
# Search
# ─────────────────────────────────────────────────────────────────────

GRAPH_SELECT = (
    "id,conversationId,subject,from,toRecipients,ccRecipients,"
    "bodyPreview,receivedDateTime,sentDateTime,internetMessageId,isRead,hasAttachments"
)


def search_messages(token: str, kql: str, max_results: int = 250) -> list[dict]:
    """Run a $search query against /me/messages, paginate through nextLink."""
    url: str | None = f"{GRAPH}/me/messages"
    # Graph requires the $search value to be a quoted string.
    params: dict | None = {
        "$top": "50",
        "$search": f'"{kql}"',
        "$select": GRAPH_SELECT,
    }
    out: list[dict] = []
    while url and len(out) < max_results:
        data = graph_get(token, url, params=params)
        for item in data.get("value", []):
            out.append(item)
            if len(out) >= max_results:
                break
        url = data.get("@odata.nextLink")
        params = None  # nextLink already carries the query string
    return out


def get_message_body(token: str, message_id: str) -> dict:
    """Fetch a single message including body."""
    url = f"{GRAPH}/me/messages/{message_id}"
    params = {
        "$select": (
            "id,conversationId,subject,from,toRecipients,ccRecipients,"
            "body,bodyPreview,receivedDateTime,sentDateTime,internetMessageId"
        ),
    }
    return graph_get(token, url, params=params)


# ─────────────────────────────────────────────────────────────────────
# Classification
# ─────────────────────────────────────────────────────────────────────

def classify(item: dict) -> str | None:
    """Return bucket name or None if message should be dropped."""
    sender = ((item.get("from") or {}).get("emailAddress") or {}).get("address") or ""
    sender_l = sender.lower()
    subject = item.get("subject") or ""

    if sender_l in {s.lower() for s in EXCLUDED_SENDERS}:
        return None

    if sender_l == LONNY_EMAIL.lower():
        if REPLY_PREFIX.match(subject):
            return "lonny_reply"
        return "lonny_outbound"

    if sender_l == MBD_BOOKING_EMAIL.lower():
        if RATE_RESPONSE_SUBJECT.match(subject):
            return "mbd_rate_response"
        return "mbd_inbound"

    return None  # any other sender → drop


# ─────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────

def parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    s = ts.rstrip("Z")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def load_existing_stage_keys() -> tuple[set[str], set[str]]:
    """Return (existing_ids, existing_imids) so dedup catches both keys.

    Cowork's MCP wrote `id` (Graph message id) — a folder-scoped opaque
    string. When we re-fetch the same email from a different folder, Graph
    gives it a different id but the same internetMessageId. Index both so
    refresh_stage doesn't double-stage emails that already exist under their
    other-folder id.
    """
    if not STAGE_PATH.exists():
        return set(), set()
    ids: set[str] = set()
    imids: set[str] = set()
    with open(STAGE_PATH, encoding="utf-8") as f:
        for line in f:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("id"):
                ids.add(rec["id"])
            if rec.get("imid"):
                imids.add(rec["imid"])
    return ids, imids


def load_existing_body_imids() -> set[str]:
    if not BODIES_PATH.exists():
        return set()
    out = set()
    with open(BODIES_PATH, encoding="utf-8") as f:
        for line in f:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("imid"):
                out.add(rec["imid"])
    return out


def append_stage_record(rec: dict) -> None:
    with open(STAGE_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def build_stage_record(item: dict, bucket: str) -> dict:
    """Same shape Cowork's MCP wrote — id / imid / bucket / received / sent / subject / summary_preview / uri."""
    return {
        "id": item["id"],
        "imid": item.get("internetMessageId"),
        "bucket": bucket,
        "received": item.get("receivedDateTime"),
        "sent": item.get("sentDateTime"),
        "subject": item.get("subject") or "",
        "summary_preview": (item.get("bodyPreview") or "")[:300],
        "uri": f"mail:///messages/{item['id']}",
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--days-back", type=int, default=14,
                    help="Lower bound for receivedDateTime (default 14)")
    ap.add_argument("--since", type=str,
                    help="Explicit lower bound, ISO date e.g. 2026-04-01. Overrides --days-back.")
    ap.add_argument("--dry", action="store_true",
                    help="Don't write — log what would be added")
    ap.add_argument("--verbose", action="store_true",
                    help="Per-message classification trace")
    ap.add_argument("--no-bodies", action="store_true",
                    help="Stage metadata only — skip body fetch (debug)")
    ap.add_argument("--max-results-per-query", type=int, default=250,
                    help="Cap results per search query (default 250)")
    args = ap.parse_args()

    if args.since:
        cutoff = datetime.fromisoformat(args.since)
        if cutoff.tzinfo is None:
            cutoff = cutoff.replace(tzinfo=timezone.utc)
    else:
        cutoff = datetime.now(timezone.utc) - timedelta(days=args.days_back)
    print(f"refresh_stage: cutoff = {cutoff.isoformat()}")

    print("refresh_stage: acquiring Graph token (silent refresh)…")
    token = get_token_silent()
    print(f"refresh_stage: token OK")

    existing_ids, existing_stage_imids = load_existing_stage_keys()
    existing_body_imids = load_existing_body_imids()
    print(f"refresh_stage: existing stage records = {len(existing_ids)} (by id), "
          f"{len(existing_stage_imids)} (by imid)")
    print(f"refresh_stage: existing body records  = {len(existing_body_imids)}")

    # Two queries. The HILMAR-bookings query MUST use `subject:HILMAR` (not
    # bare-word `HILMAR`) — bare-word matches body content too, and NUMIDIA
    # bookings (a different MBD client) frequently quote/forward HILMAR
    # templates and trip a body match. Caught 2026-05-05 during the live
    # cutover attempt: 6 NUMIDIA booking confirmations bled in as Unknown-
    # destination HILMAR wins. KQL property restrictors limit scope to
    # the parsed subject.
    q1 = f'from:{LONNY_EMAIL} OR to:{LONNY_EMAIL}'
    q2 = f'from:{MBD_BOOKING_EMAIL} AND subject:HILMAR'
    queries = [
        ("lonny-flow", q1),
        ("hilmar-bookings", q2),
    ]

    # Dedupe across queries AND across folder-copies (Inbox vs Sent Items vs
    # archive subfolders all carry distinct Graph `id`s for the same email).
    # internetMessageId (imid) is the RFC822 Message-ID header — stable across
    # folders. We keep the first copy we see; if the email lacks an imid (rare,
    # mostly drafts) we fall back to Graph id to avoid silently dropping it.
    all_items: dict[str, dict] = {}  # imid (or id) -> Graph item
    for label, kql in queries:
        print(f"refresh_stage: query {label!r}: {kql}")
        items = search_messages(token, kql, max_results=args.max_results_per_query)
        print(f"refresh_stage:   got {len(items)} results from {label}")
        for it in items:
            key = it.get("internetMessageId") or it["id"]
            if key not in all_items:
                all_items[key] = it
    print(f"refresh_stage: total unique results across queries: {len(all_items)}")

    # Apply client-side date filter, classify, dedupe vs existing stage.
    new_stage: list[tuple[dict, str]] = []  # (item, bucket)
    bucket_counts: dict[str, int] = {}
    skipped_old = 0
    skipped_unclassified = 0
    skipped_excluded = 0
    skipped_existing = 0
    for it in all_items.values():
        ts = parse_iso(it.get("receivedDateTime")) or parse_iso(it.get("sentDateTime"))
        if ts and ts < cutoff:
            skipped_old += 1
            continue
        bucket = classify(it)
        if bucket is None:
            sender = ((it.get("from") or {}).get("emailAddress") or {}).get("address") or ""
            if sender.lower() in {s.lower() for s in EXCLUDED_SENDERS}:
                skipped_excluded += 1
                if args.verbose:
                    print(f"  EXCL {sender} | {(it.get('subject') or '')[:80]}")
            else:
                skipped_unclassified += 1
                if args.verbose:
                    print(f"  DROP {sender} | {(it.get('subject') or '')[:80]}")
            continue
        # Two-key dedup: skip if this Graph id was previously staged OR if
        # any prior stage record has the same internetMessageId (folder-copy
        # of an already-seen message).
        imid = it.get("internetMessageId")
        if it["id"] in existing_ids or (imid and imid in existing_stage_imids):
            skipped_existing += 1
            continue
        new_stage.append((it, bucket))
        bucket_counts[bucket] = bucket_counts.get(bucket, 0) + 1
        if args.verbose:
            print(f"  NEW  {bucket:<22} {(it.get('subject') or '')[:80]!r}")

    print()
    print(f"refresh_stage: NEW staged records: {len(new_stage)}")
    for b, c in sorted(bucket_counts.items()):
        print(f"  {b}: {c}")
    print(f"refresh_stage: skipped {skipped_old} pre-cutoff, "
          f"{skipped_excluded} excluded, "
          f"{skipped_unclassified} unclassified, "
          f"{skipped_existing} already-staged")

    if args.dry:
        # Sample-by-bucket so the operator can sanity-check classification
        # before committing writes — especially important when the broader
        # HILMAR-bookings query may pull in operational/ops emails for
        # other clients that incidentally mention HILMAR.
        print("\nSamples per bucket (up to 8 each) — sanity-check before running for real:")
        samples_by_bucket: dict[str, list[str]] = {}
        for it, bucket in new_stage:
            samples_by_bucket.setdefault(bucket, []).append(
                f"{(it.get('subject') or '')[:100]}"
            )
        for b in sorted(samples_by_bucket.keys()):
            print(f"\n  --- {b} ({len(samples_by_bucket[b])}) ---")
            for s in samples_by_bucket[b][:8]:
                print(f"    {s!r}")
        print("\nDRY RUN — no writes")
        return 0

    if not new_stage:
        print("\nNothing new to stage.")
        return 0

    # Append stage records (atomic — we hold the file open ourselves)
    print(f"\nAppending {len(new_stage)} records to {STAGE_PATH.name}...")
    for it, bucket in new_stage:
        append_stage_record(build_stage_record(it, bucket))

    if args.no_bodies:
        print("--no-bodies set — skipping body fetch.")
        return 0

    # Fetch bodies for new entries (skipping any whose imid is already in bodies file)
    body_count = 0
    body_failures = 0
    for it, bucket in new_stage:
        imid = it.get("internetMessageId")
        if imid and imid in existing_body_imids:
            continue
        try:
            full = get_message_body(token, it["id"])
        except Exception as e:
            body_failures += 1
            print(f"  body fetch FAIL {(it.get('subject') or '')[:60]!r}: {e}")
            continue
        body = full.get("body") or {}
        sender = ((full.get("from") or {}).get("emailAddress") or {}).get("address")
        FB.upsert_body(
            imid=full.get("internetMessageId") or full["id"],
            bucket=bucket,
            uri=f"mail:///messages/{full['id']}",
            subject=full.get("subject") or "",
            html_body=body.get("content") or "",
            conversation_id=full.get("conversationId"),
            sender_email=sender,
            sent_ts=full.get("sentDateTime"),
            received_ts=full.get("receivedDateTime"),
        )
        body_count += 1
        if args.verbose:
            print(f"  BODY {bucket:<22} {(full.get('subject') or '')[:60]!r}")

    print(f"\nrefresh_stage: fetched {body_count} new bodies, {body_failures} failures")
    return 0 if body_failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
