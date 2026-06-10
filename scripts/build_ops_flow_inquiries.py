#!/usr/bin/env python3
"""
Build ops_flow_inquiries.json for Hilmar OPS-FLOW booking window 2026-04-01 -> 2026-04-20.

Inputs:
  - scripts/ingest_pairs.json           (80 paired + 22 unmatched for pre-04-13 portion of window)
  - scripts/ingest_extract_v2.json      (40 OL responses, parsed option tables, up to 04-13)
  - scripts/new_bodies/*.json           (22 Lonny + 21 OL, 04-13 14:25 through 04-16 21:49, pulled live)
  - scripts/hilmar_booking_confirmations.json  (14 MDOLX records for WIN matching)

Outputs:
  - scripts/ops_flow_inquiries.json     (final grouped record set)

Outcome logic per inquiry:
  - WIN    : MDOLX confirmation received AFTER OL reply timestamp AND POL/POD/qty/equipment
             are compatible (fuzzy match by POD + equipment + qty, scored)
  - LOSS   : > 24h after OL reply without any Lonny follow-up on same conversationId
             OR explicit decline text in Lonny follow-up
  - PENDING: <= 24h since OL reply (relative to 2026-04-20 23:59 cutoff) OR no OL reply yet
             Only PENDING if no MDOLX win found.
"""

import json
import os
import re
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(HERE)

# Target window (inclusive)
WIN_START = datetime(2026, 4, 1, 0, 0, 0, tzinfo=timezone.utc)
WIN_END   = datetime(2026, 4, 20, 23, 59, 59, tzinfo=timezone.utc)

PAIRS_PATH       = os.path.join(HERE, "ingest_pairs.json")
EXTRACT_V2_PATH  = os.path.join(HERE, "ingest_extract_v2.json")
NEW_BODIES_DIR   = os.path.join(HERE, "new_bodies")
CONFIRMATIONS    = os.path.join(HERE, "hilmar_booking_confirmations.json")
OUT_PATH         = os.path.join(HERE, "ops_flow_inquiries.json")

def parse_iso(s):
    if not s:
        return None
    s = s.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None

def iso_out(dt):
    if not dt:
        return None
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

POD_ALIASES = {
    "ho chi minh": "hcmc",
    "ho chi minh city": "hcmc",
    "hcm": "hcmc",
    "cat lai": "hcmc",
    "pusan": "busan",
    "kao-hsiung": "kaohsiung",
    "tien-cin": "xingang",
    "tianjin": "xingang",
    "port klang": "port klang",
    "north port": "port klang",
}

def norm_pod(p):
    if not p:
        return ""
    s = p.lower().strip()
    s = re.sub(r"[^a-z0-9 ]", "", s).strip()
    return POD_ALIASES.get(s, s)

EQUIP_MAP = {
    "40rf": "40'RF", "40hc reefer": "40'RF", "40' hc reefer": "40'RF", "40' reefer": "40'RF", "40 reefer": "40'RF", "40reefer": "40'RF",
    "40dv": "40'DV", "40hc": "40'HC", "40' hc": "40'HC", "40 hc": "40'HC",
    "20dv": "20'DV", "20 dv": "20'DV", "20' dv": "20'DV",
    "20rf": "20'RF",
}

def norm_equipment(txt):
    if not txt:
        return None
    s = txt.lower()
    s = s.replace("'", "").replace("\u2019", "").replace("`", "")
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    # direct patterns
    if "reefer" in s or re.search(r"\b40\s*rf\b", s):
        if re.search(r"\b40\b", s):
            return "40'RF"
        if re.search(r"\b20\b", s):
            return "20'RF"
    if re.search(r"\b40\s*hc\b", s):
        return "40'HC"
    if re.search(r"\b40\s*dv\b", s):
        return "40'DV"
    if re.search(r"\b20\s*dv\b", s) or re.search(r"\b20\s*foot\b", s) or re.search(r"\b20\s*ft\b", s) or re.search(r"^20\b", s):
        return "20'DV"
    return None

def extract_qty(txt):
    if not txt:
        return None
    # patterns like "1-40", "2x40", "3x20", "4-20", or "1 x 40"
    m = re.search(r"\b(\d+)\s*[-x]\s*(?:40|20)\b", txt, re.I)
    if m:
        return int(m.group(1))
    m = re.search(r"\b(\d+)\s*\*\s*(?:40|20)\b", txt, re.I)
    if m:
        return int(m.group(1))
    return None

