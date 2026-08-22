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

import contextlib
import hashlib
import json
import os
import re
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

#: Window used by the SEND-signal aging branch and is_business_stale's
#: default. NOT the pending-Hilmar quote window (PENDING_HILMAR_LOSS_HOURS).
#: Mirrored in scripts/core.py — tests/test_core_parity.py + QC-040
#: enforce parity.
PENDING_WINDOW_HOURS = 24

#: PENDING_HILMAR quote-decision window (Michael 2026-07-14, supersedes the
#: 2026-06-04 "Tuesday 18:00 ET" carve-out FOR QUOTED ROWS): a quote awaiting
#: Lonny's decision is Quoted & Lost after 24 CLOCK hours — 72 if OL quoted on
#: a FRIDAY (ET), to carry the weekend so a Friday quote lands Monday, not
#: Sunday. Measured from the OL quote (response_timestamp). SEND-signal aging
#: (is_business_stale) is deliberately unchanged.
PENDING_HILMAR_LOSS_HOURS = 24
PENDING_HILMAR_LOSS_HOURS_FRIDAY = 72
#: PENDING-OL window — how long OL-USA has to answer Lonny's RFQ before the
#: row is called a genuine non-response (NQ). Symmetric with the Hilmar side
#: (PENDING_HILMAR_LOSS_HOURS): 24 CLOCK hours from Lonny's REQUEST, 72 when
#: the RFQ landed on a Friday (ET) so the weekend doesn't burn the window.
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
#: period is not "slow OL", it is a clock started and never stopped.
#:
#: RETIRED 2026-08-13 PM, on evidence, once shared-mailbox access closed the
#: gap described above. Measured over 288 rows carrying both timestamps: ZERO
#: responses predate their own ask, and the 8 (2.8%) beyond 30 days are April
#: asks paired to June/July replies, which QC-021 already clears at >40
#: biz-hours. Emptied rather than deleted so the falsy branch below keeps this
#: a one-line switch in BOTH directions. See scripts/core.py for the full
#: write-up.
#:
#: Mirrored from scripts/core.py — tests/test_core_parity.py enforces it.
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
#: Mirrored across trees; tests/test_core_parity.py enforces parity.
MAX_ROW_CONTAINERS = 60
MAX_ROW_TEU = 100

# AWAITING_MDOLX_AGING_HOURS (was 72) was removed 2026-05-30 — the
# send-aging branch now uses is_business_stale(send_at, now) with the
# default hours=PENDING_WINDOW_HOURS for symmetry, picking up the same
# Friday weekend carve-out. The audit confirmed zero callers existed.
RATE_TREND_THRESHOLD_PCT = 10

#: Status enum — TWO classifiers coexist intentionally during the
#: src/hilmar/ ↔ scripts/ convergence period (per Michael 2026-05-17
#: "never to allow drift like this as standard"). The split:
#:
#:   STRICT (4-state, written by src/hilmar/ingest.py + qc.py):
#:     {WIN, Q&L, PENDING, NQ}
#:     Q&L = quoted and lost.  NQ = not quoted.  PENDING = inside 24h window.
#:     More information-dense — no need for derivation at render.
#:
#:   LEGACY (3-state, used by scripts/core.py + the 155 production records):
#:     {WIN, LOSS, PENDING}
#:     LOSS + quoted=True  is equivalent to STRICT's Q&L.
#:     LOSS + quoted=False is equivalent to STRICT's NQ.
#:
#: `VALID_STATUSES` accepts BOTH forms. QC-040 (cross-folder drift check)
#: ENFORCES that any tracking-data-v2.json uses one form CONSISTENTLY —
#: mixed-form data is a parser bug and gets flagged as ERROR.
#:
#: Bridge helpers below (display_status, normalize_to_strict, normalize_to_legacy)
#: are the SINGLE SOURCE OF TRUTH for crossing between the two forms.
#: All cross-classifier comparisons MUST go through these helpers.
VALID_STATUSES_STRICT = frozenset({"WIN", "Q&L", "PENDING", "NQ"})
VALID_STATUSES_LEGACY = frozenset({"WIN", "LOSS", "PENDING"})
VALID_STATUSES = VALID_STATUSES_STRICT | VALID_STATUSES_LEGACY

#: Loss-reason / sub-state taxonomy. LOSS rows get a `loss_reason`
#: as their final-state explanation. LOSS+quoted=True (display: Q&L)
#: rows typically have PRICE / ETD_MISS / QUOTED_NOT_BOOKED.
#: LOSS+quoted=False (display: NQ) rows typically have NO_RESPONSE /
#: RESPONSE_NO_RATE. PENDING rows usually have None (a quote inside
#: the 24h Lonny-response window) but two PENDING sub-states exist:
#:   AWAITING_MDOLX  — Lonny said Send, OL hasn't generated MDOLX yet.
#:                     Auto-promotes to WIN on a later run when MDOLX
#:                     lands in the same chain.
#:   MDOLX_NO_SEND   — MDOLX present without an explicit Lonny send.
#:                     Anomaly; rare; flagged for ops review (typically
#:                     a parser miss on the lonny_reply side).
LOSS_REASONS = {
    "NO_RESPONSE",          # display-NQ — OL never responded
    "RESPONSE_NO_RATE",     # display-NQ — MBD acked but did not quote
    "QUOTED_NOT_BOOKED",    # display-Q&L — generic, no ETD signal
    "PRICE",                # display-Q&L — concrete rate gap vs winning lane median
    "ETD_MISS",             # display-Q&L — ETD missed Lonny's ask by ≥5d
    "UNDIFFERENTIATED",     # display-Q&L — quoted & lost with no concrete signal
                            #   to explain why (rate was at/below winning lane
                            #   median, ETD fit OK, no other reason). Added
                            #   2026-06-02 to replace PRICE as the catch-all
                            #   fallback that was inflating the "Push carriers"
                            #   signal — see decide_status docstring.
    "OTHER",                # display-Q&L — malformed timestamp / unknown
    "AWAITING_MDOLX",       # PENDING sub-state — Send received, MDOLX pending
    "MDOLX_NO_SEND",        # PENDING sub-state — MDOLX without send (anomaly)
    "SEND_NO_BOOKING",      # display-Q&L — AWAITING_MDOLX aged out past 72h
    "COVERED",              # scripts/core.LOSS_REASONS compat — Lonny said the load was covered elsewhere
    "DRAFT_ONLY",           # scripts/core.LOSS_REASONS compat — DRAFT RATED reply, no full rate
    "NO_RESPONSE_TS",       # PENDING sub-state, then display-Q&L — OL quoted but
                            #   the response carried no usable timestamp (typically
                            #   a patch_carriers rate from a sibling thread or PDF).
                            #   Added 2026-07-27 so it stops hiding inside "OTHER";
                            #   see the never-age-on-absence branch in decide_status.
}

#: Multiplier above lane winning median where we call a loss "PRICE".
#: A 5% premium above the lane winning median is the threshold; below
#: that the rate was competitive and the loss is UNDIFFERENTIATED.
#: Mirrored in scripts/core.py — tests/test_core_parity.py guards.
PRICE_GAP_THRESHOLD_MULT = 1.05

#: Minimum number of historical WINs on a lane before we'll trust the
#: lane winning median for PRICE determination. Fewer than 3 WINs and
#: we lack signal — fall through to UNDIFFERENTIATED.
#: Mirrored in scripts/core.py — tests/test_core_parity.py guards.
PRICE_GAP_MIN_LANE_WINS = 3


def display_status(r: dict) -> str:
    """Return the 4-state DISPLAY label for a row regardless of storage form.

    A row written by scripts/ingest.py (3-state LEGACY) and a row written by
    src/hilmar/ingest.py (4-state STRICT) both return the same display label
    after this normalization:
      WIN   → WIN
      LOSS+quoted=True  → Q&L      (storage was LEGACY)
      LOSS+quoted=False → NQ       (storage was LEGACY)
      Q&L   → Q&L                  (storage was STRICT)
      NQ    → NQ                   (storage was STRICT)
      PENDING → PENDING

    Use this anywhere you render Q&L vs NQ to a user. Never compare
    `r["status"] == "Q&L"` directly — it'll silently miss legacy rows
    where Q&L is encoded as LOSS+quoted=True. Use `is_quoted_and_lost(r)`
    or `display_status(r) == "Q&L"` instead.
    """
    s = (r or {}).get("status")
    if s == "LOSS":
        return "Q&L" if (r or {}).get("quoted") else "NQ"
    return s


def is_quoted_and_lost(r: dict) -> bool:
    """True if row is quoted-and-lost in EITHER classifier form.

    Storage-agnostic. Use this instead of `r["status"] == "Q&L"` or
    `r["status"] == "LOSS" and r["quoted"]`.
    """
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


