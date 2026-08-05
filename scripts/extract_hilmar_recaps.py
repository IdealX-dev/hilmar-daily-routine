#!/usr/bin/env python3
"""Parse Linda Echevarria weekly recap emails and extract HILMAR-related data.

IMPORTANT FINDING: Linda's weekly recaps do NOT contain a per-booking detail table
with (OL Ref, Customer, POL, POD, QTY, CNTR, Carrier, Booking#, Vessel, ETD, ETA).

What recaps DO contain (by table type):
  A. War-affected containers - columns: From, POL, To, QTY, CNTR, Booking conf, Comments
     (NO customer column; cannot attribute to HILMAR without cross-reference)
  B. Weekly customer TEU pivot - aggregate per-customer booking count + TEU sum
  C. Historical 2-week pivot (prior year vs current)
  D. CUSTOMERS THAT DID NOT BOOK (just names)
  E. PENDING RATES / PENDING BOOKING / ISSUES / DISPUTES - columns:
     Booking Operator, Customer, File # (MDOLX), Origin, Destination, CNTR TYPE, QTY, Notes
     (per-booking but no carrier/vessel/ETD/ETA)
  F. HILMAR QUOTE RESPONSE TIME (02/07-02/13 only) - per-quote POL, POD, QTY, Equipment,
     Time Received, Time Answered, Notes (QUOTES, not bookings)

Output structure therefore has:
  - hilmar_weekly_aggregates: one entry per recap week showing Hilmar booking count + TEU
  - hilmar_detail_rows: per-booking rows where Hilmar appears (from PENDING/ISSUES/DISPUTES)
  - hilmar_quote_responses: Hilmar quote response rows (from 02/07-02/13 recap)
"""
from __future__ import annotations

import json
import os
import re
import sys
from html.parser import HTMLParser

PROJECT = "/sessions/brave-sharp-davinci/mnt/PROJECT HILMAR"
OUT = os.path.join(PROJECT, "scripts", "hilmar_bookings_from_recaps.json")

CARRIER_NORM = {
    "CMA": "CMA CGM", "CMA-CGM": "CMA CGM", "CMACGM": "CMA CGM", "CMA CGM": "CMA CGM",
    "MAEU": "Maersk", "MAERSK": "Maersk",
    "HDMU": "HMM", "HMM": "HMM",
    "MSCU": "MSC", "MSC": "MSC",
    "ONEY": "ONE", "ONE": "ONE",
    "EMC": "Evergreen", "EVERGREEN": "Evergreen", "EGLV": "Evergreen",
    "COSU": "COSCO", "COSCO": "COSCO",
    "OOLU": "OOCL", "OOCL": "OOCL",
    "YMLU": "Yang Ming", "YANG MING": "Yang Ming",
    "ZIM": "ZIM",
    "HLCU": "Hapag-Lloyd", "HAPAG": "Hapag-Lloyd", "HAPAG-LLOYD": "Hapag-Lloyd",
}


def normalize_carrier(val: str) -> str | None:
    if not val:
        return None
    v = re.sub(r"\s+", " ", val.strip()).upper()
    for key in sorted(CARRIER_NORM.keys(), key=len, reverse=True):
        if v == key or v.startswith(key + " ") or v.endswith(" " + key):
            return CARRIER_NORM[key]
    first = v.split()[0] if v.split() else v
    if first in CARRIER_NORM:
        return CARRIER_NORM[first]
    return val.strip()


def title_case_port(val: str) -> str:
    if not val:
        return ""
    v = re.sub(r"\s+", " ", val.strip())
    parts = [p.strip() for p in v.split(",")]
    titled = []
    for p in parts:
        if re.fullmatch(r"[A-Z]{2,3}", p):
            titled.append(p)
        else:
            titled.append(p.title())
    return ", ".join(titled)


class TableParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tables: list[list[list[str]]] = []
        self._in_table = 0
        self._current_table = None
        self._current_row = None
        self._in_cell = False
        self._cell_text: list[str] = []

    def handle_starttag(self, tag, attrs):
        t = tag.lower()
        if t == "table":
            self._in_table += 1
            self._current_table = []
        elif t == "tr" and self._in_table:
            self._current_row = []
        elif t in ("td", "th") and self._in_table:
            self._in_cell = True
            self._cell_text = []
        elif t == "br" and self._in_cell:
            self._cell_text.append("\n")

    def handle_endtag(self, tag):
        t = tag.lower()
        if t == "table" and self._in_table:
            if self._current_table is not None:
                self.tables.append(self._current_table)
            self._in_table -= 1
            self._current_table = None
        elif t == "tr" and self._current_row is not None:
            if self._current_table is not None:
                self._current_table.append(self._current_row)
            self._current_row = None
        elif t in ("td", "th") and self._in_cell:
            text = "".join(self._cell_text)
            text = re.sub(r"\xa0", " ", text)
            text = re.sub(r"[ \t]+", " ", text)
            text = re.sub(r"\s*\n\s*", " | ", text).strip()
            if self._current_row is not None:
                self._current_row.append(text)
            self._in_cell = False
            self._cell_text = []

    def handle_data(self, data):
        if self._in_cell:
            self._cell_text.append(data)

    def handle_entityref(self, name):
        if self._in_cell:
            if name == "nbsp":
                self._cell_text.append(" ")
            elif name == "amp":
                self._cell_text.append("&")
            else:
                self._cell_text.append(" ")

    def handle_charref(self, name):
        if self._in_cell:
            try:
                ch = chr(int(name[1:], 16)) if name.startswith(("x", "X")) else chr(int(name))
                self._cell_text.append(ch)
            except Exception:
                self._cell_text.append(" ")


HILMAR_RE = re.compile(r"HILMAR", re.IGNORECASE)
MDOLX_RE = re.compile(r"MDOLX\d{5,8}", re.IGNORECASE)


def load_email(overflow_path: str) -> tuple[str, str, str]:
    with open(overflow_path, encoding="utf-8") as f:
        arr = json.load(f)
    obj = json.loads(arr[0]["text"])
    return obj.get("id", ""), obj.get("subject", ""), obj.get("body", {}).get("content", "")


def customer_canon(raw: str) -> str:
    cu = (raw or "").upper()
    if "INGREDIENT" in cu:
        return "HILMAR INGREDIENTS"
    return "HILMAR CHEESE COMPANY"


def first_mdolx(cell: str) -> str | None:
    m = MDOLX_RE.search(cell or "")
    return m.group(0).upper() if m else None


def _week_label_tokens(label: str) -> list[str]:
    """Return month/day tokens from a week label for fuzzy matching inside pivot row0."""
    out = []
    for m in re.finditer(r"(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})", label or ""):
        mm, dd, _ = m.group(1), m.group(2), m.group(3)
        out.append(f"{int(mm):02d}/{int(dd):02d}")
        out.append(f"{int(mm):02d}-{int(dd):02d}")
    return out


def _is_current_week_pivot(table: list[list[str]], week_label: str) -> bool:
    """Return True if this pivot table represents the current recap week (not historical).
    Heuristic: among the first 3 body rows, some cell contains the recap week's date tokens."""
    tokens = _week_label_tokens(week_label)
    if not tokens:
        return False
    for r in table[1:4]:
        joined = " ".join(r)
        for tok in tokens:
            if tok in joined:
                return True
    return False


def parse_weekly_teu_pivot(table: list[list[str]], week_label: str, msg_id: str) -> list[dict]:
    """Table B: weekly customer TEU pivot with header like
      ['', 'CUSTOMERS', 'Count of Customer', 'Sum of TEU', '', 'BKG TEAM', 'Count of Creator', 'Sum of TEU']
    Body rows commonly come in two shapes:
      Row1 (has week label): [<week>, <CUSTOMER NAME>, <count>, <teu>, '', <creator>, <cnt>, <teu>]
      Row2+ (offset-by-one): [<CUSTOMER NAME>, <count>, <teu>, '', <creator>, <cnt>, <teu>]
    We scan every row for a Hilmar-bearing cell and then pick the next two numeric cells.
    Only emit if this table is the CURRENT-week pivot (not a historical compare table).
    """
    if not table:
        return []
    hdr = table[0]
    hdr_u = [c.upper() for c in hdr]
    hdr_joined = " | ".join(hdr_u)
    if "CUSTOMER" not in hdr_joined or "SUM OF TEU" not in hdr_joined:
        return []
    # Only accept the single-week pivot: requires 'BKG TEAM' or 'BOOKING TEAM' in the header
    # (Linda uses that exact 8-col layout for current-week booking count by customer + creator).
    # Historical compare tables lack BKG TEAM; skip those.
    single_week_header = any("BKG TEAM" in c or "BOOKING TEAM" in c for c in hdr_u)
    if not single_week_header:
        return []

    results = []
    for r in table[1:]:
        for i, cell in enumerate(r):
            if HILMAR_RE.search(cell or ""):
                nums = []
                for j in range(i + 1, len(r)):
                    m = re.fullmatch(r"\s*(\d+)\s*", r[j])
                    if m:
                        nums.append(int(m.group(1)))
                    if len(nums) >= 2:
                        break
                if len(nums) >= 2:
                    results.append({
                        "recap_week": week_label,
                        "customer_raw": cell,
                        "customer": customer_canon(cell),
                        "booking_count": nums[0],
                        "teu_sum": nums[1],
                        "source_message_id": msg_id,
                    })
                break
    return results


