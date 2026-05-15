"""
pdf_parser.py — Extract vessel/ETD/ETA/rate/POL/POD from OL booking-confirmation PDFs.

OL's booking-confirmation emails are signature-only in the BODY — the actual
booking data is in an attached PDF. This module reads those PDFs and returns
the same shape parse_rate_table produces, so patch_carriers PASS 2 can
treat them uniformly.

Added 2026-05-13 per Michael "no.. 90 percent for all is the bare minimum"
— closes the 23 PDF-only WIN gap that text-body extraction couldn't reach.

OL PDF LAYOUT (reverse-engineered from real samples):
    Port of Loading 5/13/2026 Vessel and Voyage No.
    OAKLAND ONE OLYMPUS / 080W
    Port of Discharge 5/25/2026 Place of Delivery by On-Carrier Cut off / Terminal Closing
    TOKYO,JAPAN Closing Date: 5/8/2026 16:00
    Earliest Return Date: 5/4/2026
    ...
    5 x OCEAN FREIGHT CHARGES 285.00 1425.00 USD

The dates on "Port of Loading" / "Port of Discharge" lines are ETD / ETA.
The next line carries POL_CITY + vessel + " / " + voyage and POD_CITY.
Rate is on lines containing "OCEAN FREIGHT CHARGES" — total in 3rd/4th col.

Usage:
    from pdf_parser import parse_booking_pdf
    data = parse_booking_pdf("scripts/stage_pdfs/<imid>.pdf")
"""
from __future__ import annotations
import re
from pathlib import Path

try:
    import pdfplumber  # type: ignore
    _PDFPLUMBER_OK = True
except ImportError:
    _PDFPLUMBER_OK = False


# Carrier names (matched in vessel column or anywhere in text)
_CARRIER_PATTERNS = [
    ("CMA CGM",     re.compile(r"\b(?:CMA\s*CGM|CMA-?CGM)\b", re.I)),
    ("CMA CGM",     re.compile(r"\bCMA\b", re.I)),  # fallback alone
    ("Evergreen",   re.compile(r"\b(?:Evergreen|EMC)\b", re.I)),
    ("MSC",         re.compile(r"\bMSC\b", re.I)),
    ("Maersk",      re.compile(r"\bMaersk\b", re.I)),
    ("ONE",         re.compile(r"\b(?:ONE|Ocean Network Express)\b")),
    ("OOCL",        re.compile(r"\bOOCL\b", re.I)),
    ("HMM",         re.compile(r"\bHMM\b", re.I)),
    ("Yang Ming",   re.compile(r"\b(?:Yang\s*Ming|YML)\b", re.I)),
    ("Hapag-Lloyd", re.compile(r"\b(?:Hapag(?:[\s\-]?Lloyd)?|HLAG)\b", re.I)),
    ("COSCO",       re.compile(r"\bCOSCO\b", re.I)),
]

# Common carrier-vessel-prefix patterns to identify carrier from vessel name
_VESSEL_PREFIX_TO_CARRIER = {
    "ONE":       "ONE",        # ONE OLYMPUS, ONE HAMBURG, etc.
    "EVER":      "Evergreen",  # EVER LEGION, EVER LOGIC
    "CMA CGM":   "CMA CGM",
    "MSC":       "MSC",
    "HMM":       "HMM",
    "YM ":       "Yang Ming",
    "APL ":      "CMA CGM",    # APL is CMA's intra-asia brand
    "OOCL":      "OOCL",
    "JAMAICA":   "Hapag-Lloyd", # JAMAICA EXPRESS is Hapag service
    "PRESIDENT": "CMA CGM",    # PRESIDENT LB JOHNSON
}

# Date pattern: M/D/YYYY or MM/DD/YYYY
_DATE_PAT = re.compile(r"(\d{1,2}/\d{1,2}/\d{4})")


def _extract_pdf_text(pdf_path: Path) -> str:
    """Pull plain text from PDF via pdfplumber. Returns empty string on failure."""
    if not _PDFPLUMBER_OK:
        return ""
    try:
        import pdfplumber  # type: ignore
        with pdfplumber.open(pdf_path) as pdf:
            pages = [(p.extract_text() or "") for p in pdf.pages]
        return "\n".join(pages)
    except Exception:
        return ""


def _normalize_date(s: str) -> str:
    """Convert M/D/YYYY → YYYY-MM-DD ISO format for consistency with body parser."""
    m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", s)
    if m:
        mo, dy, yr = m.group(1).zfill(2), m.group(2).zfill(2), m.group(3)
        return f"{yr}-{mo}-{dy}"
    return s


