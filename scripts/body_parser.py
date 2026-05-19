#!/usr/bin/env python3
"""
body_parser.py -- Regex-based parsers for email bodies + MDOLX subjects.

Pure-function module. No IO. All parsers return None when unsure (never guess).
Ingest merges these values ON TOP OF the existing preview-based extractions.

Parsers:
  parse_subject_lane(subject)       -> (origin, destination) for MDOLX subjects
  parse_eta_requested(text)         -> ISO date  (Lonny's target cutoff/ETD)
  parse_etd_requested(text)         -> ISO date  (Lonny's departure-side ask)
  parse_eta_offered(text)           -> ISO date  (OL's quoted ETA)
  parse_etd_offered(text)           -> ISO date  (OL's quoted ETD/sailing)
  parse_vessel(text)                -> "Vessel Name / V.123N"
  parse_transshipment(text)         -> "SIN" | "Direct" | None
  parse_rate_table(text)            -> dict (carrier/ol_rate/etd/vessel/rate_expiry/
                                              origin_free_time/dest_free_time/erd/...)
  parse_send_signal(text)           -> bool   (Lonny says "send"/"book")
  parse_origin_cutoff(text)         -> ISO date
  parse_temperature(text)           -> "-2C" | "34F" | "Frozen" | None  (reefer rows)
  parse_product(text)               -> "Lactose" | "Cheese" | ...      (commodity)
  parse_requested_dates(text)       -> "Cutoff next week or the following" | None
  parse_lonny_notes(text)           -> free-text Lonny-side notes
  html_to_text(html)                -> plaintext

Parser-gap fixes 2026-05-19 (per Michael "no field should be empty ever"):
  - parse_temperature / parse_product / parse_requested_dates / parse_etd_requested /
    parse_lonny_notes — extract Lonny-side fields previously 100% empty
  - parse_rate_table now also surfaces rate_expiry, origin_free_time, dest_free_time,
    and erd (previously buried in the table cells dict but never returned)
"""
from __future__ import annotations

import re
from datetime import datetime, date
from html.parser import HTMLParser
from typing import Optional


# HTML -> text
class _HTMLStripper(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0
    def handle_starttag(self, tag, attrs):
        if tag in ("style", "script"):
            self._skip += 1
    def handle_endtag(self, tag):
        if tag in ("style", "script") and self._skip:
            self._skip -= 1
        if tag in ("p", "br", "div", "tr", "li"):
            self.parts.append("\n")
        # Insert column separator at end of every cell so adjacent <td>HMM RUBY</td>
        # <td>0012W</td> doesn't smash into "HMM RUBY0012W" (which broke parse_vessel).
        # Michael 2026-04-30 — vessel coverage 0/97 is the symptom. Pipe is unambiguous
        # and survives the whitespace collapse below.
        if tag in ("td", "th"):
            self.parts.append(" | ")
    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)

_CAUTION_BANNER_RX = re.compile(
    r"CAUTION:\s*THIS EMAIL ORIGINATED FROM OUTSIDE OF OUR COMPANY\.?"
    r"(?:\s*DO NOT CLICK LINKS OR OPEN ANY ATTACHMENTS UNLESS YOU RECOGNIZE THE SENDER AND KNOW THE CONTENT IS SAFE\.?)?",
    re.IGNORECASE,
)

