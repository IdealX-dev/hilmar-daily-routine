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

import calendar
import re
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser

# core is this repo's LEAF module (stdlib only, imports nothing local), so a
# hard import here cannot cycle. Hard, not the lazy try/except used for
# _canon_carrier below: a swallowed ImportError would silently stop resolving
# UN/LOCODEs and re-split Yokohama with no alarm anywhere. CI's per-module
# import smoke test proves this wiring on both trees.
import core as _core


# HTML -> text
class _HTMLStripper(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0
        self._in_cell = 0
        self._in_table = 0
    def handle_starttag(self, tag, attrs):
        if tag in ("style", "script"):
            self._skip += 1
        elif tag == "table":
            self._in_table += 1
        elif tag in ("td", "th"):
            self._in_cell += 1
    def handle_endtag(self, tag):
        if tag in ("style", "script"):
            if self._skip:
                self._skip -= 1
            return
        # Table structure must survive html_to_text as "cell | cell | …\n"
        # rows. OL's rate tables render each cell as <td><p><span>value</span>
        # </p></td> AND pretty-print the source with newlines BETWEEN tags, so
        # every cell used to land on its own line (value, blank, " | ") and the
        # table parser saw no row → the whole quote was silently dropped
        # (2026-06-16 Oakland→Yokohama reported Not Quoted though OL quoted
        # $3076/CMA). Inside a table: the only row break is </tr>; cell breaks
        # are " | "; inner block tags and source whitespace become spaces.
        if tag == "table":
            if self._in_table:
                self._in_table -= 1
            self.parts.append("\n")
        elif tag in ("td", "th"):
            if self._in_cell:
                self._in_cell -= 1
            self.parts.append(" | ")
        elif tag == "tr":
            self.parts.append("\n")
        elif tag in ("p", "br", "div", "li"):
            # Inside a table a block break is a SPACE (keep the row intact);
            # outside, it's a newline as before.
            self.parts.append(" " if self._in_table else "\n")
    def handle_data(self, data):
        if self._skip:
            return
        # Inside a table, collapse ALL whitespace (incl. the source's
        # inter-tag newlines) to single spaces so a cell value can't be split
        # across lines; row/cell structure comes from </tr> and </td> above.
        if self._in_table and data:
            data = re.sub(r"\s+", " ", data)
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

# Curated FOREIGN-PORT corpus — every export destination Hilmar ships to.
# This mirrors the foreign ports of core._TRADE_REGION_MAP (the canonical
# destination→trade-region table), Title-Cased. It DELIBERATELY excludes the
# North-America inland keys (Sturgis / Sturgis MI) — those are US-side
# movements, never export destinations. Used only by the last-resort
# destination-recovery branch in parse_subject_lane (QC-057), so a real Lonny
# RFQ whose subject names a port but no "X to Y" lane ("20' reefer request to
# Yokohama") is recovered instead of being silently dropped by
# ingest.build_requests. tests/test_auditfix_qc057_dest_recovery.py guards that
# every entry maps to a non-"Unmapped" trade region, so this corpus can't drift
# away from the map.
_KNOWN_DESTINATIONS = [
    # Far East
    "Shanghai", "Xingang", "Tianjin", "Qingdao", "Ningbo", "Dalian",
    "Huangpu",
    "Yokohama", "Tokyo", "Osaka", "Kobe", "Nagoya", "Busan", "Incheon",
    "Keelung", "Kaohsiung", "Taichung", "Hong Kong",
    # SE Asia
    "HCMC", "Ho Chi Minh", "Cat Lai", "Cai Mep", "Haiphong", "Manila",
    "Singapore", "Port Klang", "Penang", "Laem Chabang", "Bangkok",
    "Jakarta", "Surabaya", "Lat Krabang", "Pasir Gudang",
    # Oceania
    "Sydney", "Melbourne", "Brisbane", "Fremantle", "Auckland",
    # Europe
    "Hamburg", "Rotterdam", "Antwerp", "Felixstowe", "Le Havre",
    "Algeciras", "Valencia", "Genoa", "Barcelona", "Dublin",
    # Middle East
    "Jebel Ali", "Dammam", "Jeddah", "Ashdod", "Haifa",
    # Africa
    "Durban", "Lagos", "Cape Town", "Mombasa", "Alexandria",
    # South America
    "Santos", "Buenos Aires", "Callao", "Valparaiso",
    # Central America
    "Acajutla", "Puerto Barrios", "Puerto Cortes", "Puerto Quetzal",
    "Puerto Limon", "Balboa", "Caucedo",
]

#: Public alias — the single source of truth for recoverable export ports.
KNOWN_DESTINATIONS = tuple(_KNOWN_DESTINATIONS)

# Alternation built longest-first so multi-word ports ("Hong Kong") match
# before any prefix ("Hong"). re.escape so spaces/punctuation are literal.
_DEST_ALT = "|".join(
    re.escape(d) for d in sorted(_KNOWN_DESTINATIONS, key=len, reverse=True))
# Directional "to <known port>" — preferred recovery form.
_DEST_TO_RX = re.compile(rf"\bto\s+(?P<dest>{_DEST_ALT})\b", re.IGNORECASE)
# Bare known-port token anywhere — absolute last resort.
_DEST_BARE_RX = re.compile(rf"\b(?P<dest>{_DEST_ALT})\b", re.IGNORECASE)

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
    # UN/LOCODE BEFORE the Title-Case branch. That branch is what manufactured
    # the fake port "Jpyok" out of the code JPYOK — it Title-Cases any all-caps
    # token longer than three characters, and a LOCODE is exactly five. Only
    # codes listed in core.PORT_LOCODES resolve, so BUSAN/OSAKA/TOKYO/GENOA/
    # HAIFA/LAGOS — five-letter all-caps REAL ports in this corpus — fall
    # through to Title-Case untouched, which is the behaviour they already had.
    _loc = _core.resolve_locode(s)
    if _loc:
        return _loc
    if s.isupper():
        return s.title() if len(s) > 3 else s
    return s

def _scan_for_origin(segment: str):
    """The earliest known origin named in ``segment``, or None.

    WAS AN UNANCHORED str.find() (fixed 2026-08-31). _KNOWN_ORIGINS carries
    the bare three-letter forms "SLC", "OAK" and "LAX", so a substring search
    found them inside ordinary English. Executed against this module before
    the fix:

        parse_subject_lane('Relaxed cutoff to Tokyo')   -> ('LAX', 'Tokyo')
        parse_subject_lane('Flaxseed shipment to Busan')-> ('LAX', 'Busan')

    re-LAX-ed. f-LAX-seed. The origin is a lane endpoint, so a bogus one
    splits the lane bucket and mis-labels the carrier scoreboard — the same
    damage the "HILMAR" entry caused before it was removed from this list.

    Word boundaries now. Note this does NOT lose "Oakland": the long form is
    in the list ahead of the short one and still matches at its own index,
    while bare "OAK" correctly stops matching inside "Oakland" — the loop
    takes the earliest match either way.
    """
    low = segment.lower()
    best = None
    for o in _KNOWN_ORIGINS:
        m = re.search(rf"\b{re.escape(o.lower())}\b", low)
        if m and (best is None or m.start() < best[1]):
            best = (o, m.start(), m.end())
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
    ("HAPAG",            "Hapag-Lloyd"),
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

# Carrier tokens that are also common English words or fragments of a longer
# carrier name ("ONE", "CMA"/"CGM" inside "CMA CGM"). They're safe to match
# inside a cell that's KNOWN to be the carrier column, but matching them in
# free prose mis-reads "ONE container" / "for ONE day" as the carrier ONE.
# detect_carrier_token() skips these unless allow_short=True.
_AMBIGUOUS_CARRIER_TOKENS = frozenset({"ONE", "CMA", "CGM", "APL", "ANL"})


def detect_carrier_token(text, *, allow_short: bool = False):
    """Return the canonical carrier named anywhere in ``text``, or None.

    Word-boundary scan over the known carrier tokens, longest/multi-word
    first (so "CMA CGM" wins over a bare "CMA"). Short/ambiguous tokens
    (see ``_AMBIGUOUS_CARRIER_TOKENS``) only match when ``allow_short=True``
    — pass that ONLY when ``text`` is a dedicated carrier cell, never for
    free prose.

    Added 2026-06-15: an Oakland→Manila OL quote parsed its $797 rate but
    blanked the carrier because the production parse_rate_table only read a
    column literally headed "Carrier". This shared detector lets the table
    parser fall back to a data-cell / body-prose scan, and lets QC-056
    self-heal a stored row from its vessel/transshipment text.
    """
    if not text:
        return None
    up = str(text).upper()
    for raw, canonical in _SUBJECT_CARRIER_TOKENS:
        if raw in _AMBIGUOUS_CARRIER_TOKENS and not allow_short:
            continue
        if re.search(rf"\b{re.escape(raw)}\b", up):
            return canonical
    return None


# NOTE: the carrier column's alternate header names used to live here as
# _CARRIER_HEADER_ALIASES, consulted by a _carrier_from_cells() helper. Both
# were removed 2026-08-13 when parse_rate_table moved to header-to-cell
# alignment: the same aliases now sit in the single _TABLE_CELL_ALIASES map
# (all of them mapping to "carrier"), so there is exactly ONE list of header
# names to keep current instead of two that could drift apart.


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


# Region/country words that trail a destination PORT in OL/Lonny subjects
# ("Busan Korea", "Ningbo China", "Yokohama Japan"). The parse_subject_lane
# "...DEST <region> from ORIGIN" fallback strips these so it resolves the
# actual port ("Busan") rather than the country ("Korea").
_TRAILING_REGION_WORDS = frozenset({
    "korea", "china", "japan", "vietnam", "taiwan", "thailand", "indonesia",
    "india", "malaysia", "philippines", "singapore", "prc", "asia",
})

# Generic words that must never be read as a lane endpoint when scanning a
# subject heuristically ("Updated Cheese Rates Busan ... from Dalhart").
_LANE_STOPWORDS = frozenset({
    "re", "fw", "fwd", "pls", "need", "the", "rate", "rates", "rated",
    "quote", "quotes", "quoted", "pricing", "price", "cheese", "updated",
    "update", "new", "revised", "booking", "request", "requested", "cost",
    "costs", "option", "options", "cheaper", "for", "from", "to",
})

# ─────────────────────────────────────────────────────────────────────
# THREE-LETTER TOKENS THAT ARE NEVER A PLACE (contract rule 5, 2026-08-31).
#
# "3-letter IATA codes ARE the identifier — match them, but mind POSITION.
#  Seven incoterms are live IATA codes: FOB Shanghai resolves FOB to Fort
#  Bragg, CPT Hamburg to Cape Town; 12.4 CBM is Columbus."
#
# This repo is ocean-only, so it has no IATA table to collide with — but it
# reads a DESTINATION PORT out of free subject text, and an incoterm sits in
# exactly the position a port does. Executed against this module before the
# fix:
#
#   parse_subject_lane('Updated Rates FOB Korea from Dalhart') -> ('Dalhart', 'FOB')
#   parse_subject_lane('Rates CPT Japan from Tulare')          -> ('Tulare', 'CPT')
#
# FOB and CPT became destination ports, and ingest keys a lane off that. The
# trailing-region pop makes it worse, not better: it strips "Korea" and hands
# back the incoterm sitting behind it.
#
# Units are here for the same reason from the other direction — a 3-letter
# token AFTER A NUMBER is a measure, never a place.
# ─────────────────────────────────────────────────────────────────────
_INCOTERMS = frozenset({
    "exw", "fca", "fas", "fob", "cfr", "cif", "cpt", "cip",
    "daf", "des", "deq", "ddu", "dap", "dpu", "ddp",
})
_UNIT_TOKENS = frozenset({
    "cbm", "kgs", "kg", "lbs", "lb", "mt", "cbf", "cft",
    "teu", "feu", "fcl", "lcl", "hc", "rf", "dv", "gp",
})

#: Never a lane endpoint, whatever position it appears in.
_NOT_A_PLACE = _INCOTERMS | _UNIT_TOKENS



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

    # Last resort — OL/Lonny "...<DEST> [region] from <ORIGIN>" phrasing
    # ("Updated Cheese Rates Busan Korea from Dalhart"): the destination port
    # precedes a trailing region word, the origin follows "from". Only reached
    # when no "X to Y" lane matched above, so it can't shadow the normal form.
    # Without this the Lonny RFQ row is dropped entirely — ingest.build_requests
    # skips any row with no parseable destination (2026-06-24 Busan/Korea miss).
    fm = re.search(r"\bfrom\s+(?P<origin>[A-Z][A-Za-z.\-]+)\b", s)
    if fm:
        origin = fm.group("origin")
        before_toks = re.findall(r"[A-Z][A-Za-z.\-]+", s[:fm.start()])
        while before_toks and before_toks[-1].lower() in _TRAILING_REGION_WORDS:
            before_toks.pop()
        dest = before_toks[-1] if before_toks else None
        if (dest and dest.lower() not in _LANE_STOPWORDS
                and dest.lower() not in _NOT_A_PLACE
                and origin.lower() not in _LANE_STOPWORDS
                and origin.lower() not in _NOT_A_PLACE):
            return _norm(origin), _norm(dest)

    # Last-resort destination recovery (QC-057) — only reached when EVERY
    # branch above already failed, so it can only ADD recoveries, never change
    # an existing extraction. A real Lonny RFQ like "20' reefer request to
    # Yokohama" names a known export port but no "X to Y" lane; without this it
    # parses to (None, None) and ingest.build_requests silently drops the row,
    # so the RFQ is missing from the client report with no alarm. Origin stays
    # None — ingest.clean_origin defaults it to Oakland (Lonny's primary site).
    tm = _DEST_TO_RX.search(s)            # (a) directional "to <known port>"
    if tm:
        return None, _norm(tm.group("dest"))
    bm = _DEST_BARE_RX.search(s)          # (b) bare known port anywhere
    if bm:
        return None, _norm(bm.group("dest"))
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

def _date_from_match(m, default_year: int, ref_date=None):
    """ISO date from a matched date expression.

    ROLL FORWARD ON A YEAR-LESS DATE (2026-08-21). When the writer gave no
    year and the month/day lands BEFORE the email was sent, they meant next
    year: Lonny writing "ETA 1/15" on 10 December means 15 January, not a
    date five weeks in his past. Without this, a December RFQ's requested ETA
    resolved into the past, and core.requested_fit_days then measured OL's
    perfectly good January arrival as ~365 days late — an automatic ETD_MISS
    on every year-end quote.

    Only fires when BOTH the year is absent from the text AND ref_date (the
    message's own send date) is known. An explicit year always wins, and a
    date that is merely a few days stale is left alone by the 45-day grace —
    Lonny does sometimes restate a cutoff that has just passed, and rolling
    that a full year forward would be a worse lie than leaving it.
    """
    try:
        had_year = bool(m.group("y"))
        if "m" in m.groupdict() and m.group("m"):
            mon = _MONTHS.get(m.group("m").lower()[:3])
            d = int(m.group("d"))
            y = int(m.group("y")) if had_year else default_year
        else:
            mon = int(m.group("mo"))
            d = int(m.group("d"))
            y = int(m.group("y")) if had_year else default_year
        if y < 100:
            y += 2000
        out = date(y, mon, d)
        if not had_year and ref_date is not None and out < ref_date - timedelta(days=45):
            out = out.replace(year=out.year + 1)
        return out.isoformat()
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


# Relative date phrases Lonny uses instead of a calendar date (Michael
# 2026-06-16: "sometime he says for etd next week"). Resolved against the
# email's SEND date (ref_date), business reading: "next week" -> Monday of the
# following calendar week; "next <weekday>" -> that weekday next week; "end of
# month"/EOM -> last day of the send month; "end of week"/EOW -> that Friday.
_REL_WEEKDAY = {
    "monday": 0, "mon": 0, "tuesday": 1, "tues": 1, "tue": 1,
    "wednesday": 2, "wed": 2, "thursday": 3, "thurs": 3, "thur": 3, "thu": 3,
    "friday": 4, "fri": 4, "saturday": 5, "sat": 5, "sunday": 6, "sun": 6,
}
_REL_NEXT_WEEKDAY_RX = re.compile(
    r"\bnext\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday"
    r"|mon|tues|tue|wed|thurs|thur|thu|fri|sat|sun)\b", re.IGNORECASE)
_REL_NEXT_WEEK_RX = re.compile(r"\bnext\s+week\b", re.IGNORECASE)
_REL_EOM_RX = re.compile(r"\b(?:eom|end\s+of\s+(?:the\s+)?month)\b", re.IGNORECASE)
_REL_EOW_RX = re.compile(r"\b(?:eow|end\s+of\s+(?:the\s+)?week)\b", re.IGNORECASE)


def _relative_date_in(chunk, ref_date):
    """Resolve a relative date phrase in `chunk` against `ref_date` (a date).
    Returns ISO date string or None. See _REL_* above for the conventions."""
    if not chunk or ref_date is None:
        return None
    if _REL_EOM_RX.search(chunk):
        last = calendar.monthrange(ref_date.year, ref_date.month)[1]
        return ref_date.replace(day=last).isoformat()
    if _REL_EOW_RX.search(chunk):
        return (ref_date + timedelta(days=(4 - ref_date.weekday()))).isoformat()
    # Monday of the following calendar week (shared by next-week + next-weekday)
    days_to_next_monday = (7 - ref_date.weekday()) % 7 or 7
    next_monday = ref_date + timedelta(days=days_to_next_monday)
    m = _REL_NEXT_WEEKDAY_RX.search(chunk)
    if m:
        return (next_monday + timedelta(days=_REL_WEEKDAY[m.group(1).lower()])).isoformat()
    if _REL_NEXT_WEEK_RX.search(chunk):
        return next_monday.isoformat()
    return None


# Where the QUOTED CHAIN starts. Mirrors core._CHAIN_MARKER_RX; duplicated
# rather than imported because body_parser deliberately does not depend on
# core (core imports parsing helpers the other way in places).
_CHAIN_MARKER_RX = re.compile(r"(?im)^\s*(?:from:|de:|von:|enviado el:|sent:)\s")


def _top_message_end(text: str) -> int:
    """Offset where the most-recent message ends and the quoted chain begins."""
    m = _CHAIN_MARKER_RX.search(text or "")
    return m.start() if m else len(text or "")


def _find_date_near(text, anchor_rx, window=120, ref_date=None):
    """Find first plausible date after each anchor match. Reject candidates
    that look like demurrage/free-time ranges (e.g. '10-14 days free')
    or container counts (e.g. '10x40HC'). When `ref_date` is given and no
    absolute date is found in an anchor's window, fall back to a relative
    phrase ('next week', 'next Monday', 'end of month') resolved against it."""
    if not text:
        return None
    # THE EMAIL'S YEAR, NOT THE RUN'S. A bare "ETA 1/15" carries no year, and
    # this used datetime.utcnow().year — the year the PIPELINE happened to
    # run. Because reprocess_bodies re-derives every cached body on every
    # fire, Lonny's December "ETA 1/15" (meaning January) resolved to 15 Jan
    # of the OLD year while he was still in December, and silently changed
    # meaning at midnight on 1 Jan. ref_date is the message's own send date
    # and is already threaded in for the relative-phrase resolver below;
    # using it here makes the same body parse the same way forever, which is
    # what rebuild-not-merge assumes.
    now_year = (ref_date.year if ref_date is not None
                else datetime.now(timezone.utc).year)
    _top_end = _top_message_end(text)
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
                # ROLL FORWARD ONLY IN THE TOP MESSAGE. A reply quotes the
                # whole thread, so a re-ping sent in August carries Lonny's
                # May "ETA 6/1" underneath it. Against ref_date=Aug 20 that
                # date is 80 days past, and the roll-forward turned a
                # June-2026 ask into June 2027 — an ask a year out, feeding
                # ~-290d into avg_etd_fit_days. The date is still READ from
                # the chain (losing it would be worse); it just is not
                # re-dated against a clock that was not running when it was
                # written.
                _in_top = am.start() < _top_end
                iso = _date_from_match(dm, now_year,
                                       ref_date if _in_top else None)
                if iso:
                    return iso
        rel = _relative_date_in(chunk, ref_date)
        if rel:
            return rel
    return None


_ETA_REQ_ANCHORS = re.compile(
    # ARRIVAL-SIDE ONLY. 2026-08-21, Michael: "compare like with like".
    #
    # Until today this pattern also matched DEPARTURE language — "cutoff",
    # "ship by", "load by", "sailing by", "need to sail/ship/load/depart/
    # leave" — and filed whatever it found as a requested ARRIVAL. That date
    # was then differenced against OL's offered ARRIVAL to produce
    # etd_fit_days, so on any cutoff-style RFQ the "miss" it computed was the
    # OCEAN TRANSIT TIME. Measured 2026-08-21:
    #     "Cutoff 8/28"          -> eta_requested 2026-08-28
    #                               vs OL ETA 30-Sep-26 = 33 days -> ETD_MISS
    #     "Need to sail by 8/25" -> 36 days -> ETD_MISS
    # A month of ocean freight to Asia clears the 5-day ETD_MISS threshold
    # every single time, so every cutoff RFQ was auto-stamped "missed the
    # requested ETD" and that reason fed the loss analytics and the carrier
    # scoreboard.
    #
    # Every one of those anchors already lives in _ETD_REQ_ANCHORS below,
    # which populates etd_requested — the departure-side ask. Nothing is lost
    # by removing them here; the date now lands in the field that means what
    # it says, and core.requested_fit_days refuses to cross the two legs.
    r"(?:need(?:s|ed)?\s+(?:to\s+)?(?:arrive|deliver)"
    r"|target\s+eta|requested\s+eta|require(?:d)?\s+by"
    r"|prefer(?:red)?\s+eta"
    # 2026-06-16 (Michael, Lonny's real RFQ "ETA 8/7"): Lonny states his
    # target arrival as a BARE "ETA <date>" / "arrival" / "deliver by" — no
    # "target"/"requested" prefix. On the request side that IS the requested
    # ETA, so eta_requested was 100% blank on his quotes. OL-side ETAs are
    # parsed separately by parse_eta_offered, so this doesn't conflate them.
    r"|e\.?t\.?a\.?|arrival|arrive(?:\s+by)?|deliver(?:y|ed)?(?:\s+by)?"
    r"|due(?:\s+(?:by|in\s+port))?|in\s+(?:your\s+)?port\s+by)",
    re.IGNORECASE,
)

_ETD_OFFER_ANCHORS = re.compile(
    r"(?:etd(?:\s*pol)?|ets|sail(?:s|ing)?(?:\s+date)?|departs?|departure)\s*[:\-]?",
    re.IGNORECASE)
_ETA_OFFER_ANCHORS = re.compile(
    r"(?:eta(?:\s*pod)?|arriv(?:es|ing|al)?)\s*[:\-]?", re.IGNORECASE)
_ORIGIN_CUTOFF_ANCHORS = re.compile(r"(?:origin\s+cutoff|erd|pickup\s+cutoff|door\s+cutoff)\s*[:\-]?", re.IGNORECASE)

def parse_eta_requested(text, ref_date=None):
    return _find_date_near(text or "", _ETA_REQ_ANCHORS, ref_date=ref_date)
# ref_date ON THE OFFERED SIDE TOO (2026-08-21, second pass). The requested
# parsers took the year-less fallback year from the message's send date while
# these three still took it from the RUN clock. Before that split, both sides
# shared the same wrong year at a year boundary and the difference cancelled
# to roughly zero; afterwards they disagreed by a year, so a December ask
# ("ETA 1/15" -> 2027-01-15) measured against a prose-parsed offer ("ETA 1/20"
# -> 2026-01-20) came out ~360 days EARLY. Fixing one leg and not the other
# turned a wash into a fabricated number, which is worse than the bug.
#
# The grid path is unaffected either way — OL's table cells carry explicit
# years ("10-Oct-26") — so this only governs the prose fallback. Callers that
# know the message's send date should pass it; the run year remains the
# last resort for callers that genuinely have no date.
def parse_etd_offered(text, ref_date=None):
    return _find_date_near(text or "", _ETD_OFFER_ANCHORS, ref_date=ref_date)


def parse_eta_offered(text, ref_date=None):
    return _find_date_near(text or "", _ETA_OFFER_ANCHORS, ref_date=ref_date)


def parse_origin_cutoff(text, ref_date=None):
    return _find_date_near(text or "", _ORIGIN_CUTOFF_ANCHORS, ref_date=ref_date)


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
    # 2026-06-16: a BARE "ETD <date>" / "ETD next week" is Lonny's most common
    # departure ask (no "by"/"target" prefix) — without this anchor the
    # relative-date resolver had nothing to attach to.
    r"|etd|ets"
    # 2026-05-19 broadening: Lonny's "cutoff" phrasing usually means ETD.
    # Catches "Cutoff week of 4/27" / "Cut off 5/1" / "cutoff by 5/15".
    r"|cut[-\s]?off"
    r"|week\s+of"
    r"|by\s+EOD)",
    re.IGNORECASE,
)

