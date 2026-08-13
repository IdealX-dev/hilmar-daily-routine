"""
patch_carriers.py — Enrichment: fill carrier_won + lane on WIN rows that landed
without carrier attribution.

Two-stage strategy:
  1. AUTO-DISCOVERY (added 2026-05-07 per Michael 'handle all suggestions'):
     For each WIN missing carrier, scan stage_emails.txt for ALL emails
     matching the MDOLX ref. Try parse_subject_carrier on each subject —
     a single MDOLX often has multiple emails (PLEASE UPDATE, NEW BOOKING
     CONFIRMATION, REVISED BOOKING, etc.) and only one of them may carry
     the carrier name. Same trick for lane (Origin → Destination).
  2. MANUAL FALLBACK: CARRIER_BY_MDOLX dict for MDOLX refs whose subjects
     never carry a carrier signal (Lonny 'covered' off-channel, draft-only
     emails, etc.). Maintained by hand when auto-discovery returns nothing.

Idempotent — only writes where carrier_won is currently missing/empty.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import contextlib

import body_parser as BP  # noqa: E402
import core as C  # noqa: E402

try:
    import pdf_parser as PDF  # noqa: E402
    _PDF_OK = True
except Exception:
    _PDF_OK = False

# Manual fallback — only for MDOLX refs whose stage subjects truly have
# no carrier signal. Auto-discovery handles the common case.
CARRIER_BY_MDOLX: dict[str, str] = {
    "260062": "MSC",            # subject: "MSC BKG # EBKG14800694"
    "260211": "CMA CGM",        # body verified: 6x "CMA CGM"; booking NAM832...
    "260240": "Evergreen",      # subject: "EVERGREEN BKG # 404640177726"
    "260357": "MSC",            # subject: "MSC BKG # EBKG16245253"
    "260367": "Evergreen",      # subject: "EVERGREEN BKG # 404640284442"
    "260388": "Evergreen",      # subject: "EVERGREEN BKG # 404640301435"
    "260407": "Evergreen",      # subject: "EVERGREEN BKG # 404640318320"
    "260408": "Evergreen",      # subject: "EVERGREEN BKG # 404640318371"
    "260420": "ONE",            # body & subject: "// ONE: RICGE7217600"
    "260426": "CMA CGM",        # subject: "// CMA: NAM8321190"
    "260460": "CMA CGM",        # subject: "// CMA: NAM8400958" (Oakland->Tokyo 4x40RF)
    "260482": "ONE",            # subject: "// ONE LINE BKG # RICGH7587500"
    "260486": "Evergreen",      # subject: "EVERGREEN BKG # 404640376320"
    "260491": "OOCL",           # subject: "// OOCL BKG #"
}


def _load_stage_subjects_by_mdolx() -> dict[str, list[str]]:
    """Read stage_emails.txt and group all subjects by the MDOLX ref(s) they mention."""
    out: dict[str, list[str]] = {}
    stage_path = ROOT / "scripts" / "stage_emails.txt"
    if not stage_path.exists():
        return out
    for line in stage_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except Exception:
            continue
        subj = d.get("subject") or ""
        if not subj:
            continue
        for m in re.finditer(r"MDOLX\s*0*(\d{4,})", subj, flags=re.IGNORECASE):
            ref = m.group(1)
            out.setdefault(ref, []).append(subj)
    return out


#: imid -> the email's own sentDateTime, harvested alongside the bodies.
#: refresh_stage.py:546 has always written this; nothing read it until
#: 2026-07-30, which is why rate recovered here never carried a response time.
_SENT_BY_IMID: dict[str, str] = {}
#: imid -> the email's sender, harvested alongside. 2026-08-11: a recovered
#: quote's evidence must be OL-AUTHORED — on a rebuilt request row the
#: source_imids include Lonny's own ask, whose body quotes the PREVIOUS rate
#: sheet, and stamping from it manufactured same-day phantom quotes.
_SENDER_BY_IMID: dict[str, str] = {}


def _load_bodies_by_imid() -> dict[str, str]:
    """Read stage_emails_bodies.txt and index by message-id (imid).

    Q&L rows carry `source_imids` linking to the OL rate-response messages.
    When the table parser fails (prose-format quotes), we scan the body
    text directly for carrier name near a rate amount.
    """
    out: dict[str, str] = {}
    bodies_path = ROOT / "scripts" / "stage_emails_bodies.txt"
    if not bodies_path.exists():
        return out
    for line in bodies_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except Exception:
            continue
        imid = (d.get("imid") or "").strip("<>").strip()
        if not imid:
            continue
        # Field name is `text_body` in the current schema (was `body` /
        # `body_text` in legacy refresh_stage versions). Try all three so
        # this works across stage_emails_bodies.txt versions.
        body = d.get("text_body") or d.get("body") or d.get("body_text") or d.get("summary_preview") or ""
        if body:
            out[imid] = body
        # Keep the message's own send time. When a rate is recovered from this
        # body, that timestamp IS the moment OL quoted — recovering it is not
        # inventing a value, it is reading one that was already on disk.
        # C.body_send_time (core), NOT a hand-rolled list. The three spellings
        # this used belong to stage_emails.txt; THIS file is
        # stage_emails_bodies.txt, which fetch_bodies writes with
        # sent_ts/received_ts. _SENT_BY_IMID was therefore always empty and
        # _stamp_response_time — the whole #140 fix — never ran. The line
        # directly above already tried four spellings of the BODY field for
        # exactly this reason; the send time got one guess and no fallback.
        _sent = C.body_send_time(d)
        if _sent:
            _SENT_BY_IMID[imid] = _sent
        _snd = d.get("sender_email")
        if _snd:
            _SENDER_BY_IMID[imid] = _snd
    return out


def _load_rate_responses_by_thread() -> dict[tuple, str]:
    """Index mbd_rate_response bodies by (conversation_id, mdolx_ref, lane).

    Used by patch_carriers PASS 2 to find a sibling rate-response when the
    current row's source_imid points to a booking-confirmation body that
    has no inline ETD/vessel/rate (data is in the PDF attachment, but the
    rate-response email for the SAME MDOLX has the pipe-table inline).

    Returns dict keyed by (conversation_id|mdolx_ref|lane_key) → record dict
    {body, sender, sent}. Each key is a separate index entry pointing to the
    same record, so a lookup by any of the three signals finds the
    rate-response. sender/sent ride along so _find_related_rate_response can
    hold the sibling to the same quote-evidence bar as a direct body.
    """
    out: dict[tuple, dict] = {}
    bodies_path = ROOT / "scripts" / "stage_emails_bodies.txt"
    if not bodies_path.exists():
        return out
    import re as _re
    for line in bodies_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except Exception:
            continue
        if d.get("bucket") != "mbd_rate_response":
            continue
        body = d.get("text_body") or d.get("body") or ""
        if not body:
            continue
        rec = {"body": body, "sender": d.get("sender_email"),
               "sent": C.body_send_time(d)}
        conv = (d.get("conversation_id") or "").strip()
        if conv:
            out[("conv", conv)] = rec
        subject = d.get("subject") or ""
        m = _re.search(r"MDOLX\s*0*(\d{4,})", subject, _re.IGNORECASE)
        if m:
            out[("mdolx", m.group(1))] = rec
        # Lane fallback (Oakland-to-Yokohama-type subjects)
        m2 = _re.search(r"([A-Z][a-z]+)\s+to\s+([A-Z][a-z]+)", subject)
        if m2:
            lane_key = f"{m2.group(1)}->{m2.group(2)}".lower()
            out.setdefault(("lane", lane_key), rec)
    return out


def _find_related_rate_response(row: dict, by_thread: dict[tuple, dict]) -> str | None:
    """Look up a rate-response body for this row by thread signals.

    Every hit must pass core.quote_evidence_ok against THIS row's ask time.
    The conv-id join is the dangerous one: Lonny re-uses Outlook threads, so
    "the rate-response in this conversation" is routinely LAST cycle's sheet,
    sent before this ask ever existed — pasting it on is the same phantom
    quote as mining the ask body, arriving through the side door.
    """
    req_ts = row.get("request_timestamp")

    def _ok(rec: dict | None) -> str | None:
        if rec and C.quote_evidence_ok(rec.get("sender"), rec.get("sent"), req_ts):
            return rec["body"]
        return None

    conv = (row.get("conversation_id") or "").strip()
    if conv:
        body = _ok(by_thread.get(("conv", conv)))
        if body:
            return body
    for cand in [row.get("mdolx_ref")] + (row.get("mdolx_refs_all") or []):
        if cand:
            body = _ok(by_thread.get(("mdolx", str(cand))))
            if body:
                return body
    origin = (row.get("origin") or "").strip().lower()
    dest = (row.get("destination") or "").strip().lower()
    if origin and dest:
        body = _ok(by_thread.get(("lane", f"{origin}->{dest}")))
        if body:
            return body
    return None


def _dest_from_pod(pod) -> str | None:
    """Canonical export destination for a PDF/table POD value, or None.

    A bare booking amendment ("PLEASE UPDATE BKG # ... // HILMAR",
    stand_260895/stand_260905, 2026-07-10) carries NO lane in subject or
    body — the ONLY lane source is the attached booking PDF's Port of
    Discharge. This maps that POD onto the curated KNOWN_DESTINATIONS
    corpus; anything not in the corpus returns None so a garbled PDF cell
    can never invent a lane (the row stays honestly Unknown and QC-015
    keeps flagging it).

    2026-07-12 (run 29174327034): the literal placeholder "Unknown" /
    "unknown" / "" is ABSENT, not a port — rows written by older parses
    stored it verbatim. Return None fast and EXPLICITLY. (A corpus miss
    already returned None, but the intent must be pinned so a future
    corpus entry or normalizer change can never make the placeholder
    match.)
    """
    if not pod or not isinstance(pod, str):
        return None
    cleaned = pod.strip()
    if not cleaned or cleaned.lower() == "unknown":
        return None
    cand = BP._norm(cleaned)
    lookup = {d.lower(): d for d in BP.KNOWN_DESTINATIONS}
    hit = lookup.get(cand.lower())
    if hit:
        return hit
    # PDF PODs often carry a trailing country/region ("Singapore Singapore",
    # "Yokohama, Japan") — try the first token(s) against the corpus.
    first = cand.split(",")[0].strip()
    return lookup.get(first.lower())


#: POD-shaped keys tried, in order, when recovering a destination — the row
#: first, then this run's parsed body/PDF fields. pdf_parser emits "pod";
#: the aliases cover table/LLM parses so a field rename can't silently kill
#: lane recovery. "pol" is deliberately EXCLUDED — that's the ORIGIN port
#: (2026-07-12 deeper-recovery fix, run 29174327034).
_POD_FIELD_ALIASES = (
    "pod", "port_of_discharge", "discharge_port", "destination_port", "dest_port",
)


def _dest_from_row_pod(row: dict, parsed: dict) -> str | None:
    """First corpus-mappable destination among the row's / the parse's
    POD-shaped fields (see _POD_FIELD_ALIASES). _dest_from_pod treats the
    literal "Unknown" as absent, so a row that stored the placeholder
    falls through to the parsed candidates instead of dead-ending."""
    for src in (row or {}, parsed or {}):
        for key in _POD_FIELD_ALIASES:
            hit = _dest_from_pod(src.get(key))
            if hit:
                return hit
    return None


def _index_pdfs_by_mdolx() -> dict[str, Path]:
    """Scan scripts/stage_pdfs/ once and index PDFs by the MDOLX number
    they contain. Cheaper than re-parsing for every row.

    Many WIN rows' source_imids don't match the PDF imids on disk (the WIN
    was created from one message in the thread; the PDF was downloaded
    from a different message). The booking-PDF text reliably contains
    "BOOKING CONFIRMATION MDOLX<number>" near the top — use it as the
    join key.
    """
    if not _PDF_OK:
        return {}
    out: dict[str, Path] = {}
    pdf_dir = ROOT / "scripts" / "stage_pdfs"
    if not pdf_dir.exists():
        return out
    for pdf in pdf_dir.glob("*.pdf"):
        try:
            text = PDF._extract_pdf_text(pdf)
        except Exception:
            continue
        if not text:
            continue
        # OL booking PDFs say "BOOKING CONFIRMATION MDOLX<ref>" near the top
        m = re.search(r"MDOLX\s*0*(\d{4,})", text, re.IGNORECASE)
        if m:
            out.setdefault(m.group(1), pdf)
    return out


# Carrier-name patterns in body text. Keyed by canonical name; values are
# regex patterns that match the carrier in prose. Designed for false-negative
# avoidance: must be paired with a rate-dollar amount in the same body to
# claim the row.
_BODY_CARRIER_PATTERNS = [
    ("CMA CGM",   re.compile(r"\b(?:CMA\s*CGM|CMA-?CGM|\bCMA\b)\b", re.I)),
    ("MSC",       re.compile(r"\bMSC\b", re.I)),
    ("Maersk",    re.compile(r"\bMaersk\b", re.I)),
    ("ONE",       re.compile(r"\b(?:ONE|Ocean Network Express)\b")),  # case-sensitive ONE
    ("OOCL",      re.compile(r"\bOOCL\b", re.I)),
    ("Evergreen", re.compile(r"\b(?:Evergreen|EMC)\b", re.I)),
    ("HMM",       re.compile(r"\bHMM\b", re.I)),
    ("Yang Ming", re.compile(r"\b(?:Yang\s*Ming|YML)\b", re.I)),
    # Match Hapag, Hapag-Lloyd, HAPAG, or HLAG (alpha codes vary in OL prose)
    ("Hapag-Lloyd", re.compile(r"\b(?:Hapag(?:[\s\-]?Lloyd)?|HLAG)\b", re.I)),
    ("ZIM",       re.compile(r"\bZIM\b")),
    ("COSCO",     re.compile(r"\bCOSCO\b", re.I)),
]

_BODY_RATE_PATTERN = re.compile(r"\$\s*([\d,]{3,}(?:\.\d{2})?)")

# Boilerplate markers that signal we've left the rate body and entered the
# OL signature block + standard disclaimers. Everything after this point
# routinely mentions multiple carrier names (e.g. 'Maersk, Sealand, MSC,
# ONE, CMA, and Cosco do not accept Dummy SI') and MUST NOT be scanned for
# carrier attribution. Bug surfaced 2026-05-08 when 60 Q&L rows were
# falsely attributed to CMA CGM via the boilerplate.
_BOILERPLATE_MARKERS = (
    "Best Regards",
    "Best regards,",
    "Thank you & Best Regards",
    "Thank you and Best Regards",
    "*Please note that ERD",
    "*Due to the current",
    "Due to the current tensions",
    "*Maersk, Sealand",
    "*Labor unrest",
    "Email: Alexandra.Hernandez",  # signer — start of signature
    "Email: MBD_OceanExportBookingShared",
    "Email: MBD_",
    "OL-USA\n265 Post Avenue",
    "265 Post Avenue, Ste 333",
    "Phone: 440-202-",
    "CONFIDENTIAL:",
)


def _strip_boilerplate(body: str) -> str:
    """Truncate body at the first boilerplate marker so the carrier scan
    sees only the rate-quote prose, not OL's standard footer + disclaimers."""
    if not body:
        return ""
    earliest = len(body)
    for marker in _BOILERPLATE_MARKERS:
        i = body.find(marker)
        if i > 0 and i < earliest:
            earliest = i
    return body[:earliest]