def parse_detail_rows(tables: list[list[list[str]]], week_label: str, msg_id: str) -> list[dict]:
    """Tables E: Detail rows (PENDING/ISSUES/DISPUTES). Accept only rows where Customer mentions HILMAR."""
    results = []
    for _ti, t in enumerate(tables):
        # find a row that is a header with File # or Customer + Origin + Destination
        hdr_idx = None
        for ri, r in enumerate(t):
            u = " | ".join(c.upper() for c in r)
            has_file = "FILE #" in u or "FILE NO" in u or "FILE#" in u
            has_cust = "CUSTOMER" in u and "COUNT OF CUSTOMER" not in u
            has_orig = "ORIGIN" in u
            has_dest = "DESTINATION" in u
            if has_cust and has_orig and has_dest and (has_file or "NOTES" in u):
                hdr_idx = ri
                break
        if hdr_idx is None:
            continue
        headers = t[hdr_idx]
        # map columns
        col = {}
        for i, h in enumerate(headers):
            u = h.upper().strip()
            if "CUSTOMER" in u:
                col["customer"] = i
            elif "FILE" in u:
                col["file"] = i
            elif "ORIGIN" in u:
                col["origin"] = i
            elif "DESTINATION" in u:
                col["destination"] = i
            elif "CONTAINER TYPE" in u or "CNTR TYPE" in u or u == "CNTR":
                col["cntr_type"] = i
            elif "CONTAINER QTY" in u or u == "QTY":
                col["qty"] = i
            elif "NOTES" in u or "REMARK" in u or "COMMENT" in u:
                col["notes"] = i
            elif "BOOKING OPERATOR" in u or u == "OPERATOR":
                col["operator"] = i
        # find section label (the row immediately before header if header is row 1+)
        section = None
        for candidate_row in t[:hdr_idx]:
            joined = " ".join(candidate_row).strip().upper()
            if joined in ("PENDING RATES", "PENDING BOOKING CONFIRMATION FROM SSL", "BOOKINGS ISSUES", "ISSUES", "DISPUTES"):
                section = joined
        for r in t[hdr_idx + 1:]:
            ci = col.get("customer")
            if ci is None or ci >= len(r):
                continue
            cust = r[ci]
            if not HILMAR_RE.search(cust):
                continue

            def getv(key, col=col, r=r):
                i = col.get(key)
                if i is None or i >= len(r):
                    return ""
                return r[i].strip()

            file_cell = getv("file")
            mdolx = first_mdolx(file_cell) or first_mdolx(" ".join(r))
            origin = title_case_port(getv("origin"))
            destination = title_case_port(getv("destination"))
            cntr_type = getv("cntr_type")
            qty_raw = getv("qty")
            qty = None
            if qty_raw:
                mq = re.search(r"\d+", qty_raw)
                if mq:
                    try:
                        qty = int(mq.group(0))
                    except Exception:
                        qty = None
            results.append({
                "recap_week": week_label,
                "section": section,
                "customer": customer_canon(cust),
                "customer_raw": cust,
                "mdolx_ref": mdolx,
                "file_raw": file_cell or None,
                "booking_operator": getv("operator") or None,
                "origin": origin or None,
                "destination": destination or None,
                "pol": origin or None,  # origin/destination are same granularity in these tables
                "pod": destination or None,
                "container_type": cntr_type or None,
                "qty": qty,
                "carrier": None,
                "booking_confirmation": None,
                "vessel": None,
                "etd": None,
                "eta": None,
                "notes": getv("notes") or None,
                "source_message_id": msg_id,
            })
    return results