# Lonny's requested free time, e.g. "14 days demurrage requested",
# "10 days detention", "7 days free time" (Michael 2026-06-16). This is his
# ASK, distinct from origin_free_time/dest_free_time which OL quotes back.
_FREE_TIME_REQ_RX = re.compile(
    r"(\d{1,3})\s*(?:days?|dys?)\s*(?:of\s+)?"
    r"(demurrage|detention|free\s*time|combined(?:\s+free)?|free|dem|det)\b",
    re.IGNORECASE,
)


def parse_free_time_requested(text):
    """Lonny's requested free time as a short label, e.g.
    "14 days demurrage requested" -> "14d demurrage". Returns None if absent."""
    if not text:
        return None
    m = _FREE_TIME_REQ_RX.search(text)
    if not m:
        return None
    days, raw = m.group(1), m.group(2).lower()
    kind = ("demurrage" if raw.startswith("dem")
            else "detention" if raw.startswith("det")
            else "free time")
    return f"{days}d {kind}"


def parse_etd_requested(text, ref_date=None):
    """Lonny's departure-date ask. Captures explicit "ship by X" / "departure
    X" patterns AND the more common "cutoff X" phrasing (which functionally
    means ETD in shipping vernacular). With ref_date set, also resolves
    relative asks ("ETD next week"). Returns ISO date or None."""
    return _find_date_near(text or "", _ETD_REQ_ANCHORS, ref_date=ref_date)


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

