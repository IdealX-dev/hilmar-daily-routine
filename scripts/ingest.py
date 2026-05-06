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
from typing import Any

import core as C  # pure functions: parse_iso, parse_teu, decide_status, request_id, etc.
import body_parser as BP  # subject + body parsing (Plan A, Day 1)

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

OL_RESPONDER_NAME = "MBD Ocean Export Booking"   # shared mailbox identity
OL_RESPONDER_EMAIL = "MBD_OceanExportBookingShared@ol-usa.com"

DEST_RX = re.compile(r"^\s*oakland\s+to\s+(.+?)(?:\s*\(\d+\)\s*)?\s*$", re.IGNORECASE)
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


def _etd_fit_days(eta_requested: str | None, eta_offered: str | None) -> int | None:
    """Return int days difference (offered - requested). Negative = earlier than needed."""
    if not eta_requested or not eta_offered:
        return None
    try:
        req = datetime.fromisoformat(eta_requested).date()
        off = datetime.fromisoformat(eta_offered).date()
        return (off - req).days
    except (ValueError, TypeError):
        return None


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


def canonical_lane_key(destination: str | None) -> str:
    return (destination or "Unknown").strip().lower()


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
    # Split off any "(Foo Bar)" suffix
    m = re.match(r"^([A-Za-z]+)(\s*\(.+\))?$", s)
    if m:
        head, tail = m.group(1), m.group(2) or ""
        if len(head) <= 4 and head.isalpha():
            head = head.upper()
        else:
            head = head.title()
        return (head + tail).strip()
    return s.title()


# Subjects that look like rate requests but are actually operational
# follow-ups on existing bookings — they should NOT seed new request rows
# or standalone wins. Patterns are case-insensitive substrings.
_OPERATIONAL_SUBJECT_HINTS = (
    "FREE-TIME ISSUE", "FREE TIME ISSUE",
    "NEED TO SCHEDULE LOADING APPT", "LOADING APPT",
    "DEMURRAGE CHARGES MOUNTING",
    "BOOKING SCHEDULE INCONSISTENCY",
    "DISPUTE EBKG", "DISPUTE NAM",
    "PORT DISPUTE",
    "REEFER FREE TIME",        # Lonny status email, not a rate ask
    "UPDATED 20' AND 40' RATE",  # general rate update, no specific lane
    "CMA UPDATES",             # Michael internal
    "NRA AMENDMENT", "CONFIRMATION OF NRA",
    "INVOICE QUERY", "INVOICE DISPUTE",
    "TRANSPORT ORDER",         # ops follow-up tag, not a rate ask
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
            "request_date": sent_dt.date().isoformat() if sent_dt else None,
            "lonny_time_pt": C.fmt_pt(sent_dt) if sent_dt else None,
            "subject": subject,
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
            "etd_requested": None,
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
        })
    return _merge_thread_dupes(requests)


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
    for key, idxs in bucket.items():
        if len(idxs) < 2:
            continue
        # Sort by request_timestamp
        idxs.sort(key=lambda i: requests[i].get("request_timestamp") or "")
        # Walk pairs and merge if within 10 min and one has teu=0
        for a, b in zip(idxs, idxs[1:]):
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

def collect_bookings(rows: list[dict]) -> dict[str, dict]:
    """
    Return {mdolx: booking_dict}. Each booking represents a unique MDOLX
    shipment confirmation for Hilmar (1 MDOLX = 1 win, per Michael).
    """
    bookings: dict[str, dict] = {}

    for row in rows:
        bucket = row.get("bucket")
        subject = row.get("subject", "") or ""
        preview = row.get("summary_preview", "") or ""

        # Only consider Hilmar-tagged rows or rows where MDOLX+NUMIDIA/HILMAR is in subject
        is_hilmar = row.get("is_hilmar")
        if is_hilmar is None:
            is_hilmar = any(s in subject.upper() for s in ("HILMAR", "NUMIDIA", "HILMAR, CA"))
        if not is_hilmar:
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

        sent = row.get("sent")
        # Carry forward any body-parsed signer so link_bookings_to_requests can
        # populate ol_responder_signer on the matched/standalone request.
        body_parsed = row.get("body_parsed") or {}
        # First hit wins (earliest sighting of this MDOLX = booking creation time)
        existing = bookings.get(mdolx)
        if not existing or (sent and existing.get("sent", "") > sent):
            bookings[mdolx] = {
                "mdolx": mdolx,
                "subject": subject,
                "sent": sent,
                "preview": preview,
                "source_bucket": bucket,
                "source_imid": row.get("imid"),
                "source_id": row.get("id"),
                "body_signer": body_parsed.get("ol_responder_signer"),
            }

    return bookings


