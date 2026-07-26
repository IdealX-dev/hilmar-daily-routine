#!/usr/bin/env python3
"""
hilmar.ingest — Microsoft Graph–backed ingest.

Replaces the Cowork-mode flow that staged messages to ``stage_emails.jsonl``
with a live, delegated-auth Graph fetch on Michael's mailbox.

Model (per Michael 2026-04-20, unchanged from ../scripts/ingest.py):
  * Lonny outbound = 1 rate_request (PENDING until won/lost).
  * Each unique HILMAR MDOLX booking = 1 WIN.
  * Wins link back to a request by (destination, time-window).
  * Unmatched MDOLX wins become standalone bookings (prior-window rollovers).
  * Caren / MBD_Export_Pricing rates-desk traffic is EXCLUDED — that's
    ops-prep noise, not Hilmar rate-desk action.

ol_responder is always the MBD shared mailbox identity, never an individual.

Idempotent merge:
  Re-running on the same window must not duplicate or clobber. If a
  request_id already exists in tracking-data-v2.json, only None-valued
  fields are filled in. Manually-edited or QC-healed fields are preserved.
"""
from __future__ import annotations

import json
import logging
import os
import re
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import body_parser as BP
from . import core as C
from .graph_client import GraphClient, MessageBody, MessageMeta

log = logging.getLogger(__name__)

OL_RESPONDER_NAME = "MBD Ocean Export Booking"
OL_RESPONDER_EMAIL = "MBD_OceanExportBookingShared@ol-usa.com"


def _is_shared_mailbox_label(name: str | None) -> bool:
    """True when ``name`` looks like the shared-mailbox display name in
    any form Outlook may emit — "MBD Ocean Export Booking",
    "MBD Ocean Export Booking (Shared)", "MBD_OceanExportBookingShared",
    "Ocean Export Booking Shared", etc. Used to reject these as signer
    candidates so the dashboard's "Quoted by" column shows the actual
    human (Ryan / Linda / etc.), not the mailbox.
    """
    if not name:
        return True
    n = name.lower().strip()
    if not n:
        return True
    if "mbd" in n and "ocean" in n and "export" in n:
        return True
    if "(shared)" in n or "shared mailbox" in n:
        return True
    return n.replace("_", " ").replace("-", " ").startswith("mbd ocean export")

# Origin-general (was hardcoded "oakland" until 2026-06-11 — the Dalhart
# blind spot): any known Hilmar origin site, single source in body_parser.
DEST_RX = re.compile(
    rf"^\s*(?:{'|'.join(re.escape(o) for o in BP.KNOWN_ORIGINS)})(?:,?\s*[A-Z]{{2}})?"
    rf"\s+to\s+(.+?)(?:\s*\(\d+\)\s*)?\s*$",
    re.IGNORECASE)
MDOLX_RX = re.compile(r"MDOLX\s*(\d{6,})", re.IGNORECASE)

# Excluded senders / mailboxes. Lower-cased on compare.
EXCLUDED_ADDRESSES = frozenset({
    "mbd_export_pricing@ol-usa.com",
    "caren.tobel@ol-usa.com",
})


# ─────────────────────────────────────────────────────────────────────
# Bucket classification
# ─────────────────────────────────────────────────────────────────────

LONNY_DEFAULT = "lupfold@hilmaringredients.com"
MBD_SHARED_DEFAULT = "mbd_oceanexportbookingshared@ol-usa.com"


@dataclass
class IngestConfig:
    """Resolved ingest config; values flow from env or explicit overrides."""
    lonny_address: str
    mbd_shared_address: str
    sender_address: str             # Michael (the authenticated user)
    window_start: datetime
    window_end: datetime

    @classmethod
    def from_env(
        cls,
        *,
        window_start: datetime,
        window_end: datetime,
    ) -> IngestConfig:
        lonny = (os.environ.get("HILMAR_HILMAR_FROM") or LONNY_DEFAULT).lower()
        mbd = (os.environ.get("HILMAR_INGEST_MAILBOX_BOOKING") or MBD_SHARED_DEFAULT).lower()
        sender = (os.environ.get("HILMAR_SENDER_EMAIL") or "michael.deitchman@ol-usa.com").lower()
        return cls(
            lonny_address=lonny,
            mbd_shared_address=mbd,
            sender_address=sender,
            window_start=window_start,
            window_end=window_end,
        )


def is_excluded(meta: MessageMeta) -> bool:
    """Caren / MBD_Export_Pricing emails are ops-prep noise, never Hilmar rates.

    Excluded if either the sender OR any recipient is in
    :data:`EXCLUDED_ADDRESSES`. Memo-of-record: see
    ``project_hilmar_rates_scope`` (auto-memory).
    """
    addrs = {meta.from_address or ""}.union(meta.to_addresses, meta.cc_addresses)
    return any((a or "").lower() in EXCLUDED_ADDRESSES for a in addrs)


def is_hilmar_subject(subject: str) -> bool:
    """Heuristic — matches Hilmar lane / booking subjects.

    True if subject contains HILMAR / NUMIDIA, or is shaped like
    ``Oakland to <city>`` (Lonny's outbound rate request lane format).
    """
    if not subject:
        return False
    su = subject.upper()
    if any(k in su for k in ("HILMAR", "NUMIDIA")):
        return True
    cleaned = re.sub(r"^\s*(re|fw|fwd):\s*", "", subject, flags=re.IGNORECASE)
    return bool(DEST_RX.match(cleaned))


def classify_bucket(meta: MessageMeta, cfg: IngestConfig) -> str | None:
    """Decide which Hilmar pipeline bucket the message belongs to.

    Returns one of:
      * ``"lonny_outbound"`` — fresh Oakland-to-X rate request from Lonny.
      * ``"lonny_reply"``    — Lonny reply on a thread (may carry send-signal).
      * ``"mbd_rate_response"`` — MBD shared replying with rate to Lonny.
      * ``"mbd_inbound"``    — MBD shared MDOLX booking confirmation.

    Returns ``None`` for messages that don't fit any bucket — those are
    silently dropped from ingest.
    """
    if is_excluded(meta):
        return None

    sender = (meta.from_address or "").lower()
    subj = meta.subject or ""

    # Lonny outbound: from Lonny, Oakland-to-X subject, NOT a reply.
    is_reply_subj = bool(re.match(r"^\s*(re|fw|fwd):", subj, re.IGNORECASE))

    if sender == cfg.lonny_address:
        # New rate request (fresh subject) vs reply on a quoted thread
        if not is_reply_subj and DEST_RX.match(subj):
            return "lonny_outbound"
        return "lonny_reply"

    if sender == cfg.mbd_shared_address:
        # Rate response when subject has the lane shape (RE: Oakland to X)
        # Booking when subject contains MDOLX.
        if MDOLX_RX.search(subj):
            return "mbd_inbound"
        if is_hilmar_subject(subj):
            return "mbd_rate_response"
        return None

    # Anything else not from Lonny / MBD shared is out of scope.
    return None


# ─────────────────────────────────────────────────────────────────────
# Body parsing helpers (unchanged shape from scripts/ingest.py)
# ─────────────────────────────────────────────────────────────────────

def clean_destination(subject: str) -> str | None:
    """Extract destination from subject. Prefer body_parser; fall back to DEST_RX."""
    if not subject:
        return None
    _, dest = BP.parse_subject_lane(subject)
    if dest:
        return dest
    s = re.sub(r"^\s*(re|fw|fwd):\s*", "", subject, flags=re.IGNORECASE)
    s = re.sub(r"\s*\((\d+)\)\s*$", "", s)
    m = DEST_RX.match(s)
    return m.group(1).strip() if m else None


def clean_origin(subject: str, default: str = "Oakland") -> str:
    if not subject:
        return default
    origin, _ = BP.parse_subject_lane(subject)
    return origin or default


def canonical_lane_key(destination: str | None) -> str:
    return (destination or "Unknown").strip().lower()


def extract_mdolx(text: str | None) -> str | None:
    if not text:
        return None
    m = MDOLX_RX.search(text)
    return m.group(1) if m else None