# ---------- OL rate table: HEADER-TO-CELL ALIGNMENT ----------
#
# OL sends every quote as an HTML <table>. html_to_text already flattens it
# into pipe-separated rows whose header line and data line are exactly
# aligned, e.g. (real body, 2026-08-12 Oakland->HCMC):
#
#   POL | POD | Container Size | Vessel | Voyage | ERD | ... | DESTINATION FREE TIME
#   Oakland | HCMC (CAT LAI) | 2 X 20'DV | WAN HAI A01 | W019 | 24-Aug-26 | ... | 14 DETENTION + 14 DEMURRAGE FREE DAYS
#
# THE INVARIANT: when that grid is present, every field is read out of a CELL
# of the data row sitting under its own header. parse_rate_table does not
# regex-scan the surrounding body for carrier, vessel or rate. That is what
# makes OL's standing boilerplate structurally unreachable rather than merely
# unlikely.
#
# Measured 2026-08-13 on two real OL quotes before this rewrite, the
# whole-body scan returned:
#   carrier_quoted "MSC"  <- the standing footer "Maersk, Sealand, MSC, ONE,
#                            CMA and Cosco do not accept Dummy SI"
#   vessel_voyage  "dive" <- the standing disclaimer "... routing changes,
#                            vessel diversion, or alternate discharge ..."
# and both emails lost their real values (ALGECIRAS's ETA became Lonny's own
# requested "ETA 10/19" from the bottom of the forwarded chain; HCMC's
# $475.00 was dropped entirely by a `500 <= rate` prose gate).