# ─────────────────────────────────────────────────────────────────────
# Link bookings → requests
# ─────────────────────────────────────────────────────────────────────

def link_bookings_to_requests(requests: list[dict], bookings: dict[str, dict]) -> tuple[list[dict], list[dict]]:
    """
    Match each booking to the most-recent Lonny outbound request with the same
    destination (case-insensitive), request_ts <= booking_ts, within 10 days.
    Unmatched bookings become standalone wins (prior-window rollovers).

    Returns (updated_requests, standalone_wins_as_requests).
    """
    # Index requests by destination (lane key)
    by_lane: dict[str, list[dict]] = defaultdict(list)
    for r in requests:
        by_lane[canonical_lane_key(r.get("destination"))].append(r)
    for lane_reqs in by_lane.values():
        lane_reqs.sort(key=lambda r: r.get("request_timestamp") or "")

    matched_mdolx: set[str] = set()

    for mdolx, bk in bookings.items():
        bk_ts = C.parse_iso(bk.get("sent"))
        if not bk_ts:
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

        # Pick the latest request_ts before booking_ts, within 14d (widened
        # 2026-04-30 — was missing send-replies that came back 11–13d after
        # the original Lonny ask).
        best = None
        for r in candidates:
            if r.get("mdolx_ref"):           # already matched a win
                continue
            req_ts = C.parse_iso(r.get("request_timestamp"))
            if not req_ts or req_ts > bk_ts:
                continue
            if (bk_ts - req_ts) > timedelta(days=14):
                continue
            if not best or (C.parse_iso(r["request_timestamp"]) >
                            C.parse_iso(best["request_timestamp"])):
                best = r

        if best:
            best["status"] = "WIN"
            best["has_send"] = True
            best["mdolx_ref"] = mdolx
            best["mdolx_refs_all"] = sorted(set(best.get("mdolx_refs_all", []) + [mdolx]))
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

            # ONLY set response fields if we never captured a quote.
            # Preserve rate-response timestamp (true OL responsiveness) when present.
            if not best.get("quoted") or not best.get("response_timestamp"):
                best["quoted"] = True
                best["response_timestamp"] = bk.get("sent")
                resp_dt = C.parse_iso(bk.get("sent"))
                req_dt = C.parse_iso(best.get("request_timestamp"))
                if resp_dt:
                    best["olusa_time_et"] = C.fmt_et(resp_dt)
                    if req_dt:
                        best["turnaround_biz_hours"] = C.biz_hours_between(req_dt, resp_dt)
                        best["turnaround_hours"] = C.clock_hours_between(req_dt, resp_dt)

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
        s_origin, s_dest = BP.parse_subject_lane(raw_subj)
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
        s_dest = s_dest or "Unknown"
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
        standalones.append({
            "request_id": f"stand_{mdolx}",
            "status": "WIN",
            "origin": s_origin,
            "destination": s_dest,
            "lane": lane,
            "request_timestamp": None,
            "request_date": bk_ts.date().isoformat() if bk_ts else None,
            "lonny_time_pt": None,
            "subject": bk.get("subject"),
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
            "response_timestamp": bk_ts_iso,
            "olusa_time_et": C.fmt_et(bk_ts) if bk_ts else None,
            "loss_reason": None,
            "reason_detail": f"Standalone booking (pre-window request) — MDOLX{mdolx}, no Lonny ask found in 30-day window",
            "status_history": [],
            "source_imids": [bk.get("source_imid")],
            "source_ids": [bk.get("source_id")],
        })
    return requests, standalones


# ─────────────────────────────────────────────────────────────────────
# Apply MBD_OceanExportBookingShared rate responses  ("RE: Oakland to X")
# ─────────────────────────────────────────────────────────────────────

