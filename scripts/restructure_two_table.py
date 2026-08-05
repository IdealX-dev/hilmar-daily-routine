#!/usr/bin/env python3
"""
restructure_two_table.py — Rebuild tracking-data-v2.json under the correct data model.

Old model (wrong):
  - Single requests[] table with WIN/LOSS status for all 78 rate-desk items.

New model (correct, per Michael 2026-04-20):
  - rate_requests[]  — the 78 rate-desk items sent to Caren Tobel / export pricing.
                       Status vocabulary: QUOTED / NO_RESPONSE.
                       NO win/loss here — rate desk is a different function.
  - bookings[]       — actual booking events. Each MDOLX = 1 row (no collapsing).
                       Status: WIN / LOSS / PENDING / BOOKING_CANCELED.
                       Seeded here from hilmar_booking_confirmations.json (14 MDOLX rows).
                       Ops-flow inquiries (mbd_oceanexport) backfill in next step.

Run: python3 scripts/restructure_two_table.py
"""
from __future__ import annotations

import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent.parent
DATA_PATH = ROOT / "tracking-data-v2.json"
BKG_PATH = ROOT / "scripts" / "hilmar_booking_confirmations.json"

# HCMC + case canonicalization (reuse from link_mdolx_wins)
POD_CANONICAL = {
    "hcmc": "HCMC",
    "hcmc (cat lai port)": "HCMC",
    "ho chi minh": "HCMC",
    "ho chi minh city": "HCMC",
    "ho chi minh (cat lai port)": "HCMC",
}


def title_case_pod(s):
    if not s:
        return s
    key = re.sub(r"\s+", " ", s).strip().lower()
    if key in POD_CANONICAL:
        return POD_CANONICAL[key]
    parts = key.split()
    out = []
    for p in parts:
        if p in ("to", "of", "the", "and", "-"):
            out.append(p)
        elif p.startswith("("):
            inner = p.strip("()")
            out.append("(" + inner.title() + ")")
        else:
            out.append(p.title())
    return " ".join(out)


def normalize_destination(d):
    if not d:
        return None
    d = d.strip()
    if d.lower() in ("china", "unknown", "n/a", "na"):
        return None
    return title_case_pod(d)


def equipment_teu(qty, equipment):
    if qty is None:
        return 0
    eq = (equipment or "").lower()
    if "40" in eq:
        return int(qty) * 2
    if "20" in eq:
        return int(qty) * 1
    return int(qty)