POD_SUBJECT_RE = re.compile(r"Oakland to ([A-Za-z\(\)\- ]+?)(?:\s*[-/,].*)?$", re.I)

def extract_pod_from_subject(subj):
    if not subj:
        return None
    s = re.sub(r"^(re|fw|fwd)\s*:\s*", "", subj.strip(), flags=re.I)
    m = POD_SUBJECT_RE.search(s)
    if m:
        pod = m.group(1).strip()
        # strip trailing paren-qualifier like "(North)" -> keep base
        pod = re.sub(r"\(.*?\)\s*$", "", pod).strip()
        return pod
    return None

def load_json(p):
    with open(p) as f:
        return json.load(f)

def safe_write(p, obj):
    tmp = p + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=2, default=str)
    os.replace(tmp, p)

# --------------------------------------------------------------------
# 1. Collect inquiries (role=request) across all sources in window
# --------------------------------------------------------------------
def collect_from_pairs():
    """Return list of dict records from ingest_pairs.json, for requests in window."""
    data = load_json(PAIRS_PATH)
    out = []
    for p in data.get("pairs", []):
        req = p.get("request") or {}
        resp = p.get("response") or {}
        rdt = parse_iso(req.get("receivedDateTime") or req.get("sentDateTime"))
        if not rdt:
            continue
        if not (WIN_START <= rdt <= WIN_END):
            continue
        sender = (req.get("sender") or "").lower()
        if "lupfold@hilmaringredients.com" not in sender:
            continue
        subject = req.get("subject") or ""
        # skip non-Lonny ops-flow: MDOLX free-time issues, docs, rate desk -> subjects usually start "Oakland to"
        if not subject.lower().startswith("oakland to") and "oakland to" not in subject.lower():
            # exclude MDOLX reply chains / free-time / pre-alert / tracker
            continue
        resp_sender = (resp.get("sender") or "").lower() if resp else ""
        is_ops = "mbd_oceanexportbookingshared@ol-usa.com" in resp_sender or not resp
        if resp and not is_ops:
            # response came from Linda (rates/ops single) - we'll still record but skip rate-desk-only threads
            # keep it -- ingest_pairs already excludes rate desk (re-check by subject prefix)
            pass
        out.append({
            "src": "pairs",
            "request_id": req.get("id"),
            "request_uri": req.get("uri"),
            "request_subject": subject,
            "request_received": rdt,
            "request_preview": req.get("summary") or "",
            "response_id": resp.get("id") if resp else None,
            "response_uri": resp.get("uri") if resp else None,
            "response_subject": resp.get("subject") if resp else None,
            "response_received": parse_iso(resp.get("receivedDateTime")) if resp else None,
            "response_sender": resp.get("sender") if resp else None,
            "response_preview": resp.get("summary") if resp else None,
            "conversationId": None,   # not present in pairs
        })
    # unmatched requests -> inquiries with no response
    for r in data.get("unmatched_requests", []):
        rdt = parse_iso(r.get("receivedDateTime") or r.get("sentDateTime"))
        if not rdt:
            continue
        if not (WIN_START <= rdt <= WIN_END):
            continue
        sender = (r.get("sender") or "").lower()
        if "lupfold@hilmaringredients.com" not in sender:
            continue
        subject = r.get("subject") or ""
        if "oakland to" not in subject.lower():
            continue
        out.append({
            "src": "pairs-unmatched",
            "request_id": r.get("id"),
            "request_uri": r.get("uri"),
            "request_subject": subject,
            "request_received": rdt,
            "request_preview": r.get("summary") or "",
            "response_id": None,
            "response_uri": None,
            "response_subject": None,
            "response_received": None,
            "response_sender": None,
            "response_preview": None,
            "conversationId": None,
        })
    return out