def apply_rate_responses(requests: list[dict], rate_rsps: list[dict]) -> int:
    """
    For each rate-response email, match it back to the most-recent Lonny outbound
    request with the same destination (case-insensitive), request_ts <= response_ts,
    within 10 days. Flip quoted=True and populate carrier/rate/ETD/turnaround fields
    so decide_status() can split LOSS into quoted_lost vs not_quoted.

    Precondition: call BEFORE link_bookings_to_requests — if a booking lands on the
    same request it will overwrite the status to WIN, which is the correct outcome.

    Returns the number of requests that were quoted.
    """
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
            continue
        sent = rr.get("sent")
        sent_dt = C.parse_iso(sent)
        if not sent_dt:
            continue

        # Primary: exact canonical match.
        candidates = by_lane.get(canonical_lane_key(dest), [])
        # Fallback: substring match — handles "HCMC" matching "HCMC (Cat Lai)"
        # / "HCMC (Cai Mep)", "Yokohama " (trailing space) etc. Audit 2026-04-30.
        if not candidates:
            dest_canon = canonical_lane_key(dest).strip()
            for k, rs in by_lane.items():
                if k == "unknown":
                    continue
                if dest_canon in k or k in dest_canon:
                    candidates.extend(rs)

        # Match the latest Lonny outbound request before this response, within 14d,
        # that has not already been matched to a rate-response. Window widened from
        # 10d to 14d 2026-04-30 — caught Apr 28 Manila/Xingang send-replies whose
        # matching rate response was 12 days prior.
        best = None
        for r in candidates:
            if r.get("quoted"):
                continue  # one quote per request — earliest-first sort protects this
            req_dt = C.parse_iso(r.get("request_timestamp"))
            if not req_dt or req_dt > sent_dt:
                continue
            if (sent_dt - req_dt) > timedelta(days=14):
                continue
            if not best or (C.parse_iso(r["request_timestamp"]) >
                            C.parse_iso(best["request_timestamp"])):
                best = r

        if not best:
            continue

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
        best["etd_offered"] = rt.get("etd") or parsed.get("etd_offered")
        best["eta_offered"] = rt.get("eta") or parsed.get("eta_offered")
        best["vessel_voyage"] = rt.get("vessel_voyage") or parsed.get("vessel_voyage")
        best["transshipment"] = rt.get("transshipment") or parsed.get("transshipment")
        best["rate_expiry"] = rt.get("rate_expiry")
        best["detention_free"] = rt.get("detention_free")
        best["demurrage_free"] = rt.get("demurrage_free")
        # OL signer: only override if the body produced a real name. parse_signer
        # in core.py is strict-allowlist so any non-None here is a known OL person.
        body_signer = parsed.get("ol_responder_signer")
        if body_signer:
            best["ol_responder_signer"] = body_signer
        # Compute ETD-fit if we now have both eta_requested and eta_offered
        best["etd_fit_days"] = _etd_fit_days(best.get("eta_requested"), best.get("eta_offered"))
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

def apply_send_signals(requests: list[dict], lonny_replies: list[dict]) -> int:
    """
    For each lonny_reply with send_signal=True: promote the matched request to WIN
    if not already. Match by subject destination via RE: strip.
    Returns count of promotions.
    """
    promotions = 0
    by_lane: dict[str, list[dict]] = defaultdict(list)
    for r in requests:
        by_lane[canonical_lane_key(r.get("destination"))].append(r)
    for lane_reqs in by_lane.values():
        lane_reqs.sort(key=lambda r: r.get("request_timestamp") or "")

    for row in lonny_replies:
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
        candidates = by_lane.get(canonical_lane_key(dest), [])
        if not candidates:
            dest_canon = canonical_lane_key(dest).strip()
            for k, rs in by_lane.items():
                if k == "unknown":
                    continue
                if dest_canon in k or k in dest_canon:
                    candidates.extend(rs)
        best = None
        for r in candidates:
            if r.get("status") == "WIN":
                continue
            req_dt = C.parse_iso(r.get("request_timestamp"))
            if not req_dt or req_dt > sent_dt:
                continue
            # Send-reply window widened 5d -> 7d. Lonny sometimes sits on a
            # rate quote a full week before sending — this still excludes the
            # truly stale ones (>7 days = different ask).
            if (sent_dt - req_dt) > timedelta(days=7):
                continue
            if not best or (C.parse_iso(r["request_timestamp"]) >
                            C.parse_iso(best["request_timestamp"])):
                best = r
        if best:
            best["status"] = "WIN"
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
            best.setdefault("status_history", []).append({
                "at": sent,
                "from": "PENDING",
                "to": "WIN",
                "reason": "Lonny send-reply",
            })
            promotions += 1
    return promotions