# Subjects that look like rate requests but are actually operational
# follow-ups on existing bookings — they should NOT seed new request rows
# or generate fake standalone wins on top of an already-existing MDOLX.
# Patterns are case-insensitive substrings.
#
# Audit fix 2026-04-30 — orchestrator-side fix that dropped row count
# 96 → 89 by removing genuine noise (FREE-TIME ISSUE, LOADING APPT,
# DISPUTE EBKG, CMA UPDATES, NRA AMENDMENT, etc.). Same noise was
# leaking into the production pipeline pre-port.
_OPERATIONAL_SUBJECT_HINTS = (
    "FREE-TIME ISSUE", "FREE TIME ISSUE",
    "NEED TO SCHEDULE LOADING APPT", "LOADING APPT",
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
    "TRANSPORT ORDER",         # ops follow-up tag, not a rate ask
)


def is_operational_subject(subject: str | None) -> bool:
    """True if subject looks like an ops/admin email rather than a rate ask
    or new booking. Used to drop noise rows that inflate request /
    standalone-WIN counts on subjects like ``FREE-TIME ISSUE`` or
    ``LOADING APPT`` (these reference existing bookings, not new
    rate quotes).
    """
    if not subject:
        return False
    up = subject.upper()
    return any(h in up for h in _OPERATIONAL_SUBJECT_HINTS)


#: External-banner prefix patterns Outlook/M365 prepend to inbound mail
#: (CAUTION / EXTERNAL / WARNING). When the rate-desk parser falls back
#: to the email preview for container counts, these banners used to leak
#: into the dashboard's container column. We strip them before parsing.
_BANNER_PREFIX_RX = re.compile(
    r"""(?:
        \[?(?:caution|external|warning)\b[^\]]*?\]?\s*[:.\-]?\s*
        (?:this\s+(?:email|message)\s+(?:originated|came|was\s+sent)\s+from\s+(?:outside|an\s+external).*?)?
        (?:do\s+not\s+(?:click|open|reply).*?)?
        (?:unless\s+you\s+(?:recognize|trust).*?(?:safe[.!]?))?
    )+""",
    re.IGNORECASE | re.DOTALL | re.VERBOSE,
)


def strip_external_banner(text: str | None) -> str | None:
    """Strip Outlook/M365 EXTERNAL/CAUTION banner text from the start of
    a message preview/body so downstream parsers don't ingest it as
    payload. No-op when no banner is present."""
    if not text or not isinstance(text, str):
        return text
    out = _BANNER_PREFIX_RX.sub("", text, count=1).lstrip(" \r\n\t.:-—")
    return out or None


def guess_teu_from_preview(preview: str | None) -> tuple[int, int, str | None]:
    """Parse '1-20' Oakland' / '2-40' HC Reefer' into (count, teu, canonical_str)."""
    if not preview:
        return 0, 0, None
    cleaned = strip_external_banner(preview) or preview
    count, teu = C.parse_teu(cleaned)
    m = re.search(
        r"(\d+)\s*[-x×]\s*(\d{2})[\'’]?\s*"
        r"(HC|RF|DV|GP|FR|OT|HC\s*Reefer|Reefer|Flex)?",
        cleaned, re.IGNORECASE,
    )
    canonical = None
    if m:
        qty, size, equip = m.group(1), m.group(2), (m.group(3) or "").strip()
        equip_norm = equip.upper().replace("  ", " ") if equip else ""
        if equip_norm == "HC":
            equip_norm = "HC"
        elif "REEF" in equip_norm:
            equip_norm = "HC Reefer"
        elif "FLEX" in equip_norm:
            equip_norm = "Flex"
        suffix = f" {equip_norm}" if equip_norm else ""
        canonical = f"{qty}-{size}'{suffix}".strip()
    return count, teu, canonical


def _etd_fit_days(eta_requested: str | None, eta_offered: str | None) -> int | None:
    if not eta_requested or not eta_offered:
        return None
    try:
        req = datetime.fromisoformat(eta_requested).date()
        off = datetime.fromisoformat(eta_offered).date()
        return (off - req).days
    except (ValueError, TypeError):
        return None


# ─────────────────────────────────────────────────────────────────────
# Graph fetch
# ─────────────────────────────────────────────────────────────────────

def _parse_all(text_body: str, subject: str, bucket: str) -> dict[str, Any]:
    """Run every body_parser parser applicable to this bucket.

    Mirrors the legacy ``scripts/fetch_bodies.py::_parse_all`` so downstream
    matchers (``apply_rate_responses`` etc.) see the same field shape.
    """
    out: dict[str, Any] = {
        "eta_requested": None,
        "etd_offered": None,
        "eta_offered": None,
        "origin_cutoff": None,
        "vessel_voyage": None,
        "transshipment": None,
        "rate_table": None,
        "send_signal": False,
        "origin": None,
        "destination": None,
    }
    text_body = text_body or ""

    origin, dest = BP.parse_subject_lane(subject or "")
    out["origin"] = origin
    out["destination"] = dest

    out["eta_requested"] = BP.parse_eta_requested(text_body)
    out["etd_offered"] = BP.parse_etd_offered(text_body)
    out["eta_offered"] = BP.parse_eta_offered(text_body)
    out["origin_cutoff"] = BP.parse_origin_cutoff(text_body)
    out["vessel_voyage"] = BP.parse_vessel(text_body)
    out["transshipment"] = BP.parse_transshipment(text_body)

    if bucket == "mbd_rate_response":
        rt = BP.parse_rate_table(text_body)
        out["rate_table"] = rt or None
        if rt:
            out["etd_offered"] = out["etd_offered"] or rt.get("etd")
            out["eta_offered"] = out["eta_offered"] or rt.get("eta")
            out["vessel_voyage"] = out["vessel_voyage"] or rt.get("vessel_voyage")
            out["transshipment"] = out["transshipment"] or rt.get("transshipment")

    if bucket == "lonny_reply":
        out["send_signal"] = BP.parse_send_signal(text_body)

    return out


def fetch_window(client: GraphClient, cfg: IngestConfig) -> list[dict[str, Any]]:
    """Fetch all in-scope messages for the configured window.

    Performs three Graph searches and merges into a single list of "row"
    dicts shaped like the legacy stage_emails.jsonl output (so the rest of
    the pipeline doesn't need to change):

      {imid, id, conversation_id, sent, subject, summary_preview,
       bucket, body_parsed, text_body, mdolx, is_hilmar}

    Search 1: messages where Lonny is sender (lonny_outbound, lonny_reply).
    Search 2: messages where Lonny is recipient (catches MBD→Lonny rate
              responses + booking confirms).
    Search 3: explicitly fetch from MBD shared mailbox to recipient=Lonny
              (overlap with #2 — dedup by id).
    """
    seen_ids: set[str] = set()
    all_meta: list[MessageMeta] = []

    # The two queries are not equally load-bearing. The sender query is the
    # primary signal source (Lonny -> Michael); the recipient query (Lonny
    # AS recipient on something Michael was CC'd on) is best-effort under
    # delegated auth — we don't have shared-mailbox visibility. If Graph
    # rejects the recipient query with InefficientFilter (lambda-over-
    # collection + date filter is borderline complex), keep going with
    # whatever the sender query returned rather than aborting the whole run.
    queries = (
        ("sender",    {"sender":    cfg.lonny_address, "after": cfg.window_start, "before": cfg.window_end}),
        ("recipient", {"recipient": cfg.lonny_address, "after": cfg.window_start, "before": cfg.window_end}),
    )
    for label, kwargs in queries:
        try:
            metas = client.search_messages(**kwargs)
        except Exception as e:  # noqa: BLE001 — defensive on transient Graph oddities
            if label == "sender":
                # Sender-side failure is fatal — that's where Lonny's
                # outbound rate requests come from. Re-raise.
                raise
            log.warning(
                "search_messages(%s=%s) failed (%s); continuing with sender-side results only",
                label, kwargs.get(label), e,
            )
            continue
        for meta in metas:
            if meta.id in seen_ids:
                continue
            seen_ids.add(meta.id)
            all_meta.append(meta)

    rows: list[dict[str, Any]] = []
    for meta in all_meta:
        bucket = classify_bucket(meta, cfg)
        if bucket is None:
            continue

        # Body fetch — required for body-parser enrichment.
        try:
            body: MessageBody | None = client.get_message_body(meta.id)
        except Exception as e:  # network / 404 / parse
            log.warning("body fetch failed for %s: %s", meta.id, e)
            body = None

        if body is None:
            text_body = ""
        elif body.body_content_type == "text":
            text_body = body.body or ""
        else:
            # HTML — strip via the same converter the legacy pipeline used.
            text_body = BP.html_to_text(body.body or "")
        parsed = _parse_all(text_body, meta.subject, bucket)

        rows.append({
            "imid": meta.internet_message_id or meta.id,
            "id": meta.id,
            "conversation_id": meta.conversation_id,
            "sent": (meta.sent_at or meta.received_at).isoformat(),
            "subject": meta.subject,
            "summary_preview": (body.body_preview if body else "")[:200],
            "bucket": bucket,
            "body_parsed": parsed,
            "text_body": text_body or "",
            "mdolx": extract_mdolx(meta.subject) or extract_mdolx(text_body),
            "is_hilmar": is_hilmar_subject(meta.subject) or "HILMAR" in (text_body or "").upper(),
            # Capture sender display name + address so apply_rate_responses
            # can identify which individual at MBD actually composed the
            # rate response. The shared-mailbox FROM_ADDRESS is always
            # MBD_OceanExportBookingShared@ol-usa.com, but the display
            # name often carries the individual's name (Outlook
            # "send-as" semantics).
            "from_name": meta.from_name or "",
            "from_address": meta.from_address or "",
        })

    return rows