# Normalized header cell -> canonical cell key.
#
# A header cell is recognised only as a WHOLE CELL, never as a substring. That
# is the structural half of the fix: OL's NRA footer row
#   "ACCEPTANCE OF THE RATES AND TERMS OF THIS NRA OR NRA AMENDMENT."
# contains the substring "rate" and scored as a header hint under the old
# substring scan. Normalized whole-cell, it is simply not a header name.
_TABLE_CELL_ALIASES = {
    # POL/POD — OL relabels these across templates (2026-06-17).
    "pol": "pol", "port_of_loading": "pol", "load_port": "pol",
    "loading_port": "pol", "origin_port": "pol", "pol_port": "pol",
    "pod": "pod", "port_of_discharge": "pod", "discharge_port": "pod",
    "destination_port": "pod", "dest_port": "pod", "pod_port": "pod",
    "container_size": "container_size",
    "vessel": "vessel", "voyage": "voyage",
    "erd": "erd", "doc_cut": "doc_cut", "port_cut": "port_cut",
    "rail_cut": "rail_cut",
    # LINDA'S TEMPLATE, 2026-08-21. Measured, not guessed: diag run
    # 32493969967 printed her header verbatim as
    #   Port of loading | Port of discharge | … | ERD | Doc Cutoff | Cutoff |
    #   Sail | Arrive | RATE | CARRIER | …
    # Three of those named nothing here, so on every quote she sends, the
    # doc cutoff, the port cutoff and — the one that reached the CEO — the
    # ETA were read out of a column the parser could not name and dropped.
    # The Algeciras row is the proof: OL's grid says Arrive 24-Oct-26, the
    # report said 2026-10-21, and 10/21 is the date LONNY asked for in the
    # RFQ quoted underneath. Michael, 2026-08-20: "important data still
    # missing".
    "doc_cutoff": "doc_cut", "document_cutoff": "doc_cut",
    "cutoff": "port_cut", "port_cutoff": "port_cut",
    "cargo_cutoff": "port_cut", "cy_cutoff": "port_cut",
    # ETD/ETA — OL relabels these across schedule templates (2026-06-16).
    "etd": "etd", "etd_pol": "etd", "pol_etd": "etd", "sailing": "etd",
    "departure": "etd", "departs": "etd", "sail": "etd", "ets": "etd",
    "sails": "etd", "depart": "etd",
    "eta": "eta", "eta_pod": "eta", "pod_eta": "eta", "arrival": "eta",
    "arrives": "eta", "arriving": "eta", "arrive": "eta",
    "rate": "rate", "dthc": "dthc",
    # Carrier — OL relabels this too (2026-06-15 Manila fix).
    "carrier": "carrier", "ocean_carrier": "carrier", "ocean_line": "carrier",
    "line": "carrier", "carrier_line": "carrier", "line_carrier": "carrier",
    "steamship_line": "carrier", "steamship": "carrier", "ssl": "carrier",
    "scac": "carrier", "vessel_operator": "carrier", "operator": "carrier",
    "co_carrier": "carrier",
    "transshipment": "transshipment",
    "origin_free_time": "origin_free_time",
    "destination_free_time": "dest_free_time",
    "dest_free_time": "dest_free_time",
}