def parse_hilmar_quote_response_time(tables, week_label, msg_id):
    """Table F (02/07-02/13 recap): HILMAR QUOTE RESPONSE TIME. Returns quote rows."""
    results = []
    for t in tables:
        label_hit = False
        hdr_idx = None
        for ri, r in enumerate(t):
            joined = " ".join(r).upper()
            if "HILMAR QUOTE RESPONSE TIME" in joined:
                label_hit = True
            if label_hit and "POL" in joined and "POD" in joined and "TIME" in joined:
                hdr_idx = ri
                break
        if not label_hit or hdr_idx is None:
            continue
        headers = t[hdr_idx]
        col = {}
        for i, h in enumerate(headers):
            u = h.upper().strip()
            if u == "POL":
                col["pol"] = i
            elif u == "POD":
                col["pod"] = i
            elif u == "QTY":
                col["qty"] = i
            elif "EQUIPMENT" in u:
                col["equipment"] = i
            elif "TIME RECEIVED" in u:
                col["time_received"] = i
            elif "TIME ANSWERED" in u:
                col["time_answered"] = i
            elif "NOTES" in u:
                col["notes"] = i
        for r in t[hdr_idx + 1:]:
            if len(r) < 3:
                continue
            # require POL + POD non-empty
            pol_i = col.get("pol")
            pod_i = col.get("pod")
            if pol_i is None or pod_i is None or pol_i >= len(r) or pod_i >= len(r):
                continue
            pol_v = r[pol_i].strip()
            pod_v = r[pod_i].strip()
            if not pol_v or not pod_v:
                continue
            qty = None
            qi = col.get("qty")
            if qi is not None and qi < len(r):
                mq = re.search(r"\d+", r[qi])
                if mq:
                    qty = int(mq.group(0))

            def gv(k, col=col, r=r):
                i = col.get(k)
                if i is None or i >= len(r):
                    return ""
                return r[i].strip()

            results.append({
                "recap_week": week_label,
                "customer": "HILMAR CHEESE COMPANY",
                "pol": title_case_port(pol_v),
                "pod": title_case_port(pod_v),
                "qty": qty,
                "equipment": gv("equipment") or None,
                "time_received": gv("time_received") or None,
                "time_answered": gv("time_answered") or None,
                "notes": gv("notes") or None,
                "source_message_id": msg_id,
            })
    return results


def parse_war_affected(tables, week_label, msg_id):
    """Table A (war-affected). Header: From, POL, To, QTY, CNTR TYPE, Booking confirmation, COMMENTS.
    No customer column, so we include all rows but flag customer=UNKNOWN. Caller can cross-reference."""
    results = []
    for t in tables:
        if not t:
            continue
        hdr = t[0]
        hdr_u = [c.upper().strip() for c in hdr]
        joined = " | ".join(hdr_u)
        if ("POL" in hdr_u and "QTY" in hdr_u and "BOOKING CONFIRMATION" in joined):
            # war-affected table - with optional OL Ref column
            ol_i = None; from_i = None; pol_i = None; to_i = None; qty_i = None; cntr_i = None; bkg_i = None; com_i = None
            for i, h in enumerate(hdr_u):
                if "OL REF" in h:
                    ol_i = i
                elif h == "FROM":
                    from_i = i
                elif h == "POL":
                    pol_i = i
                elif h == "TO":
                    to_i = i
                elif "QTY" in h:
                    qty_i = i
                elif "CNTR" in h:
                    cntr_i = i
                elif "BOOKING" in h:
                    bkg_i = i
                elif "COMMENT" in h:
                    com_i = i
            for r in t[1:]:
                def gv(i, r=r):
                    return r[i].strip() if i is not None and i < len(r) else ""
                results.append({
                    "recap_week": week_label,
                    "section": "WAR_AFFECTED",
                    "mdolx_ref": first_mdolx(gv(ol_i)) if ol_i is not None else first_mdolx(" ".join(r)),
                    "from_city": gv(from_i),
                    "pol": title_case_port(gv(pol_i)),
                    "pod": title_case_port(gv(to_i)),
                    "qty": int(re.search(r"\d+", gv(qty_i)).group(0)) if re.search(r"\d+", gv(qty_i)) else None,
                    "container_type": gv(cntr_i),
                    "booking_confirmation": gv(bkg_i) or None,
                    "comments": gv(com_i) or None,
                    "source_message_id": msg_id,
                })
    return results