# ─────────────────────────────────────────────────────────────────────
# Build requests / apply responses / link bookings / age
# (lifted unchanged-in-spirit from scripts/ingest.py — only the I/O
# shape changed; the model rules are identical.)
# ─────────────────────────────────────────────────────────────────────

def build_requests(lonny_out: list[dict[str, Any]]) -> list[dict[str, Any]]:
    requests: list[dict[str, Any]] = []
    skipped_ops = 0
    for row in lonny_out:
        sent = row.get("sent")
        subject = row.get("subject", "")
        preview = row.get("summary_preview", "")
        parsed = row.get("body_parsed") or {}

        # Drop ops/admin emails — they aren't rate asks and inflate the row
        # count with "Unknown" destinations / fake standalones. Audit fix
        # 2026-04-30 (port from scripts/ingest.py).
        if is_operational_subject(subject):
            skipped_ops += 1
            continue

        origin = clean_origin(subject)
        destination = clean_destination(subject) or parsed.get("destination")
        count, teu, containers = guess_teu_from_preview(preview)

        eta_requested = parsed.get("eta_requested")
        conv_id = row.get("conversation_id")

        rid = C.request_id(
            conv_id=row.get("imid"),
            request_ts=sent,
            destination=destination,
        )
        sent_dt = C.parse_iso(sent)
        # NB: status intentionally NOT set here. finalize_status() is the
        # only place status is assigned — it routes through core.decide_status.
        requests.append({
            "request_id": rid,
            "origin": origin,
            "destination": destination or "Unknown",
            "lane": f"{origin} → {destination or 'Unknown'}",
            "request_timestamp": sent,
            "request_date": sent_dt.date().isoformat() if sent_dt else None,
            "lonny_time_pt": C.fmt_pt(sent_dt) if sent_dt else None,
            "subject": subject,
            # Drop the `or preview` fallback that was here pre-fix —
            # when guess_teu_from_preview can't extract a clean spec,
            # storing the entire preview leaked CAUTION banners and
            # raw body text into the dashboard's "Containers" column.
            # Better to be honest and store None; QC has a heal step
            # that re-attempts on a later run.
            "containers": containers,
            "container_count": count,
            "teu_requested": teu,
            "ol_responder": OL_RESPONDER_NAME,
            "ol_responder_email": OL_RESPONDER_EMAIL,
            "ol_responder_signer": None,
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
            "mdolx_refs_all": [],
            "etd_requested": None,
            "etd_offered": None,
            "etd_fit_days": None,
            "eta_requested": eta_requested,
            "eta_offered": None,
            "vessel_voyage": None,
            "transshipment": None,
            "conversation_id": conv_id,
            "loss_reason": None,
            "reason_detail": "Pending match to response/booking",
            "status_history": [],
            "source_imids": [row.get("imid")],
            "source_ids": [row.get("id")],
        })
    return requests