# A header row needs this many pipe cells AND this many recognised header
# names. Three recognised names is the floor that still accepts the narrowest
# real OL grid ("POL | POD | RATE | CARRIER") while rejecting every prose and
# footer line in the two measured emails (the NRA rows collapse to 1 cell and
# 0 recognised names, so they fail both gates).
_HEADER_MIN_CELLS = 4
_HEADER_MIN_KNOWN = 3
# How far under the header the data row may sit (blank / decoration lines).
_HEADER_DATA_WINDOW = 6

# Cell values that mean "OL left this column blank". Dropped before typing so
# a genuinely absent field stays absent instead of becoming the literal "-".
_TABLE_PLACEHOLDERS = frozenset({
    "", "-", "--", "---", "n/a", "na", "n.a.", "tbd", "tba", "tbn",
    "none", "null", "nil", "pending", "?",
})

_TABLE_MONEY_RX = re.compile(r"\$?\s*(\d[\d,]*(?:\.\d{1,2})?)")

# Detention/demurrage day counts, read ONLY from inside the free-time cells.
# "7 COMBINED FREE DAYS" deliberately yields neither: a combined pool is not
# detention and is not demurrage, and splitting it would be a guess.
_FREE_DAYS_RXES = (
    ("detention_free", re.compile(r"(\d{1,3})\s*(?:FREE\s+)?(?:DAYS?\s+)?DETENTION",
                                  re.IGNORECASE)),
    ("demurrage_free", re.compile(r"(\d{1,3})\s*(?:FREE\s+)?(?:DAYS?\s+)?DEMURRAGE",
                                  re.IGNORECASE)),
)

# OL's standing footer / legal / advisory lines. The prose carrier last-resort
# below never sees these, so the Dummy-SI carrier list and the vessel-diversion
# disclaimer cannot supply a carrier no matter what else changes.
_CARRIER_BOILERPLATE_RXES = (
    re.compile(r"dummy\s+si", re.IGNORECASE),
    re.compile(r"these\s+carriers\s+will\s+not\s+accept", re.IGNORECASE),
    re.compile(r"\bdisclaimer\b", re.IGNORECASE),
    re.compile(r"customer\s+advisory", re.IGNORECASE),
    re.compile(r"war\s+risk|bunker\s+surcharge|force\s+majeure", re.IGNORECASE),
    re.compile(r"nra\s+(?:or\s+nra\s+)?amendment", re.IGNORECASE),
    re.compile(r"vessel\s+diversion|voyage\s+termination|routing\s+changes",
               re.IGNORECASE),
    re.compile(r"labor\s+unrest", re.IGNORECASE),
    re.compile(r"are\s+estimates\s+and\s+may\s+change", re.IGNORECASE),
    re.compile(r"carriers?\s+are\s+initiating", re.IGNORECASE),
    re.compile(r"follow\s+us\s+on\s+social\s+media", re.IGNORECASE),
)


#: Words allowed to accompany a field word in a header without changing what
#: the column IS: "RATE (USD)" is still the rate, "ETD (POL)" still the ETD.
#: Anything NOT here and not a field alias disqualifies the header — see
#: _header_key. Deliberately short: every addition widens what the parser
#: will claim to understand.
_HEADER_QUALIFIERS = frozenset({
    "usd", "us", "usd$", "ocean", "est", "estimated", "actual",
    "date", "dates", "time", "no", "number", "num", "ref", "id",
    "of", "the", "at", "in", "on", "per", "container", "cntr", "box",
    "from", "to", "amt", "amount", "total",
})


def _norm_header(h: str) -> str:
    """Header cell -> comparable key: 'Ocean Carrier' -> 'ocean_carrier'."""
    return re.sub(r"[^a-z0-9]+", "_", (h or "").lower()).strip("_")


def _header_key(cell: str):
    """Canonical field key for a header cell, or None if it names no field.

    Whole-cell match FIRST. That is the structural half of the boilerplate
    fix: OL's NRA footer row "ACCEPTANCE OF THE RATES AND TERMS OF THIS NRA
    OR NRA AMENDMENT." contains the substring "rate" and scored as a header
    under the old substring scan; as a whole cell it names nothing.

    Failing that, fall back to the cell's own WORD TOKENS, so OL's qualified
    column labels keep mapping without anyone having to enumerate every one
    ("RATE (USD)" -> rate, "Ocean Rate" -> rate, "ETD (POL)" -> etd). This is
    still not a substring scan — "RATES" is not the token "rate", so the NRA
    footer is rejected here too. A cell whose tokens imply TWO different
    fields (a merged "Vessel/Voyage" column) is ambiguous and stays unmapped
    rather than being guessed at.
    """
    norm = _norm_header(cell)
    if norm in _TABLE_CELL_ALIASES:
        return _TABLE_CELL_ALIASES[norm]
    # TOKEN FALLBACK, AND IT REFUSES ON ANY WORD IT DOES NOT KNOW.
    #
    # An earlier form of this mapped a header if ANY of its tokens matched,
    # which is how "Terminal Operator" became the carrier column: `operator`
    # is a carrier alias, the decoy sat left of the real CARRIER column, and
    # first-mapped-wins handed carrier_quoted the value "SSA MARINE".
    # "Service Line" did the same via `line`. Measured, not theorised — an
    # adversarial review produced both, 2026-08-13.
    #
    # So an unrecognised word now DISQUALIFIES the cell. A header we only
    # half-understand is a header we do not understand, and the cost of
    # guessing is a wrong carrier or a wrong rate on a client report. Known
    # qualifiers ("RATE (USD)", "ETD (POL)") are the only words allowed to
    # ride along.
    tokens = [t for t in norm.split("_") if t]
    keys, seen_field = set(), False
    for tok in tokens:
        if tok in _TABLE_CELL_ALIASES:
            keys.add(_TABLE_CELL_ALIASES[tok])
            seen_field = True
        elif tok not in _HEADER_QUALIFIERS:
            return None
    return keys.pop() if (seen_field and len(keys) == 1) else None


def _split_pipe_cells(line: str) -> list:
    """Split one flattened table line into trimmed cells.

    Drops the trailing empties html_to_text leaves behind ("a | b | " ends in
    a pipe). Returns [] for a line carrying no pipe at all.
    """
    if "|" not in (line or ""):
        return []
    cells = [c.replace("\xa0", " ").strip() for c in line.split("|")]
    while cells and not cells[-1]:
        cells.pop()
    return cells


def _collapse_multiline_pipe_table(text: str) -> str:
    """Convert the multi-line pipe-table template (each cell on its own line
    with a leading '|') into the single-line form the row finder expects.

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
         ...
         | $3500
         | CMA

    OLD OL TEMPLATE (unchanged):
        POL | POD | Container Size | ... | RATE | CARRIER | ...
        Oakland | Yokohama | 2x40'RF | ... | $3500 | CMA | ...
    """
    if not text:
        return text
    lines = text.split("\n")
    out_lines = []
    i = 0
    while i < len(lines):
        line = lines[i].rstrip()
        stripped = line.strip()
        # Multi-line row opener: a non-empty non-pipe line followed by several
        # pipe-leading lines. 5+ collected cells to avoid firing on prose.
        if (stripped and not stripped.startswith("|") and i + 1 < len(lines)
                and lines[i + 1].strip().startswith("|")):
            row = [stripped]
            j = i + 1
            while j < len(lines):
                nxt = lines[j].strip()
                if not nxt.startswith("|"):
                    break
                row.append(nxt[1:].strip())
                j += 1
            if len(row) >= 5:
                while row and not row[-1]:
                    row.pop()
                out_lines.append(" | ".join(row))
                i = j
                continue
        out_lines.append(line)
        i += 1
    return "\n".join(out_lines)


