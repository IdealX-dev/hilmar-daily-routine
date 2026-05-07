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
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
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

PENDING_WINDOW_HOURS = 24
RATE_TREND_THRESHOLD_PCT = 10

VALID_STATUSES = {"WIN", "LOSS", "PENDING"}
# COVERED    = lost to a competitor (Lonny replied "covered")
# DRAFT_ONLY = MDOLX has only a DRAFT RATED / Move updated email — no booking
#              confirmation in stage. Reclassified from WIN since no carrier
#              was ever attached. Added 2026-05-07 for stand_260469.
# OTHER      = catch-all when nothing else fits — should be near-zero
LOSS_REASONS = {"NO_RESPONSE", "PRICE", "ETD_MISS", "COVERED", "DRAFT_ONLY", "OTHER"}

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
    # Australia / NZ
    "sydney": "Oceania", "melbourne": "Oceania", "brisbane": "Oceania",
    "fremantle": "Oceania", "auckland": "Oceania",
    # Europe
    "hamburg": "Europe", "rotterdam": "Europe", "antwerp": "Europe",
    "felixstowe": "Europe", "le havre": "Europe", "algeciras": "Europe",
    "valencia": "Europe", "genoa": "Europe", "barcelona": "Europe",
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
    # North America inland (rare — usually a typo or a US-side movement
    # tracked in the same data file. Sturgis MI surfaced 2026-05-07).
    "sturgis mi": "North America", "sturgis": "North America",
}


def trade_region_for(destination: str | None) -> str:
    """Map a destination name to a trade region. Returns 'Unmapped' (NOT 'OTHER')
    for anything not in the map — Unmapped is the signal to extend the map."""
    if not destination:
        return "Unmapped"
    key = destination.strip().lower()
    if key in _TRADE_REGION_MAP:
        return _TRADE_REGION_MAP[key]
    # Try first token (handles "HCMC (Cat Lai)", "HCMC (Cat Lai Port)", etc.)
    head = key.split("(")[0].strip()
    if head in _TRADE_REGION_MAP:
        return _TRADE_REGION_MAP[head]
    return "Unmapped"


def aggregate_trade_regions(requests: list[dict]) -> dict[str, dict]:
    """Roll requests up by trade region. Counts must reconcile to summary."""
    out: dict[str, dict] = {}
    for r in requests:
        region = trade_region_for(r.get("destination"))
        m = out.setdefault(region, {
            "region": region,
            "requests": 0, "wins": 0, "quoted_lost": 0, "not_quoted": 0, "pending": 0,
            "teu_requested": 0, "teu_won": 0, "teu_quoted_lost": 0, "teu_not_quoted": 0,
            "destinations": set(),
        })
        m["requests"] += 1
        teu = r.get("teu_requested") or 0
        m["teu_requested"] += teu
        m["destinations"].add(r.get("destination") or "Unknown")
        st = r.get("status")
        lr = r.get("loss_reason") or ""
        if st == "WIN":
            m["wins"] += 1
            m["teu_won"] += r.get("teu_won") or teu
        elif st == "LOSS" and lr == "NO_RESPONSE":
            m["not_quoted"] += 1
            m["teu_not_quoted"] += teu
        elif st == "LOSS":
            m["quoted_lost"] += 1
            m["teu_quoted_lost"] += teu
        elif st == "PENDING":
            m["pending"] += 1
    for m in out.values():
        m["destinations"] = sorted(m["destinations"])
        decided = m["wins"] + m["quoted_lost"] + m["not_quoted"]
        m["win_rate"] = round(m["wins"] / decided * 100, 1) if decided else 0.0
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
    with open(path, "r") as f:
        cfg = json.load(f)
    return _heal_session_paths(cfg, path)


def load_data(path: Path | str) -> dict:
    with open(path, "r") as f:
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

@dataclass
class StatusDecision:
    status: str                    # WIN / LOSS / PENDING
    quoted: bool
    has_send: bool
    loss_reason: str | None        # NO_RESPONSE / PRICE / ETD_MISS / OTHER / None
    reason_detail: str             # human-readable why


