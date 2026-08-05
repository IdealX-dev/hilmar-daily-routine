#!/usr/bin/env python3
"""
build_ingest_extract.py
Combine ingest_pairs.json (80 matched + 22 unmatched) with ingest_extract_v2.json
(40 response bodies with parsed booking tables) to build the canonical
scripts/ingest_extract.json that merge_ingest.py consumes.

Schema produced (dict):
{
  "pipeline": "hilmar_rate_desk_daily",
  "generated_at": ISO,
  "window": {"start": "...", "end": "..."},
  "records": [ {request-centric record}, ... ]
}

Each record:
  conversationId (synthetic: request.id)
  request_timestamp, subject, origin, destination, lane
  containers, container_count, teu_requested, product, temperature, requested_dates
  response_timestamp, ol_responder, ol_responder_signer
  quoted, carrier_quoted, vessel_offered, ol_rate, etd_offered, eta_offered,
  transshipment, erd, doc_cutoff, port_cutoff, origin_free_time, dest_free_time
  has_send (False unless MDOLX ref present), mdolx_ref, notes
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import core  # noqa: E402

ROOT = Path(__file__).parent.parent
PAIRS = ROOT / "scripts" / "ingest_pairs.json"
EXTRACT_V2 = ROOT / "scripts" / "ingest_extract_v2.json"
OUT = ROOT / "scripts" / "ingest_extract.json"

# ── Helpers ─────────────────────────────────────────────────────────────

DEST_RX_LIST = [
    re.compile(r"(?:[Oo]akland|Dalhart)\s+to\s+([A-Za-z][A-Za-z0-9 \(\)\.'’\-]*?)(?://|$|\s{2,}|_|\s*-\s|!|\?|\s+PORT\b|\s+\(North\)|\s+\(Cat\s+Lai\s+Port\)|\s+\(.*?\))", re.IGNORECASE),
    re.compile(r"(?:[Oo]akland|Dalhart)\s+to\s+([A-Za-z][A-Za-z0-9 \(\)\.'’\-]*)$", re.IGNORECASE),
]
SUBJECT_CLEAN = re.compile(r"^\s*(?:RE:|FW:|FWD:)\s*", re.IGNORECASE)
ORIGIN_RX = re.compile(r"\b(Oakland|Dalhart)\b", re.IGNORECASE)

CONTAINER_RX = re.compile(
    r"(\d+)\s*[×x\-]?\s*(\d{2})['\u2019\s]*(HC|RF|DV|GP|RE|RH|FR|OT|NOR)?",
    re.IGNORECASE,
)
PROD_RX = re.compile(r"\b(MPC|WMP|SMP|WPC|MPI|NFDM|Whey|BUTTER|butter|BMP|Milk Powder|Cheese|cheddar|mozz(?:arella)?)\b", re.IGNORECASE)
TEMP_RX = re.compile(r"(REEFER|FROZEN|CHILLED|DRY|\-\s*\d+\s*F|\d+\s*F\s+REEFER|\d+°F)", re.IGNORECASE)
MDOLX_RX = re.compile(r"(MDOLX\d+)", re.IGNORECASE)
# Carrier-in-subject patterns like "CMA BKG", "HMM BKG #", "MSC BKG #"
CARRIER_SUBJ_RX = re.compile(
    r"\b(CMA|CMACGM|CMA[-\s]CGM|ANL|APL|MSC|MAERSK|MAEU|MSCU|MSK|HMM|HDMU|HYUNDAI|ONE|ONEY|COSCO|OOCL|EVERGREEN|EMC|HAPAG|HLAG|YANG\s*MING|YML|ZIM|WAN\s*HAI|WHL)\s*(?:BKG|BOOKING|:)",
    re.IGNORECASE,
)

# Scope exclusions at subject level (additional to sender-based scope)
SUBJECT_EXCLUDE_RX = re.compile(r"\bNUMIDIA\b", re.IGNORECASE)

# WIN-trigger MDOLX subjects — booking confirmations only, NOT free-time disputes or schedule asks
WIN_MDOLX_RX = re.compile(
    r"MDOLX\d+.*?(BOOKING\s*CONFIRMATION|CONFIRMATION|\bBKG\s*#)",
    re.IGNORECASE,
)
# Non-WIN MDOLX subjects — operational follow-ups after a booking already won
NON_WIN_MDOLX_RX = re.compile(
    r"MDOLX\d+.*?(FREE[-\s]*TIME|SCHEDULE|LOADING\s+APPT|DISPUTE|CLAIM|RELEASE|INVOICE)",
    re.IGNORECASE,
)

# Admin/operational subjects that are NOT rate requests — skip entirely
NOT_A_RATE_REQUEST_RX = re.compile(
    r"^\s*(?:RE:\s*|FW:\s*)?(?:"
    r"MDOLX\d+.*?(?:FREE[-\s]*TIME|SCHEDULE|LOADING\s+APPT|DISPUTE|CLAIM|RELEASE|INVOICE|BKG\s*#|BOOKING\s*CONFIRMATION)"
    r"|pls\s+claim"
    r"|claim\s+"
    r"|REEFER\s+FREE\s+TIME"
    r"|EBKG\d+\s*$"
    r")",
    re.IGNORECASE,
)

def parse_destination(subject: str) -> str | None:
    s = SUBJECT_CLEAN.sub("", subject or "").strip()
    # Simple: split on "to " and take the rest up to // or MDOLX or end
    m = re.search(r"\b(?:[Oo]akland|Dalhart)\s+to\s+(.+?)(?:\s*//|\s*MDOLX|$)", s)
    if m:
        dest = m.group(1).strip()
        dest = re.sub(r"\s{2,}", " ", dest)
        dest = re.sub(r"[\s\-]+$", "", dest)
        dest = re.sub(r"^\-\s*", "", dest)
        return dest or None
    # For MDOLX subjects: "MDOLX...// HILMAR 1X20'DV Oakland to Port Klang"
    m2 = re.search(r"Oakland\s+to\s+([A-Za-z][A-Za-z \(\)\.'’\-]+?)(?:\s*//|$)", s, re.IGNORECASE)
    if m2:
        return m2.group(1).strip()
    return None

def parse_origin(subject: str) -> str:
    m = ORIGIN_RX.search(subject or "")
    if m:
        return m.group(1).title()
    return "Oakland"

def parse_containers(text: str | None) -> tuple[str | None, int, int]:
    """Returns (container_string, count, teu). Looks through summary text."""
    if not text:
        return None, 0, 0
    hits = []
    for m in CONTAINER_RX.finditer(text):
        qty = int(m.group(1))
        size = int(m.group(2))
        if size not in (20, 40, 45):
            continue
        kind = (m.group(3) or "").upper() or "DV"
        hits.append((qty, size, kind))
    if not hits:
        return None, 0, 0
    # Merge duplicates
    agg: dict[tuple[int,str], int] = {}
    for qty, size, kind in hits:
        key = (size, kind)
        agg[key] = agg.get(key, 0) + qty
    parts = []
    count = 0
    teu = 0
    for (size, kind), qty in sorted(agg.items()):
        parts.append(f"{qty}×{size}'{kind}")
        count += qty
        teu += qty * (2 if size >= 40 else 1)
    return ", ".join(parts), count, teu

def parse_product(text: str | None) -> str | None:
    if not text:
        return None
    m = PROD_RX.search(text)
    return m.group(1) if m else None

def parse_temp(text: str | None) -> str | None:
    if not text:
        return None
    m = TEMP_RX.search(text)
    if not m: return None
    raw = m.group(1).upper()
    return raw

def parse_mdolx(subject: str | None, notes: str | None = None) -> str | None:
    for src in (subject, notes):
        if not src: continue
        m = MDOLX_RX.search(src)
        if m: return m.group(1).upper()
    return None

def parse_subject_carrier(subject: str | None) -> str | None:
    """Pull carrier name from subject patterns like 'CMA BKG #', 'HMM BKG #', '// CMA:'."""
    if not subject:
        return None
    m = CARRIER_SUBJ_RX.search(subject)
    if not m:
        return None
    raw = m.group(1)
    return core.normalize_carrier(raw)

def subject_is_win_trigger(subject: str | None) -> bool:
    """True if this MDOLX subject is a real booking-confirmation (WIN), not a post-booking admin thread."""
    if not subject:
        return False
    if NON_WIN_MDOLX_RX.search(subject):
        return False
    if WIN_MDOLX_RX.search(subject):
        return True
    # Plain "MDOLXNNN_" prefix without any admin keyword = booking confirmation by convention
    if re.match(r"^\s*(?:RE:\s*|FW:\s*)?MDOLX\d+_?\b", subject, re.IGNORECASE):
        return not NON_WIN_MDOLX_RX.search(subject)
    return False

def is_out_of_scope_subject(subject: str | None) -> bool:
    return bool(subject and SUBJECT_EXCLUDE_RX.search(subject))

def is_not_a_rate_request(subject: str | None) -> bool:
    """True for admin/operational subjects that shouldn't be in the rate-request tracker."""
    return bool(subject and NOT_A_RATE_REQUEST_RX.search(subject))