def extract_one(overflow_path: str) -> dict:
    msg_id, subj, html = load_email(overflow_path)
    p = TableParser()
    p.feed(html)
    tables = p.tables
    week_label = subj.replace("Weekly recap for ", "").replace("Weekly recap from ", "").replace("Weekly recap ", "").strip()

    aggregates = []
    for t in tables:
        aggregates.extend(parse_weekly_teu_pivot(t, week_label, msg_id))
    details = parse_detail_rows(tables, week_label, msg_id)
    quotes = parse_hilmar_quote_response_time(tables, week_label, msg_id)
    war_rows = parse_war_affected(tables, week_label, msg_id)

    return {
        "msg_id": msg_id,
        "subject": subj,
        "week_label": week_label,
        "aggregates": aggregates,
        "details": details,
        "quotes": quotes,
        "war_rows_all": war_rows,
    }


def main():
    files = sys.argv[1:]
    per_recap = []
    for f in files:
        try:
            per_recap.append(extract_one(f))
        except Exception as e:
            print(f"ERR {f}: {e}", file=sys.stderr)
            per_recap.append({"error": str(e), "path": f})

    # Assemble
    bookings = []  # per-booking rows that include Hilmar (from detail or quote tables)
    aggregates = []
    quote_responses = []
    recaps_processed = []
    warnings = []

    for rec in per_recap:
        if "error" in rec:
            warnings.append(f"failed parse: {rec.get('path')}: {rec['error']}")
            continue
        recaps_processed.append(rec["week_label"])
        aggregates.extend(rec["aggregates"])
        quote_responses.extend(rec["quotes"])
        # Convert detail rows to "booking-like" rows in the requested schema
        for d in rec["details"]:
            bookings.append({
                "mdolx_ref": d.get("mdolx_ref"),
                "customer": d["customer"],
                "pol": d.get("pol"),
                "pod": d.get("pod"),
                "qty": d.get("qty"),
                "container_type": d.get("container_type"),
                "carrier": d.get("carrier"),
                "booking_confirmation": d.get("booking_confirmation"),
                "vessel": d.get("vessel"),
                "etd": d.get("etd"),
                "eta": d.get("eta"),
                "comments": d.get("notes"),
                "recap_week": d["recap_week"],
                "source_message_id": d["source_message_id"],
                "source_section": d.get("section"),
            })

    if not bookings:
        warnings.append(
            "No per-booking Hilmar rows found in recaps with full carrier/vessel/ETD/ETA detail. "
            "Linda's recaps do not contain that schema; they report aggregate Hilmar booking counts "
            "(see hilmar_weekly_aggregates) plus occasional Hilmar entries in ISSUES/DISPUTES/PENDING "
            "tables (captured as bookings where available)."
        )

    out = {
        "bookings": bookings,
        "recaps_processed": recaps_processed,
        "total_hilmar_bookings": len(bookings),
        "warnings": warnings,
        "hilmar_weekly_aggregates": aggregates,
        "hilmar_quote_response_time_rows": quote_responses,
        "notes": (
            "Schema note: Linda Echevarria's weekly recaps (2026-02-07 through 2026-04-10) do NOT "
            "contain a per-booking table with Carrier/Vessel/Booking#/ETD/ETA per customer. 'bookings' "
            "here reflects only Hilmar rows appearing in PENDING/ISSUES/DISPUTES tables (customer-"
            "identified but carrier/vessel/dates absent). For aggregate Hilmar booking counts per "
            "week, see 'hilmar_weekly_aggregates' (pulled from Linda's customer TEU pivot). "
            "War-affected table (03/14-20 and 03/28-04/03 recaps) has OL Refs but no customer column, "
            "so we cannot attribute those rows to Hilmar from the recap alone; they are not included."
        ),
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2)
    print(f"wrote {OUT}", file=sys.stderr)
    print(f"  recaps: {len(recaps_processed)}", file=sys.stderr)
    print(f"  bookings (Hilmar, detail rows): {len(bookings)}", file=sys.stderr)
    print(f"  aggregate weeks: {len(aggregates)}", file=sys.stderr)
    print(f"  Hilmar quote-response rows: {len(quote_responses)}", file=sys.stderr)


if __name__ == "__main__":
    main()