def collect_bookings(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    bookings: dict[str, dict[str, Any]] = {}
    for row in rows:
        bucket = row.get("bucket")
        subject = row.get("subject", "") or ""
        preview = row.get("summary_preview", "") or ""

        is_hilmar = row.get("is_hilmar")
        if is_hilmar is None:
            is_hilmar = any(k in subject.upper() for k in ("HILMAR", "NUMIDIA"))
        if not is_hilmar:
            continue

        # Skip ops/admin follow-ups so they don't generate fake standalone
        # wins for MDOLX numbers that already represent live bookings
        # (e.g. FREE-TIME ISSUE, LOADING APPT). Audit fix 2026-04-30.
        if is_operational_subject(subject):
            continue

        mdolx = row.get("mdolx") or extract_mdolx(subject) or extract_mdolx(preview)
        if not mdolx:
            continue

        sent = row.get("sent")
        existing = bookings.get(mdolx)
        if not existing or (sent and (existing.get("sent") or "") > sent):
            # Best-effort carrier extraction from subject/preview so
            # standalone-booking rows (and matched rows whose rate
            # response hasn't landed yet) don't go to QC with no
            # carrier_won. Two-tier:
            #   1. parse_subject_carrier — understands the structured
            #      MDOLX trailer ("// MSC: EBKG...", "/ CMA: NAM...",
            #      "CMA BKG # NAM...", and bare booking-ref prefix
            #      "NAM12345" → CMA CGM). High-confidence positional
            #      match. (Audit fix 2026-04-30 port from scripts/ingest.py.)
            #   2. _find_carrier — bare-prose token search across the
            #      whole subject + preview. Catches "// EVERGREEN" with
            #      no booking-ref trailer.
            # Falls back to None when neither matches; finalize_status /
            # qc.phase_3 will still flag the WIN row in QC-002.
            carrier_guess = (
                BP.parse_subject_carrier(subject)
                or BP._find_carrier(f"{subject}\n{preview}")
            )
            if carrier_guess:
                carrier_guess = (
                    C.normalize_carrier(carrier_guess) or carrier_guess
                )
            bookings[mdolx] = {
                "mdolx": mdolx,
                "subject": subject,
                "sent": sent,
                "preview": preview,
                "carrier": carrier_guess,
                "source_bucket": bucket,
                "source_imid": row.get("imid"),
                "source_id": row.get("id"),
            }
    return bookings


_BOOKING_BODY_FIELDS = ("ol_rate", "eta_offered", "vessel_voyage", "transshipment")


def _fetch_booking_fields(
    client: GraphClient | None, source_id: str | None
) -> dict[str, Any] | None:
    """Best-effort: fetch the full body of a booking-confirmation email
    and extract the four fields the standalone path cares about —
    ``ol_rate`` (from the rate table) plus ``eta_offered``,
    ``vessel_voyage``, ``transshipment`` (from the booking-body parser).

    Returns a dict with all four keys (values may be None for fields the
    parsers couldn't extract) or None on any failure that prevented a
    fetch (no client, no source_id, network error). Failures NEVER raise
    — the daily run must not die on a best-effort backfill.
    """
    if client is None or not source_id:
        return None
    try:
        msg = client.get_message_body(source_id)
    except Exception as e:  # noqa: BLE001
        log.warning("booking-body fetch failed for %s: %s", source_id, e)
        return None
    body = msg.body or ""
    if msg.body_content_type == "html":
        body = BP.html_to_text(body)
    rate_parsed = BP.parse_rate_table(body)
    rate = rate_parsed.get("ol_rate")
    try:
        rate_f = float(rate) if rate is not None else None
    except (TypeError, ValueError):
        rate_f = None
    return {
        "ol_rate": rate_f,
        "eta_offered": BP.parse_eta_offered(body),
        "vessel_voyage": BP.parse_vessel(body),
        "transshipment": BP.parse_transshipment(body),
    }


def _fetch_container_spec(
    client: GraphClient | None, source_id: str | None
) -> tuple[str | None, int, int]:
    """Best-effort: fetch a source email body and extract a container
    spec. Returns ``(spec_str, container_count, teu)`` or
    ``(None, 0, 0)`` on any failure.

    Used to backfill ``containers`` on persisted Q&L / PENDING rows
    whose Lonny outbound subject was just "Oakland to <dest>" with no
    spec — the rate-response body almost always restates it.
    """
    if client is None or not source_id:
        return None, 0, 0
    try:
        msg = client.get_message_body(source_id)
    except Exception as e:  # noqa: BLE001
        log.warning("container-spec fetch failed for %s: %s", source_id, e)
        return None, 0, 0
    body = msg.body or ""
    if msg.body_content_type == "html":
        body = BP.html_to_text(body)
    spec = BP.parse_container_spec(body)
    if not spec:
        return None, 0, 0
    count, teu = C.parse_teu(spec)
    return spec, count, teu


def backfill_quoted_containers(
    merged_requests: list[dict[str, Any]],
    client: GraphClient | None,
    *,
    max_calls: int = 10,
) -> int:
    """Walk persisted Q&L / PENDING / WIN rows whose ``containers`` is
    missing and try to recover the spec from the source-email body.
    Returns the count of rows where containers was filled.

    Catches the gap where Lonny outbound subjects are bare
    "Oakland to <dest>" lines — preview-only extraction misses, and
    on the original ingest pass the rate-response body's container
    mention also missed (or the row was already promoted to quoted=
    True before the backfill code shipped). This function is idempotent
    and capped at ``max_calls`` per run so a backlog can't burn the
    Graph quota in one go.

    Best-effort: no client → no-op. Failures NEVER raise.
    """
    if client is None:
        return 0
    healed = 0
    for r in merged_requests:
        if healed >= max_calls:
            break
        if r.get("containers"):
            continue
        if r.get("status") not in ("Q&L", "PENDING", "WIN"):
            continue
        source_id = None
        sids = r.get("source_ids") or []
        if sids:
            source_id = sids[0]
        if not source_id:
            continue
        spec, count, teu = _fetch_container_spec(client, source_id)
        if spec is None:
            continue
        r["containers"] = spec
        if not r.get("container_count") and count:
            r["container_count"] = count
        if not r.get("teu_requested") and teu:
            r["teu_requested"] = teu
        healed += 1
        log.info(
            "backfilled containers=%r (count=%d, teu=%d) on %s",
            spec, count, teu, r.get("request_id"),
        )
    return healed


def backfill_standalone_rates(
    merged_requests: list[dict[str, Any]],
    client: GraphClient | None,
    *,
    max_calls: int = 10,
) -> int:
    """Walk persisted standalone WIN rows that are missing any of the
    booking-body fields (``ol_rate``, ``eta_offered``, ``vessel_voyage``,
    ``transshipment``) and try to fetch + parse the booking-confirmation
    body. Mutates rows in place; returns the number of rows where at
    least one field was newly populated.

    This catches stand_* rows that landed pre-fix (older code paths
    only extracted ``ol_rate``) — those rows fall outside the daily
    search window, so re-running ingest never re-touches them via the
    fresh path. Capped at ``max_calls`` per run so a backlog can't
    burn through the Graph quota in one go.

    Gate: skip if all four fields are already populated. A row whose
    body has been fetched but parsers couldn't extract some fields will
    be re-fetched on subsequent runs — bounded by ``max_calls`` and
    the small standalone-WIN population (~5 rows in prod), this is
    cheaper than tracking a per-row "tried" flag.

    Best-effort: no client → no-op. Failures NEVER raise.
    """
    if client is None:
        return 0
    healed = 0
    for r in merged_requests:
        if healed >= max_calls:
            break
        rid = r.get("request_id") or ""
        if not rid.startswith("stand_"):
            continue
        if r.get("status") != "WIN":
            continue
        if all(r.get(f) is not None for f in _BOOKING_BODY_FIELDS):
            continue
        source_id = None
        sids = r.get("source_ids") or []
        if sids:
            source_id = sids[0]
        if not source_id:
            continue
        fields = _fetch_booking_fields(client, source_id)
        if fields is None:
            continue
        filled_any = False
        for f in _BOOKING_BODY_FIELDS:
            v = fields.get(f)
            if v is not None and r.get(f) is None:
                r[f] = v
                filled_any = True
        if filled_any:
            healed += 1
            log.info(
                "backfilled booking-body fields on %s: %s",
                rid,
                {f: fields.get(f) for f in _BOOKING_BODY_FIELDS if fields.get(f) is not None},
            )
    return healed


def link_bookings_to_requests(
    requests: list[dict[str, Any]],
    bookings: dict[str, dict[str, Any]],
    *,
    client: GraphClient | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    by_lane: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in requests:
        by_lane[canonical_lane_key(r.get("destination"))].append(r)
    for lane_reqs in by_lane.values():
        lane_reqs.sort(key=lambda r: r.get("request_timestamp") or "")

    matched_mdolx: set[str] = set()

    for mdolx, bk in bookings.items():
        bk_ts = C.parse_iso(bk.get("sent"))
        if not bk_ts:
            continue

        raw_subj = bk.get("subject", "") or ""
        _, dest_guess = BP.parse_subject_lane(raw_subj)
        if not dest_guess:
            subj_upper = raw_subj.upper()
            m = re.search(r"HILMAR\s*[\->]+\s*([A-Z][A-Z\s]+?)(?:\s*//|$)", subj_upper)
            if m:
                dest_guess = m.group(1).strip().title()
            if not dest_guess:
                m = re.search(r"\bTO\s+([A-Z][A-Za-z\s()]+?)(?:\s*//|\s*\d|$)", subj_upper)
                if m:
                    dest_guess = m.group(1).strip().title()

        candidates: list[dict[str, Any]] = []
        if dest_guess:
            candidates = by_lane.get(canonical_lane_key(dest_guess), [])
        if not candidates:
            subj_upper = raw_subj.upper()
            for lane_key, lane_reqs in by_lane.items():
                if lane_key != "unknown" and lane_key.upper() in subj_upper:
                    candidates.extend(lane_reqs)

        best: dict[str, Any] | None = None
        for r in candidates:
            if r.get("mdolx_ref"):
                continue
            req_ts = C.parse_iso(r.get("request_timestamp"))
            if not req_ts or req_ts > bk_ts:
                continue
            if (bk_ts - req_ts) > timedelta(days=10):
                continue
            best_ts = C.parse_iso(best["request_timestamp"]) if best else None
            if best is None or (best_ts and req_ts > best_ts):
                best = r

        if best is None:
            continue

        # Set INPUT FIELDS only — status is computed later by finalize_status.
        best["has_send"] = True
        best["mdolx_ref"] = mdolx
        best["mdolx_refs_all"] = sorted(set(best.get("mdolx_refs_all") or []) | {mdolx})
        # Prefer the rate-response carrier_quoted (extracted from the actual
        # MBD reply). Fall back to the carrier guessed from the booking
        # subject/preview when the rate response hasn't been parsed yet
        # (e.g. ingestion split across two daily runs). Pre-cutover this
        # always blanked carrier_won when carrier_quoted was None — that
        # produced QC-002 false errors on legitimate WINs whose only
        # carrier signal was in the booking confirmation.
        best["carrier_won"] = best.get("carrier_quoted") or bk.get("carrier")
        best["booking_timestamp"] = bk.get("sent")

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

        best["reason_detail"] = (
            f"Linked to MDOLX{mdolx} booking ({bk.get('source_bucket')})"
        )
        best["teu_won"] = best.get("teu_requested", 0)
        # If the matched request never had a rate-quote email body
        # parsed (LOSS-then-WIN-via-MDOLX promoted case), the
        # booking-confirmation body usually carries rate + ETA / vessel
        # / transshipment in one shot. Use ol_rate-is-None as the gate
        # — quote-stage parsing fills ol_rate alongside the other
        # fields, so its absence is the reliable signal that no quote
        # body was ever parsed for this row. When the gate fires, fill
        # all four fields from the single fetch. Never overwrites
        # quote-stage values.
        if not best.get("ol_rate"):
            promoted = _fetch_booking_fields(client, bk.get("source_id"))
            if promoted is not None:
                filled = {}
                for f in _BOOKING_BODY_FIELDS:
                    v = promoted.get(f)
                    if v is not None and best.get(f) is None:
                        best[f] = v
                        filled[f] = v
                if filled:
                    log.info("backfilled %s on %s via matched booking %s",
                             filled, best.get("request_id"), mdolx)
        # Note the booking event in the audit trail; status_history transition
        # entry is added by finalize_status when decide_status flips the value.
        best.setdefault("booking_events", []).append({
            "at": bk.get("sent"),
            "mdolx": mdolx,
            "source_bucket": bk.get("source_bucket"),
            "source_id": bk.get("source_id"),
        })
        matched_mdolx.add(mdolx)

    standalones: list[dict[str, Any]] = []
    for mdolx, bk in bookings.items():
        if mdolx in matched_mdolx:
            continue
        bk_ts_iso = bk.get("sent")
        bk_ts = C.parse_iso(bk_ts_iso)
        raw_subj = bk.get("subject", "") or ""
        s_origin, s_dest = BP.parse_subject_lane(raw_subj)
        if not s_dest:
            # Mirrors scripts/ingest.py (2026-07-09 "Lane unresolved" fix):
            # when the subject carries no lane shape, the booking BODY usually
            # names the discharge port (destination, else POD).
            _s_bp = bk.get("body_parsed") or {}
            s_dest = _s_bp.get("destination") or _s_bp.get("pod")
        s_origin = s_origin or "Oakland"
        s_dest = s_dest or "Unknown"
        lane = f"{s_origin} → {s_dest}" if s_dest != "Unknown" else "Lane unresolved"
        # Recover containers + TEU from the booking subject. Pre-fix,
        # standalones landed with teu_won=0 / containers=None and
        # contributed nothing to the trade-region TEU/value-won columns
        # — even though the subject usually carries the spec verbatim
        # ("…HILMAR 1X20'DV Oakland to Bangkok//…"). Subject is the
        # reliable surface; preview is < 200 chars and unreliable.
        containers_str = BP.parse_container_spec_from_subject(raw_subj)
        if containers_str:
            container_count, teu = C.parse_teu(containers_str)
        else:
            container_count, teu = 0, 0
        # Recover ol_rate + ETA / vessel / transshipment by fetching the
        # full booking-confirmation body over Graph. Best-effort: no
        # client (tests) or fetch failure leaves all four fields None
        # and the email's value-won column shows "n/a" per PR #40. When
        # the booking body does contain these fields, both the
        # value-won column AND the QC-008 booking-confirmation parser
        # signals light up correctly.
        booking_fields = _fetch_booking_fields(client, bk.get("source_id")) or {}
        # Standalone booking — no Lonny request matched. Set INPUT FIELDS only;
        # finalize_status flips this to WIN via decide_status (has_send=True
        # AND mdolx_ref set => WIN).
        standalones.append({
            "request_id": f"stand_{mdolx}",
            "origin": s_origin,
            "destination": s_dest,
            "lane": lane,
            "request_timestamp": None,
            "request_date": bk_ts.date().isoformat() if bk_ts else None,
            "lonny_time_pt": None,
            "subject": bk.get("subject"),
            "containers": containers_str,
            "container_count": container_count,
            "teu_requested": teu,
            "teu_won": teu,
            "ol_responder": OL_RESPONDER_NAME,
            "ol_responder_email": OL_RESPONDER_EMAIL,
            "quoted": True,
            "has_send": True,
            "mdolx_ref": mdolx,
            "mdolx_refs_all": [mdolx],
            "carrier_quoted": bk.get("carrier"),
            "carrier_won": bk.get("carrier"),
            "ol_rate": booking_fields.get("ol_rate"),
            "eta_offered": booking_fields.get("eta_offered"),
            "vessel_voyage": booking_fields.get("vessel_voyage"),
            "transshipment": booking_fields.get("transshipment"),
            "response_timestamp": bk_ts_iso,
            "olusa_time_et": C.fmt_et(bk_ts) if bk_ts else None,
            "loss_reason": None,
            "reason_detail": (
                f"Standalone booking (pre-window request) — MDOLX{mdolx}, "
                "no Lonny ask found in 30-day window"
            ),
            "status_history": [],
            "source_imids": [bk.get("source_imid")],
            "source_ids": [bk.get("source_id")],
        })
    return requests, standalones


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


def apply_rate_responses(
    requests: list[dict[str, Any]],
    rate_rsps: list[dict[str, Any]],
    *,
    fallback_ctx: Any = None,
) -> int:
    """Match rate responses (MBD → Lonny) to their originating Lonny
    rate requests by lane + time window, then apply the parsed fields.

    The optional ``fallback_ctx`` is a
    :class:`hilmar.parser_fallback.ParserFallbackContext`. When provided,
    fields that came back None from the regex parsers are LLM-extracted
    via ``parser_fallback.extract_with_fallback``. Cached + budget-capped;
    safe to pass on every run. None disables the fallback (regex-only).
    """
    quoted_count = 0
    by_lane: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in requests:
        by_lane[canonical_lane_key(r.get("destination"))].append(r)
    for lane_reqs in by_lane.values():
        lane_reqs.sort(key=lambda r: r.get("request_timestamp") or "")

    rsps_sorted = sorted(rate_rsps, key=lambda rr: rr.get("sent") or "")

    for rr in rsps_sorted:
        dest = clean_destination(rr.get("subject", ""))
        if not dest:
            continue
        sent = rr.get("sent")
        sent_dt = C.parse_iso(sent)
        if not sent_dt:
            continue

        candidates = by_lane.get(canonical_lane_key(dest), [])
        best: dict[str, Any] | None = None
        for r in candidates:
            if r.get("quoted"):
                continue
            req_dt = C.parse_iso(r.get("request_timestamp"))
            if not req_dt or req_dt > sent_dt:
                continue
            if (sent_dt - req_dt) > timedelta(days=10):
                continue
            best_dt = C.parse_iso(best["request_timestamp"]) if best else None
            if best is None or (best_dt and req_dt > best_dt):
                best = r

        if best is None:
            continue

        parsed = rr.get("body_parsed") or {}
        rt = parsed.get("rate_table") or {}
        body_text = rr.get("text_body") or ""
        carrier_norm = (
            C.normalize_carrier(rt.get("carrier_quoted"))
            if rt.get("carrier_quoted") else None
        )
        req_dt = C.parse_iso(best.get("request_timestamp"))

        # LLM-fallback extraction for fields the regex missed. No-op
        # when fallback_ctx is None (default — regex-only). When
        # provided, each missed field gets one Haiku call (cached +
        # budget-capped). The miss log under data/parser_misses.jsonl
        # builds the test-fixture queue.
        ol_rate = rt.get("ol_rate")
        etd_offered = rt.get("etd") or parsed.get("etd_offered")
        eta_offered = rt.get("eta") or parsed.get("eta_offered")
        vessel_voyage = rt.get("vessel_voyage") or parsed.get("vessel_voyage")
        transshipment = rt.get("transshipment") or parsed.get("transshipment")
        if fallback_ctx is not None and body_text:
            from . import parser_fallback as _pf
            ol_rate = _pf.extract_with_fallback("ol_rate", body_text, ol_rate, ctx=fallback_ctx)
            if not carrier_norm:
                carrier_norm = C.normalize_carrier(_pf.extract_with_fallback(
                    "carrier_quoted", body_text, None, ctx=fallback_ctx,
                )) or carrier_norm
            etd_offered = _pf.extract_with_fallback("etd_offered", body_text, etd_offered, ctx=fallback_ctx)
            eta_offered = _pf.extract_with_fallback("eta_offered", body_text, eta_offered, ctx=fallback_ctx)
            vessel_voyage = _pf.extract_with_fallback("vessel_voyage", body_text, vessel_voyage, ctx=fallback_ctx)
            transshipment = _pf.extract_with_fallback("transshipment", body_text, transshipment, ctx=fallback_ctx)

        # Identify the individual at MBD who composed this rate response.
        # Three-tier fallback:
        #   1. message ``from_name`` (Outlook display name on send-as);
        #      reject if it equals the shared-mailbox label.
        #   2. body-signature parser (parse_signer walks the closing block).
        #   3. LLM fallback if a fallback_ctx is supplied.
        # Result lands in ol_responder_signer (canonical column already
        # in the row schema) — ol_responder stays as the shared-mailbox
        # identity so Lonny still sees a consistent "from" everywhere.
        signer = (rr.get("from_name") or "").strip()
        # Reject any string that's CLEARLY the shared-mailbox display
        # name, in any form. The pre-fix check only matched the exact
        # "MBD Ocean Export Booking" string — but Outlook emits
        # "MBD Ocean Export Booking (Shared)", "MBD_OceanExportBooking",
        # and others depending on the send-as path. Substring contains
        # is correct here: a real human signer would never include
        # "MBD Ocean Export" or "(Shared)" in their name.
        if _is_shared_mailbox_label(signer):
            signer = ""
        if not signer and body_text:
            sig_candidate = BP.parse_signer(body_text) or ""
            if not _is_shared_mailbox_label(sig_candidate):
                signer = sig_candidate
        if not signer and fallback_ctx is not None and body_text:
            from . import parser_fallback as _pf
            llm_candidate = _pf.extract_with_fallback(
                "ol_responder_signer", body_text, None, ctx=fallback_ctx,
            ) or ""
            if not _is_shared_mailbox_label(llm_candidate):
                signer = llm_candidate

        best["quoted"] = True
        best["carrier_quoted"] = carrier_norm
        best["ol_rate"] = ol_rate
        best["response_timestamp"] = sent
        best["olusa_time_et"] = C.fmt_et(sent_dt)
        best["etd_offered"] = etd_offered
        best["eta_offered"] = eta_offered
        best["vessel_voyage"] = vessel_voyage
        best["transshipment"] = transshipment
        # Container-spec recovery from the rate-response body. Lonny
        # outbound subjects are often just "Oakland to <dest>" with no
        # container spec, and the email preview may also be too short
        # to carry one — those rows landed with containers=None /
        # teu_requested=0, which then dragged the lane-level TEU
        # totals. The MBD rate-response body almost always restates
        # the spec ("for your 1x40HC to Bangkok…") so mine it here as
        # a best-effort backfill. Only fills when currently missing.
        if not best.get("containers") and body_text:
            recovered_spec = BP.parse_container_spec(body_text)
            if recovered_spec:
                best["containers"] = recovered_spec
                rec_count, rec_teu = C.parse_teu(recovered_spec)
                if rec_count and not best.get("container_count"):
                    best["container_count"] = rec_count
                if rec_teu and not best.get("teu_requested"):
                    best["teu_requested"] = rec_teu
                log.info(
                    "Recovered containers=%r from rate-response body for %s",
                    recovered_spec, best.get("request_id"),
                )
        if signer:
            best["ol_responder_signer"] = signer
        best["rate_expiry"] = rt.get("rate_expiry")
        best["detention_free"] = rt.get("detention_free")
        best["demurrage_free"] = rt.get("demurrage_free")
        best["etd_fit_days"] = _etd_fit_days(best.get("eta_requested"), best.get("eta_offered"))
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
            "to": "QUOTED",
            "reason": f"MBD rate response — carrier={carrier_norm}, rate={rt.get('ol_rate')}",
        })
        best.setdefault("source_imids", []).append(rr.get("imid"))
        best.setdefault("source_ids", []).append(rr.get("id"))
        quoted_count += 1

    return quoted_count