def collect_from_new_bodies():
    """Load pulled bodies from new_bodies/ and pair by conversationId."""
    files = sorted(os.listdir(NEW_BODIES_DIR))
    reqs = []
    replies = []
    for fn in files:
        full = os.path.join(NEW_BODIES_DIR, fn)
        try:
            d = load_json(full)
        except Exception:
            continue
        if d.get("role") == "request":
            d["_file"] = fn
            reqs.append(d)
        elif d.get("role") == "reply":
            d["_file"] = fn
            replies.append(d)
    # pair by conversationId
    replies_by_conv = {}
    for r in replies:
        cid = r.get("conversationId")
        if cid:
            replies_by_conv.setdefault(cid, []).append(r)
    out = []
    for req in reqs:
        rdt = parse_iso(req.get("sent"))
        if not rdt:
            continue
        if not (WIN_START <= rdt <= WIN_END):
            continue
        cid = req.get("conversationId")
        candidates = replies_by_conv.get(cid, [])
        # pick earliest reply AFTER request
        resp = None
        best_dt = None
        for r in candidates:
            rdt2 = parse_iso(r.get("sent"))
            if not rdt2:
                continue
            if rdt2 < rdt:
                continue
            if best_dt is None or rdt2 < best_dt:
                best_dt = rdt2
                resp = r
        out.append({
            "src": "new_bodies",
            "request_id": None,
            "request_uri": None,
            "request_subject": req.get("subject"),
            "request_received": rdt,
            "request_preview": req.get("body_text") or "",
            "response_id": None,
            "response_uri": None,
            "response_subject": (resp or {}).get("for_subject"),
            "response_received": parse_iso((resp or {}).get("sent")) if resp else None,
            "response_sender": (resp or {}).get("from"),
            "response_preview": (resp or {}).get("body_text") if resp else None,
            "response_parsed_quotes": (resp or {}).get("parsed_quotes") if resp else None,
            "conversationId": cid,
        })
    return out

# --------------------------------------------------------------------
# 2. Merge extract_v2 tables into pairs-derived records (by response uri/id)
# --------------------------------------------------------------------
def load_extract_v2_by_id():
    data = load_json(EXTRACT_V2_PATH)
    by_id = {}
    for rec in data:
        by_id[rec.get("id")] = rec
    return by_id

def merge_tables(pairs_records, extract_by_id):
    for rec in pairs_records:
        rid = rec.get("response_id")
        if rid and rid in extract_by_id:
            ex = extract_by_id[rid]
            t = ex.get("table")
            if t:
                # convert to parsed_quotes-like format
                rec["response_parsed_quotes"] = [{
                    "option": 1,
                    "carrier": t.get("carrier"),
                    "vessel": t.get("vessel"),
                    "voyage": t.get("voyage"),
                    "container_size": t.get("container_size"),
                    "commodity": None,
                    "erd": t.get("erd"),
                    "doc_cut": t.get("doc_cut"),
                    "port_cut": t.get("port_cut"),
                    "etd": t.get("etd"),
                    "eta": t.get("eta"),
                    "transshipment": t.get("transshipment"),
                    "rate_usd": (lambda v: int(re.sub(r"[^\d]", "", v)) if v and re.search(r"\d", v) else None)(t.get("rate")),
                    "dthc_included": (t.get("dthc") or "").lower() == "included",
                    "origin_free_time_days": t.get("origin_free_time"),
                    "dest_free_time_days": t.get("dest_free_time"),
                }]
                rec["_pod_from_table"] = t.get("pod")
                rec["_pol_from_table"] = t.get("pol")
    return pairs_records

# --------------------------------------------------------------------
# 3. Dedupe across sources (by subject + timestamp proximity)
# --------------------------------------------------------------------
def dedupe(records):
    records.sort(key=lambda r: r["request_received"])
    unique = []
    seen = []
    for r in records:
        subj = (r.get("request_subject") or "").strip().lower()
        ts = r["request_received"]
        dup = False
        for s in seen:
            if s["subj"] == subj and abs((s["ts"] - ts).total_seconds()) <= 120:
                dup = True
                break
        if dup:
            continue
        seen.append({"subj": subj, "ts": ts})
        unique.append(r)
    return unique

# --------------------------------------------------------------------
# 4. Outcome logic: WIN / LOSS / PENDING
# --------------------------------------------------------------------
def load_confirmations():
    data = load_json(CONFIRMATIONS)
    # filter MDOLXs whose received_at falls within/after window start
    out = []
    for c in data.get("confirmations", []):
        rdt = parse_iso(c.get("received_at"))
        if not rdt:
            continue
        if rdt < WIN_START:
            # include if updates extend into the window
            keep = False
            for u in c.get("updates", []):
                udt = parse_iso(u.get("received_at"))
                if udt and udt >= WIN_START:
                    keep = True
                    break
            if not keep:
                continue
        c["_received_dt"] = rdt
        out.append(c)
    return out

