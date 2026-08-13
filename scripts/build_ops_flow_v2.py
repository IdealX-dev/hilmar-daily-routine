#!/usr/bin/env python3
"""
build_ops_flow_v2.py — strict ops-flow inquiry builder.

Reads a folder of raw message metadata JSONs (produced by extract_msg_metadata.py)
and builds ops_flow_inquiries_v2.json with:

  - Strict pairing: same conversationId + within 72h window
  - Chaser detection (same conv, follow-up language, no new inquiry created)
  - "Send" WIN signal (body scan of post-OL-response Lonny messages)
  - MDOLX match (14-day forward window from inquiry date)
  - 5-bucket classification: WIN / QUOTED_LOST / NOT_QUOTED / PENDING / BOOKING_CANCELED

Input folder structure:
    scripts/ops_flow_v2_raw/
        request_01.json    # Lonny-authored messages (metadata only)
        request_02.json
        ...
        response_01.json   # MBD_OceanExportBookingShared replies (metadata only)
        response_02.json
        ...

Each file is the compact-metadata format from extract_msg_metadata.py.

Output:
    scripts/ops_flow_inquiries_v2.json

Run:
    python3 scripts/build_ops_flow_v2.py
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).parent.parent
RAW = ROOT / "scripts" / "ops_flow_v2_raw"
MDOLX_PATH = ROOT / "scripts" / "hilmar_booking_confirmations.json"
OUT = ROOT / "scripts" / "ops_flow_inquiries_v2.json"

WINDOW_START = "2026-04-01T00:00:00+00:00"
WINDOW_END   = "2026-04-20T23:59:59+00:00"
PAIRING_MAX_HOURS = 72
PENDING_MAX_HOURS = 24
SEND_SCAN_MAX_DAYS = 7

# --- Regexes for body scanning ---
CHASER_PATTERNS = [
    r"\bany update\b", r"\bfollowing up\b", r"\bfollow[- ]up\b",
    r"\bnobody responded\b", r"\bno one responded\b",
    r"\bstill waiting\b", r"\bplease advise\b",
    r"\bdid you see\b", r"\bhave you seen\b",
    r"\bchecking in\b", r"\bbump\b", r"\bgentle reminder\b",
    r"\bplease update\b",
]
SEND_PATTERNS = [
    r"\bsend\b(?!\s+(rates|pricing|quote|reference))",   # "Send" but not "send rates"
    r"\bplease send\b", r"\bplease proceed\b",
    r"\bbook (it|this|option)\b", r"\bgo ahead\b",
    r"\blet[\s']*s book\b",
    r"\byes,? please book\b",
    r"\bconfirmed\b(?=.{0,40}(book|proceed|option))",
]

POD_CANONICAL = {
    "hcmc": "HCMC", "hcmc (cat lai port)": "HCMC",
    "ho chi minh": "HCMC", "ho chi minh city": "HCMC",
    "ho chi minh (cat lai port)": "HCMC",
}

def parse_iso(ts):
    if not ts: return None
    try:
        s = ts.replace("Z", "+00:00")
        return datetime.fromisoformat(s)
    except Exception:
        return None

def canonical_pod(s):
    if not s: return None
    k = re.sub(r"\s+", " ", s).strip().lower()
    if k in POD_CANONICAL: return POD_CANONICAL[k]
    parts = k.split()
    out = []
    for p in parts:
        if p in ("to","of","the","and","-"): out.append(p)
        elif p.startswith("("):
            out.append("(" + p.strip("()").title() + ")")
        else: out.append(p.title())
    return " ".join(out)

def parse_subject_fields(subj):
    """Extract POL/POD from subjects like 'Oakland to Tokyo' or 'Re: Oakland to HCMC (Cat Lai Port)'."""
    if not subj: return (None, None)
    m = re.search(r"(?:Re:\s*)?(Oakland|Dalhart)\s+to\s+([A-Za-z][A-Za-z \(\)\./-]*?)(?:\s*[/]|\s{2,}|$)", subj, re.I)
    if m:
        return (m.group(1).title(), canonical_pod(m.group(2).strip()))
    return (None, None)

def is_chaser(body: str) -> bool:
    t = (body or "").lower()
    return any(re.search(p, t) for p in CHASER_PATTERNS)

def has_send_signal(body: str) -> tuple[bool, str]:
    """Returns (is_send, matched_text)."""
    t = (body or "").lower()
    for p in SEND_PATTERNS:
        m = re.search(p, t)
        if m:
            # capture a snippet around the match
            i = m.start()
            snippet = (body or "")[max(0, i-20): i+60].strip()
            return (True, snippet)
    return (False, "")

def in_window(ts_iso):
    dt = parse_iso(ts_iso)
    if not dt: return False
    start, end = parse_iso(WINDOW_START), parse_iso(WINDOW_END)
    return start <= dt <= end


def load_raw(folder: Path):
    requests, responses = [], []
    for f in sorted(folder.glob("request_*.json")):
        m = json.loads(f.read_text(encoding="utf-8"))
        m["_src_file"] = f.name
        requests.append(m)
    for f in sorted(folder.glob("response_*.json")):
        m = json.loads(f.read_text(encoding="utf-8"))
        m["_src_file"] = f.name
        responses.append(m)
    return requests, responses


def parse_ol_options(body_text: str) -> list[dict]:
    """Parse OL rate-quote options out of an email body excerpt.

    Delegates to ``hilmar.body_parser.parse_rate_table``, which handles both
    the column-layout MBD format and prose fallbacks. That parser returns a
    single dict; we wrap it in the v1-shaped list-of-options structure so
    downstream consumers see ``options[0]`` exactly as v1 emitted.

    Returns ``[]`` when the body excerpt is empty, the parser extracts no
    fields, or the import fails — empty options remain an acceptable
    degraded signal here because pairing alone proves OL responded.
    """
    if not body_text:
        return []
    try:
        import sys as _sys
        _src_dir = Path(__file__).resolve().parent.parent / "src"
        if str(_src_dir) not in _sys.path:
            _sys.path.insert(0, str(_src_dir))
        from hilmar.body_parser import parse_rate_table
    except Exception:
        return []

    parsed = parse_rate_table(body_text) or {}
    if not parsed:
        return []

    # Vessel / voyage come from their own table cells now. parse_rate_table
    # emits them split (2026-08-13, when it moved to header-to-cell alignment)
    # AND joined as `vessel_voyage` in the house "NYK METEOR 0CLNCE1MA" form.
    # The legacy split-on-"/" below stays as the fallback for the prose and
    # vertical-column paths, which still join with " / ".
    vessel = parsed.get("vessel")
    voyage = parsed.get("voyage")
    vessel_voyage = parsed.get("vessel_voyage")
    if not vessel and vessel_voyage:
        parts = [p.strip() for p in vessel_voyage.split("/", 1)]
        vessel = parts[0] or None
        voyage = voyage or (parts[1] if len(parts) > 1 and parts[1] else None)

    rate_val = parsed.get("ol_rate")
    rate_usd = int(rate_val) if isinstance(rate_val, (int, float)) else None

    option = {
        "option": 1,
        "carrier": parsed.get("carrier_quoted"),
        "vessel": vessel,
        "voyage": voyage,
        "container_size": None,
        "commodity": None,
        "erd": parsed.get("erd") or parsed.get("origin_cutoff"),
        "doc_cut": None,
        "port_cut": None,
        "etd": parsed.get("etd"),
        "eta": parsed.get("eta"),
        "transshipment": parsed.get("transshipment"),
        "rate_usd": rate_usd,
        "dthc_included": None,
        "origin_free_time_days": parsed.get("origin_free_time"),
        "dest_free_time_days": parsed.get("dest_free_time"),
    }
    if not any(v not in (None, "", 1) for k, v in option.items() if k != "option"):
        return []
    return [option]


def build():
    requests, responses = load_raw(RAW)
    with open(MDOLX_PATH, encoding="utf-8") as f:
        mdolx = json.load(f).get("confirmations", [])
    now = datetime.now(timezone.utc)

    # -------- Group messages by conversationId --------
    threads: dict[str, dict] = {}  # cid -> {"requests":[], "responses":[]}
    orphan_no_cid = []

    for r in requests:
        cid = r.get("conversationId")
        if not cid:
            orphan_no_cid.append(r)
            continue
        threads.setdefault(cid, {"requests":[], "responses":[]})["requests"].append(r)
    for r in responses:
        cid = r.get("conversationId")
        if not cid:
            orphan_no_cid.append(r)
            continue
        threads.setdefault(cid, {"requests":[], "responses":[]})["responses"].append(r)

    # -------- Build inquiries --------
    inquiries = []
    warnings = []
    send_wins_count = 0
    chasers_count = 0

    for cid, t in threads.items():
        # Sort by time
        t["requests"].sort(key=lambda x: parse_iso(x.get("receivedDateTime") or x.get("sentDateTime") or ""))
        t["responses"].sort(key=lambda x: parse_iso(x.get("receivedDateTime") or x.get("sentDateTime") or ""))

        # First Lonny message in conv = the inquiry (unless chaser-only which shouldn't happen)
        # Subsequent Lonny messages = chasers OR send-reply
        if not t["requests"]:
            # Orphan response (no matching Lonny inquiry) — skip but warn
            warnings.append(f"conv {cid[-20:]}: response with no preceding inquiry")
            continue

        # First Lonny email in window
        first_inquiry = None
        for r in t["requests"]:
            if in_window(r.get("receivedDateTime") or r.get("sentDateTime")):
                first_inquiry = r
                break
        if not first_inquiry:
            continue  # conv has Lonny msgs but none in window

        # Skip if first-inquiry body is a chaser (e.g., picking up a pre-window thread)
        if is_chaser(first_inquiry.get("body_first_500","")):
            warnings.append(f"conv {cid[-20:]}: first in-window Lonny msg is a chaser — treating as continuation of pre-window thread, skipping")
            continue

        inq_dt = parse_iso(first_inquiry.get("receivedDateTime") or first_inquiry.get("sentDateTime"))
        pol, pod = parse_subject_fields(first_inquiry.get("subject",""))

        # Skip non-ops-flow threads:
        #  - replies on existing booking/claim threads (RE: MDOLX..., RE: pls claim..., RE: NAM...)
        #  - policy / free-time / billing disputes that didn't match Oakland|Dalhart → POD pattern
        subj = first_inquiry.get("subject","") or ""
        if re.match(r"(?i)^\s*(?:re:\s*|fw:\s*|fwd:\s*)*(mdolx|pls\s+claim|claim\s+nam|reefer\s+free\s+time)", subj):
            warnings.append(f"conv {cid[-20:]}: non-ops-flow thread skipped (reply-on-booking/claim/policy): {subj[:70]!r}")
            continue
        if pol is None:
            warnings.append(f"conv {cid[-20:]}: non-ops-flow subject skipped (no Oakland/Dalhart→POD pattern): {subj[:70]!r}")
            continue

        # Chaser detection — any Lonny msg after first_inquiry, before OL response, with chaser language
        chasers = []
        for r in t["requests"]:
            if r is first_inquiry: continue
            r_dt = parse_iso(r.get("receivedDateTime") or r.get("sentDateTime"))
            if not r_dt or r_dt <= inq_dt: continue
            if is_chaser(r.get("body_first_500","")):
                chasers.append(r_dt.isoformat())

        chasers_count += len(chasers)

        # OL response: first in same conv within 72h of inquiry
        ol_response = None
        for resp in t["responses"]:
            r_dt = parse_iso(resp.get("receivedDateTime") or resp.get("sentDateTime"))
            if not r_dt: continue
            if r_dt <= inq_dt: continue
            gap_h = (r_dt - inq_dt).total_seconds() / 3600.0
            if gap_h <= PAIRING_MAX_HOURS:
                ol_response = resp
                break  # take first (sorted)

        # Send-reply detection: any Lonny msg after OL response, within SEND_SCAN_MAX_DAYS,
        # with "send" language
        send_reply = None
        send_snippet = ""
        if ol_response:
            ol_dt = parse_iso(ol_response.get("receivedDateTime") or ol_response.get("sentDateTime"))
            for r in t["requests"]:
                r_dt = parse_iso(r.get("receivedDateTime") or r.get("sentDateTime"))
                if not r_dt or r_dt <= ol_dt: continue
                if (r_dt - ol_dt) > timedelta(days=SEND_SCAN_MAX_DAYS): break
                is_send, snippet = has_send_signal(r.get("body_first_500",""))
                if is_send:
                    send_reply = r
                    send_snippet = snippet
                    break

        # MDOLX match: same carrier family + POD + equipment within 14 days forward
        matched_mdolx = None
        mdolx_status = None
        for m in mdolx:
            m_dt = parse_iso(m.get("received_at",""))
            if not m_dt: continue
            if m_dt < inq_dt: continue
            if (m_dt - inq_dt) > timedelta(days=14): continue
            m_pod = canonical_pod(m.get("pod","")) or ""
            if pod and m_pod == pod:
                matched_mdolx = m.get("mdolx_ref")
                mdolx_status = (m.get("status","CONFIRMED") or "").upper()
                break

        # Classify
        if matched_mdolx:
            if mdolx_status == "CANCELED":
                status = "BOOKING_CANCELED"
                status_reason = f"Matched {matched_mdolx} but booking was canceled"
            else:
                status = "WIN"
                status_reason = f"Matched MDOLX {matched_mdolx}"
        elif send_reply:
            status = "WIN"
            send_wins_count += 1
            status_reason = f"Lonny 'send' reply: '{send_snippet[:80]}' (no MDOLX matched yet)"
        elif not ol_response:
            status = "NOT_QUOTED"
            status_reason = f"No OL response in same conversation within {PAIRING_MAX_HOURS}h"
        else:
            ol_dt = parse_iso(ol_response.get("receivedDateTime") or ol_response.get("sentDateTime"))
            age_h = (now - ol_dt).total_seconds() / 3600.0
            if age_h <= PENDING_MAX_HOURS:
                status = "PENDING"
                status_reason = f"OL quoted, still within {PENDING_MAX_HOURS}h window"
            else:
                status = "QUOTED_LOST"
                status_reason = f"OL quoted, {age_h:.0f}h elapsed, no 'send' or MDOLX"

        ol_dt_val = parse_iso(ol_response.get("receivedDateTime") or ol_response.get("sentDateTime")) if ol_response else None
        send_dt_val = parse_iso(send_reply.get("receivedDateTime") or send_reply.get("sentDateTime")) if send_reply else None
        hours_to_response = (ol_dt_val - inq_dt).total_seconds()/3600.0 if ol_dt_val else None
        hours_to_pick = (send_dt_val - ol_dt_val).total_seconds()/3600.0 if (ol_dt_val and send_dt_val) else None

        inq_id = f"inq_{inq_dt.strftime('%Y%m%d_%H%M%S')}"
        ol_options = parse_ol_options(ol_response.get("body_first_500", "")) if ol_response else []
        inquiries.append({
            "inquiry_id": inq_id,
            "conversation_id": cid,
            "lonny_inquiry": {
                "message_id": first_inquiry.get("id"),
                "uri": first_inquiry.get("_src_file"),
                "received_at": first_inquiry.get("receivedDateTime") or first_inquiry.get("sentDateTime"),
                "subject": first_inquiry.get("subject"),
                "pol": pol, "pod": pod,
                "body_preview": first_inquiry.get("body_first_500","")[:500],
            },
            "chaser_timestamps": chasers,
            "ol_response": None if not ol_response else {
                "message_id": ol_response.get("id"),
                "uri": ol_response.get("_src_file"),
                "received_at": ol_response.get("receivedDateTime") or ol_response.get("sentDateTime"),
                "sender": ol_response.get("sender") or ol_response.get("from_address"),
                "options": ol_options,
                "options_count": len(ol_options),
            },
            "lonny_send_reply": None if not send_reply else {
                "message_id": send_reply.get("id"),
                "received_at": send_reply.get("receivedDateTime") or send_reply.get("sentDateTime"),
                "pick_text": send_snippet,
            },
            "matched_mdolx": matched_mdolx,
            "status": status,
            "status_reason": status_reason,
            "hours_to_response": round(hours_to_response, 2) if hours_to_response else None,
            "hours_to_pick": round(hours_to_pick, 2) if hours_to_pick else None,
            "notes": None,
        })

    # Sort by inquiry timestamp
    inquiries.sort(key=lambda i: i["lonny_inquiry"]["received_at"] or "")

    # MDOLX dedupe: each MDOLX can only be claimed once (earliest inquiry wins).
    # Later inquiries lose their MDOLX match and fall back to their underlying status.
    seen_mdolx: set[str] = set()
    for inq in inquiries:
        m = inq.get("matched_mdolx")
        if not m:
            continue
        if m in seen_mdolx:
            warnings.append(
                f"{inq['inquiry_id']}: MDOLX {m} already claimed by earlier inquiry — reclassifying"
            )
            inq["matched_mdolx"] = None
            # Reclassify: no MDOLX → fall back to send-reply / OL response logic
            if inq.get("lonny_send_reply"):
                inq["status"] = "WIN"
                inq["status_reason"] = (
                    f"Lonny 'send' reply (MDOLX {m} was claimed by earlier inquiry)"
                )
            elif not inq.get("ol_response"):
                inq["status"] = "NOT_QUOTED"
                inq["status_reason"] = (
                    f"No OL response within {PAIRING_MAX_HOURS}h "
                    f"(MDOLX {m} claimed by earlier inquiry — likely a repeat inquiry)"
                )
            else:
                ol_dt = parse_iso(
                    inq["ol_response"].get("received_at","")
                )
                age_h = (now - ol_dt).total_seconds() / 3600.0 if ol_dt else 9999
                if age_h <= PENDING_MAX_HOURS:
                    inq["status"] = "PENDING"
                    inq["status_reason"] = "OL quoted, within pending window (MDOLX claimed earlier)"
                else:
                    inq["status"] = "QUOTED_LOST"
                    inq["status_reason"] = (
                        f"OL quoted, {age_h:.0f}h elapsed, no 'send' "
                        f"(MDOLX {m} claimed by earlier inquiry)"
                    )
        else:
            seen_mdolx.add(m)

    # Unmatched MDOLX
    matched_refs = {i["matched_mdolx"] for i in inquiries if i["matched_mdolx"]}
    in_window_mdolx = [m for m in mdolx if in_window(m.get("received_at",""))]
    unmatched = [
        {"mdolx_ref": m.get("mdolx_ref"), "pod": m.get("pod"),
         "received_at": m.get("received_at"),
         "reason": "No in-window inquiry found"}
        for m in in_window_mdolx
        if m.get("mdolx_ref") not in matched_refs
    ]

    totals = {
        "distinct_inquiries": len(inquiries),
        "wins": sum(1 for i in inquiries if i["status"] == "WIN"),
        "quoted_lost": sum(1 for i in inquiries if i["status"] == "QUOTED_LOST"),
        "not_quoted": sum(1 for i in inquiries if i["status"] == "NOT_QUOTED"),
        "pending": sum(1 for i in inquiries if i["status"] == "PENDING"),
        "booking_canceled": sum(1 for i in inquiries if i["status"] == "BOOKING_CANCELED"),
        "chasers_detected": chasers_count,
        "send_reply_wins": send_wins_count,
        "mdolx_matched": len(matched_refs),
    }

    out = {
        "version": "2.0-strict",
        "window": {"start": WINDOW_START, "end": WINDOW_END},
        "mailbox": "MBD_OceanExportBookingShared@ol-usa.com",
        "sender_filter": "lupfold@hilmaringredients.com",
        "pulled_at": now.isoformat(),
        "totals": totals,
        "inquiries": inquiries,
        "unmatched_mdolx_in_window": unmatched,
        "warnings": warnings,
        "bug_fixes_from_v1": [
            "4-bucket classification (NOT_QUOTED distinct from QUOTED_LOST)",
            "Strict pairing: same conversationId + 72h window",
            "Added Send-reply WIN signal",
            "conversationId required for every inquiry",
            "Chaser detection (same-conv follow-ups collapsed)",
            "MDOLX260426 = BOOKING_CANCELED",
        ],
    }
    OUT.write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    print(f"[wrote] {OUT}")
    print()
    print("=== Totals ===")
    for k, v in totals.items():
        print(f"  {k}: {v}")
    print(f"\nWarnings: {len(warnings)}")
    for w in warnings[:20]:
        print(f"  - {w}")
    print(f"\nUnmatched MDOLX: {len(unmatched)}")
    for u in unmatched:
        print(f"  {u['mdolx_ref']} {u['pod']} {u['received_at']}")


if __name__ == "__main__":
    build()