def decide_status(
    *,
    has_send: bool,
    mdolx_ref: str | None,
    response_timestamp: str | None,
    quoted: bool,
    etd_fit_days: int | None,
    now: datetime | None = None,
) -> StatusDecision:
    """
    Pure classification. Inputs are the minimum facts needed to make a call.
    Called by the processor on ingestion AND by QC to re-age pending entries.
    """
    now = now or now_utc()

    # WIN takes precedence — either accepted or booked
    if has_send or (mdolx_ref and mdolx_ref.strip()):
        return StatusDecision("WIN", True, True, None, "Lonny replied Send or MDOLX booking found")

    # No response at all
    if not quoted or not response_timestamp:
        return StatusDecision("LOSS", False, False, "NO_RESPONSE", "OL-USA never responded with a quote")

    # Quoted — check aging
    resp_dt = parse_iso(response_timestamp)
    if not resp_dt:
        # Malformed timestamp — treat as old enough
        return StatusDecision("LOSS", True, False, "OTHER", "Quoted but response_timestamp unparseable — assumed aged")

    hours_since = (now - resp_dt).total_seconds() / 3600.0
    if hours_since <= PENDING_WINDOW_HOURS:
        return StatusDecision("PENDING", True, False, None,
                              f"Quoted {hours_since:.1f}h ago — Lonny still within 24h window")

    # Quoted & Lost. Try to tag a reason.
    reason = "OTHER"
    detail = f"Quoted {hours_since:.1f}h ago, no Send — Quoted & Lost"
    if etd_fit_days is not None:
        if etd_fit_days >= 10:
            reason = "ETD_MISS"
            detail += f" (ETD missed Lonny's ask by {etd_fit_days}d)"
        elif etd_fit_days >= 5:
            reason = "ETD_MISS"
            detail += f" (ETD missed Lonny's ask by {etd_fit_days}d)"
        else:
            reason = "PRICE"
            detail += " (ETD fit OK → likely rate-driven)"
    else:
        reason = "PRICE"  # default fallback when ETDs were both present but ok-ish

    return StatusDecision("LOSS", True, False, reason, detail)


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
    wins = [r for r in requests if r.get("status") == "WIN"]
    losses = [r for r in requests if r.get("status") == "LOSS"]
    ql = [r for r in losses if r.get("quoted")]
    nq = [r for r in losses if not r.get("quoted")]
    pending = [r for r in requests if r.get("status") == "PENDING"]

    total_decided = len(wins) + len(ql) + len(nq)
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
        "win_rate": round(len(wins) / total_decided * 100, 1) if total_decided else 0.0,
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
        if s == "WIN":
            lm["wins"] += 1
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
            cm["quotes"] += 1
            cm["_lanes"].add(r.get("destination", "Unknown"))

            if r.get("turnaround_biz_hours") and r["turnaround_biz_hours"] > 0:
                cm["_turnaround_samples"].append(r["turnaround_biz_hours"])
            if r.get("etd_fit_days") is not None:
                cm["_etd_fit_samples"].append(r["etd_fit_days"])

            if r.get("status") == "WIN" and r.get("carrier_won") == c:
                cm["wins"] += 1
                cm["teu_won"] += r.get("teu_won", 0) or r.get("teu_requested", 0) or 0
            elif r.get("status") == "LOSS" and r.get("quoted") and r.get("carrier_quoted") == c:
                cm["losses"] += 1
                cm["teu_lost"] += r.get("teu_requested", 0) or 0
            elif r.get("status") == "PENDING" and r.get("carrier_quoted") == c:
                cm["pending"] += 1

    for c, cm in car.items():
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
        if r.get("response_timestamp") and (not prior or r.get("response_timestamp") != prior.get("response_timestamp", None)):
            if r.get("quoted"):
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
    if candidates:
        candidates.sort(key=lambda x: x[0])
        return candidates[0][1]
    bm = _BARE_OL_EMAIL_RX.search(top)
    if bm:
        name = _name_from_email(bm.group(1), bm.group(2))
        if name:
            return name
    return None
