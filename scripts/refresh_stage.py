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

import msal
import requests

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import contextlib

import fetch_bodies as FB  # noqa: E402  reuse upsert_body / _parse_all
import outlook_send as OS  # noqa: E402  reuse auth + token cache

GRAPH = "https://graph.microsoft.com/v1.0"
# 2026-05-06: renamed from .jsonl to .txt so SharePoint indexes the files
# (claude.ai routine's M365 MCP can fetch .txt via search; .jsonl is not
# indexed). Same JSON-Lines content. Fall back to legacy .jsonl if .txt
# doesn't exist yet.
def _resolve(name: str) -> Path:
    new = SCRIPTS / f"{name}.txt"
    legacy = SCRIPTS / f"{name}.jsonl"
    return new if new.exists() or not legacy.exists() else legacy
STAGE_PATH = _resolve("stage_emails")
BODIES_PATH = _resolve("stage_emails_bodies")

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
                with contextlib.suppress(ValueError):
                    wait = float(ra)
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
    "bodyPreview,receivedDateTime,sentDateTime,internetMessageId,isRead,hasAttachments,"
    # 2026-05-19 PM (Michael "you have to parse the booking team emails
    # for matches based on header meta data"): fetch the full email
    # header collection so we can read In-Reply-To + References. Those
    # link MDOLX booking confirmations back to the originating Lonny
    # RFQ thread even when conversation_id differs.
    "internetMessageHeaders"
)


def _rotate_stage(days_to_keep: int, dry: bool = False) -> dict:
    """Prune stage_emails.txt + stage_emails_bodies.txt of records older than
    `days_to_keep` days. Writes audit log to scripts/_stage_rotation.log.

    Added 2026-05-14 per best-practices batch — keeps stage bounded so it
    doesn't grow unbounded over time. Default retain = 90d (Outlook search
    catches anything we need re-pulled).
    """
    import json as _j
    from datetime import datetime as _dt
    from datetime import timedelta as _td
    from datetime import timezone as _tz
    scripts_dir = ROOT / "scripts"
    cutoff = (_dt.now(_tz.utc) - _td(days=days_to_keep)).isoformat()

    out = {"stage_pruned": 0, "bodies_pruned": 0, "cutoff": cutoff}
    stage_path = scripts_dir / "stage_emails.txt"
    bodies_path = scripts_dir / "stage_emails_bodies.txt"

    # Stage prune — keep records with received/sent ≥ cutoff
    if stage_path.exists():
        kept_lines = []
        pruned_imids = set()
        for line in stage_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                d = _j.loads(line)
            except Exception:
                kept_lines.append(line)
                continue
            ts = (d.get("received") or d.get("sent") or d.get("sent_ts") or "")
            if ts and ts < cutoff:
                pruned_imids.add((d.get("imid") or "").strip("<>"))
                out["stage_pruned"] += 1
            else:
                kept_lines.append(line)
        if not dry and out["stage_pruned"] > 0:
            # Write atomic — temp file then rename
            tmp = stage_path.with_suffix(".tmp")
            tmp.write_text("\n".join(kept_lines) + "\n", encoding="utf-8")
            tmp.replace(stage_path)

    # Bodies prune — drop bodies for imids that got rotated out
    if bodies_path.exists() and out["stage_pruned"] > 0:
        kept_bodies = []
        for line in bodies_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                d = _j.loads(line)
            except Exception:
                kept_bodies.append(line)
                continue
            imid = (d.get("imid") or "").strip("<>")
            if imid and imid in pruned_imids:
                out["bodies_pruned"] += 1
            else:
                kept_bodies.append(line)
        if not dry and out["bodies_pruned"] > 0:
            tmp = bodies_path.with_suffix(".tmp")
            tmp.write_text("\n".join(kept_bodies) + "\n", encoding="utf-8")
            tmp.replace(bodies_path)

    # Audit log
    if not dry:
        log_path = scripts_dir / "_stage_rotation.log"
        with log_path.open("a", encoding="utf-8") as f:
            f.write(f"{_dt.now(_tz.utc).isoformat()} cutoff={cutoff} "
                    f"stage_pruned={out['stage_pruned']} "
                    f"bodies_pruned={out['bodies_pruned']}\n")
    return out


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