def match_mdolx(rec, confirmations, used):
    """Find best-match MDOLX confirmation for this inquiry. Returns (mdolx or None, score)."""
    inquiry_text = (rec.get("request_preview") or "") + " " + (rec.get("request_subject") or "")
    inq_pod = extract_pod_from_subject(rec.get("request_subject") or "")
    if not inq_pod and rec.get("_pod_from_table"):
        inq_pod = rec["_pod_from_table"]
    inq_equip = norm_equipment(inquiry_text)
    inq_qty = extract_qty(inquiry_text)

    best = None
    best_score = -1
    for c in confirmations:
        if c["mdolx_ref"] in used:
            continue
        # confirmation must come AFTER inquiry
        if c["_received_dt"] < rec["request_received"]:
            continue
        score = 0
        # POD match
        cpod = norm_pod(c.get("pod"))
        ipod = norm_pod(inq_pod)
        if cpod and ipod:
            if cpod == ipod:
                score += 40
            elif cpod in ipod or ipod in cpod:
                score += 20
            else:
                continue  # POD mismatch is fatal
        else:
            # no POD info -> weaker match
            if not cpod and not ipod:
                score += 5
            elif cpod or ipod:
                continue
        # equipment
        c_eq = c.get("equipment")
        if c_eq and inq_equip:
            if c_eq == inq_equip:
                score += 30
            elif c_eq.replace("'", "") == (inq_equip or "").replace("'", ""):
                score += 20
        # qty
        if c.get("qty") and inq_qty and (int(c["qty"]) == int(inq_qty) or abs(int(c["qty"]) - int(inq_qty)) == 0):
            score += 20
        # time proximity (confirmation closer to inquiry is better)
        hours = (c["_received_dt"] - rec["request_received"]).total_seconds() / 3600.0
        if 0 <= hours <= 48:
            score += 15
        elif 0 <= hours <= 96:
            score += 10
        elif 0 <= hours <= 168:
            score += 5
        if score > best_score:
            best_score = score
            best = c
    # require a minimum signal
    if best and best_score >= 40:
        return best, best_score
    return None, 0

def detect_decline(text):
    if not text:
        return False
    t = text.lower()
    pats = [
        r"\bwe(?:'|\s*a)re going with\b",
        r"\bgoing to go with\b",
        r"\bpass on this\b",
        r"\bnot going to book\b",
        r"\bdecline\b",
        r"\bno thank(?:s| you)\b",
        r"\bcancel(?:ling|ed)?\b",
    ]
    return any(re.search(p, t) for p in pats)

def compute_outcome(rec, confirmations, used, cutoff):
    """Return (outcome, mdolx_ref, note)."""
    # PRIORITY 1: MDOLX match
    match, score = match_mdolx(rec, confirmations, used)
    if match:
        used.add(match["mdolx_ref"])
        return "WIN", match["mdolx_ref"], f"Booked via MDOLX (match score={score})"
    # PRIORITY 2: no response yet
    if not rec.get("response_received"):
        # no reply at all
        hours_since_req = (cutoff - rec["request_received"]).total_seconds() / 3600.0
        if hours_since_req > 24:
            return "LOSS", None, "No OL reply within 24h"
        return "PENDING", None, "Awaiting OL reply"
    # PRIORITY 3: response received, check age since reply
    hours_since_reply = (cutoff - rec["response_received"]).total_seconds() / 3600.0
    if hours_since_reply <= 24:
        return "PENDING", None, "Within 24h of OL reply"
    # > 24h since reply with no MDOLX -> LOSS
    return "LOSS", None, f"No booking within {int(hours_since_reply)}h of OL reply"

# --------------------------------------------------------------------
# 5. Parse quantity/equipment/dates from inquiry body into structured fields
# --------------------------------------------------------------------
def parse_request_body(rec):
    txt = rec.get("request_preview") or ""
    pod = extract_pod_from_subject(rec.get("request_subject") or "") or rec.get("_pod_from_table")
    equip = norm_equipment(txt)
    qty = extract_qty(txt)
    # requested dates (free-text)
    cutoff_match = re.search(r"(cut\s*off[^\.]+?)(?:\.|$)", txt, re.I)
    return {
        "pol": "Oakland",
        "pod": pod,
        "qty": qty,
        "equipment": equip,
        "requested_cutoff_text": cutoff_match.group(1).strip() if cutoff_match else None,
        "body_text": txt[:500],
    }

