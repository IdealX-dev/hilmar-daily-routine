#!/usr/bin/env python3
"""
Hilmar Tracker — ingest.py

Reads staged email metadata from scripts/stage_emails.jsonl and produces
tracking-data-v2.json.

MODEL (per Michael 2026-04-20):
  - Lonny outbound "Oakland to X" = 1 rate_request (PENDING until won/lost)
  - Each unique HILMAR MDOLX booking (from MBD_OceanExportBookingShared inbound
    or Lonny's own send-reply threads) = 1 WIN
  - Wins link back to a request by (destination, time window). Unmatched MDOLX
    wins are counted as standalone bookings (prior-window rollovers).
  - Rates desk emails (Caren/MBD_Export_Pricing) are EXCLUDED (ops-prep noise).

ol_responder is always the MBD shared mailbox identity — never an individual.
"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import body_parser as BP  # subject + body parsing (Plan A, Day 1)
import core as C  # pure functions: parse_iso, parse_teu, decide_status, request_id, etc.


# 2026-05-06: stage files renamed to .txt so SharePoint indexes them
# (M365 MCP cannot search-and-fetch .jsonl extension). Same JSON-Lines
# content; only the file extension changes. Falls back to legacy .jsonl
# names if the .txt files don't exist yet.
def _resolve_stage(name_no_ext: str) -> Path:
    here = Path(__file__).resolve().parent
    new = here / f"{name_no_ext}.txt"
    legacy = here / f"{name_no_ext}.jsonl"
    return new if new.exists() or not legacy.exists() else legacy
STAGE_PATH = _resolve_stage("stage_emails")
BODIES_PATH = _resolve_stage("stage_emails_bodies")
OUT_PATH_DEFAULT = Path(__file__).resolve().parent.parent / "tracking-data-v2.json"

# Operator-corrections file — authoritative human overrides applied AFTER all
# automatic classification, on every ingest, so a verdict survives re-ingest.
CORRECTIONS_PATH = Path(__file__).resolve().parent / "operator_corrections.json"

OL_RESPONDER_NAME = "MBD Ocean Export Booking"   # shared mailbox identity
OL_RESPONDER_EMAIL = "MBD_OceanExportBookingShared@ol-usa.com"

# Origin-general (was hardcoded "oakland" until 2026-06-11 — the Dalhart
# blind spot): any known Hilmar origin site, single source in body_parser.
DEST_RX = re.compile(
    rf"^\s*(?:{'|'.join(re.escape(o) for o in BP.KNOWN_ORIGINS)})(?:,?\s*[A-Z]{{2}})?"
    rf"\s+to\s+(.+?)(?:\s*\(\d+\)\s*)?\s*$",
    re.IGNORECASE)
MDOLX_RX = re.compile(r"MDOLX\s*(\d{6,})", re.IGNORECASE)


def load_bodies_index() -> dict[str, dict]:
    """Load stage_emails_bodies.jsonl into {imid: body_record}. Empty if file missing."""
    if not BODIES_PATH.exists():
        return {}
    out: dict[str, dict] = {}
    # Explicit utf-8 — on Windows the default is cp1252 and chokes on UTF-8 chars
    # that show up in OL/Lonny bodies (en-dash, smart quotes, accented names).
    with open(BODIES_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            imid = rec.get("imid")
            if imid:
                out[imid] = rec
    return out


# _etd_fit_days was removed 2026-08-21. It parsed with datetime.fromisoformat
# against fields that hold OL's raw cell text ("30-Sep-26"), so it returned
# None on every table-parsed quote — dead in production while looking live —
# and it compared whatever two dates it was handed regardless of leg. Both
# jobs now belong to core.requested_fit_days, which refuses to cross an
# arrival ask with a departure offer. One implementation, not two.


# ─────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────

def load_stage() -> list[dict]:
    if not STAGE_PATH.exists():
        raise FileNotFoundError(f"Stage not found: {STAGE_PATH}")
    rows = []
    with open(STAGE_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def clean_destination(subject: str) -> str | None:
    """Extract destination from subject line.

    Prefer the BP subject-lane parser (handles all origins + paren suffixes).
    Fall back to the narrow DEST_RX (Oakland→X only) for legacy safety.
    """
    if not subject:
        return None
    # Full BP parse first — handles Hilmar/SLC/Chicago/Dalhart/Oakland + (North)/(South)
    _, dest = BP.parse_subject_lane(subject)
    if dest:
        return dest
    # Legacy fallback
    s = re.sub(r"^\s*(re|fw|fwd):\s*", "", subject, flags=re.IGNORECASE)
    s = re.sub(r"\s*\((\d+)\)\s*$", "", s)
    m = DEST_RX.match(s)
    return m.group(1).strip() if m else None


def clean_origin(subject: str, default: str = "Oakland") -> str:
    """Extract origin from subject via BP parser; default to Oakland (Lonny's primary)."""
    if not subject:
        return default
    origin, _ = BP.parse_subject_lane(subject)
    return origin or default


def _computed_date_range(rows) -> dict:
    """The actual span of the rows being written, as {"start", "end"}.

    Dates are the ET calendar dates the rest of the system buckets by
    (core.et_date_of), so the window agrees with the day tiles rather than
    describing a different clock. Falls back to today for an empty build —
    a file with no rows still has a window, it is just an empty one.
    """
    dates = sorted(
        d for d in (
            C.et_date_of(r.get("request_timestamp")) or r.get("request_date")
            for r in (rows or [])
        ) if d
    )
    if not dates:
        today = C.et_date_of(C.now_utc())
        return {"start": today, "end": today}
    return {"start": dates[0], "end": dates[-1]}


def _pick_best_request(pool, bk_ts, bk_carrier, bk_ccount):
    """Pick the best-evidenced request for a booking from `pool` → (row, score).

    DETERMINISTIC BY CONSTRUCTION — this is the whole point. Candidates are
    filtered to asks Lonny actually sent BEFORE the booking and within 14
    days, scored on the booking subject's own evidence (container count +
    carrier), and ties break to the LATEST request before the booking.
    Nothing depends on the order the pool arrived in.

    Until 2026-07-27 the header-chain branch had no scoring at all: the first
    row encountered whose imid appeared in In-Reply-To/References won
    outright. When Lonny REUSED a thread that made STAGE-FILE ORDER decide the
    outcome — a new, still-unanswered 1x20'DV RFQ could be stamped WIN with a
    2x40'HC booking, vanishing from PENDING OL while the genuinely quoted row
    stayed open. Proved by running the same inputs in both orders.

    Returns (None, 0) when no candidate qualifies. Callers then fall through
    to the lane scan or emit a standalone — this never refuses outright,
    because an unmatched booking still has to land somewhere visible.
    """
    scored = []
    for r in pool:
        if r.get("mdolx_ref"):           # already matched a win
            continue
        req_ts = C.parse_iso(r.get("request_timestamp"))
        # An ask Lonny sent AFTER the booking cannot be what the booking
        # fulfils. This single guard is what stops a brand-new RFQ from
        # swallowing an older move's booking.
        if not req_ts or req_ts > bk_ts:
            continue
        if (bk_ts - req_ts) > timedelta(days=14):
            continue
        score = 0
        r_carrier = r.get("carrier_quoted")
        if r_carrier:
            r_carrier = C.normalize_carrier(r_carrier) or r_carrier
        if bk_carrier and r_carrier and bk_carrier == r_carrier:
            score += 2
        if bk_ccount and r.get("container_count") == bk_ccount:
            score += 2
        # The booking subject NAMES its container count. A candidate that
        # contradicts it is the wrong ask — penalise so a same-thread
        # 1x20'DV can never outrank a 2x40'HC booking's real 2x40'HC request.
        elif bk_ccount and r.get("container_count") not in (None, 0):
            score -= 2
        scored.append((score, req_ts, r))
    if not scored:
        return None, 0
    scored.sort(key=lambda t: (t[0], t[1]), reverse=True)
    return scored[0][2], scored[0][0]


def canonical_lane_key(destination: str | None) -> str:
    """Lane-matching key, alias-collapsed via core.canonical_port_key.

    Was bare `.strip().lower()` until 2026-07-26, which made "HCMC" and
    "Cat Lai" different lanes — exactly how OL's booking confirmation failed
    to link to Lonny's RFQ, fabricating a `stand_<mdolx>` WIN beside the real
    request row. Both sides of every destination comparison go through here,
    so they cannot disagree about what counts as the same place.
    """
    return C.canonical_port_key(destination)


def title_case_destination(destination: str | None) -> str:
    """Normalize destination casing so 'Hcmc' and 'HCMC' both render as 'HCMC'.

    Rules:
      - All-uppercase 3-letter codes preserved (HCMC, OAK, JFK)
      - Mixed-case acronyms with parens uppercased before paren ('Hcmc (Cat Lai)' → 'HCMC (Cat Lai)')
      - Otherwise Title Case
    """
    if not destination or destination == "Unknown":
        return destination or "Unknown"
    s = destination.strip()
    # A UN/LOCODE is not a casing problem — it is a different NAME for the
    # port, so it resolves before any casing rule runs. This function
    # Title-Cases any head longer than 4 characters, which is the SECOND
    # independent producer of the fake port "Jpyok" (body_parser._norm is the
    # first): a subject that reached here as "JPYOK" was renamed here even
    # when the parser had not touched it. Table-gated in core.resolve_locode,
    # so a 5-letter real port ("BUSAN", "GENOA") is unaffected.
    _loc = C.resolve_locode(s)
    if _loc:
        return _loc
    # Split off any "(Foo Bar)" suffix
    m = re.match(r"^([A-Za-z]+)(\s*\(.+\))?$", s)
    if m:
        head, tail = m.group(1), m.group(2) or ""
        head = head.upper() if len(head) <= 4 and head.isalpha() else head.title()
        return (head + tail).strip()
    return s.title()


# Subjects that look like rate requests but are actually operational
# follow-ups on existing bookings — they should NOT seed new request rows
# or standalone wins. Patterns are case-insensitive substrings.
_OPERATIONAL_SUBJECT_HINTS = (
    "FREE-TIME ISSUE", "FREE TIME ISSUE",
    "NEED TO SCHEDULE LOADING APPT", "LOADING APPT",
    # "LOAD APPT" is a SEPARATE string, not covered by "LOADING APPT" —
    # "LOADING" is not a prefix of "LOAD ". OL writes both, and the gap let
    # MDOLX260928 through as a WIN: "MDOLX260928_LOAD APPTS // 00+080435//
    # 2X40'RF HILMAR -> OAKLAND". Michael, 2026-08-13: "MDOLX260928 IS NOT ON
    # THE TRANSACTION REPORT I SENT YOU FOR HILMAR" — it is a NUMIDIA booking
    # (customer NUMIDIA BV-LZ, Oakland to Penang), and the "HILMAR" in it is
    # the town the boxes were trucked from.
    "LOAD APPT",
    "DEMURRAGE CHARGES MOUNTING",
    "BOOKING SCHEDULE INCONSISTENCY",
    "DISPUTE EBKG", "DISPUTE NAM",
    "PORT DISPUTE",
    "REEFER FREE TIME",        # Lonny status email, not a rate ask
    "ORIGIN FREE TIME",        # free-time policy note (no destination), not a lane RFQ — 2026-06-30 QC-057
    "UPDATED 20' AND 40' RATE",  # general rate update, no specific lane
    "CMA UPDATES",             # Michael internal
    "NRA AMENDMENT", "CONFIRMATION OF NRA",
    "INVOICE QUERY", "INVOICE DISPUTE",
    # 2026-09-05 (QC-069, Sentry HILMAR-DAILY-TRACKER-N): an INVOICE is a
    # document about a booking that already exists — it is never the booking.
    # "MDOLX261031_ EXPORT INVOICE AVAILABLE // HILMAR 1X40'RF Oakland to
    # Yokohama" passed this gate, _booking_rank admits any non-confirmation
    # subject at tier 0, and with the real confirmation not in the stage the
    # invoice became "the booking" — dated 08-27, twenty-four days after the
    # 08-03 booking it invoices. That date let an 08-26 ask pass the
    # req_ts <= bk_ts guard and take a win it never had (the 4c pair). Two
    # literal phrases, per this list's convention: "ORIGIN EXPORT DEMURRAGE
    # INVOICE" still names its lane and container spec and stays a ranked
    # tier-0 fallback (tests/test_booking_email_choice.py pins that ranking).
    "EXPORT INVOICE", "INVOICE AVAILABLE",
    "TRANSPORT ORDER",         # ops follow-up tag, not a rate ask
    # 2026-05-07: DRAFT RATED is a quote draft, not a confirmed booking.
    # Without an accompanying NEW BOOKING CONFIRMATION email there's no
    # carrier/lane signal — these inflated WIN count and broke QC-002.
    # Fired by stand_260469 ('Re: MDOLX260469_DRAFT RATED FOR HILMAR' +
    # body 'Move updated' = no booking ever confirmed). If a real booking
    # follows, the second email IS a NEW BOOKING CONFIRMATION and that
    # one creates the standalone WIN — DRAFT RATED never should.
    "DRAFT RATED",
)


def is_operational_subject(subject: str | None) -> bool:
    """True if subject looks like an ops/admin email rather than a rate ask
    or new booking. Used to drop noise rows that were inflating the win count
    (Issue surfaced in 2026-04-30 audit: 7 'Unknown' destinations + standalone
    wins for MDOLX260062/260357/260388 which are existing bookings)."""
    if not subject:
        return False
    up = subject.upper()
    return any(h in up for h in _OPERATIONAL_SUBJECT_HINTS)


# Per Michael 2026-05-20, confirmed by Linda Echevarria's 2026-05-19 audit of
# the v11 dashboard: the intake must count ONLY genuine Hilmar ocean-freight
# RFQs. Several classes of email slipped in that are NOT that, and each
# inflated the win / loss / not-quoted counts:
#   numidia  — Hilmar is the SUPPLIER on that move, not our client. A subject
#              like "MDOLX260558 ... // NUMIDIA ... HILMAR -> ACAJUTLA" carries
#              both tokens, so a HILMAR-required filter alone let it through.
#   trucking — domestic road freight (FTL / LTL), not an ocean booking, e.g.
#              "FTL Modesto CA 95357 to Sturgis MI 49091".
#   recalled — the sender recalled the message (Outlook "Recall: ..." prefix);
#              the request was withdrawn and must not seed a row.
# Any staged row matching one of these is dropped from ingest entirely.
# out_of_scope_reason() is the single source of truth — the qc_selfheal
# PHASE 3 backstop imports and reuses it so the two can never drift (QC-040).
_NUMIDIA_RX  = re.compile(r"numidia", re.IGNORECASE)
# AgriDairy — another customer shipping product FROM the Hilmar plant, so
# "HILMAR" appears in its bookings as the ORIGIN/supplier reference and the
# standalone-WIN gate ("HILMAR in subject") wrongly claims them (the live
# stand_260821 leak, Michael 2026-07-01: "only moves booked by Lonny are
# Hilmar the client"). Same class as Numidia: Hilmar-as-supplier, different
# paying customer.
_AGRIDAIRY_RX = re.compile(r"agri[\s\-_]?dairy", re.IGNORECASE)
_TRUCKING_RX = re.compile(r"\bFTL\b|\bLTL\b|truck\s?load|trucking", re.IGNORECASE)
_RECALL_RX   = re.compile(r"\brecall:", re.IGNORECASE)
# Other MBD customers seen in this mailbox, added 2026-08-10 with Michael's
# "i want full tightening". Same class as Numidia and Agri Dairy: MBD ships
# for them too, so their mail lands in the same ocean-export group inbox, and
# any of them could load out of the Hilmar plant and put "HILMAR" in a subject.
# Sourced from senders observed in the live stage during the 2026-08-10
# mailbox scan, NOT invented:
#     d.passarelli@hoogwegtus.com        HOOGWEGT
#     VGesualdo@ernolaszlo.com           ERNO LASZLO
#     claza@brisar.com                   BRISAR
# Word-bounded so they cannot fire on a substring of a port or carrier name.
# NOT added: "Solis" (a report-hub feed, not a booking counterparty) and
# tts-worldwide / Quality Forms (internal commission mail, no MDOLX).
# `la[sz]{1,2}lo` because BOTH spellings are live: the domain is
# ernolaszlo.com and the report-hub subject says ERNO-LAZLO-SHIPMENT-REPORT.
# A pattern matching only one of them is a rule that fires half the time.
_OTHER_CLIENT_RX = re.compile(
    r"\bhoogwegt\w*\b|\berno[\s\-_]?la[sz]{1,2}lo\b|\bbrisar\b", re.IGNORECASE)

# ── the Hilmar client signal ────────────────────────────────────────────────
#
# "HILMAR" is BOTH the customer tag and the origin city — Hilmar Ingredients is
# physically in Hilmar, California — so `"HILMAR" in subject` cannot tell
# "// HILMAR" (our customer) from "Hilmar, CA to La Guaira" (someone else's
# cargo loading at the same plant). That ambiguity is the whole reason
# stand_260821 leaked in July.
#
# The fix is NOT to demand a tag: a genuine Hilmar move can describe the lane
# and never name the customer, and requiring a tag would drop it. Instead,
# classify WHICH kind of mention it is, and hold origin-city-only mentions to a
# higher bar (see hilmar_admits_row).
_HILMAR_TOKEN_RX = re.compile(r"HILMAR", re.IGNORECASE)
#: What follows a HILMAR that is being used as a PLACE, not a customer:
#: "Hilmar, CA", "Hilmar CA", "Hilmar, California".
_HILMAR_AS_CITY_RX = re.compile(r"\s*,?\s*(?:CA\b|CALIF)", re.IGNORECASE)


def hilmar_signal(text: str | None) -> str | None:
    """How this text mentions HILMAR: 'tag' | 'origin_city' | None.

    'tag'          at least one mention that is NOT followed by a state — a
                   customer marker ("// HILMAR", "HILMAR - Oakland to Osaka").
    'origin_city'  every mention is followed by CA/California — the town.
    None           the word does not appear.

    Any single tag-shaped mention wins: a subject that says both
    "Hilmar, CA to Osaka // HILMAR" is our customer's, unambiguously.
    """
    if not text:
        return None
    saw_city = False
    for m in _HILMAR_TOKEN_RX.finditer(text):
        if _HILMAR_AS_CITY_RX.match(text, m.end()):
            saw_city = True
        else:
            return "tag"
    return "origin_city" if saw_city else None


def out_of_scope_mdolx(rows: list[dict]) -> dict[str, str]:
    """{mdolx: reason} for every MDOLX any of whose messages is out of scope.

    THREAD-LEVEL, which per-row filtering cannot be. out_of_scope_reason runs
    on one message at a time (ingest main, ~line 1713), so a thread whose
    booking-confirmation subjects read "Hilmar, CA to La Guaira" survives on
    its own merits while a SIBLING message in the same thread says "Agri Dairy
    Vendor Reference PO00-26002163". Today that only gets caught when the
    sibling's text is quoted into a fetched body; where no body was fetched,
    nothing connects the two.

    An MDOLX is one shipment (Michael: "1 MDOLX = 1 win"), so it has exactly
    one paying customer. If any message carrying that number names another
    customer, the number is theirs — every message of it, body or no body.
    """
    out: dict[str, str] = {}
    for r in rows:
        reason = out_of_scope_reason(r)
        if not reason:
            continue
        for field in ("subject", "summary_preview", "text_body"):
            mdolx = extract_mdolx(str(r.get(field) or ""))
            if mdolx:
                out.setdefault(mdolx, reason)
                break
    return out


def out_of_scope_reason(row: dict) -> str | None:
    """Why this staged email is NOT a Hilmar ocean RFQ — or None if it is.

    Returns 'numidia' | 'agridairy' | 'trucking' | 'recalled' | None. Rows
    with a reason are dropped from ingest entirely (no request, booking, or
    win) and are the same set the qc_selfheal PHASE 3 backstop purges from
    tracking-data-v2.
    """
    subject = str(row.get("subject") or "")
    preview = str(row.get("summary_preview") or "")
    body = str(row.get("text_body") or "")
    # Numidia — Hilmar-as-supplier. Per Michael, check subject AND body.
    if _NUMIDIA_RX.search(subject) or _NUMIDIA_RX.search(body) or _NUMIDIA_RX.search(preview):
        return "numidia"
    # AgriDairy — Hilmar-as-supplier for another customer (see _AGRIDAIRY_RX
    # note). Subject AND body, same as Numidia.
    if _AGRIDAIRY_RX.search(subject) or _AGRIDAIRY_RX.search(body) or _AGRIDAIRY_RX.search(preview):
        return "agridairy"
    # Other MBD customers (Hoogwegt / Erno Laszlo / Brisar). Same rule and same
    # three fields as Numidia and Agri Dairy — added 2026-08-10 under "full
    # tightening" so the list matches the customers actually in this mailbox
    # rather than only the two that happened to leak.
    if (_OTHER_CLIENT_RX.search(subject) or _OTHER_CLIENT_RX.search(body)
            or _OTHER_CLIENT_RX.search(preview)):
        return "other_client"
    # Trucking — the FTL/LTL request type is declared in the subject line.
    if _TRUCKING_RX.search(subject) or _TRUCKING_RX.search(preview):
        return "trucking"
    # Recalled — Outlook message-recall prefixes the subject with "Recall: ".
    if _RECALL_RX.search(subject):
        return "recalled"
    return None


def extract_mdolx(text: str | None) -> str | None:
    if not text:
        return None
    m = MDOLX_RX.search(text)
    return m.group(1) if m else None


def guess_teu_from_preview(preview: str | None) -> tuple[int, int, str | None]:
    """Parse preview like '1-20' Oakland' or '2-40' HC Reefer' into (count, teu, canonical_str)."""
    if not preview:
        return 0, 0, None
    # Strip CAUTION banner before parsing — Outlook prepends "CAUTION: THIS EMAIL ..."
    # to external sender messages, which leaks into summary_preview and breaks
    # parse_teu (it returns 0,0 because there's no container pattern in the banner).
    # Same regex as body_parser._CAUTION_BANNER_RX. 2026-04-30 — Apr 29 Nagoya dupe fix.
    preview = re.sub(
        r"CAUTION:\s*THIS EMAIL ORIGINATED FROM OUTSIDE OF OUR COMPANY\.?"
        r"(?:\s*DO NOT CLICK LINKS OR OPEN ANY ATTACHMENTS UNLESS YOU RECOGNIZE THE SENDER AND KNOW THE CONTENT IS SAFE\.?)?",
        "", preview, flags=re.IGNORECASE,
    ).strip()
    count, teu = C.parse_teu(preview)
    # Canonical container string for display
    m = re.search(r"(\d+)\s*[-x×]\s*(\d{2})[\'\u2019]?\s*(HC|RF|DV|GP|FR|OT|HC\s*Reefer|Reefer|Flex)?",
                  preview, re.IGNORECASE)
    canonical = None
    if m:
        qty, size, equip = m.group(1), m.group(2), (m.group(3) or "").strip()
        equip_norm = equip.upper().replace("  ", " ") if equip else ""
        equip_norm = "HC" if equip_norm in ("HC",) else \
                     "HC Reefer" if "REEF" in equip_norm else \
                     "Flex" if "FLEX" in equip_norm else equip_norm
        canonical = f"{qty}-{size}'{' ' + equip_norm if equip_norm else ''}".strip()
    return count, teu, canonical


# Hilmar origins that ARE the ocean load port (so POL = origin). Inland
# origins (Dalhart, Hilmar CA, Chicago, Tulare, ...) load via a gateway
# seaport we only know from the OL rate table, so we DON'T guess POL for them.
_SEAPORT_ORIGINS = {
    "oakland", "los angeles", "long beach", "seattle", "tacoma",
    "portland", "houston", "lax",
}


def _derive_ports(origin: str | None, destination: str | None) -> tuple[str | None, str | None]:
    """Baseline POL/POD from the lane endpoints (2026-06-17, QC-027 fix).

    POD = destination — the overseas discharge port for every lane, always.
    POL = origin ONLY when the origin is itself a seaport; inland origins
    leave POL empty for the OL rate table to fill. This is the intake
    baseline; OL's stated pol/pod override it at the rate-response step.
    Not fabrication — for this client the lane endpoints ARE the ports.
    """
    pod = (destination or "").strip() or None
    o = (origin or "").strip()
    # Strip a trailing US state code ("Oakland, CA" / "Dalhart TX") — require a
    # comma OR whitespace before an UPPERCASE 2-letter code so we don't chop the
    # last two letters off a plain city name ("Oakland" -> "Oakla").
    base = re.sub(r"(?:,\s*|\s+)[A-Z]{2}$", "", o).strip().lower() if o else ""
    pol = o if base in _SEAPORT_ORIGINS else None
    return pol, pod


# ─────────────────────────────────────────────────────────────────────
# Build requests from Lonny outbound
# ─────────────────────────────────────────────────────────────────────

def build_requests(lonny_out: list[dict]) -> list[dict]:
    """One rate_request per Lonny outbound email. All start as PENDING.

    When a body is available for this imid (row["body_parsed"] populated by
    main), pull in eta_requested / origin / destination overrides. Subject is
    still authoritative for lane; body eta_requested fills the 100%-blank gap.
    """
    requests = []
    skipped_ops = 0
    for row in lonny_out:
        sent = row.get("sent")
        subject = row.get("subject", "")
        preview = row.get("summary_preview", "")
        parsed = row.get("body_parsed") or {}

        # Drop ops/admin emails — they aren't rate asks and inflate the row count
        # with "Unknown" destinations. 2026-04-30 audit fix.
        if is_operational_subject(subject):
            skipped_ops += 1
            continue

        origin = clean_origin(subject)
        destination = clean_destination(subject) or parsed.get("destination")
        # Skip rows with no parseable destination at all — these were always
        # noise (subject didn't follow "Origin to Dest" pattern).
        if not destination:
            skipped_ops += 1
            continue
        destination = title_case_destination(destination)
        count, teu, containers = guess_teu_from_preview(preview)
        _pol, _pod = _derive_ports(origin, destination)

        eta_requested = parsed.get("eta_requested")
        conv_id = row.get("conversation_id")  # attached by main() if body fetched

        rid = C.request_id(
            conv_id=row.get("imid"),     # using internetMessageId as proxy
            request_ts=sent,
            destination=destination,
        )
        sent_dt = C.parse_iso(sent)
        requests.append({
            "request_id": rid,
            "status": "PENDING",
            "origin": origin,
            "destination": destination or "Unknown",
            "lane": f"{origin} → {destination or 'Unknown'}",
            "request_timestamp": sent,
            # ET, NOT UTC. Every day bucket in the system is an ET business
            # day (core.report_business_day), so a UTC calendar date silently
            # shifts late-Pacific requests forward a day: an RFQ sent Friday
            # 5:30 PM PT is 2026-07-25 in UTC but Friday 2026-07-24 in ET, and
            # since no fire ever reports a Saturday it appeared in NO day's
            # New Requests, KPI tile or day reconciliation — on any day, ever,
            # while still counting in the period totals. Proved 2026-07-26.
            "request_date": C.to_et(sent_dt).date().isoformat() if sent_dt else None,
            "lonny_time_pt": C.fmt_pt(sent_dt) if sent_dt else None,
            "subject": subject,
            "pol": _pol,
            "pod": _pod,
            "containers": containers or preview or None,
            "container_count": count,
            "teu_requested": teu,
            "ol_responder": OL_RESPONDER_NAME,
            "ol_responder_email": OL_RESPONDER_EMAIL,
            "ol_responder_signer": None,       # filled by parse_signer when body present (Phase 2 backfill)
            "quoted": False,
            "carrier_quoted": None,
            "carrier_won": None,
            "ol_rate": None,
            "response_timestamp": None,
            "olusa_time_et": None,
            "turnaround_biz_hours": None,
            "turnaround_hours": None,
            "has_send": False,
            "mdolx_ref": None,
            "mdolx_refs_all": [],              # multiple MDOLX possible per request
            "etd_requested": parsed.get("etd_requested"),  # 2026-05-19 parser-gap fix
            "etd_offered": None,
            "etd_fit_days": None,
            "eta_requested": eta_requested,
            "eta_offered": None,
            "vessel_voyage": None,
            "transshipment": None,
            "conversation_id": conv_id,
            "loss_reason": None,
            "reason_detail": "Staged — pending match to response/booking",
            "status_history": [],
            "source_imids": [row.get("imid")],
            "source_ids": [row.get("id")],
            # 2026-05-19 parser-gap fix (Michael "no field should be empty ever") —
            # Lonny-side fields extracted from RFQ body. Some are None when Lonny's
            # RFQ template doesn't include them (e.g. temperature only on reefer).
            "product":         parsed.get("product"),
            "temperature":     parsed.get("temperature"),
            "requested_dates": parsed.get("requested_dates"),
            "lonny_notes":     parsed.get("lonny_notes"),
            "free_time_requested": parsed.get("free_time_requested"),
        })
    return _merge_thread_dupes(requests)


# The stagers TRUNCATE Outlook's bodyPreview before it ever reaches ingest:
# refresh_stage.py slices [:300], the hilmar-tree stager [:200]. A preview whose
# length lands exactly on one of those caps was cut off, so anything after the
# cut — including the container line — is simply not visible to us.
_PREVIEW_TRUNCATION_LENS = (200, 300)


def _preview_was_truncated(text) -> bool:
    """True when this preview was cut off by a stager, so its CONTENT cannot
    be used as evidence that something is ABSENT.

    Deliberately biased toward True. The two ways to be wrong are not
    symmetric: calling a complete preview "truncated" costs one duplicate row,
    which is visible in the report and which QC's dupe checks catch. Calling a
    truncated preview "complete" silently DELETES a real RFQ — it never
    appears in intake, never gets chased, and nobody can see that it is gone.
    """
    s = (text or "").strip()
    return len(s) in _PREVIEW_TRUNCATION_LENS


def _merge_thread_dupes(requests: list[dict]) -> list[dict]:
    """Collapse multi-message Lonny outbound dupes within the same conversation.

    Trigger: same conversation_id + same canonical destination + sent within
    10 minutes of each other. Lonny sometimes sends a "I need two identical
    bookings" header email then a second email with the actual container line —
    Outlook gives them different imids → request_id() returns two distinct rows.

    Strategy: keep the row with NON-ZERO teu_requested as the "primary"; merge
    source_imids/ids from the secondary; drop the secondary. If both have teu>0
    we leave them alone (probably truly distinct rate asks within same thread).
    Tracked by Issue #5 in HANDOFF-TO-CODE-2026-04-30.md.
    """
    if not requests:
        return requests
    # Bucket by (conv_id, destination_lc, calendar_date)
    bucket: dict[tuple[str, str, str], list[int]] = {}
    for i, r in enumerate(requests):
        cid = (r.get("conversation_id") or "").strip()
        dest = (r.get("destination") or "").strip().lower()
        d = r.get("request_date") or ""
        if not cid or not dest or not d:
            continue
        bucket.setdefault((cid, dest, d), []).append(i)

    drop: set[int] = set()
    for _key, idxs in bucket.items():
        if len(idxs) < 2:
            continue
        # Sort by request_timestamp
        idxs.sort(key=lambda i: requests[i].get("request_timestamp") or "")
        # Walk pairs and merge if within 10 min and one has teu=0
        for a, b in zip(idxs, idxs[1:], strict=False):
            if a in drop or b in drop:
                continue
            ra, rb = requests[a], requests[b]
            ts_a = C.parse_iso(ra.get("request_timestamp"))
            ts_b = C.parse_iso(rb.get("request_timestamp"))
            if not ts_a or not ts_b:
                continue
            if (ts_b - ts_a) > timedelta(minutes=10):
                continue
            ta, tb = ra.get("teu_requested") or 0, rb.get("teu_requested") or 0
            # One is "thin" (header-only) and the other has containers — merge
            if (ta == 0) ^ (tb == 0):
                primary, secondary = (ra, rb) if tb == 0 else (rb, ra)
                sec_idx = b if tb == 0 else a
                # teu==0 IS NOT PROOF THE EMAIL HAD NO CONTAINERS.
                #
                # teu_requested comes from guess_teu_from_preview(), which
                # reads ONLY summary_preview — and the stagers cut that at 300
                # chars (refresh_stage.py:548). Lonny's RFQs open with routing
                # and dates, so on a longer ask the equipment line falls PAST
                # the cut. The row then looks "thin" while being a completely
                # ordinary second RFQ, and this branch DELETED it: a real rate
                # request that never reached intake, never got chased, never
                # counted in any total, and left no trace but a merge_note on
                # a different row.
                #
                # When nothing parsed, the row's `containers` field holds that
                # raw preview, so we can still tell whether we were looking at
                # the whole email. If it was truncated, we have no evidence of
                # absence — keep BOTH rows. A spurious duplicate is visible and
                # QC catches it; a silently deleted RFQ is neither.
                _sec_preview = secondary.get("containers")
                if _preview_was_truncated(_sec_preview):
                    primary.setdefault("merge_notes", []).append(
                        f"Declined to merge sibling "
                        f"imid={(secondary.get('source_imids') or ['?'])[0][:30]} "
                        f"— its preview was truncated at "
                        f"{len((_sec_preview or '').strip())} chars, so teu=0 "
                        f"is a parse gap, not evidence of a header-only email")
                    continue
                primary["source_imids"] = list({*(primary.get("source_imids") or []),
                                                 *(secondary.get("source_imids") or [])})
                primary["source_ids"] = list({*(primary.get("source_ids") or []),
                                                *(secondary.get("source_ids") or [])})
                # Use the EARLIEST timestamp (Lonny's first contact) to preserve
                # accurate turnaround math — preserves "real" ask time.
                if (C.parse_iso(secondary.get("request_timestamp")) or ts_b) < (
                    C.parse_iso(primary.get("request_timestamp")) or ts_a
                ):
                    primary["request_timestamp"] = secondary["request_timestamp"]
                    primary["request_date"] = secondary["request_date"]
                    primary["lonny_time_pt"] = secondary["lonny_time_pt"]
                primary.setdefault("merge_notes", []).append(
                    f"Merged thin sibling imid={(secondary.get('source_imids') or ['?'])[0][:30]} "
                    f"sent={secondary.get('request_timestamp')}"
                )
                drop.add(sec_idx)
    return [r for i, r in enumerate(requests) if i not in drop]


# ─────────────────────────────────────────────────────────────────────
# Collect MDOLX bookings (wins) from all three buckets
# ─────────────────────────────────────────────────────────────────────

#: A booking confirmation announces the booking; everything else in the thread
#: merely mentions it. Ranked because the row's lane, carrier, containers and
#: TEU are all parsed from the SUBJECT of whichever email we pick — see the
#: stand_260769 note in collect_bookings for what picking wrong costs.
_NEW_CONFIRMATION_RX = re.compile(r"NEW\s+BOOKING\s+CONFIRMATION", re.IGNORECASE)
_ANY_CONFIRMATION_RX = re.compile(r"BOOKING\s+CONFIRMATION", re.IGNORECASE)


def _booking_rank(subject: str | None, sent: str | None) -> tuple[int, str]:
    """Sort key for "which email best represents this booking".

    Higher is better. Ties break on EARLIEST sent, so the original creation
    still beats a later revision of equal rank — hence the negated timestamp
    via a descending string compare in the caller's `>` test.

      2  NEW BOOKING CONFIRMATION — the creation event
      1  any other BOOKING CONFIRMATION (REVISED / SSL CHANGED / UPDATED ETA)
      0  anything else in the thread (ops asks, invoices, status chasers)
    """
    s = subject or ""
    if _NEW_CONFIRMATION_RX.search(s):
        tier = 2
    elif _ANY_CONFIRMATION_RX.search(s):
        tier = 1
    else:
        tier = 0
    # Later strings sort higher, so invert the timestamp to make EARLIER win.
    inverted = "".join(chr(0x10FFFF - ord(c)) if ord(c) < 0x10FFFF else c
                       for c in (sent or "￿"))
    return (tier, inverted)


def collect_bookings(rows: list[dict], excluded_mdolx: dict[str, str] | None = None,
                     log_excluded=None) -> dict[str, dict]:
    """
    Return {mdolx: booking_dict}. Each booking represents a unique MDOLX
    shipment confirmation for Hilmar (1 MDOLX = 1 win, per Michael).

    `excluded_mdolx` is out_of_scope_mdolx(rows) — the thread-level verdict.
    Passing None keeps the pre-2026-08-10 per-row behaviour, which is what the
    blast-radius diagnostic uses to compare old against new.

    `log_excluded(mdolx, reason, subject)` is called for every booking the
    thread-level rule drops. NEVER let a tightening be silent: the failure mode
    of a stricter client gate is a real win that quietly stops existing, and a
    number that goes down with no line explaining why is indistinguishable
    from the pipeline breaking.
    """
    bookings: dict[str, dict] = {}
    excluded_mdolx = excluded_mdolx or {}

    for row in rows:
        bucket = row.get("bucket")
        subject = row.get("subject", "") or ""
        preview = row.get("summary_preview", "") or ""

        # Only consider rows where HILMAR appears in subject. The previous
        # implementation also accepted "NUMIDIA" alone, but NUMIDIA is OL's
        # internal client tag used across MANY customers, not just Hilmar —
        # accepting it as a Hilmar signal pulled non-Hilmar bookings into
        # our data.
        #
        # Per Michael 2026-05-17 ("your qc and parsers have to improve"):
        # 3 NUMIDIA-only standalone WINs (stand_260491 Tulare→Port Klang,
        # stand_260482 Oakland→Busan, stand_260555 no-lane) were leaking
        # in as Hilmar. The fix requires HILMAR explicitly somewhere in
        # the subject — either as the customer tag "// HILMAR" or the
        # lane origin "Hilmar, CA". A row that says only "// NUMIDIA"
        # without "HILMAR" anywhere is a different customer's booking.
        #
        # 2026-08-10, Michael: "i want full tightening." The substring test
        # became a CLASSIFIER — see hilmar_signal. It still admits an
        # origin-city-only subject, because a genuine Hilmar move can name the
        # lane and never the customer and demanding a tag would drop it; what
        # changed is that such a row must also survive the thread-level check
        # below. A "// HILMAR" tag needs no corroboration.
        # THE SUBJECT IS NOT THE ONLY PLACE OL NAMES THE CUSTOMER (2026-08-12).
        #
        # Michael sent Linda Echevarria's two example bookings. The July one
        # reads "MDOLX260963_NEW BOOKING CONFIRMATION// HILMAR 2X20'DV Oakland
        # to HCMC (Cat Lai)"; the August one reads "Oakland to Manila (North) /
        # MDOLX261070 / ONE BKG # RICGAZ641400" — same desk, same customer, no
        # HILMAR tag. OL changed the convention. Measured on both .eml files:
        # subject has HILMAR False / body has HILMAR True (7 occurrences,
        # "HILMAR INGREDIENTS").
        #
        # This gate read the SUBJECT ONLY, so every new-format booking was
        # dropped here — 15 of the 35 bookings in OL's own Jun-Aug recap
        # (MDOLX261025 through 261072) never reached the tracker. diag_find
        # proved the mail was staged with its body fetched and the ref still
        # absent from tracking-data, which is what separates this from the
        # delivery question it was mistaken for.
        #
        # The body is available here (attach_bodies runs before this in main)
        # and it is where the booking names its shipper. A body mention is
        # treated as the WEAKER signal deliberately: only a SUBJECT tag
        # overrides the thread-level exclusion below, exactly as before, so a
        # forwarded digest that merely mentions Hilmar cannot claim another
        # customer's MDOLX.
        is_hilmar = row.get("is_hilmar")
        subject_signal = hilmar_signal(subject)
        body_signal = None
        if subject_signal is None:
            body_signal = hilmar_signal(row.get("text_body"))
        signal = subject_signal or body_signal
        if is_hilmar is None:
            is_hilmar = signal is not None
        if not is_hilmar:
            # Push the rejection to Sentry as a metric so we can track
            # mis-routed standalone WINs and improve over time.
            try:
                import sys as _sys
                _sys.path.insert(0, str(Path(__file__).resolve().parent))
                import sentry_setup as _sentry
                _sentry.metric_increment(
                    "ingest.non_hilmar_filtered", 1,
                    bucket=bucket or "unknown",
                )
            except Exception:
                pass
            continue

        # Skip ops/admin follow-ups so they don't generate fake standalone wins
        # for MDOLX numbers that already represent live bookings (2026-04-30 audit
        # fix — was creating standalone wins for MDOLX260062 FREE-TIME ISSUE,
        # MDOLX260357/260388 LOADING APPT, etc.).
        if is_operational_subject(subject):
            continue

        # Try mdolx field first, else parse from subject/preview
        mdolx = row.get("mdolx") or extract_mdolx(subject) or extract_mdolx(preview)
        if not mdolx:
            continue

        # THREAD-LEVEL EXCLUSION. One MDOLX is one shipment, so it has exactly
        # one paying customer: if any message carrying this number named a
        # different one, this number is theirs. Per-row filtering cannot see
        # that — it only catches the sibling when the sibling's text happens to
        # be quoted into a fetched body, which is how stand_260821 (Agri Dairy,
        # subject "Hilmar, CA to La Guaira") leaked in July.
        #
        # A "// HILMAR" TAG OVERRIDES IT. Hilmar and another customer can share
        # a thread — same plant, same week — and an explicit customer tag is
        # not something to discard on a sibling's say-so. Only the ambiguous
        # origin-city-only rows defer to the thread.
        # subject_signal, not signal: a body mention is corroborating evidence,
        # never an override. Only an explicit customer tag in the SUBJECT is
        # strong enough to outrank a sibling naming another customer.
        if mdolx in excluded_mdolx and subject_signal != "tag":
            if log_excluded:
                log_excluded(mdolx, excluded_mdolx[mdolx], subject)
            continue

        sent = row.get("sent")
        # Carry forward any body-parsed signer so link_bookings_to_requests can
        # populate ol_responder_signer on the matched/standalone request.
        body_parsed = row.get("body_parsed") or {}
        # BEST hit wins, then earliest. Not earliest alone.
        #
        # 2026-08-10. stand_260769 carried teu_won=0 on a 3X40'RF and
        # etd=22-Apr-26 / eta=26-May-26 on a booking confirmed 16 June —
        # sailing dates two months BEFORE the booking existed. The thread:
        #
        #   17:14:37  MDOLX260769_ *NEED UPDATE TO BOOKING # NAM8482648 // HILMAR
        #   17:17:34  MDOLX260769_ *NEW BOOKING CONFIRMATION // HILMAR -
        #             Oakland to Osaka - 3X40'RF // CMA BKG # NAM8482648
        #
        # Earliest-wins took the 17:14 email — OL asking CMA to CHANGE a
        # booking, body "Can you please update this booking per below: -Reduce
        # to 3 x 40'RF". Its subject has no lane and no container spec, so
        # every field the row derives from the subject came out empty or
        # wrong, and the real confirmation three minutes later was discarded.
        #
        # The MDOLX set is unchanged by this — only WHICH email represents
        # each booking. A confirmation outranks an ops message; among
        # confirmations, earliest still wins, so a genuine creation is never
        # displaced by a later revision.
        existing = bookings.get(mdolx)
        if not existing or _booking_rank(subject, sent) > _booking_rank(
                existing.get("subject"), existing.get("sent")):
            bookings[mdolx] = {
                "mdolx": mdolx,
                "subject": subject,
                "sent": sent,
                "preview": preview,
                "source_bucket": bucket,
                "source_imid": row.get("imid"),
                "source_id": row.get("id"),
                "body_signer": body_parsed.get("ol_responder_signer"),
                # 2026-05-19 parser-gap fix: carry forward the full parsed
                # dict so link_bookings_to_requests + standalones can populate
                # erd / origin_free_time / dest_free_time / rate_expiry /
                # product / temperature from the booking confirmation body.
                "body_parsed": body_parsed,
                # 2026-05-19 PM: thread-header metadata for booking-link
                # matching (per Michael "you have to parse the booking team
                # emails for matches based on header meta data").
                "in_reply_to": row.get("in_reply_to"),
                "references": row.get("references") or [],
            }

    return bookings


# ─────────────────────────────────────────────────────────────────────
# Link bookings → requests
# ─────────────────────────────────────────────────────────────────────

def _corrected_ref_owners() -> dict[str, str]:
    """{mdolx_ref: request_id} for every `set` correction that names a booking.

    Read from the SAME file, with the SAME predicate, as apply_operator_
    corrections and claim_corrected_mdolx_refs (non-exclude entry, `set.mdolx_
    ref` present) — one source, no re-spelling, so the matcher, the applier
    and the backstop cannot disagree about which row a ref belongs to. When
    two corrections name one ref the FIRST wins, which is the order the claim
    resolves that conflict in (and reports it). A missing or unreadable file
    consults nothing, exactly as the applier applies nothing.
    """
    if not CORRECTIONS_PATH.exists():
        return {}
    try:
        doc = json.loads(CORRECTIONS_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"WARN: operator_corrections.json unreadable — the matcher "
              f"consults no corrections ({e})")
        return {}
    owners: dict[str, str] = {}
    for corr in doc.get("corrections", []):
        if corr.get("exclude"):
            continue
        ref = str((corr.get("set") or {}).get("mdolx_ref") or "").strip()
        rid = corr.get("request_id")
        if ref and rid:
            owners.setdefault(ref, rid)
    return owners


def link_bookings_to_requests(requests: list[dict], bookings: dict[str, dict]) -> tuple[list[dict], list[dict]]:
    """
    Match each booking to the most-recent Lonny outbound request with the same
    destination (case-insensitive), request_ts <= booking_ts, within 10 days.
    Unmatched bookings become standalone wins (prior-window rollovers).

    Returns (updated_requests, standalone_wins_as_requests).

    THE OPERATOR'S VERDICT IS READ BEFORE ANY SCORE (2026-09-05, Sentry
    HILMAR-DAILY-TRACKER-N — QC-069 duplicate_mdolx, 250 events, the same
    eleven refs every fire since 2026-08-14). This function and
    apply_operator_corrections are the two writers of `mdolx_ref`, and until
    now only the applier read operator_corrections.json. For the eight
    near-identical CMA CGM 1x40'RF Oakland→Yokohama bookings of Aug 3-5 every
    candidate here tied, the tiebreak ("latest ask first") is a PERMUTATION of
    the operator's mapping, and once the lane ran out of scoreable asks the
    rest were emitted as `stand_<ref>` WIN rows with their own teu_won. The
    applier then wrote the operator's ref onto the row the correction names
    and cleared nothing off this function's row — one shipment on two rows,
    TEU double counted, re-derived identically on every fire.

    claim_corrected_mdolx_refs (2026-09-03) restores the invariant AFTER both
    writers ran and stays wired as the backstop for carried-forward prior
    state. But a heal that retracts a link cannot un-write the link's
    stamps: the released 4c row still carried has_send / quoted / carrier_won
    / booking_timestamp / olusa_time_et / product / temperature /
    reason_detail and a PENDING→WIN history entry — CLAUDE.md's "nothing
    un-stamps a bad value", applied to the heal itself. So the invariant is
    held HERE, at the writer: a booking whose ref a correction names is
    linked to the operator's row, before scoring, and no rival row and no
    stand_ row is ever stamped with it. A named ref whose row is not in
    `requests` falls through to ordinary matching — a narrow consult must
    never drop a booking (QC-082 already reports the dangling correction).
    """
    # Index requests by destination (lane key)
    by_lane: dict[str, list[dict]] = defaultdict(list)
    for r in requests:
        by_lane[canonical_lane_key(r.get("destination"))].append(r)
    for lane_reqs in by_lane.values():
        lane_reqs.sort(key=lambda r: r.get("request_timestamp") or "")
    by_id = {r.get("request_id"): r for r in requests if r.get("request_id")}
    ref_owners = _corrected_ref_owners()

    matched_mdolx: set[str] = set()

    def _operator_owner(mdolx) -> dict | None:
        rid = ref_owners.get(str(mdolx).strip())
        return by_id.get(rid) if rid else None

    # NAMED REFS FIRST. A row the operator has reserved for one booking must
    # not be consumed by the scorer on behalf of an UNNAMED booking that
    # happens to tie on the same lane — linking the named ones first puts a
    # ref on those rows, and _pick_best_request's "already matched" skip then
    # keeps them out of every later pool. Stable: stage order is preserved
    # within each half, so nothing else about the outcome moves.
    _ordered = sorted(bookings.items(),
                      key=lambda kv: 0 if _operator_owner(kv[0]) is not None else 1)

    for mdolx, bk in _ordered:
        best = _operator_owner(mdolx)
        best_via = "operator_correction" if best is not None else "lane+time"
        bk_ts = C.parse_iso(bk.get("sent"))
        if not bk_ts and best is None:
            continue

        # Figure out destination from booking subject via BP parser (handles
        # all origin variants + paren suffixes). Fall back to legacy regex
        # only if BP returns nothing.
        raw_subj = bk.get("subject", "") or ""
        _, dest_guess = BP.parse_subject_lane(raw_subj)
        if not dest_guess:
            subj = raw_subj.upper()
            m = re.search(r"HILMAR\s*[\->]+\s*([A-Z][A-Z\s]+?)(?:\s*//|$)", subj)
            if m:
                dest_guess = m.group(1).strip().title()
            if not dest_guess:
                m = re.search(r"\bTO\s+([A-Z][A-Za-z\s()]+?)(?:\s*//|\s*\d|$)", subj)
                if m:
                    dest_guess = m.group(1).strip().title()
        subj = raw_subj.upper()  # kept for the substring scan below

        # Find candidate lane(s)
        candidates: list[dict] = []
        if dest_guess:
            candidates = by_lane.get(canonical_lane_key(dest_guess), [])

        # If no exact lane hit, scan all lanes where dest substring matches the subject
        if not candidates:
            for lane_key, lane_reqs in by_lane.items():
                if lane_key != "unknown" and lane_key.upper() in subj:
                    candidates.extend(lane_reqs)

        # 2026-05-19 PM (Michael "you have to parse the booking team emails
        # for matches based on header meta data and you'll find them"):
        # HIGHEST-CONFIDENCE MATCH — the booking's In-Reply-To / References
        # headers point to the imid of the prior message in the thread. If
        # any unmatched Lonny RFQ's imid is in that chain, it's THE match.
        # Falls back to lane+time when the headers aren't populated (older
        # stage records pre-2026-05-19, or when the booking is a new thread
        # with no References).
        bk_in_reply_to = (bk.get("in_reply_to") or "").strip()
        bk_references = bk.get("references") or []
        bk_chain: set[str] = set()
        if bk_in_reply_to:
            bk_chain.add(bk_in_reply_to.strip("<>"))
        for ref in bk_references:
            if ref:
                bk_chain.add(ref.strip("<>"))

        # The booking subject's own evidence — container count and carrier —
        # scores EVERY candidate set below, chain-matched or lane-matched.
        bk_carrier = BP.parse_subject_carrier(bk.get("subject"))
        if bk_carrier:
            bk_carrier = C.normalize_carrier(bk_carrier) or bk_carrier
        _ccm = re.search(r"(\d+)\s*[xX]\s*\d{2}", raw_subj)
        bk_ccount = int(_ccm.group(1)) if _ccm else None

        if best is None and bk_chain:
            # The header chain is a strong signal, but it is a FILTER, not a
            # decision. Until 2026-07-27 the first row encountered whose imid
            # appeared anywhere in In-Reply-To/References won outright — so
            # when Lonny REUSED a thread, the outcome was decided by whatever
            # order the stage file happened to hold the rows in.
            #
            # Proved: thread with RFQ_old (2x40'HC, 07-20, quoted) and a NEW
            # unanswered RFQ (1x20'DV, 07-22). OL books the OLD move; its
            # References carry both imids. Stage holds NEW first -> the
            # booking lands on req_new; stage holds OLD first -> it lands on
            # req_old. Same inputs, same day, opposite business outcome. The
            # new request is stamped WIN with a booking for equipment it never
            # asked for and vanishes from PENDING OL, while the genuinely
            # quoted row sits open. That is the operator's 2026-07-22
            # Oakland->HCMC report.
            #
            # Now: every chain member is a candidate, and the SAME evidence
            # scoring that governs the lane fallback picks between them.
            chain_pool = [
                r for r in requests
                if any(s and s.strip("<>") in bk_chain
                       for s in (r.get("source_imids") or []))
            ]
            best, _chain_score = _pick_best_request(
                chain_pool, bk_ts, bk_carrier, bk_ccount)
            if best:
                best_via = "in_reply_to/references"
                if len(chain_pool) > 1:
                    # Never silently. A reused thread means a human may need
                    # to confirm which ask this booking settles.
                    best_via += f" (chose 1 of {len(chain_pool)} in-thread by evidence)"

        # Fallback: score unmatched RFQs on the lane within 14d, same rules.
        if not best:
            best, _lane_score = _pick_best_request(
                candidates, bk_ts, bk_carrier, bk_ccount)
            if best and _lane_score > 0:
                best_via = "lane+container+carrier"

        if best:
            best["status"] = "WIN"
            best["has_send"] = True
            best["mdolx_ref"] = mdolx
            best["mdolx_refs_all"] = sorted(set(best.get("mdolx_refs_all", []) + [mdolx]))
            best["_booking_match_via"] = best_via  # observability — was: 'in_reply_to/references' | 'lane+time'
            # carrier_won prefers carrier_quoted (from rate-response body) → falls
            # back to the booking subject (e.g. "// MSC: EBKG..." trailer or
            # NAM-prefix booking-ref). Last resort: leave None for QC.
            carrier_won = best.get("carrier_quoted")
            if not carrier_won:
                carrier_won = BP.parse_subject_carrier(bk.get("subject"))
            if carrier_won:
                carrier_won = C.normalize_carrier(carrier_won) or carrier_won
            best["carrier_won"] = carrier_won
            best["booking_timestamp"] = bk.get("sent")
            # If the booking body produced a signer (mbd_inbound bucket) and
            # the request didn't already have one, propagate it.
            if bk.get("body_signer") and not best.get("ol_responder_signer"):
                best["ol_responder_signer"] = bk.get("body_signer")
            # 2026-05-19 parser-gap fix: booking confirmations carry ERD +
            # free-time + rate-expiry + product + temperature. Only set when
            # the request side didn't already have these from the rate
            # response (which is more authoritative for rate-side fields).
            bp = bk.get("body_parsed") or {}
            if not best.get("erd") and bp.get("erd"):
                best["erd"] = bp["erd"]
            if not best.get("origin_free_time") and bp.get("origin_free_time"):
                best["origin_free_time"] = bp["origin_free_time"]
            if not best.get("dest_free_time") and bp.get("dest_free_time"):
                best["dest_free_time"] = bp["dest_free_time"]
            if not best.get("rate_expiry") and bp.get("rate_expiry"):
                best["rate_expiry"] = bp["rate_expiry"]
            if not best.get("product") and bp.get("product"):
                best["product"] = bp["product"]
            if not best.get("temperature") and bp.get("temperature"):
                best["temperature"] = bp["temperature"]

            # ONLY set response fields if we never captured a quote.
            # Preserve rate-response timestamp (true OL responsiveness) when present.
            #
            # 2026-05-19 PM bug fix (Michael "on turnaround report.. i think
            # you have errors.. as no way is something 171 hours"): when a
            # booking confirmation arrives WITHOUT a prior rate-response
            # email, the old code set response_timestamp = booking_timestamp
            # AND computed turnaround_biz_hours from it. That measured
            # "Lonny RFQ → Booking Confirmation" (the FULL negotiation
            # cycle, often 7-11 days) instead of "Lonny RFQ → OL rate
            # response" (the chase metric, should be hours). Visible as
            # the 85.78h / 73.34h / 55.58h rows on Apr 17 bookings.
            #
            # Fix: in the no-prior-quote branch, record `booking_timestamp`
            # (separate schema field) for chronology but LEAVE turnaround
            # fields unset. A row with `turnaround_biz_hours = None` means
            # "no rate-response timing data" — which is the truth, not an
            # 80h "response time".
            if not best.get("quoted") or not best.get("response_timestamp"):
                best["quoted"] = True
                # Use booking_timestamp (separate field) to preserve chronology;
                # do NOT pollute response_timestamp with the booking time.
                # response_timestamp stays None to signal "we never saw a
                # rate response — booking arrived directly".
                best["booking_timestamp"] = bk.get("sent")
                # turnaround_biz_hours / turnaround_hours STAY None. They
                # represent "Lonny RFQ → OL rate response" which never
                # happened in this code path.
                resp_dt = C.parse_iso(bk.get("sent"))
                if resp_dt and not best.get("olusa_time_et"):
                    # olusa_time_et is just the display label; safe to set
                    # so the dashboard's "OL Response" column isn't blank.
                    best["olusa_time_et"] = C.fmt_et(resp_dt)

            prior_detail = best.get("reason_detail") or ""
            prior_tag = prior_detail.split(" | ")[0] if "Rate responded" in prior_detail else ""
            best["reason_detail"] = (
                f"{prior_tag} | Booked MDOLX{mdolx} ({bk.get('source_bucket')})".strip(" |")
                if prior_tag else f"Linked to MDOLX{mdolx} booking ({bk.get('source_bucket')})"
            )
            best["teu_won"] = best.get("teu_requested", 0)
            best.setdefault("status_history", []).append({
                "at": bk.get("sent"),
                "from": "PENDING",
                "to": "WIN",
                "reason": f"MDOLX{mdolx} booking confirmed",
            })
            matched_mdolx.add(mdolx)

    # Unmatched bookings → standalone win rows.
    # Use BP subject-lane parser to resolve origin/destination instead of the
    # old "Unknown (prior window)" label. This kills the 21.7% "Unknown" dest
    # rate Michael flagged.
    standalones: list[dict] = []
    for mdolx, bk in bookings.items():
        if mdolx in matched_mdolx:
            continue
        bk_ts_iso = bk.get("sent")
        bk_ts = C.parse_iso(bk_ts_iso)
        raw_subj = bk.get("subject", "") or ""
        # Body parse comes up FRONT (not just for erd/product below): it also
        # feeds the destination fallback — the 2026-07-09 client email showed
        # a "Lane unresolved" standalone whose BODY had parsed fine (product/
        # temp visible) while the subject carried no lane shape.
        s_bp = bk.get("body_parsed") or {}
        s_origin, s_dest = BP.parse_subject_lane(raw_subj)
        if not s_dest:
            # Subject gave nothing — the booking body usually names the
            # discharge port (destination, else POD). Same data the row
            # already displays for product/temperature.
            s_dest = s_bp.get("destination") or s_bp.get("pod")
        # All Hilmar shipments load at Oakland regardless of cargo source city.
        # Lonny's outbound rate-request model is "Oakland to X" everywhere
        # (per orchestrator.md). NUMIDIA-routed booking confirmations encode
        # the cargo source as "Hilmar, CA" or "Hilmar" in the subject — the
        # parser correctly picks that up but for report-consistency we
        # normalize to the port-of-loading. Caught 2026-05-05 by Michael's
        # screenshot of rows 39/40/41/43/44 showing "Hilmar →" labels.
        if s_origin and s_origin.lower() in ("hilmar", "hilmar, ca"):
            s_origin = "Oakland"
        s_origin = s_origin or "Oakland"    # sensible fallback (Lonny default)
        # "Port Penang" / "Port Ho Chi Minh" are sloppy NUMIDIA aliases for
        # destinations the Lonny side already tracks under their canonical
        # short names ("Penang", "HCMC"). Strip the Port- prefix when the
        # tail matches a known canonical destination — keeps the per-lane
        # rollup undisplaced. Don't touch "Port Klang" (that IS the canonical
        # port name).
        if s_dest:
            normalized = re.sub(r"^\s*Port\s+(?=Penang|Ho Chi Minh|Jakarta)\b", "", s_dest, flags=re.IGNORECASE)
            if normalized != s_dest:
                s_dest = normalized.strip()
        # THE THIRD SPELLING, CLOSED. Every other destination writer in this
        # file goes through title_case_destination; this standalone-booking
        # path did not, so whatever body_parser put in `destination` or `pod`
        # was stored verbatim — including a raw table-cell "JPYOK", which is
        # neither the parser's "Jpyok" nor the map's "Yokohama". Routing it
        # through the same normalizer as every other row is what makes ONE
        # spelling structural rather than a coincidence, and it applies the
        # LOCODE table here for free.
        s_dest = title_case_destination(s_dest) if s_dest else s_dest
        s_dest = s_dest or "Unknown"
        # A destination that resolves to the SAME port as the origin is a
        # parse failure, not a shipment. It happens on re-forwarded or
        # return-leg confirmations whose subject names only one port
        # ("HILMAR 1X40'HC to Oakland"): s_dest picks up "Oakland" and
        # s_origin defaults to "Oakland", producing a degenerate
        # "Oakland → Oakland" lane that then appears in Lane Performance as a
        # real trade lane. Michael's reported defect #3. Alias-aware, so
        # "Oakland → OAK" is caught too. Treat it as unresolved and let QC-015
        # / QC-073 surface it rather than inventing a lane.
        if s_dest != "Unknown" and C.canonical_port_key(s_dest) == C.canonical_port_key(s_origin):
            s_dest = "Unknown"
        lane = f"{s_origin} → {s_dest}" if s_dest != "Unknown" else "Lane unresolved"
        # Standalone wins have no rate-response body to mine for carrier_quoted —
        # the only signal is the MDOLX subject ("// MSC: EBKG..."). 2026-04-30
        # carrier_won = 6/97 fix (Issue #3 in HANDOFF-TO-CODE-2026-04-30.md).
        s_carrier = BP.parse_subject_carrier(raw_subj)
        if s_carrier:
            s_carrier = C.normalize_carrier(s_carrier) or s_carrier
        # Extract container counts from the MDOLX confirmation subject.
        # Format examples that work: "HILMAR 2X40'RF Oakland to Yokohama",
        # "HILMAR 1x20'DV Oakland to HCMC (Cat Lai)", "1X40'Flex". Caught
        # 2026-05-05 — booking-confirmation wins were rendering with empty
        # cargo + 0 TEU columns because we never parsed the subject.
        s_containers = BP.parse_subject_containers(raw_subj)
        s_count, s_teu = C.parse_teu(s_containers) if s_containers else (0, 0)
        # (s_bp — the booking body's parse — is loaded above, where it also
        # feeds the destination fallback.)
        standalones.append({
            "request_id": f"stand_{mdolx}",
            "status": "WIN",
            "origin": s_origin,
            "destination": s_dest,
            "lane": lane,
            "request_timestamp": None,
            # ET for the same reason as the request row above — one clock.
            "request_date": C.to_et(bk_ts).date().isoformat() if bk_ts else None,
            "lonny_time_pt": None,
            "subject": bk.get("subject"),
            "pol": s_bp.get("pol") or _derive_ports(s_origin, s_dest)[0],
            "pod": s_bp.get("pod") or _derive_ports(s_origin, s_dest)[1],
            "containers": s_containers,
            "container_count": s_count,
            "teu_requested": s_teu,
            "teu_won": s_teu,
            "ol_responder": OL_RESPONDER_NAME,
            "ol_responder_email": OL_RESPONDER_EMAIL,
            "ol_responder_signer": bk.get("body_signer"),
            "quoted": True,
            "has_send": True,
            "mdolx_ref": mdolx,
            "mdolx_refs_all": [mdolx],
            "carrier_quoted": s_carrier,
            "carrier_won": s_carrier,
            # response_timestamp stays None — the SAME rule the matched path
            # spells out 100 lines up ("we never saw a rate response, the
            # booking arrived directly"). Writing the booking time here made
            # the standalone row claim OL sent a rate quote at a moment it
            # only sent a booking confirmation, which is the 171-hour
            # turnaround defect fixed on the matched path in 2026-05-19 and
            # left live on this one. booking_timestamp carries the chronology.
            "response_timestamp": None,
            "booking_timestamp": bk.get("sent"),
            "olusa_time_et": C.fmt_et(bk_ts) if bk_ts else None,
            "loss_reason": None,
            "reason_detail": f"Standalone booking (pre-window request) — MDOLX{mdolx}, no Lonny ask found in 30-day window",
            # A real transition entry so a NEW standalone WIN surfaces in the
            # daily email's STATUS CHANGES section — its correct home. (The
            # daily New-Requests / OL-Responses tables exclude stand_* rows:
            # a booking confirmation is neither a Lonny ask nor a rate quote.)
            "status_history": [{
                "at": bk_ts_iso,
                "from": "PENDING",
                "to": "WIN",
                "reason": f"MDOLX{mdolx} standalone booking confirmation",
            }],
            "source_imids": [bk.get("source_imid")],
            "source_ids": [bk.get("source_id")],
            # 2026-05-19 parser-gap fix: surface booking-body fields on the
            # standalone WIN row so the audit/dashboard show real values.
            "erd":              s_bp.get("erd"),
            "origin_free_time": s_bp.get("origin_free_time"),
            "dest_free_time":   s_bp.get("dest_free_time"),
            "rate_expiry":      s_bp.get("rate_expiry"),
            "product":          s_bp.get("product"),
            "temperature":      s_bp.get("temperature"),
        })
    return requests, standalones


# ─────────────────────────────────────────────────────────────────────
# Apply MBD_OceanExportBookingShared rate responses  ("RE: Oakland to X")
# ─────────────────────────────────────────────────────────────────────

def counts_as_rate_response(row: dict) -> bool:
    """Stage-time bucketing was origin-locked to Oakland until 2026-06-11,
    so every Dalhart-lane quote from the MBD shared mailbox was stamped
    mbd_inbound and its RFQ surfaced as Not Quoted. Re-derive here: an
    mbd_* bucket implies sender = the MBD shared mailbox (refresh_stage
    only assigns those buckets to that sender), so bucket + the
    origin-general lane subject is sufficient — already-staged history is
    honored without a stage-file migration."""
    bucket = row.get("bucket")
    if bucket == "mbd_rate_response":
        return True
    if bucket != "mbd_inbound":
        return False
    return bool(BP.RATE_RESPONSE_SUBJECT_RX.match(row.get("subject") or ""))


def apply_rate_responses(requests: list[dict], rate_rsps: list[dict],
                         trace=None) -> int:
    """
    For each rate-response email, match it back to the most-recent Lonny outbound
    request with the same destination (case-insensitive), request_ts <= response_ts,
    within 10 days. Flip quoted=True and populate carrier/rate/ETD/turnaround fields
    so decide_status() can split LOSS into quoted_lost vs not_quoted.

    Precondition: call BEFORE link_bookings_to_requests — if a booking lands on the
    same request it will overwrite the status to WIN, which is the correct outcome.

    Returns the number of requests that were quoted.

    `trace` (2026-08-12, Michael: "ol responded to everything") — optional
    callback(rr, outcome, detail_dict) invoked at every point this function
    decides a rate response's fate. Production passes nothing; scripts/
    diag_matching.py passes a collector so a diagnostic can report WHY each
    reply failed to attach WITHOUT modelling the matcher a second time. The
    repo has been burned before by a diagnostic that reimplemented pipeline
    logic in a different order and confidently answered questions about a
    system that does not exist; observing the real function is the fix.
    """
    def _t(rr, outcome, **detail):
        if trace is not None:
            trace(rr, outcome, detail)

    quoted_count = 0
    by_lane: dict[str, list[dict]] = defaultdict(list)
    for r in requests:
        by_lane[canonical_lane_key(r.get("destination"))].append(r)
    for lane_reqs in by_lane.values():
        lane_reqs.sort(key=lambda r: r.get("request_timestamp") or "")

    # Sort rate responses earliest-first so the first (fastest) quote wins the match
    rate_rsps_sorted = sorted(rate_rsps, key=lambda rr: rr.get("sent") or "")

    for rr in rate_rsps_sorted:
        dest = rr.get("destination") or clean_destination(rr.get("subject", ""))
        if not dest:
            _t(rr, "no_destination", subject=rr.get("subject"),
               row_destination=rr.get("destination"))
            continue
        sent = rr.get("sent")
        sent_dt = C.parse_iso(sent)
        if not sent_dt:
            _t(rr, "no_send_time", subject=rr.get("subject"), sent=rr.get("sent"))
            continue

        # Primary: exact canonical match.
        candidates = by_lane.get(canonical_lane_key(dest), [])
        # Fallback: still widen, but on PORT IDENTITY rather than a bare
        # substring test. `dest_canon in k or k in dest_canon` pooled Manila
        # (North) with Manila (South) — one is a substring of neither, but both
        # canonicalise to "manila" — so a reply on a thread titled "RE: Oakland
        # to Manila" could write ol_rate / carrier_quoted / etd_offered /
        # vessel_voyage onto the WRONG terminal's row. The client then saw a
        # South-terminal rate reported as the North lane's quote, while the
        # correct request stayed unquoted and aged out as NQ.
        # core.same_port requires terminal equality when BOTH sides name one.
        if not candidates:
            for k, rs in by_lane.items():
                if k == "unknown":
                    continue
                candidates.extend(r for r in rs
                                  if C.same_port(dest, r.get("destination")))
        else:
            # Even an exact key hit can span terminals now that
            # canonical_port_key collapses "Manila (North)"/"Manila (South)"
            # to one key. Narrow to the compatible ones — UNCONDITIONALLY.
            # An earlier version kept the unfiltered list when nothing was
            # compatible, which defeated the whole check: a Manila (South)
            # reply still landed on the Manila (North) row. If no candidate
            # is compatible then there is no match, and leaving the row
            # unquoted is the correct outcome — a wrong rate on the client's
            # quote is worse than a missing one.
            _pre_narrow = len(candidates)
            candidates = [r for r in candidates
                          if C.same_port(dest, r.get("destination"))]
            if _pre_narrow and not candidates:
                _t(rr, "terminal_filter_emptied", dest=dest,
                   subject=rr.get("subject"), pre_narrow=_pre_narrow,
                   lane_key=canonical_lane_key(dest))

        # 2026-05-19 PM (Michael "you have to check each email header as
        # often lonny sends same rate requests/routes for the same moves he
        # has regularly"): PREFER conversation_id match. Outlook's
        # conversationId is stable across an entire email thread — the rate
        # response is in the SAME thread as the RFQ Lonny sent. When both
        # sides have a conversation_id, an exact match is the strongest
        # signal we have for "this response belongs to THIS specific RFQ"
        # (handles the case where Lonny sent 3 RFQs for Oakland → Yokohama
        # in a week and OL responded to the most recent one — pure
        # lane+time matching could mis-attribute).
        #
        # Fall back to lane + time-window matching ONLY when:
        #   - rate response has no conversation_id (older fetched bodies)
        #   - no Lonny outbound row in this conversation has a match
        # The fallback uses the same "latest unmatched, within 14d" rule
        # as before.
        rr_conv = rr.get("conversation_id") or ""
        best = None
        best_via = "lane+time"
        if rr_conv:
            # IMPORTANT (2026-05-19 PM 2nd round): in the conversation_id
            # branch pick the LATEST unmatched RFQ in the thread that's
            # BEFORE the response. Lonny re-uses Outlook threads for
            # recurring rate requests (caught when QC-048 still flagged
            # 27 rows post-fix — diagnostic showed rate responses being
            # matched to the FIRST RFQ in a long-running thread, ignoring
            # newer RFQs Lonny sent in the same thread). OL replies to
            # the most recent ask in the conversation, not the original.
            # Constrained to BEFORE the response timestamp so we don't
            # match to a future RFQ.
            for r in candidates:
                if r.get("quoted"):
                    continue
                if r.get("conversation_id") != rr_conv:
                    continue
                req_dt = C.parse_iso(r.get("request_timestamp"))
                if not req_dt or req_dt > sent_dt:
                    continue
                if not best or (C.parse_iso(r.get("request_timestamp") or "") >
                                C.parse_iso(best.get("request_timestamp") or "")):
                    best = r
                    best_via = "conversation_id"
        if not best:
            # Fallback: latest unmatched RFQ before this response, within 14d.
            # (Original logic — window widened to 14d 2026-04-30 for Apr 28
            # Manila/Xingang send-replies whose matching rate response was
            # 12 days prior.)
            for r in candidates:
                if r.get("quoted"):
                    continue
                req_dt = C.parse_iso(r.get("request_timestamp"))
                if not req_dt or req_dt > sent_dt:
                    continue
                if (sent_dt - req_dt) > timedelta(days=14):
                    continue
                if not best or (C.parse_iso(r["request_timestamp"]) >
                                C.parse_iso(best["request_timestamp"])):
                    best = r

        if not best:
            # WHY no candidate survived — the four reasons are mutually
            # exclusive per row, so counting them apportions the failure.
            _why = Counter()
            for r in candidates:
                if r.get("quoted"):
                    _why["already_quoted"] += 1
                    continue
                _rdt = C.parse_iso(r.get("request_timestamp"))
                if not _rdt:
                    _why["request_undated"] += 1
                elif _rdt > sent_dt:
                    _why["ask_after_reply"] += 1
                elif (sent_dt - _rdt) > timedelta(days=14):
                    _why["outside_14d"] += 1
                else:
                    _why["unexplained"] += 1
            _t(rr, "no_candidate_matched", dest=dest, subject=rr.get("subject"),
               sent=sent, candidates=len(candidates), conv=bool(rr_conv),
               reasons=dict(_why))
            continue
        _t(rr, "matched", dest=dest, subject=rr.get("subject"), sent=sent,
           via=best_via, request_id=best.get("request_id"),
           request_timestamp=best.get("request_timestamp"))
        best["_match_via"] = best_via  # for QC + audit observability

        # Prefer body-parsed rate_table (populated when body was fetched);
        # fall back to legacy rr.rate_table for backward-compat.
        parsed = rr.get("body_parsed") or {}
        rt = parsed.get("rate_table") or rr.get("rate_table") or {}
        carrier_norm = C.normalize_carrier(rt.get("carrier_quoted")) if rt.get("carrier_quoted") else None
        req_dt = C.parse_iso(best.get("request_timestamp"))

        best["quoted"] = True
        best["carrier_quoted"] = carrier_norm
        best["ol_rate"] = rt.get("ol_rate")
        best["response_timestamp"] = sent
        best["olusa_time_et"] = C.fmt_et(sent_dt)
        # rt.get("etd_offered") added 2026-08-21 alongside the legacy
        # rt.get("etd"), for the same reason the ETA line below already hedges
        # both: scripts/body_parser.parse_rate_table emits etd_offered and
        # NEVER emits "etd" (that key is the src/hilmar mirror's), so the old
        # term was dead here and every production ETD arrived via `parsed`.
        best["etd_offered"] = (rt.get("etd") or rt.get("etd_offered")
                               or parsed.get("etd_offered"))
        # ETA KEEPS A KNOWN VALUE RATHER THAN BEING NULLED BY A LATER EMAIL.
        #
        # 2026-08-10, tracing QC-027's ETA at 93.3% (307/329, the only field
        # under 95%). This line was unconditional, and Lonny re-uses Outlook
        # threads for recurring rate requests (see the note ~50 lines up), so a
        # SECOND rate response on the same thread whose table carries no ETA
        # overwrote a good one with None. A later quote that states an ETA
        # still wins — `rt`/`parsed` are tried first; the fallback only fires
        # when the new email says nothing at all, where keeping the ETA we
        # already had beats forgetting it.
        #
        # The correct shape was already two lines below, on pol/pod:
        #     best["pol"] = rt.get("pol") or best.get("pol") or _dpol
        # Ports preserved; the table fields did not.
        #
        # rt.get("eta_offered") added alongside the legacy rt.get("eta"):
        # scripts/body_parser.parse_rate_table emits eta_offered and never
        # emits "eta" (that key is the src/hilmar mirror's), so the old term is
        # dead in production. fetch_bodies.py:208 already hedges both — this
        # now matches it instead of relying on `parsed` having bubbled it up.
        best["eta_offered"] = (rt.get("eta") or rt.get("eta_offered")
                               or parsed.get("eta_offered")
                               or best.get("eta_offered"))
        best["vessel_voyage"] = rt.get("vessel_voyage") or parsed.get("vessel_voyage")
        best["transshipment"] = rt.get("transshipment") or parsed.get("transshipment")
        # POL/POD: OL's stated ports win; fall back to the lane-derived
        # baseline set at build time (POD=destination, POL=seaport origin).
        _dpol, _dpod = _derive_ports(best.get("origin"), best.get("destination"))
        best["pol"] = rt.get("pol") or best.get("pol") or _dpol
        best["pod"] = rt.get("pod") or best.get("pod") or _dpod
        # 2026-05-19 parser-gap fix: pull the 4 newly-exposed fields from
        # the rate-table OR parsed.* (parsed bubbles them up in fetch_bodies).
        best["rate_expiry"]       = rt.get("rate_expiry") or parsed.get("rate_expiry")
        best["origin_free_time"]  = rt.get("origin_free_time") or parsed.get("origin_free_time")
        best["dest_free_time"]    = rt.get("dest_free_time") or parsed.get("dest_free_time")
        # ERD goes to both schema names (erd is canonical; origin_cutoff is legacy alias)
        erd_val = rt.get("erd") or parsed.get("erd")
        if erd_val:
            best["erd"] = erd_val
        best["detention_free"] = rt.get("detention_free")
        best["demurrage_free"] = rt.get("demurrage_free")
        # EVERY option OL offered, not just the one that became the headline.
        # 2026-08-21 (Michael "the numbers are wrong for $"): an OL reply
        # routinely carries two sailings on two steamship lines at two prices.
        # ol_rate is now the best of them and the rest were being thrown away
        # at the parser; keeping them on the row is what lets the report say
        # "$675, 1 other option" instead of quietly showing one number.
        # Absent for a single-option quote, so nothing changes for those rows.
        best["rate_options"] = rt.get("rate_options")
        best["other_lane_pods"] = rt.get("other_lane_pods")
        # OL signer: only override if the body produced a real name. parse_signer
        # in core.py is strict-allowlist so any non-None here is a known OL person.
        body_signer = parsed.get("ol_responder_signer")
        if body_signer:
            best["ol_responder_signer"] = body_signer
        # Compute ETD-fit if we now have both eta_requested and eta_offered
        # LIKE FOR LIKE (2026-08-21). Two defects died on this line. The
        # helper it used, _etd_fit_days, parsed with datetime.fromisoformat
        # while production deliberately persists OL's RAW cell text
        # ("30-Sep-26"), so it returned None on every table-parsed quote and
        # this assignment was dead in production. And what it MEANT to do —
        # difference a requested date against eta_offered whatever leg either
        # one was — is the bug that stamped every cutoff RFQ ETD_MISS.
        _fit, _basis = C.requested_fit_days(best)
        best["etd_fit_days"] = _fit
        best["etd_fit_basis"] = _basis
        # Capture conversation_id if fetched
        if rr.get("conversation_id") and not best.get("conversation_id"):
            best["conversation_id"] = rr.get("conversation_id")
        if req_dt:
            best["turnaround_biz_hours"] = C.biz_hours_between(req_dt, sent_dt)
            best["turnaround_hours"] = C.clock_hours_between(req_dt, sent_dt)
        best["reason_detail"] = (
            f"Rate responded by MBD {sent[:10]} — "
            f"{carrier_norm or '?'} @ ${rt.get('ol_rate') or '?'} "
            f"ETD {rt.get('etd') or '?'}"
        )
        best.setdefault("status_history", []).append({
            "at": sent,
            "from": "PENDING",
            "to": "QUOTED",  # logical sub-state; decide_status will finalize WIN/LOSS
            "reason": f"MBD rate response — carrier={carrier_norm}, rate={rt.get('ol_rate')}",
        })
        best.setdefault("source_imids", []).append(rr.get("imid"))
        best.setdefault("source_ids", []).append(rr.get("id"))
        quoted_count += 1

    return quoted_count


# ─────────────────────────────────────────────────────────────────────
# Apply send-signals from Lonny replies
# ─────────────────────────────────────────────────────────────────────

def send_thread_anchors(row: dict) -> set[str]:
    """Every message id this reply is anchored to — its own In-Reply-To and
    References, normalised without angle brackets.

    refresh_stage.build_stage_record stages `in_reply_to` + `references` +
    `conversation_id` on EVERY message (refresh_stage.py:1105-1110), and both
    other matchers already use them: link_bookings_to_requests scores its
    header-chain pool (ingest.py:983-1027) and apply_rate_responses prefers a
    conversation_id hit (ingest.py:1374-1404). apply_send_signals was the only
    one of the three that never looked, which is the whole defect below.
    """
    chain: set[str] = set()
    irt = (row.get("in_reply_to") or "").strip()
    if irt:
        chain.add(irt.strip("<>"))
    for ref in (row.get("references") or []):
        if ref:
            chain.add(str(ref).strip().strip("<>"))
    return chain


def send_reply_is_in_thread(r: dict, chain: set[str], conv_id: str) -> bool:
    """True when request row `r` is part of the thread this Send replies to.

    Two independent proofs, either sufficient:
      - the row's own RFQ imid appears in the reply's In-Reply-To/References
      - the row and the reply carry the same Outlook conversation_id

    conversation_id is deliberately NOT trusted as an identity key on its own
    anywhere else (core.request_id's docstring: Outlook reuses it across
    identical subjects). Here that reuse is harmless — it can only ADD the
    sibling ask to the pool, and the scoring below then picks between them on
    booking evidence, which is exactly what we want it to do.
    """
    if conv_id and (r.get("conversation_id") or "").strip() == conv_id:
        return True
    return any(s and str(s).strip().strip("<>") in chain
               for s in (r.get("source_imids") or []))


def apply_send_signals(requests: list[dict], lonny_replies: list[dict]) -> int:
    """Attribute each of Lonny's "Send" replies to the ONE request it accepts.

    A Send is thread evidence that is spent exactly once. Resolution order:
    the reply's own thread (In-Reply-To / References / conversation_id), then
    the booking that landed at or after it, then recency on the lane — all
    inside the existing terminal-strict lane narrowing. A Send that resolves
    to an already-booked row is CONSUMED there and promotes nothing else.

    Returns the number of PROMOTIONS (a Send consumed by a booking is not one
    — the row was already WIN on harder evidence).
    """
    promotions = 0
    by_lane: dict[str, list[dict]] = defaultdict(list)
    for r in requests:
        by_lane[canonical_lane_key(r.get("destination"))].append(r)
    for lane_reqs in by_lane.values():
        lane_reqs.sort(key=lambda r: r.get("request_timestamp") or "")

    # A row absorbs at most ONE Send per fire. Without this ledger the
    # booking-evidence rule below would hand every Send on the lane to the
    # same booked row; with it, a second genuine Send falls through to the
    # next-best open row, which is the behaviour Lonny's real double-asks need.
    _send_consumed: set[int] = set()

    # CHRONOLOGICAL, not stage-file order. `_pick_best_request` already made
    # booking-linking deterministic by construction for exactly this reason
    # (ingest.py:153-171); a matcher whose outcome depends on the order rows
    # happened to be written in is the same defect wearing a different hat.
    for row in sorted(lonny_replies, key=lambda x: (x.get("sent") or "",
                                                    x.get("imid") or "")):
        # Prefer body-parsed send_signal; fall back to legacy row field.
        parsed = row.get("body_parsed") or {}
        has_signal = parsed.get("send_signal") or row.get("send_signal")
        if not has_signal:
            continue
        dest = clean_destination(row.get("subject", ""))
        if not dest:
            continue
        sent = row.get("sent")
        sent_dt = C.parse_iso(sent)
        if not sent_dt:
            continue
        # Primary canonical match + substring fallback (audit fix 2026-04-30 —
        # was missing send-replies whose subject was "Oakland to HCMC " (trailing
        # space) or "Oakland to HCMC (Cat Lai)" while the original ask used
        # "HCMC (Cai Mep)").
        # SAME terminal narrowing as apply_rate_responses, and for a stronger
        # reason. canonical_lane_key IS canonical_port_key, which deliberately
        # collapses "Manila (North)" and "Manila (South)" onto "manila" — so
        # both terminals' rows arrive in one candidate list, and the loop below
        # tie-breaks purely on the latest request_timestamp. A "Send" reply on
        # the NORTH thread could therefore promote the SOUTH row to WIN,
        # inheriting its carrier_won, while the row Lonny actually confirmed
        # stayed open and aged out as a loss. A wrong WIN is a stronger and
        # more client-facing claim than a wrong rate.
        candidates = by_lane.get(canonical_lane_key(dest), [])
        if not candidates:
            for k, rs in by_lane.items():
                if k == "unknown":
                    continue
                candidates.extend(r for r in rs
                                  if C.same_port(dest, r.get("destination")))
        else:
            # Unconditional, same as apply_rate_responses: if no candidate is
            # terminal-compatible then there is no match, and leaving the row
            # open is the correct outcome.
            candidates = [r for r in candidates
                          if C.same_port(dest, r.get("destination"))]
        # ── WHICH ROW DOES THIS SEND BELONG TO ───────────────────────────
        # A Send is evidence on a THREAD and it is spent ONCE. Until
        # 2026-08-27 this loop asked neither question: it ignored the
        # threading headers the stager puts on every message, and it SKIPPED
        # every row already WIN — so a Send whose own row had just been booked
        # fell through to the next-latest open row on the lane and promoted
        # THAT one instead. The comment 40 lines up already names the outcome
        # in the terminal case ("the row Lonny actually confirmed stayed open
        # and aged out as a loss"); the same cascade fires with no terminals
        # in sight whenever Lonny asks for one move twice.
        #
        # Proved by the Oakland -> Tokyo pair of 2026-08-25/26: one move,
        # asked on two days, quoted at one rate (Wan Hai $2,884), booked once
        # on MDOLX261145 against the 08-26 row — and the 08-25 row promoted to
        # WIN by the very Send that booking had already consumed, then aged to
        # LOSS/SEND_NO_BOOKING. An invented loss on a shipment that shipped,
        # and an OL-never-confirmed accusation against a booking OL confirmed.
        #
        # Now, in order:
        #   1. thread pool — rows this reply is provably anchored to. When it
        #      is non-empty it is the WHOLE candidate set: a Send that names
        #      its thread never lands outside it.
        #   2. booking evidence — a row whose MDOLX landed AT OR AFTER this
        #      Send outranks an open row. That booking IS what the Send
        #      bought; a booked row is an eligible target, not a skipped one.
        #   3. recency — the previous rule, unchanged, as the tie-break.
        # Every Send is consumed by at most one row (`_send_consumed`), so two
        # genuine Sends on one lane still promote two rows.
        chain = send_thread_anchors(row)
        conv = (row.get("conversation_id") or "").strip()
        pool = [r for r in candidates if id(r) not in _send_consumed]
        thread_pool = [r for r in pool if send_reply_is_in_thread(r, chain, conv)]
        match_via = "thread" if thread_pool else "lane+time"
        best = None
        best_key = None
        for r in (thread_pool or pool):
            req_dt = C.parse_iso(r.get("request_timestamp"))
            if not req_dt or req_dt > sent_dt:
                continue
            # Send-reply window widened 5d -> 7d. Lonny sometimes sits on a
            # rate quote a full week before sending — this still excludes the
            # truly stale ones (>7 days = different ask).
            if (sent_dt - req_dt) > timedelta(days=7):
                continue
            bk_dt = C.parse_iso(r.get("booking_timestamp"))
            booked_after = 1 if (r.get("mdolx_ref") and bk_dt and bk_dt >= sent_dt) else 0
            key = (booked_after, req_dt)
            if best is None or key > best_key:
                best, best_key = r, key
        if best is not None:
            _send_consumed.add(id(best))
            best["_send_match_via"] = match_via
            _imid = row.get("imid")
            if _imid:
                # Filtered, not just unioned: build_requests appends
                # row.get("imid") unguarded, so a stage row with no
                # internetMessageId leaves a None in here and sorted() would
                # die comparing None to str on the first such lane.
                best["source_imids"] = sorted(
                    {i for i in {*(best.get("source_imids") or []), _imid} if i})
        if best is not None and best.get("mdolx_ref"):
            # CONSUMED, NOT CASCADED. This Send produced THIS booking; there is
            # nothing left of it to promote anything else with. Returning here
            # is the whole fix — the row is already WIN with has_send set by
            # link_bookings_to_requests, so nothing is written that could go
            # stale, and no sibling ask inherits an acceptance that was never
            # about it.
            print(f"  send consumed by booked win MDOLX{best.get('mdolx_ref')} "
                  f"({best.get('lane')}) via {match_via} — no cascade")
            continue
        if best:
            # record_transition, not a bare assignment — see age_requests.
            # A promotion to WIN with no history entry is the same defect in
            # the other direction: the row reads WIN while status_history has
            # no record of when or why it got there.
            C.record_transition(best, "WIN", "Lonny replied Send", at=sent_dt)
            best["quoted"] = True
            best["has_send"] = True
            # Inherit carrier_won from carrier_quoted if we captured one earlier
            if not best.get("carrier_won") and best.get("carrier_quoted"):
                best["carrier_won"] = best["carrier_quoted"]
            # If still missing, look back at the most recent quoted SIBLING on
            # the same canonical lane within 30 days — Lonny's "send" usually
            # references the last rate OL gave on that lane (audit fix
            # 2026-04-30 — was leaving 6 send-reply wins with no carrier).
            if not best.get("carrier_won"):
                best_dest_key = canonical_lane_key(best.get("destination"))
                fallback_carrier = None
                for sib in candidates:
                    if sib is best or not sib.get("carrier_quoted"):
                        continue
                    sib_dt = C.parse_iso(sib.get("response_timestamp") or sib.get("request_timestamp"))
                    if not sib_dt or sib_dt > sent_dt:
                        continue
                    if (sent_dt - sib_dt) > timedelta(days=30):
                        continue
                    fallback_carrier = sib.get("carrier_quoted")
                if fallback_carrier:
                    best["carrier_won"] = fallback_carrier
                    best["carrier_quoted"] = fallback_carrier
            # Last resort: substring lane fallback. Look at ALL requests
            # whose canonical lane KEY shares a prefix/substring (e.g.
            # "hcmc (cai mep)" ↔ "hcmc (cat lai)" both share "hcmc"). This
            # finalizes the 3 unsourceable wins from 2026-05-01 audit
            # (Cai Mep, Cat Lai, Manila North) when off-channel rates were
            # accepted via "send" without the rate-response email in the
            # corpus. Conservative: only inherits from a sibling within 30d.
            if not best.get("carrier_won"):
                best_dest_key = canonical_lane_key(best.get("destination"))
                # Collapse "hcmc (cai mep)" → "hcmc"; "manila (north)" → "manila"
                best_prefix = best_dest_key.split(" (", 1)[0].strip()
                if best_prefix and best_prefix != "unknown":
                    fallback_carrier = None
                    for r2 in requests:
                        if r2 is best or not r2.get("carrier_quoted"):
                            continue
                        sib_key = canonical_lane_key(r2.get("destination"))
                        sib_prefix = sib_key.split(" (", 1)[0].strip()
                        if sib_prefix != best_prefix:
                            continue
                        sib_dt = C.parse_iso(r2.get("response_timestamp") or r2.get("request_timestamp"))
                        if not sib_dt or sib_dt > sent_dt:
                            continue
                        if (sent_dt - sib_dt) > timedelta(days=30):
                            continue
                        fallback_carrier = r2.get("carrier_quoted")
                    if fallback_carrier:
                        best["carrier_won"] = fallback_carrier
                        best["carrier_quoted"] = fallback_carrier
            best["reason_detail"] = (best.get("reason_detail") or "") + \
                                    f" | Lonny Send reply {sent[:10]}"
            best["teu_won"] = best.get("teu_requested", 0)
            # The history entry is written by record_transition above, not
            # here. This hand-rolled append hardcoded "from": "PENDING" (wrong
            # whenever the row was in any other state) and fired even when the
            # row was ALREADY WIN, growing a duplicate entry on every ingest.
            promotions += 1
    return promotions


# ─────────────────────────────────────────────────────────────────────
# Age out pending → loss via core.decide_status
# ─────────────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────────────
# Age out pending → loss via core.decide_status
# ─────────────────────────────────────────────────────────────────────

def _clear_win_evidence_on_exit(r: dict, prior_status, new_status) -> None:
    """Drop win-only volume when a row LEAVES WIN.

    `teu_won` is the TEU we actually booked. When a send-signal WIN ages out
    to LOSS/SEND_NO_BOOKING because OL never issued the MDOLX, that volume was
    never booked — but nothing cleared it, so the row sat at status="LOSS"
    with teu_won=2 and every won-TEU rollup counted a shipment that does not
    exist. Deliberately narrow: this clears ONLY on the WIN → not-WIN edge,
    and only the volume. `has_send` / `mdolx_ref` stay — they are evidence of
    what happened, and erasing them is the separate 2026-07-26 defect fixed in
    core.decide_status and the qc_selfheal has_send heal.
    """
    if prior_status == "WIN" and new_status != "WIN" and r.get("teu_won"):
        r["teu_won"] = 0


def age_requests(requests: list[dict], now: datetime | None = None,
                 only: list[dict] | None = None) -> None:
    """Re-derive status. `only` restricts WHICH rows are re-decided.

    `only` exists because this function has no `manual_locked` guard and is
    normally called BEFORE apply_operator_corrections, which is what makes the
    applier's "runs LAST so it wins over every automatic classification" true.
    A second unrestricted call after the operator layer silently un-does human
    verdicts: measured 2026-09-03, a correction setting LOSS/SEND_NO_BOOKING on
    a fresh send-signal came back PENDING/AWAITING_MDOLX. The medians are still
    computed from the FULL list — a subset would skew the PRICE classifier.
    """
    now = now or C.now_utc()
    # Compute lane winning medians ONCE before the per-row decide loop —
    # see core.decide_status docstring (2026-06-02 PRICE classifier).
    lane_winning_median = C.compute_lane_winning_medians(requests)
    _targets = requests if only is None else only
    for r in _targets:
        if r.get("manual_locked") and only is not None:
            # An explicit re-derive never overrules a human. The ref this row
            # lost was reassigned BY a correction; the row's own verdict, if
            # it has one, still stands.
            continue
        # CONFIRMED wins (have an MDOLX booking) are terminal — skip.
        # But a row that's WIN with NO mdolx is a send-signal-only
        # promotion that must stay re-evaluable: if it never booked it
        # has to demote to SEND_NO_BOOKING. The old unconditional
        # `status == "WIN": continue` froze those as permanent phantom
        # wins (the QC-049 backlog). Fixed 2026-05-30.
        if r.get("status") == "WIN" and (r.get("mdolx_ref") or r.get("mdolx_refs_all")):
            continue
        # For pending requests where we never saw a quote, decide_status is the authority
        decision = C.decide_status(
            has_send=r.get("has_send", False),
            mdolx_ref=r.get("mdolx_ref"),
            response_timestamp=r.get("response_timestamp"),
            quoted=r.get("quoted", False),
            etd_fit_days=r.get("etd_fit_days"),
            request_timestamp=r.get("request_timestamp") or r.get("request_date"),
            send_signal_events=r.get("send_signal_events"),
            mdolx_refs_all=r.get("mdolx_refs_all"),
            now=now,
            ol_rate=r.get("ol_rate"),
            lane=r.get("lane"),
            lane_winning_median=lane_winning_median,
        )
        # Route through record_transition, never a bare assignment.
        # status_history is the field schema.json declares as THE transition
        # record — audits, the dashboard timeline and Sentry triage all
        # reconstruct the outcome from it. Assigning r["status"] directly left
        # the two contradicting each other: a send-signal WIN that never booked
        # was re-decided here to LOSS/SEND_NO_BOOKING while status_history
        # still ended at {"to": "WIN"}, so anything reading the history
        # reported the row as WON with no record of the regression. Proved on
        # a real age_requests run 2026-07-26.
        _prior_status = r.get("status")
        if _prior_status != decision.status:
            # STAMP THE DEADLINE, NOT THE FIRE CLOCK (2026-08-28).
            #
            # `at=now` is why a derived reversal never reached a report.
            # Measured on the production shape (window=previous, 06:30 ET
            # fire) for a Mon-14:00 send that never booked: WIN→LOSS stamped
            # 08-26 while the report covered 08-25, then 08-27 against 08-26,
            # then 08-28 against 08-27. Always exactly one day ahead of the
            # window and walking forward with it, because every fire rebuilds
            # status_history and re-stamps at that morning's now. Michael saw
            # the consequence as a green WIN pill beside "REVERSED, now LOSS"
            # with no reversal ever following it.
            #
            # decision.stale_at is the instant the row actually crossed its
            # window, from the same predicate that decided it was past. It is
            # in the PAST by construction on any aging branch (stale means
            # now > deadline), it is IDENTICAL on every later fire, so the
            # entry stops migrating, and it lands the event on the business
            # day it happened — which is the day the report covers.
            #
            # None for a decision no window produced (a dateless row, or a
            # non-aging status): fall back to now rather than invent a clock.
            # Same rule, same reason, as the 2026-08-11 prior-build WIN
            # restore below — "date it from the evidence, never from now".
            C.record_transition(r, decision.status, decision.reason_detail,
                                at=decision.stale_at or now)
        else:
            r["status"] = decision.status
        r["loss_reason"] = decision.loss_reason
        _clear_win_evidence_on_exit(r, _prior_status, decision.status)
        # Don't overwrite reason_detail if it was set by a successful link
        if not r.get("reason_detail") or "Staged — pending match" in (r.get("reason_detail") or ""):
            r["reason_detail"] = decision.reason_detail


# ─────────────────────────────────────────────────────────────────────
# Operator corrections — authoritative human overrides
# ─────────────────────────────────────────────────────────────────────

def apply_operator_corrections(requests: list[dict]) -> int:
    """Apply authoritative operator corrections from operator_corrections.json.

    Runs LAST — after every automatic classification — and on EVERY ingest, so
    a human's verdict on a specific row survives re-ingest (which otherwise
    rebuilds the row from the staged email and re-promotes it). Each correction
    is matched by request_id; the row's fields are overwritten per `set`, the
    row is marked manual_locked so qc_selfheal won't re-decide it, and the
    override is recorded in status_history + reason_detail. Idempotent.

    qc_selfheal imports and calls this same function as a self-heal backstop —
    one source of truth, so the intake apply and the QC apply cannot drift.
    """
    if not CORRECTIONS_PATH.exists():
        return 0
    try:
        doc = json.loads(CORRECTIONS_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"WARN: operator_corrections.json unreadable — skipping ({e})")
        return 0
    by_id = {r.get("request_id"): r for r in requests}
    applied = 0
    for corr in doc.get("corrections", []):
        rid = corr.get("request_id")
        changes = corr.get("set") or {}
        row = by_id.get(rid)
        # `exclude: true` — the row is NOT a Hilmar-the-client move at all
        # (e.g. stand_260821: an AgriDairy booking whose "HILMAR" token is the
        # plant/origin, not the customer). Remove it from the data entirely —
        # a `set` override would still count it in Hilmar's wins/TEU. A
        # missing row is the EXPECTED steady state once excluded (fresh
        # ingest also drops it), so absence is silent, not a WARN.
        if corr.get("exclude"):
            # Pop from by_id too so a duplicate correction for the same rid
            # (a second exclude, or an exclude followed by a `set`) sees "no
            # row" instead of crashing requests.remove() on an already-removed
            # object or "applying" a set to a row no longer in the dataset.
            if row is not None and row in requests:
                requests.remove(row)
                by_id.pop(rid, None)
                applied += 1
                print(f"Operator correction: EXCLUDED {rid} — "
                      f"{corr.get('note') or corr.get('source') or 'not a Hilmar-client move'}")
            continue
        if not row and corr.get("create"):
            # `create: true` — RECORD A WIN THAT NO EMAIL CAN PRODUCE.
            #
            # Michael, 2026-08-12: "everything she sent as a booking is a win
            # ... assume that each win was a quote request so just use the
            # wins if you cannot find the emails from lonny". OL's booking
            # recap is the system of record; a booking in it happened whether
            # or not the confirmation reached the mailbox we read. Amending an
            # existing row cannot express that when there is no row at all,
            # which is the case for a booking whose entire thread went
            # To: Lonny, Cc: the group.
            #
            # GUARDED AGAINST THE DUPLICATE IT WOULD OTHERWISE CAUSE: once the
            # forwarding fix lets the real confirmation through, ingest builds
            # its own row for that MDOLX. If any row already carries this
            # number, the created row is skipped — the derived row wins,
            # because it has the email behind it. So this heals a gap without
            # becoming a second source of the same win.
            _ref = str(changes.get("mdolx_ref") or "").lstrip("0")
            # mdolx_refs_seen included: a ref demoted by
            # claim_corrected_mdolx_refs is still HELD by the dataset, just no
            # longer counted. Omitting it would let this branch create a
            # second row for a booking that is already represented.
            _seen = {str(x).lstrip("0")
                     for r in requests
                     for x in [r.get("mdolx_ref"),
                               *(r.get("mdolx_refs_all") or []),
                               *(r.get("mdolx_refs_seen") or [])]
                     if x}
            if _ref and _ref in _seen:
                continue
            _at = changes.get("booking_timestamp") or corr.get("at") or C.now_utc().isoformat()
            row = {
                "request_id": rid,
                "status": changes.get("status", "WIN"),
                "origin": changes.get("origin") or "Oakland",
                "destination": changes.get("destination") or "Unknown",
                "lane": changes.get("lane") or "Lane unresolved",
                "request_timestamp": None,
                "request_date": C.et_date_of(_at),
                "response_timestamp": None,
                "booking_timestamp": _at,
                "quoted": True,
                "has_send": True,
                "teu_requested": changes.get("teu_requested", 0),
                "teu_won": changes.get("teu_won", changes.get("teu_requested", 0)),
                "source_imids": [],
                "source_ids": [],
                "status_history": [{
                    "at": _at, "from": "PENDING", "to": "WIN",
                    "reason": f"MDOLX{_ref or '?'} booked — recorded from OL's "
                              "booking recap; no confirmation email reached "
                              "this mailbox",
                }],
            }
            requests.append(row)
            by_id[rid] = row
            print(f"Operator correction: CREATED {rid} (MDOLX{_ref}) — "
                  f"{corr.get('note') or corr.get('source') or 'from OL booking recap'}")
        if not row:
            print(f"WARN: operator correction for {rid} has no matching row — skipped")
            continue
        # Idempotent — qc_selfheal re-runs this; only act when something changes.
        if all(row.get(k) == v for k, v in changes.items()):
            continue
        prior_status = row.get("status")
        row.update(changes)
        row["manual_locked"] = True
        # History records a TRANSITION, so it is written only when the status
        # actually changes. The rebuild wipes correction fields every fire, so
        # this applier re-runs daily — and until 2026-08-12 it appended a
        # fire-time WIN→WIN entry each time on rows whose status the
        # correction merely confirmed. core.win_event_date reads the last
        # →WIN entry, so stand_260905's Jul-9 booking re-dated to "today",
        # every day — the last survivor of the rolling-win defect
        # (diag-weekly, run 31611357523).
        new_status = changes.get("status", prior_status)
        if new_status != prior_status:
            # WHEN THE BOOKING HAPPENED, NOT WHEN WE LEARNED OF IT.
            #
            # This was C.now_utc(), so a correction back-entering a real
            # booking stamped it "today". On 2026-08-13 that put 18 bookings
            # — MDOLX 260896 and the 261025-261047 batch out of OL's
            # transaction report — into the week of Aug 10-14, and the weekly
            # summary reported 17 wins against 12 requests. Michael, seeing
            # it: "that's unusual and sounds like bad parse". It was: the
            # bookings are real, the DATE was manufactured by the applier.
            #
            # Same preference order the prior-WIN restore already uses
            # (_restore_prior_win): the booking's own time, then the reply
            # that carried it, then the ask. now() only when the row carries
            # no clock at all, and then it is the honest answer.
            _at = (changes.get("booking_timestamp") or corr.get("at")
                   or row.get("booking_timestamp") or row.get("response_timestamp")
                   or row.get("request_timestamp") or C.now_utc().isoformat())
            row.setdefault("status_history", []).append({
                "at": _at,
                "from": prior_status,
                "to": new_status,
                "reason": "Operator correction: " + (corr.get("note") or corr.get("source") or ""),
            })
        if corr.get("note"):
            row["reason_detail"] = corr["note"]
        applied += 1
    return applied


#: Booking-derived fields an absorbed duplicate may hand to the row that keeps
#: the ref. FILL-ONLY, never overwrite — the surviving row's own value came
#: from its own thread and is the better evidence. Same rule, same reason, as
#: `_merge_prior_win_into`: "evidence fields fill only where the rebuilt row
#: has nothing."
_ABSORBABLE_BOOKING_FIELDS = (
    # teu_won FIRST because it is the one that under-counts if forgotten.
    # Measured 2026-09-03 rehearsing this heal over all eleven live findings:
    # several owner rows carry teu_won=None while the orphan booking row
    # beside them holds the volume (the orphan was built FROM the booking
    # confirmation, which names the containers; the owner's RFQ thread may
    # not). Absorb without it and removing the orphan silently deletes booked
    # TEU for a shipment OL confirmed — the client-report under-count this
    # whole design is arranged to avoid, committed by the fix for it.
    # Fill-only like the rest, so an owner with its own volume keeps it.
    "teu_won",
    "carrier_won", "booking_timestamp", "vessel_voyage", "mdolx_date",
    "erd", "doc_cutoff", "port_cutoff", "origin_free_time", "dest_free_time",
    "rate_expiry", "product", "temperature", "pod", "booking_no",
)


def _demote_ref(row: dict, ref: str) -> None:
    """Move `ref` out of the counting fields into `mdolx_refs_seen`.

    NOT a delete, and the difference is the whole design. `mdolx_refs_all` is
    read by TWO kinds of consumer and they want opposite things:

      COUNTERS   core.booking_count unions mdolx_ref + mdolx_refs_all and
                 returns len(); it is the numerator AND denominator of every
                 win and request count. A ref on two rows is counted twice.
      JOIN KEYS  patch_carriers looks a booking PDF up by
                 [mdolx_ref] + mdolx_refs_all (:701), and the same list finds
                 the related rate response (:209) and the carrier (:533). The
                 PDF is where `pod` comes from, and PASS 2b recovers
                 `destination` / `lane` from `pod`.

    Deleting the ref outright fixes the count and severs the join. A row whose
    lane came from that PDF then falls to "Lane unresolved", and
    gen_client_email._lane_resolved drops it from every client bucket — Lonny
    told one FEWER booking, and less TEU, for a shipment OL really confirmed.
    Every guard the first draft of this heal leaned on stays green while that
    happens: is_confirmed_win still True on the surviving ref, QC-049 needs
    BOTH fields empty, and QC-069 goes quiet. Same defect class as 2026-08-10,
    "you sent lonny we won no shipment last week".

    So the ref stops being COUNTED and stays JOINABLE. `mdolx_refs_seen` is
    read by the three enrichment lookups and by nothing that counts.

    Always writes lists. ingest.py:1054 does
    `set(best.get("mdolx_refs_all", []) + [mdolx])`, which TypeErrors on None.
    """
    ref = str(ref)
    cleared_primary = str(row.get("mdolx_ref") or "") == ref
    if cleared_primary:
        row["mdolx_ref"] = None
    row["mdolx_refs_all"] = sorted(
        {str(m) for m in (row.get("mdolx_refs_all") or []) if m and str(m) != ref})
    row["mdolx_refs_seen"] = sorted(
        {str(m) for m in (row.get("mdolx_refs_seen") or []) if m} | {ref})

    # PROMOTE A SURVIVOR. A row can hold more than one booking, and losing the
    # DISPUTED one must not strand the others: `mdolx_ref` is the primary that
    # decide_status reads, and it is the ONLY ref qc_selfheal's decide loop
    # passes (qc_selfheal.py:1540 `mdolx_ref=r.get("mdolx_ref")`). Measured
    # 2026-09-03 without this: a row holding 261031 (disputed) and 261099
    # (real, undisputed) came out mdolx_ref=None, and decide_status returned
    # LOSS/SEND_NO_BOOKING — the surviving booking silently stops being a win,
    # its TEU crosses to quoted-lost, and the row leaves every client bucket.
    # is_confirmed_win reads the union and stays True the whole time, so
    # nothing else notices.
    if cleared_primary and row["mdolx_refs_all"]:
        row["mdolx_ref"] = row["mdolx_refs_all"][0]


def _row_holds(row: dict, ref: str) -> bool:
    """Does this row CLAIM the ref — as its primary, or in the counted list?

    `mdolx_refs_seen` deliberately does not count: a demoted ref is exactly
    the state this heal produces, so treating it as a claim would make the
    pass non-idempotent and it would re-absorb the same row every fire.
    """
    ref = str(ref)
    if str(row.get("mdolx_ref") or "") == ref:
        return True
    return any(str(m) == ref for m in (row.get("mdolx_refs_all") or []))


def claim_corrected_mdolx_refs(requests: list[dict]) -> tuple[int, list[dict]]:
    """A booking ref an operator correction names belongs to ONE row: that one.

    2026-09-03, approved by Michael after the mechanism was measured. QC-069
    flagged 11 duplicate_mdolx findings and ALL ELEVEN are the same collision
    (diag-blob run 33788252407, `scripts/diag_duplicate_mdolx.py`):

      link_bookings_to_requests   writes mdolx_ref AND appends to
                                  mdolx_refs_all on the row IT scored best
      apply_operator_corrections  row.update(changes) overwrites mdolx_ref on
                                  the row the OPERATOR named — and touches
                                  mdolx_refs_all not at all, and never clears
                                  the ref off the matcher's row

    They do not name the same row. Eight corrections back-entered the
    MDOLX261026-261046 batch from Linda Echevarria's Aug-12 recap, each noting
    "The confirmation never reached [this mailbox]" — true when written, false
    now: the confirmations arrived 2026-08-13 20:04-20:21, one day after the
    recap. So every fire since has run both writers over the same refs and
    nothing un-stamped the loser. CLAUDE.md's standing corollary, in one field.

    THE OPERATOR'S ROW WINS. Not a preference — operator_corrections.json is
    this repo's single durable human override, and the source here is OL's own
    system of record. The correction also names a row with a real RFQ thread;
    the matcher's loser is frequently a `stand_<ref>` row with none.

    Three shapes, measured, each handled differently because the damage differs:

      4a  the loser holds it only in mdolx_refs_all and keeps a ref of its own.
          DEMOTE (see _demote_ref) — no row removed, no status changed.
      4b  the loser is a whole standalone WIN row (stand_/ol_, no RFQ chain)
          that exists ONLY because of this booking. Its evidence is absorbed
          into the owner and the row is REMOVED — the shipment stops being
          counted twice.
      4c  the loser is an ordinary request row claiming it as its own
          mdolx_ref. Demoting leaves it a WIN with no booking ref, so its
          status is re-derived by the age_requests call that follows.

    NEVER touches a ref no correction names — that is case (3) in
    diag_duplicate_mdolx, genuinely ambiguous, and there is no operator
    verdict to defer to. NEVER empties the owner. Idempotent: a second pass
    finds nothing, because a demoted ref no longer counts as a claim.

    Returns (acted, released) — the number of losing rows acted on, and the
    rows left with NO booking ref that a caller must re-derive. NOT a count of
    rows to re-age wholesale: `age_requests` has no `manual_locked` guard, so
    re-running it over the whole dataset AFTER the operator layer overwrites
    human verdicts. Measured 2026-09-03: a correction setting
    LOSS/SEND_NO_BOOKING on a FRESH send-signal came back PENDING /
    AWAITING_MDOLX, because decide_status re-derives what the human overruled.
    """
    if not CORRECTIONS_PATH.exists():
        return 0, []
    try:
        doc = json.loads(CORRECTIONS_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"WARN: operator_corrections.json unreadable — no ref claim ({e})")
        return 0, []

    by_id = {r.get("request_id"): r for r in requests}
    acted = 0
    drop: list[dict] = []
    released: list[dict] = []

    for corr in doc.get("corrections", []):
        if corr.get("exclude"):
            continue
        ref = str((corr.get("set") or {}).get("mdolx_ref") or "").strip()
        if not ref:
            continue
        owner = by_id.get(corr.get("request_id"))
        # The owner must actually HOLD the ref. A correction naming a row that
        # does not carry the number is not this collision, and acting on it
        # would clear a field on evidence that never mentioned these rows.
        if owner is None or not _row_holds(owner, ref):
            continue

        for loser in requests:
            if loser is owner or loser in drop or not _row_holds(loser, ref):
                continue

            chainless = C.has_no_rfq_chain(loser)

            # ABSORB ONLY FROM A ROW WITH NO RFQ CHAIN, and the distinction is
            # not a nicety — it is the difference between recovering evidence
            # and inflating a win.
            #
            # A `stand_`/`ol_` row IS the booking: it was built from the
            # confirmation email and represents the SAME shipment as the
            # owner, so its containers, carrier and dates belong to the owner
            # and would be lost with the row.
            #
            # An ordinary `req_` row is a DIFFERENT ASK that the matcher
            # merely stamped with this booking. Caught 2026-09-03 rehearsing
            # the heal on the live 4c pair: req_f942b9672ff756ab is its own
            # 2026-08-26 RFQ carrying teu_won=8, and absorbing that moved 8
            # TEU onto req_b789e573316ead86's shipment — a fabricated volume
            # on a confirmed win, which is the worst thing this file can do.
            # Its evidence stays with it; only the REF moves.
            if chainless:
                owner["source_imids"] = sorted(
                    {i for i in ((owner.get("source_imids") or [])
                                 + (loser.get("source_imids") or [])) if i})
                owner["source_ids"] = sorted(
                    {i for i in ((owner.get("source_ids") or [])
                                 + (loser.get("source_ids") or [])) if i})
                for k in _ABSORBABLE_BOOKING_FIELDS:
                    if not owner.get(k) and loser.get(k):
                        owner[k] = loser[k]

            _demote_ref(loser, ref)
            acted += 1

            emptied = not loser.get("mdolx_ref") and not (loser.get("mdolx_refs_all") or [])

            if emptied and chainless:
                # 4b. This row exists ONLY because link_bookings_to_requests
                # could not place the booking. With the ref gone it represents
                # nothing, and leaving it would keep the shipment counted
                # twice — which is the finding.
                drop.append(loser)
                owner.setdefault("merge_notes", []).append(
                    f"Absorbed orphan booking row {loser.get('request_id')} for "
                    f"MDOLX{ref}: operator correction "
                    f"({corr.get('source') or 'operator_corrections.json'}) "
                    f"assigns this booking here, and that row carried no RFQ "
                    f"thread of its own.")
                print(f"MDOLX claim: absorbed {loser.get('request_id')} into "
                      f"{owner.get('request_id')} (MDOLX{ref}) — orphan booking "
                      f"row, no RFQ chain; shipment was counted twice")
            elif emptied:
                # 4c. A REAL request row the matcher stamped WIN with someone
                # else's booking. Its status is now unsupported; age_requests
                # re-derives it (it is no longer terminal at ingest.py:1864,
                # which skips a WIN only while it still holds a ref).
                released.append(loser)
                if loser.get("manual_locked"):
                    # TWO CORRECTIONS DISAGREE, and neither is mine to overrule.
                    # One correction assigns this booking to `owner`; another
                    # locked THIS row's verdict. The re-derive skips locked
                    # rows, so the row stays as the human left it — which for a
                    # locked WIN means a WIN with no booking ref, and QC-049
                    # errors on exactly that. That is the honest outcome (the
                    # alternative is silently reversing a human), but it must
                    # not be silent: the contradiction is in the corrections
                    # FILE and only a human can settle it.
                    print(f"::warning::MDOLX claim: {loser.get('request_id')} "
                          f"lost MDOLX{ref} to {owner.get('request_id')} but "
                          f"carries an operator correction of its own — NOT "
                          f"re-derived. Two corrections disagree about this "
                          f"booking; resolve it in operator_corrections.json.")
                else:
                    print(f"MDOLX claim: {loser.get('request_id')} released "
                          f"MDOLX{ref} to {owner.get('request_id')} per operator "
                          f"correction — it holds no other booking ref, so its "
                          f"status is re-derived below")
            else:
                # 4a. Keeps a ref of its own; nothing else changes.
                print(f"MDOLX claim: {loser.get('request_id')} released MDOLX{ref} "
                      f"to {owner.get('request_id')} per operator correction "
                      f"(still holds {loser.get('mdolx_ref') or loser.get('mdolx_refs_all')})")

    for row in drop:
        if row in requests:
            requests.remove(row)
    return acted, [r for r in released if r in requests]


#: Win EVIDENCE carried back onto a rebuilt row. Everything here is a fact the
#: prior build observed and the fresh stage simply could not see again — a
#: booking ref, who won it, the volume booked. Deliberately excludes anything
#: the fresh ingest re-derives correctly (lane, containers, timestamps), so a
#: carry-forward can restore an outcome without freezing stale display fields.
_PRIOR_WIN_EVIDENCE = (
    "mdolx_ref", "carrier_won", "teu_won", "booking_timestamp",
    "vessel_voyage", "mdolx_date",
)


def _merge_prior_win_into(existing: dict, prior_win: dict, prior_mtime: str) -> None:
    """Fold a prior WIN's evidence into the row the fresh stage rebuilt.

    Called when the carry-forward finds a row ALREADY carrying that
    request_id — which is the normal case whenever the RFQ email is still
    inside the stage window, since the fresh build reconstructs it as PENDING
    with no knowledge of the booking. Appending a second row instead (the
    behaviour until 2026-07-27) double-counted the shipment and left the same
    id in two contradicting states for phase_4 to arbitrate by field count.

    The prior WIN's status wins outright: it is backed by a booking the fresh
    stage cannot see. `mdolx_refs_all` unions, evidence fields fill only where
    the rebuilt row has nothing, and the transition is recorded so the audit
    trail explains the change (QC-072's invariant).
    """
    existing["mdolx_refs_all"] = sorted(
        {m for m in (list(existing.get("mdolx_refs_all") or [])
                     + list(prior_win.get("mdolx_refs_all") or [])
                     + [existing.get("mdolx_ref"), prior_win.get("mdolx_ref")]) if m}
    )
    for k in _PRIOR_WIN_EVIDENCE:
        if prior_win.get(k) not in (None, "", 0) and not existing.get(k):
            existing[k] = prior_win[k]
    # HOLD THE INVARIANT AT THE WRITER: a row with any booking ref has a
    # PRIMARY booking ref. The loop above skips a falsy prior value, so a
    # prior win whose mdolx_ref is "" — or one already carrying its only ref
    # in the list — merges into a row with mdolx_ref=None beside a populated
    # mdolx_refs_all. MEASURED, both cases produce it. That shape is what
    # made decide_status and its own booking_count disagree; the same
    # survivor promotion _demote_ref already performs closes it here.
    if not existing.get("mdolx_ref") and existing.get("mdolx_refs_all"):
        existing["mdolx_ref"] = existing["mdolx_refs_all"][0]
    if not existing.get("teu_won"):
        existing["teu_won"] = prior_win.get("teu_won") or existing.get("teu_requested") or 0
    existing["quoted"] = True
    existing["has_send"] = True
    if existing.get("status") != "WIN":
        # DATE THE RESTORE FROM THE PRIOR EVIDENCE, NEVER FROM NOW.
        #
        # 2026-08-11. record_transition defaults at=now, and this restore runs
        # on EVERY fire for a win whose booking never re-enters the stage
        # window — so eight April wins carried a brand-new →WIN transition
        # dated each morning's fire, forever. core.win_event_date reads the
        # last →WIN transition, so those wins re-dated to "this week" daily
        # and every win-event surface mis-bucketed them permanently
        # (diag-weekly run 3: nine wins with win_event=2026-08-11, eight of
        # them April bookings, history reason "Prior-build WIN restored").
        #
        # Preference order: the prior row's ORIGINAL →WIN transition, carried
        # verbatim so its date survives the rebuild; else the booking time;
        # else the response time; else the ask time. Only a row with none of
        # those falls to now — and then once, not daily, since the carried
        # history keeps its date on every later fire. Restore entries from
        # the poisoned era are excluded as a date source by their reason
        # prefix, so already-rolled rows self-heal to real evidence.
        _orig_wins = [h for h in (prior_win.get("status_history") or [])
                      if h.get("to") == "WIN" and h.get("at")
                      and not str(h.get("reason") or "").startswith(
                          "Prior-build WIN restored")]
        if _orig_wins:
            existing.setdefault("status_history", []).extend(_orig_wins)
            existing["status"] = "WIN"
        else:
            _at = C.parse_iso(prior_win.get("booking_timestamp")
                              or prior_win.get("response_timestamp")
                              or prior_win.get("request_timestamp")
                              # a degraded prior can lack even its ask time;
                              # the rebuilt row carries the same ask
                              or existing.get("request_timestamp"))
            C.record_transition(
                existing, "WIN",
                f"Prior-build WIN restored (MDOLX{prior_win.get('mdolx_ref') or '?'}) — "
                f"booking not visible in the current stage window",
                at=_at)
    existing["loss_reason"] = None
    existing["preserved_from_prior"] = True
    existing["preserved_source_mtime"] = prior_mtime


def _prior_win_captured(wm, wma, new_mdolx_all, wdest, wdate, new_lane_dates) -> bool:
    """Is this prior WIN already represented in the freshly-built wins?

    Returns True when the prior WIN is "captured" (present in the new build, so
    it must NOT be carried forward as a duplicate). A WIN is captured when ANY
    of its MDOLX refs — primary OR any secondary — appears among the new build's
    MDOLX values. When the prior WIN has no MDOLX at all, fall back to a
    lane+date match.

    The primary `mdolx_ref` is just the last-linked ref and can change which one
    is primary across re-ingest (a request accumulates `mdolx_refs_all`). The
    old inline test `(wm and wm not in new_mdolx_all) or (wma and not any(...))`
    OR'd the two clauses on the primary alone, so a prior WIN whose primary ref
    was absent but whose secondary ref WAS present got re-appended as a second
    WIN row for one booking — inflating wins, win_rate, and teu_won.
    """
    all_refs = {wm, *(wma or [])} - {None, ""}
    if all_refs:
        return bool(all_refs & new_mdolx_all)
    # "unknown" is canonical_port_key's sentinel for a MISSING destination, not
    # a place. Two rows that both failed to resolve a lane are not evidence of
    # each other, and treating them as a match would DROP a prior WIN instead
    # of preserving it — the opposite of this function's job. The old bare
    # .lower() got this right by accident (empty string is falsy); the
    # alias-aware key has to say it out loud.
    if not wdest or wdest == "unknown":
        return False
    return (wdest, wdate) in new_lane_dates


# ─────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────

def attach_bodies(rows: list[dict]) -> int:
    """Join fetched bodies onto stage rows; return how many rows got one.

    Plan A, Day 1. For imids without a fetched body, `body_parsed` stays empty
    — everything still works on preview only. Note this is also the ONLY place
    conversation_id reaches a stage row, so anything that reasons about
    threads must run after it.

    Extracted from main() 2026-08-12 so diag_matching.py can replay the real
    join instead of copying it. A diagnostic that skips this step sees rows
    with no bodies and no conversation_id and answers questions about a
    pipeline nobody runs (the 2026-08-10 diag_bookings lesson).
    """
    bodies_idx = load_bodies_index()
    attached = 0
    for r in rows:
        imid = r.get("imid")
        bod = bodies_idx.get(imid) if imid else None
        if bod:
            r["body_parsed"] = bod.get("parsed") or {}
            r["text_body"] = bod.get("text_body") or ""
            r["conversation_id"] = bod.get("conversation_id")
            attached += 1
        else:
            r["body_parsed"] = {}
            r["text_body"] = ""
    return attached


def main() -> int:
    rows = load_stage()
    by_bucket = Counter(r.get("bucket") for r in rows)
    print(f"Loaded {len(rows)} staged rows: {dict(by_bucket)}")

    attached = attach_bodies(rows)
    print(f"Body enrichment: {attached}/{len(rows)} rows have fetched bodies")

    # Out-of-scope exclusion (Michael 2026-05-20; Linda Echevarria audit) —
    # drop staged rows that are NOT Hilmar ocean RFQs (Numidia / trucking /
    # recalled) BEFORE the bucket split, so no downstream path (requests,
    # bookings, wins, not-quoted) can count them.
    _oos = Counter()
    _kept_rows = []
    _dropped_rows = []
    for _r in rows:
        _reason = out_of_scope_reason(_r)
        if _reason:
            _oos[_reason] += 1
            _dropped_rows.append(_r)
        else:
            _kept_rows.append(_r)
    # The MDOLX numbers those dropped messages carry, captured HERE because in
    # two lines they are gone: `rows` becomes the kept set and nothing
    # downstream can ever see the sibling that gave the thread away. This is
    # the whole thread-level signal (see out_of_scope_mdolx).
    _excluded_mdolx = out_of_scope_mdolx(_dropped_rows)
    rows = _kept_rows
    if _oos:
        print("Out-of-scope exclusion: dropped "
              + ", ".join(f"{n} {k}" for k, n in sorted(_oos.items())))
    if _excluded_mdolx:
        print(f"Thread-level: {len(_excluded_mdolx)} MDOLX number(s) belong to "
              "another customer — " + ", ".join(
                  f"{m}={why}" for m, why in sorted(_excluded_mdolx.items())))

    lonny_out   = [r for r in rows if r.get("bucket") == "lonny_outbound"]
    lonny_reply = [r for r in rows if r.get("bucket") == "lonny_reply"]
    rate_rsps   = [r for r in rows if counts_as_rate_response(r)]
    # mbd_inbound handled inside collect_bookings; lonny_reply MDOLX also feeds bookings

    requests = build_requests(lonny_out)
    print(f"Built {len(requests)} rate_requests from Lonny outbound")

    # Apply rate responses FIRST so quoted=True is set before we check bookings.
    quoted = apply_rate_responses(requests, rate_rsps)
    print(f"Rate-response matches: {quoted}/{len(rate_rsps)} (requests now marked quoted)")

    # NEVER SILENT. A stricter client gate fails by making a real win quietly
    # stop existing, and a booking count that drops with no line explaining
    # why is indistinguishable from the pipeline breaking. Every thread-level
    # drop names itself, with the subject, so the next reader can judge it.
    _tl_dropped = []

    def _log_thread_drop(mdolx, reason, subject):
        _tl_dropped.append((mdolx, reason, subject))
        print(f"  thread-level drop: MDOLX{mdolx} ({reason}) — {subject[:90]}")

    bookings = collect_bookings(rows, excluded_mdolx=_excluded_mdolx,
                                log_excluded=_log_thread_drop)
    print(f"Collected {len(bookings)} unique HILMAR MDOLX bookings"
          + (f" ({len(_tl_dropped)} message(s) dropped by the thread-level "
             "client check)" if _tl_dropped else ""))

    requests, standalones = link_bookings_to_requests(requests, bookings)
    matched = sum(1 for r in requests if r.get("status") == "WIN")
    _by_operator = sum(1 for r in requests
                       if r.get("_booking_match_via") == "operator_correction")
    print(f"Linked {matched}/{len(bookings)} bookings to requests; {len(standalones)} standalone wins"
          + (f"; {_by_operator} placed on the row an operator correction names, before scoring"
             if _by_operator else ""))

    promos = apply_send_signals(requests, lonny_reply)
    print(f"Send-reply promotions: {promos}")

    all_requests = requests + standalones

    # Finalizer: backfill carrier_quoted from carrier_won when we know the
    # winning carrier (e.g. via subject parser) but the rate response wasn't
    # in our corpus. The booking IS the quote+book in one shot; carrier_won
    # implies that same carrier was quoted. Audit fix 2026-05-01.
    cross_filled = 0
    for r in all_requests:
        if r.get("carrier_won") and not r.get("carrier_quoted"):
            r["carrier_quoted"] = r["carrier_won"]
            cross_filled += 1
    if cross_filled:
        print(f"Carrier_quoted backfill from carrier_won: {cross_filled}")

    # ─────────────────────────────────────────────────────────────────────
    # Additive merge — preserve prior wins that the fresh stage can't reproduce.
    #
    # Background (2026-05-05 cutover audit):
    # ingest.py was originally destructive — every run rebuilt tracking-data-v2
    # from whatever was in stage. That worked when the stage was the only
    # source of truth. But some historic wins (4 of 30 in today's data) carry
    # MDOLX numbers like 260364/260365/260434 that NEVER had a booking-
    # confirmation email in Michael's mailbox — they were known to OL only
    # via Linda Echevarria's weekly recap emails. When the broader laptop
    # refresh runs ingest, those wins silently demote to LOSS because there's
    # no booking email to link.
    #
    # Fix: load the prior tracking-data-v2.json and carry forward any prior
    # WIN that the fresh ingest didn't reproduce as a WIN. Match by mdolx_ref
    # (most stable), then by request_id, then by destination + date as a
    # last resort. Tag each preserved entry with `preserved_from_prior=True`
    # plus the mtime of the source — QC layer alerts if this set grows
    # beyond a threshold (signalling we've lost more bookings than we can
    # paper over and need to widen the search).
    # ─────────────────────────────────────────────────────────────────────
    PRIOR_PATH = Path(__file__).resolve().parent.parent / "tracking-data-v2.json"
    preserved_count = 0
    preserved_recs: list[dict] = []
    if PRIOR_PATH.exists():
        try:
            prior = json.loads(PRIOR_PATH.read_text(encoding="utf-8"))
            prior_wins = [r for r in prior.get("requests", []) if r.get("status") == "WIN"]
            # Match keys are SCOPED TO NEW WINS — a prior WIN that landed as
            # LOSS in the new build (same req_id but demoted) MUST still be
            # preserved, because the demotion is the failure mode we're
            # protecting against. Caught 2026-05-05 first iteration:
            # KOBE/Nagoya/Taichung were silently NOT preserved because their
            # req_ids were already in `new_req_ids` (as LOSSes).
            new_wins = [r for r in all_requests if r.get("status") == "WIN"]
            new_mdolx_all: set[str] = {
                r.get("mdolx_ref") for r in new_wins if r.get("mdolx_ref")
            }
            for r in new_wins:
                for m in r.get("mdolx_refs_all", []) or []:
                    new_mdolx_all.add(m)
            # BOTH SIDES OF THIS COMPARISON GO THROUGH canonical_port_key.
            #
            # It was a bare .lower() on each side, which means the fallback
            # match fails the moment the two sides SPELL the port differently
            # — and that is exactly what a normalization change creates on the
            # day it ships. The 2026-08-27 UN/LOCODE merge is the live case:
            # the prior file says "Jpyok", the fresh build says "Yokohama", so
            # a prior WIN with no MDOLX ref would look uncaptured and be
            # APPENDED — a duplicate row carrying the pre-merge spelling and
            # the pre-merge request_id, i.e. the bug re-imported one day after
            # it was fixed. (The reconcile-by-request_id branch below cannot
            # save it either; the id moved with the destination.) Alias-aware
            # keys also fix the standing HCMC/Cat Lai version of the same
            # mismatch, which had the same shape and no ticket.
            new_lane_dates = {
                (C.canonical_port_key(r.get("destination")),
                 (r.get("request_timestamp") or "")[:10])
                for r in new_wins
            }
            prior_mtime = datetime.fromtimestamp(
                PRIOR_PATH.stat().st_mtime, tz=timezone.utc
            ).isoformat(timespec="seconds")
            for w in prior_wins:
                # Out-of-scope exclusion (Michael 2026-05-20): never resurrect
                # a prior WIN that is a Numidia / trucking / recalled row. The
                # fresh ingest already drops these; the additive merge must
                # not undo it. Prior rows carry only the subject.
                if out_of_scope_reason({"subject": w.get("subject")}):
                    continue
                wm = w.get("mdolx_ref")
                wma = list(w.get("mdolx_refs_all") or [])
                wdate = (w.get("request_timestamp") or "")[:10]
                wdest = C.canonical_port_key(w.get("destination"))
                # MDOLX is the strongest signal: the prior WIN is captured
                # (already represented, so do NOT carry it forward) when ANY of
                # its MDOLX refs — primary or secondary — appears among the new
                # wins. Only when none of its refs survive is the win genuinely
                # lost and preserved. If the old win had NO MDOLX (e.g. promoted
                # via send-signal without a booking visible), fall back to a
                # lane+date match — any new WIN on the same destination + same
                # calendar day likely represents the same logical win, even
                # under a renormalized conversation key. (See _prior_win_captured
                # for why the primary ref alone is not sufficient.)
                captured = _prior_win_captured(
                    wm, wma, new_mdolx_all, wdest, wdate, new_lane_dates)
                if captured:
                    continue
                # This prior WIN is not represented in the new build.
                #
                # RECONCILE BY request_id FIRST, then append. Appending
                # unconditionally created a SECOND row carrying the SAME
                # request_id: the fresh stage still holds the RFQ email, so it
                # rebuilds that row as PENDING, and this loop then appended the
                # old WIN beside it. tracking-data-v2.json ended up reporting
                # 2 entries and 8 TEU for one 4-TEU shipment, the same id
                # simultaneously PENDING/NQ and WIN — and when phase_4 later
                # collapsed the pair by non-empty FIELD COUNT it could keep
                # either, in one observed run discarding the very win this
                # carry-forward exists to protect (MDOLX260500 Oakland→
                # Yokohama, stamped WIN from Linda's recap).
                #
                # A status contradiction is never resolved by counting fields.
                # An mdolx-backed WIN is evidence; a rebuilt PENDING/LOSS on
                # the same id is the absence of evidence. Evidence wins.
                _wid = w.get("request_id")
                _existing = next(
                    (r for r in all_requests if r.get("request_id") == _wid), None
                ) if _wid else None
                if _existing is not None:
                    _merge_prior_win_into(_existing, w, prior_mtime)
                    preserved_count += 1
                    continue
                carried = dict(w)
                carried["preserved_from_prior"] = True
                carried["preserved_source_mtime"] = prior_mtime
                preserved_recs.append(carried)
                preserved_count += 1
        except Exception as e:
            print(f"WARN: additive merge failed to load prior — proceeding without: {e}")
    if preserved_count:
        print(f"Preserved {preserved_count} prior WIN(s) not reproduced by fresh stage")
        all_requests.extend(preserved_recs)

    age_requests(all_requests)

    # Operator corrections — authoritative human overrides, applied LAST so
    # they win over every automatic classification and survive re-ingest.
    corrected = apply_operator_corrections(all_requests)
    if corrected:
        print(f"Operator corrections applied: {corrected}")

    # A ref a correction names belongs to that row alone. Runs AFTER the
    # corrections (it reads what they wrote) and BEFORE the aggregates.
    claimed, released = claim_corrected_mdolx_refs(all_requests)
    if claimed:
        print(f"MDOLX claim: {claimed} duplicate ref holding(s) resolved")
    if released:
        # RE-DERIVE ONLY THE RELEASED ROWS. The claim can leave a 4c row a WIN
        # with no booking ref, and age_requests already ran (before the
        # corrections). Leaving it would publish a ref-less WIN for a whole
        # fire — QC-049 errors on it, and it is the "worse than the duplicate
        # it replaced" state this heal exists to avoid.
        #
        # `only=`, NOT a second unrestricted call. age_requests has no
        # manual_locked guard and the applier above is documented as running
        # LAST so it wins over every automatic classification; re-aging the
        # whole dataset after it silently reverses human verdicts. Measured
        # 2026-09-03 before this was scoped: a correction setting
        # LOSS/SEND_NO_BOOKING on a fresh send-signal came back
        # PENDING/AWAITING_MDOLX.
        print(f"MDOLX claim: re-deriving {len(released)} released row(s)")
        age_requests(all_requests, only=released)

    summary = C.aggregate_summary(all_requests)
    lanes   = C.aggregate_lanes(all_requests)
    carriers = C.aggregate_carriers(all_requests)

    output = {
        "version": "6.1-plan-a-bodies",
        "generated_at": C.now_utc().isoformat(),
        # KEY IS `date_range`, NOT `data_range`, and the window is COMPUTED.
        #
        # Two defects in one line. (a) Every reader — gen_email, gen_email_new,
        # render.data_window, restructure_two_table, insights — asks for
        # `date_range`; ingest wrote `data_range`, so the key was silently
        # absent and gen_email fell back to `cfg["data_range"]["start"]`,
        # printing the CONFIG's start date instead of the data's. (The hilmar
        # tree's qc.py:199 heals data_range->date_range, but the scripts/
        # pipeline that actually runs in production has no such heal.)
        # (b) The value was a hardcoded 2026-04-01..2026-04-19 literal, three
        # months stale, so even a reader that found it got the wrong window.
        # Computed from the rows actually in the file; dict shape, which
        # render._data_window and the schema's oneOf both accept.
        "date_range": _computed_date_range(all_requests),
        "requests": all_requests,
        "summary": summary,
        "lanes": list(lanes.values()),
        "carriers": list(carriers.values()),
        "notes": {
            "ingest_model": "Lonny outbound = request. MDOLX Hilmar booking = win. Rates-desk emails excluded.",
            "ol_responder_rule": "Always the MBD_OceanExportBookingShared mailbox identity.",
            "body_enrichment": "Bodies loaded from stage_emails_bodies.jsonl; body_parser fills eta/vessel/transshipment/rate.",
        },
    }

    out_path = Path(sys.argv[1]) if len(sys.argv) > 1 else OUT_PATH_DEFAULT
    C.save_data(output, out_path)
    print(f"Wrote {out_path}")
    print(f"Summary: {summary}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