# ── Main ────────────────────────────────────────────────────────────────

def main():
    pairs_doc = json.loads(PAIRS.read_text(encoding="utf-8"))
    extract_v2 = json.loads(EXTRACT_V2.read_text(encoding="utf-8"))

    # Index extract_v2 by response id (the email id)
    extract_by_id: dict[str, dict] = {r.get("id"): r for r in extract_v2 if r.get("id")}

    records = []
    all_ts = []
    skipped_numidia = 0
    skipped_admin = 0

    # ── 80 matched pairs ────────────────────────────────────────────────
    for pair in pairs_doc["pairs"]:
        req = pair["request"]
        resp = pair["response"]

        # Subject-level scope exclusion (NUMIDIA etc.)
        if is_out_of_scope_subject(req.get("subject")) or is_out_of_scope_subject(resp.get("subject")):
            skipped_numidia += 1
            continue
        # Skip admin/operational subjects — these are post-booking threads, not rate requests
        if is_not_a_rate_request(req.get("subject")):
            skipped_admin += 1
            continue

        req_ts = req.get("sentDateTime") or req.get("receivedDateTime")
        resp_ts = resp.get("sentDateTime") or resp.get("receivedDateTime")
        subj = req.get("subject") or ""
        origin = parse_origin(subj)
        dest = parse_destination(subj)
        containers_str, container_count, teu = parse_containers(req.get("summary",""))
        product = parse_product(req.get("summary",""))
        temperature = parse_temp(req.get("summary",""))

        # Join response table
        ext = extract_by_id.get(resp.get("id"), {})
        tbl = (ext.get("table") or {}) if ext else {}
        has_table = bool(ext.get("has_table"))

        carrier_quoted = tbl.get("carrier") if has_table else None
        rate = tbl.get("rate") if has_table else None
        vessel = tbl.get("vessel") if has_table else None
        etd = tbl.get("etd") if has_table else None
        eta = tbl.get("eta") if has_table else None
        transshipment = tbl.get("transshipment") if has_table else None
        erd = tbl.get("erd") if has_table else None
        doc_cut = tbl.get("doc_cut") if has_table else None
        port_cut = tbl.get("port_cut") if has_table else None
        oft = tbl.get("origin_free_time") if has_table else None
        dft = tbl.get("dest_free_time") if has_table else None

        # MDOLX can come from either the request subject or the response subject,
        # but we only treat it as a WIN-trigger if the subject is a real booking confirmation.
        req_subj_ext = subj
        resp_subj_ext = ext.get("subject") or resp.get("subject") or ""
        mdolx_req = parse_mdolx(req_subj_ext)
        mdolx_resp = parse_mdolx(resp_subj_ext)
        mdolx = mdolx_req or mdolx_resp
        # WIN trigger = MDOLX present AND at least one side is a booking-confirmation subject
        mdolx_is_win = bool(mdolx) and (subject_is_win_trigger(req_subj_ext) or subject_is_win_trigger(resp_subj_ext))

        # Subject-based carrier fallback (covers "MDOLX...// CMA: NAM..." and "MDOLXNNN_ CMA BKG # NAM..." patterns)
        subj_carrier_req = parse_subject_carrier(req_subj_ext)
        subj_carrier_resp = parse_subject_carrier(resp_subj_ext)
        subj_carrier = subj_carrier_req or subj_carrier_resp
        # ol_responder: if extract says MBD_OceanExportBookingShared -> "OL Booking Team"
        sender = (ext.get("sender") or resp.get("sender") or "").lower()
        if "mbd_oceanexportbookingshared" in sender:
            ol_responder = "MBD Ocean Export Booking"
        elif sender:
            # Use the visible name component
            ol_responder = sender.split("@")[0].replace(".", " ").title()
        else:
            ol_responder = "OL Booking Team"
        ol_responder_signer = ol_responder

        notes = ext.get("notes") or None
        quoted = bool(has_table and (carrier_quoted or rate or vessel))

        # Fill carrier from subject when table missed it AND subject contains carrier ref
        if not carrier_quoted and subj_carrier:
            carrier_quoted = subj_carrier
            notes = (notes + " | " if notes else "") + f"carrier inferred from subject ({subj_carrier})"

        # has_send: True only when this is a real booking-confirmation MDOLX thread
        has_send = bool(mdolx_is_win)
        # Only set mdolx_ref if it's a WIN-trigger; operational MDOLX threads (free-time, schedule) should NOT flip to WIN
        effective_mdolx = mdolx if mdolx_is_win else None

        record = {
            "conversationId": req["id"],
            "request_timestamp": req_ts,
            "response_timestamp": resp_ts,
            "subject": subj,
            "origin": origin,
            "destination": dest,
            "lane": f"{origin} → {dest}" if dest else origin,
            "containers": containers_str,
            "container_count": container_count,
            "teu_requested": teu,
            "product": product,
            "temperature": temperature,
            "requested_dates": None,
            "ol_responder": ol_responder,
            "ol_responder_signer": ol_responder_signer,
            "quoted": quoted,
            "carrier_quoted": carrier_quoted,
            "vessel_offered": vessel,
            "ol_rate": rate,
            "etd_offered": etd,
            "eta_offered": eta,
            "transshipment": transshipment,
            "erd": erd,
            "doc_cutoff": doc_cut,
            "port_cutoff": port_cut,
            "origin_free_time": oft,
            "dest_free_time": dft,
            "has_send": has_send,
            "mdolx_ref": effective_mdolx,
            "notes": notes,
        }
        records.append(record)
        if req_ts: all_ts.append(req_ts)
        if resp_ts: all_ts.append(resp_ts)

    # ── 22 unmatched requests (no OL response visible) ──────────────────
    for req in pairs_doc["unmatched_requests"]:
        if is_out_of_scope_subject(req.get("subject")):
            skipped_numidia += 1
            continue
        if is_not_a_rate_request(req.get("subject")):
            skipped_admin += 1
            continue
        req_ts = req.get("sentDateTime") or req.get("receivedDateTime")
        subj = req.get("subject") or ""
        origin = parse_origin(subj)
        dest = parse_destination(subj)
        containers_str, container_count, teu = parse_containers(req.get("summary",""))
        product = parse_product(req.get("summary",""))
        temperature = parse_temp(req.get("summary",""))
        # For unmatched requests: only count MDOLX as WIN-trigger if it's a real booking subject
        mdolx_raw = parse_mdolx(subj)
        mdolx = mdolx_raw if (mdolx_raw and subject_is_win_trigger(subj)) else None
        subj_carrier_um = parse_subject_carrier(subj)

        record = {
            "conversationId": req["id"],
            "request_timestamp": req_ts,
            "response_timestamp": None,
            "subject": subj,
            "origin": origin,
            "destination": dest,
            "lane": f"{origin} → {dest}" if dest else origin,
            "containers": containers_str,
            "container_count": container_count,
            "teu_requested": teu,
            "product": product,
            "temperature": temperature,
            "requested_dates": None,
            "ol_responder": None,
            "ol_responder_signer": None,
            "quoted": False,
            "carrier_quoted": subj_carrier_um,
            "vessel_offered": None,
            "ol_rate": None,
            "etd_offered": None,
            "eta_offered": None,
            "transshipment": None,
            "erd": None,
            "doc_cutoff": None,
            "port_cutoff": None,
            "origin_free_time": None,
            "dest_free_time": None,
            "has_send": False,
            "mdolx_ref": mdolx,
            "notes": "NO_RESPONSE — no OL-USA reply visible in delegated inbox",
        }
        records.append(record)
        if req_ts: all_ts.append(req_ts)

    # Window
    window_start = min(all_ts)[:10] if all_ts else "2026-01-01"
    window_end = max(all_ts)[:10] if all_ts else "2026-12-31"

    out = {
        "pipeline": "hilmar_rate_desk_daily",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "window": {"start": window_start, "end": window_end},
        "source_mailbox_requests": "lupfold@hilmaringredients.com",
        "source_mailbox_responses": "Any @ol-usa.com (all via MBD_OceanExportBookingShared delegated); excluded: mbd_export_pricing, mbd_exportdocsshared, bvann@hilmarcheese, rkumar",
        "records": records,
        "warnings": [
            f"{pairs_doc['unmatched_requests_count']} unmatched requests present as NO_RESPONSE losses",
            f"{len([r for r in records if r['quoted'] and not r['carrier_quoted']])} quoted records missing carrier cell",
        ],
    }

    OUT.write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")

    print(f"wrote {OUT}")
    print(f"skipped NUMIDIA: {skipped_numidia}")
    print(f"skipped admin/post-booking: {skipped_admin}")
    print(f"total records: {len(records)}")
    print(f"  quoted: {sum(1 for r in records if r['quoted'])}")
    print(f"  no-response: {sum(1 for r in records if not r['response_timestamp'])}")
    print(f"  mdolx_ref set: {sum(1 for r in records if r['mdolx_ref'])}")
    from collections import Counter
    carriers = Counter(r['carrier_quoted'] for r in records if r['carrier_quoted'])
    print("  carriers quoted:")
    for c, n in carriers.most_common():
        print(f"    {c}: {n}")

if __name__ == "__main__":
    main()
