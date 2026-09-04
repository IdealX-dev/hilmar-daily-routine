#!/usr/bin/env python3
"""
Hilmar Tracker — core pure-functions library.

Every downstream script (qc_selfheal, gen_dashboard, gen_pdf, gen_email, run_tests)
imports from here. Never import Outlook/OneDrive SDKs in this file — that's Claude's
job at orchestration time.

Design rules:
- Pure functions. No filesystem I/O (except load_config/load_data helpers).
- DST-safe timezones via zoneinfo (NOT fixed UTC offsets).
- Single source of truth for status rules, TEU math, turnaround, dedup keys.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

# ─────────────────────────────────────────────────────────────────────
# Config & constants
# ─────────────────────────────────────────────────────────────────────

ET = ZoneInfo("America/New_York")      # DST-safe — handles EDT/EST automatically
PT = ZoneInfo("America/Los_Angeles")   # DST-safe — handles PDT/PST automatically
UTC = timezone.utc

BIZ_START = time(8, 30)   # 8:30 AM ET
BIZ_END   = time(17, 30)  # 5:30 PM ET
BIZ_DAY_HOURS = 9.0

#: Window used by the SEND-signal aging branch (send received, MDOLX not yet
#: → SEND_NO_BOOKING) and by is_business_stale's default. NOT the pending-
#: Hilmar quote window — that's PENDING_HILMAR_LOSS_HOURS below.
#: Mirrored in src/hilmar/core.py — tests/test_core_parity.py + QC-040
#: enforce parity.
PENDING_WINDOW_HOURS = 24

#: PENDING_HILMAR quote-decision window: a quote awaiting Lonny's decision is
#: Quoted & Lost after 24 CLOCK hours — 72 if OL quoted on a FRIDAY (ET), to
#: carry the weekend so a Friday quote lands Monday, not Sunday. Measured from
#: the OL quote (response_timestamp).
#:
#: 24, set by Michael in 0c73c4b (2026-07-26): "PENDING_HILMAR_LOSS_HOURS
#: 48->24 (supersedes 2026-07-14)". The comment here said 48 until 2026-08-06
#: — it described the superseded rule and was never updated when the value
#: changed, so four separate places in this file asserted a threshold the code
#: had not used for eleven days. That is not cosmetic: reading this block
#: while chasing "PENDING OL (0)" makes the constant look like the bug and
#: invites someone to "fix" an operator decision back to a value he had
#: already rejected. test_timer_docs_match_constants now fails on any such
#: drift. The SEND-signal aging (is_business_stale) is deliberately unchanged.
PENDING_HILMAR_LOSS_HOURS = 24
PENDING_HILMAR_LOSS_HOURS_FRIDAY = 72
#: PENDING-OL window — how long OL-USA has to answer Lonny's RFQ before the
#: row is called a genuine non-response (NQ). Symmetric with the Hilmar side
#: (PENDING_HILMAR_LOSS_HOURS): 24 CLOCK hours from Lonny's REQUEST, 72 when
#: the RFQ landed on a Friday (ET) so the weekend doesn't burn the window.
#: Also set to 24 in 0c73c4b; this comment said 48 for the same eleven days.
#: Added 2026-07-24 — before this, an unquoted request was classified
#: LOSS/NO_RESPONSE the instant it was ingested, with NO grace at all, which
#: made PENDING_OL structurally unreachable and buried live open business as
#: "lost". Mirrored across trees; tests/test_core_parity.py enforces parity.
PENDING_OL_LOSS_HOURS = 24
PENDING_OL_LOSS_HOURS_FRIDAY = 72

#: OL-USA's RESPONSE SLA, in BUSINESS hours (ET 8:30-17:30 Mon-Fri) — the same
#: clock the report's "Time to Quote" column already uses, so the SLA and the
#: displayed metric can never disagree. Michael 2026-07-26: "ol response time
#: has to be 3 hours" (config.json rules.overdue_no_response_hours = 3).
#: This is an OVERDUE/escalation threshold, NOT a loss threshold: past 3 biz
#: hours OL has breached and must be chased, but the row stays OPEN
#: (PENDING_OL) until the 24/72h win-loss timer above resolves it. Keeping the
#: two separate is deliberate — collapsing them would re-bury live business as
#: "lost", which is the 2026-07-24 defect this whole area exists to prevent.
PENDING_OL_SLA_BIZ_HOURS = 3

#: TIMING RESET. Michael 2026-08-13: "WHEN RUNNING KPI'S JUST INDICATE THE
#: TURN AROUND CLOCK AND SUCH IS OFF AND START RUNNING IT AGAIN STARTING
#: TODAY AND INDICATE THAT ON THE REPORTS".
#:
#: Every clock in this file measures from Lonny's request to OL's reply. Both
#: ends have to be in the mailbox for that to mean anything, and for roughly
#: Jul 1 - Aug 12 OL's side was not: booking confirmations and quote replies
#: went To: Lonny, Cc: the group, and never reached the mailbox this pipeline
#: reads (forwarding was fixed 2026-08-12). A turnaround average over that
#: period is not "slow OL", it is a clock started and never stopped —
#: measured against whatever fraction of replies happened to arrive. Publishing
#: it as a performance number would be inventing a fact, which is the one
#: thing this project does not do.
#:
#: So the clock RESTARTS. Samples dated before this floor are excluded from
#: every turnaround aggregate and the exclusion is stated on the reports
#: rather than quietly applied. Wins, losses and volumes are NOT affected —
#: those are reconciled against OL's own booking export and stand on their
#: own evidence.
#:
#: Retiring this is a one-line change once enough post-fix history exists to
#: trust: delete the constant and the branches that read it.
#:
#: RETIRED the same day, 2026-08-13, on evidence. Michael, once the shared
#: mailbox came online: "turnaround clock should be fine now that you see the
#: shard box yourself". Measured before flipping it rather than assuming
#: (diag-blob 31736160870, stored state, 288 rows carrying BOTH a request and
#: a response time):
#:
#:      NEGATIVE (response before ask)     0
#:      0-48h                            ~254
#:      48h-7d                              3
#:      7-30d                               1
#:      >30d                                8
#:      IMPLAUSIBLE (negative or >30d)      8 of 288  (2.8%)
#:
#: Not one response predates its own ask, which is the shape that would say
#: the pairing logic is broken. The 8 outliers are all April asks paired to
#: June/July replies — Lonny re-using an Outlook thread months later, the
#: known 2026-08-11 failure — and QC-021 already CLEARS turnaround above 40
#: biz-hours as implausible, so they are excluded from every average rather
#: than skewing it. The guard that made this constant necessary is not this
#: one; it is that clearing rule, and it is still in force.
#:
#: Left as an empty string rather than deleted. `timing_is_valid` returns True
#: for everything when it is falsy and `timing_reset_note` returns "", so the
#: banners drop out of the email, the dashboard and the PDF with no other
#: edit — and setting a date here re-arms the whole mechanism in one line if
#: the clock ever needs stopping again. Deleting the constant would mean
#: rebuilding all of that under pressure, which is when it was built the first
#: time.
TIMING_VALID_FROM = ""


def timing_is_valid(when) -> bool:
    """Is a timestamp inside the window where turnaround means anything?"""
    if not TIMING_VALID_FROM:
        return True
    s = when.isoformat() if hasattr(when, "isoformat") else str(when or "")
    return bool(s) and s[:10] >= TIMING_VALID_FROM


def timing_reset_note(short: bool = False) -> str:
    """The one sentence every surface showing a turnaround number prints."""
    if not TIMING_VALID_FROM:
        return ""
    if short:
        return f"Response clock restarted {TIMING_VALID_FROM}"
    return (
        f"Response-time metrics restarted {TIMING_VALID_FROM}. OL's replies "
        "were not reaching this mailbox before then (they went To: Lonny, "
        "Cc: the group), so any turnaround measured over that period would "
        "be a clock started and never stopped. Win, loss and volume figures "
        "are unaffected — those are reconciled against OL's booking export."
    )

#: PER-ROW TEU SANITY CEILING. On 2026-07-26 a reference number in a subject
#: line ("PO 4451440") parsed as 44,514 x 40' = 89,028 TEU from ONE row and
#: poisoned every volume figure in the day's email, dashboard, PDF and lane
#: rollups. `parse_teu`'s regex was hardened the same day, but a regex is one
#: line of defence and a wrong-but-huge number is invisible until someone
#: reads the report. This is the second line: any parse above the ceiling is
#: REFUSED (parse_teu returns 0, 0) rather than trusted, and QC-070 errors on
#: any stored row that exceeds it.
#:
#: Calibration: Hilmar quotes 1-6 containers per RFQ line in every real
#: sample on record (largest observed: 6x40'RF = 12 TEU). 100 TEU is 50 forty-
#: foots in a SINGLE request line — an order of magnitude beyond anything the
#: business has ever asked for, so a real quote can never trip it, while both
#: known parser defects (89,028 TEU and the 200 TEU "quote 10040" misread) are
#: caught. Raise it deliberately if Hilmar's volume ever changes; do not raise
#: it to silence a QC-070 that is telling you the parser regressed.
MAX_ROW_CONTAINERS = 60
MAX_ROW_TEU = 100

RATE_TREND_THRESHOLD_PCT = 10

VALID_STATUSES = {"WIN", "LOSS", "PENDING"}
# LOSS_REASONS — kept ALIGNED with src/hilmar/core.py LOSS_REASONS per QC-040
# cross-folder drift check. Each new reason here must also exist in the
# src/hilmar/ version (or vice versa).
#
# COVERED            = lost to a competitor (Lonny replied "covered")
# DRAFT_ONLY         = MDOLX has only a DRAFT RATED / Move updated email — no
#                       booking confirmation in stage. Added 2026-05-07.
# OTHER              = catch-all when nothing else fits — near-zero
# NO_RESPONSE        = OL never responded (display: NQ)
# RESPONSE_NO_RATE   = MBD acked but did not quote (display: NQ)
# QUOTED_NOT_BOOKED  = quoted, generic no-ETD signal (display: Q&L)
# PRICE              = quoted, OL's rate was uncompetitive vs winning lane
#                       median (>5% above) — real rate-gap determination
#                       (display: Q&L). Pre-2026-06-02 this was the
#                       catch-all fallback; see UNDIFFERENTIATED.
# ETD_MISS           = quoted but ETD missed Lonny's ask by ≥5d (display: Q&L)
# UNDIFFERENTIATED   = quoted & lost, but no concrete signal explains why.
#                       Rate was at/below winning lane median, ETD fit OK,
#                       no other reason — the honest "we lost, can't say
#                       what tipped it" bucket. Added 2026-06-02 to stop
#                       PRICE from being a false-positive catch-all that
#                       drove the "Push carriers" tag on rate-competitive
#                       losses (display: Q&L).
# AWAITING_MDOLX     = PENDING sub-state: Send received, MDOLX pending
# MDOLX_NO_SEND      = PENDING sub-state: MDOLX without send (anomaly)
# SEND_NO_BOOKING    = AWAITING_MDOLX aged out past 72h (display: Q&L)
LOSS_REASONS = {
    "NO_RESPONSE",
    "RESPONSE_NO_RATE",
    "QUOTED_NOT_BOOKED",
    "PRICE",
    "ETD_MISS",
    "UNDIFFERENTIATED",
    "OTHER",
    "COVERED",
    "DRAFT_ONLY",
    "AWAITING_MDOLX",
    "MDOLX_NO_SEND",
    "SEND_NO_BOOKING",
    # PENDING sub-state (and, once the request clock expires, a real loss):
    # OL quoted but the response carried no usable timestamp — typically a
    # patch_carriers rate recovered from a sibling thread or a booking PDF.
    # Added 2026-07-27 so this case stops hiding inside "OTHER"; see the
    # never-age-on-absence branch in decide_status.
    "NO_RESPONSE_TS",
}

#: Multiplier above lane winning median where we call a loss "PRICE".
#: A 5% premium above the lane winning median is the threshold; below
#: that the rate was competitive and the loss is UNDIFFERENTIATED.
#: Mirrored in src/hilmar/core.py — tests/test_core_parity.py guards.
PRICE_GAP_THRESHOLD_MULT = 1.05

#: Minimum number of historical WINs on a lane before we'll trust the
#: lane winning median for PRICE determination. Fewer than 3 WINs and
#: we lack signal — fall through to UNDIFFERENTIATED.
#: Mirrored in src/hilmar/core.py — tests/test_core_parity.py guards.
PRICE_GAP_MIN_LANE_WINS = 3

# ─────────────────────────────────────────────────────────────────────
# Trade-region map — destination → region (used for "Volume by Trade Region")
# Values must reconcile to summary totals; unmapped destinations should be
# rare and surfaced as warnings (NOT silently bucketed as "OTHER").
# ─────────────────────────────────────────────────────────────────────
_TRADE_REGION_MAP = {
    # Far East / Asia
    "shanghai": "Far East", "xingang": "Far East", "tianjin": "Far East",
    "qingdao": "Far East", "ningbo": "Far East", "dalian": "Far East",
    "yokohama": "Far East", "tokyo": "Far East", "osaka": "Far East",
    "kobe": "Far East", "nagoya": "Far East", "busan": "Far East",
    "port busan": "Far East", "incheon": "Far East", "keelung": "Far East",
    "kaohsiung": "Far East", "taichung": "Far East", "hong kong": "Far East",
    # Shekou — a terminal of Shenzhen, on the Pearl River Delta. Added
    # 2026-08-16; confirmed present in the real book as ol_260291
    # (Oakland → Shekou), where it had been sitting Unmapped.
    "shekou": "Far East", "shenzhen": "Far East", "yantian": "Far East",
    # Huangpu (Guangzhou) — added 2026-08-27. Michael: "still shows things
    # unmapped". This one is a GENUINE map gap: a real port name OL wrote
    # that simply was not here, which is the "extend the map" signal doing
    # its job. Its neighbour in that same Unmapped row, "Jpyok", is NOT —
    # that is a UN/LOCODE our own parser title-cased into a fake port name
    # (body_parser._norm), and adding it here would turn the row green while
    # splitting Yokohama across two spellings forever. Fix that one at the
    # parser, never here.
    "huangpu": "Far East",
    # Southeast Asia
    "hcmc": "SE Asia", "ho chi minh": "SE Asia", "cat lai": "SE Asia",
    "cai mep": "SE Asia", "haiphong": "SE Asia", "manila": "SE Asia",
    "manila (north)": "SE Asia", "manila (south)": "SE Asia",
    "singapore": "SE Asia", "port klang": "SE Asia", "penang": "SE Asia",
    "laem chabang": "SE Asia", "bangkok": "SE Asia", "jakarta": "SE Asia",
    "surabaya": "SE Asia",
    # Lat Krabang ICD (Bangkok area inland container depot — appears as
    # "Lat Krab" / "Lat Krabang" in Lonny's RFQs). Added 2026-05-07.
    "lat krabang": "SE Asia", "lat krab": "SE Asia", "ladkrabang": "SE Asia",
    # Pasir Gudang — Malaysia's main industrial port near Johor. Added
    # 2026-05-17 after QC-015 flagged it as Unmapped in production data
    # (Michael "trade region.. nothing is every unmapped").
    "pasir gudang": "SE Asia",
    # Australia / NZ
    "sydney": "Oceania", "melbourne": "Oceania", "brisbane": "Oceania",
    "fremantle": "Oceania", "auckland": "Oceania",
    # Lyttelton — Christchurch's port, South Island NZ. Added 2026-08-16;
    # confirmed present in the real book as ol_260140 (Oakland → Lyttelton),
    # where it had been sitting Unmapped.
    "lyttelton": "Oceania",
    # Europe
    "hamburg": "Europe", "rotterdam": "Europe", "antwerp": "Europe",
    "felixstowe": "Europe", "le havre": "Europe", "algeciras": "Europe",
    "valencia": "Europe", "genoa": "Europe", "barcelona": "Europe",
    # Dublin (Ireland) — added 2026-05-28 after QC-015 surfaced it as
    # Unmapped. Ireland's main container port for Hilmar dairy exports
    # into the UK/Irish market.
    "dublin": "Europe",
    # Mid-East
    "jebel ali": "Middle East", "dammam": "Middle East", "jeddah": "Middle East",
    "ashdod": "Middle East", "haifa": "Middle East",
    # Africa
    "durban": "Africa", "lagos": "Africa", "cape town": "Africa",
    "mombasa": "Africa", "alexandria": "Africa",
    # South America
    "santos": "South America", "buenos aires": "South America",
    "callao": "South America", "valparaiso": "South America",
    # Central America (added 2026-05-07 per Michael 'handle all suggestions' —
    # Acajutla is El Salvador's main port, surfaced in Hilmar dairy export RFQs).
    "acajutla": "Central America", "puerto barrios": "Central America",
    "puerto cortes": "Central America", "puerto quetzal": "Central America",
    "puerto limon": "Central America", "balboa": "Central America",
    "manzanillo (panama)": "Central America",
    # Caucedo (DP World, Dominican Republic) — added 2026-05-27 after QC-015
    # surfaced it as Unmapped. Major Caribbean transshipment hub; grouped with
    # Central America to match the existing trade-lane buckets (no separate
    # Caribbean bucket — same role as Balboa / Manzanillo Panama for region
    # rollups in client reporting).
    "caucedo": "Central America",
    # North America inland (rare — usually a typo or a US-side movement
    # tracked in the same data file. Sturgis MI surfaced 2026-05-07).
    "sturgis mi": "North America", "sturgis": "North America",
}


def pending_substate(req: dict) -> str | None:
    """Split PENDING into its two materially different waits — per Michael
    2026-06-12 "on pending there should be several pending statuses to be
    clear": PENDING_OL (RFQ sent, OL hasn't quoted — chase OL) vs
    PENDING_HILMAR (OL quoted, Lonny hasn't decided — chase Lonny).
    Derived from the existing `quoted` flag at render time; the 4-status
    state machine, the data file, and the QC day-row math are untouched."""
    if req.get("status") != "PENDING":
        return None
    return "PENDING_HILMAR" if req.get("quoted") else "PENDING_OL"


def trade_region_for(destination: str | None) -> str:
    """Map a destination name to a trade region. Returns 'Unmapped' (NOT 'OTHER')
    for anything not in the map — Unmapped is the signal to extend the map.

    Country-qualified forms resolve to the same region as the bare port.
    Michael 2026-08-05, on a dashboard showing every row Unmapped: "unmapped
    shouldn't exist". Every one of those destinations — "Shanghai, CN",
    "Busan, KR", "Qingdao, CN", "Yokohama, JP" — was ALREADY in the map under
    its bare name. The map was never the problem; the lookup was. It tried the
    whole string and then the part before "(", so a comma-qualified name missed
    on both and fell through to Unmapped, and the standing instruction that
    Unmapped means "extend the map" sent every previous investigation off to
    add rows that were already there.

    So we peel comma segments off the tail, longest first, and take the first
    form the map actually knows. This only ever matches a key that is genuinely
    present — nothing is inferred from the country code itself — so it cannot
    invent a region for a port we have not classified. "Sturgis, MI" finds
    "sturgis"; "Rotterdam, NL" finds "rotterdam"; an unknown port stays
    Unmapped, which is still the signal to extend the map.
    """
    if not destination:
        return "Unmapped"
    # A UN/LOCODE is not a port name and has no region of its own — resolve it
    # to the port first. Without this a row still carrying the pre-fix "Jpyok"
    # (a carried-forward prior WIN, which is copied verbatim and never rebuilt)
    # would keep colouring the dashboard's Unmapped bucket pink forever.
    key = (resolve_locode(destination) or destination).strip().lower()
    for candidate in _region_lookup_forms(key):
        if candidate in _TRADE_REGION_MAP:
            return _TRADE_REGION_MAP[candidate]
    return "Unmapped"


def _region_lookup_forms(key: str):
    """Yield the forms of a destination to try against _TRADE_REGION_MAP, most
    specific first: the whole string and its progressively shorter comma
    prefixes, then the same for the part before any "(".

    So "Shanghai, CN" finds "shanghai" and "HCMC (Cat Lai)" finds "hcmc".

    Whole-string-first is what keeps the paren strip honest: "Manzanillo
    (Panama)" is an exact key and must resolve before anything reduces it to a
    bare "manzanillo", which is a different port on a different coast.
    """
    seen = set()
    for base in (key, key.split("(")[0].strip()):
        parts = base.split(",")
        for i in range(len(parts), 0, -1):
            form = ",".join(parts[:i]).strip()
            if form and form not in seen:
                seen.add(form)
                yield form


# Destinations that name no real port — a row still PENDING lane assignment,
# NOT a genuine "not in the region map" signal. Kept distinct from Unmapped so
# the CLIENT-facing rollup (gen_pdf) can drop them: after qc_selfheal FIX 1
# nulls a poisoned "Unknown" pod/destination, the row must not surface as a
# mystery "Unmapped" region in Lonny's PDF (2026-07-14, run 29292014093).
_UNRESOLVED_DEST_PLACEHOLDERS = frozenset({
    "", "unknown", "n/a", "na", "none", "null", "tbd", "-", "—",
})


def is_unresolved_destination(destination) -> bool:
    """True when `destination` names no real port — None, empty, or a garbage
    placeholder ("Unknown"/"N/A"/…). Such a row is pending lane assignment and
    is excluded from the CLIENT-facing trade-region rollup (see
    aggregate_trade_regions' ``include_unresolved``)."""
    if destination is None:
        return True
    if not isinstance(destination, str):
        return False
    return destination.strip().lower() in _UNRESOLVED_DEST_PLACEHOLDERS


def aggregate_trade_regions(requests: list[dict],
                            include_unresolved: bool = True) -> dict[str, dict]:
    """Roll requests up by trade region. Counts must reconcile to summary.

    ``include_unresolved`` (default True) keeps every row — an unresolved
    destination (None/""/"Unknown"/…) buckets to "Unmapped" via
    ``trade_region_for``, so the STAFF/QC totals reconcile to summary exactly
    as before. Pass ``False`` for CLIENT-facing surfaces (gen_pdf): rows with
    no real destination are DROPPED so a poisoned/placeholder row never renders
    as a mystery "Unmapped" region in front of the client. The caller
    reconciles the drop with a "+N unresolved" footnote (see
    ``is_unresolved_destination``). A real-but-unmapped destination string is
    NOT dropped — it still buckets to "Unmapped" as the extend-the-map signal.
    """
    out: dict[str, dict] = {}
    for r in requests:
        if not include_unresolved and is_unresolved_destination(r.get("destination")):
            continue
        region = trade_region_for(r.get("destination"))
        m = out.setdefault(region, {
            "region": region,
            "requests": 0, "wins": 0, "quoted_lost": 0, "not_quoted": 0, "pending": 0,
            "teu_requested": 0, "teu_won": 0, "teu_quoted_lost": 0, "teu_not_quoted": 0,
            "destinations": set(),
        })
        # SHIPMENTS, NOT ROWS — see shipment_count. requests and wins move
        # together on a multi-booking row, which is what keeps QC-075's
        # "reconciles to summary" line balanced against aggregate_summary.
        _n = shipment_count(r)
        m["requests"] += _n
        teu = r.get("teu_requested") or 0
        m["teu_requested"] += teu
        m["destinations"].add(r.get("destination") or "Unknown")
        st = r.get("status")
        if st == "WIN":
            m["wins"] += _n
            m["teu_won"] += r.get("teu_won") or teu
        # SAME predicate as aggregate_summary — is_not_quoted, not a
        # loss_reason test. This branch used `loss_reason == "NO_RESPONSE"`,
        # so a RESPONSE_NO_RATE row (OL acked the RFQ but sent no rate;
        # quoted=False) was counted Q&L here while aggregate_summary counted
        # it NQ. That is the "Volume by Trade Region: NQ 0 / Q&L 1" line
        # printed directly beneath the words "reconciles to summary" — the
        # divergence QC-075 now escalates instead of printing.
        elif is_not_quoted(r):
            # NQ FLOOR (NQ_VALID_FROM). Captured by THIS branch even when
            # floored, then counted only if valid. Letting a floored row fall
            # through to the LOSS branch below would book it as Quoted & Lost
            # — inflating losses and moving win rate, which is exactly what
            # this reset must not touch. It stays in `requests`/`teu_requested`
            # (incremented above), matching summary.total_entries, so QC-075's
            # reconciliation still balances.
            if nq_is_valid(r.get("request_date") or r.get("date")
                           or r.get("request_timestamp")):
                m["not_quoted"] += 1
                m["teu_not_quoted"] += teu
        elif st == "LOSS":
            m["quoted_lost"] += 1
            m["teu_quoted_lost"] += teu
        elif st == "PENDING":
            m["pending"] += 1
    for m in out.values():
        m["destinations"] = sorted(m["destinations"])
        # Per-trade-region win_rate also excludes NQ from the denominator
        # (CLAUDE.md §6 — NQ is "no contest happened"). Same bug fix as
        # aggregate_summary's headline KPI (track 03 finding C-1).
        win_rate_denom = m["wins"] + m["quoted_lost"]
        m["win_rate"] = round(m["wins"] / win_rate_denom * 100, 1) if win_rate_denom else 0.0
    return out

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.json"

# ─────────────────────────────────────────────────────────────────────
# Carrier normalization — dedup variants that are the same steamship line
# ─────────────────────────────────────────────────────────────────────
# Michael: "cma and cma-cgm are the same steamship line"
# Canonical names (right side) are used everywhere downstream: email, dashboard, scorecards.

CARRIER_ALIASES: dict[str, str] = {
    # CMA CGM family
    "CMA":       "CMA CGM",
    "CMACGM":    "CMA CGM",
    "CMA-CGM":   "CMA CGM",
    "CMA CGM":   "CMA CGM",
    "CMA-CGM GROUP": "CMA CGM",
    "CGM":       "CMA CGM",
    "ANL":       "CMA CGM",    # ANL is a CMA CGM subsidiary
    "APL":       "CMA CGM",    # APL is a CMA CGM subsidiary
    # Ocean Network Express (ONE)
    "ONE":       "ONE",
    "ONE LINE":  "ONE",
    "OCEAN NETWORK EXPRESS": "ONE",
    # Maersk family
    "MAERSK":    "Maersk",
    "MAERSK LINE": "Maersk",
    "MSK":       "Maersk",
    "SEALAND":   "Maersk",
    "HAMBURG SUD": "Maersk",
    # MSC
    "MSC":       "MSC",
    "MEDITERRANEAN SHIPPING CO": "MSC",
    # Hapag-Lloyd
    "HAPAG":     "Hapag-Lloyd",
    "HAPAG LLOYD": "Hapag-Lloyd",
    "HAPAG-LLOYD": "Hapag-Lloyd",
    "HLAG":      "Hapag-Lloyd",
    # Evergreen
    "EVERGREEN": "Evergreen",
    "EMC":       "Evergreen",
    # COSCO / OOCL (both Cosco Shipping Group but keep OOCL distinct since shippers price them separately)
    "COSCO":     "COSCO",
    "COSCON":    "COSCO",
    "OOCL":      "OOCL",
    # Yang Ming
    "YANG MING": "Yang Ming",
    "YML":       "Yang Ming",
    # HMM
    "HMM":       "HMM",
    "HYUNDAI":   "HMM",
    # ZIM
    "ZIM":       "ZIM",
    # Wan Hai
    "WAN HAI":   "Wan Hai",
    "WHL":       "Wan Hai",
    # OL's own operational spellings, from the 2026 customer transaction
    # report (2026-08-13). Added because the report is now the authority on
    # what Hilmar booked, and its names are the LEGAL entities rather than
    # the trade names this file already canonicalises: without these, ONE
    # appears twice (38 bookings as "ONE", 19 backfilled as "OCEAN NETWORK
    # EXPRESS PTE, LTD") and every carrier rollup splits one carrier in two.
    "OCEAN NETWORK EXPRESS PTE, LTD": "ONE",
    "OCEAN NETWORK EXPRESS PTE LTD":  "ONE",
    "OCEAN NETWORK EXPRESS, ONE":     "ONE",
    "CMA CGM SA":                     "CMA CGM",
    "MEDITERRANEAN SHIPPING LINES":   "MSC",
    "HAPAG-LLOYD AMERICA":            "Hapag-Lloyd",
    "EVERGREEN SHIPPING AGENCY (AMERICA)": "Evergreen",
    "HYUNDAI MERCHANT MARINE INC.":   "HMM",
}


#: request_id prefixes for rows with NO Lonny->OL RFQ chain behind them.
#:   stand_  a booking confirmation arrived with no matching RFQ.
#:   ol_     the booking was recovered from OL's operational export and no
#:           email exists AT ALL.
#:
#: Added 2026-08-13 after the SECOND place a bare `startswith("stand_")`
#: failed to recognise the 49 backfilled bookings. The first cost a blocked
#: fire (QC-039 graded them on a rate they cannot have); the second put all
#: 49 into QC-077's "quotes recorded with a rate or carrier but no response
#: time" banner on the report Michael reads — they carry carrier_quoted from
#: OL's export and can never have a response time, because there was never a
#: quote. Michael: "this is absurd ... we should be clean."
#:
#: One tuple, one predicate, so the next surface cannot know only half of it.
#: NOT every stand_ check should adopt this — qc_selfheal's scope purge
#: drops stand_ rows whose SUBJECT lacks HILMAR, and an ol_ row has no
#: subject at all, so adopting it there would delete every backfilled win.
NO_RFQ_CHAIN_PREFIXES = ("stand_", "ol_")


def has_no_rfq_chain(row_or_id) -> bool:
    """True when this row was recorded from a booking, not from an RFQ.

    Accepts a row dict or a bare request_id. These rows have no
    rate-response email, so rate/ETD/response-time fields are correctly
    absent rather than missing.
    """
    rid = (row_or_id.get("request_id") if isinstance(row_or_id, dict)
           else row_or_id) or ""
    return str(rid).startswith(NO_RFQ_CHAIN_PREFIXES)


def normalize_carrier(name: str | None) -> str | None:
    """Canonicalize a carrier string. Returns None on empty input; otherwise best-effort canonical.

    - Strips, collapses whitespace
    - Case-insensitive alias match
    - If no alias hit, returns Title-Cased version of the cleaned input (so unknowns still render cleanly)
    """
    if not name or not isinstance(name, str):
        return None
    cleaned = re.sub(r"\s+", " ", name).strip()
    if not cleaned:
        return None
    key = cleaned.upper()
    if key in CARRIER_ALIASES:
        return CARRIER_ALIASES[key]
    # Try stripping common suffixes
    stripped = re.sub(r"\s+(LINE|LINES|GROUP|SHIPPING|CO\.?|LTD\.?)$", "", key).strip()
    if stripped in CARRIER_ALIASES:
        return CARRIER_ALIASES[stripped]
    # Fall back: return the cleaned input with original casing preserved if it already looks canonical
    return cleaned


def _heal_session_paths(cfg: dict, config_file: Path) -> dict:
    """Auto-heal stale session-style absolute paths in cfg['paths'].

    Cowork mounts this project under /sessions/<session-id>/mnt/PROJECT HILMAR,
    and the session-id changes every session. config.json's paths.* are absolute
    and become stale on session change. Rather than re-migrating config.json on
    disk every run, we resolve the live root from the config file's own location
    and rewrite stale paths IN MEMORY ONLY — config.json is never mutated here.

    Logic:
      - live_root  = directory containing the config file
      - stale_root = cfg['paths']['root']
      - If stale_root != live_root and stale_root doesn't exist on disk,
        every path under cfg['paths'] that begins with stale_root is rewritten
        to live_root.
      - If stale_root resolves to a real directory (e.g. tests using a tmp dir),
        no rewrite happens — caller knows what they're doing.
    """
    paths = cfg.get("paths") or {}
    stale_root = paths.get("root")
    if not stale_root:
        return cfg
    live_root = str(config_file.resolve().parent)
    if stale_root == live_root:
        return cfg
    # If the configured root resolves to a real, accessible directory (e.g. test
    # harness using a tmp dir), trust it. PermissionError = inaccessible foreign
    # session mount; treat as stale and heal.
    try:
        if Path(stale_root).is_dir():
            return cfg
    except (PermissionError, OSError):
        pass  # treat as stale → fall through to rewrite
    # Rewrite every paths.* value whose prefix matches the stale root.
    healed = {}
    for k, v in paths.items():
        if isinstance(v, str) and v.startswith(stale_root):
            healed[k] = live_root + v[len(stale_root):]
        else:
            healed[k] = v
    healed["root"] = live_root
    cfg["paths"] = healed
    cfg.setdefault("_path_heal", {})
    cfg["_path_heal"] = {"stale_root": stale_root, "live_root": live_root}
    return cfg


def load_config(path: Path | str | None = None) -> dict:
    """Load config.json and auto-heal stale session paths in memory.

    Never writes to disk — the on-disk config can stay stale across sessions.
    See _heal_session_paths for the heal logic.
    """
    path = Path(path) if path else CONFIG_PATH
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    return _heal_session_paths(cfg, path)


def load_data(path: Path | str) -> dict:
    """Read the tracking file. ALWAYS utf-8 — never the platform default.

    `open(path)` uses locale.getpreferredencoding(), which on Windows is
    cp1252 UNLESS UTF-8 mode is on. Reading utf-8 bytes as cp1252 does not
    raise; it silently succeeds and turns every "→" into "â†’" and every "×"
    into "Ã—". The row then flows through ingest, gets written back out, and
    the mangling is permanent — the original bytes are gone.

    Every entry point we ship today does set UTF-8 mode — daily.yml and the
    Windows wrappers all export PYTHONUTF8=1 — so this is defence in depth,
    not a bug being fixed. The point of naming the codec here is that the
    guarantee then lives in the code rather than in an env var a NEW entry
    point can forget, and the failure mode if one does is a read that does
    not fail.
    """
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_data(data: dict, path: Path | str) -> None:
    """Persist the tracking file ATOMICALLY — temp file, fsync, os.replace.

    `open(path, "w")` truncates the destination the instant it is called and
    then streams into it. A crash, an OOM kill, or a cancelled CI job partway
    through left tracking-data-v2.json truncated mid-JSON — and the daily
    workflow's blob push runs under `if: always()`, so that half-written file
    was then uploaded over the canonical state. The state store's own backup
    could not help: it snapshots the same corrupt file.

    os.replace is atomic on POSIX, so a reader either sees the entire previous
    file or the entire new one, never a partial write. The fsync is what makes
    that promise survive a machine-level crash rather than just a process one:
    without it the rename can reach disk before the bytes it points at do.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, default=str)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        # Never leave a stray .tmp behind to be mistaken for real state.
        tmp.unlink(missing_ok=True)
        raise


def validate_data_shape(data: dict, strict: bool = False) -> tuple[bool, list[str]]:
    """Lightweight schema validation of tracking-data-v2.json shape.

    Added 2026-05-14 per best-practices batch — catches structural drift
    before bad data writes to disk. NOT a full JSON Schema (we already have
    schema.json for full validation if needed); this is a fast invariant
    check that runs in every save_data path.

    Returns (is_valid, list_of_issues).
    is_valid=False with strict=True raises ValueError.
    """
    issues = []
    # Top-level keys
    for key in ("requests", "summary", "version"):
        if key not in data:
            issues.append(f"missing top-level key: {key}")

    # requests must be a list of dicts
    reqs = data.get("requests")
    if reqs is not None and not isinstance(reqs, list):
        issues.append(f"requests must be list, got {type(reqs).__name__}")
    elif isinstance(reqs, list):
        for i, r in enumerate(reqs):
            if not isinstance(r, dict):
                issues.append(f"requests[{i}] not a dict: {type(r).__name__}")
                continue
            # Each request must have at minimum request_id, status, lane
            for req_key in ("request_id", "status"):
                if req_key not in r:
                    issues.append(f"requests[{i}] missing {req_key}")
            # status must be in VALID_STATUSES
            if r.get("status") and r["status"] not in VALID_STATUSES:
                issues.append(f"requests[{i}] invalid status: {r['status']}")
            # loss_reason must be in LOSS_REASONS if set
            lr = r.get("loss_reason")
            if lr and lr not in LOSS_REASONS:
                issues.append(f"requests[{i}] invalid loss_reason: {lr}")

    # summary must be a dict
    summary = data.get("summary")
    if summary is not None and not isinstance(summary, dict):
        issues.append(f"summary must be dict, got {type(summary).__name__}")

    ok = len(issues) == 0
    if strict and not ok:
        raise ValueError("Schema validation failed: " + "; ".join(issues[:5]))
    return ok, issues


def save_data_validated(data: dict, path: Path | str, strict: bool = True) -> None:
    """save_data + schema validation gate. Use everywhere we write
    tracking-data-v2.json so structural drift gets caught early.
    """
    ok, issues = validate_data_shape(data, strict=False)
    if not ok and strict:
        raise ValueError(
            f"Refusing to save invalid data to {path}: " + "; ".join(issues[:5])
        )
    save_data(data, path)


# ─────────────────────────────────────────────────────────────────────
# Timezone helpers (DST-safe — this is critical)
# ─────────────────────────────────────────────────────────────────────

def parse_iso(ts: str | None) -> datetime | None:
    """Parse ISO8601 timestamp into an aware UTC datetime. Returns None on bad input."""
    if not ts or not isinstance(ts, str):
        return None
    try:
        s = ts.strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)
    except Exception:
        return None


def to_et(dt: datetime | None) -> datetime | None:
    return dt.astimezone(ET) if dt else None


def to_pt(dt: datetime | None) -> datetime | None:
    return dt.astimezone(PT) if dt else None


#: UN/LOCODE → the ONE display spelling this book uses for that port.
#:
#: A UN/LOCODE is the 5-character UNECE code for a place (2-letter ISO-3166
#: country + 3-letter locality). OL and the carriers write them into booking
#: subjects and POD columns interchangeably with the port name. Nothing in
#: this repo knew what one was until 2026-08-27, so `body_parser._norm` — which
#: Title-Cases any all-caps token longer than three characters — turned the
#: code `JPYOK` into the fake port name "Jpyok", and Yokohama (44 of the 134
#: bookings in data/ol-transaction-report-2026.json — the largest lane in the
#: book) split across two spellings. `aggregate_lanes` and
#: `compute_lane_winning_medians` both key on the raw "Oakland → X" display
#: string, so the split also starved the Yokohama winning median below
#: PRICE_GAP_MIN_LANE_WINS and flipped that lane's Q&L losses from PRICE to
#: UNDIFFERENTIATED. Michael, confirming the identity himself: "JPYOK and
#: Yokohama are same JPYOK is the UN LOC code for Yokohama".
#:
#: TABLE-GATED, AND THAT IS THE WHOLE SAFETY ARGUMENT. A shape rule ("5 caps
#: letters is a LOCODE") would eat BUSAN, OSAKA, TOKYO, GENOA, HAIFA and LAGOS
#: — every one a real port in this corpus. Only codes listed HERE resolve; an
#: unrecognised code stays raw, lands as an unmapped destination, and trips
#: QC-015, which now names it as a possible LOCODE. Absent code = one warning
#: and a one-line PR. Wrong code = two real ports silently merged forever.
#:
#: SEEDED FROM EVIDENCE, NOT FROM MEMORY. JPYOK is the only entry because it
#: is the only code this book has actually produced and the only one the
#: operator has confirmed. The remaining ports of KNOWN_DESTINATIONS are NOT
#: pre-seeded: their codes could not be verified against the UNECE list in the
#: session that wrote this (egress to unece.org / unlocode.info is blocked
#: from the runner), and CLAUDE.md forbids guessing into production. Add each
#: one when a fire actually surfaces it, with the UNECE citation in the
#: comment. tests/test_locode_merge.py::test_every_locode_value_is_a_real_
#: corpus_port refuses any entry whose value is not already a
#: KNOWN_DESTINATIONS spelling that maps to a real trade region.
PORT_LOCODES = {
    # UNECE JP / Yokohama. Confirmed by the operator 2026-08-27 and observed
    # in production as the "Jpyok" row of the dashboard's Unmapped bucket.
    "JPYOK": "Yokohama",
}


def resolve_locode(value) -> str | None:
    """The port name a UN/LOCODE names, or None when `value` is not a code
    WE HAVE LISTED.

    Case-insensitive on purpose: by the time a stored row reaches a reader,
    `_norm` has already Title-Cased the code, so the damaged spelling on disk
    is "Jpyok", not "JPYOK". Matching both is what lets the fix reach rows
    that were written before it shipped without rewriting stored history.

    Returns the DISPLAY spelling ("Yokohama"). Callers that need a matching
    key lowercase it themselves — `canonical_port_key` does exactly that, so
    resolve_locode("JPYOK") and canonical_port_key("Yokohama") agree.

    Never guesses. A 5-letter token that is not a key here returns None and
    the caller keeps the raw text, which is what keeps BUSAN/OSAKA/TOKYO/
    GENOA/HAIFA/LAGOS intact.
    """
    if not value or not isinstance(value, str):
        return None
    token = value.strip()
    if len(token) != 5 or not token.isalpha():
        return None
    return PORT_LOCODES.get(token.upper())


#: Ports that Lonny and OL routinely call by DIFFERENT names for the SAME
#: destination. Until 2026-07-26 the only lane key in the system was
#: `.strip().lower()`, so "HCMC" and "Cat Lai" were different lanes — which is
#: precisely how OL's booking confirmation ("Oakland to Cat Lai") failed to
#: link to Lonny's RFQ ("Oakland to HCMC"). The unlinked booking then became a
#: fabricated `stand_<mdolx>` WIN row, so ONE shipment was stored TWICE: once
#: as a won Oakland→Cat Lai booking and once as the still-open Oakland→HCMC
#: request. TEU double counted, a phantom lane in Lane Performance, and 24h
#: later the orphaned PENDING copy aged into a LOSS reporting that OL never
#: quoted a move OL had actually booked.
#:
#: DELIBERATELY CONSERVATIVE. Only names for the same place, or terminals of
#: one port complex that the two sides genuinely use interchangeably, are
#: merged. Distinct ports with distinct rates (Bangkok vs Laem Chabang,
#: Tokyo vs Yokohama) are NOT merged — collapsing those would cross-match real
#: separate business, which is a worse failure than the one being fixed.
_PORT_ALIASES = {
    # Ho Chi Minh City — Cat Lai and Cai Mep are its two container terminals.
    # Lonny asks for "HCMC"; OL confirms whichever terminal the vessel calls.
    "hcmc": "hcmc", "ho chi minh": "hcmc", "ho chi minh city": "hcmc",
    "saigon": "hcmc", "cat lai": "hcmc", "cai mep": "hcmc",
    "cat lai port": "hcmc", "cai mep port": "hcmc",
    # Manila North / South are terminals of one port.
    "manila": "manila", "manila north": "manila", "manila south": "manila",
    # Busan (formerly romanised Pusan).
    "busan": "busan", "port busan": "busan", "pusan": "busan",
    # Lat Krabang ICD — three spellings appear in real RFQs.
    "lat krabang": "lat krabang", "lat krab": "lat krabang",
    "ladkrabang": "lat krabang", "lad krabang": "lat krabang",
    # Hong Kong.
    "hong kong": "hong kong", "hongkong": "hong kong",
}


def canonical_port_key(destination) -> str:
    """THE lane-matching key — one name per physical destination.

    Used on BOTH sides of every destination comparison (booking→request
    linking, rate-response attachment, QC duplicate detection) so the two
    sides cannot disagree about what counts as the same place.

    Resolution order, most specific first:
      1. the whole string ("cat lai" → "hcmc")
      2. the head before a parenthetical ("HCMC (Cat Lai)" → "hcmc")
      3. the parenthetical itself ("Vietnam (Cat Lai)" → "hcmc")
    Unknown names fall through to their own lowercased head, so this is a
    strict refinement of the old `.strip().lower()`: it can only ever merge
    names the map explicitly lists, never split ones that used to match.

    This is a MATCHING key, not a display value — `title_case_destination`
    still renders "HCMC (Cat Lai)" for humans.
    """
    raw = (destination or "").strip().lower()
    if not raw:
        return "unknown"
    # A UN/LOCODE collapses to the port it names BEFORE anything else, so a
    # stray code that got past the write-side normalizers still matches the
    # port on THIS side of every comparison. Lowercased here, which is why
    # resolve_locode returns the display spelling and this returns the key:
    # canonical_port_key("JPYOK") == canonical_port_key("Yokohama").
    _loc = resolve_locode(raw)
    if _loc:
        raw = _loc.strip().lower()
    if raw in _PORT_ALIASES:
        return _PORT_ALIASES[raw]
    head, paren = raw, None
    m = re.match(r"^([^(]+?)\s*\((.+)\)\s*$", raw)
    if m:
        head, paren = m.group(1).strip(), m.group(2).strip()
    if head in _PORT_ALIASES:
        return _PORT_ALIASES[head]
    if paren and paren in _PORT_ALIASES:
        return _PORT_ALIASES[paren]
    return head or raw


def port_terminal(destination) -> str:
    """The terminal a destination names, or "" when it names only a city.

    "Manila (North)" -> "north";  "Manila" -> "";  "HCMC (Cat Lai)" -> "cat lai"
    """
    m = re.match(r"^[^(]+\(\s*(.+?)\s*\)\s*$", (destination or "").strip())
    return m.group(1).strip().lower() if m else ""


def same_port(a, b) -> bool:
    """True when two destinations can refer to the SAME physical call.

    Same canonical city (`canonical_port_key`), AND — when BOTH sides name a
    terminal — the same terminal. A terminal-less side matches either.

    Exists because rate-response matching fell back to a bare substring test
    ("manila" in "manila (north)"), which pooled Manila (North) and Manila
    (South) as one candidate set. A reply on a thread titled "RE: Oakland to
    Manila" could then write ol_rate, carrier_quoted, etd_offered and
    vessel_voyage onto the WRONG terminal's row — reporting a South-terminal
    rate to the client as the North lane's quote, while the correct request
    stayed unquoted and aged out as NQ.
    """
    if canonical_port_key(a) != canonical_port_key(b):
        return False
    ta, tb = port_terminal(a), port_terminal(b)
    if ta and tb:
        return ta == tb
    return True


def et_date_of(ts) -> str | None:
    """THE canonical ET calendar date for a timestamp — the one clock every
    day bucket in this system runs on.

    Exists because `request_date` had THREE conflicting producers as of
    2026-07-26: ingest wrote the UTC calendar date, merge_ingest took a raw
    `ts[:10]` UTC slice, and qc_selfheal's heal wrote PT — while every reader
    (gen_email, gen_dashboard, gen_client_email, the day reconciliation)
    buckets by the ET business day from `report_business_day`. An RFQ sent
    Friday 5:30 PM PT is 2026-07-25 in UTC and Friday 2026-07-24 in ET; since
    no fire ever reports a Saturday, that row appeared in NO day's New
    Requests, KPI tile or reconciliation on any day, ever — while still
    counting toward the period totals, so the day tiles and period tiles
    disagreed by exactly the rows the clocks disagreed about.

    Accepts an ISO string or a datetime. Returns None when it cannot parse,
    so callers can fall back rather than invent a date. Date-only strings
    ("2026-07-24") pass through unchanged — they carry no timezone to
    convert, and re-interpreting them as midnight UTC would shift them a day
    in exactly the direction this function exists to prevent.
    """
    if not ts:
        return None
    if isinstance(ts, datetime):
        dt = ts
    elif isinstance(ts, str):
        if len(ts.strip()) == 10 and ts.strip().count("-") == 2:
            return ts.strip()
        dt = parse_iso(ts)
    else:
        return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(ET).date().isoformat()



def booking_count(r) -> int:
    """How many BOOKINGS a won row represents.

    Michael, 2026-08-24, asked whether an RFQ booked as three shipments is one
    win or three: "no it would be three requests to three wins". Multiple
    MDOLX refs live on ONE row (ingest.py:1044 unions them into
    mdolx_refs_all), and every count in the reports was row-based, so a
    three-shipment RFQ reported as a single win. That understated wins and,
    with them, the win rate.

    Returns 0 for a row that is not a win, so callers can sum this directly.
    Returns 1 for a win with no MDOLX recorded — the booking happened even if
    its reference did not reach us; dropping it would lose a real shipment.
    """
    if (r.get("status") or "").upper() != "WIN":
        return 0
    refs = {x for x in [r.get("mdolx_ref"), *(r.get("mdolx_refs_all") or [])] if x}
    return len(refs) or 1


def shipment_count(r) -> int:
    """How many SHIPMENTS one tracker row represents. THE counting rule.

    Michael, 2026-08-24: "count shipments, not emails ... no it would be
    three requests to three wins". `booking_count` said what a WIN row is
    worth; this says what ANY row is worth, and it exists because saying it
    in only one place is what broke.

    #223 wired booking_count into gen_weekly_summary.analyze_week and
    NOWHERE else. The result, verified on main 2026-08-26: an RFQ booked as
    three shipments counts 3 in the weekly KPI tile, 1 in the same email's
    Top Winning Lanes (`by_lane[lane]["wins"] += 1`), 1 in Carrier of the
    Week, 1 in the daily email's win tile, and 1 in the period-to-date
    summary every renderer reads. One booking, five numbers, in reports
    Michael reads side by side — the exact shape of the 2026-08-24
    complaint ("how are there 16 requests with 9 wins and 10 losses"),
    one level down.

    A WIN row is worth its distinct MDOLX refs. Every other row is worth 1:
    a quote that lost is one request and one loss, and a pending row is one
    request, whatever it may later book as.

    THE DENOMINATOR EXPANDS WITH THE NUMERATOR. Michael, on the same day:
    "there are no bookings without rfqs" — three bookings are three
    requests. So callers must count REQUESTS with this function too, never
    with len(). Counting wins by shipment and requests by row is how a
    175% quote rate gets printed.
    """
    return booking_count(r) or 1

def win_event_date(r) -> str | None:
    """THE ET calendar date a WIN happened — the ONE clock every report must
    credit a booking to.

    Exists because the daily email and the weekly summary disagreed about
    which period a booking belonged to. Michael directed on 2026-07-21 ("a win
    belongs to the day Lonny booked it") that the daily count wins by EVENT
    date, so `gen_email._today_summary` counts →WIN status_history transitions
    dated the report day. `gen_weekly_summary` was never changed: it filters
    every row — wins included — by `request_date`. An RFQ received Friday and
    booked the following Monday is therefore a win in Monday's daily email
    (week of the 27th) and a win in the PREVIOUS week's summary (week of the
    20th). The same booking, credited to two different weeks, in two reports
    Michael reads side by side.

    Returns the ET date of the row's LAST →WIN transition. Falls back to
    `request_date` for legacy WIN rows recorded before transitions were kept,
    so every WIN lands in exactly one bucket and none vanishes. Returns None
    for a row whose CURRENT status is not WIN — a row that was won and then
    reversed is not a win, and must not be credited to any period.

    LAST transition, not any: a row reversed out of WIN and later re-won has
    two →WIN entries, and testing "any transition on this day" credited it to
    both days. The booking that stands is the latest one.

    Self-transitions (WIN→WIN) are NOT win events (2026-08-12). The
    operator-corrections applier appends a fire-time WIN→WIN entry on every
    fire it re-applies a correction, so counting them re-dated stand_260905's
    April... rather, its Jul-9 booking to "today", every day — the last
    survivor of the rolling-win defect (diag-weekly on run 31611357523). A
    row's win event is when it BECAME a win, not when a correction re-touched
    a row that already was one.
    """
    if (r.get("status") or "").upper() != "WIN":
        return None
    # booking_timestamp FIRST (2026-08-21). It is the booking's own clock —
    # the confirmation email's send time, or the date an operator supplied
    # when back-entering one from OL's transaction report. The transition
    # stamp is only ever a proxy for it, and for a back-entered booking the
    # proxy is the day the tracker was TOLD, which is how 49 Jan-Apr bookings
    # were all credited to one week in August.
    booked = et_date_of(r.get("booking_timestamp"))
    if booked:
        return booked
    dated = [d for d in (et_date_of(h.get("at"))
                         for h in (r.get("status_history") or [])
                         if h.get("to") == "WIN" and h.get("from") != "WIN") if d]
    if dated:
        return max(dated)
    return et_date_of(r.get("request_date") or r.get("request_timestamp"))


def now_utc() -> datetime:
    return datetime.now(UTC)


# Reporting window. The daily fire moved to ~6 PM ET (2026-06-16, Michael
# "move this to end of every day"), so it now reports the CURRENT, now-complete
# Pacific business day instead of the previous one. Override to "previous" via
# HILMAR_REPORT_WINDOW to roll back to the old 10 AM ET morning behavior.
REPORT_WINDOW = os.environ.get("HILMAR_REPORT_WINDOW", "current").strip().lower()

def report_business_day(now_et=None, window=None):
    """The business day the daily email REPORTS ON, as a date.

    window="current"  (evening fire): today if a weekday; Friday on Sat/Sun.
    window="previous" (old 10 AM fire): the most recent COMPLETE business day
                       before today (Mon->Fri, Tue->Mon, ... Sat/Sun->Fri).
    `now_et` may be an aware datetime in ET or a date; defaults to wall-clock ET.
    """
    if now_et is None:
        now_et = datetime.now(timezone.utc).astimezone(ET)
    today = now_et.date() if hasattr(now_et, "date") else now_et
    # WEE-HOURS RULE (2026-07-02): the fire is an EVENING fire (~6 PM ET). A
    # run between midnight and 6 AM ET is a very-late cron tick or an
    # after-hours manual dispatch — either way the calendar day that just
    # STARTED is empty, and the meaningful report day is the business day
    # that just ENDED. Without this, a 12:38 AM Thursday dispatch reported an
    # all-zero "Thu" to the full distribution AND poisoned Thursday's
    # send-flag + mailbox guard, blocking the real Thursday-evening send
    # (live failure, run #76). Applies only when a time-of-day is known;
    # date-only inputs (tests, explicit report dates) are untouched.
    if hasattr(now_et, "hour") and now_et.hour < 6:
        today = today - timedelta(days=1)
    wd = today.weekday()  # Mon=0..Sun=6
    win = (window or REPORT_WINDOW)
    if win == "current":
        if wd == 5:  return today - timedelta(days=1)   # Sat -> Fri
        if wd == 6:  return today - timedelta(days=2)    # Sun -> Fri
        return today                                     # Mon-Fri -> today
    # "previous"
    if wd == 0:   delta = 3
    elif wd == 5: delta = 1
    elif wd == 6: delta = 2
    else:         delta = 1
    return today - timedelta(days=delta)


# ─────────────────────────────────────────────────────────────────────
# Status-form helpers — work against EITHER LEGACY (WIN/LOSS/PENDING
# with `quoted` disambiguator) OR STRICT (WIN/Q&L/NQ/PENDING) storage.
# Ported from src/hilmar/core.py 2026-06-02 so QC-017 (and any other
# cross-form check) doesn't have to inline the same logic with subtle
# drift risk. tests/test_core_parity.py locks parity.
# ─────────────────────────────────────────────────────────────────────

def display_status(r: dict) -> str:
    """Return the 4-state DISPLAY label regardless of storage form.

    A row written by scripts/ingest.py (3-state LEGACY) and a row
    written by src/hilmar/ingest.py (4-state STRICT) both return the
    same label after this normalization:
      WIN   → WIN
      LOSS + quoted=True  → Q&L      (storage was LEGACY)
      LOSS + quoted=False → NQ       (storage was LEGACY)
      Q&L   → Q&L                    (storage was STRICT)
      NQ    → NQ                     (storage was STRICT)
      PENDING → PENDING
    """
    s = (r or {}).get("status")
    if s == "LOSS":
        return "Q&L" if (r or {}).get("quoted") else "NQ"
    return s


def is_quoted_and_lost(r: dict) -> bool:
    """True if row is quoted-and-lost in EITHER classifier form."""
    s = (r or {}).get("status")
    if s == "Q&L":
        return True
    return s == "LOSS" and bool((r or {}).get("quoted"))


def is_not_quoted(r: dict) -> bool:
    """True if row is not-quoted in EITHER classifier form."""
    s = (r or {}).get("status")
    if s == "NQ":
        return True
    return s == "LOSS" and not (r or {}).get("quoted")


def is_loss(r: dict) -> bool:
    """True if row is any kind of loss (quoted-and-lost OR not-quoted).
    Covers both LEGACY LOSS and STRICT Q&L/NQ rows.
    """
    s = (r or {}).get("status")
    return s == "LOSS" or s == "Q&L" or s == "NQ"


# ─────────────────────────────────────────────────────────────────────
# "Is there a real rate on this row?" — ONE predicate.
#
# qc_selfheal's NQ-contamination heal writes the STRING "Not Quoted" into
# ol_rate as a sentinel, so `ol_rate is not None` reads that sentinel as a
# quote. PR #148 fixed that for QC-077 by adding _is_real_rate in
# qc_selfheal.py — and left four other spellings of the same question in
# place, which is how the staff email's undated-quotes note and the QC-077
# banner came to report different counts off the same data. Copilot caught it.
#
# It lives in core because the consumers are in different modules and
# gen_email cannot import qc_selfheal to get at it. tests/test_audit_batch8
# holds the sentinel list to the heal that writes it.
# ─────────────────────────────────────────────────────────────────────

NON_RATE_SENTINELS = ("", "not quoted", "n/a", "none", "null", "—", "-")


def is_real_rate(v) -> bool:
    """True only when ol_rate holds an actual quoted amount.

    Note what this is NOT for: deciding whether an ol_rate needs normalising
    to the sentinel. That asks "is this already the sentinel", a different
    question with a different answer for "—" and "N/A", and the NQ-
    contamination heal keeps its own guard for it.
    """
    if v is None:
        return False
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return True
    return str(v).strip().lower() not in NON_RATE_SENTINELS


def has_quote_evidence(r: dict) -> bool:
    """True when a row carries a real rate OR a quoted carrier — the shared
    'OL responded with something' test behind the undated-quotes note, the
    QC-077 banner, and the quoted-flag reconciliation."""
    r = r or {}
    return bool(is_real_rate(r.get("ol_rate")) or r.get("carrier_quoted"))


#: NOT-QUOTED RESET. Michael 2026-08-14, on the report's "Not Quoted — Last 14
#: Days (9 listed • 25 total • 104 TEU)" section: "get rid of thjs as all
#: quoted / and restart the count monday."
#:
#: NQ means "Lonny asked and OL never answered". Every row in that section was
#: in fact ANSWERED — the replies went To: Lonny with the group copied and
#: never reached the mailbox this pipeline reads, which is the same root cause
#: that produced the empty OL-USA RESPONSES section and forced the turnaround
#: reset. So the label was an artefact of our visibility, not OL's behaviour,
#: and it was being shown to the CEO as a list of OL failures.
#:
#: Same shape as TIMING_VALID_FROM, deliberately: a row asked BEFORE this
#: floor is not counted or listed as Not Quoted, and the report SAYS SO rather
#: than quietly shrinking. Monday 2026-08-17 is the restart Michael named.
#:
#: WHAT THIS DOES NOT TOUCH, and must not: wins, Q&L, TEU volumes, lane
#: rollups, or win rate. Win rate is Wins/(Wins+Q&L) and has never included
#: NQ, so suppressing NQ cannot move it. The rows keep their stored status —
#: nothing is deleted, and clearing the floor restores them in one line.
NQ_VALID_FROM = "2026-08-17"


def nq_is_valid(when) -> bool:
    """True when a request is recent enough to be CALLED Not Quoted.

    Mirrors timing_is_valid: falsy floor ⇒ everything counts, so retiring
    this is a one-line change. A row with no parseable request date counts
    as valid — an undateable row is a data defect that should stay visible,
    not something to hide behind the floor.
    """
    if not NQ_VALID_FROM:
        return True
    s = when if isinstance(when, str) else (when.isoformat() if when else "")
    if not s:
        return True
    return s[:10] >= NQ_VALID_FROM


def counts_as_not_quoted(r: dict) -> bool:
    """is_not_quoted AND recent enough to be called that (NQ_VALID_FROM).

    ONE predicate for the count, the listing, the TEU tally and the QC
    aggregate check. They are four different call sites over one dataset and
    they have drifted before; QC-020b exists because a display filter once
    leaked into the aggregate.
    """
    r = r or {}
    return is_not_quoted(r) and nq_is_valid(
        r.get("request_date") or r.get("date") or r.get("request_timestamp"))


def nq_reset_note(short: bool = False) -> str:
    """One sentence for the report, or "" once the floor is retired."""
    if not NQ_VALID_FROM:
        return ""
    if short:
        return f"Not-Quoted count restarted {NQ_VALID_FROM}"
    return (
        f"Not-Quoted counting restarted {NQ_VALID_FROM}. Earlier requests are "
        f"not listed or counted here: OL did answer them, but those replies "
        f"went to Lonny with the group copied and never reached this mailbox, "
        f"so calling them unanswered measured our visibility, not OL. Wins, "
        f"losses, TEU volumes and win rate are unaffected."
    )


#: How recent an undated quote has to be before the report says anything about
#: it. Michael 2026-08-13, on a banner reporting 16: "all that truly matters at
#: end of days is the wins and losses. turnaround is secondary for the past
#: moves.. so clear this error".
#:
#: He is right, and the banner's own wording was part of why it read as alarming
#: — "they cannot be dated and are NOT COUNTED above" meant "absent from the
#: dated OL-USA RESPONSES table", but read as "missing from the totals". Every
#: one of those 16 rows IS counted in wins, losses, TEU and every lane rollup.
#: The only thing missing is WHEN OL sent the quote, which feeds turnaround —
#: and for moves that already resolved, turnaround is history nobody can act on.
#:
#: So the gap is reported only while it is still actionable: a quote from this
#: week with no send time is worth chasing, one from April is a permanent,
#: known, accepted condition. NOT deleted — a silent detector is how the count
#: reached 41 unnoticed in the first place; the audit still states the backlog,
#: it just stops calling it an error.
UNDATED_QUOTE_RECENT_DAYS = 14


def undated_quote_is_current(r: dict, now: datetime | None = None) -> bool:
    """True when an undated quote is recent enough to still be worth chasing.

    Anchored on the row's own request timestamp — the one clock these rows
    always carry (their whole defect is having no response time). A row with
    no usable request date counts as CURRENT, deliberately: an undateable row
    that is also unanchored is a data defect, and defaulting it to "old" would
    hide exactly the shape most worth seeing.
    """
    ts = parse_iso((r or {}).get("request_timestamp") or (r or {}).get("request_date"))
    if ts is None:
        return True
    now = now or now_utc()
    return (now - ts).total_seconds() <= UNDATED_QUOTE_RECENT_DAYS * 86400


def quote_evidence_is_booking_derived(r: dict) -> bool:
    """True when a row's ONLY 'OL quoted' evidence is a carrier a BOOKING
    could have written.

    2026-08-13, Michael on the QC-077 banner: "still shouldn't exist". Measured
    on stored state (diag-blob 31732181146), the banner's 22 rows split:

        10  LOSS, rate present, no booking ref     <- real undated quotes
         8  WIN, NO rate, booking ref, operator-corrected
         3  WIN, rate present, booking ref
         1  WIN, NO rate, booking ref

    Nine of the 22 carry NO rate at all. Their only evidence is
    `carrier_quoted`, and on those rows the carrier was written by the
    reconciliation that folded in OL's transaction report — CMA CGM on
    MDOLX261026-33, ONE on MDOLX261068. That is BOOKING evidence. It says a
    shipment moved and on whose vessel; it says nothing about a quote email
    ever arriving, and for Jun-Aug none did — OL replied to Lonny with the
    group copied and it never reached the mailbox we read.

    So has_quote_evidence's `rate OR carrier` is right for "did OL respond
    with something" and wrong for "is there a quote here we failed to date".
    A row like this is not an undated quote; it is a booking whose quote we
    never received, and reporting it as a data defect sends the reader looking
    for a message that does not exist.

    NARROW BY CONSTRUCTION. A real rate always wins — a row with a rate is a
    quote, whatever else it carries. Absent a booking reference, a bare
    carrier still counts as a quote, because OL does occasionally quote a
    carrier with the rate to follow (see QC-056's own note). Only the
    intersection — no rate, a carrier, AND a booking that explains it — is
    excluded.
    """
    r = r or {}
    if is_real_rate(r.get("ol_rate")):
        return False
    if not r.get("carrier_quoted"):
        return False
    return bool(r.get("mdolx_ref") or r.get("mdolx_refs_all")
                or r.get("booking_no") or r.get("booking_timestamp"))


# ─────────────────────────────────────────────────────────────────────
# The send time of a cached email body — ONE reader, both schemas.
#
# 2026-08-06. The undated-quote count went 29 (07-30) → 41 (08-05) → 43
# (08-06) THROUGH the heal shipped on 08-05 to shrink it. The heal never dated
# a single row, and could not have:
#
#   fetch_bodies.upsert_body writes    "sent_ts" / "received_ts"
#   qc_selfheal._body_send_time read   "sent" / "sentDateTime" / "received"
#   patch_carriers._load_bodies_by_imid read the same wrong three
#
# stage_emails.txt really does use sent/received; stage_emails_bodies.txt uses
# sent_ts/received_ts. Two file schemas, one concept, and both healers reached
# for the other file's spelling — so every lookup returned None, silently, and
# the QC-077 set became monotonic: rate recovery keeps ADDING undated rows and
# nothing could ever remove one. refresh_stage.py:254 already read both
# spellings, which is the tell that the split was known and unshared.
#
# SEND before RECEIVED: when OL quoted is the send time. Received is the
# fallback because a record missing sentDateTime still pins the quote to
# within a delivery hop, and an approximately-dated quote beats one that is
# invisible to every dated section forever.
BODY_SEND_TIME_FIELDS = (
    "sent_ts", "sentDateTime", "sent",
    "received_ts", "receivedDateTime", "received",
)


def body_send_time(rec) -> str | None:
    """The moment the cached OL message was sent, whichever schema wrote it.

    tests/test_body_send_time.py builds a record through the REAL writer and
    asserts this finds it, so the reader is pinned to the writer rather than
    to a list someone has to remember to update.
    """
    for f in BODY_SEND_TIME_FIELDS:
        v = (rec or {}).get(f)
        if v:
            return v
    return None


def quote_evidence_ok(sender_email, sent_ts, request_timestamp) -> bool:
    """May this cached message serve as evidence of an OL QUOTE on this row?

    2026-08-11, the phantom-Q&L machine. Lonny re-uses Outlook threads, so his
    new ask carries the PREVIOUS quote quoted below it. Every recovery heal
    read bodies by the row's source_imids — which, on a rebuilt request row,
    is the ask itself — and each step then "recovered" a fact out of Lonny's
    own email: a carrier, then quoted=True, then the old rate, then a
    response_timestamp equal to the ask's own send time. Individually each
    step was recovery; jointly they FABRICATED a same-day OL quote. When OL's
    real replies stopped being staged (~Jul 24, the Reno intake gap), the
    fabricated ones became the only quotes, and W31/W32 rendered as 25
    requests quoted-and-lost — the "consistently wrong" table.

    Two facts a message must prove before it can evidence a quote:
      1. OL WROTE IT. sender must end @ol-usa.com — a quote cannot come out
         of the customer's own email. Missing sender fails CLOSED: a stamp
         that cannot prove authorship is a guess, and an undated quote that
         QC-077 flags honestly beats a dated fabrication.
      2. IT POSTDATES THE ASK. A reply sent at-or-before the request it
         answers is an impossible ordering (QC-066's class). Rows without a
         request_timestamp (standalones, legacy) skip this half.
    """
    s = str(sender_email or "").strip().lower()
    if not s.endswith("@ol-usa.com"):
        return False
    sent_dt = parse_iso(sent_ts)
    req_dt = parse_iso(request_timestamp)
    return not (sent_dt and req_dt and sent_dt <= req_dt)


def format_date_range(value, fallback_start=None, fallback_end=None) -> str:
    """Render the data window as prose, from EITHER shape it is stored in.

    Michael 2026-08-06, on the production email header, which read:

        Reporting Wednesday August 5, 2026 — the prior business day ·
        {'start': '2026-04-02', 'end': '2026-08-05'} | Updated: ...

    A Python dict repr, in a header nine people read every morning. The cause
    is two writers with two shapes for one fact:

        scripts/ingest.py      → {"start": ..., "end": ...}   (production)
        scripts/merge_ingest.py→ "2026-04-02 to 2026-08-05"   (a string)
        tests/fixtures/        → a string

    schema.json permits both (`oneOf [string, object]`), so neither writer is
    wrong. The READERS were: gen_email, gen_pdf and gen_carrier_scorecard_pdf
    each did `data.get("date_range") or <fallback>` and interpolated the
    result, which stringifies a dict as its repr. A dict is truthy, so the
    fallback branch was unreachable in production and every one of those
    renderers had been printing a repr — including gen_pdf, which goes to the
    client.

    Every golden test passed throughout, because the fixture holds the STRING
    form. That is the identical shape as the status-vocabulary bug fixed on
    2026-08-05: one fact, two storages, the fixture exercising one and
    production carrying the other.
    """
    if isinstance(value, dict):
        start, end = value.get("start"), value.get("end")
    elif value:
        return str(value)
    else:
        start, end = fallback_start, fallback_end
    if not (start or end):
        return "—"
    if start and end:
        return f"{_short_day(start)} – {_short_day(end)}" if start != end else _short_day(start)
    return _short_day(start or end)


def _short_day(d) -> str:
    """'2026-08-05' → 'Aug 5, 2026'. Anything unparseable passes through as-is
    rather than being dropped: a date we cannot read is still information, and
    hiding it would trade a formatting flaw for a missing one."""
    s = str(d or "").strip()
    try:
        dt = datetime.strptime(s[:10], "%Y-%m-%d")
    except (ValueError, TypeError):
        return s or "—"
    # No %-d / %#d — CLAUDE.md rule #8, Windows portability.
    return dt.strftime("%b %d, %Y").replace(" 0", " ", 1)


def is_win(r: dict) -> bool:
    """True if row is a win. WIN is spelled the same in both storage forms —
    this exists so a renderer can classify a row entirely through these
    helpers, with no `status ==` literal left to pick the wrong vocabulary.
    A bucketing loop that reads is_win/is_pending/is_quoted_and_lost/
    is_not_quoted is obviously total; one that reads WIN/PENDING/LOSS silently
    drops every STRICT row on the floor.
    """
    return (r or {}).get("status") == "WIN"


def is_confirmed_win(r: dict) -> bool:
    """True if row is a win WITH a booking confirmation behind it.

    THE PREDICATE FOR ANYTHING WE TELL THE CUSTOMER. `is_win` is the internal
    business signal — a row flips to WIN on a send-signal, which is the right
    call for our own KPIs and the wrong one for a claim to Hilmar.

    2026-08-10, Michael: "data missing.. you sent lonny we won no shipment
    last week." He is right and it was a promise we had not earned. The client
    weekly counted bookings as `is_win`, and both client templates rendered
    `mdolx_ref or "Confirmation to follow"` under headings reading "Your
    confirmed bookings" and "Shipments confirmed". A row with no MDOLX
    reference has no booking confirmation — printing "Confirmation to follow"
    beside it tells the customer one is coming when nothing says it is.

    The definition is deliberately IDENTICAL to QC-049's, which has flagged
    exactly these rows internally as "UNCONFIRMED — flipped to WIN on a
    send-signal with no MDOLX booking confirmation linked" at ERROR severity
    since 2026-05. We knew. The client-facing renderers just never asked.
    Keep the two definitions in step: if QC-049's changes, change this.
    """
    if not is_win(r):
        return False
    r = r or {}
    return bool(r.get("mdolx_ref") or r.get("mdolx_refs_all"))


# Fields that describe WHICH SAILING a quote is for. They move together or
# not at all — see snap_quote_to_booked_option.
_SAILING_FIELDS = (
    "vessel_voyage", "etd_offered", "eta_offered", "transshipment",
    "origin_free_time", "dest_free_time",
)


def _current_rate_option(r: dict, options: list):
    """Which of the offered options is this row currently sitting on?

    Matched on rate first (the field the headline rule selects) and carrier
    second. Returns None when the row's values match no option — which means
    something other than the rate sheet wrote them, and snapping must not
    disturb them.
    """
    rate = r.get("ol_rate")
    if rate is None:
        return None
    carrier = normalize_carrier(r.get("carrier_quoted") or "") or None
    for opt in options:
        if opt.get("ol_rate") != rate:
            continue
        opt_carrier = normalize_carrier(opt.get("carrier_quoted") or "") or None
        if carrier is None or opt_carrier is None or opt_carrier == carrier:
            return opt
    return None


def snap_quote_to_booked_option(r: dict):
    """Move a multi-option quote onto the option Hilmar actually BOOKED.

    Michael's ruling, 2026-08-21, asked which rate the report should call
    "the rate" on a reply offering several sailings: "the booked one when
    there is a booking."

    So the ladder is evidence-first. The parser's headline rule (lowest rate
    offered on the lane) answers a quote nobody has acted on yet — it is the
    best OL put on the table, and it is the honest answer while the decision
    is still open. But once a booking confirmation exists, guessing is over:
    the option Hilmar booked IS the transaction, and reporting a cheaper one
    it declined would be as wrong as the row-order rule this replaces.

    "There is a booking" means is_confirmed_win — a WIN with an MDOLX
    reference behind it, the same bar every client-facing claim clears
    (QC-049). A send-signal WIN with no confirmation is not a booking, and
    this will not move a row on one.

    THE SAILING FIELDS MOVE WITH THE RATE, OR NOT AT ALL. A quote may never
    pair one sailing's price with another sailing's schedule — that invariant
    is the whole point of reading options as units. But a WIN row's ETD/ETA
    may already have been written from the booking PDF, which is BETTER
    evidence than the rate sheet, so a field is only rewritten when it still
    holds the value of the option the row is leaving. Anything else was
    written by a stronger source and is left alone.

    Returns the booked option's rate when the row moved, else None.
    """
    options = (r or {}).get("rate_options")
    if not isinstance(options, list) or len(options) < 2:
        return None
    if not is_confirmed_win(r):
        return None
    booked = normalize_carrier(r.get("carrier_won") or "")
    if not booked:
        return None
    chosen = None
    for opt in options:
        if normalize_carrier(opt.get("carrier_quoted") or "") == booked:
            chosen = opt
            break
    if chosen is None or chosen.get("ol_rate") is None:
        return None
    if (r.get("ol_rate") == chosen["ol_rate"]
            and normalize_carrier(r.get("carrier_quoted") or "") == booked):
        return None                      # already on the booked option
    leaving = _current_rate_option(r, options)
    for field in _SAILING_FIELDS:
        value = chosen.get(field)
        if value is None:
            continue
        if r.get(field) is None or (leaving is not None
                                    and r.get(field) == leaving.get(field)):
            r[field] = value
    r["ol_rate"] = chosen["ol_rate"]
    r["carrier_quoted"] = booked
    # Why this row is not on the cheapest option. QC-079 reads it, and so
    # does anyone reading the row six months from now.
    r["rate_option_source"] = BOOKED_RATE_OPTION
    return chosen["ol_rate"]


def is_pending(r: dict) -> bool:
    """True if row is still pending. Same spelling in both forms — see is_win."""
    return (r or {}).get("status") == "PENDING"


def is_undated_quote(r: dict) -> bool:
    """A real quote carrying no response time — QC-077's population, and the
    report banner's, in ONE place.

    THE WHOLE POINT IS THAT THERE IS ONE SPELLING. PR #148 shipped two numbers
    off one dataset here; the test written to stop that
    (test_qc077_and_the_note_count_the_same_rows) then RE-TYPED the predicate
    inline instead of calling it, so it went green while the two drifted —
    proven by mutation on 2026-08-19: deleting the exclusion from QC-077
    changed nothing in the suite. Every clause below was, at some point, added
    to one caller and not the other.

    The exclusions, each earned:
      - no RFQ chain (stand_*/ol_*): ingest leaves response_timestamp None
        deliberately, to say "no rate-response email existed", not to report a
        defect.
      - booking-derived evidence: a carrier the transaction report wrote is
        not a quote we failed to date (9 of 22 rows on 2026-08-13).
      - a booking-confirmed WIN: a closed outcome with no send time left to
        chase. Added 2026-08-19 when the sibling heal stopped stamping these
        and un-dated eight Yokohama rows at once.
      - is_real_rate, not `is not None`: this module writes the STRING
        "Not Quoted" into ol_rate as an NQ sentinel.
    """
    r = r or {}
    if not (is_real_rate(r.get("ol_rate")) or r.get("carrier_quoted")):
        return False
    if r.get("response_timestamp"):
        return False
    if has_no_rfq_chain(r):
        return False
    if quote_evidence_is_booking_derived(r):
        return False
    return not is_confirmed_win(r)


#: Marker qc_selfheal writes on a row whose response_timestamp was COPIED from
#: another row's quote rather than read off an email of its own.
BORROWED_RESPONSE_TIME = "sibling_quote"

#: rate_option_source marker: this row's rate is the option Hilmar BOOKED,
#: not the cheapest OL offered. Set only by snap_quote_to_booked_option.
BOOKED_RATE_OPTION = "booked_carrier"


def response_time_is_evidenced(r: dict) -> bool:
    """True when this row's response_timestamp came from an actual email.

    ONE predicate for every renderer that prints a quote time or reasons about
    one, because on 2026-08-19 the alternative was demonstrated: the marker was
    added to qc_selfheal and read in exactly one of the six places that print
    or average a response time. The staff report stopped counting borrowed
    dates as replies while gen_client_email still printed "Quoted at (ET)" off
    the borrowed minute — to LONNY, the external client.

    A borrowed date is real evidence about WHICH quote covered a lane, and the
    win/loss ledger and QC-077 are right to keep reading it. What it is not is
    proof that OL sent something at that minute, so nothing may present it as
    an observed time, a turnaround, or an elapsed age.

    Rows with no response time at all return False: there is nothing evidenced
    to show. Callers that only want to exclude BORROWED times (and still treat
    "no time" separately) should test the marker directly.
    """
    r = r or {}
    if not r.get("response_timestamp"):
        return False
    return r.get("response_time_source") != BORROWED_RESPONSE_TIME


def fmt_pt(dt: datetime | None, with_date: bool = True) -> str:
    if not dt:
        return "—"
    pt = to_pt(dt)
    return pt.strftime(("%b %d " if with_date else "") + "%I:%M %p PT").lstrip("0")


def fmt_et(dt: datetime | None, with_date: bool = True) -> str:
    if not dt:
        return "—"
    et = to_et(dt)
    return et.strftime(("%b %d " if with_date else "") + "%I:%M %p ET").lstrip("0")


# ─────────────────────────────────────────────────────────────────────
# Business-hours turnaround (OL-USA window)
# ─────────────────────────────────────────────────────────────────────

def is_biz_day_et(dt_et: datetime) -> bool:
    return dt_et.weekday() < 5  # Mon-Fri


def is_after_hours_et(dt: datetime | None) -> bool:
    """True if timestamp falls outside OL-USA business hours (8:30 AM – 5:30 PM ET weekdays)."""
    if not dt:
        return False
    et = to_et(dt)
    if not is_biz_day_et(et):
        return True
    t = et.time()
    return t < BIZ_START or t >= BIZ_END


def _biz_hours_between_window(
    start: datetime | None,
    end: datetime | None,
    tz: ZoneInfo,
    win_start: time,
    win_end: time,
) -> float | None:
    """Shared business-window loop behind biz_hours_between (ET) and
    biz_hours_between_pt (PT): counts hours inside the ``win_start``–
    ``win_end`` window on Mon–Fri in ``tz``, DST-safe.

    Added 2026-07-12 (Michael 2026-07-11: "lonny is uswc and we are usec")
    — the client email needs the SAME 8:30–17:30 window counted on the
    Pacific clock. Parameterized instead of copied so the two windows can
    never drift. Mirrored byte-for-byte in the paired core (QC-040);
    tests/test_auditfix_fri_evening_fire_tz.py locks source parity.
    """
    if not start or not end:
        return None
    start_local = start.astimezone(tz)
    end_local = end.astimezone(tz)
    if end_local <= start_local:
        return None

    total = 0.0
    cursor = start_local
    while cursor < end_local:
        day = cursor.date()
        biz_open = datetime.combine(day, win_start, tzinfo=tz)
        biz_close = datetime.combine(day, win_end, tzinfo=tz)

        if cursor.weekday() < 5:  # Mon-Fri, evaluated on the window's own clock
            window_start = max(cursor, biz_open)
            window_end = min(end_local, biz_close)
            if window_end > window_start:
                total += (window_end - window_start).total_seconds() / 3600.0

        # Advance to next day at 00:00 local
        next_day = datetime.combine(day + timedelta(days=1), time(0, 0), tzinfo=tz)
        cursor = next_day

    return round(total, 2) if total > 0 else 0.0


def biz_hours_between(start: datetime | None, end: datetime | None) -> float | None:
    """
    Business-hours delta in ET (8:30–17:30 Mon-Fri), DST-safe.
    Returns None if inputs invalid or end <= start.

    This is the STAFF desk SLA (OL-USA's East-coast window) — the stored
    turnaround_biz_hours metric and gen_email's Time to Quote. Semantics
    unchanged by the 2026-07-12 refactor onto the shared window loop.
    """
    return _biz_hours_between_window(start, end, ET, BIZ_START, BIZ_END)


def biz_hours_between_pt(start: datetime | None, end: datetime | None) -> float | None:
    """
    Business-hours delta in PT (8:30–17:30 America/Los_Angeles, Mon-Fri),
    DST-safe. Returns None if inputs invalid or end <= start.

    CLIENT-facing reply-speed metric (added 2026-07-12; Michael 2026-07-11:
    "lonny is uswc and we are usec"): Lonny's desk is Pacific, so the
    client email narrates HIS experienced wait on HIS clock. Never swap
    this into the staff Time-to-Quote — that stays biz_hours_between (ET).
    """
    return _biz_hours_between_window(start, end, PT, BIZ_START, BIZ_END)


def clock_hours_between(start: datetime | None, end: datetime | None) -> float | None:
    if not start or not end or end <= start:
        return None
    return round((end - start).total_seconds() / 3600.0, 2)


# ─────────────────────────────────────────────────────────────────────
# Container / TEU parsing
# ─────────────────────────────────────────────────────────────────────

_CONTAINER_RX = re.compile(
    # Digit-boundary guarded so a PO/reference number can NEVER be mined as a
    # container spec. Pre-2026-07-26 this was `(\d+)\s*[×x\-]?\s*(\d{2})`,
    # which let the greedy `\d+` eat "PO 4451440" and hand back qty=44514,
    # size=40 -> 89,028 TEU from ONE row, poisoning every volume figure in the
    # day's email, dashboard, PDF and lane rollups. Now:
    #   (?<![\d.,])  - not preceded by a digit/decimal (no mid-number starts)
    #   \d{1,3}      - a real container quantity, never a 7-digit reference
    #   (20|40|45)   - only real ISO sizes, so \d+ cannot over-consume
    #   (?![\d])     - not followed by a digit (no "1234520" tail match)
    #   (?:\s*[×x-]\s*|\s+)  - a SEPARATOR IS REQUIRED between qty and size,
    #                        so a bare 5-digit reference ("quote 10040")
    #                        cannot read as 100 x 40'. Every real spelling has
    #                        one: "2-20'", "1x20'DV", "2×40'RF", "3 x 40 HC",
    #                        "2 40'HC".
    r"(?<![\d.,])(\d{1,3})(?:\s*[×x\-]\s*|\s+)(20|40|45)(?![\d])['\u2019\s]*"
    r"(HC|RF|DV|GP|RE|RH|FR|OT|NOR)?",
    re.IGNORECASE,
)

#: Reverse phrasing OL and Lonny both use — "40'HC x 2", "40' x 3". The
#: forward pattern returns 0 for these, which silently UNDER-counted real
#: bookings (the mirror of the PO over-count). Only consulted when the
#: forward pattern matched nothing, so a normal "2-40'HC" can never be
#: counted twice.
_CONTAINER_REVERSE_RX = re.compile(
    r"(?<![\d.,])(20|40|45)(?![\d])['\u2019\s]*"
    r"(?:HC|RF|DV|GP|RE|RH|FR|OT|NOR)?\s*[×x]\s*(\d{1,3})(?![\d])",
    re.IGNORECASE,
)


def teu_implausible(container_count: int, teu: int) -> str | None:
    """Return a human reason when a per-row (containers, TEU) pair cannot be real.

    The sanity gate behind MAX_ROW_CONTAINERS / MAX_ROW_TEU. Used in two
    places on purpose:
      * `parse_teu` — refuses to RETURN an impossible parse, so a regex
        regression can never inject a poisoned number into a fresh ingest.
      * `qc070_teu_sanity` — errors on any STORED row above the ceiling, so a
        number already sitting in the dataset (written by an older build, a
        carry-forward, or a hand edit) is caught before it reaches a report.

    Returns None when the pair is plausible.
    """
    if container_count > MAX_ROW_CONTAINERS:
        return (f"{container_count} containers on one row exceeds the "
                f"{MAX_ROW_CONTAINERS}-container per-row ceiling")
    if teu > MAX_ROW_TEU:
        return (f"{teu} TEU on one row exceeds the {MAX_ROW_TEU} TEU "
                f"per-row ceiling")
    if container_count < 0 or teu < 0:
        return f"negative volume ({container_count} containers, {teu} TEU)"
    return None


def parse_teu(containers: str | None) -> tuple[int, int]:
    """
    Parse a container string into (container_count, teu_total).
    Handles common patterns: "2×40'RF", "1x20'DV", "2-40' HC Reefers", "3×20'DV + 1×40'HC".
    20' = 1 TEU, 40' = 2 TEU.

    Refuses implausible results: anything above the per-row ceiling
    (`teu_implausible`) returns (0, 0) instead of the poisoned figure. Zero is
    a visibly wrong answer that QC-070 flags and a human can fix; 89,028 TEU
    is an invisibly wrong answer that silently rewrites every rollup in the
    report. The failure mode is chosen deliberately.
    """
    if not containers or not isinstance(containers, str):
        return 0, 0
    total_count = 0
    total_teu = 0
    for match in _CONTAINER_RX.finditer(containers):
        qty = int(match.group(1))
        size = int(match.group(2))
        if size not in (20, 40, 45):  # belt-and-braces; regex already restricts
            continue
        teu_per = 2 if size >= 40 else 1
        total_count += qty
        total_teu += qty * teu_per
    if total_count == 0:
        # Reverse phrasing only when the forward pattern found nothing, so a
        # normal "2-40'HC" is never double counted.
        for match in _CONTAINER_REVERSE_RX.finditer(containers):
            size = int(match.group(1))
            qty = int(match.group(2))
            if size not in (20, 40, 45):
                continue
            total_count += qty
            total_teu += qty * (2 if size >= 40 else 1)
    if teu_implausible(total_count, total_teu):
        return 0, 0
    return total_count, total_teu


# ─────────────────────────────────────────────────────────────────────
# Send detection (regex — not body.startswith)
# ─────────────────────────────────────────────────────────────────────

# Lonny's acceptance phrasings. Until 2026-06-16 this only matched a bare
# "send" (+ a tiny whitelist) at the very start of the first line, so real
# booking instructions were silently dropped and the row never flipped to
# WIN: "Send Carter" (pick the President Carter sailing), "book it",
# "go ahead", "proceed", "please send" all returned False (Michael
# 2026-06-16: "why are you not showing these as wins"). Broadened to the
# vocabulary Lonny actually uses, still anchored to the first line and still
# guarded by NOT_SEND_HINTS so request-like "send me the rates" is excluded.
# A false positive is self-limiting: a send with no MDOLX booking inside ~48
# biz-hours ages to Q&L (SEND_NO_BOOKING) via decide_status.
SEND_RX = re.compile(
    r"""
    ^\s*
    (?:                     # optional courtesy openers (repeatable)
        (?:hi|hey|hello|ok|okay|yes|yep|yup|sure|great|perfect|
           sounds\s+good|sg|thanks|thank\s+you)\W+
    )*
    (?:please\s+)?
    (?:                     # the acceptance verb
        send                #   send / send Carter / send the President Carter
      | book                #   book / book it / book the Carter
      | proceed
      | go\s+ahead
      | confirm(?:\s+(?:the\s+)?booking)?
      | accept(?:ed)?
      | let(?:'?s|\s+us)\s+(?:book|send|go|proceed)   # let's / lets / let us
    )
    \b
    """,
    re.IGNORECASE | re.VERBOSE,
)

# Phrases that LOOK like an acceptance but are actually a request for info
# (or a non-acceptance word). Checked BEFORE SEND_RX in is_lonny_send_reply.
NOT_SEND_HINTS = re.compile(
    r"""
    \b(?:
        send\s+both\s+cutoffs?
      | send\s+(?:me|us|over)\b
      | send\s+(?:me\s+|us\s+|over\s+|the\s+|me\s+the\s+|us\s+the\s+)?
        (?:rate|rates|pricing|price|quote|quotes|cutoff|cutoffs|schedule|
           detail|details|info|breakdown|number|numbers)
      | sending | sender | resend
    )\b
    """,
    re.IGNORECASE | re.VERBOSE,
)


def is_lonny_send_reply(body: str, is_reply: bool = True) -> bool:
    """
    True if Lonny's email body constitutes an acceptance.
    Rules:
      1. Must be a reply (RE:) — a new request with "send" is NOT acceptance.
      2. First line body matches SEND_RX.
      3. Body does NOT match NOT_SEND_HINTS (which catches 'send both cutoffs' etc.).
    """
    if not body or not is_reply:
        return False
    first_chunk = body.strip().split("\n")[0][:200]
    if NOT_SEND_HINTS.search(first_chunk):
        return False
    return bool(SEND_RX.match(first_chunk + "\n"))


# ─────────────────────────────────────────────────────────────────────
# Dedup key — NEVER use conversationId alone
# ─────────────────────────────────────────────────────────────────────

def request_id(conv_id: str | None, request_ts: str | None, destination: str | None) -> str:
    """
    Stable dedup key for a rate request:
      sha1(conversationId | request_timestamp (minute-precision) | destination)
    Prevents dedup collisions when Outlook reuses conversationId across identical subjects.
    """
    parts = [
        (conv_id or "").strip(),
        (request_ts or "")[:16],     # minute precision
        (destination or "").strip().lower(),
    ]
    h = hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:16]
    return f"req_{h}"


# ─────────────────────────────────────────────────────────────────────
# Status state machine (single source of truth)
# ─────────────────────────────────────────────────────────────────────

@dataclass
class StatusDecision:
    status: str                    # WIN / LOSS / PENDING
    quoted: bool
    has_send: bool
    loss_reason: str | None        # NO_RESPONSE / PRICE / ETD_MISS / OTHER / None
    reason_detail: str             # human-readable why
    #: WHEN the row crossed the deadline that produced this status — set only
    #: on an AGING outcome (a LOSS reached because a window expired), None on
    #: every other decision. The caller stamps status_history with this
    #: instead of its own clock, so a reversal carries the day the row died
    #: rather than the day the pipeline noticed. None means "no deadline to
    #: name" (a dateless row, or a status that is not time-derived) and the
    #: caller must fall back to now — never fabricate one.
    stale_at: datetime | None = None


# ─────────────────────────────────────────────────────────────────────
# WHEN a row went stale — not just whether. Added 2026-08-28.
#
# Each predicate below already computed a deadline internally and threw it
# away, returning only a bool. The caller then stamped the resulting
# transition with `now` — the FIRE clock — which is how a derived reversal
# came to carry the day the pipeline noticed instead of the day the row
# actually died. Measured on the production shape (window=previous, 06:30 ET
# fire): a Mon-14:00 send aged out with the WIN->LOSS stamped 08-26 while the
# report covered 08-25, then 08-27 against 08-26, then 08-28 against 08-27 —
# exactly one day ahead of the window, walking forward with it forever,
# because every fire rebuilds status_history and re-stamps at that morning's
# now. It is not late. It never arrives.
#
# The precedent is already in this codebase: ingest.py's prior-build WIN
# restore, 2026-08-11, "DATE THE RESTORE FROM THE PRIOR EVIDENCE, NEVER FROM
# NOW" — same defect, same fix, opposite direction of travel.
#
# EACH FUNCTION REPRODUCES ITS OWN PREDICATE'S ARITHMETIC, DELIBERATELY NOT A
# UNIFIED ONE. Per the CPython datetime docs (checked 2026-08-28, not
# recalled): adding a timedelta to an aware datetime "adjusts the date and
# time while preserving the original tzinfo attribute without performing
# timezone adjustments", so ET-localised `+ timedelta(hours=24)` advances 24
# WALL hours (23 or 25 absolute across a DST change), while subtracting two
# aware datetimes normalises both to UTC and measures ABSOLUTE elapsed time.
# is_business_stale does the first; pending_hilmar_stale and pending_ol_stale
# do the second. They therefore already disagree by an hour across a DST
# boundary. That divergence predates this change and is NOT silently unified
# here — each deadline is lifted from the predicate it belongs to, so the
# deadline and the bool can never disagree about the same row.
# ─────────────────────────────────────────────────────────────────────

def business_stale_deadline(dt: datetime | None, hours: int = 24) -> datetime | None:
    """The instant ``dt`` becomes stale under is_business_stale. None when
    there is no clock to measure from — a row with no date has no deadline,
    and inventing one is the mistake this whole module keeps paying for."""
    if dt is None:
        return None
    dt_et = dt.astimezone(ET)
    if dt_et.weekday() >= 4:                          # Fri=4, Sat=5, Sun=6
        days_to_tue = (1 - dt_et.weekday()) % 7       # Fri=4→4, Sat=5→3, Sun=6→2
        return (dt_et + timedelta(days=days_to_tue)).replace(
            hour=18, minute=0, second=0, microsecond=0)
    return dt_et + timedelta(hours=hours)


def pending_hilmar_deadline(resp_dt: datetime | None, *,
                            request_dt: datetime | None = None) -> datetime | None:
    """The instant a QUOTED PENDING-Hilmar row ages out to Q&L.

    Same anchor rule as pending_hilmar_stale, including the request_dt
    fallback and its keyword-only guard."""
    anchor = resp_dt if resp_dt is not None else request_dt
    if anchor is None:
        return None
    hours = (PENDING_HILMAR_LOSS_HOURS_FRIDAY
             if anchor.astimezone(ET).weekday() == 4
             else PENDING_HILMAR_LOSS_HOURS)
    return anchor + timedelta(hours=hours)


def pending_ol_deadline(request_dt: datetime | None) -> datetime | None:
    """The instant an UNQUOTED row becomes a genuine non-response.

    None for a dateless row. pending_ol_stale returns True in that case (it
    preserves the pre-2026-07-24 immediate-NQ behaviour), so the caller falls
    back to its own clock rather than being handed a fabricated deadline."""
    if request_dt is None:
        return None
    hours = (PENDING_OL_LOSS_HOURS_FRIDAY
             if request_dt.astimezone(ET).weekday() == 4
             else PENDING_OL_LOSS_HOURS)
    return request_dt + timedelta(hours=hours)


def is_business_stale(
    dt: datetime | None,
    now: datetime | None = None,
    hours: int = 24,
) -> bool:
    """True when ``dt`` is older than ``hours`` wall-hours, with the
    weekend carve-out per Michael's stated rule (2026-06-04 restated):
    a Friday/Saturday/Sunday timestamp isn't stale until the following
    **Tuesday 18:00 ET** — gives Lonny Monday + Tuesday to reply.

    Used for BOTH staleness windows in decide_status:
      - send-signal aging (PENDING(AWAITING_MDOLX) → Q&L(SEND_NO_BOOKING))
      - quote-window aging (PENDING(quoted) → Q&L)

    Default `hours=24` matches PENDING_WINDOW_HOURS. Callers that need a
    different window must pass it explicitly.

    Kept byte-for-byte identical to src/hilmar/core.is_business_stale —
    tests/test_core_parity.py fails if they drift.
    """
    deadline = business_stale_deadline(dt, hours)
    if deadline is None:
        return False
    now = now or now_utc()
    return now.astimezone(ET) > deadline


# Backwards-compatibility alias — the original public name. Older code
# and tests call send_signal_stale(send_dt, now); preserved as an alias
# now that is_business_stale is the canonical name.
send_signal_stale = is_business_stale


def pending_hilmar_stale(resp_dt: datetime | None, now: datetime | None = None,
                         *, request_dt: datetime | None = None) -> bool:
    """True when a QUOTED PENDING-Hilmar row has aged out to Quoted & Lost.

    Pure CLOCK hours from the OL quote (response_timestamp):
    >= PENDING_HILMAR_LOSS_HOURS → Q&L, or >= PENDING_HILMAR_LOSS_HOURS_FRIDAY
    when OL quoted on a Friday (ET) so the weekend lands Lonny on Monday.

    Michael said 48h [historic] on 2026-07-14 and then 24h on 2026-07-26 (0c73c4b,
    "supersedes"). This docstring quoted the FIRST instruction for eleven days
    after the second one shipped. Naming the constants instead of spelling a
    number is what stops that recurring. Distinct from the SEND-
    signal aging (is_business_stale), which is unchanged.

    `request_dt` (2026-08-13) is a FALLBACK anchor, used only when `resp_dt`
    is None. Michael, verbatim: "if you have the quotes and you do not see a
    booking for the quote, then it's a loss  that's it". A row that carries a
    rate or a carrier but no parseable response_timestamp still has a clock we
    trust — Lonny's request — and decide_status already ages off it inline.
    Without this parameter every DETECTOR had to skip such rows (they all did:
    QC-007, gen_improvements_report, auto_chase_pending), so a row stuck in
    PENDING raised nothing and got no chase. The fallback exists so those
    callers stop re-deriving the rule, or skipping it.

    It is KEYWORD-ONLY on purpose. Positionally it would slide into `now`,
    which is the same class of mistake that once put a hardcoded 24h literal
    in QC-007 while decide_status ran 48h+Friday [historic].

    Behaviour for existing 2-arg callers is unchanged: with request_dt
    defaulting to None the anchor is exactly resp_dt, including the None case.

    Kept byte-for-byte identical to src/hilmar/core.pending_hilmar_stale —
    tests/test_core_parity.py fails if they drift.
    """
    deadline = pending_hilmar_deadline(resp_dt, request_dt=request_dt)
    if deadline is None:
        return False
    now = now or now_utc()
    return now >= deadline


def pending_ol_stale(request_dt, now=None) -> bool:
    """True when an UNQUOTED row has waited long enough on OL that it counts
    as a genuine non-response (NQ) rather than an open request (PENDING_OL).

    Anchored on Lonny's REQUEST time (there is no response yet, by
    definition). Pure CLOCK hours, mirroring pending_hilmar_stale:
    >= PENDING_OL_LOSS_HOURS, or >= PENDING_OL_LOSS_HOURS_FRIDAY when Lonny
    asked on a Friday (ET) so the weekend lands OL on Monday. Named rather
    than spelled: this docstring said "48h" [historic] while the constant was 24 from
    2026-07-26 to 2026-08-06.

    request_dt None → STALE (True). We cannot measure a window without a
    date, so we preserve the pre-2026-07-24 behavior (immediate NQ) rather
    than inventing a row that stays PENDING forever. This keeps the fix
    strictly ADDITIVE: the grace window applies only to rows we can actually
    date. Callers pass request_timestamp OR request_date, so in production
    every real row is dateable and does get the window.

    Kept byte-for-byte identical across scripts/core.py and
    src/hilmar/core.py — tests/test_core_parity.py fails if they drift.
    """
    deadline = pending_ol_deadline(request_dt)
    if deadline is None:
        return True
    now = now or now_utc()
    return now >= deadline


def pending_ol_overdue(request_dt, now=None) -> bool:
    """True when OL has BLOWN its response SLA on a still-open RFQ.

    Business hours (ET 8:30-17:30 Mon-Fri) via biz_hours_between — the exact
    measure the report shows as "Time to Quote", so an overdue flag and the
    displayed hours can never contradict each other. An RFQ that lands at
    6:42 PM ET does not start burning SLA until 8:30 the next business
    morning, and the weekend never counts against OL.

    NOT a loss signal — see PENDING_OL_SLA_BIZ_HOURS. The row remains
    PENDING_OL (open, chase OL); this only marks that the chase is now owed.

    Kept byte-for-byte identical across scripts/core.py and
    src/hilmar/core.py — tests/test_core_parity.py fails if they drift.
    """
    if request_dt is None:
        return False
    now = now or now_utc()
    elapsed = biz_hours_between(request_dt, now)
    if elapsed is None:
        return False
    return elapsed >= PENDING_OL_SLA_BIZ_HOURS


def decide_status(
    *,
    has_send: bool,
    mdolx_ref: str | None,
    response_timestamp: str | None,
    quoted: bool,
    etd_fit_days: int | None,
    request_timestamp: str | None = None,
    send_signal_events: list | None = None,
    mdolx_refs_all: list | None = None,
    now: datetime | None = None,
    ol_rate: float | str | None = None,
    lane: str | None = None,
    lane_winning_median: dict[str, float] | None = None,
) -> StatusDecision:
    """
    Pure classification. Inputs are the minimum facts needed to make a call.
    Called by the processor on ingestion AND by QC to re-age pending entries.

    WIN requires BOTH a Lonny "send" handoff AND an OL-side MDOLX booking
    confirmation (Reading B, Michael 2026-04-27 — ported into production
    2026-05-30 after the old ``has_send OR mdolx`` rule produced
    permanent phantom WINs from send-signals that never booked). A send
    with no MDOLX stages as PENDING(AWAITING_MDOLX) and auto-promotes to
    WIN when the booking lands; if it goes stale (see send_signal_stale —
    real wins confirm within PENDING_WINDOW_HOURS biz) it demotes to
    LOSS(SEND_NO_BOOKING),
    which the audit displays as Q&L.

    PRICE determination (Q&L sub-classification, 2026-06-02 rewrite):
      The pre-rewrite code labeled every Q&L row with etd_fit_days<5 as
      "PRICE" — a catch-all that produced 94% PRICE-driven readouts even
      when winning median and losing median on a lane were identical.
      Now PRICE requires a concrete rate gap:
        ol_rate > lane_winning_median[lane] * PRICE_GAP_THRESHOLD_MULT
      When the rate is competitive (≤ threshold) OR we lack signal
      (no lane median, no ol_rate, fewer than PRICE_GAP_MIN_LANE_WINS
      historical wins on the lane) → UNDIFFERENTIATED. That's the honest
      "we lost, the data doesn't tell us why" bucket — it surfaces as the
      operator's signal to dig into the email thread rather than blaming
      rate by default.

    ``lane_winning_median`` is computed once by the caller (use
    ``compute_lane_winning_medians(requests)``) and passed as a lookup
    dict so this function stays per-row pure. When None or missing the
    lane key, PRICE never fires — UNDIFFERENTIATED is the safe fallback.
    """
    now = now or now_utc()
    # A BOOKING REF IS A BOOKING REF, WHICHEVER FIELD HOLDS IT.
    #
    # This read the primary scalar only, while src/hilmar/core.py:1381 has
    # always read the union — so the two trees returned different verdicts for
    # the same row and no parity case covered it, because production's
    # signature could not even accept the argument (TypeError, measured).
    #
    #   has_send=True, mdolx_ref=None, mdolx_refs_all=['261031']
    #     production  LOSS / SEND_NO_BOOKING  "booking never confirmed"
    #     library     WIN                     "MDOLX booking confirmed"
    #
    # Production was the wrong one, on three counts:
    #   * Reading B (Michael 2026-04-27) requires "an OL-side MDOLX booking
    #     confirmation" — not one in a particular field. A ref only reaches
    #     mdolx_refs_all by parsing a real OL booking email.
    #   * booking_count (the counting rule) and is_confirmed_win (what the
    #     client report renders) BOTH already read the union. decide_status
    #     was the outlier — and it WRITES the status those two then read.
    #   * The row was calling itself a loss while holding the booking.
    #
    # And it erased its own evidence: booking_count gates on the stored
    # status, so once qc_selfheal wrote LOSS the count fell 1 -> 0 and the two
    # agreed again. Nothing was left to detect.
    #
    # NOT a return to the pre-Reading-B "has_send OR mdolx" rule that produced
    # phantom WINs for a month — BOTH signals are still required, and an empty
    # mdolx_refs_all is still no booking. test_old_or_rule_is_gone_in_both
    # holds either way.
    has_mdolx = (bool(mdolx_ref and str(mdolx_ref).strip())
                 or bool(mdolx_refs_all))

    # WIN — strict: requires BOTH signals.
    if has_send and has_mdolx:
        return StatusDecision("WIN", True, True, None,
                              "Lonny replied Send AND MDOLX booking confirmed")

    # MDOLX present but no send — anomaly. Hold PENDING for ops review
    # rather than auto-winning (mirrors src/hilmar Reading-B).
    if has_mdolx and not has_send:
        # has_send=False — the branch is literally `has_mdolx and not has_send`,
        # so claiming True here asserted the opposite of the condition that got
        # us here. NOT cosmetic: qc_selfheal writes the decision back onto the
        # row, so pass 2 re-read has_send=True alongside the MDOLX and took the
        # WIN branch. The anomaly that was supposed to be HELD for ops review
        # silently promoted itself to a WIN one fire later, with no send signal
        # and nobody looking. Found 2026-07-26 while fixing the SEND_NO_BOOKING
        # evidence bug — same class, and it lived in scripts/ only (src/hilmar
        # already returned False), i.e. the exact "green in CI, wrong on the
        # box" split tests/test_core_parity.py exists to catch.
        return StatusDecision("PENDING", True, False, "MDOLX_NO_SEND",
                              "MDOLX booking present but no Lonny Send — anomaly, review")

    # Send received, MDOLX not yet — booking in flight. Demote to
    # Q&L(SEND_NO_BOOKING) once the send goes stale; otherwise hold
    # PENDING(AWAITING_MDOLX) so a later run can promote to WIN when
    # MDOLX lands.
    if has_send and not has_mdolx:
        send_at = parse_iso(response_timestamp)
        for ev in (send_signal_events or []):
            ts = parse_iso(ev.get("at") if isinstance(ev, dict) else None)
            if ts and (send_at is None or ts > send_at):
                send_at = ts
        # THE ROW THAT NEVER AGED (2026-08-13). Michael, verbatim: "if you
        # have the quotes and you do not see a booking for the quote, then
        # it's a loss  that's it".
        #
        # send_at was derived ONLY from response_timestamp and
        # send_signal_events, and is_business_stale returns False on None. A
        # row holding a rate but no parseable response timestamp — exactly
        # what patch_carriers produces when it recovers a rate from a sibling
        # thread or a booking PDF — therefore had NO clock on this branch and
        # returned PENDING/AWAITING_MDOLX forever. Measured before the fix:
        # identical PENDING at +1d, +30d, +365d and +3650d. Quote evidence,
        # no booking, never a loss; pending_substate keys off `quoted`, so it
        # rendered under PENDING HILMAR and fed the report's "cannot be dated"
        # banner.
        #
        # Fall back to Lonny's request, the one clock we always have and never
        # invent — the same anchor the quote-aging branch below already uses
        # for the same missing evidence. Deliberately NOT a change to
        # is_business_stale: that predicate must keep returning False on None
        # so a row with NO clock at all stays PENDING and surfaces as a DATA
        # defect via QC-007, rather than being aged on a timestamp nobody can
        # evidence.
        if send_at is None:
            send_at = parse_iso(request_timestamp)
        # The deadline this row crossed, kept so the caller can stamp the
        # transition with it. send_signal_stale IS is_business_stale, so the
        # deadline comes from that predicate's own arithmetic.
        _stale_at = business_stale_deadline(send_at, PENDING_WINDOW_HOURS)
        if send_signal_stale(send_at, now):
            # has_send stays TRUE. It is an EVIDENCE field — "did Lonny
            # accept?" — not a state field, and on this branch the answer is
            # yes by definition: SEND_NO_BOOKING means the send happened and
            # OL never confirmed. Returning False here erased the only record
            # that Lonny accepted, and because qc_selfheal writes the decision
            # back onto the row (`r["has_send"] = decision.has_send`), the NEXT
            # pass re-read has_send=False, fell through to the quote-aging
            # branch and relabelled the row UNDIFFERENTIATED — "we lost, cause
            # unknown". Unrecoverable: the OL-dropped-the-ball signal was gone
            # from the loss mix, the carrier scorecards (_OL_SILENT) and the
            # improvement report. Proved on live-shaped data 2026-07-26:
            # pass1 SEND_NO_BOOKING/has_send=False -> pass2 UNDIFFERENTIATED.
            return StatusDecision(
                "LOSS", True, True, "SEND_NO_BOOKING",
                f"Send received but no MDOLX within the "
                f"{PENDING_WINDOW_HOURS}h (biz-hours) cutoff — booking never "
                f"confirmed (real wins confirm same/next business day)",
                stale_at=_stale_at)
        return StatusDecision("PENDING", True, True, "AWAITING_MDOLX",
                              "Lonny replied Send — awaiting MDOLX booking confirmation")

    # OL never QUOTED → NQ. Check the quote FIRST: a row that DID carry a rate
    # (quoted=True) can NEVER be NO_RESPONSE. The old `not quoted or not
    # response_timestamp` gate bucketed a real quote with a missing
    # response_timestamp as "OL never responded" — inflating NQ and rendering
    # Time-to-Quote as "—". Now: no response at all → NO_RESPONSE; OL responded
    # but no rate parsed → RESPONSE_NO_RATE. Both are quoted=False so
    # display_status shows them as NQ. A quoted row falls through to the aging
    # block below, where parse_iso(None)→None hits the "assumed aged" Q&L guard.
    if not quoted:
        if not response_timestamp:
            # OL has not answered YET. Michael 2026-07-24 ("your quality
            # control system is not functioning"): a request Lonny sent this
            # morning is OPEN BUSINESS TO CHASE, not a loss. Before this,
            # every unquoted row was classified LOSS/NO_RESPONSE the instant
            # it was ingested — with zero grace — which (a) buried live RFQs
            # as "lost" in the STORED data and (b) made PENDING_OL
            # structurally unreachable, so "PENDING OL (0) — awaiting OL
            # quote" was permanently empty (proved: 0 of 96 input
            # combinations could produce it). Hold PENDING (quoted=False →
            # pending_substate PENDING_OL) until the window expires; only
            # then is it a genuine non-response.
            req_dt = parse_iso(request_timestamp)
            if not pending_ol_stale(req_dt, now):
                _w = (PENDING_OL_LOSS_HOURS_FRIDAY
                      if req_dt and req_dt.astimezone(ET).weekday() == 4
                      else PENDING_OL_LOSS_HOURS)
                return StatusDecision(
                    "PENDING", False, False, None,
                    f"Awaiting OL quote — within the {_w}h response window")
            return StatusDecision("LOSS", False, False, "NO_RESPONSE",
                                  "OL-USA never responded with a quote",
                                  stale_at=pending_ol_deadline(req_dt))
        return StatusDecision("LOSS", False, False, "RESPONSE_NO_RATE", "OL responded but no rate was extracted")

    # Quoted — check aging
    resp_dt = parse_iso(response_timestamp)
    if not resp_dt:
        # NEVER AGE ON ABSENCE. "assumed aged" used to return LOSS/OTHER here
        # the instant a quoted row arrived without a parseable response
        # timestamp — which is exactly what patch_carriers produces when it
        # recovers a rate from a sibling thread or a booking PDF that carried
        # no usable timestamp. An RFQ Lonny sent THIS MORNING, quoted an hour
        # ago, was reported to staff AND to the client as a LOSS: counted
        # against win rate, dropped from auto_chase_pending, and absent from
        # every pending bucket, so PENDING OL read 0 and nobody followed up on
        # live business the system had already buried. Proved 2026-07-27: a
        # request 2h old returned LOSS/OTHER for both a missing and a
        # malformed timestamp.
        #
        # A missing timestamp is missing EVIDENCE, not elapsed time. Fall back
        # to the clock we do trust — Lonny's request — and hold PENDING until
        # THAT window expires. Only then is the row genuinely old enough to be
        # a loss, and it is tagged NO_RESPONSE_TS so the cause is legible
        # rather than hidden inside "OTHER".
        req_dt = parse_iso(request_timestamp)
        if req_dt and not pending_hilmar_stale(req_dt, now):
            _age = (now - req_dt).total_seconds() / 3600.0
            return StatusDecision(
                "PENDING", True, False, "NO_RESPONSE_TS",
                f"Quoted, but the OL response carried no usable timestamp — "
                f"aging off Lonny's request instead ({_age:.1f}h ago, still "
                f"inside the decision window)")
        return StatusDecision(
            "LOSS", True, False, "NO_RESPONSE_TS",
            "Quoted but response_timestamp unparseable, and the request itself "
            "is past the decision window",
            stale_at=pending_hilmar_deadline(req_dt))

    hours_since = (now - resp_dt).total_seconds() / 3600.0
    # PENDING-Hilmar quote-decision window. The numbers live in
    # PENDING_HILMAR_LOSS_HOURS / _FRIDAY and are interpolated below rather
    # than restated here — this comment named the SUPERSEDED 2026-07-14
    # figure until 2026-08-21, eleven days after Michael set the current one
    # in 0c73c4b, and it sat directly above the branch a reader lands on when
    # chasing exactly this behaviour. On 2026-08-21 it cost a session: three
    # Aug-20 quotes aged out correctly and the stale prose made the CONSTANT
    # look like the bug, one edit away from reverting an operator decision he
    # had already made. Measured against the constant, never prose.
    # SEND-signal aging above still uses is_business_stale.
    if not pending_hilmar_stale(resp_dt, now):
        _win = PENDING_HILMAR_LOSS_HOURS_FRIDAY if resp_dt.astimezone(ET).weekday() == 4 else PENDING_HILMAR_LOSS_HOURS
        return StatusDecision("PENDING", True, False, None,
                              f"Quoted {hours_since:.1f}h ago — Lonny still within "
                              f"the {_win}h decision window")

    # Quoted & Lost. Try to tag a reason.
    detail = f"Quoted {hours_since:.1f}h ago, no Send — Quoted & Lost"

    # ONE DEADLINE FOR THE WHOLE Q&L TAIL. ETD_MISS, PRICE, UNDIFFERENTIATED
    # and QUOTED_NOT_BOOKED are not four events — they are one aging event
    # (the quote-decision window expiring) wearing four labels, so they are
    # all stamped with the moment that window closed. Bound here, once, above
    # every return below it: a branch added later that forgets to pass it
    # fails tests/test_reversals_are_dated_when_they_happened.py.
    _ql_stale_at = pending_hilmar_deadline(resp_dt)

    # ETD-miss wins first — a missed ETD is a concrete signal regardless
    # of price competitiveness.
    if etd_fit_days is not None and etd_fit_days >= ETD_MISS_DAYS:
        reason = "ETD_MISS"
        detail += f" (ETD missed Lonny's ask by {etd_fit_days}d)"
        return StatusDecision("LOSS", True, False, reason, detail,
                              stale_at=_ql_stale_at)

    # Otherwise, did OL's rate actually clear above the winning lane
    # median? Only call PRICE when we have a real rate gap.
    rate_val = parse_rate(ol_rate) if isinstance(ol_rate, str) else (
        float(ol_rate) if isinstance(ol_rate, (int, float)) else None
    )
    lane_med = None
    if lane_winning_median and lane:
        # RAW FIRST, CANONICAL AS A FALLBACK. compute_lane_winning_medians
        # emits under every spelling it saw, so a raw lookup hits and no
        # caller had to change. The fallback covers a dict built some
        # other way — a lookup that silently misses reads as "no lane
        # history" and drops every Q&L on that lane from PRICE to
        # UNDIFFERENTIATED, the same wrong answer by a new route.
        lane_med = (lane_winning_median.get(lane)
                    or lane_winning_median.get(canonical_lane_id(lane)))
    if rate_val is not None and lane_med and lane_med > 0:
        if rate_val > lane_med * PRICE_GAP_THRESHOLD_MULT:
            reason = "PRICE"
            gap_pct = (rate_val - lane_med) / lane_med * 100.0
            detail += (
                f" (rate ${rate_val:.0f} is {gap_pct:.0f}% above lane "
                f"winning median ${lane_med:.0f} → rate-driven)"
            )
            return StatusDecision("LOSS", True, False, reason, detail,
                              stale_at=_ql_stale_at)
        # Rate was at/below winning median — not a price story.
        reason = "UNDIFFERENTIATED"
        detail += (
            f" (rate ${rate_val:.0f} ≤ lane winning median "
            f"${lane_med:.0f} — competitive on price, root cause unclear)"
        )
        return StatusDecision("LOSS", True, False, reason, detail,
                              stale_at=_ql_stale_at)

    # No signal to determine PRICE — be honest about the gap.
    reason = "UNDIFFERENTIATED"
    if rate_val is None:
        detail += " (no ol_rate to compare against lane winning median)"
    elif lane_med is None:
        detail += " (no lane winning history to benchmark against)"
    return StatusDecision("LOSS", True, False, reason, detail,
                          stale_at=_ql_stale_at)


def record_transition(request: dict, new_status: str, reason: str, at: datetime | None = None) -> None:
    """Append to status_history if status actually changed. Mutates request in place."""
    at = at or now_utc()
    old = request.get("status")
    if old == new_status:
        return
    history = request.setdefault("status_history", [])
    history.append({
        "at": at.isoformat(),
        "from": old,
        "to": new_status,
        "reason": reason,
    })
    request["status"] = new_status


# ─────────────────────────────────────────────────────────────────────
# ETD fit — how far off was OL's offered date from Lonny's ask?
# ─────────────────────────────────────────────────────────────────────

_DATE_PATTERNS = [
    "%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%d-%b-%y", "%d-%b-%Y", "%b %d", "%b %d %Y", "%B %d",
]


def _parse_loose_date(s: str | None, fallback_year: int | None = None) -> date | None:
    if not s or not isinstance(s, str):
        return None
    s = s.strip().strip('"\'')
    # Strip prefixes like "ETA ", "ETD "
    s = re.sub(r"(?i)^(eta|etd)\s+", "", s)
    # Strip trailing notes like "not earlier"
    s = re.sub(r"(?i)\s+(not\s+earlier.*|or\s+later.*)$", "", s)
    for fmt in _DATE_PATTERNS:
        try:
            parsed = datetime.strptime(s, fmt)
            if parsed.year == 1900 and fallback_year:  # %b %d defaults to 1900
                parsed = parsed.replace(year=fallback_year)
            return parsed.date()
        except Exception:
            continue
    # Try "5/13" → use fallback year
    m = re.match(r"^(\d{1,2})/(\d{1,2})$", s)
    if m and fallback_year:
        try:
            return date(fallback_year, int(m.group(1)), int(m.group(2)))
        except Exception:
            return None
    return None


def offered_date(value, fallback_year: int | None = None) -> date | None:
    """THE reader for an ETD/ETA that OL *offered*. Route every one through here.

    An offered date is a free-text table cell copied out of a carrier's email —
    NOT a timestamp. OL writes both forms, in the same dataset, on the same day:

        stand_260769   etd=22-Apr-26   eta=26-May-26      <- d-Mmm-yy
        req_5d2685f3…  etd=1-Jul-26    eta=2026-07-25     <- ISO

    2026-08-10: three different parsers were reading this one field, and the
    CLIENT-FACING one was the strict one —

        share_intel.py:255        _parse_loose_date(...)   internal, loose
        gen_client_email.py:326   _iso_date(...)           Lonny's email, STRICT
        gen_client_weekly.py:184  _iso_date(...)           Lonny's weekly, STRICT

    `_iso_date` is `strptime(s[:10], "%Y-%m-%d")`. So a `26-May-26` ETA is
    truthy for QC-027 (the field IS populated, it counts toward 93.3%) and
    invisible to "Currently in transit", which drops any row whose ETA will not
    parse. The internal intel feed saw those shipments; the client's report did
    not. One fact, three readers, and the one that mattered held the wrong one.

    A bare "Jul 25" with no year still returns None rather than guessing the
    year — the cell genuinely does not carry one, and inventing it would put a
    fabricated sail date in front of the customer.
    """
    d = _parse_loose_date(value, fallback_year=fallback_year)
    if d:
        # "%b %d" ("Jul 25") carries no year, and strptime defaults it to 1900.
        # Without a fallback_year that is a FABRICATED date, not a parse — and
        # a 1900 sail date sorts to the front of the client's transit table.
        # None is the honest answer; the cell really does not say which year.
        if d.year == 1900 and fallback_year is None:
            return None
        return d
    # Non-zero-padded M/D/YY(YY). share_intel carried this fallback and the
    # client renderers did not — part of how the two came to disagree.
    if isinstance(value, str):
        m = re.match(r"\s*(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})\s*$", value)
        if m:
            yr = int(m.group(3))
            if yr < 100:
                yr += 2000
            try:
                return date(yr, int(m.group(1)), int(m.group(2)))
            except ValueError:
                return None
    return None


def etd_fit_days(lonny_requested: str | None, ol_offered: str | None, fallback_year: int = 2026) -> int | None:
    """
    Positive = OL offered a later date than Lonny asked for (bad).
    Negative = OL offered earlier (neutral/good).
    None if either side is unparseable.
    """
    a = _parse_loose_date(lonny_requested, fallback_year)
    b = _parse_loose_date(ol_offered, fallback_year)
    if not a or not b:
        return None
    return (b - a).days


#: A quote is ETD_MISS when OL's offer misses Lonny's ask by this many days.
#: Named once here because core.decide_status, the QC checks and the tests all
#: have to agree on it — it was a bare `>= 5` in decide_status until 2026-08-21.
#: Last-resort year for a date carrying none and no context to infer
#: one from. Only reachable when the ASK itself is year-less, which our
#: own parsers do not produce — kept so the helpers never crash.
DEFAULT_FALLBACK_YEAR = 2026

ETD_MISS_DAYS = 5


def requested_fit_days(row: dict) -> tuple[int | None, str | None]:
    """Days by which OL's offer misses Lonny's ask — LIKE FOR LIKE ONLY.

    Returns ``(days, basis)`` where basis is "arrival" or "departure", or
    ``(None, None)`` when the two legs cannot honestly be compared. Positive
    means OL offered a LATER date than Lonny asked for.

    2026-08-21, Michael, asked whether a cutoff ask may be measured against an
    arrival offer: "compare like with like only" — and, on what to do when
    they don't match, record no miss rather than a fabricated one.

    THE DEFECT THIS REPLACES. Both writers (ingest and the qc_selfheal
    backfill) paired ``eta_requested or requested_dates or cutoff_requested``
    against ``eta_offered or etd_offered`` — an arrival ask could be
    differenced against a departure offer and, far worse, a DEPARTURE ask
    against an ARRIVAL offer. Because _ETA_REQ_ANCHORS also matched "cutoff",
    "ship by" and "need to sail", a cutoff RFQ produced a "miss" that was
    really the ocean transit time. Measured on real Aug-2026 asks:

        "Cutoff 8/28"          -> ask 2026-08-28 vs OL ETA 30-Sep-26 = 33d
        "Need to sail by 8/25" -> ask 2026-08-25 vs OL ETA 30-Sep-26 = 36d

    Both cleared the 5-day ETD_MISS gate, so every cutoff-style RFQ that lost
    was stamped "missed the requested ETD" — a reason that then fed
    loss analytics, avg_etd_fit_days and the carrier scoreboard. A month of
    ocean freight to Asia is not a missed ETD.

    THE ARRIVAL LEG IS TRIED FIRST because it is the ask Lonny states most
    often and the one OL's grid answers most completely. If that PAIR is
    incomplete the departure PAIR is tried — each leg is only ever compared
    against itself, so falling through cannot cross them, and a row carrying
    both an arrival ask and a departure ask/offer is measured on the leg it
    can actually answer. What never happens is the mix: an arrival ask is
    never differenced against a departure offer, or vice versa.

    ``requested_dates`` is deliberately NOT consulted. It is free text
    ("Cutoff next week or the following") with no stated leg, and guessing
    which one it means is exactly what this function exists to stop.
    """
    def _leg(ask, offer, basis):
        # THE OFFER GOES THROUGH offered_date, the module's declared single
        # reader for a date OL wrote in a table cell. etd_fit_days' own loose
        # parser is narrower: it returns None for "9-30-26", a form OL really
        # sends, so the whole comparison silently vanished instead of
        # reporting a miss. The ASK is our own parsers' output and is already
        # ISO, so the loose reader is right for that side.
        #
        # The year context comes from the ASK, not from a hardcoded 2026.
        # A bare "Sep 30" cell must resolve in the same year the ask is
        # talking about, or the difference is a year wide.
        a = _parse_loose_date(ask, DEFAULT_FALLBACK_YEAR)
        if not a:
            return None
        # offered_date lives only in scripts/core.py — it is one of the
        # symbols in the declared split between the two trees. Resolved at
        # call time so the production tree gets the full OL-cell reader and
        # the library tree degrades to the loose parser instead of raising.
        _read_offer = globals().get("offered_date") or _parse_loose_date
        b = _read_offer(offer, fallback_year=a.year)
        if not b:
            return None
        return (b - a).days, basis

    got = _leg(row.get("eta_requested"), row.get("eta_offered"), "arrival")
    if got:
        return got
    got = _leg(row.get("etd_requested") or row.get("cutoff_requested"),
               row.get("etd_offered"), "departure")
    if got:
        return got
    return None, None


# ─────────────────────────────────────────────────────────────────────
# Summary / lane / carrier aggregation
# ─────────────────────────────────────────────────────────────────────

def _sum(iterable: Iterable[int]) -> int:
    return sum(x or 0 for x in iterable)


def aggregate_summary(requests: list[dict]) -> dict:
    wins = [r for r in requests if r.get("status") == "WIN"]
    losses = [r for r in requests if r.get("status") == "LOSS"]
    ql = [r for r in losses if r.get("quoted")]
    # NQ RESET (Michael 2026-08-14, see NQ_VALID_FROM): a request from before
    # the floor was answered — we just could not see the answer — so it is not
    # counted as Not Quoted. Excluded and COUNTED, so the report states how
    # many rather than silently shrinking. Wins/Q&L/win rate are untouched:
    # NQ has never been in the win-rate denominator.
    _nq_all = [r for r in losses if not r.get("quoted")]
    nq = [r for r in _nq_all if nq_is_valid(
        r.get("request_date") or r.get("date") or r.get("request_timestamp"))]
    nq_excluded = len(_nq_all) - len(nq)
    pending = [r for r in requests if r.get("status") == "PENDING"]

    # win_rate per CLAUDE.md §6 = Wins / (Wins + Q&L). NQ is "no contest
    # happened" (NO_RESPONSE / RESPONSE_NO_RATE) and must be EXCLUDED from
    # the denominator — otherwise a busy day with OL silent on many quotes
    # silently suppresses the win-rate number on the daily client email.
    # Bug discovered 2026-06-02 audit (track 03 Critical finding C-1).
    # NQ rate is reported as its own separate metric ("not_quoted").
    # SHIPMENTS, NOT ROWS (2026-08-26). Every count below goes through
    # shipment_count, so a row carrying three MDOLX refs is three wins AND
    # three requests. Counting the numerator by shipment and the denominator
    # by row is what produces a win rate above 100%; see shipment_count.
    n_wins = _sum(shipment_count(r) for r in wins)
    n_ql, n_nq, n_pending = len(ql), len(nq), len(pending)
    win_rate_denom = n_wins + n_ql
    # total counts EVERY row by shipment — not the sum of the four buckets.
    # A floored NQ row (NQ_VALID_FROM) is excluded from `not_quoted` but
    # stays in total_entries and teu_requested, which is what QC-075's
    # trade-region reconciliation balances against; deriving total from the
    # buckets would silently drop it and make that check fire on healthy
    # data. Same reason a status outside WIN/LOSS/PENDING still counts.
    total = _sum(shipment_count(r) for r in requests)
    total_quoted = n_wins + n_ql + n_pending

    # TIMING RESET (Michael 2026-08-13, see TIMING_VALID_FROM). A sample from
    # before the floor measures a clock that was started and never stopped,
    # because OL's replies were not reaching this mailbox. Excluded from the
    # aggregate and COUNTED, so the report can say how many rather than
    # silently shrinking the sample.
    # response_time_is_evidenced FIRST, before the clock-reset floor. A
    # borrowed date is a minute OL never sent, so it is not a sample at all —
    # and filtering it here rather than after keeps it out of ta_excluded,
    # which gen_pdf labels "earlier sample(s) excluded" under the clock-reset
    # note. A fabricated row is not an excluded historical sample; counting it
    # as one would explain it with the wrong reason.
    _timed = [r for r in requests if response_time_is_evidenced(r)]
    _measurable = [r for r in _timed if (r.get("turnaround_biz_hours") or 0) > 0]
    ta_entries = [r for r in _measurable
                  if timing_is_valid(r.get("request_timestamp"))]
    ta_excluded = len(_measurable) - len(ta_entries)
    avg_biz = (round(sum(r["turnaround_biz_hours"] for r in ta_entries)
                     / len(ta_entries), 2) if ta_entries else None)

    _clock = [r for r in _timed if (r.get("turnaround_hours") or 0) > 0
              and timing_is_valid(r.get("request_timestamp"))]
    avg_clock = (round(sum(r["turnaround_hours"] for r in _clock)
                       / len(_clock), 2) if _clock else None)

    return {
        "total_entries": total,
        "wins": n_wins,
        "quoted_lost": n_ql,
        "not_quoted": n_nq,
        "not_quoted_excluded": nq_excluded,
        "nq_valid_from": NQ_VALID_FROM,
        "pending_hilmar": n_pending,
        "win_rate": round(n_wins / win_rate_denom * 100, 1) if win_rate_denom else 0.0,
        "quote_rate": round(total_quoted / total * 100, 1) if total else 0.0,
        "teu_requested": _sum(r.get("teu_requested", 0) for r in requests),
        "teu_won": _sum(r.get("teu_won", 0) or r.get("teu_requested", 0) for r in wins),
        "teu_quoted_lost": _sum(r.get("teu_requested", 0) for r in ql),
        "teu_not_quoted": _sum(r.get("teu_requested", 0) for r in nq),
        "teu_pending": _sum(r.get("teu_requested", 0) for r in pending),
        "turnaround_entries": len(ta_entries),
        "turnaround_avg_biz_hours": avg_biz,
        "turnaround_avg_clock_hours": avg_clock,
        # None rather than 0.0 when nothing is measurable yet: "0h" reads as
        # an instant reply, which is a lie in the flattering direction.
        "turnaround_valid_from": TIMING_VALID_FROM,
        "turnaround_excluded": ta_excluded,
    }


ARROW = "→"


def canonical_lane_id(lane, origin=None, destination=None):
    """THE bucket key for a lane. Not a display value — never render this.

    WHY THIS EXISTS. `aggregate_lanes` and `compute_lane_winning_medians` both
    keyed on the raw "Oakland → X" DISPLAY string. The note above PORT_LOCODES
    already named that as the reason the Yokohama split starved its own
    winning median in 2026-08; #230 fixed the JPYOK spelling at parse time and
    left the keying alone. So the cause is still live, and it is not
    hypothetical — six operator corrections pin destination='KOBE' while the
    parser corpus spells it 'Kobe'. Measured 2026-08-31:

        same_port('KOBE', 'Kobe')                 -> True
        canonical_port_key('KOBE') == ...('Kobe') -> True   ('kobe')
        aggregate_lanes(...)                      -> ['Oakland -> KOBE',
                                                      'Oakland -> Kobe']

    The repo knew they were one port at every level except the one that
    counts. Two buckets means half the wins each, and PRICE_GAP_MIN_LANE_WINS
    is 3 — so a lane with four wins split 2/2 produces NO median, and every
    Q&L loss on it falls from PRICE to UNDIFFERENTIATED. That is the Yokohama
    defect exactly, wearing a different spelling.

    THE MERGE IS AN OPERATOR RULING, NOT AN INFERENCE. canonical_port_key's
    own docstring calls itself "a MATCHING key, not a display value", built for
    booking->request linking — so reusing it to bucket a REPORTING aggregate
    needed a decision, and the decision was Michael's, 2026-08-31: *"no they
    are all hcmc with two different terminal requests in ho chi minh"*. Cat
    Lai and Cai Mep are two terminals of ONE lane, priced as one lane. That is
    what _PORT_ALIASES already said ("Lonny asks for 'HCMC'; OL confirms
    whichever terminal the vessel calls") and it is now confirmed for pricing
    as well as for matching.

    Routes BOTH ends through canonical_port_key, which already carries the
    alias table and the "unknown" sentinel for a lane it cannot resolve. Two
    unresolved ends therefore bucket together under unknown/unknown, which is
    correct here: they are equally unattributable, and the caller displays
    whatever spelling the rows actually carried.
    """
    if origin is not None or destination is not None:
        return canonical_port_key(origin) + ARROW + canonical_port_key(destination)
    if not lane:
        return None
    parts = str(lane).split(ARROW)
    if len(parts) != 2:
        # A degenerate or single-token lane. Key it on itself rather than
        # inventing an origin — a lane we cannot split is its own bucket.
        return canonical_port_key(lane)
    return canonical_port_key(parts[0]) + ARROW + canonical_port_key(parts[1])


def _display_lane_for(spellings) -> str:
    """Pick ONE display spelling for a merged bucket, deterministically.

    Most frequent wins; ties break alphabetically. Deterministic matters more
    than pretty — a display that flipped between fires would make the
    dashboard and the PDF disagree about the same lane on the same day.
    """
    counts = Counter(spellings)
    return min(counts, key=lambda s: (-counts[s], s))


def aggregate_lanes(requests: list[dict]) -> dict[str, dict]:
    # BUCKET ON THE CANONICAL KEY, DISPLAY THE SPELLING THE ROWS CARRIED.
    # Keying on the raw display string made one port two lanes whenever
    # two spellings reached here — see canonical_lane_id. The returned
    # dict is still keyed by a DISPLAY string, so every consumer
    # (gen_dashboard, gen_pdf, gen_rate_intelligence, share_intel) is
    # unchanged; two entries simply become one.
    _spellings: dict[str, list[str]] = {}
    for r in requests:
        _o = r.get("origin", "Oakland")
        _d = r.get("destination", "Unknown")
        _spellings.setdefault(canonical_lane_id(None, _o, _d), []).append(f"{_o} → {_d}")
    _display = {k: _display_lane_for(v) for k, v in _spellings.items()}

    lanes: dict[str, dict] = {}
    for r in requests:
        dest = r.get("destination", "Unknown")
        origin = r.get("origin", "Oakland")
        lane_key = _display[canonical_lane_id(None, origin, dest)]
        lm = lanes.setdefault(lane_key, {
            "lane": lane_key,
            "requests": 0, "wins": 0, "quoted_lost": 0, "not_quoted": 0, "pending": 0,
            "teu_requested": 0, "teu_won": 0, "teu_quoted_lost": 0, "teu_not_quoted": 0, "teu_pending": 0,
            "_winning_carriers": set(),
            "_equipment": set(),
        })
        _n = shipment_count(r)          # shipments, not rows
        lm["requests"] += _n
        lm["teu_requested"] += r.get("teu_requested", 0) or 0
        if r.get("containers"):
            lm["_equipment"].add(r["containers"])

        s = r.get("status")
        if s == "WIN":
            lm["wins"] += _n
            lm["teu_won"] += r.get("teu_won", 0) or r.get("teu_requested", 0) or 0
            if r.get("carrier_won"):
                lm["_winning_carriers"].add(r["carrier_won"])
        elif s == "PENDING":
            lm["pending"] += 1
            lm["teu_pending"] += r.get("teu_requested", 0) or 0
        elif s == "LOSS":
            if r.get("quoted"):
                lm["quoted_lost"] += 1
                lm["teu_quoted_lost"] += r.get("teu_requested", 0) or 0
            else:
                lm["not_quoted"] += 1
                lm["teu_not_quoted"] += r.get("teu_requested", 0) or 0

    for lm in lanes.values():
        lm["winning_carriers"] = ", ".join(sorted(lm.pop("_winning_carriers"))) or ""
        lm["equipment"] = ", ".join(sorted(lm.pop("_equipment"))) or ""
    return lanes


def compute_lane_winning_medians(
    requests: list[dict],
    *,
    min_wins: int = PRICE_GAP_MIN_LANE_WINS,
) -> dict[str, float]:
    """Build a {lane: median_winning_rate} lookup for PRICE classification.

    The "winning lane median" is the median ol_rate across WIN rows on a
    given lane. ``decide_status`` consumes this lookup to determine when
    a Q&L row's rate was actually uncompetitive vs simply lost for some
    other reason.

    Scope decisions (documented per CLAUDE.md §3 "every new pattern ships
    with its QC + tests"):
      - Lane key = ``r["lane"]`` (the same canonical "Oakland → Yokohama"
        format used by aggregate_lanes). Falls back to constructing from
        origin/destination if `lane` is missing.
      - WIN scope = ALL WINs in the input dataset. The tracking-data file
        already represents the active 30-day rolling window per the
        architecture, so the dataset itself is the time-bounded universe.
      - Lanes with fewer than ``min_wins`` (default 3) WINs are EXCLUDED
        — too little signal to call a median. Those lanes fall through to
        UNDIFFERENTIATED in decide_status (the honest "no benchmark"
        case).
      - Carrier scope: ALL winning carriers count. A losing-rate analysis
        cares about what cleared on the lane, not which carrier did.
      - Rates that don't parse are skipped silently (they shouldn't be in
        the dataset post-ingest, but decide_status is per-row and we
        prefer "no signal" over a crash here).

    Kept byte-for-byte identical to src/hilmar/core.compute_lane_winning_medians
    — tests/test_core_parity.py guards drift.
    """
    by_lane: dict[str, list[float]] = {}
    _seen: dict[str, set[str]] = {}
    for r in requests or []:
        if r.get("status") != "WIN":
            continue
        lane = r.get("lane")
        if not lane:
            origin = r.get("origin")
            dest = r.get("destination")
            if origin and dest:
                lane = f"{origin} → {dest}"
        if not lane:
            continue
        # ol_rate may be a string ("$3500/40HC") or a bare number — ingest
        # stores both forms across the dataset.
        raw = r.get("ol_rate")
        rate = parse_rate(raw) if isinstance(raw, str) else (
            float(raw) if isinstance(raw, (int, float)) else None
        )
        if rate is None or rate <= 0:
            continue
        # BUCKET CANONICALLY so one port spelled two ways cannot split its
        # own median below PRICE_GAP_MIN_LANE_WINS — the Yokohama defect,
        # named in the note above PORT_LOCODES and left unfixed there.
        # `_seen` keeps every spelling that fed the bucket so the RESULT
        # can be emitted under all of them and no caller has to change.
        _ck = canonical_lane_id(lane)
        by_lane.setdefault(_ck, []).append(rate)
        _seen.setdefault(_ck, set()).add(lane)

    medians: dict[str, float] = {}
    for canon, rates in by_lane.items():
        if len(rates) < min_wins:
            continue
        rates_sorted = sorted(rates)
        n = len(rates_sorted)
        med = (rates_sorted[n // 2] if n % 2 == 1
               else (rates_sorted[n // 2 - 1] + rates_sorted[n // 2]) / 2.0)
        # Emitted under EVERY spelling that fed this bucket, and ONLY those.
        # Two spellings of one port now read the SAME median instead of two
        # halves, while every existing caller keeps looking it up exactly the
        # way it always did. The canonical key is deliberately NOT emitted: it
        # is an internal bucket id, and putting it in a returned dict would
        # change an observable contract for no caller that needs it.
        for spelling in _seen.get(canon, {canon}):
            medians[spelling] = med
    return medians


def aggregate_loss_reasons(
    requests: list[dict],
    window_days: int | None = None,
    now: datetime | None = None,
) -> dict:
    """Book-wide loss-reason mix — the "why did we not win" lens.

    Kept byte-for-byte identical to src/hilmar/core.aggregate_loss_reasons;
    tests/test_core_parity.py guards drift. See that module's docstring
    for the full output shape.
    """
    losses = []
    cutoff = None
    if window_days is not None:
        cutoff = (now or now_utc()) - timedelta(days=window_days)

    for r in requests:
        status = r.get("status")
        if status not in {"Q&L", "NQ", "LOSS"}:
            continue
        lr = r.get("loss_reason")
        if not lr:
            continue
        if cutoff is not None:
            ts = (parse_iso(r.get("response_timestamp"))
                  or parse_iso(r.get("request_timestamp")))
            if ts is None or ts < cutoff:
                continue
        losses.append(lr)

    by_reason: dict[str, int] = {}
    for lr in losses:
        by_reason[lr] = by_reason.get(lr, 0) + 1
    ranked = sorted(by_reason.items(), key=lambda kv: -kv[1])

    # NB UNDIFFERENTIATED falls into "other" intentionally — those losses
    # had no concrete rate gap, no ETD miss, and no OL-silent signal. We
    # don't know what tipped them, so be honest: NOT rate_driven. This is
    # the fix for the 2026-06-02 "94% PRICE" distortion that arose from
    # the old PRICE catch-all on the decide_status side.
    _RATE_DRIVEN = {"PRICE"}
    _ETD_DRIVEN = {"ETD_MISS"}
    _OL_SILENT = {"NO_RESPONSE", "RESPONSE_NO_RATE", "SEND_NO_BOOKING"}
    actionable = {
        "rate_driven": sum(c for lr, c in by_reason.items() if lr in _RATE_DRIVEN),
        "etd_driven":  sum(c for lr, c in by_reason.items() if lr in _ETD_DRIVEN),
        "ol_silent":   sum(c for lr, c in by_reason.items() if lr in _OL_SILENT),
        "other":       sum(c for lr, c in by_reason.items()
                           if lr not in (_RATE_DRIVEN | _ETD_DRIVEN | _OL_SILENT)),
    }

    return {
        "total": len(losses),
        "by_reason": by_reason,
        "ranked": ranked,
        "window_days": window_days,
        "actionable_mix": actionable,
    }


def aggregate_carriers(requests: list[dict]) -> dict[str, dict]:
    car: dict[str, dict] = {}
    for r in requests:
        carriers = set()
        if r.get("carrier_quoted"):
            carriers.add(r["carrier_quoted"])
        if r.get("carrier_won"):
            carriers.add(r["carrier_won"])
        for c in carriers:
            if not c or c in ("N/A", ""):
                continue
            cm = car.setdefault(c, {
                "carrier": c, "quotes": 0, "wins": 0, "losses": 0, "pending": 0,
                "teu_won": 0, "teu_lost": 0, "_lanes": set(),
                "_turnaround_samples": [], "_etd_fit_samples": [],
            })
            # quotes expands with wins on a multi-booking row, or win_rate
            # (wins/quotes) exceeds 100% the moment one lands. See
            # shipment_count: the denominator moves with the numerator.
            _n = shipment_count(r)
            cm["quotes"] += _n
            cm["_lanes"].add(r.get("destination", "Unknown"))

            # Same timing reset as summarize(): a per-carrier average built
            # from pre-floor samples would rank carriers on a clock that was
            # never stopped.
            # response_time_is_evidenced for the same reason the summary
            # average has it: gen_pdf SORTS the carrier scoreboard by this
            # number and the dashboard tells the reader to use it in line
            # meetings. Guarding the summary alone would leave the carrier
            # table disagreeing with the KPI strip printed above it — two
            # numbers off one dataset, again.
            if (r.get("turnaround_biz_hours") and r["turnaround_biz_hours"] > 0
                    and response_time_is_evidenced(r)
                    and timing_is_valid(r.get("request_timestamp"))):
                cm["_turnaround_samples"].append(r["turnaround_biz_hours"])
            if r.get("etd_fit_days") is not None:
                cm["_etd_fit_samples"].append(r["etd_fit_days"])

            if r.get("status") == "WIN" and r.get("carrier_won") == c:
                cm["wins"] += _n
                cm["teu_won"] += r.get("teu_won", 0) or r.get("teu_requested", 0) or 0
            elif r.get("status") == "LOSS" and r.get("quoted") and r.get("carrier_quoted") == c:
                cm["losses"] += 1
                cm["teu_lost"] += r.get("teu_requested", 0) or 0
            elif r.get("status") == "PENDING" and r.get("carrier_quoted") == c:
                cm["pending"] += 1

    for _c, cm in car.items():
        cm["lanes_quoted"] = len(cm.pop("_lanes"))
        ta = cm.pop("_turnaround_samples")
        ef = cm.pop("_etd_fit_samples")
        cm["avg_turnaround_biz_hours"] = round(sum(ta) / len(ta), 2) if ta else None
        cm["avg_etd_fit_days"] = round(sum(ef) / len(ef), 1) if ef else None
        cm["win_rate"] = round(cm["wins"] / cm["quotes"] * 100, 1) if cm["quotes"] else 0.0
    return car


# ─────────────────────────────────────────────────────────────────────
# DOD (What Happened Today) diff
# ─────────────────────────────────────────────────────────────────────

def snapshot_state(requests: list[dict]) -> dict[str, dict]:
    """Compact snapshot keyed by request_id for diffing."""
    out = {}
    for r in requests:
        rid = r.get("request_id")
        if not rid:
            continue
        out[rid] = {
            "status": r.get("status"),
            "quoted": bool(r.get("quoted")),
            "has_send": bool(r.get("has_send")),
            "carrier_won": r.get("carrier_won"),
            "mdolx_ref": r.get("mdolx_ref"),
        }
    return out


def compute_dod(prev: dict[str, dict], curr_requests: list[dict], today_iso: str | None = None) -> dict:
    """Build the 'What Happened Today' object from prev vs current."""
    today_iso = today_iso or datetime.now(ET).strftime("%Y-%m-%d")
    new_requests = []
    new_responses = []
    status_changes = []
    new_wins = []
    new_pending = []
    newly_lost = []

    for r in curr_requests:
        rid = r.get("request_id")
        if not rid:
            continue
        lane = r.get("lane") or f"Oakland → {r.get('destination','?')}"
        prior = prev.get(rid)

        # Totally new request
        if not prior:
            new_requests.append({
                "lane": lane,
                "equipment": r.get("containers"),
                "teu": r.get("teu_requested", 0),
                "request_time_pt": r.get("lonny_time_pt") or "—",
            })

        # New response
        if (r.get("response_timestamp")
                and (not prior or r.get("response_timestamp") != prior.get("response_timestamp", None))
                and r.get("quoted")):
            new_responses.append({
                "lane": lane,
                "carrier": r.get("carrier_quoted") or "—",
                "rate": r.get("ol_rate") or "—",
                "response_time_et": r.get("olusa_time_et") or "—",
                "turnaround_biz": f"{r.get('turnaround_biz_hours', 0)}h",
            })

        # Status transitions
        prior_status = prior["status"] if prior else None
        cur_status = r.get("status")
        if prior_status and prior_status != cur_status:
            status_changes.append({
                "lane": lane,
                "from": prior_status,
                "to": cur_status,
                "mdolx": r.get("mdolx_ref") or "",
                "carrier_won": r.get("carrier_won") or "",
            })
            if cur_status == "WIN":
                new_wins.append({
                    "lane": lane, "carrier": r.get("carrier_won"),
                    "mdolx": r.get("mdolx_ref"), "teu": r.get("teu_won", 0),
                })
            elif cur_status == "LOSS" and r.get("quoted"):
                newly_lost.append({
                    "lane": lane, "carrier": r.get("carrier_quoted"),
                    "rate": r.get("ol_rate"), "teu": r.get("teu_requested", 0),
                })

        # Still pending (new today)
        if cur_status == "PENDING" and (not prior or prior_status != "PENDING"):
            resp = parse_iso(r.get("response_timestamp"))
            hours_since = None
            if resp:
                hours_since = round((now_utc() - resp).total_seconds() / 3600.0, 1)
            new_pending.append({
                "lane": lane, "carrier": r.get("carrier_quoted"),
                "rate": r.get("ol_rate"), "hours_since_quote": hours_since,
            })

    summary_text = (
        f"{len(new_requests)} new requests, "
        f"{len(new_responses)} quotes received, "
        f"{len(new_wins)} wins, "
        f"{len(new_pending)} pending Hilmar response"
    )

    return {
        "date": today_iso,
        "new_requests": new_requests,
        "new_responses": new_responses,
        "status_changes": status_changes,
        "new_wins": new_wins,
        "new_pending": new_pending,
        "newly_lost": newly_lost,
        "summary_text": summary_text,
    }


# ─────────────────────────────────────────────────────────────────────
# Rate trend helper
# ─────────────────────────────────────────────────────────────────────

_RATE_RX = re.compile(r"\$\s*([0-9][0-9,]*(?:\.\d+)?)")


def parse_rate(rate_str: str | None) -> float | None:
    if not rate_str or not isinstance(rate_str, str):
        return None
    m = _RATE_RX.search(rate_str.replace(",", ""))
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except Exception:
        return None


def rate_trends(requests: list[dict], *, min_abs_pct: float = 0.1) -> list[dict]:
    """
    Per (carrier, destination) series of rate_date, rate. Sorted by date.
    Returned rows: {carrier, destination, series:[{date, rate}], latest, prior_avg, pct_change}.

    Michael's rule: "a biggest rate mover is not something with 0 percent change."
    We therefore EXCLUDE any (carrier, destination) pair whose absolute pct change
    is below ``min_abs_pct`` (default 0.1%). Flat lanes never show up as "movers".

    Carrier names are normalized (CMA / CMA-CGM → "CMA CGM") before bucketing so the
    same steamship line does not split across rows.
    """
    buckets: dict[tuple[str, str], list[dict]] = {}
    for r in requests:
        carrier_raw = r.get("carrier_quoted") or r.get("carrier_won")
        carrier = normalize_carrier(carrier_raw)
        dest = r.get("destination")
        rate = parse_rate(r.get("ol_rate"))
        d = r.get("request_date") or r.get("date")
        if not carrier or not dest or rate is None or not d:
            continue
        buckets.setdefault((carrier, dest), []).append({"date": d, "rate": rate})

    out = []
    for (carrier, dest), series in buckets.items():
        series.sort(key=lambda x: x["date"])
        if len(series) < 2:
            continue
        latest = series[-1]["rate"]
        prior = series[:-1]
        prior_avg = sum(x["rate"] for x in prior) / len(prior)
        if not prior_avg:
            continue  # can't compute a pct change from a zero baseline
        pct = round((latest - prior_avg) / prior_avg * 100, 1)
        # Guard: exclude flat movers (Michael's rule).
        if abs(pct) < min_abs_pct:
            continue
        out.append({
            "carrier": carrier, "destination": dest, "series": series,
            "latest": latest, "prior_avg": round(prior_avg, 2), "pct_change": pct,
        })
    out.sort(key=lambda x: abs(x["pct_change"]), reverse=True)
    return out



# ─────────────────────────────────────────────────────────────────────
# OL responder signer extraction (added 2026-04-30 — Plan A schema lift)
# ─────────────────────────────────────────────────────────────────────
# When OL replies come from a shared mailbox (MBD_OceanExportBookingShared,
# MBD_Export_Pricing), the From shows the mailbox display name BUT the actual
# human is in (a) the From display-name override, (b) the body signature,
# or (c) the email From-name field. This helper normalizes any of those.
#
# Known OL-USA team (config.provider.responders + observed signers).
# Last-name-only entries (e.g. "tobel") are intentional: they let the
# Email-pattern reverse map ("caren.tobel@ol-usa.com" → "Caren Tobel") match
# even when only the last name appears in the body display name.
_OL_INDIVIDUALS_FULL = {
    # Booking & operations
    "caren tobel":        "Caren Tobel",
    "linda echevarria":   "Linda Echevarria",
    "steve petriccione":  "Steve Petriccione",
    "alan baer":          "Alan Baer",
    "carrie murphy":      "Carrie Murphy",
    "seada sabic":        "Seada Sabic",
    "michael deitchman":  "Michael Deitchman",
    # Export operations / coordinators we've observed signing
    "alexandra hernandez":"Alexandra Hernandez",
    "ryan gordon":        "Ryan Gordon",
    "matthew fleisig":    "Matthew Fleisig",  # tts-worldwide (parent of OL)
    "joseph corcoran":    "Joseph Corcoran",
    "karen larada":       "Karen Larada",
    # Pricing desk individuals
    "thomas ryan":        "Thomas Ryan",
    "christopher martin": "Christopher Martin",
}

# First-name → canonical full-name lookup. Built from _OL_INDIVIDUALS_FULL.
_OL_FIRST_NAMES = {full.split()[0].lower(): full for full in _OL_INDIVIDUALS_FULL.values()}

# Composite set used by lower-level checks
_OL_INDIVIDUALS = set(_OL_INDIVIDUALS_FULL.keys()) | set(_OL_FIRST_NAMES.keys())

# Mailbox display-names we should NEVER treat as a signer
_OL_MAILBOX_NAMES = {
    "mbd ocean export booking", "mbd ocean export booking shared",
    "mbd export pricing", "mbd export docs", "mbd export docs (shared)",
}

# Names that frequently appear in chain replies and must NEVER be returned
# even if we somehow find them via fuzzy match. These are CUSTOMER side.
_BLOCKLIST = {
    "lonny upfold", "lonny",
    "ignacio pronczuk", "ignacio",  # NUMIDIA (allow reroute via from_name only)
    "lucia fernandez", "lucia",
    "paula borraz", "paula",
    "angela gamboa", "angela",
    "zdenka torres", "zdenka",
    "eddie",
}

#: Words that appear where a name would and are not one. The signature regex
#: matches the line after a sign-off, which is usually the person — but on a
#: block like "Best regards,\nOcean Export Team" it is not. Cheap, and it only
#: has to catch the shapes that actually occur in OL mail.
_NOT_A_NAME = {
    "ocean", "export", "import", "team", "desk", "pricing", "booking",
    "customer", "service", "support", "operations", "logistics", "best",
    "regards", "thanks", "thank", "sincerely", "cheers", "sent", "from",
    "subject", "hilmar", "ingredients",
}


def _looks_like_person(name: str) -> bool:
    """A parsed signature line that is plausibly a human name.

    Deliberately permissive — the roster gate it replaces was the bug, and
    the sender domain (see parse_signer) is what actually establishes that
    this is OL staff. This only rejects the obvious non-names.
    """
    parts = [p for p in name.strip().split() if p]
    if not 1 <= len(parts) <= 3:
        return False
    if any(p.lower() in _NOT_A_NAME for p in parts):
        return False
    return all(p[:1].isupper() and p.isalpha() for p in parts)


_SIGNATURE_PATTERNS = [
    # "Best regards,\nFirstname Lastname"
    re.compile(r"(?im)^[ \t]*(?:thanks|thank you|regards|best|best regards|kind regards|cheers|sincerely|warm regards|warmest regards|warmest)[,!&\s]*(?:and\s+best\s+regards)?[,!]?[ \t]*\r?\n+[ \t]*([A-Z][a-z]+(?:[ \t]+[A-Z][a-z]+)?)(?:\s*\([^)]+\))?\s*\r?$"),
    # Standalone first-name line like "Caren" or "Caren " between blank lines
    re.compile(r"(?im)^\s*(Caren|Linda|Steve|Alan|Carrie|Seada|Alexandra|Ryan|Matthew|Karen|Thomas|Christopher)(?:\s+[A-Z][a-z]+)?\s*$"),
]

# "Email: Firstname.Lastname@ol-usa.com" → reliable signer ID
_EMAIL_SIG_RX = re.compile(
    r"(?:^|\s)email:?\s*([A-Za-z][A-Za-z\.'\-]*)\.([A-Za-z][A-Za-z\.'\-]*)@ol-usa\.com",
    re.IGNORECASE,
)
# Bare "Firstname.Lastname@ol-usa.com" anywhere (fallback, no Email: prefix)
_BARE_OL_EMAIL_RX = re.compile(
    r"\b([A-Za-z][A-Za-z\.'\-]+)\.([A-Za-z][A-Za-z\.'\-]+)@ol-usa\.com",
    re.IGNORECASE,
)

# Chain-reply marker — anything below this is the previous message and must
# be excluded from signer search.
_CHAIN_MARKER_RX = re.compile(
    r"(?im)^\s*(?:from:|de:|von:|enviado el:|sent:)\s",
)

def _strip_chain(body: str) -> str:
    """Return only the most-recent message portion (everything above the first
    'From:' / 'De:' / 'Sent:' chain marker)."""
    if not body:
        return ""
    m = _CHAIN_MARKER_RX.search(body)
    return body[: m.start()] if m else body


def _name_from_email(local_first: str, local_last: str):
    """Convert 'Firstname.Lastname' email local-part into 'Firstname Lastname',
    accepting only known OL individuals."""
    full = f"{local_first} {local_last}".lower()
    if full in _OL_INDIVIDUALS_FULL:
        return _OL_INDIVIDUALS_FULL[full]
    return None


def parse_signer(from_name, body=None):
    """Return the human OL signer, or None if unresolvable."""
    if from_name:
        clean = from_name.strip().lower()
        if clean in _OL_MAILBOX_NAMES:
            pass
        elif clean in _OL_INDIVIDUALS_FULL:
            return _OL_INDIVIDUALS_FULL[clean]
        elif clean in _OL_FIRST_NAMES:
            return _OL_FIRST_NAMES[clean]
        else:
            for known in _OL_INDIVIDUALS_FULL:
                if known in clean or clean in known:
                    return _OL_INDIVIDUALS_FULL[known]
            return None
    if not body:
        return None
    top = _strip_chain(body)
    em = _EMAIL_SIG_RX.search(top)
    if em:
        name = _name_from_email(em.group(1), em.group(2))
        if name:
            return name
    candidates = []
    for rx in _SIGNATURE_PATTERNS:
        for m in rx.finditer(top):
            name = m.group(1).strip()
            low = name.lower()
            if low in _BLOCKLIST:
                continue
            if low in _OL_INDIVIDUALS_FULL:
                candidates.append((m.start(), _OL_INDIVIDUALS_FULL[low]))
                continue
            if low in _OL_FIRST_NAMES:
                candidates.append((m.start(), _OL_FIRST_NAMES[low]))
                continue
            parts = low.split()
            if len(parts) == 2:
                full_key = f"{parts[0]} {parts[1]}"
                if full_key in _OL_INDIVIDUALS_FULL:
                    candidates.append((m.start(), _OL_INDIVIDUALS_FULL[full_key]))
                    continue
                if parts[0] in _OL_FIRST_NAMES:
                    candidates.append((m.start(), _OL_FIRST_NAMES[parts[0]]))
                    continue
            # A NAME OFF THE ROSTER IS STILL A NAME. Michael 2026-08-20:
            # "a signor is a signor if new staff comes, new staff comes. if
            # they change they change.. maria machado is staff then."
            #
            # Until now every branch above required membership in a
            # 14-entry hardcoded roster, so a cleanly-parsed signature was
            # FOUND and then discarded. Measured on the Aug-19 report: 8 of
            # 12 quoted rows had a blank signer, and the body of one showed
            # "Best regards, / Maria Machado / Ocean Export Specialist" in
            # plain text with a full OL phone and address block. She is
            # staff; the roster simply had not been edited. A closed list
            # means every new OL hire is invisible by construction, and
            # silently so — nothing checks this field.
            #
            # WHY ACCEPTING AN UNKNOWN NAME IS SAFE HERE. parse_signer is
            # only ever called on a body whose bucket is mbd_inbound or
            # mbd_rate_response (fetch_bodies.py:231), and refresh_stage
            # assigns those buckets ONLY when the sender is @ol-usa.com.
            # Michael, same day: "lonny doesn't sign from an ol email
            # address" — exactly so, and the bucket already enforces it.
            # _BLOCKLIST stays as the second line of defence: an OL reply
            # quotes the ask beneath it, and if _strip_chain ever fails to
            # cut the chain, Lonny's own sign-off is sitting right there.
            if _looks_like_person(name):
                candidates.append((m.start(), name))
    if candidates:
        candidates.sort(key=lambda x: x[0])
        return candidates[0][1]
    bm = _BARE_OL_EMAIL_RX.search(top)
    if bm:
        name = _name_from_email(bm.group(1), bm.group(2))
        if name:
            return name
    return None
