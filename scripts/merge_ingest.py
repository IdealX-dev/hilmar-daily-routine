#!/usr/bin/env python3
"""
merge_ingest.py — idempotent merge of scripts/ingest_extract.json into tracking-data-v2.json.

Uses core.request_id() for dedup and core.decide_status() for status assignment.
NEVER overwrites filled fields with None.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import core  # noqa: E402

ROOT = Path(__file__).parent.parent
EXTRACT_PATH = ROOT / "scripts" / "ingest_extract.json"
DATA_PATH = ROOT / "tracking-data-v2.json"


def merge_record(rec: dict, existing_requests: list[dict]) -> tuple[str, dict]:
    """
    Returns (action, request_dict) where action in {"created", "updated", "noop"}.
    """
    conv_id = rec.get("conversationId")
    req_ts = rec.get("request_timestamp")
    dest = rec.get("destination")

    rid = core.request_id(conv_id, req_ts, dest)

    # turnaround
    req_dt = core.parse_iso(req_ts)
    resp_dt = core.parse_iso(rec.get("response_timestamp"))
    turnaround_hours = None
    turnaround_biz_hours = None
    if req_dt and resp_dt:
        turnaround_hours = round((resp_dt - req_dt).total_seconds() / 3600.0, 2)
        turnaround_biz_hours = core.biz_hours_between(req_dt, resp_dt)

    # after-hours flag
    after_hours = False
    if req_dt:
        et = req_dt.astimezone(core.ET)
        # Monday=0 .. Sunday=6; biz hours Mon-Fri 8:30-17:30 ET
        if et.weekday() >= 5:
            after_hours = True
        else:
            t = et.time()
            if t < core.BIZ_START or t >= core.BIZ_END:
                after_hours = True

    # etd fit
    etd_fit_days = None
    # We don't always have a requested ETD to compare, so skip unless present.

    # status decision
    decision = core.decide_status(
        has_send=bool(rec.get("has_send")),
        mdolx_ref=rec.get("mdolx_ref"),
        response_timestamp=rec.get("response_timestamp"),
        quoted=bool(rec.get("quoted")),
        etd_fit_days=etd_fit_days,
    )

    # Loss reason already captured in decision.loss_reason

    # PT / ET strings
    lonny_pt = None
    olusa_et = None
    if req_dt:
        lonny_pt = req_dt.astimezone(core.PT).strftime("%-I:%M %p PT")
    if resp_dt:
        olusa_et = resp_dt.astimezone(core.ET).strftime("%-I:%M %p ET")

    # Canonicalize carrier names so CMA / CMA-CGM / CMA CGM all collapse to "CMA CGM"
    carrier_quoted_norm = core.normalize_carrier(rec.get("carrier_quoted"))

    new_entry = {
        "request_id": rid,
        "conversationId": conv_id,
        "request_date": (req_ts or "")[:10],
        "request_timestamp": req_ts,
        "subject": rec.get("subject"),
        "origin": rec.get("origin"),
        "destination": dest,
        "lane": rec.get("lane"),
        "containers": rec.get("containers"),
        "container_count": rec.get("container_count"),
        "teu_requested": rec.get("teu_requested"),
        "product": rec.get("product"),
        "temperature": rec.get("temperature"),
        "requested_dates": rec.get("requested_dates"),
        "eta_requested": None,
        "response_timestamp": rec.get("response_timestamp"),
        "ol_responder": rec.get("ol_responder"),
        "ol_responder_signer": rec.get("ol_responder_signer"),
        "quoted": bool(rec.get("quoted")),
        "carrier_quoted": carrier_quoted_norm,
        "vessel_offered": rec.get("vessel_offered"),
        "ol_rate": rec.get("ol_rate"),
        "etd_offered": rec.get("etd_offered"),
        "eta_offered": rec.get("eta_offered"),
        "transshipment": rec.get("transshipment"),
        "erd": rec.get("erd"),
        "doc_cutoff": rec.get("doc_cutoff"),
        "port_cutoff": rec.get("port_cutoff"),
        "origin_free_time": rec.get("origin_free_time"),
        "dest_free_time": rec.get("dest_free_time"),
        "turnaround_hours": turnaround_hours,
        "turnaround_biz_hours": turnaround_biz_hours,
        "after_hours_request": after_hours,
        "status": decision.status,
        "has_send": bool(rec.get("has_send")),
        "carrier_won": carrier_quoted_norm if decision.status == "WIN" else None,
        "vessel": rec.get("vessel_offered") if decision.status == "WIN" else None,
        "teu_won": rec.get("teu_requested") if decision.status == "WIN" else 0,
        "mdolx_ref": rec.get("mdolx_ref"),
        "mdolx_date": None,
        "loss_reason": decision.loss_reason,
        "etd_fit_days": etd_fit_days or 0,
        "notes": rec.get("notes"),
        "status_history": [
            {
                "at": datetime.now(timezone.utc).isoformat(),
                "from": None,
                "to": decision.status,
                "reason": decision.reason_detail,
            }
        ],
        "date": (req_ts or "")[:10],
        "lonny_time_pt": lonny_pt,
        "olusa_time_et": olusa_et,
    }

    # Look up existing by request_id
    for i, r in enumerate(existing_requests):
        if r.get("request_id") == rid:
            # Idempotent update — fill missing only; never overwrite filled with None
            changed = False
            for k, v in new_entry.items():
                if k in ("request_id", "status_history"):
                    continue
                old_v = r.get(k)
                if (old_v is None or old_v == "" or old_v == 0) and v not in (None, "", 0):
                    r[k] = v
                    changed = True
                # For status, let decide_status rule
                if k == "status" and v and v != old_v:
                    # Only overwrite status if new decision is derived from richer inputs
                    # (i.e., the existing record was missing response_timestamp but we now have one)
                    if (not r.get("response_timestamp")) and new_entry.get("response_timestamp"):
                        r["status"] = v
                        r.setdefault("status_history", []).append({
                            "at": datetime.now(timezone.utc).isoformat(),
                            "from": old_v,
                            "to": v,
                            "reason": decision.reason_detail + " (from ingest update)",
                        })
                        changed = True
            return ("updated" if changed else "noop", r)

    # New entry
    existing_requests.append(new_entry)
    return ("created", new_entry)


def main():
    with open(EXTRACT_PATH, "r") as f:
        extract = json.load(f)
    with open(DATA_PATH, "r") as f:
        data = json.load(f)

    records = extract.get("records", [])
    requests = data.setdefault("requests", [])

    created = updated = noop = 0
    for rec in records:
        action, _ = merge_record(rec, requests)
        if action == "created":
            created += 1
        elif action == "updated":
            updated += 1
        else:
            noop += 1

    # Update metadata
    data["last_updated"] = datetime.now(timezone.utc).isoformat()
    data["date_range"] = f"{extract['window']['start']} to {extract['window']['end']} — merged ingest"
    data["ingest_notes"] = (
        f"Merged {len(records)} records from ingest_extract.json "
        f"(created={created}, updated={updated}, noop={noop}). "
        f"Warnings: {'; '.join(extract.get('warnings', []))[:300]}"
    )

    with open(DATA_PATH, "w") as f:
        json.dump(data, f, indent=2, default=str)

    print(f"Merged: created={created} updated={updated} noop={noop} total={len(records)}")


if __name__ == "__main__":
    main()
