"""
fetch_bodies.py - Body-fetch orchestration for the Hilmar tracker pipeline.

Why this exists:
  Day-1 audit (Apr 21 2026) showed ~50% of tracker fields were blank because
  ingest.py only ever read the ~50-char preview from stage_emails.jsonl.
  Fields like eta_requested, eta_offered, vessel, transshipment, rate body,
  and destination on standalone bookings live in the EMAIL BODY - not the
  preview. This module is Plan A, Day 1 of Plan D (Hybrid A+B).

What it does:
  1. Defines the `stage_emails_bodies.jsonl` schema (imid-keyed).
  2. Loads/merges bodies idempotently (dedup by imid).
  3. Runs body_parser over each fetched body and persists parsed fields
     alongside the raw HTML + text.
  4. Reports what's missing so a Claude-driven fetch loop knows what to
     pull via Outlook MCP read_resource.

Fetch pattern (actual HTTP-ish work):
  MCP read_resource is only callable from inside a Claude session, NOT
  from a plain Python process. So the fetch is orchestrated in-session:
    - Claude calls: read_resource({"uri": staged_row["uri"]})
    - Claude appends the response via fetch_bodies.upsert_body(...)
  This module is the CONSUMER side (storage + parsing). The producer
  side is the Claude turn that fans out read_resource calls.

Schema (stage_emails_bodies.jsonl - one JSON row per imid):
  {
    "imid": "<internet-message-id>",       # primary key (dedup)
    "bucket": "lonny_outbound|lonny_reply|mbd_inbound|mbd_rate_response",
    "uri": "<outlook://... resource URI>",
    "subject": "...",
    "conversation_id": "AAQkA...",          # Outlook thread key
    "sender_email": "lonny.x@hilmar.com",
    "sent_ts": "2026-04-20T17:04:12Z",
    "received_ts": "2026-04-20T17:04:40Z",
    "html_body": "<html>...</html>",        # raw as returned by read_resource
    "text_body": "plaintext",               # html_to_text(html_body)
    "parsed": {                             # body_parser outputs merged:
       "eta_requested": "2026-04-30" | null,
       "etd_offered":   "2026-04-30" | null,
       "eta_offered":   "2026-05-18" | null,
       "origin_cutoff": "2026-04-25" | null,
       "vessel_voyage": "MSC OSCAR / 012E" | null,
       "transshipment":"Singapore" | "Direct" | null,
       "rate_table":   { ... parse_rate_table ... } | null,
       "send_signal":   true | false,        # lonny_reply only
       "origin":        "..." | null,
       "destination":   "..." | null
    },
    "fetched_at": "2026-04-21T22:10:00Z"
  }

CLI:
  python3 scripts/fetch_bodies.py --status          # count fetched vs staged
  python3 scripts/fetch_bodies.py --list-missing    # print imids still to fetch
  python3 scripts/fetch_bodies.py --list-missing --bucket mbd_rate_response
  python3 scripts/fetch_bodies.py --priority        # rank for pilot fetch
"""
from __future__ import annotations
import json
import sys
import argparse
from pathlib import Path
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import body_parser as BP  # noqa: E402
import core as C  # noqa: E402  -- for parse_signer (added 2026-04-30)

STAGE_PATH   = ROOT / "scripts" / "stage_emails.jsonl"
BODIES_PATH  = ROOT / "scripts" / "stage_emails_bodies.jsonl"


# ---------- I/O ----------

def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    with path.open("r", encoding="utf-8") as f:
        for i, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError as e:
                print(f"  WARN: {path.name} line {i}: {e}", file=sys.stderr)
    return out


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def load_staged() -> list[dict]:
    return _read_jsonl(STAGE_PATH)


def load_bodies() -> list[dict]:
    return _read_jsonl(BODIES_PATH)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# ---------- Parse helpers ----------

def _parse_all(text_body: str, subject: str, bucket: str) -> dict:
    """Run every body_parser parser applicable to this bucket."""
    out = {
        "eta_requested": None,
        "etd_offered": None,
        "eta_offered": None,
        "origin_cutoff": None,
        "vessel_voyage": None,
        "transshipment": None,
        "rate_table": None,
        "send_signal": False,
        "origin": None,
        "destination": None,
        "ol_responder_signer": None,
    }
    if not text_body:
        text_body = ""

    # Subject-based lane (all buckets)
    origin, dest = BP.parse_subject_lane(subject or "")
    out["origin"] = origin
    out["destination"] = dest

    # Date parsers — anchor-based (parse_etd_offered etc.) — work on any body.
    out["eta_requested"]  = BP.parse_eta_requested(text_body)
    out["etd_offered"]    = BP.parse_etd_offered(text_body)
    out["eta_offered"]    = BP.parse_eta_offered(text_body)
    out["origin_cutoff"]  = BP.parse_origin_cutoff(text_body)
    # NOTE: body_parser does NOT export standalone parse_vessel /
    # parse_transshipment — vessel + transshipment come exclusively from the
    # OL rate-table (the column extraction inside parse_rate_table). Older
    # versions of this function had stale BP.parse_vessel / BP.parse_send_signal
    # references that would AttributeError on every body fetch (caught
    # 2026-05-05 during refresh_stage build).

    # Rate table only makes sense on carrier responses
    if bucket == "mbd_rate_response":
        rt = BP.parse_rate_table(text_body)
        out["rate_table"] = rt if rt else None
        # Bubble up individual fields for convenience
        if rt:
            out["etd_offered"]   = out["etd_offered"] or rt.get("etd_offered") or rt.get("etd")
            out["eta_offered"]   = out["eta_offered"] or rt.get("eta_offered") or rt.get("eta")
            out["vessel_voyage"] = rt.get("vessel_voyage")
            out["transshipment"] = rt.get("transshipment")

    # Chain-send signal — heuristic: bare "Send"/"SEND" + Lonny in lonny_reply
    # Use core's canonical detector (existing function, used by ingest too).
    if bucket == "lonny_reply":
        try:
            out["send_signal"] = bool(C.is_lonny_send_reply(text_body, is_reply=True))
        except Exception:
            out["send_signal"] = False

    # OL-side signer: parse human name from body sign-off
    # (when From=mailbox like MBD_OceanExportBookingShared, the actual person
    # signs off in the body — Caren / Linda / Steve / etc.). Added 2026-04-30.
    if bucket in ("mbd_inbound", "mbd_rate_response"):
        out["ol_responder_signer"] = C.parse_signer(None, text_body)

    return out