def html_to_text(html: str | None) -> str:
    if not html:
        return ""
    s = _HTMLStripper()
    try:
        s.feed(html)
    except Exception:
        return re.sub(r"<[^>]+>", " ", html)
    text = "".join(s.parts)
    text = re.sub(r"\r", "", text)
    # Strip the OL external-sender CAUTION banner before downstream parsers see it
    # (otherwise it bleeds into containers/preview fields). Michael 2026-04-30.
    text = _CAUTION_BANNER_RX.sub("", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ---------- Subject lane parser ----------

_ORIGIN_ALIASES = {
    "hilmar, ca": "Hilmar",
    "hilmar ca":  "Hilmar",
    "slc":        "Salt Lake City",
    "oak":        "Oakland",
    "dfw":        "Dallas",
}

_KNOWN_ORIGINS = [
    "Hilmar, CA", "Hilmar",
    "Salt Lake City", "SLC",
    "Oakland", "OAK",
    "Chicago",
    "Dalhart, TX", "Dalhart",
    "Tulare, CA", "Tulare",
    "Visalia, CA", "Visalia",
    "Modesto, CA", "Modesto",
    "Fresno, CA", "Fresno",
    "Los Angeles", "LAX",
    "Houston",
    "Seattle",
    "Tacoma",
    "Portland",
    "Long Beach",
]

_LANE_RX_B = re.compile(
    r"(?P<origin>HILMAR(?:,?\s*CA)?)\s*[-\u2013]+>\s*(?P<dest>[A-Z][A-Za-z\.\s,]+?)(?:\s*//|\s*\d|$)",
    re.IGNORECASE,
)

_CONTAINER_MARK_RX = re.compile(
    r"\b\d+\s*[xX\u00d7]\s*\d{2}'?\s*(HC|RF|DV|GP|FR|OT|Reefer|Door|Chill|Flex)?\b",
    re.IGNORECASE,
)

# Use for extraction (vs the strip-only _CONTAINER_MARK_RX). Captures the
# "qty x size [type]" tokens out of MDOLX confirmation subjects like
#   "HILMAR 2X40'RF Oakland to Yokohama"
#   "HILMAR 1x20'DV Oakland to HCMC (Cat Lai)"
#   "HILMAR - Oakland to Bangkok - 1X20'DV"
#   "HILMAR 3x20'DV Oakland to Xingang"
_CONTAINER_EXTRACT_RX = re.compile(
    r"(\d+)\s*[xX\u00d7]\s*(\d{2})'?\s*(HC|RF|DV|GP|FR|OT|Reefer|Door|Chill|Flex)?",
    re.IGNORECASE,
)


def parse_subject_containers(subject: str | None) -> str | None:
    """Extract a normalized container string ("2-40'RF") from an MDOLX subject.

    Returns a string like "2-40'RF + 1-20'DV" or None when nothing matches.
    Format matches what core.parse_teu() expects, so the caller can do:
        cont = parse_subject_containers(subject)
        n, teu = core.parse_teu(cont)
    """
    if not subject:
        return None
    parts: list[str] = []
    for m in _CONTAINER_EXTRACT_RX.finditer(subject):
        qty, size, kind = m.group(1), m.group(2), (m.group(3) or "").upper()
        # Drop garbage matches like "260491" being read as "26x0491" (size 04
        # filters out via the explicit (20|40|45) check below).
        if size not in ("20", "40", "45"):
            continue
        seg = f"{qty}-{size}'"
        if kind:
            seg += kind
        parts.append(seg)
    return " + ".join(parts) if parts else None

def _norm(s: str) -> str:
    s = s.strip().strip(",.")
    key = s.lower()
    if key in _ORIGIN_ALIASES:
        return _ORIGIN_ALIASES[key]
    if s.isupper():
        return s.title() if len(s) > 3 else s
    return s

def _scan_for_origin(segment: str):
    low = segment.lower()
    best = None
    for o in _KNOWN_ORIGINS:
        idx = low.find(o.lower())
        if idx != -1:
            end = idx + len(o)
            if best is None or idx < best[1]:
                best = (o, idx, end)
    if best:
        return best[0], best[2]
    return None

# Carriers that show up as the "// CARRIER: BOOKING_REF" trailer on MDOLX
# subjects. Order matters — longer multi-word names FIRST so 'CMA CGM' wins
# before 'CMA'. Values map raw → canonical (the canonical form is what core's
# normalize_carrier already accepts).
_SUBJECT_CARRIER_TOKENS = [
    ("CMA CGM",          "CMA CGM"),
    ("CMA-CGM",          "CMA CGM"),
    ("CMACGM",           "CMA CGM"),
    ("HAPAG-LLOYD",      "Hapag-Lloyd"),
    ("HAPAG LLOYD",      "Hapag-Lloyd"),
    ("YANG MING",        "Yang Ming"),
    ("WAN HAI",          "Wan Hai"),
    ("EVERGREEN",        "Evergreen"),
    ("MAERSK",           "Maersk"),
    ("HAMBURG SUD",      "Maersk"),
    ("SEALAND",          "Maersk"),
    ("COSCO",            "COSCO"),
    ("MSC",              "MSC"),
    ("ONE",              "ONE"),
    ("HMM",              "HMM"),
    ("OOCL",             "OOCL"),
    ("ZIM",              "ZIM"),
    ("APL",              "CMA CGM"),
    ("ANL",              "CMA CGM"),
    ("CMA",              "CMA CGM"),
    ("CGM",              "CMA CGM"),
]

# Booking-ref prefixes that indicate the carrier (e.g. NAM... = CMA CGM, EBKG... = MSC).
# Used as a tertiary fallback when the carrier word itself isn't on the subject line.
_BOOKING_PREFIX_TO_CARRIER = {
    "NAM":  "CMA CGM",
    "EBKG": "MSC",
    "MEDU": "MSC",
    "RICG": "ONE",
    "SCNB": "ONE",
    "MAEU": "Maersk",
}

def parse_subject_carrier(subject):
    """Extract the winning carrier from an MDOLX confirmation subject.

    Real subjects look like:
      MDOLX260453_UPDATED BOOKING CONFIRMATION// BTG 1X40'HC ... // MSC: EBKG16491184
      MDOLX260114 / 2x40'RF CMA: NAM8322223
      MDOLX260473 ... // CMA BKG # NAM8451437
      MDOLX260407 ... // EVERGREEN

    Returns the canonical carrier name (matching core.normalize_carrier output)
    or None if no signal found.
    """
    if not subject:
        return None
    up = subject.upper()
    # Pattern A: "// CARRIER: REF" or "/ CARRIER: REF"
    for raw, canonical in _SUBJECT_CARRIER_TOKENS:
        # CARRIER followed by optional " BKG #" / " :" / ":" then a booking ref
        rx = re.compile(rf"\b{re.escape(raw)}\b\s*(?:BKG\s*#?|BOOKING\s*#?)?\s*[:#]?\s*[A-Z]{{2,5}}\d{{4,}}")
        if rx.search(up):
            return canonical
    # Pattern B: trailing "// CARRIER" with no ref
    m = re.search(r"//\s*([A-Z][A-Z\s\-]{2,20})\s*$", subject)
    if m:
        tail = m.group(1).strip().upper()
        for raw, canonical in _SUBJECT_CARRIER_TOKENS:
            if tail.startswith(raw):
                return canonical
    # Pattern C: any carrier token anywhere on the subject (only run for MDOLX subjects
    # to avoid matching "CMA UPDATES" type chatter on non-booking emails).
    if "MDOLX" in up:
        for raw, canonical in _SUBJECT_CARRIER_TOKENS:
            if re.search(rf"\b{re.escape(raw)}\b", up):
                return canonical
        # Pattern D: booking-ref prefix → carrier
        m = re.search(r"\b(NAM|EBKG|MEDU|RICG|SCNB|MAEU)\d{6,}\b", up)
        if m:
            return _BOOKING_PREFIX_TO_CARRIER.get(m.group(1))
    return None


def parse_subject_lane(subject):
    if not subject:
        return None, None
    s = subject
    s = re.sub(r"^\s*(re|fw|fwd):\s*", "", s, flags=re.IGNORECASE)
    is_mdolx = bool(re.search(r"\bMDOLX\d+", s, re.IGNORECASE))
    s = re.sub(
        r"MDOLX\s*\d+_?\s*\*?(NEW|REVISED|UPDATED)?\s*(BOOKING|TRANSPORT)?\s*(CONFIRMATION|ORDER|SCHEDULE)?\s*//?\s*",
        " ", s, flags=re.IGNORECASE)
    s = _CONTAINER_MARK_RX.sub(" ", s)
    s = re.sub(r"\s*\(\s*\d+\s*\)\s*$", "", s)
    # MDOLX confirmation subjects open with "HILMAR" as a CUSTOMER tag, not an
    # origin (e.g. "MDOLX260432_ ... HILMAR 2X40'RF Oakland to Yokohama// CMA: ...").
    # Without this strip, _scan_for_origin finds "Hilmar" first and reports
    # "Hilmar → Yokohama" — the screenshot Michael flagged 2026-05-05
    # rows 39-44. After the MDOLX_ prefix is removed and the container chunk
    # is collapsed, the leading "HILMAR" (with optional " - " separator)
    # always precedes the real origin city, so it's safe to strip here.
    if is_mdolx:
        s = re.sub(r"^\s*HILMAR(?:,?\s*CA)?\s*[-:/]?\s*", " ", s, flags=re.IGNORECASE)

    m = _LANE_RX_B.search(s)
    if m:
        return _norm(m.group("origin")), _norm(m.group("dest"))

    found = _scan_for_origin(s)
    if found:
        origin, end_idx = found
        tail = s[end_idx:]
        dm = re.search(
            r"\s+to\s+(?P<dest>[A-Z][A-Za-z\.\s,]+?\s*\([A-Za-z ]+\))"
            r"(?=\s*(?://|\s-\s|/|$|\s+\d))",
            tail, re.IGNORECASE,
        )
        if not dm:
            dm = re.search(
                r"\s+to\s+(?P<dest>[A-Z][A-Za-z\.\s,]+?)"
                r"(?=\s*(?://|\s-\s|/|$|\s+\d))",
                tail, re.IGNORECASE,
            )
        if dm:
            dest = dm.group("dest").strip().strip(",.-/")
            return _norm(origin), _norm(dest)

    gm = re.search(
        r"(?P<origin>[A-Z][A-Za-z\.]{3,}(?:\s*,\s*[A-Z]{2})?(?:\s+[A-Z][A-Za-z\.]+){0,2})"
        r"\s+to\s+"
        r"(?P<dest>[A-Z][A-Za-z\.]{3,}(?:\s*,\s*[A-Za-z\.]+)?)"
        r"(?:\s*(?://|\s-\s|/|$|\s+\d))",
        s,
    )
    if gm:
        origin = gm.group("origin").strip()
        dest = gm.group("dest").strip().strip(",.-")
        if origin.lower() in ("re", "fw", "fwd", "pls", "need", "the"):
            return None, None
        return _norm(origin), _norm(dest)
    return None, None


# ---------- Date parsers ----------

_MONTHS = {
    "jan":1,"january":1,"feb":2,"february":2,"mar":3,"march":3,
    "apr":4,"april":4,"may":5,"jun":6,"june":6,"jul":7,"july":7,
    "aug":8,"august":8,"sep":9,"sept":9,"september":9,
    "oct":10,"october":10,"nov":11,"november":11,"dec":12,"december":12,
}

_DATE_RXES = [
    re.compile(r"\b(?P<d>\d{1,2})[-\s/](?P<m>jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*"
               r"(?:[-\s/](?P<y>\d{2,4}))?\b", re.IGNORECASE),
    re.compile(r"\b(?P<m>jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*[\s/]+(?P<d>\d{1,2})"
               r"(?:[\s,/]+(?P<y>\d{2,4}))?\b", re.IGNORECASE),
    re.compile(r"\b(?P<mo>\d{1,2})[/-](?P<d>\d{1,2})(?:[/-](?P<y>\d{2,4}))?\b"),
]

def _date_from_match(m, default_year: int):
    try:
        if "m" in m.groupdict() and m.group("m"):
            mon = _MONTHS.get(m.group("m").lower()[:3])
            d = int(m.group("d"))
            y = int(m.group("y")) if m.group("y") else default_year
        else:
            mon = int(m.group("mo"))
            d = int(m.group("d"))
            y = int(m.group("y")) if m.group("y") else default_year
        if y < 100:
            y += 2000
        return date(y, mon, d).isoformat()
    except (ValueError, TypeError):
        return None

# Phrases that indicate a number range is NOT a date (free-time, demurrage,
# detention, container counts, etc.). If a date candidate is immediately
# followed by one of these, reject it. Fix: 2026-05-04, "10-14 days free"
# was being parsed as Oct 14 by the cutoff anchor.
_NOT_A_DATE_TAIL = re.compile(
    r"\s*(?:days?\s+(?:free|demurrage|detention|combined|equipment)"
    r"|\s*days?\b|teu|containers?|hc\b|reefer|dry\b|rf\b|free)",
    re.IGNORECASE,
)
# Likewise, reject dates preceded by "x" (e.g., "10x40'HC") which are
# container-count formats, not dates.
_NOT_A_DATE_LEAD = re.compile(r"\d\s*[xX]\s*$")


def _find_date_near(text, anchor_rx, window=120):
    """Find first plausible date after each anchor match. Reject candidates
    that look like demurrage/free-time ranges (e.g. '10-14 days free')
    or container counts (e.g. '10x40HC')."""
    if not text:
        return None
    now_year = datetime.utcnow().year
    for am in anchor_rx.finditer(text):
        start = am.end()
        chunk = text[start:start+window]
        for drx in _DATE_RXES:
            for dm in drx.finditer(chunk):
                tail = chunk[dm.end():dm.end()+30]
                if _NOT_A_DATE_TAIL.match(tail):
                    continue
                lead = chunk[max(0, dm.start()-4):dm.start()]
                if _NOT_A_DATE_LEAD.search(lead):
                    continue
                iso = _date_from_match(dm, now_year)
                if iso:
                    return iso
    return None


_ETA_REQ_ANCHORS = re.compile(
    r"(?:need(?:s|ed)?\s+(?:to\s+)?(?:sail|ship|load|depart|leave)"
    r"|target\s+etd|requested\s+etd|require(?:d)?\s+by"
    r"|sailing\s+by|ship\s+by|load(?:ing)?\s+by|cutoff"
    r"|prefer(?:red)?\s+etd)",
    re.IGNORECASE,
)

_ETD_OFFER_ANCHORS = re.compile(r"(?:etd|ets|sailing\s+date|departure)\s*[:\-]?", re.IGNORECASE)
_ETA_OFFER_ANCHORS = re.compile(r"(?:eta|arrival)\s*[:\-]?", re.IGNORECASE)
_ORIGIN_CUTOFF_ANCHORS = re.compile(r"(?:origin\s+cutoff|erd|pickup\s+cutoff|door\s+cutoff)\s*[:\-]?", re.IGNORECASE)

def parse_eta_requested(text):  return _find_date_near(text or "", _ETA_REQ_ANCHORS)
def parse_etd_offered(text):    return _find_date_near(text or "", _ETD_OFFER_ANCHORS)
def parse_eta_offered(text):    return _find_date_near(text or "", _ETA_OFFER_ANCHORS)
def parse_origin_cutoff(text):  return _find_date_near(text or "", _ORIGIN_CUTOFF_ANCHORS)


# ─────────────────────────────────────────────────────────────────────
# Parser-gap fixes (Michael 2026-05-19 — "no field should be empty ever")
# ─────────────────────────────────────────────────────────────────────

# parse_etd_requested — Lonny's departure-side ask. In shipping vernacular
# "cutoff" usually means ETD (you need to load/ship by X), so we accept both
# the explicit "departure" anchors AND the "cutoff" patterns. The eta_requested
# parser stays narrowly anchored on "ETA X" / "arrive by X" so the two fields
# split cleanly when Lonny actually writes both.
_ETD_REQ_ANCHORS = re.compile(
    r"(?:need(?:s|ed)?\s+(?:to\s+)?(?:sail|ship|load|depart|leave)"
    r"|target\s+etd|requested\s+etd|require(?:d)?\s+(?:etd|to\s+depart)"
    r"|sailing\s+by|ship\s+by|load(?:ing)?\s+by|departure\s+by"
    r"|prefer(?:red)?\s+etd|preferred\s+departure"
    r"|etd\s+by|departure\s+date|sail\s+by"
    # 2026-05-19 broadening: Lonny's "cutoff" phrasing usually means ETD.
    # Catches "Cutoff week of 4/27" / "Cut off 5/1" / "cutoff by 5/15".
    r"|cut[-\s]?off"
    r"|week\s+of"
    r"|by\s+EOD)",
    re.IGNORECASE,
)

def parse_etd_requested(text):
    """Lonny's departure-date ask. Captures explicit "ship by X" / "departure
    X" patterns AND the more common "cutoff X" phrasing (which functionally
    means ETD in shipping vernacular). Returns ISO date or None."""
    return _find_date_near(text or "", _ETD_REQ_ANCHORS)


# parse_temperature — reefer rows only. Recognises:
#   "-2C" / "+2°C" / "0F" / "34 F" / "34F" / "set at 34F"
#   "frozen" / "chilled" / "ambient" / "dry"
# Numeric range: -40..+60 (covers all real reefer temps); reject outside to
# avoid false-positives like "234 FCL" or "5 days free".
_TEMP_NUMERIC_RX = re.compile(
    r"(?:^|\s|\b)(?P<sign>[+\-])?\s*(?P<val>\d{1,2})\s*°?\s*(?P<unit>[CF])\b",
)
_TEMP_KEYWORD_RX = re.compile(
    r"\b(frozen|chilled|ambient|dry\s+container|reefer|temp(?:erature)?\s*[:\-]?\s*\w+)\b",
    re.IGNORECASE,
)

def parse_temperature(text):
    """Extract reefer temperature from text. Returns canonical string like
    '-2C' / '34F' / 'Frozen' / 'Chilled'. None when no signal found.

    Numeric matches are bounded to -40..+60 to avoid false positives
    (e.g. '234 FCL' would otherwise read as '34F')."""
    if not text:
        return None
    # Numeric match first (more specific). Walk all matches and pick the
    # first one that lands in a plausible reefer-temperature range.
    for m in _TEMP_NUMERIC_RX.finditer(text):
        try:
            sign = m.group("sign") or ""
            val = int(m.group("val"))
            unit = m.group("unit").upper()
        except (ValueError, TypeError):
            continue
        signed = -val if sign == "-" else val
        # C: -40..+30, F: -40..+120. Outside ranges = false positive.
        if unit == "C" and not (-40 <= signed <= 30):
            continue
        if unit == "F" and not (-40 <= signed <= 120):
            continue
        # Reject if preceded by a digit (e.g. "234F" → not 34F)
        lead = text[max(0, m.start()-1):m.start()]
        if lead and lead[-1].isdigit():
            continue
        # Reject if followed by certain words ("Free", "Combined", "FCL")
        tail = text[m.end():m.end()+10]
        if re.match(r"\s*(free|combined|fcl|fcls|consolidated)\b", tail, re.IGNORECASE):
            continue
        return f"{sign if sign == '-' else ''}{val}{unit}"
    # Keyword match — only return canonical reefer-condition words
    m = _TEMP_KEYWORD_RX.search(text)
    if m:
        kw = m.group(1).strip().lower()
        if kw in ("frozen", "chilled", "ambient"):
            return kw.capitalize()
        if kw.startswith("dry"):
            return "Dry"
    return None


# parse_product — commodity description. Recognises "Product Lactose" /
# "product: cheese" / "Product is Skim Milk Powder" patterns + bare commodity
# words from a known-Hilmar dictionary.
_PRODUCT_LABELED_RX = re.compile(
    r"\bproduct\s*(?:is\s+|:\s*|\s+-\s*|-\s*|\s+)([A-Za-z][A-Za-z0-9 &\-/]{2,40})",
    re.IGNORECASE,
)
_PRODUCT_COMMODITY_DICT = (
    # Order matters — longer multi-word names first so "Skim Milk Powder"
    # wins before bare "Milk". Lowercase for the regex; canonicalize on output.
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
    ("amf", "AMF"),
    ("wpc 80", "WPC 80"),
    ("wpc", "WPC"),
    ("wpi", "WPI"),
    ("mpc", "MPC"),
    ("mpi", "MPI"),
    ("protein", "Protein"),
)
_TRAILING_TRIM_RX = re.compile(r"[,.\n;:!?]")

def parse_product(text):
    """Extract commodity from RFQ / booking body text.

    Returns canonical product string ('Lactose', 'Skim Milk Powder', etc.)
    or None when no recognisable commodity is mentioned.
    """
    if not text:
        return None
    # Labeled pattern first (highest confidence)
    m = _PRODUCT_LABELED_RX.search(text)
    if m:
        raw = m.group(1).strip()
        # Trim at first punctuation
        cut = _TRAILING_TRIM_RX.search(raw)
        if cut:
            raw = raw[:cut.start()]
        raw = raw.strip()
        # Try to canonicalize via the dictionary
        low = raw.lower()
        for needle, canonical in _PRODUCT_COMMODITY_DICT:
            if needle in low:
                return canonical
        # Otherwise return whatever Lonny wrote (capitalized) if it looks
        # like a real noun phrase (2-30 chars, no digits-only).
        if 2 <= len(raw) <= 30 and not raw.isdigit():
            return raw.title()
    # Dictionary-only fallback (no "Product" label but commodity word present)
    low = text.lower()
    for needle, canonical in _PRODUCT_COMMODITY_DICT:
        # Word-boundary match so "lactose" doesn't pick up inside other tokens
        if re.search(rf"\b{re.escape(needle)}\b", low):
            return canonical
    return None


# parse_requested_dates — Lonny's free-text date / sailing-window ask. This
# is NOT an ISO date (use parse_eta_requested / parse_etd_requested for that).
# It captures the raw phrase ("Cutoff next week or the following") so the
# operator sees Lonny's actual ask in the daily audit.
_REQ_DATES_ANCHORS = (
    "cutoff", "cut-off", "cut off",
    "need ", "needs ", "needed ",
    "require", "required ", "requires ",
    "sailing", "sail by", "ship by", "load by", "loading by",
    "target etd", "requested etd", "preferred etd", "prefer etd",
    "week of", "departure", "by eod", "by end of",
    "asap",
)
_REQ_DATES_RX = re.compile(
    r"(?P<anchor>" + "|".join(re.escape(a) for a in _REQ_DATES_ANCHORS) + r")"
    r"\s*(?P<tail>[A-Za-z0-9][^.\n;!?]{2,60})",
    re.IGNORECASE,
)
_REQ_DATES_REJECT_TAIL = re.compile(
    r"^\s*(?:date|cargo|day|hour|time)\s+(?:is|are)\s+",
    re.IGNORECASE,
)

def parse_requested_dates(text):
    """Extract Lonny's free-text date ask (departure window, cutoff phrase).

    Returns the matched phrase (anchor + short context) or None.
    """
    if not text:
        return None
    m = _REQ_DATES_RX.search(text)
    if not m:
        return None
    anchor = m.group("anchor")
    tail = m.group("tail").strip()
    if _REQ_DATES_REJECT_TAIL.match(tail):
        return None
    # Reconstruct the full phrase, cap at 80 chars
    phrase = f"{anchor} {tail}".strip()
    phrase = re.sub(r"\s+", " ", phrase)
    return phrase[:80] if 3 <= len(phrase) <= 200 else None


# parse_lonny_notes — free-form Lonny-side body text. The simplest correct
# answer: take Lonny's body, strip the email signature + Outlook quote chain,
# return what's left. Capped at 300 chars so the audit display stays sane.
_SIGNATURE_TRIM_RX = re.compile(
    r"(?im)^\s*(?:thanks?|regards|best(?:\s+regards)?|cheers|sincerely)[,.\s]*$",
)
_OUTLOOK_QUOTE_RX = re.compile(
    r"(?im)^(?:from:|sent:|on .+ wrote:).*$",
)
_LONNY_NAME_RX = re.compile(
    r"(?im)^(?:lonny\s+upfold|logistics\s+coordinator|hilmar\s+ingredients).*$",
)

def parse_lonny_notes(text):
    """Extract Lonny-side notes from an RFQ body. Strips signature, Outlook
    quote chains, and common boilerplate. Returns the trimmed body up to
    300 chars, or None when no meaningful text remains."""
    if not text:
        return None
    t = text
    # Strip signature block onwards
    sig_match = _SIGNATURE_TRIM_RX.search(t)
    if sig_match:
        t = t[:sig_match.start()]
    # Strip Outlook reply-chain headers (everything from "From: ..." down)
    quote_match = _OUTLOOK_QUOTE_RX.search(t)
    if quote_match:
        t = t[:quote_match.start()]
    # Strip Lonny's title lines if present
    t = _LONNY_NAME_RX.sub("", t)
    # Collapse whitespace
    t = re.sub(r"\s+", " ", t).strip()
    # Reject if too short to be a useful note
    if len(t) < 8:
        return None
    return t[:300]


# parse_rate_expiry regex bank — used by parse_rate_expiry() above. Recognises
# the common "valid through" / "expires" prose patterns in OL rate-response
# bodies. Each pattern captures the date-or-window phrase as group(1).
_RATE_EXPIRY_RXES = [
    re.compile(
        r"(?:rate\s+)?valid\s+(?:thru|through|until|to)\s+"
        r"([A-Za-z0-9 \-/,]{3,30})",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:rate\s+)?expir(?:ation|ing|es|y|e)\s*(?:on|at|:|-)?\s*"
        r"([A-Za-z0-9 \-/,]{3,30})",
        re.IGNORECASE,
    ),
    re.compile(
        r"good\s+(?:thru|through|until)\s+([A-Za-z0-9 \-/,]{3,30})",
        re.IGNORECASE,
    ),
    re.compile(
        r"validity\s*[:\-]?\s*([A-Za-z0-9 \-/,]{3,30})",
        re.IGNORECASE,
    ),
]