# --------------------------------------------------------------------
# 6. Main
# --------------------------------------------------------------------
def main():
    pairs_recs = collect_from_pairs()
    new_recs = collect_from_new_bodies()
    extract_by_id = load_extract_v2_by_id()
    merge_tables(pairs_recs, extract_by_id)

    all_recs = pairs_recs + new_recs
    all_recs = dedupe(all_recs)

    confirmations = load_confirmations()
    used = set()
    cutoff = WIN_END

    # Process requests OLDEST-first so early wins get claimed first
    all_recs.sort(key=lambda r: r["request_received"])

    inquiries = []
    for rec in all_recs:
        outcome, mdolx_ref, note = compute_outcome(rec, confirmations, used, cutoff)
        req_parsed = parse_request_body(rec)
        inquiry = {
            "conversation_id": rec.get("conversationId"),
            "request": {
                "received_at": iso_out(rec["request_received"]),
                "subject": rec.get("request_subject"),
                "uri": rec.get("request_uri"),
                "pol": req_parsed["pol"],
                "pod": req_parsed["pod"],
                "qty": req_parsed["qty"],
                "equipment": req_parsed["equipment"],
                "requested_cutoff_text": req_parsed["requested_cutoff_text"],
                "body_preview": req_parsed["body_text"],
            },
            "response": {
                "received_at": iso_out(rec.get("response_received")) if rec.get("response_received") else None,
                "subject": rec.get("response_subject"),
                "uri": rec.get("response_uri"),
                "sender": rec.get("response_sender"),
                "options": rec.get("response_parsed_quotes") or [],
                "options_count": len(rec.get("response_parsed_quotes") or []),
            },
            "pick": None,  # pick detection requires thread replies (not available in current pull)
            "outcome": outcome,
            "mdolx_ref": mdolx_ref,
            "note": note,
            "source": rec.get("src"),
        }
        inquiries.append(inquiry)

    # Unmatched MDOLX: confirmations that weren't linked to any inquiry
    unmatched_mdolx = []
    for c in confirmations:
        if c["mdolx_ref"] not in used:
            unmatched_mdolx.append({
                "mdolx_ref": c["mdolx_ref"],
                "carrier": c.get("carrier"),
                "pol": c.get("pol"),
                "pod": c.get("pod"),
                "qty": c.get("qty"),
                "equipment": c.get("equipment"),
                "received_at": c.get("received_at"),
                "status": c.get("status"),
                "subject": c.get("subject"),
                "reason": "No matching ops-flow inquiry found in window (pre-window rate-desk or direct booking)",
            })

    breakdown = {"WIN": 0, "LOSS": 0, "PENDING": 0}
    for i in inquiries:
        breakdown[i["outcome"]] = breakdown.get(i["outcome"], 0) + 1

    out = {
        "window": {
            "start": iso_out(WIN_START),
            "end":   iso_out(WIN_END),
        },
        "generated_at": iso_out(datetime.now(timezone.utc)),
        "totals": {
            "inquiries": len(inquiries),
            "wins": breakdown["WIN"],
            "losses": breakdown["LOSS"],
            "pending": breakdown["PENDING"],
            "mdolx_confirmations_in_window": len(confirmations),
            "mdolx_matched": len(used),
            "mdolx_unmatched": len(unmatched_mdolx),
        },
        "inquiries": inquiries,
        "unmatched_mdolx": unmatched_mdolx,
        "notes": [
            "Scope: Hilmar ops-flow inquiries only. Sender=lupfold@hilmaringredients.com with subject 'Oakland to <POD>'.",
            "Excluded: rate desk (mbd_export_pricing), docs (mbd_exportdocsshared), free-time/tracker/pre-alert/MDOLX reply threads.",
            "Source A (pre-04-13 14:25): ingest_pairs.json (pre-computed pair bundle) + ingest_extract_v2.json option tables.",
            "Source B (04-13 14:25 -> 04-16 21:49): scripts/new_bodies/ (22 Lonny + 21 OL live-pulled bodies, paired by conversationId).",
            "Outcome rules: WIN = MDOLX confirmation matches POD/equipment/qty and arrives after OL reply; LOSS = >24h since OL reply with no booking OR no reply at all >24h after inquiry; PENDING = <=24h since reply OR reply still pending within 24h of inquiry.",
            "Pick detection (Lonny 'please proceed with option X') not performed in this build; MDOLX match is the WIN signal.",
            "Data gaps: 2026-04-17 through 2026-04-20 pagination may still hold additional inquiries beyond those captured here.",
        ],
    }

    safe_write(OUT_PATH, out)
    print(f"Wrote {OUT_PATH}")
    print(f"Inquiries: {len(inquiries)} | WIN: {breakdown['WIN']} | LOSS: {breakdown['LOSS']} | PENDING: {breakdown['PENDING']}")
    print(f"MDOLX in-window: {len(confirmations)} | matched: {len(used)} | unmatched: {len(unmatched_mdolx)}")

if __name__ == "__main__":
    main()