def main():
    with open(DATA_PATH, encoding="utf-8") as f:
        data = json.load(f)
    with open(BKG_PATH, encoding="utf-8") as f:
        bkg = json.load(f)

    now_iso = datetime.now(timezone.utc).isoformat()
    ts_tag = now_iso.replace(":", "").replace(".", "").split("+")[0]
    backup = DATA_PATH.with_suffix(f".pre-restructure-{ts_tag}.json.bak")
    shutil.copy2(DATA_PATH, backup)
    print(f"[backup] wrote {backup.name}")

    old_requests = data.get("requests", [])
    confirmations = bkg.get("confirmations", [])

    # -------- Build rate_requests[] --------
    rate_requests = []
    stats_rr = {"QUOTED": 0, "NO_RESPONSE": 0, "dropped_china": 0, "renormalized_dest": 0}
    for r in old_requests:
        # Normalize destination
        old_dest = r.get("destination")
        new_dest = normalize_destination(old_dest)
        if old_dest and not new_dest:
            stats_rr["dropped_china"] += 1  # destination was junk ("china"/"unknown")
            # Keep the record but null the dest
        if new_dest != old_dest:
            stats_rr["renormalized_dest"] += 1

        # Determine status under rate-desk vocabulary
        if r.get("quoted"):
            rr_status = "QUOTED"
        elif r.get("response_timestamp"):
            rr_status = "QUOTED"  # OL responded; treat as quoted even if quoted flag was false
        else:
            rr_status = "NO_RESPONSE"
        stats_rr[rr_status] = stats_rr.get(rr_status, 0) + 1

        rate_requests.append({
            "request_id": r.get("request_id"),
            "conversationId": r.get("conversationId"),
            "request_date": r.get("request_date"),
            "request_timestamp": r.get("request_timestamp"),
            "subject": r.get("subject"),
            "origin": r.get("origin"),
            "destination": new_dest,
            "lane": r.get("lane"),
            "containers": r.get("containers"),
            "container_count": r.get("container_count"),
            "teu_requested": r.get("teu_requested"),
            "product": r.get("product"),
            "temperature": r.get("temperature"),
            "requested_dates": r.get("requested_dates"),
            "response_timestamp": r.get("response_timestamp"),
            "ol_responder": r.get("ol_responder"),
            "quoted": bool(r.get("quoted")),
            "carrier_quoted": r.get("carrier_quoted"),
            "vessel_offered": r.get("vessel_offered"),
            "ol_rate": r.get("ol_rate"),
            "etd_offered": r.get("etd_offered"),
            "eta_offered": r.get("eta_offered"),
            "transshipment": r.get("transshipment"),
            "erd": r.get("erd"),
            "doc_cutoff": r.get("doc_cutoff"),
            "port_cutoff": r.get("port_cutoff"),
            "origin_free_time": r.get("origin_free_time"),
            "dest_free_time": r.get("dest_free_time"),
            "turnaround_hours": r.get("turnaround_hours"),
            "turnaround_biz_hours": r.get("turnaround_biz_hours"),
            "after_hours_request": r.get("after_hours_request"),
            "status": rr_status,
            "lonny_time_pt": r.get("lonny_time_pt"),
            "olusa_time_et": r.get("olusa_time_et"),
            "notes": r.get("notes"),
            # NOTE: deliberately dropped — these belonged in bookings[]:
            #   status, has_send, carrier_won, vessel, teu_won, mdolx_ref, mdolx_date,
            #   loss_reason, etd_fit_days, status_history, date
        })

    # -------- Build bookings[] --------
    bookings = []
    stats_bk = {"WIN": 0, "BOOKING_CANCELED": 0}
    for c in confirmations:
        mdolx = c.get("mdolx_ref")
        carrier = c.get("carrier")
        bkg_num = c.get("bkg_num")
        pod_raw = c.get("pod")
        pod = normalize_destination(pod_raw)
        qty = c.get("qty")
        equipment = c.get("equipment")
        received_at = c.get("received_at")
        raw_status = (c.get("status") or "CONFIRMED").upper()

        if raw_status == "CANCELED":
            bk_status = "BOOKING_CANCELED"
            teu = 0
        else:
            bk_status = "WIN"  # per Michael: if MDOLX exists, it's a win
            teu = equipment_teu(qty, equipment)
        stats_bk[bk_status] = stats_bk.get(bk_status, 0) + 1

        bookings.append({
            "booking_id": f"bk_{mdolx}",
            "source": "mdolx_confirmation",
            "mdolx_ref": mdolx,
            "carrier_bkg_num": bkg_num,
            "carrier": carrier,
            "pol": c.get("pol"),
            "pod": pod,
            "pod_raw": pod_raw,
            "qty": qty,
            "equipment": equipment,
            "teu": teu,
            "vessel": c.get("vessel"),
            "voyage": c.get("voyage"),
            "received_at": received_at,
            "booking_date": (received_at or "")[:10],
            "status": bk_status,
            "notes": c.get("notes"),
            # Fields reserved for ops-flow backfill:
            "ops_inquiry_conversationId": None,
            "ops_inquiry_timestamp": None,
            "ops_response_timestamp": None,
            "options_sent_count": None,
            "lonny_pick": None,
            "time_to_decision_hours": None,
            "status_history": [
                {"at": now_iso, "from": None, "to": bk_status,
                 "reason": f"seeded from hilmar_booking_confirmations.json (MDOLX {mdolx})"}
            ],
        })

    # -------- Restructure top-level --------
    new_data = {
        "version": "2.0",
        "client": data.get("client"),
        "contact": data.get("contact"),
        "provider": data.get("provider"),
        "date_range": data.get("date_range"),
        "last_updated": now_iso,
        "data_model": "two_table_v1 — rate_requests[] (rate desk activity, Caren/export pricing) + bookings[] (ops-flow booking events, MDOLX)",
        "rate_requests": rate_requests,
        "bookings": bookings,
        "metadata": data.get("metadata") or {},
        "_notes": data.get("_notes"),
        "restructure_stats": {
            "rate_requests": stats_rr,
            "bookings": stats_bk,
            "rate_requests_total": len(rate_requests),
            "bookings_total": len(bookings),
        },
    }

    with open(DATA_PATH, "w", encoding="utf-8") as f:
        json.dump(new_data, f, indent=2, default=str)

    print()
    print("=== Two-Table Restructure Summary ===")
    print(f"rate_requests[]: {len(rate_requests)} rows")
    for k, v in stats_rr.items():
        print(f"    {k}: {v}")
    print(f"bookings[]: {len(bookings)} rows")
    for k, v in stats_bk.items():
        print(f"    {k}: {v}")

    # Lane distribution in bookings
    lane_cnt = {}
    carrier_cnt = {}
    for b in bookings:
        pod = b["pod"] or "UNKNOWN"
        lane_cnt[pod] = lane_cnt.get(pod, 0) + 1
        carrier_cnt[b["carrier"]] = carrier_cnt.get(b["carrier"], 0) + 1
    print()
    print("Booking POD distribution:")
    for k, v in sorted(lane_cnt.items(), key=lambda x: -x[1]):
        print(f"    {k}: {v}")
    print("Booking carrier distribution:")
    for k, v in sorted(carrier_cnt.items(), key=lambda x: -x[1]):
        print(f"    {k}: {v}")


if __name__ == "__main__":
    main()