def _is_rule_row(cells: list) -> bool:
    """True for a decoration row ('|---|---|') that carries no values."""
    return bool(cells) and all(
        (not c) or re.fullmatch(r"[-=:_\s]+", c) for c in cells)


def _find_table_block(text: str):
    """Locate OL's rate-table header and EVERY option row aligned under it.

    Returns ``[header_cells, data_cells, ...]`` or None. The FIRST qualifying
    header wins: in a flattened reply chain the newest message sits at the
    top, so first-found is the current quote, not a quoted older one.

    ONE HEADER, MANY ROWS — the 2026-08-21 change. This used to stop at the
    first data row, so an OL reply offering a choice was stored as whichever
    line Maria happened to type first and the rest was discarded unseen
    (Michael, 2026-08-20: "the numbers are wrong for $" / "could be different
    rates for different steamship lines"). Measured on real bodies from the
    week of 2026-08-17: Oakland->Xingang offered $810 ONE via Pusan AND $675
    CMA direct; Oakland->Algeciras offered $4,938 CMA AND $4,201 Hapag. Both
    discarded options were the cheaper one.

    Every option row is returned. Choosing among them is _pick_headline's job,
    and dropping rows that quote a different lane is _same_lane_options'.
    """
    if not text:
        return None
    lines = _collapse_multiline_pipe_table(text).split("\n")
    for i, line in enumerate(lines):
        header = _split_pipe_cells(line)
        if len(header) < _HEADER_MIN_CELLS:
            continue
        known = sum(1 for c in header if _header_key(c))
        if known < _HEADER_MIN_KNOWN:
            continue
        rows: list = []
        gap = 0
        for j in range(i + 1, len(lines)):
            data = _split_pipe_cells(lines[j])
            if sum(1 for c in data if c) >= 2 and not _is_rule_row(data):
                # A repeated header ENDS this grid instead of joining it —
                # OL restates the column names when a second table follows.
                if sum(1 for c in data if _header_key(c)) >= _HEADER_MIN_KNOWN:
                    break
                rows.append(data)
                gap = 0
                continue
            # Blank and decoration lines are tolerated between rows, but any
            # NON-blank line that is not a data row ends the grid, and the run
            # of skipped lines is bounded. Together those stop the scan from
            # wandering out of the table and into OL's NRA footer, which is
            # itself pipe-shaped ("... NRA AMENDMENT. | | |").
            if rows and lines[j].strip():
                break
            gap += 1
            if gap > _HEADER_DATA_WINDOW:
                break
        return [header, *rows] if rows else None
    return None


def _find_table_rows(text: str):
    """Header plus the FIRST data row only.

    Kept as the narrow view onto _find_table_block for callers that genuinely
    want one row (and for the tests that pin the header-detection gates).
    """
    block = _find_table_block(text)
    return [block[0], block[1]] if block else None


def _option_pod(opt: dict) -> str:
    """Comparable form of an option's POD cell ('Haiphong'/'HAIPHONG' -> same)."""
    return re.sub(r"[^a-z]", "", (opt.get("pod") or "").lower())


def _same_lane_options(options: list):
    """Drop option rows quoting a DIFFERENT destination from the first row's.

    OL pastes more than one lane into a single reply. The 2026-08-19 answer to
    "Oakland to Haiphong" carries a Haiphong row AND an
    "OAKLAND | SHANGHAI | ... | $740 | CMA" row. Nothing but row order was
    keeping that Shanghai price off a Haiphong quote, and row order is not a
    guarantee — it is what Maria typed.

    A row with no POD cell is KEPT: an absent cell is not evidence of a
    different lane, and most narrow grids have no POD column at all.

    Returns (kept, dropped_pods).
    """
    lane = ""
    for opt in options:
        lane = _option_pod(opt)
        if lane:
            break
    if not lane:
        return list(options), []
    kept, dropped = [], []
    for opt in options:
        pod = _option_pod(opt)
        if pod and pod != lane:
            dropped.append(opt.get("pod"))
        else:
            kept.append(opt)
    return kept, dropped


def _pick_headline(options: list) -> dict:
    """The option that represents the quote: the LOWEST rate OL offered.

    [ASSUMPTION 2026-08-21, stated to Michael, awaiting his ruling] "What OL
    quoted" for a lane is the best price OL put on the table for it. The rule
    it replaces was not a rule at all — it was "whichever row came first",
    which on 2026-08-19 reported $4,938 to Algeciras when OL had also offered
    $4,201, and $810 to Xingang against $675. In both the discarded option was
    cheaper AND arrived sooner, so first-row was not buying service quality.

    Every other field of the quote — carrier, vessel, voyage, ETD, ETA, free
    time — comes from THIS SAME ROW, so a quote can never pair one sailing's
    price with another sailing's schedule.

    When no row priced anything, the first row still stands: it carries the
    carrier and the schedule, and dropping it would lose a real response.
    """
    priced = [o for o in options if o.get("ol_rate") is not None]
    if not priced:
        return dict(options[0]) if options else {}
    return dict(min(priced, key=lambda o: o["ol_rate"]))


def _table_options(block: list, text: str):
    """Parse every option row of a matched grid, then drop the foreign lanes.

    Shared by both trees' parse_rate_table so they can never disagree about
    which option is the quote. Returns (options, dropped_pods).
    """
    header, data_rows = block[0], block[1:]
    options = []
    for idx, data in enumerate(data_rows):
        cells = _table_cells(header, data)
        opt = _rate_table_from_cells(cells)
        if "carrier_quoted" not in opt:
            opt.update(_carrier_fallback(data, text or ""))
        opt.update(_table_date_fields(cells))
        if not opt:
            continue
        # AN OPTION ROW MUST NAME A SHIP OR A LINE (2026-08-26).
        #
        # When OL's grid wraps, the tail cells form their own pipe line, and
        # reading EVERY row under the header — the 2026-08-21 multi-option
        # change — accepted that tail as an option. Its cells land under
        # whatever headers they happen to line up with, so free-time text
        # became a POD: QC-079 reported req_811913d0bdd8e1d1 (Osaka) as also
        # pricing "ORIGIN FREE DAYS, 3 DETENTION + 4 DEMURRAGE FREE DAYS".
        #
        # Mostly the lane guard below then drops it, which is why this looked
        # like noise. It is not. A tail whose POD cell is BLANK has no lane to
        # disagree with, so it survives — and if it carries any $ figure it
        # becomes a priced option and WINS the lowest-rate pick. Measured:
        #
        #     OAKLAND | OSAKA | ... | $3,210 | CMA
        #     SUBJECT TO |  | SPACE AND EQUIPMENT | ... | $99 |
        #     -> ol_rate 99.0 instead of 3210.0
        #
        # Every real option row OL sends carries a vessel AND a carrier (all
        # five multi-option bodies in diag run 32493969967 do). Boilerplate
        # and wrapped tails carry neither. The FIRST row is exempt: a narrow
        # grid can legitimately omit the carrier column, and _carrier_fallback
        # exists for exactly that.
        if idx and not (opt.get("carrier_quoted") or opt.get("vessel")):
            continue
        options.append(opt)
    return _same_lane_options(options)


def _table_cells(header: list, data: list) -> dict:
    """Zip a header row onto its data row BY POSITION, keyed by canonical name.

    A short data row is padded and a long one truncated, so a value can never
    slide under the wrong header. Unknown headers still consume their column
    slot (that is what preserves alignment) but emit nothing.
    """
    data = list(data[:len(header)]) + [""] * max(0, len(header) - len(data))
    cells: dict = {}
    for raw_header, raw_value in zip(header, data, strict=True):
        key = _header_key(raw_header)
        if not key:
            continue
        value = re.sub(r"\s+", " ", (raw_value or "").replace("\xa0", " ")).strip()
        if value.lower() in _TABLE_PLACEHOLDERS:
            continue
        if value and key not in cells:
            cells[key] = value
    return cells