def fetch_pdf_attachments(token: str, message_id: str, imid: str, dest_dir: Path) -> list[str]:
    """Download PDF attachments for a message. Returns list of saved filenames.

    Added 2026-05-13 per Michael "90 percent for all is the bare minimum".
    Booking-confirmation emails have signature-only bodies; the actual
    vessel/ETD/rate data is in the attached PDF. pdf_parser.py reads these.
    Saves to scripts/stage_pdfs/<safe_imid>.pdf. Idempotent — skips if
    already downloaded.
    """
    import base64

    dest_dir.mkdir(parents=True, exist_ok=True)
    saved = []
    safe_imid = re.sub(r"[^A-Za-z0-9._-]+", "_", imid.strip("<>"))[:100]
    target_pdf = dest_dir / f"{safe_imid}.pdf"
    if target_pdf.exists():
        return [target_pdf.name]  # already cached
    # List attachments
    list_url = f"{GRAPH}/me/messages/{message_id}/attachments?$select=id,name,contentType,size"
    try:
        listing = graph_get(token, list_url)
    except Exception:
        return saved
    for att in listing.get("value", []) or []:
        name = (att.get("name") or "").lower()
        ctype = (att.get("contentType") or "").lower()
        if not (name.endswith(".pdf") or "pdf" in ctype):
            continue
        att_id = att.get("id")
        if not att_id:
            continue
        # Fetch content
        content_url = f"{GRAPH}/me/messages/{message_id}/attachments/{att_id}"
        try:
            data = graph_get(token, content_url)
        except Exception:
            continue
        bytes_b64 = data.get("contentBytes")
        if not bytes_b64:
            continue
        try:
            pdf_bytes = base64.b64decode(bytes_b64)
        except Exception:
            continue
        target_pdf.write_bytes(pdf_bytes)
        saved.append(target_pdf.name)
        break  # one PDF per message is enough
    return saved


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


def _extract_thread_headers(item: dict) -> dict:
    """Extract In-Reply-To + References from Graph's internetMessageHeaders.

    2026-05-19 PM (Michael "parse the booking team emails for matches
    based on header meta data and you'll find them"): the booking-
    confirmation email is sent by OL in a NEW conversation_id (different
    from the original Lonny RFQ thread), but its In-Reply-To / References
    headers point back to the prior thread's Message-ID. We capture those
    so ingest.link_bookings_to_requests can match a booking back to its
    originating RFQ even when conversation_id differs.

    Returns: {"in_reply_to": "<...>" | None, "references": ["<...>", ...]}
    """
    headers = item.get("internetMessageHeaders") or []
    out = {"in_reply_to": None, "references": []}
    for h in headers:
        name = (h.get("name") or "").lower()
        value = (h.get("value") or "").strip()
        if name == "in-reply-to":
            out["in_reply_to"] = value
        elif name == "references":
            # References is space-or-CRLF separated list of <message-id>'s
            out["references"] = [m for m in value.replace("\n", " ").replace("\r", " ").split() if m]
    return out