def detect_classifier_form(requests: list[dict]) -> str:
    """Detect which classifier form a list of requests uses.

    Returns one of:
      "strict"  — uses STRICT 4-state (Q&L / NQ present, no LOSS)
      "legacy"  — uses LEGACY 3-state (LOSS present, no Q&L / NQ)
      "mixed"   — DRIFT: both forms present (parser bug, flagged by QC-040)
      "empty"   — no non-WIN/PENDING rows to determine
    """
    has_strict = any(r.get("status") in ("Q&L", "NQ") for r in (requests or []))
    has_legacy = any(r.get("status") == "LOSS" for r in (requests or []))
    if has_strict and has_legacy:
        return "mixed"
    if has_strict:
        return "strict"
    if has_legacy:
        return "legacy"
    return "empty"

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


def load_config(path: Path | str | None = None) -> dict:
    path = Path(path) if path else CONFIG_PATH
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_data(path: Path | str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_data(data: dict, path: Path | str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, default=str)


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
    """
    if (r.get("status") or "").upper() != "WIN":
        return None
    dated = [d for d in (et_date_of(h.get("at"))
                         for h in (r.get("status_history") or [])
                         if h.get("to") == "WIN") if d]
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
    # that just ENDED (live failure, run #76: a 12:38 AM Thursday dispatch
    # reported an all-zero "Thu" and poisoned Thursday's send-flag). Applies
    # only when a time-of-day is known; date-only inputs are untouched.
    # Mirrors scripts/core.py (paired surface).
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
# biz-hours ages to Q&L (SEND_NO_BOOKING) via decide_status. Mirror of
# scripts/core.py — keep the two byte-identical (test_core_parity).
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

#: Status enum — single source of truth for the four-state classifier.
#: Production bug 2026-04-27 motivated the expansion: the old
#: {WIN, LOSS, PENDING} classifier collapsed two distinct cases (LOSS-
#: with-quoted-rate vs LOSS-with-no-rate) into a single LOSS state, and
#: callers had to disambiguate via a separate `quoted` flag. When the
#: `quoted` flag itself got stale (Bug 1), 26 quoted-and-lost rows
#: looked like not-quoted ones, the summary dropped the quote rate to
#: 25% (real ~90%), and qc.phase_3 then "cleaned" the rate fields off
#: those rows because !quoted. Splitting LOSS into Q&L + NQ at the
#: status level removes the disambiguation step entirely.
STATUS_WIN = "WIN"
STATUS_Q_AND_L = "Q&L"      # quoted, no booking, past 24h window
STATUS_PENDING = "PENDING"  # quoted, within 24h response window
STATUS_NQ = "NQ"            # not quoted (inc. responded-but-no-rate edge)

ALL_STATUSES = (STATUS_WIN, STATUS_Q_AND_L, STATUS_PENDING, STATUS_NQ)


@dataclass
class StatusDecision:
    status: str                    # one of ALL_STATUSES
    quoted: bool
    has_send: bool
    loss_reason: str | None        # see below
    reason_detail: str             # human-readable why

    # loss_reason taxonomy:
    #   None              — WIN or PENDING
    #   "NO_RESPONSE"     — NQ, OL-USA never responded
    #   "RESPONSE_NO_RATE"— NQ, MBD acknowledged but did not actually quote
    #   "QUOTED_NOT_BOOKED"— Q&L, generic (no ETD signal to disambiguate)
    #   "PRICE"           — Q&L, ol_rate > lane_winning_median * 1.05
    #   "ETD_MISS"        — Q&L, ETD missed Lonny's ask by ≥5 days
    #   "UNDIFFERENTIATED"— Q&L, no concrete signal (rate competitive,
    #                       ETD fit OK, or insufficient lane history)
    #   "OTHER"           — Q&L, malformed timestamp / unknown


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

    Kept byte-for-byte identical to scripts/core.is_business_stale —
    tests/test_core_parity.py fails if they drift.
    """
    if dt is None:
        return False
    now = now or now_utc()
    dt_et = dt.astimezone(ET)
    if dt_et.weekday() >= 4:                          # Fri=4, Sat=5, Sun=6
        # Land on the upcoming Tuesday 18:00 ET. Friday → +4 days, Saturday
        # → +3, Sunday → +2. (weekday(Tue) == 1.)
        days_to_tue = (1 - dt_et.weekday()) % 7       # Fri=4→4, Sat=5→3, Sun=6→2
        deadline = (dt_et + timedelta(days=days_to_tue)).replace(
            hour=18, minute=0, second=0, microsecond=0)
    else:
        deadline = dt_et + timedelta(hours=hours)
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
    in QC-007 while decide_status ran 48h+Friday.

    Behaviour for existing 2-arg callers is unchanged: with request_dt
    defaulting to None the anchor is exactly resp_dt, including the None case.

    Kept byte-for-byte identical to scripts/core.pending_hilmar_stale —
    tests/test_core_parity.py fails if they drift.
    """
    anchor = resp_dt if resp_dt is not None else request_dt
    if anchor is None:
        return False
    now = now or now_utc()
    anchor_et = anchor.astimezone(ET)
    deadline = (PENDING_HILMAR_LOSS_HOURS_FRIDAY if anchor_et.weekday() == 4
                else PENDING_HILMAR_LOSS_HOURS)
    return (now - anchor).total_seconds() / 3600.0 >= deadline


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
    if request_dt is None:
        return True
    now = now or now_utc()
    req_et = request_dt.astimezone(ET)
    deadline = (PENDING_OL_LOSS_HOURS_FRIDAY if req_et.weekday() == 4
                else PENDING_OL_LOSS_HOURS)
    return (now - request_dt).total_seconds() / 3600.0 >= deadline


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

    Decision tree (post 2026-06-02 — smarter PRICE classifier):
      has_send AND has_mdolx              → WIN
      has_send AND !has_mdolx             → PENDING AWAITING_MDOLX
      has_mdolx AND !has_send             → PENDING MDOLX_NO_SEND (anomaly)
      else !response_timestamp            → NQ NO_RESPONSE
      else !quoted (rare edge)            → NQ RESPONSE_NO_RATE
      else timestamp unparseable          → Q&L OTHER (assumed aged)
      else within the PENDING_HILMAR window → PENDING
      else etd_fit_days ≥ ETD_MISS_DAYS   → Q&L ETD_MISS
      else rate > lane_med * 1.05         → Q&L PRICE
      else (rate competitive OR no signal)→ Q&L UNDIFFERENTIATED
      else (no etd, no lane_med, no rate) → Q&L QUOTED_NOT_BOOKED

    Reading B (Michael, 2026-04-27): WIN requires BOTH a Lonny "send"
    handoff AND an OL-side MDOLX booking confirmation in the same chain.
    Send-only stages as AWAITING_MDOLX and auto-promotes to WIN on a
    later daily run when MDOLX lands (mdolx_ref is in _RECOMPUTED_FIELDS,
    so the merge picks up the fresh value and the next decide_status
    pass flips status). Pre-Reading-B: WIN if has_send OR mdolx_ref —
    that produced false-positive WINs whenever a Send went out before
    OL confirmed the booking.

    PRICE determination (2026-06-02 rewrite): Pre-rewrite the function
    used ``etd_fit_days < 5 → PRICE`` as a catch-all, which produced
    94%-PRICE-driven loss-mix readouts even on lanes where winning and
    losing medians were identical (Oakland→Yokohama $3500/$3500, etc.).
    PRICE now requires a concrete rate gap: ``ol_rate`` must exceed the
    winning lane median by more than PRICE_GAP_THRESHOLD_MULT (default
    5%). Otherwise the loss is UNDIFFERENTIATED — the honest "we lost,
    can't pin a cause" bucket that surfaces as the operator's signal to
    investigate the email thread rather than blaming rate by default.

    ``lane_winning_median`` is computed once by the caller (use
    ``compute_lane_winning_medians(requests)``) and passed in as a
    {lane: median_rate} lookup so this function stays per-row pure.
    When None or missing the lane key, PRICE never fires —
    UNDIFFERENTIATED is the safe fallback.

    The send_signal_events / mdolx_refs_all params are secondary
    membership checks — Lonny may have confirmed via a chain that
    didn't update the primary ``has_send`` flag, or MDOLX may have
    landed via a child thread that's only tracked in mdolx_refs_all.
    """
    now = now or now_utc()

    # Effective signal checks per Reading B: draw from primary OR
    # secondary fields on each side.
    has_send_eff = bool(has_send) or bool(send_signal_events)
    has_mdolx_eff = bool(mdolx_ref and str(mdolx_ref).strip()) or bool(mdolx_refs_all)

    # WIN — strict: BOTH signals required.
    if has_send_eff and has_mdolx_eff:
        return StatusDecision(STATUS_WIN, True, True, None,
                              "Lonny replied Send AND MDOLX booking confirmed")

    # Send received, MDOLX not yet confirmed — booking in flight. Stages
    # as PENDING(AWAITING_MDOLX) and auto-promotes to WIN on a later run
    # when OL generates MDOLX. If too much time has passed since the send
    # without an MDOLX arriving, demote to Q&L(SEND_NO_BOOKING) — at that
    # point the deal is functionally lost (OL dropped the ball, carrier
    # never confirmed, or the booking moved off-system).
    if has_send_eff and not has_mdolx_eff:
        # Aging clock starts at the most recent send signal. send_signal_events
        # is the canonical timestamp source (set by ingest.apply_send_signals);
        # fall back to response_timestamp if no events were captured.
        send_at = None
        for ev in (send_signal_events or []):
            ts = parse_iso(ev.get("at") if isinstance(ev, dict) else None)
            if ts and (send_at is None or ts > send_at):
                send_at = ts
        if send_at is None:
            send_at = parse_iso(response_timestamp)
        # THE ROW THAT NEVER AGED (2026-08-13). Michael, verbatim: "if you
        # have the quotes and you do not see a booking for the quote, then
        # it's a loss  that's it". With neither a send event nor a
        # response_timestamp, send_at stayed None and is_business_stale
        # returns False on None — so the row held PENDING/AWAITING_MDOLX at
        # ANY age. Fall back to Lonny's request, the one clock we never have
        # to invent. See scripts/core.py for the full write-up;
        # tests/test_core_parity.py enforces the two agree.
        if send_at is None:
            send_at = parse_iso(request_timestamp)
        if is_business_stale(send_at, now):
            # has_send stays TRUE — evidence field, not a state field. See the
            # matching branch in scripts/core.py for the full 2026-07-26
            # write-up; tests/test_core_parity.py enforces they agree.
            return StatusDecision(
                STATUS_Q_AND_L, True, True, "SEND_NO_BOOKING",
                f"Send received but no MDOLX within the "
                f"{PENDING_WINDOW_HOURS}h (biz-hours) cutoff — booking never "
                f"confirmed (real wins confirm same/next business day)"
            )
        return StatusDecision(STATUS_PENDING, True, True, "AWAITING_MDOLX",
                              "Lonny replied Send — awaiting MDOLX booking confirmation")

    # MDOLX present without an explicit send signal — anomaly. In normal
    # flow ingest's standalone-booking path explicitly sets has_send=True,
    # so this branch generally only fires when a parser misses the send
    # on the lonny_reply side. Flag for ops review.
    if has_mdolx_eff and not has_send_eff:
        return StatusDecision(STATUS_PENDING, True, False, "MDOLX_NO_SEND",
                              "MDOLX present without send signal — anomaly, see reason_detail")

    # OL never QUOTED → NQ. Check the quote FIRST: a row that DID carry a rate
    # (quoted=True) can NEVER be NO_RESPONSE. The old order tested
    # response_timestamp before `quoted`, so a real quote with a missing
    # timestamp was bucketed as "OL never responded" — inflating NQ and
    # rendering Time-to-Quote as "—". No response at all → NO_RESPONSE;
    # response landed but no rate was extracted (e.g. MBD said "checking with
    # carrier...") → RESPONSE_NO_RATE. Both display as NQ.
    if not quoted:
        if not response_timestamp:
            # OL has not answered YET — open business to chase, not a loss.
            # See scripts/core.decide_status for the full rationale
            # (2026-07-24): the old immediate NO_RESPONSE made PENDING_OL
            # structurally unreachable and stored live RFQs as losses.
            req_dt = parse_iso(request_timestamp)
            if not pending_ol_stale(req_dt, now):
                _w = (PENDING_OL_LOSS_HOURS_FRIDAY
                      if req_dt and req_dt.astimezone(ET).weekday() == 4
                      else PENDING_OL_LOSS_HOURS)
                return StatusDecision(
                    STATUS_PENDING, False, False, None,
                    f"Awaiting OL quote — within the {_w}h response window")
            return StatusDecision(STATUS_NQ, False, False, "NO_RESPONSE",
                                  "OL-USA never responded with a quote")
        return StatusDecision(STATUS_NQ, False, False, "RESPONSE_NO_RATE",
                              "MBD responded but no rate extracted — see reason_detail")

    # Quoted — a quoted row with a missing/None response_timestamp falls
    # through here; parse_iso(None)→None hits the "assumed aged" Q&L guard.
    resp_dt = parse_iso(response_timestamp)
    if not resp_dt:
        # NEVER AGE ON ABSENCE — see the matching branch in scripts/core.py
        # for the full 2026-07-27 write-up. A missing timestamp is missing
        # EVIDENCE, not elapsed time; fall back to Lonny's request clock and
        # hold PENDING until that window expires.
        req_dt = parse_iso(request_timestamp)
        if req_dt and not pending_hilmar_stale(req_dt, now):
            _age = (now - req_dt).total_seconds() / 3600.0
            return StatusDecision(
                STATUS_PENDING, True, False, "NO_RESPONSE_TS",
                f"Quoted, but the OL response carried no usable timestamp — "
                f"aging off Lonny's request instead ({_age:.1f}h ago, still "
                f"inside the decision window)")
        return StatusDecision(
            STATUS_Q_AND_L, True, False, "NO_RESPONSE_TS",
            "Quoted but response_timestamp unparseable, and the request itself "
            "is past the decision window")

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
        return StatusDecision(STATUS_PENDING, True, False, None,
                              f"Quoted {hours_since:.1f}h ago — Lonny still within "
                              f"the {_win}h decision window")

    # Quoted & Lost. Tag the reason as best we can.
    base = f"Quoted {hours_since:.1f}h ago, no Send — Q&L"

    # ETD miss wins first — it's a concrete signal regardless of price.
    if etd_fit_days is not None and etd_fit_days >= ETD_MISS_DAYS:
        return StatusDecision(STATUS_Q_AND_L, True, False, "ETD_MISS",
                              f"{base} (ETD missed Lonny's ask by {etd_fit_days}d)")

    # PRICE requires a real rate gap vs the winning lane median (2026-06-02).
    rate_val = parse_rate(ol_rate) if isinstance(ol_rate, str) else (
        float(ol_rate) if isinstance(ol_rate, (int, float)) else None
    )
    lane_med = None
    if lane_winning_median and lane:
        lane_med = lane_winning_median.get(lane)
    if rate_val is not None and lane_med and lane_med > 0:
        if rate_val > lane_med * PRICE_GAP_THRESHOLD_MULT:
            gap_pct = (rate_val - lane_med) / lane_med * 100.0
            return StatusDecision(
                STATUS_Q_AND_L, True, False, "PRICE",
                f"{base} (rate ${rate_val:.0f} is {gap_pct:.0f}% above "
                f"lane winning median ${lane_med:.0f} → rate-driven)"
            )
        return StatusDecision(
            STATUS_Q_AND_L, True, False, "UNDIFFERENTIATED",
            f"{base} (rate ${rate_val:.0f} ≤ lane winning median "
            f"${lane_med:.0f} — competitive on price, root cause unclear)"
        )

    # No signal to determine PRICE.
    if rate_val is None and etd_fit_days is None and lane_med is None:
        return StatusDecision(
            STATUS_Q_AND_L, True, False, "QUOTED_NOT_BOOKED",
            f"{base}, no ETD signal, no rate parsed")
    detail_suffix = ""
    if rate_val is None:
        detail_suffix = " (no ol_rate to compare against lane winning median)"
    elif lane_med is None:
        detail_suffix = " (no lane winning history to benchmark against)"
    return StatusDecision(
        STATUS_Q_AND_L, True, False, "UNDIFFERENTIATED",
        f"{base}{detail_suffix}")


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
    often and the one OL's grid answers most completely. Neither leg falls
    back to the other: a row with an arrival ask and no offered ETA yields
    (None, None), not a departure comparison.

    ``requested_dates`` is deliberately NOT consulted. It is free text
    ("Cutoff next week or the following") with no stated leg, and guessing
    which one it means is exactly what this function exists to stop.
    """
    eta_ask, eta_off = row.get("eta_requested"), row.get("eta_offered")
    if eta_ask and eta_off:
        fit = etd_fit_days(eta_ask, eta_off)
        if fit is not None:
            return fit, "arrival"
    etd_ask = row.get("etd_requested") or row.get("cutoff_requested")
    etd_off = row.get("etd_offered")
    if etd_ask and etd_off:
        fit = etd_fit_days(etd_ask, etd_off)
        if fit is not None:
            return fit, "departure"
    return None, None



# ─────────────────────────────────────────────────────────────────────
# Summary / lane / carrier aggregation
# ─────────────────────────────────────────────────────────────────────

def _sum(iterable: Iterable[int]) -> int:
    return sum(x or 0 for x in iterable)


#: Marker qc_selfheal writes on a row whose response_timestamp was COPIED
#: from another row's quote rather than read off an email of its own.
#: MIRRORS scripts/core.py. Production renders from scripts/, but
#: test_timing_reset pairs the two aggregate_summary implementations directly
#: and a guard living in one tree only is exactly how they drift.
BORROWED_RESPONSE_TIME = "sibling_quote"


def response_time_is_evidenced(r: dict) -> bool:
    """True when this row's response_timestamp came from an actual email.
    Full history on scripts/core.response_time_is_evidenced."""
    r = r or {}
    if not r.get("response_timestamp"):
        return False
    return r.get("response_time_source") != BORROWED_RESPONSE_TIME


def aggregate_summary(requests: list[dict]) -> dict:
    # Bucket by the status field directly (post 2026-04-27 four-state
    # classifier). The legacy code derived ql/nq from a single LOSS
    # bucket using the `quoted` flag, which silently mislabelled rows
    # whenever `quoted` was stale (Bug 1). Keying off `status` removes
    # that disambiguation.
    wins = [r for r in requests if r.get("status") == STATUS_WIN]
    ql = [r for r in requests if r.get("status") == STATUS_Q_AND_L]
    nq = [r for r in requests if r.get("status") == STATUS_NQ]
    pending = [r for r in requests if r.get("status") == STATUS_PENDING]

    # win_rate per CLAUDE.md §6 = Wins / (Wins + Q&L). NQ is "no contest
    # happened" (NO_RESPONSE / RESPONSE_NO_RATE) and must be EXCLUDED from
    # the denominator — otherwise a busy day with OL silent on many quotes
    # silently suppresses the win-rate number on the daily client email.
    # Bug discovered 2026-06-02 audit (track 03 Critical finding C-1).
    # NQ rate is reported as its own separate metric ("not_quoted").
    win_rate_denom = len(wins) + len(ql)
    total = len(requests)
    total_quoted = len(wins) + len(ql) + len(pending)

    # TIMING RESET (Michael 2026-08-13, see TIMING_VALID_FROM). A sample from
    # before the floor measures a clock that was started and never stopped.
    _timed = [r for r in requests if response_time_is_evidenced(r)]
    _measurable = [r for r in _timed if (r.get("turnaround_biz_hours") or 0) > 0]
    ta_entries = [r for r in _measurable
                  if timing_is_valid(r.get("request_timestamp"))]
    ta_excluded = len(_measurable) - len(ta_entries)
    avg_biz = (round(sum(r["turnaround_biz_hours"] for r in ta_entries)
                     / len(ta_entries), 2) if ta_entries else None)

    _clock = [r for r in requests if (r.get("turnaround_hours") or 0) > 0
              and timing_is_valid(r.get("request_timestamp"))]
    avg_clock = (round(sum(r["turnaround_hours"] for r in _clock)
                       / len(_clock), 2) if _clock else None)

    return {
        "total_entries": total,
        "wins": len(wins),
        "quoted_lost": len(ql),
        "not_quoted": len(nq),
        "pending_hilmar": len(pending),
        "win_rate": round(len(wins) / win_rate_denom * 100, 1) if win_rate_denom else 0.0,
        "quote_rate": round(total_quoted / total * 100, 1) if total else 0.0,
        "teu_requested": _sum(r.get("teu_requested", 0) for r in requests),
        "teu_won": _sum(r.get("teu_won", 0) or r.get("teu_requested", 0) for r in wins),
        "teu_quoted_lost": _sum(r.get("teu_requested", 0) for r in ql),
        "teu_not_quoted": _sum(r.get("teu_requested", 0) for r in nq),
        "teu_pending": _sum(r.get("teu_requested", 0) for r in pending),
        "turnaround_entries": len(ta_entries),
        "turnaround_avg_biz_hours": avg_biz,
        "turnaround_avg_clock_hours": avg_clock,
        "turnaround_valid_from": TIMING_VALID_FROM,
        "turnaround_excluded": ta_excluded,
    }


def aggregate_lanes(requests: list[dict]) -> dict[str, dict]:
    lanes: dict[str, dict] = {}
    for r in requests:
        dest = r.get("destination", "Unknown")
        origin = r.get("origin", "Oakland")
        lane_key = f"{origin} → {dest}"
        lm = lanes.setdefault(lane_key, {
            "lane": lane_key,
            "requests": 0, "wins": 0, "quoted_lost": 0, "not_quoted": 0, "pending": 0,
            "teu_requested": 0, "teu_won": 0, "teu_quoted_lost": 0, "teu_not_quoted": 0, "teu_pending": 0,
            "_winning_carriers": set(),
            "_equipment": set(),
        })
        lm["requests"] += 1
        lm["teu_requested"] += r.get("teu_requested", 0) or 0
        if r.get("containers"):
            lm["_equipment"].add(r["containers"])

        s = r.get("status")
        if s == STATUS_WIN:
            lm["wins"] += 1
            lm["teu_won"] += r.get("teu_won", 0) or r.get("teu_requested", 0) or 0
            if r.get("carrier_won"):
                lm["_winning_carriers"].add(r["carrier_won"])
        elif s == STATUS_PENDING:
            lm["pending"] += 1
            lm["teu_pending"] += r.get("teu_requested", 0) or 0
        elif s == STATUS_Q_AND_L:
            lm["quoted_lost"] += 1
            lm["teu_quoted_lost"] += r.get("teu_requested", 0) or 0
        elif s == STATUS_NQ:
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
    other reason — the fix for the 2026-06-02 "94% PRICE" distortion.

    Scope decisions (documented per CLAUDE.md §3 "every new pattern ships
    with its QC + tests"):
      - Lane key = ``r["lane"]`` (the canonical "Oakland → Yokohama"
        format used by aggregate_lanes). Falls back to constructing
        from origin/destination if `lane` is missing.
      - WIN scope = ALL WINs in the input dataset. tracking-data already
        represents an active rolling window, so the dataset itself is
        the time-bounded universe.
      - Lanes with fewer than ``min_wins`` (default 3) WINs are EXCLUDED
        — too little signal to call a median. Those lanes fall through
        to UNDIFFERENTIATED in decide_status.
      - Carrier scope: ALL winning carriers count. A losing-rate
        analysis cares about what cleared on the lane, not which
        carrier did.
      - Rates that don't parse are skipped silently — decide_status is
        per-row and prefers "no signal" over a crash.

    Kept byte-for-byte identical to scripts/core.compute_lane_winning_medians
    — tests/test_core_parity.py guards drift.
    """
    by_lane: dict[str, list[float]] = {}
    for r in requests or []:
        if r.get("status") != STATUS_WIN:
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
        by_lane.setdefault(lane, []).append(rate)

    medians: dict[str, float] = {}
    for lane, rates in by_lane.items():
        if len(rates) < min_wins:
            continue
        rates_sorted = sorted(rates)
        n = len(rates_sorted)
        if n % 2 == 1:
            medians[lane] = rates_sorted[n // 2]
        else:
            medians[lane] = (rates_sorted[n // 2 - 1] + rates_sorted[n // 2]) / 2.0
    return medians


def aggregate_loss_reasons(
    requests: list[dict],
    window_days: int | None = None,
    now: datetime | None = None,
) -> dict:
    """Book-wide loss-reason mix — the "why did we not win" lens.

    Returns::

      {
        "total":   <int>,                 # total losses in window
        "by_reason": {"PRICE": 28, "ETD_MISS": 12, "NO_RESPONSE": 5, ...},
        "ranked":  [("PRICE", 28), ("ETD_MISS", 12), ...],   # high → low
        "window_days": <int>|None,        # None = all-time
        "actionable_mix": {               # buckets for the daily email banner
            "rate_driven":  <int>,    # PRICE only (NOT UNDIFFERENTIATED)
            "etd_driven":   <int>,    # ETD_MISS
            "ol_silent":    <int>,    # NO_RESPONSE + RESPONSE_NO_RATE + SEND_NO_BOOKING
            "other":        <int>,    # UNDIFFERENTIATED + OTHER + QUOTED_NOT_BOOKED
                                       # + COVERED + DRAFT_ONLY
        },
      }

    This is the chart the daily email never shipped — per the 2026-05-31
    audit, ``aggregate_carriers.loss_reasons`` already computes a
    per-carrier mix but the book-wide rollup was missing. With this in
    place, the daily email can show e.g. "of your 47 losses last 30d:
    28 PRICE, 12 ETD_MISS, 5 NO_RESPONSE" — telling Michael whether to
    push carriers (rate_driven), push ops (etd_driven), or push Lonny
    (ol_silent).

    Counts BOTH STRICT (status=="Q&L"/"NQ") and LEGACY (status=="LOSS")
    rows so the function works against either tree's status vocabulary.
    Rows without a loss_reason are dropped.
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
                # Per-carrier loss-reason distribution. Populated only on
                # Q&L rows (where carrier_quoted == c) — captures why
                # OL-USA lost the booking. Renders as e.g.
                #   "MSC: 12 lost — 7 PRICE, 4 ETD_MISS, 1 OTHER"
                # in the carrier scoreboard.
                "loss_reasons": {},
            })
            cm["quotes"] += 1
            cm["_lanes"].add(r.get("destination", "Unknown"))

            # Same timing reset as summarize().
            if (r.get("turnaround_biz_hours") and r["turnaround_biz_hours"] > 0
                    and timing_is_valid(r.get("request_timestamp"))):
                cm["_turnaround_samples"].append(r["turnaround_biz_hours"])
            if r.get("etd_fit_days") is not None:
                cm["_etd_fit_samples"].append(r["etd_fit_days"])

            if r.get("status") == STATUS_WIN and r.get("carrier_won") == c:
                cm["wins"] += 1
                cm["teu_won"] += r.get("teu_won", 0) or r.get("teu_requested", 0) or 0
            elif r.get("status") == STATUS_Q_AND_L and r.get("carrier_quoted") == c:
                cm["losses"] += 1
                cm["teu_lost"] += r.get("teu_requested", 0) or 0
                lr = r.get("loss_reason") or "OTHER"
                cm["loss_reasons"][lr] = cm["loss_reasons"].get(lr, 0) + 1
            elif r.get("status") == STATUS_PENDING and r.get("carrier_quoted") == c:
                cm["pending"] += 1

    for cm in car.values():
        cm["lanes_quoted"] = len(cm.pop("_lanes"))
        ta = cm.pop("_turnaround_samples")
        ef = cm.pop("_etd_fit_samples")
        cm["avg_turnaround_biz_hours"] = round(sum(ta) / len(ta), 2) if ta else None
        cm["avg_etd_fit_days"] = round(sum(ef) / len(ef), 1) if ef else None
        cm["win_rate"] = round(cm["wins"] / cm["quotes"] * 100, 1) if cm["quotes"] else 0.0
        # Human-readable summary: "12 lost — 7 PRICE, 4 ETD_MISS, 1 OTHER".
        # Sorted by count DESC for emphasis on the dominant reason.
        if cm["loss_reasons"]:
            ordered = sorted(cm["loss_reasons"].items(), key=lambda kv: -kv[1])
            cm["loss_reason_summary"] = ", ".join(f"{n} {r}" for r, n in ordered)
        else:
            cm["loss_reason_summary"] = ""
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
            "response_timestamp": r.get("response_timestamp"),
        }
    return out


def persist_daily_snapshot(
    data: dict,
    snapshots_dir: Path | str,
    *,
    today_iso: str | None = None,
) -> Path:
    """Write a per-day snapshot file for trend analysis + dod diffing.

    Schema:
      {
        "date": "YYYY-MM-DD",
        "generated_at": "<ISO8601>",
        "row_state": {request_id: snapshot_state row},
        "summary": <data["summary"] block>
      }

    One file per day at ``snapshots_dir/YYYY-MM-DD.json``. Re-running the
    same day overwrites — last-run-of-the-day wins, which is what we want
    for the daily diff (compute_dod compares yesterday's last state to
    today's). Files are tiny (~5–10KB) so retention is effectively unlimited.
    """
    snapshots_dir = Path(snapshots_dir)
    snapshots_dir.mkdir(parents=True, exist_ok=True)
    today = today_iso or datetime.now(ET).strftime("%Y-%m-%d")
    payload = {
        "date": today,
        "generated_at": now_utc().isoformat(),
        "row_state": snapshot_state(data.get("requests") or []),
        "summary": data.get("summary") or {},
    }
    out = snapshots_dir / f"{today}.json"
    out.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return out


def load_previous_snapshot(
    snapshots_dir: Path | str,
    *,
    today_iso: str | None = None,
) -> dict | None:
    """Load the most recent daily snapshot whose date is STRICTLY BEFORE
    ``today_iso``. Used by compute_dod to diff today's state against the
    previous day's. Returns None if no prior snapshot exists (first-run case).
    """
    snapshots_dir = Path(snapshots_dir)
    if not snapshots_dir.exists():
        return None
    today = today_iso or datetime.now(ET).strftime("%Y-%m-%d")
    candidates = sorted(
        p for p in snapshots_dir.glob("*.json")
        if p.stem < today  # ISO date strings sort lexically = chronologically
    )
    if not candidates:
        return None
    try:
        return json.loads(candidates[-1].read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


# State labels for the active-conversations table in the daily email.
# Renamed 2026-05-01: "PENDING SEND" was confusing — it sounded like the
# send was pending OL's action. The real semantic: OL sent the quote,
# Lonny hasn't said "send" yet — i.e. we're awaiting Hilmar's send
# instruction. New labels:
#   AWAITING QUOTE = Lonny asked, OL hasn't quoted yet (was "AWAITING")
#   AWAITING SEND  = OL quoted, Lonny hasn't said send (was "PENDING SEND")
_ACTIVE_STATE_PRIORITY = {"AWAITING SEND": 0, "AWAITING QUOTE": 1}

# Names that must never be rendered as the OL "Quoted by" attribution.
# Defense in depth — even if parse_signer leaks one of these through,
# the renderer drops it. Source of truth lives in body_parser, but this
# is a last-mile guard for the user-facing dashboard.
_CUSTOMER_SIDE_NAMES_LC = frozenset({
    "lonny upfold", "lonny", "upfold",
    "hilmar ingredients", "hilmar, ca",
})


def compute_dod(prev: dict[str, dict], curr_requests: list[dict], today_iso: str | None = None) -> dict:
    """Build the 'What Happened Today' object from prev vs current.

    Emits two views of the same activity:
      * Granular lists (``new_requests``, ``new_responses``, ``new_pending``,
        ``new_wins``, ``newly_lost``, ``status_changes``) — one entry per
        per-row event. The same request can appear in multiple lists when
        it transitioned through several states in one day.
      * Unified ``active_conversations`` list — one entry per request that
        had activity since last run, in its current state. Replaces the
        three "open pipeline" tables (rate-asked / quoted / pending-send)
        in the email so the same conversation isn't shown three times.
    """
    today_iso = today_iso or datetime.now(ET).strftime("%Y-%m-%d")
    new_requests = []
    new_responses = []
    status_changes = []
    new_wins = []
    new_pending = []
    newly_lost = []
    active_by_id: dict[str, dict] = {}

    def _quoted_by(r: dict) -> str:
        # Reject any customer-side name that leaked through parse_signer
        # (defense in depth — see body_parser._CUSTOMER_SIDE_SIGNERS).
        # When the only attribution we have is a customer name, fall back
        # to the team mailbox label or the responder mailbox display name
        # rather than mis-attributing the quote.
        signer = r.get("ol_responder_signer")
        if signer and signer.strip().lower() in _CUSTOMER_SIDE_NAMES_LC:
            signer = None
        # If we still have no individual signer, surface the team alias
        # ("OL Rate Desk") so the column is never empty for a quoted row —
        # better to show a credible team attribution than "—" / Lonny.
        if signer:
            return signer
        responder = r.get("ol_responder")
        if responder and responder.strip().lower() not in _CUSTOMER_SIDE_NAMES_LC:
            return responder
        # Last resort — quoted rows without any attribution should still
        # show a meaningful label. Production OL is the rate desk.
        return "OL Rate Desk" if r.get("quoted") else "—"

    def _equip_label(r: dict) -> str:
        cont = r.get("containers")
        eq = r.get("equipment_size")
        if cont:
            return cont
        if eq:
            return f"{eq}'"
        return "—"

    for r in curr_requests:
        rid = r.get("request_id")
        if not rid:
            continue
        lane = r.get("lane") or f"Oakland → {r.get('destination','?')}"
        prior = prev.get(rid)
        prior_status = prior["status"] if prior else None
        cur_status = r.get("status")
        equip = _equip_label(r)
        teu = r.get("teu_requested", 0)
        carrier = r.get("carrier_quoted") or r.get("carrier_won")
        rate = r.get("ol_rate")
        requested_at_pt = r.get("lonny_time_pt") or "—"
        quoted_at_et = r.get("olusa_time_et") or "—"
        quoted_by = _quoted_by(r)
        tat = f"{r.get('turnaround_biz_hours', 0)}h"

        # Totally new request
        if not prior:
            new_requests.append({
                "lane": lane,
                "equipment": r.get("containers"),
                "teu": teu,
                "request_time_pt": requested_at_pt,
            })
            if cur_status == STATUS_PENDING and not r.get("quoted"):
                # Lonny asked, OL hasn't quoted yet.
                active_by_id[rid] = {
                    "request_id": rid, "lane": lane, "equipment": equip,
                    "teu": teu, "carrier": "—", "rate": None,
                    "requested_at_pt": requested_at_pt, "quoted_at_et": "—",
                    "quoted_by": "—", "tat": "—", "hours_since_quote": None,
                    "state": "AWAITING QUOTE",
                }

        # New response (quote arrived today)
        new_response_today = (
            r.get("response_timestamp")
            and (not prior or r.get("response_timestamp") != prior.get("response_timestamp", None))
            and r.get("quoted")
        )
        if new_response_today:
            new_responses.append({
                "lane": lane,
                "carrier": carrier or "—",
                "rate": rate or "—",
                "response_time_et": quoted_at_et,
                "turnaround_biz": tat,
                "requested_at_pt": requested_at_pt,
                "quoted_by": quoted_by,
            })

        # Status transitions
        if prior_status and prior_status != cur_status:
            status_changes.append({
                "lane": lane,
                "from": prior_status,
                "to": cur_status,
                "mdolx": r.get("mdolx_ref") or "",
                "carrier_won": r.get("carrier_won") or "",
            })
            if cur_status == STATUS_WIN:
                new_wins.append({
                    "lane": lane, "carrier": r.get("carrier_won"),
                    "mdolx": r.get("mdolx_ref"), "teu": r.get("teu_won", 0),
                })
            elif cur_status == STATUS_Q_AND_L:
                # Q&L = quoted-and-lost, the only LOSS variant we report
                # in "newly lost" (NQ rows are tracked separately).
                newly_lost.append({
                    "lane": lane, "carrier": carrier,
                    "rate": rate, "teu": teu,
                })

        # Still pending (new today)
        new_pending_today = (
            cur_status == STATUS_PENDING and (not prior or prior_status != STATUS_PENDING)
        )
        if new_pending_today:
            resp = parse_iso(r.get("response_timestamp"))
            hours_since = None
            if resp:
                hours_since = round((now_utc() - resp).total_seconds() / 3600.0, 1)
            new_pending.append({
                "lane": lane, "carrier": carrier,
                "rate": rate, "hours_since_quote": hours_since,
            })

        # Active-conversation roll-up. Only rows in non-terminal states
        # land here — AWAITING SEND (quote received, Lonny hasn't said
        # send) or AWAITING QUOTE (request in, no quote yet, set above
        # in the not-prior branch). Q&L / WIN / NQ are terminal and
        # surface via newly_lost / new_wins / status_changes instead, so
        # they don't appear in active_conversations to avoid duplication.
        if cur_status == STATUS_PENDING and r.get("quoted"):
            resp = parse_iso(r.get("response_timestamp"))
            hours_since = None
            if resp:
                hours_since = round((now_utc() - resp).total_seconds() / 3600.0, 1)
            active_by_id[rid] = {
                "request_id": rid, "lane": lane, "equipment": equip,
                "teu": teu, "carrier": carrier or "—", "rate": rate,
                "requested_at_pt": requested_at_pt,
                "quoted_at_et": quoted_at_et, "quoted_by": quoted_by,
                "tat": tat, "hours_since_quote": hours_since,
                "state": "AWAITING SEND",
            }

    active_conversations = sorted(
        active_by_id.values(),
        key=lambda c: (_ACTIVE_STATE_PRIORITY.get(c["state"], 99), -(parse_rate(c.get("rate")) or 0)),
    )

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
        "active_conversations": active_conversations,
        "summary_text": summary_text,
    }


# ─────────────────────────────────────────────────────────────────────
# Rate trend helper
# ─────────────────────────────────────────────────────────────────────

#: Pre-fix this regex required a leading "$" — but ingest stores the
#: parsed numeric portion of ol_rate as a bare string (e.g. "3500.0"),
#: so dashboards rendered "—" for every Rate (per FEU) cell. Two
#: alternations now: ``$N`` accepts any digit run; bare ``N`` requires
#: ≥ 3 digits so we don't catch FEU sizes like "40" as a rate.
_RATE_DOLLAR_RX = re.compile(
    r"\$\s*(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)",
)
_RATE_BARE_RX = re.compile(
    r"\b(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d{3,}(?:\.\d+)?)\b",
)


def parse_rate(rate_str: str | int | float | None) -> float | None:
    """Parse a rate from string OR numeric input.

    Pre-fix this rejected non-strings, but ingest stores ol_rate as
    a bare float (e.g. ``420.0``) — so every WIN row ended up with
    ``rate_per_feu = None``, which collapsed the Value-won KPI / subject
    headline / trade-region value_won column to ``$0``.
    """
    if rate_str is None:
        return None
    if isinstance(rate_str, (int, float)):
        try:
            return float(rate_str)
        except (TypeError, ValueError):
            return None
    if not isinstance(rate_str, str) or not rate_str:
        return None
    m = _RATE_DOLLAR_RX.search(rate_str) or _RATE_BARE_RX.search(rate_str)
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except (TypeError, ValueError):
        return None


def parse_rate_per_feu(rate_str: str | None, containers: str | None = None) -> float | None:
    """Normalize ``ol_rate`` to per-FEU (40' equivalent unit) so cross-lane
    comparisons line up. ``$2400/40HC`` → 2400. ``$1200/20'`` → 2400 (×2
    because two TEU = one FEU). Returns None when the rate can't be parsed.

    When ``containers`` is provided we use its first container size. Falls
    back to scanning the rate string itself for ``20``/``40`` size hints.
    Default assumption is 40' if neither source gives a hint — most rate
    desks quote per 40' container by default.
    """
    rate = parse_rate(rate_str)
    if rate is None:
        return None
    size = None
    if containers:
        for m in _CONTAINER_RX.finditer(containers):
            try:
                size = int(m.group(2))
                break
            except (TypeError, ValueError):
                continue
    if size is None and isinstance(rate_str, str) and rate_str:
        if "20" in rate_str and "200" not in rate_str:  # crude but skips $2,200
            size = 20
        elif "40" in rate_str:
            size = 40
    if size == 20:
        return rate * 2.0
    return rate


# ─────────────────────────────────────────────────────────────────────
# Equipment-size derivation (e.g. "2×40'HC" → "40HC")
# ─────────────────────────────────────────────────────────────────────

_EQUIP_TYPE_RX = re.compile(r"(?P<size>20|40|45)['\s]*(?P<type>HC|RF|DV|GP|HQ|REEFER|HIGH\s*CUBE)?", re.IGNORECASE)


def equipment_size(containers: str | None) -> str | None:
    """Return the canonical equipment size string for the request — used
    by analytics that segment by 20' vs 40' vs 40HC vs 40RF.

    Examples:
      "2×40'HC"          → "40HC"
      "1x20'DV"          → "20"
      "3×20' + 2×40'HC"  → "20+40HC" (mixed)
      "" / None          → None
    """
    if not containers or not isinstance(containers, str):
        return None
    seen: list[str] = []
    for m in _EQUIP_TYPE_RX.finditer(containers):
        size = m.group("size")
        type_raw = (m.group("type") or "").upper().replace(" ", "")
        type_norm = "HC" if type_raw in ("HC", "HIGHCUBE", "HQ") else (
            "RF" if type_raw in ("RF", "REEFER") else "")
        slug = f"{size}{type_norm}"
        if slug not in seen:
            seen.append(slug)
    if not seen:
        return None
    return "+".join(seen)


# ─────────────────────────────────────────────────────────────────────
# Trade-region mapping (destination → trade lane bucket)
# ─────────────────────────────────────────────────────────────────────

#: Destination → trade region mapping. Conservative — only countries we've
#: seen rate-desk activity on. Unknown destinations return "Other".
_TRADE_REGION_BY_KEYWORD: tuple[tuple[tuple[str, ...], str], ...] = (
    (("shanghai", "ningbo", "xingang", "qingdao", "tianjin", "yantian",
      "shenzhen", "dalian", "xiamen", "guangzhou"), "China"),
    (("yokohama", "tokyo", "kobe", "nagoya", "osaka"), "Japan"),
    (("busan", "incheon"), "Korea"),
    (("hcmc", "ho chi minh", "haiphong", "hai phong"), "Vietnam"),
    (("manila", "subic"), "Philippines"),
    (("singapore",), "Singapore"),
    (("port klang", "klang", "tanjung"), "Malaysia"),
    (("bangkok", "laem chabang"), "Thailand"),
    (("jakarta", "surabaya"), "Indonesia"),
    (("kaohsiung", "taipei", "keelung"), "Taiwan"),
    (("hong kong",), "Hong Kong"),
    (("rotterdam", "antwerp", "hamburg", "felixstowe", "le havre"), "North Europe"),
    (("genoa", "barcelona", "valencia", "marseille", "piraeus", "naples"), "Mediterranean"),
    (("dubai", "jebel ali", "abu dhabi", "doha", "dammam", "jeddah"), "Middle East"),
    (("santos", "buenos aires", "callao", "valparaiso", "manzanillo"), "Latin America"),
    (("durban", "cape town", "lagos", "mombasa"), "Africa"),
    (("sydney", "melbourne", "brisbane", "auckland"), "Oceania"),
)


def trade_region(destination: str | None) -> str:
    """Map a destination to its trade region bucket. Returns ``"Other"``
    when no keyword matches. Used by analytics that aggregate across
    destinations (e.g. "are we losing more on China vs SE Asia?")."""
    if not destination or not isinstance(destination, str):
        return "Other"
    needle = destination.lower()
    for keywords, region in _TRADE_REGION_BY_KEYWORD:
        if any(k in needle for k in keywords):
            return region
    return "Other"


# ─────────────────────────────────────────────────────────────────────
# Validity-window parser (regex for "valid until X" patterns)
# ─────────────────────────────────────────────────────────────────────

_VALIDITY_RX = re.compile(
    r"""
    \b(?:rate\s+)?(?:valid|validity|valid\s+through|valid\s+until|expir(?:es|y))
    \s*[:\-]?\s*
    (?P<window>
        \d{1,2}[/\-\.]\d{1,2}(?:[/\-\.]\d{2,4})?
        (?:\s*(?:to|through|thru|-)\s*\d{1,2}[/\-\.]\d{1,2}(?:[/\-\.]\d{2,4})?)?
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)


def parse_validity_window(body: str | None) -> str | None:
    """Extract a rate's validity window (e.g. "5/15-5/31") from an email
    body. Returns the matched window string or None.

    Targets the most common patterns rate desks use:
      "valid 5/15-5/31"
      "Rate valid through 5/31"
      "validity: 5/15"
      "Expires 5/31"

    Caller can hand the None case to the LLM-fallback layer for less
    common phrasings.
    """
    if not body or not isinstance(body, str):
        return None
    m = _VALIDITY_RX.search(body)
    if not m:
        return None
    return m.group("window").strip()


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
# Period trends (WoW / MoM / YTD) — read from daily_snapshots/{date}.json
# ─────────────────────────────────────────────────────────────────────

#: Summary fields we trend across periods.
_TREND_METRICS: tuple[str, ...] = (
    "wins", "quoted_lost", "not_quoted", "pending_hilmar",
    "win_rate", "quote_rate", "teu_won", "teu_requested",
)


def _load_snapshots_in_range(
    snapshots_dir: Path | str,
    *,
    start_date: date,
    end_date: date,
) -> list[dict]:
    """Load daily snapshots whose date is in [start_date, end_date].
    Ordered ascending. Missing days silently skipped (no interpolation).
    Bad JSON skipped."""
    snapshots_dir = Path(snapshots_dir)
    if not snapshots_dir.exists():
        return []
    out: list[dict] = []
    for p in sorted(snapshots_dir.glob("*.json")):
        try:
            d = date.fromisoformat(p.stem)
        except ValueError:
            continue
        if start_date <= d <= end_date:
            try:
                out.append(json.loads(p.read_text(encoding="utf-8")))
            except (json.JSONDecodeError, OSError):
                continue
    return out


def _aggregate_period(snapshots: list[dict]) -> dict[str, float]:
    """Take END-OF-PERIOD value for every metric.

    Each daily snapshot's ``summary`` is a CUMULATIVE running total
    produced by ``aggregate_summary`` (e.g. wins=10 means "10 total
    wins in the dataset as of end-of-day"). Summing across days
    double-counts — the right answer for both count and rate metrics
    is the latest snapshot's value within the window.

    Trend deltas in :func:`_period_block` then compare end-of-current
    vs end-of-prior: e.g. "10 wins today vs 7 a week ago = +3 (+42.9%)".
    """
    if not snapshots:
        return {}
    out: dict[str, float] = {}
    for k in _TREND_METRICS:
        for s in reversed(snapshots):
            v = (s.get("summary") or {}).get(k)
            if v is not None:
                with contextlib.suppress(TypeError, ValueError):
                    out[k] = float(v)
                break
    return out


def _period_block(
    cur: list[dict],
    prev: list[dict],
    *,
    n_need: int,
    label: str,
) -> dict:
    """One period's comparison + sufficiency flag."""
    cur_agg = _aggregate_period(cur)
    prev_agg = _aggregate_period(prev)
    delta: dict[str, float | None] = {}
    pct_metrics = {"win_rate", "quote_rate"}
    for m in _TREND_METRICS:
        c = cur_agg.get(m)
        p = prev_agg.get(m)
        if c is None or p is None:
            delta[m] = None
        elif m in pct_metrics:
            delta[m] = round(c - p, 1)
        else:
            if p == 0:
                # No prior baseline. Distinguish "no change" from
                # "new activity" — string sentinel so the renderer can
                # show "(new)" instead of a meaningless inf%.
                delta[m] = None if c == 0 else "new"
            else:
                delta[m] = round(100.0 * (c - p) / p, 1)
    return {
        "label": label,
        "current": cur_agg,
        "prior": prev_agg,
        "delta": delta,
        "n_days_have": len(cur),
        "n_days_need": n_need,
        "sufficient": len(cur) >= n_need and len(prev) >= n_need,
    }


def compute_period_trends(
    snapshots_dir: Path | str,
    *,
    today: date | None = None,
) -> dict[str, dict]:
    """Compute WoW / MoM / YTD trends from daily_snapshots/{date}.json.

    Each block carries a ``sufficient`` flag — render uses that to show
    real numbers vs '(collecting — N/M days)' placeholders during the
    history-warm-up period.
    """
    snapshots_dir = Path(snapshots_dir)
    today = today or now_utc().date()
    out: dict[str, dict] = {}

    wow_cur = _load_snapshots_in_range(snapshots_dir,
        start_date=today - timedelta(days=6), end_date=today)
    wow_prev = _load_snapshots_in_range(snapshots_dir,
        start_date=today - timedelta(days=13), end_date=today - timedelta(days=7))
    out["wow"] = _period_block(wow_cur, wow_prev, n_need=7, label="WoW")

    mom_cur = _load_snapshots_in_range(snapshots_dir,
        start_date=today - timedelta(days=29), end_date=today)
    mom_prev = _load_snapshots_in_range(snapshots_dir,
        start_date=today - timedelta(days=59), end_date=today - timedelta(days=30))
    out["mom"] = _period_block(mom_cur, mom_prev, n_need=30, label="MoM")

    ytd = _load_snapshots_in_range(snapshots_dir,
        start_date=date(today.year, 1, 1), end_date=today)
    out["ytd"] = {
        "label": "YTD",
        "current": _aggregate_period(ytd),
        "n_days_have": len(ytd),
        "since": date(today.year, 1, 1).isoformat(),
        "sufficient": len(ytd) >= 1,
    }
    return out


# ─────────────────────────────────────────────────────────────────────
# Pricing-level analysis (per-lane median + our quote vs market)
# ─────────────────────────────────────────────────────────────────────


def compute_pricing_levels(
    requests: list[dict],
    *,
    min_quotes_per_lane: int = 2,
) -> dict:
    """Per (carrier, destination) pricing analysis. Surfaces the lanes
    where our quote is significantly above (expensive) or below (cheap)
    the lane median — actionable for rate-desk negotiation prep.

    Works on the current data window — no history required.
    """
    buckets: dict[tuple[str, str], list[dict]] = {}
    for r in requests:
        carrier_raw = r.get("carrier_quoted") or r.get("carrier_won")
        carrier = normalize_carrier(carrier_raw) if carrier_raw else None
        dest = r.get("destination")
        rate = parse_rate(r.get("ol_rate"))
        d = r.get("response_timestamp") or r.get("request_date")
        if not carrier or not dest or rate is None:
            continue
        buckets.setdefault((carrier, dest), []).append({
            "rate": rate, "date": d or "",
        })

    per_lane: list[dict] = []
    for (carrier, dest), points in buckets.items():
        if len(points) < min_quotes_per_lane:
            continue
        rates = sorted(p["rate"] for p in points)
        n = len(rates)
        median = rates[n // 2] if n % 2 else (rates[n // 2 - 1] + rates[n // 2]) / 2
        latest_pt = max(points, key=lambda p: p.get("date") or "")
        latest = latest_pt["rate"]
        pct_vs_median = round(100.0 * (latest - median) / median, 1) if median else 0.0
        per_lane.append({
            "carrier": carrier,
            "destination": dest,
            "median": round(median, 2),
            "n_quotes": n,
            "latest": round(latest, 2),
            "latest_pct_vs_median": pct_vs_median,
            "min": min(rates),
            "max": max(rates),
        })

    expensive = sorted(
        [p for p in per_lane if p["latest_pct_vs_median"] >= 10],
        key=lambda x: -x["latest_pct_vs_median"],
    )[:5]
    cheap = sorted(
        [p for p in per_lane if p["latest_pct_vs_median"] <= -10],
        key=lambda x: x["latest_pct_vs_median"],
    )[:5]

    return {
        "per_lane": sorted(per_lane, key=lambda x: -x["n_quotes"]),
        "expensive": expensive,
        "cheap": cheap,
    }


# ─────────────────────────────────────────────────────────────────────
# Sparklines — unicode blocks for email-client-safe trend rendering
# ─────────────────────────────────────────────────────────────────────


_SPARK_CHARS = "▁▂▃▄▅▆▇█"


def status_as_of(row: dict, as_of: date) -> str | None:
    """Compute a row's status as of the given date by walking
    ``status_history``. Returns None if the row didn't exist yet
    (request_date > as_of) — caller filters these out before aggregating.

    Status is derived as follows:
      * If request_date > as_of: row did not exist yet → None.
      * If row has no status_history: use the row's current ``status``
        as the as-of value (closest available signal).
      * Else: walk history entries with at <= as_of; the latest such
        entry's ``to`` is the as-of status. If no history entry is on
        or before as_of, the row's pre-history status was PENDING (the
        default for fresh Lonny outbound).
    """
    rd = row.get("request_date")
    if rd:
        try:
            rd_date = date.fromisoformat(rd[:10])
            if rd_date > as_of:
                return None
        except ValueError:
            pass

    history = row.get("status_history") or []
    if not history:
        return row.get("status")

    cutoff_iso = as_of.isoformat() + "T23:59:59+00:00"
    latest_to: str | None = None
    for h in history:
        at = h.get("at") or ""
        if at and at <= cutoff_iso:
            latest_to = h.get("to") or latest_to
    if latest_to is not None:
        return latest_to
    # Pre-history default. Original Reading-A/B classifier outputs
    # PENDING for a fresh quoted row before any transitions. NQ if no
    # response_timestamp by as_of.
    rt = row.get("response_timestamp")
    if rt and rt[:10] <= as_of.isoformat():
        return "PENDING"
    return "NQ"


def synthesize_snapshot_for_date(
    requests: list[dict],
    as_of: date,
    *,
    aggregate_fn=None,
) -> dict:
    """Build a synthetic daily snapshot for ``as_of`` by reconstructing
    each row's status as of that day. Used by the backfill path
    (:func:`backfill_daily_snapshots`) so WoW/MoM/YTD trends can show
    real numbers from day 1 instead of waiting 7-30 days for natural
    history to accumulate.

    Returns the same shape as ``persist_daily_snapshot``:
      ``{date, generated_at, row_state, summary}``.
    """
    aggregate_fn = aggregate_fn or aggregate_summary
    as_of_rows: list[dict] = []
    for r in requests:
        s = status_as_of(r, as_of)
        if s is None:
            continue
        # Build a SHALLOW clone with the as-of status so aggregate_summary
        # buckets correctly. Other fields stay as-current; for trend-line
        # metrics that's accurate enough — wins/losses/teu fields don't
        # change after status freezes.
        clone = {**r, "status": s}
        as_of_rows.append(clone)

    summary = aggregate_fn(as_of_rows) if as_of_rows else {}
    row_state = {
        r["request_id"]: {
            "status": r.get("status"),
            "quoted": bool(r.get("quoted")),
            "has_send": bool(r.get("has_send")),
            "carrier_won": r.get("carrier_won"),
            "mdolx_ref": r.get("mdolx_ref"),
            "response_timestamp": r.get("response_timestamp"),
        }
        for r in as_of_rows if r.get("request_id")
    }
    return {
        "date": as_of.isoformat(),
        "generated_at": now_utc().isoformat(),
        "row_state": row_state,
        "summary": summary,
        "_synthesized": True,  # marker — these came from backfill, not live
    }


def backfill_daily_snapshots(
    requests: list[dict],
    snapshots_dir: Path | str,
    *,
    start_date: date,
    end_date: date,
    overwrite: bool = False,
) -> int:
    """Write synthetic snapshots for every date in [start_date, end_date].

    Skips dates where a snapshot already exists unless ``overwrite=True``
    — protects today's REAL snapshot from being overwritten with a
    backfilled approximation. Returns the count of new files written.
    """
    snapshots_dir = Path(snapshots_dir)
    snapshots_dir.mkdir(parents=True, exist_ok=True)
    written = 0
    cur = start_date
    while cur <= end_date:
        out = snapshots_dir / f"{cur.isoformat()}.json"
        if out.exists() and not overwrite:
            cur += timedelta(days=1)
            continue
        snap = synthesize_snapshot_for_date(requests, cur)
        out.write_text(json.dumps(snap, indent=2, default=str), encoding="utf-8")
        written += 1
        cur += timedelta(days=1)
    return written


def compute_lane_activity_sparklines(
    requests: list[dict],
    *,
    days: int = 14,
    today: date | None = None,
) -> dict[str, dict]:
    """Per-lane daily-request sparklines for the last N days.

    For each lane in the dataset, return:
      ``{lane: {"sparkline_total": "▁▂...", "sparkline_wins": "...",
                "n_total": <int>, "n_wins": <int>}}``

    The "lane" key is the canonical ``Origin → Destination`` string used
    by :func:`aggregate_lanes`. Days with no activity render as ``▁``
    (low end of the scale) so the line stays continuous; an entirely
    empty lane returns an empty string.

    Computed directly from live tracking-data — request_timestamp /
    response_timestamp pin the request to its day exactly. No
    cross-referencing with daily_snapshots needed.
    """
    today = today or now_utc().date()
    days_axis = [today - timedelta(days=days - 1 - i) for i in range(days)]

    by_lane_total: dict[str, list[int]] = {}
    by_lane_wins: dict[str, list[int]] = {}
    for r in requests:
        dest = r.get("destination") or "Unknown"
        origin = r.get("origin") or "Oakland"
        lane = f"{origin} → {dest}"
        ts = (r.get("request_timestamp") or r.get("response_timestamp") or "")[:10]
        if not ts:
            continue
        try:
            d = date.fromisoformat(ts)
        except ValueError:
            continue
        if d not in days_axis:
            continue
        idx = days_axis.index(d)
        totals = by_lane_total.setdefault(lane, [0] * days)
        wins = by_lane_wins.setdefault(lane, [0] * days)
        totals[idx] += 1
        if r.get("status") == STATUS_WIN:
            wins[idx] += 1

    out: dict[str, dict] = {}
    for lane, totals in by_lane_total.items():
        wins = by_lane_wins.get(lane, [0] * days)
        out[lane] = {
            "sparkline_total": sparkline(totals, width=days),
            "sparkline_wins": sparkline(wins, width=days),
            "n_total": sum(totals),
            "n_wins": sum(wins),
            "days": days,
        }
    return out


def sparkline(values: list, *, width: int = 20) -> str:
    """Convert a numeric series to a unicode sparkline. Email-client
    safe (no JS, no images). None values render as ' ' so gaps show.
    Empty input → empty string."""
    series = [v for v in values if v is not None]
    if not series:
        return ""
    lo, hi = min(series), max(series)
    rng = (hi - lo) or 1.0
    out: list[str] = []
    for v in values[-width:]:
        if v is None:
            out.append(" ")
        else:
            idx = int(((v - lo) / rng) * (len(_SPARK_CHARS) - 1))
            idx = max(0, min(len(_SPARK_CHARS) - 1, idx))
            out.append(_SPARK_CHARS[idx])
    return "".join(out)
