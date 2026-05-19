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
    # 2026-05-19 expansions to close the 26-PDF carrier gap
    "NYK":       "ONE",        # NYK ORION/THERMIDOR/etc. are ONE-alliance vessels
    "CONTI":     "ONE",        # CONTI CONQUEST etc. — chartered/feeder ONE
    "HYUNDAI":   "HMM",        # HYUNDAI BANGKOK, etc.
    "MOL":       "ONE",        # MOL = Mitsui O.S.K. Lines — ONE parent
    "K LINE":    "ONE",        # K LINE = Kawasaki — ONE parent
    "ZIM":       "ZIM",
    "COSCO":     "COSCO",
    "WAN HAI":   "Wan Hai",
    "ITAL":      "Evergreen",  # ITAL feeder partnership with Evergreen
    "EXPRESS":   "Hapag-Lloyd", # GLASGOW EXPRESS, ALGECIRAS EXPRESS, etc.
}

# Booking-ref prefix → carrier. The OL PDF stores the carrier booking
# number near "Carrier Booking No." or as "REF.:" — the alpha prefix
# identifies the carrier in most cases (industry-standard naming).
_BOOKING_PREFIX_TO_CARRIER = {
    "NAM":   "CMA CGM",
    "APL":   "CMA CGM",
    "CGM":   "CMA CGM",
    "EBKG":  "MSC",
    "MEDU":  "MSC",
    "RICG":  "ONE",
    "SCNB":  "ONE",
    "ONEY":  "ONE",
    "MAEU":  "Maersk",
    "TLL":   "Yang Ming",
    "YMLU":  "Yang Ming",
    "HLBU":  "Hapag-Lloyd",
    "HLCU":  "Hapag-Lloyd",
    "GVT":   "Hapag-Lloyd",
    "EGLV":  "Evergreen",
    "EITU":  "Evergreen",
    "HMMU":  "HMM",
    "OOLU":  "OOCL",
    "OOCU":  "OOCL",
    "COSU":  "COSCO",
    "ZIMU":  "ZIM",
    "WHLC":  "Wan Hai",
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


def parse_booking_pdf(pdf_path: str | Path, *, allow_llm: bool = True) -> dict:
    """Extract booking fields from an OL booking-confirmation PDF.

    Returns dict with keys: carrier_quoted, vessel_voyage, etd_offered,
    eta_offered, pol, pod, ol_rate, transshipment, container_size,
    erd, doc_cutoff, port_cutoff, mdolx_ref, product, temperature,
    origin_free_time, dest_free_time, booking_ref.
    Empty fields are omitted so dict.update() merges safely.

    2026-05-19 parser-gap fix (Michael "PARSER MUST REACH 95 PERCENT AT A
    MINIMUM AND INCLUDE ATTACHMENTS"): added ERD + doc/port cutoff +
    free-time + commodity + booking-ref extraction. OL booking PDFs are
    the ONLY place these fields appear consistently — email body has them
    sometimes; the PDF has them always.

    2026-05-19 evening LLM rescue (Michael "go with task 11 and llm"):
    when pdfplumber returns empty text (image-only scanned PDF — about
    3 of 110 booking PDFs in current corpus), fall back to Claude's
    document-input API via pdf_llm_rescue.extract_from_pdf. Costs ~$0.001
    per PDF and caches result in data/pdf_llm_cache.json so same PDF
    never costs twice. Set allow_llm=False to skip the rescue (e.g. in
    one-off tests where we don't want surprise API charges).
    """
    if not _PDFPLUMBER_OK:
        return {}
    p = Path(pdf_path)
    if not p.exists():
        return {}
    text = _extract_pdf_text(p)
    if not text:
        # Image-only PDF — try the LLM rescue if enabled. Returns None if
        # the rescue is disabled (no API key) OR fails (logged, doesn't raise).
        if allow_llm:
            try:
                from pdf_llm_rescue import extract_from_pdf as _llm_extract
                rescued = _llm_extract(p)
                if rescued:
                    return rescued
            except ImportError:
                pass  # rescue module not on path — degrade gracefully
            except Exception:
                pass  # rescue threw — degrade gracefully, never crash patch_carriers
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
        # Container counts: "5 x 40' HC" or "5x40'HC" — capture ALL occurrences
        # because PDFs list each container as its own line (1 x 40' HC,
        # 1 x 40' HC, 1 x 40' HC for a 3-container booking). 2026-05-19 fix
        # to surface container_count + teu_requested on standalone WIN rows
        # whose subject doesn't carry an MDOLX container marker.
        elif re.search(r"\d+\s*[xX]\s*\d{2}'?[A-Z]{2,3}", line):
            cm = re.search(r"(\d+)\s*[xX]\s*(\d{2})'?([A-Z]{2,3})?", line)
            if cm:
                qty = int(cm.group(1))
                size = cm.group(2)
                eq = (cm.group(3) or "").upper()
                # Validate size — booking PDFs use 20' or 40' (occasionally 45')
                if size in ("20", "40", "45"):
                    out.setdefault("_pdf_containers", []).append((qty, size, eq))
                    if "container_size" not in out:
                        out["container_size"] = f"{qty}X{size}'{eq}".strip("'")
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

    # ─────────────────────────────────────────────────────────────────────
    # 2026-05-19 parser-gap fix — extract the previously-missed fields.
    # All operate on the full text (joined lines) since OL's PDFs aren't
    # strictly line-aligned — anchors may span newlines.
    # ─────────────────────────────────────────────────────────────────────
    flat = " ".join(lines)

    # MDOLX booking ref: "BOOKING CONFIRMATION MDOLX260409"
    m = re.search(r"\bMDOLX\s*(\d{6,})\b", flat, re.IGNORECASE)
    if m:
        out["mdolx_ref"] = m.group(1)

    # Carrier booking number. OL PDFs put it in two places:
    #   1. Right after "Carrier Booking No. AES Authorization" header
    #      (sometimes alpha-prefixed like RICGH7587500, sometimes numeric
    #      like 404640318443 = AES auth, NOT a carrier ref)
    #   2. As "REF.: <BOOKING_REF>" (this one is more reliable)
    #
    # Take the alpha-prefixed match first (RICGH/NAM/EBKG/etc.) since
    # those are real carrier booking refs. Fall back to the numeric AES
    # authorization for record-keeping only.
    m = re.search(r"REF\.\s*:\s*([A-Z]{3,5}\d{5,})", flat)
    if m:
        out["booking_ref"] = m.group(1)
    else:
        m = re.search(r"\bCarrier\s+Booking\s+No\.[^A-Z\d]*([A-Z]{3,5}\d{5,}|\d{8,})",
                      flat, re.IGNORECASE)
        if m:
            out["booking_ref"] = m.group(1)

    # ERD: "Earliest Return Date: 5/4/2026"
    m = re.search(r"Earliest\s+Return\s+Date\s*[:\-]?\s*(\d{1,2}/\d{1,2}/\d{4})",
                  flat, re.IGNORECASE)
    if m:
        out["erd"] = _normalize_date(m.group(1))
        # Also expose under origin_cutoff alias for back-compat with the
        # text-body rate-table parser.
        out["origin_cutoff"] = out["erd"]

    # Port / Terminal closing: "Closing Date: 5/8/2026 16:00"
    m = re.search(r"Closing\s+Date\s*[:\-]?\s*(\d{1,2}/\d{1,2}/\d{4})",
                  flat, re.IGNORECASE)
    if m:
        out["port_cutoff"] = _normalize_date(m.group(1))

    # Doc due / document cutoff: "DOCUMENT DUE DATE: 5/6/2026 12:00"
    m = re.search(r"DOCUMENT\s+DUE\s+DATE\s*[:\-]?\s*(\d{1,2}/\d{1,2}/\d{4})",
                  flat, re.IGNORECASE)
    if m:
        out["doc_cutoff"] = _normalize_date(m.group(1))

    # Free time line: "14 DETENTION + 14 DEMURRAGE FREE DAYS"
    # OL booking PDFs put a single combined line (not split origin/dest like
    # the rate-table emails). Per Michael 2026-04-30: PDF's free-time line
    # is the DESTINATION side (origin free time is set by the trucker, not
    # contract). Map to dest_free_time.
    ft_patterns = [
        r"(\d+\s+DETENTION\s+\+\s+\d+\s+DEM[UR]+R?AGE\s+FREE\s+DAYS)",
        r"(\d+\s+COMBINED\s+FREE\s+DAYS)",
        r"(\d+\s+DAYS?\s+FREE\s+(?:DETENTION|DEMURRAGE|COMBINED))",
    ]
    free_time_matches = []
    for pat in ft_patterns:
        for m in re.finditer(pat, flat, re.IGNORECASE):
            free_time_matches.append((m.start(), m.group(1).strip()))
    free_time_matches.sort(key=lambda x: x[0])
    if free_time_matches:
        # First occurrence = destination free time (above the carrier line in PDFs).
        # If two are present, the second is origin free time.
        out["dest_free_time"] = free_time_matches[0][1]
        if len(free_time_matches) >= 2:
            out["origin_free_time"] = free_time_matches[1][1]

    # Product / commodity. OL PDFs list it under "Description of Packages and Goods"
    # block. After the container-size line, the goods line carries the commodity.
    # Patterns: "LACTOSE" / "CHEESE" / "WPC 80" / "MILK PROTEIN" etc.
    # Use the same commodity dictionary as body_parser.parse_product.
    _PRODUCT_DICT = (
        ("skim milk powder", "Skim Milk Powder"),
        ("whole milk powder", "Whole Milk Powder"),
        ("milk powder", "Milk Powder"),
        ("anhydrous milk fat", "Anhydrous Milk Fat"),
        ("milk protein isolate", "Milk Protein Isolate"),
        ("milk protein concentrate", "Milk Protein Concentrate"),
        ("whey protein concentrate", "Whey Protein Concentrate"),
        ("whey protein isolate", "Whey Protein Isolate"),
        ("lactose", "Lactose"),
        ("cheese", "Cheese"),
        ("whey", "Whey"),
        ("casein", "Casein"),
        ("butter", "Butter"),
        ("wpc 80", "WPC 80"),
        ("wpc", "WPC"),
        ("wpi", "WPI"),
        ("mpc", "MPC"),
        ("mpi", "MPI"),
        ("amf", "AMF"),
        ("protein", "Protein"),
    )
    low = flat.lower()
    for needle, canonical in _PRODUCT_DICT:
        # Word-boundary match
        if re.search(rf"\b{re.escape(needle)}\b", low):
            out["product"] = canonical
            break

    # Temperature — look for "+2C" / "-2C" / "34F" / "Set Point: X" patterns.
    # Reefer bookings sometimes carry "TEMP: -2C" or similar in the
    # Special Instructions block.
    tm = re.search(
        r"(?:temp(?:erature)?|set\s*point)\s*[:\-]?\s*([+\-]?\d{1,2})\s*°?\s*([CF])\b",
        flat, re.IGNORECASE,
    )
    if tm:
        sign, val, unit = tm.group(1)[:1] if tm.group(1)[:1] in "+-" else "", \
                          re.sub(r"[+\-]", "", tm.group(1)), tm.group(2).upper()
        try:
            v = int(val)
            if (unit == "C" and -40 <= (-v if sign == "-" else v) <= 30) or \
               (unit == "F" and -40 <= (-v if sign == "-" else v) <= 120):
                out["temperature"] = f"{sign if sign == '-' else ''}{val}{unit}"
        except ValueError:
            pass
    elif not out.get("temperature"):
        # Bare numeric "+2C" / "-2C" / "34F" anywhere in the PDF (no anchor)
        bm = re.search(r"(?:^|\s)([+\-]?\d{1,2})\s*°?\s*([CF])\b", flat)
        if bm:
            sign = bm.group(1)[:1] if bm.group(1)[:1] in "+-" else ""
            val = re.sub(r"[+\-]", "", bm.group(1))
            unit = bm.group(2).upper()
            try:
                v = int(val)
                if (unit == "C" and -40 <= (-v if sign == "-" else v) <= 30) or \
                   (unit == "F" and -40 <= (-v if sign == "-" else v) <= 120):
                    # Reject if preceded by digit (e.g. "234F")
                    pos = bm.start(1)
                    if pos == 0 or not flat[pos-1].isdigit():
                        out["temperature"] = f"{sign if sign == '-' else ''}{val}{unit}"
            except ValueError:
                pass

    # Rate-expiry — booking PDFs rarely carry it explicitly, but the
    # confirmation date itself acts as the effective rate-lock date.
    # Pattern: "WESTBURY, APRIL 15, 2026" or "confirmation_olusa 4/15/2026"
    # Future enhancement: derive booking_date + 7-day standard validity.

    # ─────────────────────────────────────────────────────────────────────
    # Carrier inference. Priority order (highest confidence first):
    #   1. Vessel-prefix match (ONE OLYMPUS → ONE, EVER LEGION → Evergreen).
    #      This is the strongest signal because the vessel name uniquely
    #      identifies the operator.
    #   2. Free-text scan (_CARRIER_PATTERNS) for canonical names.
    #
    # NOT used as a signal: "SHIPPING LINE: OL USA VIA <X> SHIPPING AGENCY" —
    # that's OL USA's parent-agency boilerplate (OL operates as an NVOCC under
    # Evergreen's commercial license) and is the SAME string on every PDF
    # regardless of actual carrier. Caught 2026-05-19 when the new sweep
    # showed every PDF reporting Evergreen carrier even when vessel was ONE.
    # ─────────────────────────────────────────────────────────────────────
    if out.get("vessel_voyage"):
        for prefix, carrier in _VESSEL_PREFIX_TO_CARRIER.items():
            if out["vessel_voyage"].upper().startswith(prefix):
                out["carrier_quoted"] = carrier
                break
    # Booking-ref prefix is the SECOND-strongest signal (alpha prefix maps
    # to the carrier per industry SCAC convention). RICGH7587500 → ONE.
    if not out.get("carrier_quoted") and out.get("booking_ref"):
        ref = out["booking_ref"].upper()
        for prefix, carrier in _BOOKING_PREFIX_TO_CARRIER.items():
            if ref.startswith(prefix):
                out["carrier_quoted"] = carrier
                break
    # SHIPPING LINE VIA <CARRIER> hint at the bottom of the PDF. SKIP when
    # the value is "EVERGREEN SHIPPING AGENCY" (parent agency boilerplate)
    # but trust shorter forms like "VIA ONE" / "VIA HMM" — those are the
    # actual carrier.
    if not out.get("carrier_quoted"):
        via = re.search(
            r"SHIPPING\s+LINE\s*[:\-]?\s*OL\s+USA\s+VIA\s+([A-Z][A-Z\s\-]{2,30}?)"
            r"(?:\s+SHIPPING\b|\s*$|\n)",
            flat, re.IGNORECASE,
        )
        if via:
            tail = via.group(1).strip().upper()
            # Reject the Evergreen-agency boilerplate
            if not tail.startswith("EVERGREEN"):
                for raw, canonical in (
                    ("ONE", "ONE"),
                    ("HMM", "HMM"),
                    ("CMA", "CMA CGM"),
                    ("MSC", "MSC"),
                    ("OOCL", "OOCL"),
                    ("MAERSK", "Maersk"),
                    ("YANG MING", "Yang Ming"),
                    ("HAPAG", "Hapag-Lloyd"),
                    ("COSCO", "COSCO"),
                    ("ZIM", "ZIM"),
                    ("WAN HAI", "Wan Hai"),
                ):
                    if raw in tail:
                        out["carrier_quoted"] = canonical
                        break
    # Last resort: free-text carrier name scan
    if not out.get("carrier_quoted"):
        best_pos = None
        best_canon = None
        for canonical, pat in _CARRIER_PATTERNS:
            m = pat.search(text)
            if m and (best_pos is None or m.start() < best_pos):
                # Skip matches inside the OL boilerplate "SHIPPING LINE VIA"
                # context — that's parent-agency, not booking carrier.
                ctx_start = max(0, m.start() - 50)
                ctx = text[ctx_start:m.start()].upper()
                if "VIA" in ctx and "SHIPPING LINE" in text[max(0,m.start()-80):m.start()].upper():
                    continue
                best_pos = m.start()
                best_canon = canonical
        if best_canon:
            out["carrier_quoted"] = best_canon

    # 2026-05-19 parser-gap fix: compute container_count + teu_requested
    # from the list of "N x SIZE EQ" occurrences captured above. PDFs list
    # each container on its own line so SUM the quantities. Drop the
    # raw accumulator from the output — only the computed totals ship.
    pdf_containers = out.pop("_pdf_containers", [])
    if pdf_containers:
        total_count = sum(qty for qty, _, _ in pdf_containers)
        total_teu = sum(qty * (2 if size == "40" else (2 if size == "45" else 1))
                        for qty, size, _ in pdf_containers)
        out["container_count"] = total_count
        out["teu_requested"] = total_teu
        # Also format a "containers" string for the row display.
        # Group by (size, eq): e.g. {(40, HC): 3} → "3-40'HC"
        from collections import Counter
        groups = Counter()
        for qty, size, eq in pdf_containers:
            groups[(size, eq)] += qty
        parts = []
        for (size, eq), qty in sorted(groups.items(), key=lambda x: (-x[1], x[0])):
            seg = f"{qty}-{size}'"
            if eq:
                seg += eq
            parts.append(seg)
        out["containers"] = " + ".join(parts)

    return out