def _stamp_response_time(r: dict, parsed: dict) -> bool:
    """Give a recovered quote the send time of the email it came from.

    THE 2026-07-30 DEFECT. OL-USA RESPONSES is bucketed by event date
    (gen_email.py:186-199, off response_timestamp). PENDING HILMAR is current
    state and is not windowed. So a row that gains an ol_rate here but no
    response_timestamp is invisible to OL-USA RESPONSES on EVERY day, forever,
    while still displaying its quote under PENDING HILMAR. Measured on the
    stored state: 29 of 315 rows, and the newest response_timestamp anywhere
    was 2026-07-23 — the section had been silently empty since Jul 24.

    ingest.py:1199-1200 sets rate and timestamp together, and skips any rate
    response it cannot date at all (`if not sent_dt: continue`). This module is
    the OTHER way a rate reaches a row, and it only ever recovered the rate.

    This is RECOVERY, NOT FABRICATION, and the distinction is the whole point:
    the value written is the `sent` of the very message the rate was parsed
    out of, which refresh_stage.py:546 already stored. Nothing is inferred
    from a sibling, a lane, or a guess. If that message has no send time, the
    field stays None — an undated quote is honest, an invented turnaround is
    not, and QC-077 will keep flagging it.

    Returns True when a timestamp was written.
    """
    if r.get("response_timestamp"):
        return False
    imid = (parsed or {}).get("_src_imid")
    if not imid:
        return False
    sent = _SENT_BY_IMID.get(imid)
    if not sent:
        return False
    # 2026-08-11: recovery is only recovery when the message is OL's and
    # postdates the ask. Same predicate as qc_selfheal's stamp — one rule,
    # one home (core.quote_evidence_ok); see its docstring for the
    # phantom-Q&L machine this closes.
    if not C.quote_evidence_ok(_SENDER_BY_IMID.get(imid), sent,
                               r.get("request_timestamp")):
        return False
    r["response_timestamp"] = sent
    return True


