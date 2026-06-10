#!/usr/bin/env python3
"""
merge_ingest_v2.py — build ingest_extract_v2.json from 40 email_bodies/body_XX.json
files, joined with responses_unique.json for URI.

Writes:
  - scripts/ingest_extract_v2.json : array of 40 records
  - prints summary to stdout
"""
import contextlib
import json
import re
from html.parser import HTMLParser
from pathlib import Path

SCRIPTS = Path("/sessions/brave-sharp-davinci/mnt/PROJECT HILMAR/scripts")
BODIES = SCRIPTS / "email_bodies"
RESPONSES = SCRIPTS / "responses_unique.json"
OUT = SCRIPTS / "ingest_extract_v2.json"

# Carrier normalization per spec
CARRIER_MAP = {
    "CMA": "CMA CGM",
    "CMACGM": "CMA CGM",
    "CMA CGM": "CMA CGM",
    "CMA-CGM": "CMA CGM",
    "CGM": "CMA CGM",
    "ANL": "CMA CGM",
    "APL": "CMA CGM",
    "MSCU": "MSC",
    "MSC": "MSC",
    "MAEU": "MAERSK",
    "MAERSK": "MAERSK",
    "HDMU": "HMM",
    "HMM": "HMM",
    "ONEY": "ONE",
    "ONE": "ONE",
    "ONE LINE": "ONE",
}

def normalize_carrier(raw):
    if not raw:
        return None
    key = re.sub(r"\s+", " ", raw).strip().upper()
    if key in CARRIER_MAP:
        return CARRIER_MAP[key]
    # Also handle stripped-no-space form
    key2 = key.replace(" ", "")
    if key2 in CARRIER_MAP:
        return CARRIER_MAP[key2]
    return key  # pass-through unknown carriers, upper-cased

class TableExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.in_table = False
        self.current_row = []
        self.current_cell = []
        self.rows = []
        self.in_td = False
    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self.in_table = True
            self.current_row = []
        elif tag == "tr" and self.in_table:
            self.current_row = []
        elif tag in ("td","th") and self.in_table:
            self.in_td = True
            self.current_cell = []
    def handle_endtag(self, tag):
        if tag in ("td","th") and self.in_table:
            self.current_row.append("".join(self.current_cell).strip())
            self.in_td = False
        elif tag == "tr" and self.in_table and self.current_row:
            self.rows.append(self.current_row)
            self.current_row = []
        elif tag == "table" and self.in_table:
            self.in_table = False
    def handle_data(self, data):
        if self.in_td:
            txt = data.strip()
            if txt:
                self.current_cell.append(txt + " ")

def clean(s):
    return re.sub(r"\s+", " ", (s or "")).strip()

HEADER_MAP = {
    "pol": ["pol"],
    "pod": ["pod"],
    "container_size": ["container size","container"],
    "vessel": ["vessel"],
    "voyage": ["voyage"],
    "erd": ["erd"],
    "doc_cut": ["doc cut","docs cut"],
    "port_cut": ["port cut"],
    "etd": ["etd"],
    "eta": ["eta"],
    "rate": ["rate"],
    "dthc": ["dthc"],
    "carrier": ["carrier"],
    "transshipment": ["transshipment"],
    "origin_free_time": ["origin free time","origin free-time"],
    "dest_free_time": ["destination free time","destination free-time","dest free time"],
}

COLUMNS = list(HEADER_MAP.keys())

def extract_table(body_html):
    if not body_html:
        return None, False
    p = TableExtractor()
    with contextlib.suppress(Exception):
        p.feed(body_html)
    # Find first booking-style header
    for idx, row in enumerate(p.rows):
        row_norm = [clean(c).lower() for c in row]
        if ("pol" in row_norm) and ("pod" in row_norm) and any("carrier" in c for c in row_norm):
            header = row_norm
            data_rows = []
            for r2 in p.rows[idx+1:]:
                if not r2: continue
                if len(r2) >= 4:  # accept reasonably-sized data rows
                    data_rows.append([clean(c) for c in r2])
            if not data_rows:
                return None, True  # saw header but no data
            col_idx = {}
            for col, aliases in HEADER_MAP.items():
                for i, h in enumerate(header):
                    if any(a == h or a in h for a in aliases):
                        col_idx[col] = i
                        break
            # Build first data row as canonical
            row0 = data_rows[0]
            out = {}
            for col in COLUMNS:
                if col in col_idx and col_idx[col] < len(row0):
                    out[col] = row0[col_idx[col]]
                else:
                    out[col] = None
            return out, True
    return None, False

def parse_subject(subj):
    subj = subj or ""
    mdolx = None
    m = re.match(r"(MDOLX\d+)_", subj.strip())
    if m: mdolx = m.group(1)
    subj_carrier = None
    m2 = re.search(r"//\s*([A-Z][A-Z0-9 \-]{1,10}?):\s*[A-Z0-9]+", subj)
    if m2: subj_carrier = m2.group(1).strip()
    # Destination capture: "Oakland to X" or similar
    dest = None
    m3 = re.search(r"(?:[Oo]akland|Dalhart)\s+to\s+([A-Za-z][A-Za-z \(\)\.]*?)(?://|$|\s{2,}|_|\s*-)", subj)
    if m3:
        dest = m3.group(1).strip()
    return mdolx, subj_carrier, dest