def apply_send_signals(
    requests: list[dict[str, Any]],
    lonny_replies: list[dict[str, Any]],
) -> int:
    promotions = 0
    by_lane: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in requests:
        by_lane[canonical_lane_key(r.get("destination"))].append(r)
    for lane_reqs in by_lane.values():
        lane_reqs.sort(key=lambda r: r.get("request_timestamp") or "")

    for row in lonny_replies:
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
        candidates = by_lane.get(canonical_lane_key(dest), [])
        best: dict[str, Any] | None = None
        for r in candidates:
            # Skip already-matched-to-MDOLX rows; finalize_status will WIN them.
            if r.get("mdolx_ref"):
                continue
            req_dt = C.parse_iso(r.get("request_timestamp"))
            if not req_dt or req_dt > sent_dt:
                continue
            if (sent_dt - req_dt) > timedelta(days=5):
                continue
            best_dt = C.parse_iso(best["request_timestamp"]) if best else None
            if best is None or (best_dt and req_dt > best_dt):
                best = r
        if best is None:
            continue
        # Set INPUT FIELDS only — status flows through finalize_status.
        best["quoted"] = True
        best["has_send"] = True
        # Inherit carrier_won from carrier_quoted (rate-response body)
        # when present.
        if not best.get("carrier_won") and best.get("carrier_quoted"):
            best["carrier_won"] = best["carrier_quoted"]
        # If still missing, look back at the most recent quoted SIBLING on
        # the same canonical lane within 30 days — Lonny's "send" usually
        # references the last rate OL gave on that lane, even if the rate
        # response email itself didn't survive in the corpus. Audit fix
        # 2026-04-30 (port from scripts/ingest.py): drops missing-carrier
        # WINs from 6 → 1 in the production corpus.
        if not best.get("carrier_won"):
            fallback_carrier = None
            for sib in candidates:
                if sib is best or not sib.get("carrier_quoted"):
                    continue
                sib_dt = C.parse_iso(
                    sib.get("response_timestamp") or sib.get("request_timestamp")
                )
                if not sib_dt or sib_dt > sent_dt:
                    continue
                if (sent_dt - sib_dt) > timedelta(days=30):
                    continue
                fallback_carrier = sib.get("carrier_quoted")
            if fallback_carrier:
                best["carrier_won"] = fallback_carrier
                best["carrier_quoted"] = fallback_carrier
        # Last resort: substring-prefix fallback on the canonical lane
        # key. Lanes "hcmc (cai mep)" and "hcmc (cat lai)" share the
        # prefix "hcmc"; "manila (north)" / "manila (south)" share
        # "manila". Conservative — only inherits from a sibling within
        # 30 days. Closes off-channel-rate cases where the rate-response
        # email was never in the corpus but a recent prior quote on a
        # sibling sub-lane carried the carrier.
        if not best.get("carrier_won"):
            best_dest_key = canonical_lane_key(best.get("destination"))
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
                    sib_dt = C.parse_iso(
                        r2.get("response_timestamp") or r2.get("request_timestamp")
                    )
                    if not sib_dt or sib_dt > sent_dt:
                        continue
                    if (sent_dt - sib_dt) > timedelta(days=30):
                        continue
                    fallback_carrier = r2.get("carrier_quoted")
                if fallback_carrier:
                    best["carrier_won"] = fallback_carrier
                    best["carrier_quoted"] = fallback_carrier
        best["reason_detail"] = (
            (best.get("reason_detail") or "") + f" | Lonny Send reply {sent[:10]}"
        )
        best["teu_won"] = best.get("teu_requested", 0)
        best.setdefault("send_signal_events", []).append({
            "at": sent,
            "source": "lonny_reply",
        })
        promotions += 1
    return promotions