def _discover_full_quote_from_bodies(imids: list[str], bodies_by_imid: dict[str, str],
                                     request_ts: str | None = None) -> dict:
    """Return the full parsed quote from a QUALIFYING source body — carrier,
    rate, ETD, ETA, vessel/voyage, transshipment, free-time, POL/POD.

    Returns a dict ready to merge into the request row. Empty dict if nothing
    parseable. Extended 2026-05-13 per Michael "data missing throughout the
    report" — ingest's old runs lost these fields; this backfills.

    QUALIFYING (2026-08-12): the body must pass core.quote_evidence_ok —
    written by OL, sent after the ask. On a rebuilt row source_imids is the
    ask itself, and Lonny re-uses threads, so his new ask carries the
    PREVIOUS rate sheet quoted below it. Parsing it here is how the Aug-12
    staff email showed three fresh requests "quoted" (Yang Ming $797 /
    CMA CGM $725 / ONE $505) that OL never sent — the run log's own
    "PATCH PND" lines, and 46 more of QC-077's 49 undated quotes. The
    aa39f16 guard stopped this function's timestamps but not its rates:
    a quote this function returns must clear the same bar as the time it
    would be stamped with.
    """
    for imid in imids or []:
        key = imid.strip("<>").strip()
        body = bodies_by_imid.get(key)
        if not body:
            continue
        if not C.quote_evidence_ok(_SENDER_BY_IMID.get(key),
                                   _SENT_BY_IMID.get(key), request_ts):
            continue
        try:
            parsed = BP.parse_rate_table(body)
        except Exception:
            parsed = {}
        if parsed.get("carrier_quoted"):
            # Canonicalize carrier name
            canon = C.normalize_carrier(parsed["carrier_quoted"]) or parsed["carrier_quoted"]
            parsed["carrier_quoted"] = canon
            parsed["_src_imid"] = key
            return parsed
        # Fallback prose-scan for carrier+rate (still useful for non-table bodies)
        truncated = _strip_boilerplate(body)
        if not truncated:
            continue
        rate_m = _BODY_RATE_PATTERN.search(truncated)
        if not rate_m:
            continue
        best_pos = None
        best_canon = None
        for canonical, pat in _BODY_CARRIER_PATTERNS:
            m = pat.search(truncated)
            if m and (best_pos is None or m.start() < best_pos):
                best_pos = m.start()
                best_canon = canonical
        if best_canon:
            try:
                rate_val = float(rate_m.group(1).replace(",", ""))
            except ValueError:
                rate_val = None
            return {"carrier_quoted": best_canon, "ol_rate": rate_val,
                    "_src_imid": key}
    return {}