# ─────────────────────────────────────────────────────────────────────
# Age out pending → loss via core.decide_status
# ─────────────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────────────
# Age out pending → loss via core.decide_status
# ─────────────────────────────────────────────────────────────────────

def age_requests(requests: list[dict], now: datetime | None = None) -> None:
    now = now or C.now_utc()
    for r in requests:
        if r.get("status") == "WIN":
            continue
        # For pending requests where we never saw a quote, decide_status is the authority
        decision = C.decide_status(
            has_send=r.get("has_send", False),
            mdolx_ref=r.get("mdolx_ref"),
            response_timestamp=r.get("response_timestamp"),
            quoted=r.get("quoted", False),
            etd_fit_days=r.get("etd_fit_days"),
            now=now,
        )
        r["status"] = decision.status
        r["loss_reason"] = decision.loss_reason
        # Don't overwrite reason_detail if it was set by a successful link
        if not r.get("reason_detail") or "Staged — pending match" in (r.get("reason_detail") or ""):
            r["reason_detail"] = decision.reason_detail


# ─────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────

def main() -> int:
    rows = load_stage()
    by_bucket = Counter(r.get("bucket") for r in rows)
    print(f"Loaded {len(rows)} staged rows: {dict(by_bucket)}")

    # Attach body-parsed fields (Plan A, Day 1). For imids without a fetched
    # body, `body_parsed` stays empty — everything still works on preview only.
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
    print(f"Body enrichment: {attached}/{len(rows)} rows have fetched bodies")

    lonny_out   = [r for r in rows if r.get("bucket") == "lonny_outbound"]
    lonny_reply = [r for r in rows if r.get("bucket") == "lonny_reply"]
    rate_rsps   = [r for r in rows if r.get("bucket") == "mbd_rate_response"]
    # mbd_inbound handled inside collect_bookings; lonny_reply MDOLX also feeds bookings

    requests = build_requests(lonny_out)
    print(f"Built {len(requests)} rate_requests from Lonny outbound")

    # Apply rate responses FIRST so quoted=True is set before we check bookings.
    quoted = apply_rate_responses(requests, rate_rsps)
    print(f"Rate-response matches: {quoted}/{len(rate_rsps)} (requests now marked quoted)")

    bookings = collect_bookings(rows)
    print(f"Collected {len(bookings)} unique HILMAR MDOLX bookings")

    requests, standalones = link_bookings_to_requests(requests, bookings)
    matched = sum(1 for r in requests if r.get("status") == "WIN")
    print(f"Linked {matched}/{len(bookings)} bookings to requests; {len(standalones)} standalone wins")

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
            new_req_ids = {r.get("request_id") for r in new_wins if r.get("request_id")}
            new_lane_dates = {
                ((r.get("destination") or "").lower(),
                 (r.get("request_timestamp") or "")[:10])
                for r in new_wins
            }
            prior_mtime = datetime.fromtimestamp(
                PRIOR_PATH.stat().st_mtime, tz=timezone.utc
            ).isoformat(timespec="seconds")
            for w in prior_wins:
                wm = w.get("mdolx_ref")
                wma = list(w.get("mdolx_refs_all") or [])
                wdate = (w.get("request_timestamp") or "")[:10]
                wdest = (w.get("destination") or "").lower()
                # MDOLX is the strongest signal — if the old win had one and
                # it's NOT among the new wins' MDOLX values, we definitely
                # lost the win in the new build. Preserve.
                # If the old win had NO MDOLX (e.g., promoted via send-signal
                # without booking visible), fall back to lane+date match —
                # any new WIN on the same destination + same calendar day
                # likely represents the same logical win, even if it has a
                # different request_id under a renormalized conversation key.
                if wm and wm not in new_mdolx_all:
                    captured = False
                elif wma and not any(m in new_mdolx_all for m in wma):
                    captured = False
                elif not wm and not wma:
                    captured = bool(wdest and (wdest, wdate) in new_lane_dates)
                else:
                    captured = True
                if captured:
                    continue
                # This prior WIN is not represented in the new build. Carry forward.
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

    summary = C.aggregate_summary(all_requests)
    lanes   = C.aggregate_lanes(all_requests)
    carriers = C.aggregate_carriers(all_requests)

    output = {
        "version": "6.1-plan-a-bodies",
        "generated_at": C.now_utc().isoformat(),
        "data_range": {"start": "2026-04-01", "end": "2026-04-19"},
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