# ---------- Upsert ----------

def upsert_body(
    imid: str,
    bucket: str,
    uri: str,
    subject: str,
    html_body: str,
    conversation_id: str | None = None,
    sender_email: str | None = None,
    sent_ts: str | None = None,
    received_ts: str | None = None,
) -> dict:
    """Add or replace a body row. Returns the stored record."""
    text_body = BP.html_to_text(html_body or "")
    parsed = _parse_all(text_body, subject, bucket)

    rec = {
        "imid": imid,
        "bucket": bucket,
        "uri": uri,
        "subject": subject,
        "conversation_id": conversation_id,
        "sender_email": sender_email,
        "sent_ts": sent_ts,
        "received_ts": received_ts,
        "html_body": html_body or "",
        "text_body": text_body,
        "parsed": parsed,
        "fetched_at": _now_iso(),
    }

    rows = load_bodies()
    index = {r["imid"]: i for i, r in enumerate(rows) if r.get("imid")}
    if imid in index:
        rows[index[imid]] = rec
    else:
        rows.append(rec)
    _write_jsonl(BODIES_PATH, rows)
    return rec


def upsert_batch(records: list[dict]) -> int:
    """Bulk upsert. `records` items must have keys matching upsert_body()."""
    count = 0
    for r in records:
        upsert_body(**r)
        count += 1
    return count


# ---------- Reporting ----------

def _index_bodies() -> dict[str, dict]:
    return {r["imid"]: r for r in load_bodies() if r.get("imid")}


def list_missing(bucket_filter: str | None = None) -> list[dict]:
    staged = load_staged()
    got = _index_bodies()
    out = []
    for row in staged:
        imid = row.get("imid")
        if not imid:
            continue
        if bucket_filter and row.get("bucket") != bucket_filter:
            continue
        if imid not in got:
            out.append(row)
    return out


def status() -> dict:
    staged = load_staged()
    got = _index_bodies()
    by_bucket = {}
    for row in staged:
        b = row.get("bucket", "?")
        s = by_bucket.setdefault(b, {"staged": 0, "fetched": 0})
        s["staged"] += 1
        if row.get("imid") in got:
            s["fetched"] += 1
    return {
        "total_staged": len(staged),
        "total_fetched": len(got),
        "by_bucket": by_bucket,
    }


def priority_queue(limit: int = 20) -> list[dict]:
    """Rank missing rows by where body data moves the needle most.

    Priority (descending):
      1. mbd_rate_response    - unlocks rate_table / ETD / ETA / vessel
      2. mbd_inbound          - unlocks standalone-booking lane + carrier
      3. lonny_reply          - unlocks send_signal wins
      4. lonny_outbound       - unlocks eta_requested
    """
    order = {
        "mbd_rate_response": 0,
        "mbd_inbound": 1,
        "lonny_reply": 2,
        "lonny_outbound": 3,
    }
    missing = list_missing()
    missing.sort(key=lambda r: (order.get(r.get("bucket"), 9), r.get("received") or r.get("sent") or ""))
    return missing[:limit]


# ---------- CLI ----------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--list-missing", action="store_true")
    ap.add_argument("--priority", action="store_true")
    ap.add_argument("--bucket", default=None)
    ap.add_argument("--limit", type=int, default=20)
    args = ap.parse_args()

    if args.status or (not args.list_missing and not args.priority):
        s = status()
        print(f"Staged: {s['total_staged']}  Fetched bodies: {s['total_fetched']}")
        for b, n in sorted(s["by_bucket"].items()):
            pct = (100.0 * n["fetched"] / n["staged"]) if n["staged"] else 0.0
            print(f"  {b:22s}  {n['fetched']:3d}/{n['staged']:3d}  ({pct:5.1f}%)")

    if args.list_missing:
        rows = list_missing(args.bucket)
        print(f"\nMissing bodies ({len(rows)}{' in ' + args.bucket if args.bucket else ''}):")
        for r in rows[: args.limit]:
            print(f"  {r.get('bucket','?'):22s}  {r.get('imid','?')[:60]:60s}  {r.get('subject','')[:70]}")

    if args.priority:
        rows = priority_queue(args.limit)
        print(f"\nPriority fetch queue (top {len(rows)}):")
        for r in rows:
            print(f"  {r.get('bucket','?'):22s}  imid={r.get('imid','?')[:50]:50s}")
            print(f"     subj: {r.get('subject','')[:90]}")
            print(f"     uri : {r.get('uri','')[:110]}")


if __name__ == "__main__":
    main()