def build_stage_record(item: dict, bucket: str) -> dict:
    """Same shape Cowork's MCP wrote — id / imid / bucket / received / sent / subject / summary_preview / uri.

    2026-05-19 PM: now also includes `in_reply_to` + `references` (parsed
    from internetMessageHeaders) so the booking-link matcher can use them.
    Backward-compat: rows without these fields still work (matcher falls
    back to conv_id + lane+time).
    """
    threading = _extract_thread_headers(item)
    return {
        "id": item["id"],
        "imid": item.get("internetMessageId"),
        "bucket": bucket,
        "received": item.get("receivedDateTime"),
        "sent": item.get("sentDateTime"),
        "subject": item.get("subject") or "",
        "summary_preview": (item.get("bodyPreview") or "")[:300],
        "uri": f"mail:///messages/{item['id']}",
        # Threading metadata for header-based booking matching
        "in_reply_to": threading["in_reply_to"],
        "references": threading["references"],
        "conversation_id": item.get("conversationId"),
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
    ap.add_argument("--rotate-stage-older-than", type=int, default=None,
                    help="Prune stage_emails.txt + bodies of records older than N days. "
                         "Keeps live data fast and bounded. Per Michael 2026-05-14 "
                         "best-practices batch. Recommended: 90.")
    ap.add_argument("--pdf-backfill", action="store_true",
                    help="Also fetch PDF attachments for existing mbd_inbound bodies that don't have them on disk yet (one-time catch-up).")
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
    print("refresh_stage: token OK")

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
    # Stage rotation — prune old records before fetch so we don't carry stale
    # state through the pipeline. Audit log written to scripts/_stage_rotation.log.
    if args.rotate_stage_older_than:
        rotated = _rotate_stage(args.rotate_stage_older_than, dry=args.dry)
        print(f"refresh_stage: rotation pruned {rotated['stage_pruned']} stage records "
              f"+ {rotated['bodies_pruned']} bodies older than {args.rotate_stage_older_than}d "
              f"({'DRY' if args.dry else 'live'})")

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

    if not new_stage and not args.pdf_backfill:
        print("\nNothing new to stage.")
        return 0
    if not new_stage and args.pdf_backfill:
        print("\nNothing new to stage — proceeding to PDF backfill of existing bodies.")
        # Fall through to backfill block below by skipping the new-body fetch loop
        body_count = 0
        body_failures = 0
        pdf_count = 0
        pdf_dir = ROOT / "scripts" / "stage_pdfs"
        # Jump to backfill block via a guard
        _skip_to_backfill = True
    else:
        _skip_to_backfill = False

    if not _skip_to_backfill:
        # Append stage records (atomic — we hold the file open ourselves)
        print(f"\nAppending {len(new_stage)} records to {STAGE_PATH.name}...")
        for it, bucket in new_stage:
            append_stage_record(build_stage_record(it, bucket))

        if args.no_bodies:
            print("--no-bodies set — skipping body fetch.")
            return 0

    # Fetch bodies for new entries (skipping any whose imid is already in bodies file)
    if not _skip_to_backfill:
        body_count = 0
        body_failures = 0
        pdf_count = 0
        pdf_dir = ROOT / "scripts" / "stage_pdfs"
    _body_iter = [] if _skip_to_backfill else new_stage
    for it, bucket in _body_iter:
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
        msg_imid = full.get("internetMessageId") or full["id"]
        FB.upsert_body(
            imid=msg_imid,
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
        # Booking-confirmation bodies are signature-only — pull the PDF
        # attachment for pdf_parser.py to extract vessel/ETD/rate from.
        # Only mbd_inbound (booking confirmations) — the other buckets
        # don't carry useful PDFs.
        if bucket == "mbd_inbound" and it.get("hasAttachments"):
            try:
                saved = fetch_pdf_attachments(token, full["id"], msg_imid, pdf_dir)
                if saved:
                    pdf_count += 1
                    if args.verbose:
                        print(f"  PDF  {saved[0]}")
            except Exception as e:
                if args.verbose:
                    print(f"  pdf fetch FAIL {imid[:40]}: {e}")
        if args.verbose:
            print(f"  BODY {bucket:<22} {(full.get('subject') or '')[:60]!r}")

    print(f"\nrefresh_stage: fetched {body_count} new bodies, {body_failures} failures, "
          f"{pdf_count} PDF attachments")

    # Backfill PDF attachments for existing mbd_inbound bodies. The PDF
    # download was added 2026-05-13 — prior fires never pulled PDFs.
    # This catches up the historical booking confirmations so pdf_parser
    # can extract their data. Idempotent: skips imids whose PDF is
    # already on disk.
    if args.pdf_backfill:
        print("\nrefresh_stage: PDF backfill for existing mbd_inbound bodies…")
        bodies_path = ROOT / "scripts" / "stage_emails_bodies.txt"
        pdf_backfilled = 0
        pdf_skipped = 0
        pdf_fail = 0
        if bodies_path.exists():
            import re as _re
            for line in bodies_path.read_text(encoding="utf-8", errors="ignore").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                if d.get("bucket") != "mbd_inbound":
                    continue
                imid = (d.get("imid") or "").strip("<>")
                if not imid:
                    continue
                safe_imid = _re.sub(r"[^A-Za-z0-9._-]+", "_", imid)[:100]
                target = pdf_dir / f"{safe_imid}.pdf"
                if target.exists():
                    pdf_skipped += 1
                    continue
                # Reconstruct the Graph message_id from the URI saved in body record
                uri = d.get("uri") or ""
                m = _re.search(r"messages/([^/]+)", uri)
                if not m:
                    continue
                msg_id = m.group(1)
                try:
                    saved = fetch_pdf_attachments(token, msg_id, imid, pdf_dir)
                    if saved:
                        pdf_backfilled += 1
                    if args.verbose:
                        print(f"  backfill {('PDF ' + saved[0]) if saved else 'no-attach'} {imid[:50]}")
                except Exception as e:
                    pdf_fail += 1
                    if args.verbose:
                        print(f"  backfill FAIL {imid[:50]}: {e}")
        print(f"refresh_stage: PDF backfill — {pdf_backfilled} new, {pdf_skipped} already-cached, {pdf_fail} failed")

    return 0 if body_failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