def main():
    responses = json.loads(RESPONSES.read_text())
    uri_by_id = {r["id"]: r["uri"] for r in responses}

    records = []
    for i in range(40):
        fp = BODIES / f"body_{i:02d}.json"
        raw = fp.read_text()
        obj = json.loads(raw)
        body_html = (obj.get("body") or {}).get("content","") or ""
        body_len = len(body_html)
        tbl, saw_header = extract_table(body_html)
        subj = obj.get("subject","") or ""
        mdolx, subj_carrier, dest = parse_subject(subj)
        sender = (obj.get("sender") or {}).get("address","")
        mid = obj.get("id")
        uri = uri_by_id.get(mid)
        # Normalize carrier in table cell
        tbl_carrier_raw = tbl.get("carrier") if tbl else None
        tbl_carrier_norm = normalize_carrier(tbl_carrier_raw) if tbl_carrier_raw else None
        if tbl and tbl_carrier_norm:
            tbl["carrier"] = tbl_carrier_norm
            tbl["_carrier_raw"] = tbl_carrier_raw

        rec = {
            "index": i,
            "uri": uri,
            "id": mid,
            "subject": subj,
            "sender": sender,
            "received_at": obj.get("receivedDateTime"),
            "sent_at": obj.get("sentDateTime"),
            "mdolx_ref": mdolx,
            "destination_from_subject": dest,
            "subject_carrier": normalize_carrier(subj_carrier) if subj_carrier else None,
            "subject_carrier_raw": subj_carrier,
            "table": tbl,
            "body_length_chars": body_len,
            "has_table": tbl is not None,
            "notes": obj.get("note"),
        }
        records.append(rec)

    OUT.write_text(json.dumps(records, indent=2, default=str))

    # Summary
    total = len(records)
    with_table = sum(1 for r in records if r["has_table"])
    without = total - with_table
    carrier_counts = {}
    for r in records:
        if r["table"] and r["table"].get("carrier"):
            c = r["table"]["carrier"]
            carrier_counts[c] = carrier_counts.get(c, 0) + 1
    subj_carrier_counts = {}
    for r in records:
        if r["subject_carrier"]:
            c = r["subject_carrier"]
            subj_carrier_counts[c] = subj_carrier_counts.get(c, 0) + 1
    # Mismatches: subject_carrier set AND table carrier set AND different
    mismatches = []
    for r in records:
        sc = r.get("subject_carrier")
        tc = r["table"]["carrier"] if r.get("table") and r["table"].get("carrier") else None
        if sc and tc and sc != tc:
            mismatches.append({"index": r["index"], "subject": r["subject"],
                               "subject_carrier": sc, "table_carrier": tc})
    # Vessel/carrier suspicious (vessel contains carrier name but carrier cell is a different family)
    vessel_flags = []
    family_hints = {
        "HMM ": "HMM", "CMA CGM": "CMA CGM", "MAERSK": "MAERSK", "MSC ": "MSC",
        "WAN HAI": "WANHAI", "WANHAI": "WANHAI", "ONE ": "ONE", "YML": "YML",
        "EVERGREEN": "EVERGREEN", "OOCL": "OOCL", "HAPAG": "HAPAG",
    }
    for r in records:
        t = r.get("table") or {}
        vessel = (t.get("vessel") or "").upper()
        tc = t.get("carrier")
        if not vessel or not tc:
            continue
        for hint, fam in family_hints.items():
            if hint.strip() and hint in vessel and tc != fam and fam != "WANHAI":
                # skip WANHAI fuzzy; still flag for review
                vessel_flags.append({"index": r["index"], "subject": r["subject"],
                                     "vessel": t.get("vessel"), "carrier_cell": tc,
                                     "vessel_family_hint": fam})
                break

    missing_uris = [r["index"] for r in records if not r["uri"]]

    print("=== ingest_extract_v2 summary ===")
    print(f"total records: {total}")
    print(f"with booking table: {with_table}")
    print(f"without table: {without}")
    print("carrier distribution (table cell, normalized):")
    for c, n in sorted(carrier_counts.items(), key=lambda x: -x[1]):
        print(f"  {c}: {n}")
    print("subject-carrier distribution (normalized):")
    for c, n in sorted(subj_carrier_counts.items(), key=lambda x: -x[1]):
        print(f"  {c}: {n}")
    print(f"missing URIs: {missing_uris}")
    print(f"subject/table carrier mismatches: {len(mismatches)}")
    for m in mismatches:
        print(f"  #{m['index']} [{m['subject']}] subject={m['subject_carrier']} table={m['table_carrier']}")
    print(f"vessel/carrier cell suspicious flags: {len(vessel_flags)}")
    for v in vessel_flags:
        print(f"  #{v['index']} vessel='{v['vessel']}' cell='{v['carrier_cell']}' hint={v['vessel_family_hint']} subject={v['subject']}")
    # No-table records (notes)
    print("no-table records:")
    for r in records:
        if not r["has_table"]:
            print(f"  #{r['index']} [{r['subject']}] note={r['notes']}")

if __name__ == "__main__":
    main()