def _cell_money(value: str):
    """Float out of a RATE cell, or None when the cell holds no number.

    No plausibility gate. The value comes from the column OL headed RATE, and
    header-to-cell alignment already rules out a stray date or voyage landing
    here — whereas the old prose fallback's `500 <= val` gate is precisely
    what dropped HCMC's real $475.00 quote on the floor.
    """
    m = _TABLE_MONEY_RX.search(value or "")
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None


def _strip_carrier_boilerplate(text: str) -> str:
    """Drop OL's standing legal/advisory lines before any prose carrier scan."""
    return "\n".join(
        ln for ln in (text or "").split("\n")
        if not any(rx.search(ln) for rx in _CARRIER_BOILERPLATE_RXES)
    )


def _carrier_from_prose(text: str):
    """Last-resort carrier from the sentence that introduces the grid
    ("Pleased to offer the below on Maersk ..." — the 2026-06-15 Manila fix,
    and OL's prose-only quotes such as the 2026-06-24 Houston->Busan Hapag).

    Two guards make boilerplate unusable as a source:
      1. OL's standing disclaimer lines are stripped first.
      2. A line naming TWO OR MORE distinct carriers is rejected outright. A
         LIST of carriers never identifies THE quoted carrier — and a list is
         exactly what "Maersk, Sealand, MSC, ONE, CMA and Cosco do not accept
         Dummy SI" is. Reading "MSC" off that line is the defect this replaces.
    """
    for line in _strip_carrier_boilerplate(text).split("\n"):
        tok = detect_carrier_token(line, allow_short=False)
        if not tok:
            continue
        up = line.upper()
        named = {canon for raw, canon in _SUBJECT_CARRIER_TOKENS
                 if raw not in _AMBIGUOUS_CARRIER_TOKENS
                 and re.search(rf"\b{re.escape(raw)}\b", up)}
        if len(named) == 1:
            return tok
    return None


def _rate_table_from_cells(cells: dict) -> dict:
    """Build the rate-table output dict from aligned cells and NOTHING else.

    Every value here traces to one cell of the matched data row. Callers add
    only the carrier fallbacks (for grids with no carrier column) and the
    tree-local date/legacy keys.
    """
    out: dict = {}

    carrier = re.sub(r"[\*\(].*$", "", cells.get("carrier") or "").strip()
    if carrier:
        # This IS the dedicated carrier cell, so allow_short is safe here and
        # nowhere else: a bare "CMA" / "ONE" in this column is the carrier,
        # not English prose. core.normalize_carrier does the canonicalizing
        # ("ONE LINE" -> ONE, "CMA" -> CMA CGM).
        out["carrier_quoted"] = _canon_carrier(
            detect_carrier_token(carrier, allow_short=True) or carrier)

    rate = _cell_money(cells.get("rate") or "")
    if rate is not None:
        out["ol_rate"] = rate

    vessel = cells.get("vessel") or ""
    voyage = cells.get("voyage") or ""
    if vessel:
        out["vessel"] = vessel
    if voyage:
        out["voyage"] = voyage
    if vessel or voyage:
        # House convention, matching scripts/pdf_parser ("ONE OLYMPUS 080W").
        out["vessel_voyage"] = f"{vessel} {voyage}".strip()

    for key in ("pol", "pod", "container_size", "transshipment", "dthc",
                "origin_free_time", "dest_free_time"):
        if cells.get(key):
            out[key] = cells[key]
    # POL/POD are the only PLACE columns in the grid, and OL writes UN/LOCODEs
    # into them ("JPYOK" in a Port of Discharge cell). Everything else in the
    # loop above is a rate, a size or a free-time string and must stay verbatim.
    # This is the third spelling: the cell copy above never called _norm, so a
    # POD reading JPYOK landed raw, and ingest's standalone-booking path
    # (ingest.py:1148) stored it as the row's destination with no normalizer at
    # all. core.resolve_locode returns None for anything not in the table, so a
    # real port name in these cells is untouched.
    for _place in ("pol", "pod"):
        _resolved = _core.resolve_locode(out.get(_place))
        if _resolved:
            out[_place] = _resolved

    return out


def _free_day_counts(out: dict) -> dict:
    """Detention/demurrage day counts read ONLY from the free-time strings
    already extracted from their own cells. Never from body prose."""
    blob = " ".join(v for v in (out.get("origin_free_time"),
                                out.get("dest_free_time")) if v)
    found = {}
    for key, rx in _FREE_DAYS_RXES:
        m = rx.search(blob)
        if m:
            found[key] = int(m.group(1))
    return found

def _canon_carrier(name):
    """Canonicalize a carrier name through core.normalize_carrier
    ("ONE LINE" -> ONE, "CMA" -> CMA CGM). Returns the name unchanged when
    core is unavailable or has no mapping — never None for a non-empty name."""
    if not name:
        return None
    try:
        import core as _core
        return _core.normalize_carrier(name) or name
    except Exception:
        return name


# The src/hilmar mirror sets this True. It selects that tree's HISTORICAL
# output contract (ISO dates, the legacy `etd`/`eta` key spellings,
# detention/demurrage day integers, prose rate_expiry), which
# src/hilmar/ingest.py and scripts/build_ops_flow_v2.py read. Production
# (scripts/) persists the RAW cell text — scripts/ingest.py and every report
# renderer have always written "7-Sep-26". Converting either side is a
# persisted-data migration, not a parser fix, so both contracts stand and the
# divergence is DECLARED HERE instead of hiding in two different parsers.
_LEGACY_SRC_CONTRACT = False


_TABLE_DATE_RX = re.compile(
    r"^(\d{1,2})[-\s/]"
    r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*"
    r"[-\s/](\d{2,4})$",
    re.IGNORECASE,
)


def _parse_table_date(s):
    """'DD-Mon-YY' / 'DD-Mon-YYYY' / ISO 'YYYY-MM-DD' -> ISO date, else None.
    Only consulted under _LEGACY_SRC_CONTRACT; production keeps the raw cell."""
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


def _carrier_fallback(data_cells, text: str) -> dict:
    """Carrier for a grid that has no carrier column at all.

    Scans the matched row's OWN data cells first (OL sometimes merges the
    carrier into an unlabeled column), then falls back to the grid's
    introducing prose through _carrier_from_prose's boilerplate guards.
    """
    tok = next((t for t in (detect_carrier_token(c, allow_short=False)
                            for c in data_cells) if t), None)
    tok = tok or _carrier_from_prose(text)
    return {"carrier_quoted": _canon_carrier(tok)} if tok else {}


def _table_date_fields(cells: dict) -> dict:
    """Date-ish columns, in whichever format this tree's consumers read.
    See _LEGACY_SRC_CONTRACT above for why the two trees differ here."""
    out: dict = {}
    for cell_key, out_keys in (
        ("etd", ("etd_offered",)),
        ("eta", ("eta_offered",)),
        # ERD goes to both schema names (erd is canonical, origin_cutoff is
        # the legacy alias ingest/patch_carriers read). Same value, two names.
        ("erd", ("erd", "origin_cutoff")),
        ("doc_cut", ("doc_cutoff",)),
        ("port_cut", ("port_cutoff",)),
    ):
        raw = cells.get(cell_key)
        if not raw:
            continue
        value = (_parse_table_date(raw) or raw) if _LEGACY_SRC_CONTRACT else raw
        for key in out_keys:
            out[key] = value
    if _LEGACY_SRC_CONTRACT:
        if "etd_offered" in out:
            out["etd"] = out["etd_offered"]
        if "eta_offered" in out:
            out["eta"] = out["eta_offered"]
    return out




