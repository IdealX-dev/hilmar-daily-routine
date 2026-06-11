#!/usr/bin/env python3
"""
body_parser.py -- Regex-based parsers for email bodies + MDOLX subjects.

Pure-function module. No IO. All parsers return None when unsure (never guess).
Ingest merges these values ON TOP OF the existing preview-based extractions.

Parsers:
  parse_subject_lane(subject)       -> (origin, destination) for MDOLX subjects
  parse_eta_requested(text)         -> ISO date  (Lonny's target cutoff/ETD)
  parse_eta_offered(text)           -> ISO date  (OL's quoted ETA)
  parse_etd_offered(text)           -> ISO date  (OL's quoted ETD/sailing)
  parse_vessel(text)                -> "Vessel Name / V.123N"
  parse_transshipment(text)         -> "SIN" | "Direct" | None
  parse_rate_table(text)            -> dict (carrier/ol_rate/etd/vessel/rate_expiry/...)
  parse_send_signal(text)           -> bool   (Lonny says "send"/"book")
  parse_origin_cutoff(text)         -> ISO date
  html_to_text(html)                -> plaintext
"""
from __future__ import annotations

import re
from datetime import date, datetime
from html.parser import HTMLParser


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
    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)

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

# Ocean ports-of-loading that legitimately appear as origin in subject lanes.
# "HILMAR" was previously in this list but is the customer-name reference
# in booking subjects ("MDOLX...// HILMAR 3x40'RF Oakland to Tokyo"), not
# a port. Including it caused _scan_for_origin to greedily pick "Hilmar"
# over the actual port "Oakland" (since it appears first in the string),
# producing lane labels like "Hilmar → Tokyo" that split the lane bucket
# in the carrier scoreboard. The actual ports-of-loading remain.
_KNOWN_ORIGINS = [
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

#: Public alias — the single source of truth for Hilmar origin sites.
KNOWN_ORIGINS = tuple(_KNOWN_ORIGINS)

#: Rate-response subject: "Re: <known origin> to <destination>". Built from
#: the origins list above — NEVER hardcode a city here. Until 2026-06-11 this
#: pattern (in refresh_stage + ingest) was literally "re: oakland to", so
#: every Dalhart-lane quote from the MBD shared mailbox was filed as generic
#: inbound and the requests showed as Not Quoted in the client email (live
#: failure: 4 quoted Dalhart RFQs reported NQ on 2026-06-11).
_ORIGIN_ALT = "|".join(re.escape(o) for o in _KNOWN_ORIGINS)
RATE_RESPONSE_SUBJECT_RX = re.compile(
    rf"^\s*re\s*:\s*(?:{_ORIGIN_ALT})(?:,?\s*[A-Z]{{2}})?\s+to\s+", re.IGNORECASE)

_LANE_RX_B = re.compile(
    r"(?P<origin>HILMAR(?:,?\s*CA)?)\s*[-\u2013]+>\s*(?P<dest>[A-Z][A-Za-z\.\s,]+?)(?:\s*//|\s*\d|$)",
    re.IGNORECASE,
)

_CONTAINER_MARK_RX = re.compile(
    r"\b\d+\s*[xX\u00d7]\s*\d{2}'?\s*(HC|RF|DV|GP|FR|OT|Reefer|Door|Chill|Flex)?\b",
    re.IGNORECASE,
)


def parse_container_spec(text: str | None) -> str | None:
    """Extract a container spec ("4X40'RF", "1x20'DV") from arbitrary
    text \u2014 works on subjects, previews, or full email bodies. Returns
    the matched substring (consumable by :func:`core.parse_teu`) or
    None when no spec is present.

    Booking-confirmation subjects (most reliable shape, e.g.
    "MDOLX260420_UPDATED ETA BOOKING CONFIRMATION// HILMAR 1X20'DV
    Oakland to Bangkok// ONE: \u2026") are the primary use case; rate-
    response email bodies often mention the spec in prose ("for your
    1x40HC shipment to Bangkok\u2026"). Lonny outbound subjects are usually
    just "Oakland to <dest>" with no spec \u2014 which is why this function
    is also called against the matched rate-response body to recover
    containers when the subject was empty.
    """
    if not text:
        return None
    m = _CONTAINER_MARK_RX.search(text)
    return m.group(0).strip() if m else None


def parse_container_spec_from_subject(subject: str | None) -> str | None:
    """Backwards-compatible alias of :func:`parse_container_spec` \u2014
    kept because callers pass ``subject`` semantically and the name
    documents intent."""
    return parse_container_spec(subject)

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
# before 'CMA'. Values map raw → canonical (the canonical form is what
# core.normalize_carrier already accepts).
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

# Booking-ref prefixes that indicate the carrier (e.g. NAM... = CMA CGM,
# EBKG... = MSC). Used as a tertiary fallback when the carrier word itself
# isn't on the subject line.
_BOOKING_PREFIX_TO_CARRIER = {
    "NAM":  "CMA CGM",
    "EBKG": "MSC",
    "MEDU": "MSC",
    "RICG": "ONE",
    "SCNB": "ONE",
    "MAEU": "Maersk",
}


def parse_subject_carrier(subject: str | None) -> str | None:
    """Extract the winning carrier from an MDOLX confirmation subject.

    Real subjects look like::

      MDOLX260453_UPDATED BOOKING CONFIRMATION// BTG 1X40'HC ... // MSC: EBKG16491184
      MDOLX260114 / 2x40'RF CMA: NAM8322223
      MDOLX260473 ... // CMA BKG # NAM8451437
      MDOLX260407 ... // EVERGREEN

    Returns the canonical carrier name (matching ``core.normalize_carrier``
    output) or ``None`` if no signal found.

    This is the subject-line counterpart of :func:`_find_carrier` (which
    walks bare prose); it understands the structured booking-ref trailer
    formats that MDOLX subjects use. Hooked at ingest time on standalone
    wins and matched-booking carrier_won so the dashboard reflects the
    carrier on the first pass — no QC last-resort pass needed.
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
    # Pattern C: any carrier token anywhere on the subject (only run for
    # MDOLX subjects to avoid matching "CMA UPDATES" type chatter on
    # non-booking emails).
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
    s = re.sub(
        r"MDOLX\s*\d+_?\s*\*?(NEW|REVISED|UPDATED)?\s*(BOOKING|TRANSPORT)?\s*(CONFIRMATION|ORDER|SCHEDULE)?\s*//?\s*",
        " ", s, flags=re.IGNORECASE)
    s = _CONTAINER_MARK_RX.sub(" ", s)
    s = re.sub(r"\s*\(\s*\d+\s*\)\s*$", "", s)

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

def _find_date_near(text, anchor_rx, window=120):
    if not text:
        return None
    now_year = datetime.utcnow().year
    for am in anchor_rx.finditer(text):
        start = am.end()
        chunk = text[start:start+window]
        for drx in _DATE_RXES:
            dm = drx.search(chunk)
            if dm:
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
# Mirror of scripts/body_parser.py additions; tests run against this file.
# ─────────────────────────────────────────────────────────────────────

_ETD_REQ_ANCHORS = re.compile(
    r"(?:need(?:s|ed)?\s+(?:to\s+)?(?:sail|ship|load|depart|leave)"
    r"|target\s+etd|requested\s+etd|require(?:d)?\s+(?:etd|to\s+depart)"
    r"|sailing\s+by|ship\s+by|load(?:ing)?\s+by|departure\s+by"
    r"|prefer(?:red)?\s+etd|preferred\s+departure"
    r"|etd\s+by|departure\s+date|sail\s+by"
    r"|cut[-\s]?off"
    r"|week\s+of"
    r"|by\s+EOD)",
    re.IGNORECASE,
)

def parse_etd_requested(text):
    """Lonny's departure-date ask. See scripts/body_parser.py for full docstring."""
    return _find_date_near(text or "", _ETD_REQ_ANCHORS)


_TEMP_NUMERIC_RX = re.compile(
    r"(?:^|\s|\b)(?P<sign>[+\-])?\s*(?P<val>\d{1,2})\s*°?\s*(?P<unit>[CF])\b",
)
_TEMP_KEYWORD_RX = re.compile(
    r"\b(frozen|chilled|ambient|dry\s+container|reefer|temp(?:erature)?\s*[:\-]?\s*\w+)\b",
    re.IGNORECASE,
)

def parse_temperature(text):
    """Reefer temperature from text. See scripts/body_parser.py for full docs."""
    if not text:
        return None
    for m in _TEMP_NUMERIC_RX.finditer(text):
        try:
            sign = m.group("sign") or ""
            val = int(m.group("val"))
            unit = m.group("unit").upper()
        except (ValueError, TypeError):
            continue
        signed = -val if sign == "-" else val
        if unit == "C" and not (-40 <= signed <= 30):
            continue
        if unit == "F" and not (-40 <= signed <= 120):
            continue
        lead = text[max(0, m.start()-1):m.start()]
        if lead and lead[-1].isdigit():
            continue
        tail = text[m.end():m.end()+10]
        if re.match(r"\s*(free|combined|fcl|fcls|consolidated)\b", tail, re.IGNORECASE):
            continue
        return f"{sign if sign == '-' else ''}{val}{unit}"
    m = _TEMP_KEYWORD_RX.search(text)
    if m:
        kw = m.group(1).strip().lower()
        if kw in ("frozen", "chilled", "ambient"):
            return kw.capitalize()
        if kw.startswith("dry"):
            return "Dry"
    return None


_PRODUCT_LABELED_RX = re.compile(
    r"\bproduct\s*(?:is\s+|:\s*|\s+-\s*|-\s*|\s+)([A-Za-z][A-Za-z0-9 &\-/]{2,40})",
    re.IGNORECASE,
)
_PRODUCT_COMMODITY_DICT = (
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
    """Commodity from text. See scripts/body_parser.py for full docs."""
    if not text:
        return None
    m = _PRODUCT_LABELED_RX.search(text)
    if m:
        raw = m.group(1).strip()
        cut = _TRAILING_TRIM_RX.search(raw)
        if cut:
            raw = raw[:cut.start()]
        raw = raw.strip()
        low = raw.lower()
        for needle, canonical in _PRODUCT_COMMODITY_DICT:
            if needle in low:
                return canonical
        if 2 <= len(raw) <= 30 and not raw.isdigit():
            return raw.title()
    low = text.lower()
    for needle, canonical in _PRODUCT_COMMODITY_DICT:
        if re.search(rf"\b{re.escape(needle)}\b", low):
            return canonical
    return None


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
    """Lonny's free-text date ask. See scripts/body_parser.py for full docs."""
    if not text:
        return None
    m = _REQ_DATES_RX.search(text)
    if not m:
        return None
    anchor = m.group("anchor")
    tail = m.group("tail").strip()
    if _REQ_DATES_REJECT_TAIL.match(tail):
        return None
    phrase = f"{anchor} {tail}".strip()
    phrase = re.sub(r"\s+", " ", phrase)
    return phrase[:80] if 3 <= len(phrase) <= 200 else None


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
    """Free-form Lonny-side notes. See scripts/body_parser.py for full docs."""
    if not text:
        return None
    t = text
    sig_match = _SIGNATURE_TRIM_RX.search(t)
    if sig_match:
        t = t[:sig_match.start()]
    quote_match = _OUTLOOK_QUOTE_RX.search(t)
    if quote_match:
        t = t[:quote_match.start()]
    t = _LONNY_NAME_RX.sub("", t)
    t = re.sub(r"\s+", " ", t).strip()
    if len(t) < 8:
        return None
    return t[:300]


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

def parse_rate_expiry(text):
    """Rate-validity phrase from text. See scripts/body_parser.py for full docs."""
    if not text:
        return None
    for rx in _RATE_EXPIRY_RXES:
        m = rx.search(text)
        if m:
            raw = m.group(1).strip(' ,.;:-')
            if 3 <= len(raw) <= 40:
                return raw
    return None


# ---------- Vessel / transshipment ----------

_VESSEL_RX = re.compile(
    r"(?:vessel|m/?v)\s*[:\-]?\s*(?P<name>[A-Z][A-Z0-9 \-\.]{3,40}?)"
    r"(?:\s*[/,]\s*(?P<voy>V?\.?\s*\d{1,4}[A-Z]?))?",
    re.IGNORECASE,
)

_CARRIER_EXCLUDE = {"MSC", "CMA", "ONE", "HMM", "OOCL", "ZIM"}

def parse_vessel(text):
    if not text:
        return None
    m = _VESSEL_RX.search(text)
    if not m:
        return None
    name = (m.group("name") or "").strip().strip(".,")
    voy = (m.group("voy") or "").strip().strip(".,")
    if len(name) < 3 or name.lower() in ("name", "number", "info"):
        return None
    # Reject carrier-name-as-vessel (e.g. "Vessel: MSC" without a specific ship name)
    if name.upper() in _CARRIER_EXCLUDE and not voy:
        return None
    return f"{name} / {voy}".strip(" /") if voy else name


_TS_DIRECT_RX = re.compile(r"\b(direct)\b(?!\s*(?:call|deposit))", re.IGNORECASE)
_TS_VIA_RX = re.compile(r"\b(?:via|t/?s|transshipment)\s*[:\-]?\s*([A-Z][A-Za-z \-]{2,30})", re.IGNORECASE)

def parse_transshipment(text):
    if not text:
        return None
    m = _TS_VIA_RX.search(text)
    if m:
        return m.group(1).strip().title()
    if _TS_DIRECT_RX.search(text):
        return "Direct"
    return None


# ---------- Rate table ----------

_CARRIER_TOKENS = ["MSC", "CMA CGM", "CMA", "EVERGREEN", "ONE", "MAERSK",
                   "HMM", "COSCO", "OOCL", "WAN HAI", "YANG MING", "ZIM",
                   "HAPAG", "HAPAG-LLOYD"]

def _find_carrier(text):
    up = text.upper()
    for tok in _CARRIER_TOKENS:
        if tok in up:
            return tok.title() if tok not in ("MSC", "ONE", "HMM", "OOCL", "ZIM") else tok
    return None

# ---------- MBD column-layout table parser ----------
# MBD's standard rate-response format is a two-block column table — one
# label per line, then a blank, then values in the same positional order:
#
#     POL                            Oakland
#     POD                            HCMC
#     Container Size                 2 X 20'DV
#     Vessel                         WAN HAI A01
#     Voyage         (blank)         W017
#     ERD            ─────►          21-Apr-26
#     Doc Cut                        24-Apr-26
#     ...                            ...
#     RATE                           $450.00
#     CARRIER                        ONE LINE
#     TRANSSHIPMENT                  DIRECT VIA CAI MEP
#
# The prose-pattern regexes in parse_etd_offered / parse_eta_offered /
# parse_rate_table all miss this layout because there's no anchor word
# adjacent to the value. Today's parser_misses.jsonl shows 27 LLM
# extractions on this exact format. parse_mbd_rate_columns recognises
# the labels block, reads N values from the following block, and maps
# them positionally — should drop the LLM-fallback rate from majority
# to near-zero on the dominant rate-response format.

_TABLE_LABELS = {
    # Label string (case-insensitive) → canonical key
    "POL": "pol", "POD": "pod", "CONTAINER SIZE": "container_size",
    "VESSEL": "vessel", "VOYAGE": "voyage",
    "ERD": "erd", "DOC CUT": "doc_cut", "PORT CUT": "port_cut",
    "RAIL CUT": "rail_cut", "ETD": "etd", "ETA": "eta",
    "RATE": "rate", "DTHC": "dthc", "CARRIER": "carrier",
    "TRANSSHIPMENT": "transshipment",
    "ORIGIN FREE TIME": "origin_free_time",
    "DESTINATION FREE TIME": "destination_free_time",
}

_TABLE_DATE_RX = re.compile(
    r"^(\d{1,2})[-\s/]"
    r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*"
    r"[-\s/](\d{2,4})$",
    re.IGNORECASE,
)


def _parse_table_date(s):
    """Parse 'DD-Mon-YY' / 'DD-Mon-YYYY' / ISO 'YYYY-MM-DD' → ISO date."""
    if not s:
        return None
    s = s.strip()
    if re.match(r"^\d{4}-\d{2}-\d{2}$", s):
        return s
    m = _TABLE_DATE_RX.match(s)
    if not m:
        return None
    d, mon, y = m.groups()
    mon_num = _MONTHS.get(mon.lower()[:3])
    if not mon_num:
        return None
    yi = int(y)
    if yi < 100:
        yi += 2000
    try:
        return date(yi, mon_num, int(d)).isoformat()
    except (ValueError, TypeError):
        return None


def parse_mbd_rate_columns(text):
    """Extract the column-layout MBD rate table (see docstring above).

    Returns dict with any extractable fields, or None if the format
    doesn't match. Designed to be called BEFORE the prose-pattern
    parsers — anything it finds is high-confidence (positional match).
    """
    if not text:
        return None
    lines = [ln.strip() for ln in text.splitlines()]

    # Find a contiguous run of recognised labels. Allow a small number
    # of stray lines between (some MBD emails interleave decoration).
    labels: list[tuple[int, str]] = []  # (line_idx, canonical_key)
    last_label_idx = None
    for i, line in enumerate(lines):
        norm = line.upper().strip().rstrip(":").strip()
        if norm in _TABLE_LABELS:
            labels.append((i, _TABLE_LABELS[norm]))
            last_label_idx = i
        elif labels and last_label_idx is not None and i - last_label_idx > 3:
            # Gap too big — labels block ended.
            break

    # Need at least 5 of these labels to be confident — random emails
    # might have one or two of "ETA", "Rate" inline.
    if len(labels) < 5:
        return None
    label_end = labels[-1][0]

    # Skip blanks until first value line, then read len(labels) values.
    i = label_end + 1
    while i < len(lines) and not lines[i].strip():
        i += 1
    values: list[str] = []
    while i < len(lines) and len(values) < len(labels):
        v = lines[i].strip()
        if v:
            values.append(v)
        i += 1
    if len(values) < len(labels):
        return None  # truncated email or wrong block detected

    # Map label-keys to values by position.
    raw: dict[str, str] = {}
    for (_, key), val in zip(labels, values, strict=False):
        raw[key] = val

    out: dict = {}
    if "rate" in raw:
        rs = raw["rate"].replace("$", "").replace(",", "").strip()
        m = re.match(r"^(\d+(?:\.\d+)?)", rs)
        if m:
            try:
                rate_val = float(m.group(1))
                # Sanity range — MBD rates are $200–$50k. Anything outside
                # is probably a misaligned column (e.g. ETA picked up here).
                if 200 <= rate_val <= 50000:
                    out["ol_rate"] = rate_val
            except ValueError:
                pass
    if "carrier" in raw:
        out["carrier_quoted"] = raw["carrier"]
    if "etd" in raw:
        d = _parse_table_date(raw["etd"])
        if d:
            out["etd"] = d
    if "eta" in raw:
        d = _parse_table_date(raw["eta"])
        if d:
            out["eta"] = d
    if "vessel" in raw and "voyage" in raw:
        out["vessel_voyage"] = f"{raw['vessel']} / {raw['voyage']}".strip()
    elif "vessel" in raw:
        out["vessel_voyage"] = raw["vessel"]
    if "transshipment" in raw:
        out["transshipment"] = raw["transshipment"]
    # 2026-05-19 parser-gap fix: surface ERD + free-time labels that were
    # mapped in _TABLE_LABELS but never bubbled to the output dict.
    if "erd" in raw:
        # Pass through both the canonical `erd` schema key AND the legacy
        # `origin_cutoff` alias used by the scripts/ rate-table parser.
        erd_d = _parse_table_date(raw["erd"])
        out["erd"] = erd_d or raw["erd"]
        out["origin_cutoff"] = out["erd"]
    if "origin_free_time" in raw:
        out["origin_free_time"] = raw["origin_free_time"]
    if "destination_free_time" in raw:
        out["dest_free_time"] = raw["destination_free_time"]
    return out or None


def parse_rate_table(text):
    """Extract rate-table fields. Tries the column-layout MBD parser
    first (the dominant format — was producing all the LLM-fallback
    misses pre 2026-04-28 PR #15); falls back to the prose patterns
    that were the historical primary."""
    out = {}
    if not text:
        return out

    # New: column-layout table parser. High-confidence positional match
    # when the format is detected.
    cols = parse_mbd_rate_columns(text)
    if cols:
        out.update(cols)
        # Normalize carrier through core's alias map so "ONE LINE",
        # "WAN HAI", "CMA" all canonicalize.
        if out.get("carrier_quoted"):
            from . import core as _core
            out["carrier_quoted"] = _core.normalize_carrier(out["carrier_quoted"]) or out["carrier_quoted"]

    # Prose fallback — only fill fields the column parser didn't catch.
    if "carrier_quoted" not in out:
        car = _find_carrier(text)
        if car:
            out["carrier_quoted"] = car
    if "ol_rate" not in out:
        m = re.search(r"\$\s*(\d{1,2},\d{3}|\d{3,5})(?:\.\d{2})?", text)
        if m:
            try:
                val = float(m.group(1).replace(",", ""))
                if 500 <= val <= 50000:
                    out["ol_rate"] = val
            except ValueError:
                pass
    if "etd" not in out:
        etd = parse_etd_offered(text)
        if etd:
            out["etd"] = etd
    if "eta" not in out:
        eta = parse_eta_offered(text)
        if eta:
            out["eta"] = eta
    if "vessel_voyage" not in out:
        vessel = parse_vessel(text)
        if vessel:
            out["vessel_voyage"] = vessel
    if "transshipment" not in out:
        ts = parse_transshipment(text)
        if ts:
            out["transshipment"] = ts

    # These are unconditional — always parsed via prose since the
    # column layout doesn't surface them as their own labels.
    exp = re.search(
        r"(?:rate\s+)?(?:expir[ey]s?|valid\s+(?:thru|through|until))\s*[:\-]?\s*([A-Za-z0-9 \-/]{5,20})",
        text, re.IGNORECASE)
    if exp:
        out["rate_expiry"] = exp.group(1).strip()
    dem = re.search(r"(\d+)\s*(?:days?\s+)?(?:free\s+)?demurrage", text, re.IGNORECASE)
    if dem:
        out["demurrage_free"] = int(dem.group(1))
    det = re.search(r"(\d+)\s*(?:days?\s+)?(?:free\s+)?detention", text, re.IGNORECASE)
    if det:
        out["detention_free"] = int(det.group(1))
    return out


# ---------- Send signal ----------

_SEND_RX = re.compile(
    r"\b(?:please\s+)?(?:send|book|go\s+ahead|proceed|confirm\s+booking|accept(?:ed)?|"
    r"let'?s?\s+(?:book|send|go))\b",
    re.IGNORECASE,
)

# ---------- Signer extraction (individual at MBD shared mailbox) ----------
# The MBD shared mailbox sends from a single address but each rate
# response is composed by an individual rate-desk person. ``from_name``
# on the message often carries that individual's display name (Outlook
# send-as), but not always — some clients render it as the mailbox
# name. This parser is the body-text fallback: it walks the email's
# closing block looking for a name on the line(s) right after a
# greeting keyword like "Best", "Thanks", "Regards".

# Words that disqualify a candidate line — these are role labels,
# company tags, or boilerplate, not human names.
_SIGNER_NEGATIVE_TOKENS = (
    "@", "ol-usa", "ol usa", "olusa",
    "phone", "tel:", "tel ", "mobile", "office:", "fax",
    "ocean export", "booking team", "booking shared",
    "mbd ocean", "mbd_ocean", "rate desk",
    "manager", "specialist", "coordinator", "assistant",
    "team", "group", "shared",
    "logistics", "shipping",
    "http://", "https://", "www.",
    "www ", "linkedin",
    "address", "street", "blvd", "ave",
    "confidential", "intended recipient",
    "this email", "please consider",
)

# Customer-side signers that must NEVER appear as the OL responder/quoter.
# These are forwarded-chain leaks — when a rate response email contains
# the original Lonny ask quoted at the bottom, the parser walks past the
# OL signature into Lonny's signature. Any line whose lowercase form
# matches one of these full-name strings is rejected as the signer.
# 2026-05-01 fix — was producing "Quoted by: Lonny Upfold" on dashboard.
_CUSTOMER_SIDE_SIGNERS = frozenset({
    "lonny upfold",
    "lonny",
    "upfold",
    "michael deitchman",   # OL-side internal but not the rate quoter
    "caren tobel",         # also internal, not rate quoter
    "hilmar ingredients",
    "hilmar, ca",
})

# OL-USA known rate-desk staff. When the parser finds one of these names
# (case-insensitive), prefer them over any other candidate. This protects
# against forwarded-chain leaks where Lonny's signature comes BEFORE the
# OL signature in the flattened text. Add new staff here as discovered.
_OL_KNOWN_SIGNERS = (
    "Alexandra Hernandez",
    "Ryan Gordon",
    "Linda Echevarria",
    "Caren Tobel",
    "Michael Deitchman",
)
_OL_KNOWN_SIGNERS_LC = {n.lower() for n in _OL_KNOWN_SIGNERS}

# Greeting keywords that almost always immediately precede a signer.
_SIGNER_CLOSING_RX = re.compile(
    r"^\s*(?:"
    r"Best(?:\s+regards?)?|Regards|Thanks(?:\s+(?:so\s+much|again))?|"
    r"Thank\s+you|Sincerely|Cheers|Kind\s+regards|Warm\s+regards|"
    r"Cordially|Respectfully|Best\s+wishes|Many\s+thanks"
    r")\s*[,.\-—!]?\s*$",
    re.IGNORECASE | re.MULTILINE,
)

# Pattern that looks like a person's name. Conservative: First Last,
# or First M. Last, or "First Last-Hyphen". 1-3 capitalized tokens.
_SIGNER_NAME_RX = re.compile(
    r"^[A-Z][A-Za-z'\-]{1,20}"           # first name
    r"(?:\s+[A-Z]\.?)?"                   # optional middle initial
    r"(?:\s+[A-Z][A-Za-z'\-]{1,20}){1,2}" # 1–2 more name tokens
    r"\s*$",
)


def parse_signer(text: str) -> str | None:
    """Extract the individual signer from an email body's signature.

    Returns the name as a string, or None if no confident match.
    Conservative — when in doubt, return None so the LLM fallback can
    weigh in (per :mod:`hilmar.parser_fallback`).

    Hardening 2026-05-01:
      1. **Allowlist priority** — if any known OL-USA staff name appears
         anywhere in the body, prefer them. Wins against forwarded-chain
         leaks where Lonny's signature is encountered first.
      2. **Customer blocklist** — names in ``_CUSTOMER_SIDE_SIGNERS``
         (Lonny Upfold, etc.) are NEVER accepted as the signer.
    """
    if not text or not isinstance(text, str):
        return None

    # 1. Allowlist scan — find OL-USA staff anywhere in the text. We do
    #    this first because forwarded chains commonly put Lonny's sig
    #    before the OL sig in the flattened body, and the closing-token
    #    walk would otherwise pick him up.
    text_lc = text.lower()
    for name in _OL_KNOWN_SIGNERS:
        if name.lower() in text_lc:
            # Re-grab the canonical form from the original text to
            # preserve casing (handles "ALEXANDRA HERNANDEZ" → "Alexandra
            # Hernandez").
            return name

    # 2. Closing-token walk fallback for unknown OL staff. Customer-side
    #    names get rejected before being returned.
    for closing in _SIGNER_CLOSING_RX.finditer(text):
        tail_start = closing.end()
        tail_lines = text[tail_start:].splitlines()
        seen = 0
        for raw in tail_lines:
            line = raw.strip().rstrip(",.")
            if not line:
                continue
            seen += 1
            if seen > 6:
                break
            low = line.lower()
            if any(tok in low for tok in _SIGNER_NEGATIVE_TOKENS):
                continue
            if _SIGNER_NAME_RX.match(line):
                tokens = line.split()
                clean: list[str] = []
                for t in tokens:
                    if t and t[0].isupper() and not t.isupper() or len(t) <= 3 and t.endswith("."):
                        clean.append(t)
                    else:
                        break
                candidate: str | None = None
                if 2 <= len(clean) <= 4:
                    candidate = " ".join(clean)
                elif len(clean) == 1:
                    candidate = clean[0]
                if candidate and candidate.lower() not in _CUSTOMER_SIDE_SIGNERS:
                    return candidate
    return None


def parse_send_signal(text):
    if not text:
        return False
    lines = [ln for ln in text.splitlines() if not ln.lstrip().startswith(">")]
    stripped = "\n".join(lines[:30])
    return bool(_SEND_RX.search(stripped))


# ---------- Smoke test ----------

if __name__ == "__main__":
    samples = [
        "MDOLX260453_UPDATED BOOKING CONFIRMATION// BTG 1X40'HC Salt Lake City to Paranagua, Brazil// MSC: EBKG16491184",
        "MDOLX260490_ *NEW Booking CONFIRMATION // HOOGWEGT - Chicago to Montevideo - 3x40'",
        "MDOLX260407_ *REVISED BOOKING CONFIRMATION // NUMIDIA - 00+074402 / Hilmar, CA to Tokyo / 2x40HC // EVERGREEN",
        "RE: Oakland to Manila (North)",
        "Oakland to Sydney (7)",
        "Dalhart, TX - Houston Port Dispute// MDOLX260114 / 2x40'RF CMA: NAM8322223",
    ]
    for s in samples:
        print(s[:90], "->", parse_subject_lane(s))
    print()
    body = "Rate confirmed at $4,200/40HC on MSC via Singapore. ETD 30-Apr-2026 ETA 18-May-2026. Vessel: MSC OSCAR / 012E. 14 days free demurrage."
    print("RATE:", parse_rate_table(body))
    print("SEND:", parse_send_signal("Please send, thanks"))
# end of smoke test (removed trailing line to avoid OneDrive sync truncation)ail by 30-Apr"))