def parse_booking_pdf(pdf_path: str | Path) -> dict:
    """Extract booking fields from an OL booking-confirmation PDF.

    Returns dict with keys: carrier_quoted, vessel_voyage, etd_offered,
    eta_offered, pol, pod, ol_rate, transshipment, container_size.
    Empty fields are omitted so dict.update() merges safely.
    """
    if not _PDFPLUMBER_OK:
        return {}
    p = Path(pdf_path)
    if not p.exists():
        return {}
    text = _extract_pdf_text(p)
    if not text:
        return {}

    out: dict = {}
    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]

    # Carrier-prefix word list — vessel names start with these
    _CARRIER_VESSEL_PREFIXES = (
        "ONE", "EVER", "CMA", "MSC", "HMM", "OOCL", "APL", "PRESIDENT",
        "JAMAICA", "ZIM", "COSCO", "MAERSK", "EVERGREEN", "HAPAG",
        "YANG", "YM"
    )

    # Walk line by line looking for the structured anchors. OL booking-PDF
    # text uses prefixes like "Place of Receipt Port of Loading <DATE>" —
    # not always starting with "Port of Loading", so use substring check.
    for i, line in enumerate(lines):
        if "Port of Loading" in line:
            m = _DATE_PAT.search(line)
            if m:
                out["etd_offered"] = _normalize_date(m.group(1))
            # Next line: "OAKLAND ONE OLYMPUS / 080W"
            if i + 1 < len(lines):
                nxt = lines[i + 1]
                # Split at " / " to isolate voyage from vessel+POL
                voy_parts = nxt.split(" / ", 1)
                if len(voy_parts) == 2:
                    voyage = voy_parts[1].split()[0] if voy_parts[1].split() else ""
                    prefix_words = voy_parts[0].split()
                    # Find vessel start: first word matching a carrier prefix
                    vessel_idx = None
                    for idx, w in enumerate(prefix_words):
                        if any(w.upper().startswith(cs) for cs in _CARRIER_VESSEL_PREFIXES):
                            vessel_idx = idx
                            break
                    if vessel_idx is not None and vessel_idx > 0:
                        out["pol"] = " ".join(prefix_words[:vessel_idx]).title()
                        vessel = " ".join(prefix_words[vessel_idx:])
                        out["vessel_voyage"] = f"{vessel} {voyage}".strip()
                    elif prefix_words:
                        # Fallback: take first word as POL
                        out["pol"] = prefix_words[0].title()
        elif "Port of Discharge" in line:
            m = _DATE_PAT.search(line)
            if m:
                out["eta_offered"] = _normalize_date(m.group(1))
            if i + 1 < len(lines):
                nxt = lines[i + 1]
                # POD is the city name, often "TOKYO,JAPAN" or just "TOKYO"
                pod_m = re.match(r"^([A-Z]+(?:,[A-Z]+)?)", nxt)
                if pod_m:
                    pod_raw = pod_m.group(1)
                    # Strip country suffix like ",JAPAN" — keep just the city
                    pod_city = pod_raw.split(",")[0]
                    out["pod"] = pod_city.title()
        # Direct sailing indicator
        elif "DIRECT SAILING" in line.upper():
            out["transshipment"] = "DIRECT"
        # Container counts: "5 x 40' HC" or "5x40'HC"
        elif re.search(r"\d+\s*[xX]\s*\d{2}'?[A-Z]{2,3}", line):
            cm = re.search(r"(\d+\s*[xX]\s*\d{2}'?[A-Z]{2,3})", line)
            if cm and "container_size" not in out:
                out["container_size"] = cm.group(1).replace(" ", "").upper()
        # Rate line: "5 x OCEAN FREIGHT CHARGES 285.00 1425.00 USD"
        elif "OCEAN FREIGHT" in line.upper() and "CHARGES" in line.upper():
            # Pull numbers from the line
            nums = re.findall(r"\b(\d+(?:,\d{3})*(?:\.\d{1,2})?)\b", line)
            # Usually [count, per_unit, total]. Total is the largest.
            try:
                vals = [float(n.replace(",", "")) for n in nums if "." in n or len(n) >= 3]
                if vals:
                    out["ol_rate"] = max(vals)
            except ValueError:
                pass

    # Carrier inference: scan ALL text, prefer earliest match
    best_pos = None
    best_canon = None
    for canonical, pat in _CARRIER_PATTERNS:
        m = pat.search(text)
        if m and (best_pos is None or m.start() < best_pos):
            best_pos = m.start()
            best_canon = canonical
    if best_canon:
        out["carrier_quoted"] = best_canon
    # Vessel-prefix fallback (more reliable than free-text carrier scan for ONE/EVER)
    if out.get("vessel_voyage"):
        for prefix, carrier in _VESSEL_PREFIX_TO_CARRIER.items():
            if out["vessel_voyage"].upper().startswith(prefix):
                out["carrier_quoted"] = carrier
                break

    return out
