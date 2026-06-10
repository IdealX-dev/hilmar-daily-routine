#!/usr/bin/env python3
"""
link_mdolx_wins.py — post-merge enricher.

Reads:
  - tracking-data-v2.json (rate requests)
  - scripts/hilmar_booking_confirmations.json (14 MDOLX booking confirmations)

For each booking confirmation with a resolvable POD:
  - Find the most recent Lonny rate request for that destination within 14 days prior
  - Stamp: status=WIN, carrier_won, mdolx_ref, mdolx_date, vessel, teu_won

Also performs:
  - Case normalization on existing destination fields (Title Case)
  - Canonicalization of HCMC variants to a single canonical form

Writes: tracking-data-v2.json (in-place) with a timestamped backup.

Run: python3 scripts/link_mdolx_wins.py
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

# Canonical POD mapping — title-cased forms + HCMC variants collapse to one
POD_CANONICAL = {
    "hcmc": "HCMC",
    "hcmc (cat lai port)": "HCMC",
    "ho chi minh": "HCMC",
    "ho chi minh city": "HCMC",
    "ho chi minh (cat lai port)": "HCMC",
}


def title_case_pod(s):
    """Normalize a destination string to Title Case, collapsing HCMC variants."""
    if not s:
        return s
    key = re.sub(r"\s+", " ", s).strip().lower()
    if key in POD_CANONICAL:
        return POD_CANONICAL[key]
    # Default: Title Case each word, preserve common lowercase tokens
    parts = key.split()
    out = []
    for p in parts:
        if p in ("to", "of", "the", "and", "-"):
            out.append(p)
        elif p.startswith("("):
            # preserve parens casing: (Cat Lai Port)
            inner = p.strip("()")
            out.append("(" + inner.title() + ")")
        else:
            out.append(p.title())
    return " ".join(out)


def normalize_destination(d):
    """Apply canonical rules for matching + display."""
    if not d:
        return None
    d = d.strip()
    # Drop obvious junk
    if d.lower() in ("china", "unknown", "n/a", "na"):
        return None
    return title_case_pod(d)


def parse_iso(ts):
    if not ts:
        return None
    try:
        s = ts.rstrip("Z")
        if "+" not in s and "-" not in s[-6:]:
            s = s + "+00:00"
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        try:
            return datetime.fromisoformat(ts)
        except Exception:
            return None


def equipment_teu(qty, equipment):
    """Approximate TEU from qty + equipment string."""
    if qty is None:
        return 0
    eq = (equipment or "").lower()
    if "40" in eq:
        return int(qty) * 2
    if "20" in eq:
        return int(qty) * 1
    return int(qty)  # default


def main():
    with open(DATA_PATH) as f:
        data = json.load(f)
    with open(BKG_PATH) as f:
        bkg = json.load(f)
    requests = data.get("requests", [])
    confirmations = bkg.get("confirmations", [])

    now_iso = datetime.now(timezone.utc).isoformat()
    ts_tag = now_iso.replace(":", "").replace(".", "").split("+")[0]
    backup = DATA_PATH.with_suffix(f".pre-link-{ts_tag}.json.bak")
    shutil.copy2(DATA_PATH, backup)
    print(f"[backup] wrote {backup.name}")

    # Step 1: Case-normalize destinations in all requests
    renormalized = 0
    for r in requests:
        old = r.get("destination")
        new = normalize_destination(old)
        if new != old:
            r["destination"] = new
            renormalized += 1
    print(f"[normalize] destination renormalized: {renormalized}")

    # Step 2: Apply MDOLX booking confirmations as WINs
    stats = {
        "confirmations_total": len(confirmations),
        "linked": 0,
        "unlinked_pod_unknown": 0,
        "unlinked_no_match": 0,
        "canceled_confirmations": 0,
        "already_win": 0,
    }
    linked_log = []
    unlinked_log = []

    for c in confirmations:
        mdolx = c.get("mdolx_ref")
        carrier = c.get("carrier")
        bkg_num = c.get("bkg_num")
        pod_raw = c.get("pod")
        pod = normalize_destination(pod_raw)
        qty = c.get("qty")
        equipment = c.get("equipment")
        conf_dt = parse_iso(c.get("received_at"))
        status = (c.get("status") or "CONFIRMED").upper()

        if status == "CANCELED":
            stats["canceled_confirmations"] += 1
            # still try to link but stamp status=BOOKING_CANCELED

        if not pod:
            stats["unlinked_pod_unknown"] += 1
            unlinked_log.append({
                "mdolx": mdolx, "carrier": carrier, "bkg_num": bkg_num,
                "reason": "pod_unknown_or_unattributable", "status": status,
            })
            continue

        # Find the most recent rate request for this POD within 14 days prior
        best = None
        best_delta = None
        for r in requests:
            rdest = normalize_destination(r.get("destination"))
            if rdest != pod:
                continue
            req_dt = parse_iso(r.get("request_timestamp"))
            if not req_dt or not conf_dt:
                continue
            if req_dt > conf_dt:
                continue
            delta = (conf_dt - req_dt).total_seconds()
            if delta > 14 * 86400:
                continue
            if best is None or delta < best_delta:
                best = r
                best_delta = delta

        if not best:
            stats["unlinked_no_match"] += 1
            unlinked_log.append({
                "mdolx": mdolx, "carrier": carrier, "bkg_num": bkg_num,
                "pod": pod, "conf_dt": c.get("received_at"),
                "reason": "no_matching_request_in_14day_window", "status": status,
            })
            continue

        # Stamp WIN (or BOOKING_CANCELED)
        prior_status = best.get("status")
        new_status = "WIN" if status == "CONFIRMED" else "BOOKING_CANCELED"
        if prior_status == "WIN" and new_status == "WIN":
            stats["already_win"] += 1
        else:
            best["status"] = new_status
            stats["linked"] += 1

        best["has_send"] = True
        best["carrier_won"] = carrier if new_status == "WIN" else None
        best["mdolx_ref"] = mdolx
        best["mdolx_date"] = c.get("received_at")
        best["mdolx_bkg_num"] = bkg_num
        teu_won = equipment_teu(qty, equipment)
        best["teu_won"] = teu_won if new_status == "WIN" else 0
        # Keep loss_reason cleared on wins
        if new_status == "WIN":
            best["loss_reason"] = None

        # Append to status_history
        best.setdefault("status_history", []).append({
            "at": now_iso,
            "from": prior_status,
            "to": new_status,
            "reason": f"linked via MDOLX booking confirmation {mdolx} ({carrier} {bkg_num}, {qty}x{equipment}, confirmed {c.get('received_at','')[:10]})",
        })

        linked_log.append({
            "mdolx": mdolx, "carrier": carrier, "bkg_num": bkg_num,
            "pod": pod, "conf_dt": c.get("received_at")[:10],
            "request_id": best.get("request_id"),
            "request_date": best.get("request_date"),
            "conversationId": best.get("conversationId"),
            "new_status": new_status,
            "teu_won": teu_won,
        })

    # Step 3: Recompute carrier_summary / lane_summary from scratch
    carrier_summary = {}
    lane_summary = {}
    for r in requests:
        carrier = r.get("carrier_quoted") or r.get("carrier_won")
        if carrier:
            c = carrier_summary.setdefault(carrier, {"quoted": 0, "won": 0, "teu_won": 0})
            c["quoted"] += 1
            if r.get("status") == "WIN":
                c["won"] += 1
                c["teu_won"] += r.get("teu_won") or 0
        dest = normalize_destination(r.get("destination"))
        if dest:
            ln = lane_summary.setdefault(dest, {"count": 0, "wins": 0})
            ln["count"] += 1
            if r.get("status") == "WIN":
                ln["wins"] += 1

    data["carrier_summary"] = carrier_summary
    data["lane_summary"] = lane_summary
    data["last_updated"] = now_iso
    data["link_mdolx_stats"] = stats

    with open(DATA_PATH, "w") as f:
        json.dump(data, f, indent=2, default=str)

    # Summary print
    print()
    print("=== MDOLX → Rate Request Linker Results ===")
    print(f"confirmations processed: {stats['confirmations_total']}")
    print(f"  linked to request:     {stats['linked']}")
    print(f"  already WIN:           {stats['already_win']}")
    print(f"  canceled bookings:     {stats['canceled_confirmations']}")
    print(f"  unlinked (POD unknown):{stats['unlinked_pod_unknown']}")
    print(f"  unlinked (no match):   {stats['unlinked_no_match']}")

    print()
    print("=== Linked ===")
    for x in linked_log:
        print(f"  [{x['mdolx']}] {x['pod']:20s} {x['new_status']:20s} carrier={x['carrier']:8s} bkg={x['bkg_num']} req_date={x['request_date']} teu={x['teu_won']}")

    if unlinked_log:
        print()
        print("=== Unlinked (flagged) ===")
        for x in unlinked_log:
            print(f"  [{x['mdolx']}] carrier={x.get('carrier')} bkg={x.get('bkg_num')} pod={x.get('pod','?')} reason={x['reason']}")

    # Final status distribution
    stat_dist = {}
    for r in requests:
        s = r.get("status") or "UNKNOWN"
        stat_dist[s] = stat_dist.get(s, 0) + 1
    print()
    print("=== Final status distribution ===")
    for s, n in sorted(stat_dist.items(), key=lambda x: -x[1]):
        print(f"  {s}: {n}")

    print()
    print("=== Carrier summary ===")
    for c, v in sorted(carrier_summary.items(), key=lambda x: -x[1].get("won", 0)):
        print(f"  {c}: quoted={v['quoted']} won={v['won']} teu_won={v['teu_won']}")


if __name__ == "__main__":
    main()
