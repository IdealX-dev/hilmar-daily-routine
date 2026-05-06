#!/usr/bin/env python3
"""
build_real_sample.py — Seed tracking-data-v2.json with a small, fully-verified
real-thread sample (from Outlook ingest) so the pipeline can be exercised
end-to-end against authentic data before the full orchestrator ingest runs.

This is NOT a substitute for scripts/ingest (which is Claude's job via MCP).
It's a one-shot seeder for the initial dry-run.

Sample included:
  - Oakland → Manila (North) — Apr 16, 2026 — 1×20' Dairy Lactose
    - Quote came back 14 min later from MBD Ocean Export Booking
    - Two options presented (CMA $2040 via Shanghai / ONE $1015 via Pusan)
    - Status = PENDING (within 24h window, no Send yet)
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import core  # noqa

# ─────────────────────────────────────────────────────────────────────────────
# Thread 1 — Oakland → Manila (North)
# ─────────────────────────────────────────────────────────────────────────────

manila_conv_id = "AAQkAGQ3MjcwNWZhLTk3M2YtNDYzOS1hMWZlLWQwMmYzODE0MTU5NQAQAKhptuZ9dXlJsvDHRW2ubYA="
manila_req_ts = "2026-04-16T21:49:55Z"
manila_resp_ts = "2026-04-16T22:03:04Z"

req_id = core.request_id(manila_conv_id, manila_req_ts, "Manila (North), PH")
count, teu = core.parse_teu("1×20'DV")

biz_hours = core.biz_hours_between(
    core.parse_iso(manila_req_ts),
    core.parse_iso(manila_resp_ts),
)
clock_hours = core.clock_hours_between(
    core.parse_iso(manila_req_ts),
    core.parse_iso(manila_resp_ts),
)

etd_fit = core.etd_fit_days("2026-04-25", "2026-04-25")  # Lonny asked "cutoff next week"; OL Option 1 ETD 25-Apr

decision = core.decide_status(
    has_send=False,
    mdolx_ref=None,
    response_timestamp=manila_resp_ts,
    quoted=True,
    etd_fit_days=etd_fit,
    now=core.parse_iso("2026-04-19T18:00:00Z"),  # "now" = current ingest moment
)

manila_request = {
    "request_id": req_id,
    "conversationId": manila_conv_id,
    "request_date": "2026-04-16",
    "request_timestamp": manila_req_ts,
    "subject": "Oakland to Manila (North)",
    "origin": "Oakland, CA",
    "destination": "Manila (North), PH",
    "lane": "Oakland → Manila (North)",
    "containers": "1×20'DV",
    "container_count": count,
    "teu_requested": teu,
    "product": "Dairy — Lactose",
    "temperature": None,
    "requested_dates": "cutoff next week or the following week",
    "eta_requested": None,
    "response_timestamp": manila_resp_ts,
    "ol_responder": "MBD Ocean Export Booking",
    "ol_responder_signer": "Alexandra Hernandez",
    "quoted": True,
    # Lonny asked OL to quote both weeks — OL returned 2 options on 2 carriers.
    # We record the primary (Option 1 / CMA via Shanghai) as the headline quote,
    # and keep the full options list in notes for the carrier scorecard.
    "carrier_quoted": "CMA CGM",
    "vessel_offered": "EVER LEGION 0TBNEW1MA",
    "ol_rate": "$2,040/20DV",
    "etd_offered": "2026-04-25",
    "eta_offered": "2026-05-30",
    "transshipment": "Shanghai",
    "erd": "2026-04-17",
    "doc_cutoff": None,
    "port_cutoff": None,
    "origin_free_time": "4 det + 5 dem",
    "dest_free_time": "21 days combined",
    "turnaround_hours": clock_hours,
    "turnaround_biz_hours": biz_hours,
    "after_hours_request": core.is_after_hours_et(core.parse_iso(manila_req_ts)),
    "status": decision.status,
    "has_send": decision.has_send,
    "carrier_won": None,
    "vessel": None,
    "teu_won": 0,
    "mdolx_ref": None,
    "mdolx_date": None,
    "loss_reason": decision.loss_reason,
    "etd_fit_days": etd_fit,
    "notes": (
        "OL returned 2 options: "
        "Opt 1 — CMA CGM EVER LEGION, ETD 25-Apr, ETA 30-May, $2040/20DV via Shanghai; "
        "Opt 2 — ONE HMM TURQUOISE, ETD 3-May, ETA 29-May, $1015/20DV via Pusan."
    ),
    "status_history": [
        {
            "at": manila_resp_ts,
            "from": None,
            "to": decision.status,
            "reason": decision.reason_detail,
        }
    ],
}

# ─────────────────────────────────────────────────────────────────────────────
# Build tracking-data-v2.json payload
# ─────────────────────────────────────────────────────────────────────────────

requests = [manila_request]
summary = core.aggregate_summary(requests)
lanes = core.aggregate_lanes(requests)
carriers = core.aggregate_carriers(requests)

data = {
    "version": "6.0-claude-native",
    "client": "Hilmar Ingredients",
    "contact": "Lonny Upfold",
    "provider": "OL-USA",
    "date_range": "2026-04-16 to 2026-04-19 (sample — 1 verified thread)",
    "last_updated": core.now_utc().isoformat(),
    "ingest_notes": (
        "Initial seed: 1 fully-verified thread (Oakland→Manila North, Apr 16). "
        "Full Apr 1-19 ingest pending — run via orchestrator.md in fresh session."
    ),
    "requests": requests,
    "summary": summary,
    "lane_summary": lanes,
    "carrier_summary": carriers,
    "mdolx_bookings": [],
    "qc": {"status": "pending", "issues": [], "healed": []},
    "escalations_sent": {},
    "metadata": {
        "seed_source": "scripts/build_real_sample.py",
        "seeded_at": core.now_utc().isoformat(),
    },
}

cfg = core.load_config()
out = Path(cfg["paths"]["data"])
core.save_data(data, out)

print(f"✅ Seeded real sample → {out}")
print(f"   Requests: {len(requests)}")
print(f"   Status:   {requests[0]['status']}")
print(f"   Lane:     {requests[0]['lane']}")
print(f"   Carrier:  {requests[0]['carrier_quoted']}")
print(f"   Rate:     {requests[0]['ol_rate']}")
print(f"   Biz-hrs:  {biz_hours}h (clock: {clock_hours}h)")