def finalize_status(
    requests: Iterable[dict[str, Any]],
    now: datetime | None = None,
) -> None:
    """The ONLY place ingest sets request status. Routes everything through
    :func:`core.decide_status` and uses :func:`core.record_transition` so any
    actual change appends a status_history row.

    No literal status-string assignment lives anywhere else in this module —
    see ``test_ingest.py::test_no_direct_status_string_assignment_in_module``
    which greps the source for the forbidden pattern.
    """
    now = now or C.now_utc()
    # Materialize so we can pre-compute lane winning medians for the
    # PRICE-vs-UNDIFFERENTIATED determination (decide_status 2026-06-02).
    rows = list(requests)
    lane_winning_median = C.compute_lane_winning_medians(rows)
    for r in rows:
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
        # record_transition only mutates if status actually changed; the
        # assignment inside record_transition uses a variable, not a literal,
        # so the grep-test for direct literal assignment still passes.
        C.record_transition(r, decision.status, decision.reason_detail, at=now)
        r["loss_reason"] = decision.loss_reason
        prior = r.get("reason_detail") or ""
        if not prior or "Pending match" in prior:
            r["reason_detail"] = decision.reason_detail


# Backward-compat alias — older code/tests may still call age_requests.
age_requests = finalize_status


# ─────────────────────────────────────────────────────────────────────
# Idempotent merge with existing tracking-data-v2.json
# ─────────────────────────────────────────────────────────────────────