def _prose_lane(text: str):
    """Resolve origin/dest from OL prose: "from <ORIGIN> port to <DEST>" or a
    bare "<ORIGIN> to <DEST>" spec line. Returns (origin, dest) or (None,None).
    Case-sensitive on the place names (they're Capitalized) so a stray "to"
    in lowercase prose can't form a false lane."""
    if not text:
        return None, None
    pats = (
        # "from Houston port to Busan" — tolerate an interposed
        # "port"/"terminal" word (stripped off the captured origin below).
        # Dest excludes '.' so it stops at the sentence period ("Busan. We ...").
        re.compile(r"\bfrom\s+(?P<o>[A-Z][A-Za-z.\s]{2,30}?)\s+to\s+"
                   r"(?P<d>[A-Z][A-Za-z]+(?:\s[A-Z][A-Za-z]+){0,2})\b"),
        # A bare "Houston to Busan" spec line (start-anchored, dest followed
        # by a separator/size token: "Houston to Busan _ 40' Reefer").
        re.compile(r"(?m)^[\s>]*(?P<o>[A-Z][A-Za-z]+(?:\s[A-Z][A-Za-z]+){0,1})"
                   r"\s+to\s+(?P<d>[A-Z][A-Za-z]+(?:\s[A-Z][A-Za-z]+){0,1})"
                   r"(?=\s*(?:[_|–\-]|\d|$))"),
    )
    for rx in pats:
        m = rx.search(text)
        if not m:
            continue
        o = re.sub(r"\s+(?:port|terminal|seaport)\s*$", "", m.group("o"),
                   flags=re.IGNORECASE).strip()
        d = m.group("d").strip()
        o, d = _norm(o), _norm(d)
        if (o and d and o.lower() not in _LANE_STOPWORDS
                and o.lower() not in _NOT_A_PLACE
                and d.lower() not in _LANE_STOPWORDS
                and d.lower() not in _NOT_A_PLACE):
            return o, d
    return None, None


def parse_prose_rate(text: str) -> dict:
    """Extract a quote from an OL *prose* rate reply (no pipe/column table).

    OL sends some quotes as free prose instead of the grid, e.g. the
    2026-06-24 Houston->Busan Hapag quote:

        Please see able Hapag option from Houston port to Busan.
        Houston to Busan _ 40' Reefer _ Chilled Cheese
        Hapag: $2,275/40' reefer
        4 equipment free days at Origin
        3 equipment free days at destination
        Direct service

    Returns the same field shape as parse_rate_table (carrier_quoted /
    ol_rate / pol / pod / container_size / origin_free_time / dest_free_time
    / transshipment / etd_offered / eta_offered). Gated on a plausible $
    ocean rate — the caller already knows the email is an OL rate response,
    so a dollar figure is the one signal that this prose *is* a quote.
    Returns {} when no rate is found.
    """
    if not text:
        return {}
    out: dict = {}
    # Rate — first plausible $ amount in the $200–$50k ocean range. The newest
    # quote sits at the top of a reply, so first-match is the current rate.
    for m in re.finditer(r"\$\s*([\d,]+(?:\.\d{2})?)", text):
        try:
            val = float(m.group(1).replace(",", ""))
        except ValueError:
            continue
        if 200 <= val <= 50000:
            out["ol_rate"] = val
            break
    if "ol_rate" not in out:
        return {}
    # Carrier — normalized through core's alias map ("Hapag" -> "Hapag-Lloyd").
    # Routed through _carrier_from_prose, NOT a raw whole-body token scan: this
    # body has no grid, so OL's standing footer is the only other place a
    # carrier name appears, and a bare scan reads "MSC" straight out of
    # "Maersk, Sealand, MSC, ONE, CMA and Cosco do not accept Dummy SI".
    car = _carrier_from_prose(text)
    if car:
        out["carrier_quoted"] = _canon_carrier(car)
    # Lane -> POL/POD.
    pol, pod = _prose_lane(text)
    if pol:
        out["pol"] = pol
    if pod:
        out["pod"] = pod
    # Container size ("40' Reefer", "6x40'RF", "2 x 20'DV").
    cs = re.search(r"\b(?:\d{1,2}\s*[xX]\s*)?\d{2}[\'’]?\s*"
                   r"(?:HC\s*Reefer|Reefer|RF|HC|DV|GP|ST|FR|OT)\b", text)
    if cs:
        out["container_size"] = re.sub(r"\s+", " ", cs.group(0)).strip()
    # Free time ("4 equipment free days at Origin", "3 ... at destination").
    oft = re.search(r"(\d{1,2})\s*(?:equipment\s+|calendar\s+|business\s+)?"
                    r"(?:free\s+days?|days?\s+free)\s+(?:at\s+)?origin",
                    text, re.IGNORECASE)
    if oft:
        out["origin_free_time"] = f"{oft.group(1)} days"
    dft = re.search(r"(\d{1,2})\s*(?:equipment\s+|calendar\s+|business\s+)?"
                    r"(?:free\s+days?|days?\s+free)\s+(?:at\s+)?(?:destination|dest)\b",
                    text, re.IGNORECASE)
    if dft:
        out["dest_free_time"] = f"{dft.group(1)} days"
    # Transshipment — "Direct service" -> Direct, else "via <PORT>".
    if re.search(r"\bdirect\s+(?:service|sailing|routing)\b", text, re.IGNORECASE):
        out["transshipment"] = "Direct"
    else:
        ts = re.search(r"\b(?:via|t/?s|transship(?:ment)?)\s*[:\-]?\s*"
                       r"([A-Z][A-Za-z .\-]{2,24})", text)
        if ts:
            out["transshipment"] = ts.group(1).strip()
    # Offered ETD/ETA via the anchor-based prose date parsers.
    etd = parse_etd_offered(text)
    if etd:
        out["etd_offered"] = etd
    eta = parse_eta_offered(text)
    if eta:
        out["eta_offered"] = eta
    return out


def parse_rate_table(text: str) -> dict:
    """Extract the quote from an OL rate reply.

    HEADER-TO-CELL ALIGNMENT ONLY. When OL's grid is present, every field is
    read out of the cell sitting under its own header, and the body prose is
    never consulted for carrier, vessel or rate — see the invariant note above
    _TABLE_CELL_ALIASES for the boilerplate this makes unreachable.

    When there is NO grid at all, the prose path runs instead: OL does send
    some quotes as free prose (2026-06-24 Houston->Busan Hapag), and returning
    nothing there loses a real quote.

    Emits, all optional and all absent when OL left the column blank:
      carrier_quoted, ol_rate, pol, pod, container_size, dthc,
      vessel, voyage, vessel_voyage, transshipment,
      etd_offered, eta_offered, erd, origin_cutoff, doc_cutoff, port_cutoff,
      origin_free_time, dest_free_time

    Plus, only when OL offered a choice:
      rate_options    — every option row, in OL's order, each the same shape
      other_lane_pods — PODs of rows dropped for quoting a different lane

    rate_expiry is deliberately NOT emitted here: it lives in the body prose,
    not the grid, and fetch_bodies composes it from parse_rate_expiry at the
    call site. Keeping the table parser pure is the point.
    """
    block = _find_table_block(text or "")
    if not block:
        return parse_prose_rate(text or "")
    options, dropped = _table_options(block, text or "")
    if not options:
        return parse_prose_rate(text or "")
    out = _pick_headline(options)
    # EVERY option OL wrote, in the order OL wrote them, so nothing is
    # discarded silently and the report can show Michael the choice he was
    # actually offered. Present only when there IS a choice — a one-row grid
    # keeps exactly its old shape, keys and all.
    if len(options) > 1:
        out["rate_options"] = [dict(o) for o in options]
    if dropped:
        out["other_lane_pods"] = dropped
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