# _discover_carrier_from_bodies was deleted 2026-08-12: zero callers, and it
# duplicated _discover_full_quote_from_bodies' mining loop WITHOUT the
# quote-evidence gate — dead code holding the ungated fabrication path open
# for the next caller to find.


def _discover_carrier_from_subjects(subjects: list[str]) -> str | None:
    """Try parse_subject_carrier on each subject — return first non-None hit."""
    for s in subjects:
        c = BP.parse_subject_carrier(s)
        if c:
            return C.normalize_carrier(c)
    return None


def _discover_lane_from_subjects(subjects: list[str]) -> tuple[str | None, str | None]:
    """Find an 'Origin to Destination' phrase in any of the MDOLX's subjects.

    Booking-confirmation subjects look like:
      MDOLX260587_ *NEW BOOKING CONFIRMATION // HILMAR - Oakland to Osaka - 2X40'RF // EVERGREEN ...
    """
    for s in subjects:
        try:
            o, d = BP.parse_subject_lane(s)
            if o and d and o != "Unknown" and d != "Unknown":
                return o, d
        except Exception:
            pass
    return None, None


def main():
    cfg = C.load_config()
    data_path = Path(cfg["paths"]["data"])
    data = json.loads(data_path.read_text(encoding="utf-8"))

    requests = data.get("requests", [])
    patched_carrier = 0
    patched_lane = 0
    patched_ql_carrier = 0
    patched_rate = 0
    patched_resp_ts = 0
    auto_hits: list[str] = []
    manual_hits: list[str] = []
    body_hits: list[str] = []

    stage_by_mdolx = _load_stage_subjects_by_mdolx()
    bodies_by_imid = _load_bodies_by_imid()
    rate_by_thread = _load_rate_responses_by_thread()
    pdfs_by_mdolx = _index_pdfs_by_mdolx()
    normalized_manual = {mdolx: C.normalize_carrier(c) for mdolx, c in CARRIER_BY_MDOLX.items()}
    if pdfs_by_mdolx:
        print(f"  loaded {len(pdfs_by_mdolx)} PDFs indexed by MDOLX")

    def _row_mdolx_candidates(row):
        out = []
        if row.get("mdolx_ref"):
            out.append(row["mdolx_ref"])
        for m in row.get("mdolx_refs_all", []) or []:
            out.append(m)
        if row.get("booking_id"):
            out.append(row["booking_id"])
        if row.get("mdolx"):
            out.append(row["mdolx"])
        return out

    for r in requests:
        if r.get("status") != "WIN":
            continue

        # CARRIER attribution
        if not (r.get("carrier_won") or r.get("carrier_quoted")):
            canon = None
            mdolx_used = None
            # 1. Auto-discovery from stage subjects
            for cand in _row_mdolx_candidates(r):
                subjects = stage_by_mdolx.get(cand, [])
                if subjects:
                    canon = _discover_carrier_from_subjects(subjects)
                    if canon:
                        mdolx_used = cand
                        auto_hits.append(f"{cand}->{canon}")
                        break
            # 2. Manual fallback
            if not canon:
                for cand in _row_mdolx_candidates(r):
                    if cand in normalized_manual:
                        canon = normalized_manual[cand]
                        mdolx_used = cand
                        manual_hits.append(f"{cand}->{canon}")
                        break
            if canon:
                r["carrier_won"] = canon
                if not r.get("carrier_quoted"):
                    r["carrier_quoted"] = canon
                patched_carrier += 1
                print(f"  PATCH carrier {mdolx_used} -> {canon} (dest={r.get('destination')})")

        # LANE attribution — only when current lane is unresolved/empty
        lane_now = r.get("lane") or ""
        dest_now = r.get("destination") or ""
        if lane_now in ("", "Lane unresolved") or dest_now in ("", "Unknown"):
            for cand in _row_mdolx_candidates(r):
                subjects = stage_by_mdolx.get(cand, [])
                o, d = _discover_lane_from_subjects(subjects)
                if o and d:
                    if not r.get("origin") or r.get("origin") in ("", "Unknown"):
                        r["origin"] = o
                    if not r.get("destination") or r.get("destination") in ("", "Unknown"):
                        r["destination"] = d
                    r["lane"] = f"{r.get('origin', o)} → {r.get('destination', d)}"
                    patched_lane += 1
                    print(f"  PATCH lane    {cand} -> {r['lane']}")
                    break

    # Q&L body-text carrier fallback — added 2026-05-07 per Michael "did
    # you fix the drifts and all from the 1233pm report". The table-format
    # parser caught ~48% of Q&L. The body-scan looks at the actual rate-
    # response message body for carrier + rate co-occurrence.
    # 2026-05-13: extended to also patch PENDING rows (per Michael "status
    # change of pending to quoted with no carrier and no rate"). When OL
    # responds to a request, it transitions PENDING -> QUOTED but the row
    # stays in PENDING-final-status until Lonny replies. Those PENDING
    # rows have a rate-response in source_imids that carries carrier+rate.
    # The body-scan should fill those just like it fills Q&L.
    # Two-pass enrichment:
    # PASS 1 — fill carrier_quoted on Q&L + PENDING rows missing it (primary
    #          goal of patch_carriers since its inception).
    # PASS 2 — fill etd_offered, eta_offered, vessel_voyage, transshipment,
    #          and other table fields on ALL rows where they're missing AND
    #          source_imids has a parseable rate-response body. Added 2026-05-13
    #          per Michael "data missing throughout the report" — addresses
    #          the 70% etd_offered / 69% vessel_voyage missing-rate.
    patched_fields = 0
    field_hits: dict[str, int] = {}
    BACKFILL_KEYS = (
        "etd_offered", "eta_offered", "vessel_voyage", "transshipment",
        "container_size", "pol", "pod", "dthc",
        "origin_cutoff", "doc_cutoff", "port_cutoff",
        # 2026-05-19 parser-gap fix (Michael "no field should be empty ever"):
        # extend patch_carriers PASS 2 to backfill the newly-extracted fields
        # too. Without this, the new fields would be set on ingest but
        # NOT enriched from sibling bodies — patch_carriers is the safety
        # net that fills from any body in the conversation's source_imids.
        "erd", "rate_expiry", "origin_free_time", "dest_free_time",
        "product", "temperature", "requested_dates", "etd_requested",
        "lonny_notes",
        # 2026-05-19 (Michael "PARSER MUST REACH 95 PERCENT AT A MINIMUM"):
        # extract container_count + teu_requested + containers string from
        # the booking PDF so standalone WIN rows whose subject didn't carry
        # an MDOLX container marker can still populate volume fields.
        # Only the PDF reliably encodes quantity × size on a 3-container
        # booking like the NUMIDIA samples (subject only says "1X40'HC"
        # for 3 containers — wrong count).
        "container_count", "teu_requested", "containers", "booking_ref",
    )

    for r in requests:
        imids = r.get("source_imids") or []
        parsed = (_discover_full_quote_from_bodies(
                      imids, bodies_by_imid, r.get("request_timestamp"))
                  if imids else {})

        # Cross-thread fallback: if the source_imid body had no parseable
        # table (booking-confirmation bodies are signature-only — data is
        # in the PDF attachment), look up a sibling rate-response for the
        # same MDOLX / conversation / lane. Added 2026-05-13 per Michael
        # "no.. 90 percent for all is the bare minimum". Booking-conf
        # rows now inherit ETD/vessel/rate from their corresponding
        # rate-response email.
        # eta_offered joins the trigger 2026-08-10. BACKFILL_KEYS has always
        # been able to WRITE it, but this gate — the thing that decides whether
        # we go looking — named only etd/vessel/rate. Every one of the 22 rows
        # QC-027 counts as ETA-missing carries etd+vessel+rate, so every one of
        # them failed this test and never triggered the sibling lookup that
        # could have supplied an ETA. The field was gradeable but unreachable.
        needs_fields = not all(parsed.get(k) for k in
                               ("etd_offered", "eta_offered", "vessel_voyage", "ol_rate"))
        if needs_fields:
            sibling = _find_related_rate_response(r, rate_by_thread)
            if sibling:
                try:
                    sib_parsed = BP.parse_rate_table(sibling)
                except Exception:
                    sib_parsed = {}
                # Merge — sibling fills only what current parse missed
                for k, v in (sib_parsed or {}).items():
                    if v and not parsed.get(k):
                        parsed[k] = v
                if sib_parsed.get("carrier_quoted"):
                    canon = C.normalize_carrier(sib_parsed["carrier_quoted"]) or sib_parsed["carrier_quoted"]
                    parsed.setdefault("carrier_quoted", canon)

        # PDF-attachment fallback: for booking-confirmation rows whose
        # body is signature-only AND have no sibling rate-response in
        # stage, try the attached PDF. Two lookup paths:
        #   1. Direct: PDF saved at same imid filename
        #   2. Cross-reference by MDOLX (PDFs are indexed by the MDOLX
        #      number found in their text — works even when the row's
        #      source_imid doesn't match the PDF's imid)
        #
        # 2026-05-19 (Michael "PARSER MUST REACH 95 PERCENT AT A MINIMUM
        # AND INCLUDE ATTACHMENTS"): the gate also fires when PDF-ONLY
        # fields are missing — erd, doc_cutoff, port_cutoff, dest_free_time,
        # product. These only appear in the booking PDF; the email body
        # rate-table doesn't have them. So even when etd/vessel/rate are
        # already populated from the rate response, we still parse the
        # PDF for the PDF-only enrichment.
        _PDF_ONLY_TARGETS = ("erd", "doc_cutoff", "port_cutoff", "dest_free_time", "product")
        _need_pdf_only = any(not r.get(k) for k in _PDF_ONLY_TARGETS) and r.get("status") == "WIN"
        if _PDF_OK and (not all(parsed.get(k) for k in ("etd_offered", "vessel_voyage", "ol_rate"))
                        or _need_pdf_only):
            pdf_path = None
            # Try direct imid match
            for imid in imids:
                safe_imid = re.sub(r"[^A-Za-z0-9._-]+", "_", imid.strip("<>"))[:100]
                p = ROOT / "scripts" / "stage_pdfs" / f"{safe_imid}.pdf"
                if p.exists():
                    pdf_path = p
                    break
            # Cross-reference by MDOLX if no direct match
            if pdf_path is None:
                for cand in [r.get("mdolx_ref")] + (r.get("mdolx_refs_all") or []):
                    if cand and cand in pdfs_by_mdolx:
                        pdf_path = pdfs_by_mdolx[cand]
                        break
            if pdf_path:
                try:
                    pdf_parsed = PDF.parse_booking_pdf(pdf_path)
                except Exception:
                    pdf_parsed = {}
                for k, v in (pdf_parsed or {}).items():
                    if v and not parsed.get(k):
                        parsed[k] = v
                if pdf_parsed.get("carrier_quoted"):
                    canon = C.normalize_carrier(pdf_parsed["carrier_quoted"]) or pdf_parsed["carrier_quoted"]
                    parsed.setdefault("carrier_quoted", canon)

        if parsed:
            # PASS 1: carrier+rate (only on Q&L + PENDING)
            target_status = (r.get("status") == "LOSS" and r.get("quoted")) or (r.get("status") == "PENDING")
            if target_status and not r.get("carrier_quoted") and parsed.get("carrier_quoted"):
                canon = parsed["carrier_quoted"]
                r["carrier_quoted"] = canon
                patched_ql_carrier += 1
                body_hits.append(f"{r.get('request_id')}->{canon}")
                if parsed.get("ol_rate") is not None and not r.get("ol_rate"):
                    r["ol_rate"] = parsed["ol_rate"]
                    r["quoted"] = True   # a recovered rate IS a quote — never NQ
                    patched_rate += 1
                    if _stamp_response_time(r, parsed):
                        patched_resp_ts += 1
                status_tag = "Q&L" if r.get("status") == "LOSS" else "PND"
                print(f"  PATCH {status_tag}  {r.get('request_id')[:16]} -> {canon}"
                      + (f" @ ${parsed['ol_rate']:.0f}" if parsed.get('ol_rate') else ""))

            # PASS 2: structured-table fields on ALL rows (regardless of status).
            # Only fill if the row doesn't already have the value and the parse
            # produced one. Never overwrite existing data.
            for k in BACKFILL_KEYS:
                if not r.get(k) and parsed.get(k):
                    r[k] = parsed[k]
                    patched_fields += 1
                    field_hits[k] = field_hits.get(k, 0) + 1
            # ol_rate on WIN/PENDING rows too (PASS 1 only does it during carrier patch)
            if not r.get("ol_rate") and parsed.get("ol_rate") is not None:
                r["ol_rate"] = parsed["ol_rate"]
                r["quoted"] = True   # a recovered rate IS a quote — never NQ
                patched_rate += 1
                if _stamp_response_time(r, parsed):
                    patched_resp_ts += 1

        # PASS 2b: destination + lane from the row's own POD. A bare
        # booking amendment has no lane in subject/body and no sibling
        # subject naming one (the earlier LANE-attribution pass misses it)
        # — but the booking PDF's Port of Discharge, backfilled into
        # r["pod"]/parsed["pod"] by PASS 2 just above, IS the lane.
        # Guarded by _dest_from_pod's KNOWN_DESTINATIONS check so a
        # garbled PDF cell can never invent a lane. Zero 'Lane unresolved'
        # in the daily email is the standard (Michael, 2026-07-09/10).
        #
        # 2026-07-12 (run 29174327034): this block — and the LANE-DIAG
        # breadcrumb below — now runs OUTSIDE the `if parsed:` guard. The
        # old `if not parsed: continue` short-circuit skipped every row
        # whose bodies parsed to nothing, so stand_260905 stayed
        # unresolved with NO diagnostic line in the run log, even though
        # r["pod"] can already sit on the row from a prior fire's PASS 2.
        # _dest_from_row_pod also tries the parse's POD-shaped aliases
        # and treats the literal "Unknown" as absent.
        if (r.get("destination") or "Unknown") in ("Unknown", "") or \
                (r.get("lane") or "") == "Lane unresolved":
            _pod_dest = _dest_from_row_pod(r, parsed)
            if _pod_dest:
                r["destination"] = _pod_dest
                _o = r.get("origin") or "Oakland"
                r["lane"] = f"{_o} → {_pod_dest}"
                patched_lane += 1
                print(f"  PATCH lane    {r.get('request_id', '')[:16]} -> "
                      f"{r['lane']} (from booking-PDF POD)")
            elif C.has_no_rfq_chain(r):
                # Say WHY the row stays unresolved — pod missing vs pod
                # unmappable vs no PDF fields at all — so the run log alone
                # is enough to target the next fix (no ad-hoc diagnostics).
                _pdf_saw = any(parsed.get(k) for k in
                               ("erd", "doc_cutoff", "port_cutoff", "product"))
                print(f"  LANE-DIAG {r.get('request_id')}: unresolved — "
                      f"pod={(r.get('pod') or parsed.get('pod')) or 'none'}; "
                      f"pdf_fields_present={'yes' if _pdf_saw else 'no'}")

    # ─────────────────────────────────────────────────────────────────
    # PASS 4 — Stage-scan for WINs still missing carrier_won
    #
    # Per Michael 2026-05-17 ("your qc and parsers have to improve").
    # Some Hilmar WINs arrive via a request thread that DOESN'T contain
    # an MDOLX booking confirmation (the booking confirmation lives in
    # a separate thread). PASS 1-3 above can't find a carrier because
    # they only look within the WIN's own source emails. PASS 4 scans
    # the broader stage looking for HILMAR-tagged MDOLX subjects that
    # match the WIN's lane + time window, and extracts the carrier from
    # the subject's booking-ref prefix (NAM=CMA, RICG=ONE, etc.) or
    # explicit name.
    #
    # Strict matching only — no ambiguous fills. If 2+ candidate
    # bookings match the same WIN, skip (operator review needed).
    # ─────────────────────────────────────────────────────────────────
    patched_pass4 = 0
    pass4_skipped_ambig = 0
    try:
        import re as _re
        from pathlib import Path as _Path

        # Carrier name + booking-ref-prefix map (mirrored from
        # backfill_mdolx.py — single source of truth would be nicer
        # later, but inline for now).
        _CARRIER_PREFIXES: dict[str, tuple[str, ...]] = {
            "CMA CGM":     ("NAM", "APL", "ANL", "CMA", "CGM"),
            "Maersk":      ("MAEU", "SEAU", "SUDU", "MSK", "MAERSK"),
            "MSC":         ("MEDU", "MSCU", "EBKG", "MSC"),
            "ONE":         ("ONEY", "RICG", "SCNB", "ONE"),
            "Evergreen":   ("EBKG", "EISU", "EGLV", "EVERGREEN", "EMC"),
            "Hapag-Lloyd": ("HLCU", "HLBU", "HLAG", "HAPAG"),
            "OOCL":        ("OOLU", "OOCL"),
            "Yang Ming":   ("YMLU", "YML", "YANGMING", "YANG"),
            "HMM":         ("HMMU", "HMM", "HYUNDAI"),
            "ZIM":         ("ZIMU", "ZIM"),
            "COSCO":       ("COSU", "COSCO", "COSCON"),
        }

        def _carrier_from_subject(subj: str) -> str | None:
            """Return the canonical carrier name detected in subject, or None."""
            if not subj:
                return None
            up = subj.upper()
            for canonical, prefixes in _CARRIER_PREFIXES.items():
                if canonical.upper() in up:
                    return canonical
                for p in prefixes:
                    if p in up:
                        return canonical
            return None

        # Load stage. Prefer .txt (current), fall back .jsonl.
        stage_path = _Path(__file__).resolve().parent / "stage_emails.txt"
        if not stage_path.exists():
            stage_path = _Path(__file__).resolve().parent / "stage_emails.jsonl"
        stage_rows = []
        if stage_path.exists():
            for line in stage_path.read_text(encoding="utf-8", errors="ignore").splitlines():
                if not line.strip():
                    continue
                with contextlib.suppress(Exception):
                    stage_rows.append(json.loads(line))

        # Pre-filter stage to HILMAR-tagged MDOLX subjects (way faster)
        mdolx_re = _re.compile(r"MDOL[XMFD][-\s_]*(\d{4,})", _re.IGNORECASE)
        hilmar_mdolx_stage = []
        for s in stage_rows:
            subj = (s.get("subject") or "")
            if "HILMAR" not in subj.upper():
                continue
            if not mdolx_re.search(subj):
                continue
            hilmar_mdolx_stage.append(s)

        from datetime import datetime as _dt
        def _days_apart(a: str, b: str) -> int:
            try:
                da = _dt.strptime(a[:10], "%Y-%m-%d").date()
                db = _dt.strptime(b[:10], "%Y-%m-%d").date()
                return (da - db).days
            except Exception:
                return 9999

        wins_missing = [r for r in data["requests"]
                        if r.get("status") == "WIN" and not r.get("carrier_won")]
        for w in wins_missing:
            lane = (w.get("lane") or "").strip()
            if " → " not in lane:
                continue
            origin, dest = lane.split(" → ", 1)
            origin = origin.strip().lower()
            dest = dest.strip().lower()
            if not (origin and dest):
                continue
            win_date = w.get("request_date") or (w.get("request_timestamp") or "")[:10]
            if not win_date:
                continue

            candidates = []  # list of (carrier, mdolx, subject)
            for s in hilmar_mdolx_stage:
                subj = s.get("subject") or ""
                subj_lower = subj.lower()
                if origin not in subj_lower or dest not in subj_lower:
                    continue
                sent = (s.get("sent") or s.get("received") or "")[:10]
                if not sent or abs(_days_apart(sent, win_date)) > 14:
                    continue
                carrier = _carrier_from_subject(subj)
                if not carrier:
                    continue
                m = mdolx_re.search(subj)
                mdolx_str = f"MDOL{m.group(0)[4]}{m.group(1)}" if m else None
                candidates.append((carrier, mdolx_str, subj))

            # Distinct carriers — if multiple different carriers match,
            # we can't disambiguate without more signal. Skip.
            distinct_carriers = {c[0] for c in candidates}
            if len(distinct_carriers) == 1:
                carrier = candidates[0][0]
                mdolx_str = candidates[0][1]
                w["carrier_won"] = carrier
                if mdolx_str and not w.get("mdolx_ref"):
                    w["mdolx_ref"] = mdolx_str[5:]  # strip "MDOLX" prefix → digits
                patched_pass4 += 1
                print(f"  PASS4 {w.get('request_id', '?')[:20]} {lane:35} -> {carrier}"
                      + (f" (MDOLX {mdolx_str})" if mdolx_str else ""))
            elif len(distinct_carriers) > 1:
                pass4_skipped_ambig += 1
                # Capture context to Sentry — operator visibility into
                # ambiguous cases that need manual review
                try:
                    import sys as _sys
                    _sys.path.insert(0, str(_Path(__file__).resolve().parent))
                    import sentry_setup as _sentry
                    _sentry.capture_qc_warning(
                        "patch_carriers.ambiguous_match",
                        f"WIN {w.get('request_id', '?')} on lane {lane} matched "
                        f"{len(candidates)} candidate bookings across {len(distinct_carriers)} "
                        f"carriers: {sorted(distinct_carriers)} — needs manual review"
                    )
                except Exception:
                    pass
            else:
                # Zero matches — capture for trend tracking
                try:
                    import sys as _sys
                    _sys.path.insert(0, str(_Path(__file__).resolve().parent))
                    import sentry_setup as _sentry
                    _sentry.metric_increment(
                        "patch_carriers.pass4_no_match", 1,
                        lane=lane[:30],
                    )
                except Exception:
                    pass

        if patched_pass4:
            print(f"\nPASS 4 backfilled {patched_pass4} WIN carrier(s) "
                  f"via stage-scan (HILMAR + lane + time-window + unique-carrier matching). "
                  f"Skipped {pass4_skipped_ambig} ambiguous case(s).")
            patched_carrier += patched_pass4
    except Exception as _e:
        print(f"⚠️  PASS 4 failed (continuing): {type(_e).__name__}: {_e}")

    if (patched_carrier == 0 and patched_lane == 0
            and patched_ql_carrier == 0 and patched_fields == 0):
        print("Nothing to patch - all target rows already complete.")
        return

    print(f"\nSummary: {patched_carrier} WIN-carrier patches "
          f"({len(auto_hits)} auto / {len(manual_hits)} manual / {patched_pass4} PASS-4 stage-scan), "
          f"{patched_lane} lane patches, "
          f"{patched_ql_carrier} Q&L-carrier patches (via body scan), "
          f"{patched_rate} rate patches "
          f"({patched_resp_ts} dated from the source email), "
          f"{patched_fields} field backfills "
          f"({', '.join(f'{k}:{v}' for k, v in sorted(field_hits.items()))})")

    meta = data.setdefault("meta", {})
    rev = int(meta.get("revision", 0)) + 1
    meta["revision"] = rev
    meta["patched_by"] = "patch_carriers.py"

    from datetime import datetime, timezone
    data["last_updated"] = datetime.now(timezone.utc).isoformat()

    C.save_data_validated(data, data_path)
    print(f"OK Patched -> revision {rev}")


if __name__ == "__main__":
    main()