# Fields that are AUTO-derived per run — overwriting them is fine.
#
# The principle: anything ingest extracts FROM RAW EMAIL CONTENT on every
# pass should be recomputed; anything a human OR QC adds (notes, audit
# annotations, source_imids history) should be preserved.
#
# Production bug 2026-04-27 motivated the rate-response and WIN-signal
# additions below: a Lonny outbound row created on Day-N-1 (no rate
# response yet → quoted=False) was persisted. On Day-N the rate response
# arrived and apply_rate_responses set quoted=True / carrier_quoted=MSC
# in fresh memory. Without these fields in _RECOMPUTED_FIELDS, the merge
# preserved the stale Day-N-1 quoted=False, then qc.phase_3_entries' "NQ
# contamination cleanup" branch fired (because status==LOSS AND
# !quoted), CLEARING carrier_quoted back to None — even though
# reason_detail already said "Rate responded by MBD ... MSC @ $540". 26
# rows landed in this corrupted state in the cowork run.
_RECOMPUTED_FIELDS = frozenset({
    # Status / loss + display strings
    "status", "loss_reason", "reason_detail",
    # Lane / mailbox identity (re-derived from subject every run)
    "lane",
    "ol_responder", "ol_responder_email",
    # Individual at MBD who composed the rate response — re-extracted
    # every run from the source email's from_name + body signature so a
    # better signal on a later run clobbers a stale one.
    "ol_responder_signer",
    # Timestamps in Michael-friendly TZ (clock walk, not wall clock)
    "request_date", "lonny_time_pt", "olusa_time_et",
    "turnaround_biz_hours", "turnaround_hours", "etd_fit_days",
    # Rate-response signals — extracted by apply_rate_responses from the
    # MBD shared mailbox reply. Were stuck stale on disk pre-fix.
    "quoted", "response_timestamp",
    "carrier_quoted", "ol_rate",
    "etd_offered", "eta_offered",
    "vessel_voyage", "transshipment",
    "rate_expiry", "detention_free", "demurrage_free",
    # WIN signals — extracted by link_bookings_to_requests +
    # apply_send_signals. Same staleness risk as the rate-response set.
    "has_send", "mdolx_ref", "mdolx_refs_all",
    "carrier_won", "booking_timestamp", "teu_won",
})


def _merge_status_history(old, new):
    """Union two status_history lists, deduped and ordered by `at`.

    status_history is an append-only LOG, so neither merge rule fits it:
    "preserve existing" drops transitions the fresh run discovered, and
    "recompute" (adding it to _RECOMPUTED_FIELDS) would throw away every
    transition ever accumulated, since a freshly-built row starts empty.
    Union is the only rule that keeps the log honest.

    Dedup collapses CONSECUTIVE entries sharing (from, to, reason), keeping
    the earliest. Deduping on `at` is not enough and caught a real regression
    in tests/test_ingest.py: a fresh run re-derives the same transition with a
    NEW timestamp, so an exact-match key let every daily fire append another
    copy of "None → WIN" forever. Requiring ADJACENCY rather than global
    uniqueness is deliberate — a row that genuinely goes WIN → LOSS → WIN
    keeps both WIN entries, so `[-1]["to"]` stays the true current state,
    which is the invariant QC-072 checks.

    Entries with no parseable `at` sort last but are never dropped — a
    transition we cannot date is still a transition that happened.
    """
    combined = [e for e in (list(old or []) + list(new or []))
                if isinstance(e, dict)]
    combined.sort(key=lambda e: (e.get("at") is None, e.get("at") or ""))
    out = []
    for e in combined:
        key = (e.get("from"), e.get("to"), e.get("reason"))
        if out and (out[-1].get("from"), out[-1].get("to"),
                    out[-1].get("reason")) == key:
            continue
        out.append(e)
    return out


