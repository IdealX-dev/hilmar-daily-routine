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

#: Window before a QUOTED-but-not-booked PENDING row ages out to Q&L.
#: Per Michael 2026-06-04 (restated rule, "i've said this fifty times"):
#:   - Normal biz week: 24 wall-hours from OL response → LOSS if no reply.
#:   - Friday quote (or weekend): not LOSS until Tuesday 18:00 ET, applied
#:     via is_business_stale's weekend carve-out.
#: Mirrored in scripts/core.py — tests/test_core_parity.py + QC-040
#: enforce parity.
PENDING_WINDOW_HOURS = 24
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
}


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
    with open(path) as f:
        return json.load(f)


def load_data(path: Path | str) -> dict:
    with open(path) as f:
        return json.load(f)


def save_data(data: dict, path: Path | str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
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


def now_utc() -> datetime:
    return datetime.now(UTC)


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


def biz_hours_between(start: datetime | None, end: datetime | None) -> float | None:
    """
    Business-hours delta in ET (8:30–17:30 Mon-Fri), DST-safe.
    Returns None if inputs invalid or end <= start.
    """
    if not start or not end:
        return None
    start_et = to_et(start)
    end_et = to_et(end)
    if end_et <= start_et:
        return None

    total = 0.0
    cursor = start_et
    while cursor < end_et:
        day = cursor.date()
        biz_open = datetime.combine(day, BIZ_START, tzinfo=ET)
        biz_close = datetime.combine(day, BIZ_END, tzinfo=ET)

        if is_biz_day_et(cursor):
            window_start = max(cursor, biz_open)
            window_end = min(end_et, biz_close)
            if window_end > window_start:
                total += (window_end - window_start).total_seconds() / 3600.0

        # Advance to next day at 00:00 ET
        next_day = datetime.combine(day + timedelta(days=1), time(0, 0), tzinfo=ET)
        cursor = next_day

    return round(total, 2) if total > 0 else 0.0


def clock_hours_between(start: datetime | None, end: datetime | None) -> float | None:
    if not start or not end or end <= start:
        return None
    return round((end - start).total_seconds() / 3600.0, 2)


# ─────────────────────────────────────────────────────────────────────
# Container / TEU parsing
# ─────────────────────────────────────────────────────────────────────

_CONTAINER_RX = re.compile(
    r"(\d+)\s*[×x\-]?\s*(\d{2})['\u2019\s]*(HC|RF|DV|GP|RE|RH|FR|OT|NOR)?",
    re.IGNORECASE,
)


def parse_teu(containers: str | None) -> tuple[int, int]:
    """
    Parse a container string into (container_count, teu_total).
    Handles common patterns: "2×40'RF", "1x20'DV", "2-40' HC Reefers", "3×20'DV + 1×40'HC".
    20' = 1 TEU, 40' = 2 TEU.
    """
    if not containers or not isinstance(containers, str):
        return 0, 0
    total_count = 0
    total_teu = 0
    for match in _CONTAINER_RX.finditer(containers):
        qty = int(match.group(1))
        size = int(match.group(2))
        if size not in (20, 40, 45):  # guard against garbage like "2250F"
            continue
        teu_per = 2 if size >= 40 else 1
        total_count += qty
        total_teu += qty * teu_per
    return total_count, total_teu


# ─────────────────────────────────────────────────────────────────────
# Send detection (regex — not body.startswith)
# ─────────────────────────────────────────────────────────────────────

# Match "send" or "SEND" as the first meaningful word — not inside a quoted reply
# or inside a word like "Sending" / "sender".
SEND_RX = re.compile(
    r"""
    ^                       # start of body
    \s*                     # optional whitespace
    (?:                     # optional courtesy openers
        (?:hi|hey|hello)\W+
    )?
    \bsend\b                # the word "send" as a whole word
    [\s.!,\-—]*             # optional trailing punctuation
    (?:both|all|please|thanks|thank\s+you|it|this|that|the\s+quote)?
    \s*
    (?:\n|$|<)              # followed by newline, end, or tag
    """,
    re.IGNORECASE | re.VERBOSE,
)

# Phrases that LOOK like "send" but mean something else
NOT_SEND_HINTS = re.compile(
    r"\b(send\s+both\s+cutoffs?|send\s+rates?|send\s+pricing|sending|sender|resend)\b",
    re.IGNORECASE,
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


def decide_status(
    *,
    has_send: bool,
    mdolx_ref: str | None,
    response_timestamp: str | None,
    quoted: bool,
    etd_fit_days: int | None,
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
      else within 48h biz window          → PENDING
      else etd_fit_days ≥ 5               → Q&L ETD_MISS
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
        if is_business_stale(send_at, now):
            return StatusDecision(
                STATUS_Q_AND_L, True, False, "SEND_NO_BOOKING",
                "Send received but no MDOLX within the 48h (biz-hours) cutoff — "
                "booking never confirmed (real wins confirm same/next business day)"
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

    # Truly silent — OL/MBD never responded.
    if not response_timestamp:
        return StatusDecision(STATUS_NQ, False, False, "NO_RESPONSE",
                              "OL-USA never responded with a quote")

    # Edge: response landed but no rate was extracted (e.g. MBD said
    # "checking with carrier..." without quoting). Tracked as NQ but
    # flagged separately so we don't conflate with true silence.
    if not quoted:
        return StatusDecision(STATUS_NQ, False, False, "RESPONSE_NO_RATE",
                              "MBD responded but no rate extracted — see reason_detail")

    # Quoted — check aging.
    resp_dt = parse_iso(response_timestamp)
    if not resp_dt:
        # Malformed timestamp — treat as past window.
        return StatusDecision(STATUS_Q_AND_L, True, False, "OTHER",
                              "Quoted but response_timestamp unparseable — assumed aged")

    hours_since = (now - resp_dt).total_seconds() / 3600.0
    # Weekend-aware check — a Friday quote doesn't flip to Q&L over the
    # weekend before Lonny's Monday workday (is_business_stale handles
    # the Fri/Sat/Sun → Monday 18:00 ET carve-out).
    if not is_business_stale(resp_dt, now, hours=PENDING_WINDOW_HOURS):
        return StatusDecision(STATUS_PENDING, True, False, None,
                              f"Quoted {hours_since:.1f}h ago — Lonny still within "
                              f"{PENDING_WINDOW_HOURS}h biz window (weekend-aware)")

    # Quoted & Lost. Tag the reason as best we can.
    base = f"Quoted {hours_since:.1f}h ago, no Send — Q&L"

    # ETD miss wins first — it's a concrete signal regardless of price.
    if etd_fit_days is not None and etd_fit_days >= 5:
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


# ─────────────────────────────────────────────────────────────────────
# Summary / lane / carrier aggregation
# ─────────────────────────────────────────────────────────────────────

def _sum(iterable: Iterable[int]) -> int:
    return sum(x or 0 for x in iterable)


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

    ta_entries = [r for r in requests if (r.get("turnaround_biz_hours") or 0) > 0]
    avg_biz = round(sum(r["turnaround_biz_hours"] for r in ta_entries) / len(ta_entries), 2) if ta_entries else 0.0
    avg_clock = round(
        sum(r["turnaround_hours"] for r in requests if (r.get("turnaround_hours") or 0) > 0)
        / max(1, sum(1 for r in requests if (r.get("turnaround_hours") or 0) > 0)),
        2,
    ) if any((r.get("turnaround_hours") or 0) > 0 for r in requests) else 0.0

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

            if r.get("turnaround_biz_hours") and r["turnaround_biz_hours"] > 0:
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