# ---------- Vessel / transshipment ----------

_VESSEL_RX = re.compile(
    r"(?:vessel|m/?v)\s*[:\-]?\s*(?P<name>[A-Z][A-Z0-9 \-\.]{3,40}?)"
    r"(?:\s*[/,]\s*(?P<voy>V?\.?\s*\d{1,4}[A-Z]?))?",
    re.IGNORECASE,
)

_CARRIER_EXCLUDE = {"MSC", "CMA", "ONE", "HMM", "OOCL", "ZIM"}

# OL rate-table parser. After html_to_text inserts " | " between <td>/<th>
# cells, the body looks like:
#   POL | POD | Container Size | Vessel | Voyage | ERD | ...
#   Oakland | Busan | 5x40'RF | HMM RUBY | 0012W | 17-Apr-26 | ...
# So we locate the header row (case-insensitive scan for "Vessel" and "Voyage"
# headers) and pull the aligned cells from the next row.
_TABLE_HEADER_HINTS = ("vessel", "voyage", "etd", "eta", "rate")


def _collapse_multiline_pipe_table(text: str) -> str:
    """Convert multi-line pipe-table format (each cell on its own line with
    leading '|') into the single-line format _find_table_rows expects.

    NEW OL TEMPLATE (caught 2026-05-13 — Michael "status change of pending to
    quoted with no carrier and no rate"):
        POL
         | POD
         | Container Size
         ...
         | DESTINATION FREE TIME
         |
        Oakland
         | Yokohama
         | 2x40'RF
         ...
         | $3500
         | CMA

    OLD OL TEMPLATE (still works):
        POL | POD | Container Size | ... | RATE | CARRIER | ...
        Oakland | Yokohama | 2x40'RF | ... | $3500 | CMA | ...

    Both templates need to parse. This collapser detects the multi-line
    shape (a non-pipe-leading word/phrase followed by 5+ pipe-leading lines)
    and joins them with ' | ' separators. Old template is unchanged.
    """
    if not text:
        return text
    lines = text.split("\n")
    out_lines = []
    i = 0
    while i < len(lines):
        line = lines[i].rstrip()
        stripped = line.strip()
        # Multi-line table opener: a non-empty non-pipe line followed by
        # several pipe-leading lines. Threshold: 4+ continuation lines to
        # avoid false positives on regular prose.
        if (stripped and not stripped.startswith("|") and i + 1 < len(lines)
                and lines[i + 1].strip().startswith("|")):
            row = [stripped]
            j = i + 1
            while j < len(lines):
                nxt = lines[j].strip()
                if nxt.startswith("|"):
                    # Strip leading '|' (and optional ' ') then capture cell
                    cell = nxt[1:].strip()
                    row.append(cell)
                    j += 1
                else:
                    break
            if len(row) >= 5:  # Real multi-line row, not a fluke
                # Drop trailing blank cells (from trailing "| " lines)
                while row and not row[-1]:
                    row.pop()
                out_lines.append(" | ".join(row))
                i = j
                continue
        out_lines.append(line)
        i += 1
    return "\n".join(out_lines)