def merge_idempotent(
    existing: list[dict[str, Any]] | None,
    fresh: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Merge ``fresh`` into ``existing``, keying on ``request_id``.

    Rules:
      * New request_id → append.
      * Existing request_id → for each field in fresh:
          - if existing's value is None / missing → take fresh's value.
          - if field is in _RECOMPUTED_FIELDS → take fresh's value (it's
            derived per run and may legitimately update).
          - otherwise → keep existing's value (don't clobber human edits
            or QC heals).
      * Preserve any existing keys not present in fresh (e.g., QC-added
        annotations).
    """
    if not existing:
        return list(fresh)

    by_id: dict[str, dict[str, Any]] = {}
    for r in existing:
        rid = r.get("request_id")
        if rid:
            by_id[rid] = r

    out: list[dict[str, Any]] = []
    seen_fresh_ids: set[str] = set()

    for new_r in fresh:
        rid = new_r.get("request_id")
        if not rid:
            out.append(new_r)
            continue
        seen_fresh_ids.add(rid)
        old_r = by_id.get(rid)
        if old_r is None:
            out.append(new_r)
            continue
        merged = dict(old_r)  # start from existing
        for k, v in new_r.items():
            old_v = merged.get(k)
            if old_v is None or old_v == "" or k in _RECOMPUTED_FIELDS:
                merged[k] = v
            # else preserve existing
        # status_history is neither "recompute" nor "preserve" — it is a LOG,
        # so it UNIONS. `status` is in _RECOMPUTED_FIELDS and the history is
        # not, which left the merged row asserting one outcome in `status` and
        # a different one in `status_history[-1]["to"]` — and the history is
        # the field schema.json declares as the transition record, so audits,
        # the dashboard timeline and Sentry triage all read the stale answer.
        # Union by (at, from, to) so re-ingesting the same run is idempotent,
        # then order by `at` so [-1] is genuinely the latest transition.
        merged["status_history"] = _merge_status_history(
            old_r.get("status_history"), new_r.get("status_history"))
        if not merged["status_history"]:
            merged.pop("status_history", None)
        out.append(merged)

    # Carry forward existing rows not in fresh (e.g. older window survivors).
    for rid, old_r in by_id.items():
        if rid not in seen_fresh_ids:
            out.append(old_r)

    return out


# ─────────────────────────────────────────────────────────────────────
# Main entry
# ─────────────────────────────────────────────────────────────────────

def _build_doc(
    client: GraphClient,
    cfg: IngestConfig,
    existing_doc: dict[str, Any],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Compute the merged tracking-data document — pure function over
    ``existing_doc`` + Graph contents. Does NOT write to disk.
    """
    rows = fetch_window(client, cfg)

    lonny_out = [r for r in rows if r.get("bucket") == "lonny_outbound"]
    lonny_reply = [r for r in rows if r.get("bucket") == "lonny_reply"]
    rate_rsps = [r for r in rows if counts_as_rate_response(r)]

    requests = build_requests(lonny_out)
    # Initialize the LLM-fallback context once per run (cache + miss log
    # live alongside tracking-data; budget honors HILMAR_PARSER_FALLBACK_
    # BUDGET, default 20 calls/run). When HILMAR_PARSER_FALLBACK_DISABLE
    # is truthy, ctx stays None and apply_rate_responses runs regex-only
    # — used by tests + CI to avoid network calls.
    fallback_ctx = None
    if not os.environ.get("HILMAR_PARSER_FALLBACK_DISABLE"):
        try:
            from . import parser_fallback as _pf
            from . import paths as _paths
            fallback_ctx = _pf.ParserFallbackContext.from_data_dir(_paths.data_dir())
        except Exception as e:  # noqa: BLE001
            log.warning("parser_fallback init failed (%s) — running regex-only", e)
    apply_rate_responses(requests, rate_rsps, fallback_ctx=fallback_ctx)
    # Persist whatever LLM extractions happened this run so tomorrow's
    # cache hit avoids the LLM call entirely.
    if fallback_ctx is not None:
        try:
            fallback_ctx.persist_cache()
            log.info(
                "parser_fallback: %d LLM calls made, %d budget skips, cache size %d",
                fallback_ctx.calls_made, fallback_ctx.skips, len(fallback_ctx.cache),
            )
        except Exception as e:  # noqa: BLE001
            log.warning("parser_cache persist failed (%s) — non-fatal", e)
    bookings = collect_bookings(rows)
    requests, standalones = link_bookings_to_requests(requests, bookings, client=client)
    apply_send_signals(requests, lonny_reply)
    fresh_requests = requests + standalones

    # Finalizer: backfill carrier_quoted from carrier_won when we know
    # the winning carrier (via parse_subject_carrier on the booking
    # subject) but the rate-response email never landed in our corpus.
    # The MDOLX booking IS the quote+book in one shot — when we know
    # carrier_won, the same carrier was implicitly quoted. Audit fix
    # 2026-05-01 (port from scripts/ingest.py).
    cross_filled = 0
    for r in fresh_requests:
        if r.get("carrier_won") and not r.get("carrier_quoted"):
            r["carrier_quoted"] = r["carrier_won"]
            cross_filled += 1
    if cross_filled:
        log.info("carrier_quoted backfill from carrier_won: %d rows", cross_filled)

    finalize_status(fresh_requests, now=now)

    merged_requests = merge_idempotent(existing_doc.get("requests"), fresh_requests)
    # Backfill ol_rate on persisted standalone WINs that landed pre-fix
    # (their MDOLX fell outside the search window today, so the fresh
    # path never re-touched them). Best-effort, capped per run.
    backfill_standalone_rates(merged_requests, client)
    # Backfill containers on persisted rows where the Lonny outbound
    # subject was just "Oakland to <dest>" with no spec — those landed
    # with containers=None and dragged TEU totals. The rate-response
    # body almost always restates the spec; recover it. Best-effort,
    # capped per run.
    backfill_quoted_containers(merged_requests, client)

    # Phase A invariant (commit refactor/aggregates-single-writer):
    # ingest writes RAW shape only — `requests` plus immutable metadata.
    # Aggregates (`summary` / `lanes` / `carriers`) are derived exclusively
    # by ``qc.phase_5_summaries`` AFTER ``phase_3_entries`` has done its
    # healing. Pre-Phase-A, ingest computed aggregates that QC then
    # immediately overwrote — wasted work, and a drift trap whenever any
    # consumer read between the two writes (cf. PR #3 carrier-list
    # staleness, PR #1 orchestrator/QC win-count drift).
    return {
        "version": "8.0-aggregates-post-qc",
        "generated_at": (now or datetime.now(timezone.utc)).isoformat(),
        "date_range": {
            "start": cfg.window_start.date().isoformat(),
            "end": cfg.window_end.date().isoformat(),
        },
        "requests": merged_requests,
        "notes": {
            "ingest_model": (
                "Lonny outbound = request. MDOLX Hilmar booking = win. "
                "Rates-desk emails (Caren, MBD_Export_Pricing) excluded."
            ),
            "ol_responder_rule": "Always the MBD_OceanExportBookingShared mailbox identity.",
            "auth": "MSAL device-code on Michael's mailbox (delegated).",
        },
    }


def _load_existing(data_path: Path) -> dict[str, Any]:
    if not data_path.exists():
        return {}
    try:
        return json.loads(data_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        log.warning("existing data file %s did not parse — treating as empty", data_path)
        return {}


def run_ingest(
    *,
    client: GraphClient,
    data_path: Path,
    window_start: datetime,
    window_end: datetime,
    cfg: IngestConfig | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """End-to-end ingest. Computes + writes the merged tracking-data doc.

    Caller is responsible for backup before calling — see ``hilmar.backup``.
    For preview-only behavior, use :func:`run_ingest_dry_diff`.
    """
    cfg = cfg or IngestConfig.from_env(window_start=window_start, window_end=window_end)
    existing_doc = _load_existing(data_path)
    output = _build_doc(client, cfg, existing_doc, now=now)
    data_path.parent.mkdir(parents=True, exist_ok=True)
    data_path.write_text(
        json.dumps(output, indent=2, ensure_ascii=False), encoding="utf-8",
    )
    return output


@dataclass
class IngestDryDiff:
    """Read-only diff — what ``run_ingest`` *would* change if invoked.

    Shipped as the answer to the user's gate (d): never silently overwrite
    ``../tracking-data-v2.json`` until Michael has eyeballed the diff.
    """
    requests_added: list[str]              # request_ids new in fresh
    requests_changed: list[dict[str, Any]] # {request_id, field, before, after}
    requests_unchanged: int
    summary_before: dict[str, Any]
    summary_after: dict[str, Any]

    def is_empty(self) -> bool:
        return not self.requests_added and not self.requests_changed


def run_ingest_dry_diff(
    *,
    client: GraphClient,
    data_path: Path,
    window_start: datetime,
    window_end: datetime,
    cfg: IngestConfig | None = None,
    now: datetime | None = None,
) -> IngestDryDiff:
    """Compute what ``run_ingest`` would write — but don't touch the file.

    Returns an :class:`IngestDryDiff` describing added/changed requests
    so the caller can review before flipping to the write path.
    """
    cfg = cfg or IngestConfig.from_env(window_start=window_start, window_end=window_end)
    existing_doc = _load_existing(data_path)
    fresh_doc = _build_doc(client, cfg, existing_doc, now=now)

    before_by_id: dict[str, dict[str, Any]] = {
        r.get("request_id"): r for r in (existing_doc.get("requests") or [])
        if r.get("request_id")
    }
    after_by_id: dict[str, dict[str, Any]] = {
        r.get("request_id"): r for r in (fresh_doc.get("requests") or [])
        if r.get("request_id")
    }

    added: list[str] = []
    changed: list[dict[str, Any]] = []
    unchanged = 0
    for rid, after in after_by_id.items():
        before = before_by_id.get(rid)
        if before is None:
            added.append(rid)
            continue
        per_field: list[tuple[str, Any, Any]] = []
        for k, v_after in after.items():
            v_before = before.get(k)
            if v_before != v_after:
                per_field.append((k, v_before, v_after))
        if per_field:
            for field, b, a in per_field:
                changed.append({
                    "request_id": rid, "field": field,
                    "before": b, "after": a,
                })
        else:
            unchanged += 1

    # Post-Phase-A: ingest no longer pre-computes summary, so derive both
    # sides on the fly from their respective request lists. Same
    # ``core.aggregate_summary`` function the QC pass uses, so dry-diff
    # is comparing apples-to-apples regardless of when the prior data
    # was last QC'd.
    return IngestDryDiff(
        requests_added=added,
        requests_changed=changed,
        requests_unchanged=unchanged,
        summary_before=(
            existing_doc.get("summary")
            or C.aggregate_summary(existing_doc.get("requests") or [])
        ),
        summary_after=C.aggregate_summary(fresh_doc.get("requests") or []),
    )


def main() -> int:
    """Console-script entrypoint registered as ``hilmar-ingest``."""
    import argparse

    ap = argparse.ArgumentParser(description="Hilmar Graph-backed ingest")
    ap.add_argument("--data", type=Path, required=True, help="tracking-data-v2.json path")
    ap.add_argument("--days-back", type=int, default=14, help="window size in days")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=args.days_back)

    client = GraphClient()
    client.authenticate(interactive_ok=False)
    out = run_ingest(client=client, data_path=args.data, window_start=start, window_end=end)
    print(f"Wrote {args.data} — {len(out['requests'])} requests, summary={out['summary']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
