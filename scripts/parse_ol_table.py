#!/usr/bin/env python3
"""
parse_ol_table.py — extract structured booking fields from an OL response email body.
Input: path to a raw read_resource output file (JSON) OR JSON on stdin.
Output: JSON with {request_id-ish fields}
"""
import contextlib
import json
import re
import sys
from html.parser import HTMLParser
from pathlib import Path

COLUMNS = ["pol","pod","container_size","vessel","voyage","erd","doc_cut",
           "port_cut","etd","eta","rate","dthc","carrier","transshipment",
           "origin_free_time","dest_free_time"]

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
            # Only first substantial table (the booking table)
            self.in_table = True
            self.current_row = []
            self.rows_for_table = []
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
            # Skip empty/whitespace-only
            txt = data.strip()
            if txt:
                self.current_cell.append(txt + " ")

def clean(s):
    return re.sub(r"\s+", " ", (s or "")).strip()

def extract_from_body(body_html: str):
    p = TableExtractor()
    with contextlib.suppress(Exception):
        p.feed(body_html)
    # Find the first "booking" table: one that has a header row with POL/POD/Container
    candidates = []
    for idx, row in enumerate(p.rows):
        row_norm = [clean(c).lower() for c in row]
        if ("pol" in row_norm) and ("pod" in row_norm) and any("carrier" in c for c in row_norm):
            # Next rows after this header (until table end) are data rows
            # Look at indices idx+1 ... up to next header-like row
            header = row_norm
            data_rows = []
            for r2 in p.rows[idx+1:]:
                if not r2: continue
                r2_norm = [clean(c) for c in r2]
                # Heuristic: if 10+ cells and contains likely carrier/rate, it's a data row
                if len(r2_norm) >= 10:
                    data_rows.append(r2_norm)
            if data_rows:
                candidates.append((header, data_rows))
    if not candidates:
        return None
    header, data_rows = candidates[0]
    # Map columns by finding indices in header
    col_idx = {}
    header_map = {
        "pol": ["pol"], "pod": ["pod"], "container_size": ["container size","container"],
        "vessel": ["vessel"], "voyage": ["voyage"], "erd": ["erd"], "doc_cut": ["doc cut","docs cut"],
        "port_cut": ["port cut"], "etd": ["etd"], "eta": ["eta"], "rate": ["rate"],
        "dthc": ["dthc"], "carrier": ["carrier"], "transshipment": ["transshipment"],
        "origin_free_time": ["origin free time","origin free-time"],
        "dest_free_time": ["destination free time","destination free-time","dest free time"],
    }
    for col, aliases in header_map.items():
        for i, h in enumerate(header):
            if any(a in h for a in aliases):
                col_idx[col] = i
                break
    # Build first data row
    row = data_rows[0]
    out = {}
    for col, i in col_idx.items():
        if i < len(row):
            out[col] = clean(row[i])
    return out

def main():
    # Accept arg: path to JSON file that is a read_resource result (inline dict OR overflow-array)
    if len(sys.argv) < 2:
        print("usage: parse_ol_table.py <resource.json>", file=sys.stderr); sys.exit(1)
    p = Path(sys.argv[1])
    raw = p.read_text(encoding="utf-8")
    try:
        obj = json.loads(raw)
    except Exception as e:
        print(f"JSON parse fail: {e}", file=sys.stderr); sys.exit(2)
    # overflow files are [{"type":"text","text":"{...}"}, ...] — take first
    if isinstance(obj, list):
        obj = json.loads(obj[0].get("text","{}"))
    body_html = (obj.get("body",{}) or {}).get("content","")
    subj = obj.get("subject","")
    recv = obj.get("receivedDateTime","")
    sender = (obj.get("sender") or {}).get("address","")
    tbl = extract_from_body(body_html) or {}
    # Also pull MDOLX ref from subject if present
    mdolx = None
    m = re.match(r"(MDOLX\d+)_", subj.strip())
    if m:
        mdolx = m.group(1)
    # Carrier hint from subject MDOLX//CARRIER: pattern
    subj_carrier = None
    m2 = re.search(r"//\s*([A-Z][A-Z0-9 \-]{1,10}?):\s*[A-Z0-9]+", subj)
    if m2:
        subj_carrier = m2.group(1).strip()
    # Destination from subject "Oakland to X"
    dest = None
    m3 = re.search(r"[Oo]akland to ([A-Za-z][A-Za-z \(\)\.]*?)(?://|$|\s{2,})", subj)
    if m3:
        dest = m3.group(1).strip()
    out = {
        "id": obj.get("id"),
        "subject": subj,
        "sender": sender,
        "received": recv,
        "conversationId": obj.get("conversationId"),
        "internetMessageId": obj.get("internetMessageId"),
        "mdolx_ref": mdolx,
        "subject_carrier": subj_carrier,
        "destination_from_subject": dest,
        "table": tbl,
    }
    print(json.dumps(out, indent=2, default=str))

if __name__ == "__main__":
    main()