def _find_table_rows(text: str):
    """Extract pipe-delimited rate-table rows from OL response bodies.
    Returns [header_row, data_row] or None.
    Handles both single-line and multi-line pipe-table formats — multi-line
    bodies are pre-collapsed before scanning.
    """
    if not text:
        return None
    text = _collapse_multiline_pipe_table(text)
    rows = []
    header_idx = None
    for line in text.split("\n"):
        if " | " not in line:
            continue
        cells = [c.strip() for c in line.split("|")]
        while cells and not cells[-1]:
            cells.pop()
        if len(cells) < 4:
            continue
        low = line.lower()
        hint_count = sum(1 for h in _TABLE_HEADER_HINTS if h in low)
        if header_idx is None and hint_count >= 2:
            header_idx = len(rows)
            rows.append(cells)
        elif header_idx is not None:
            rows.append(cells)
            break
    return rows if rows and header_idx is not None else None


def _norm_header(h: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", h.lower()).strip("_")


def parse_rate_table(text: str) -> dict:
    """Extract carrier_quoted / ol_rate / etd_offered / eta_offered /
    vessel_voyage / transshipment / pol / pod from an OL pipe-table reply."""
    rows = _find_table_rows(text or "")
    if not rows or len(rows) < 2:
        return {}
    header = [_norm_header(c) for c in rows[0]]
    data = rows[1]
    if len(data) < len(header):
        data = data + [""] * (len(header) - len(data))
    elif len(data) > len(header):
        data = data[:len(header)]
    cells = dict(zip(header, [d.strip() for d in data]))
    out = {}
    car = cells.get("carrier") or ""
    if car:
        car = re.sub(r"[\*\(].*$", "", car).strip()
        if car:
            try:
                import core as _core
                norm = _core.normalize_carrier(car)
                out["carrier_quoted"] = norm or car
            except Exception:
                out["carrier_quoted"] = car
    rate_raw = cells.get("rate") or ""
    if rate_raw:
        m = re.search(r"\$?\s*([\d,]+(?:\.\d+)?)", rate_raw.replace(",", ""))
        if m:
            try:
                out["ol_rate"] = float(m.group(1))
            except ValueError:
                pass
    vessel = cells.get("vessel") or ""
    voy = cells.get("voyage") or ""
    if vessel or voy:
        out["vessel_voyage"] = (vessel + (" " + voy if voy else "")).strip()
    for k_in, k_out in (
        ("etd", "etd_offered"),
        ("eta", "eta_offered"),
        # ERD column → both `erd` (schema field) and `origin_cutoff` (legacy
        # alias used by ingest/patch_carriers). Same value, two names.
        # Per docs/PARSER-GAPS.md 2026-05-19: `erd` was 155/155 empty because
        # parse_rate_table only emitted origin_cutoff. Fixed by surfacing both.
        ("erd", "erd"),
        ("erd", "origin_cutoff"),
        ("doc_cut", "doc_cutoff"),
        ("port_cut", "port_cutoff"),
    ):
        if cells.get(k_in):
            out[k_out] = cells[k_in]
    for k in ("transshipment", "container_size", "pol", "pod", "dthc"):
        v = cells.get(k)
        if v:
            out[k] = v
    # 2026-05-19 parser-gap fix: surface free-time + rate-expiry from the
    # table cells. Header normalization via _norm_header() lowercases +
    # replaces non-alnum with underscores, so:
    #   "ORIGIN FREE TIME"      -> "origin_free_time"
    #   "DESTINATION FREE TIME" -> "destination_free_time"  (alias to dest_free_time)
    if cells.get("origin_free_time"):
        out["origin_free_time"] = cells["origin_free_time"]
    if cells.get("destination_free_time"):
        out["dest_free_time"] = cells["destination_free_time"]
    elif cells.get("dest_free_time"):
        out["dest_free_time"] = cells["dest_free_time"]
    # rate_expiry — typically NOT in the table itself but in the body
    # prose (e.g. "valid through 5/31", "rate expires 6/15"). Parsed
    # separately by parse_rate_expiry which is called from fetch_bodies.
    # Leave the table parser pure; expiry is composed at the call site.
    return out


def parse_rate_expiry(text):
    """Extract validity-window string from an OL rate response body.

    Recognised shapes:
      "valid through 5/31"
      "valid until 6/15"
      "rate expires 6/15/26"
      "expiry: 31-May-26"
      "good through May 31"

    Returns the matched expiry phrase (raw, trimmed) or None.
    """
    if not text:
        return None
    for rx in _RATE_EXPIRY_RXES:
        m = rx.search(text)
        if m:
            raw = m.group(1).strip(' ,.;:-')
            if 3 <= len(raw) <= 40:
                return raw
    return None
