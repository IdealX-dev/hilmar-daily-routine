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

#: PENDING_HILMAR quote-decision window (Michael 2026-07-14, supersedes the
#: 2026-06-04 "Tuesday 18:00 ET" carve-out FOR QUOTED ROWS): a quote awaiting
#: Lonny's decision is Quoted & Lost after 48 CLOCK hours — 72 if OL quoted on
#: a FRIDAY (ET), to carry the weekend so a Friday quote lands Monday, not
#: Sunday. Measured from the OL quote (response_timestamp). This changed the
#: prior behavior on two counts: 24→48h normal, and Friday no longer runs to
#: Tuesday 6 PM (that left 73–78h Friday quotes stuck PENDING). The SEND-signal
#: aging (is_business_stale) is deliberately unchanged.
PENDING_HILMAR_LOSS_HOURS = 24
PENDING_HILMAR_LOSS_HOURS_FRIDAY = 72
#: PENDING-OL window — how long OL-USA has to answer Lonny's RFQ before the
#: row is called a genuine non-response (NQ). Symmetric with the Hilmar side
#: (PENDING_HILMAR_LOSS_HOURS): 48 CLOCK hours from Lonny's REQUEST, 72 when
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
    key = destination.strip().lower()
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
        m["requests"] += 1
        teu = r.get("teu_requested") or 0
        m["teu_requested"] += teu
        m["destinations"].add(r.get("destination") or "Unknown")
        st = r.get("status")
        if st == "WIN":
            m["wins"] += 1
            m["teu_won"] += r.get("teu_won") or teu
        # SAME predicate as aggregate_summary — is_not_quoted, not a
        # loss_reason test. This branch used `loss_reason == "NO_RESPONSE"`,
        # so a RESPONSE_NO_RATE row (OL acked the RFQ but sent no rate;
        # quoted=False) was counted Q&L here while aggregate_summary counted
        # it NQ. That is the "Volume by Trade Region: NQ 0 / Q&L 1" line
        # printed directly beneath the words "reconciles to summary" — the
        # divergence QC-075 now escalates instead of printing.
        elif is_not_quoted(r):
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


def is_win(r: dict) -> bool:
    """True if row is a win. WIN is spelled the same in both storage forms —
    this exists so a renderer can classify a row entirely through these
    helpers, with no `status ==` literal left to pick the wrong vocabulary.
    A bucketing loop that reads is_win/is_pending/is_quoted_and_lost/
    is_not_quoted is obviously total; one that reads WIN/PENDING/LOSS silently
    drops every STRICT row on the floor.
    """
    return (r or {}).get("status") == "WIN"


def is_pending(r: dict) -> bool:
    """True if row is still pending. Same spelling in both forms — see is_win."""
    return (r or {}).get("status") == "PENDING"


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


def pending_hilmar_stale(resp_dt: datetime | None, now: datetime | None = None) -> bool:
    """True when a QUOTED PENDING-Hilmar row has aged out to Quoted & Lost.

    Michael 2026-07-14: "pending hilmar is 48 hours most, then it's lost if we
    don't win.. except fridays, it's 72 hours." Pure CLOCK hours from the OL
    quote (response_timestamp): >= 48h → Q&L, or >= 72h when OL quoted on a
    Friday (ET) so the weekend lands Lonny on Monday. Distinct from the SEND-
    signal aging (is_business_stale), which is unchanged.

    Kept byte-for-byte identical to src/hilmar/core.pending_hilmar_stale —
    tests/test_core_parity.py fails if they drift.
    """
    if resp_dt is None:
        return False
    now = now or now_utc()
    resp_et = resp_dt.astimezone(ET)
    deadline = (PENDING_HILMAR_LOSS_HOURS_FRIDAY if resp_et.weekday() == 4
                else PENDING_HILMAR_LOSS_HOURS)
    return (now - resp_dt).total_seconds() / 3600.0 >= deadline


def pending_ol_stale(request_dt, now=None) -> bool:
    """True when an UNQUOTED row has waited long enough on OL that it counts
    as a genuine non-response (NQ) rather than an open request (PENDING_OL).

    Anchored on Lonny's REQUEST time (there is no response yet, by
    definition). Pure CLOCK hours, mirroring pending_hilmar_stale: >= 48h, or
    >= 72h when Lonny asked on a Friday (ET) so the weekend lands OL on Monday.

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
    real wins confirm within ~48h biz) it demotes to LOSS(SEND_NO_BOOKING),
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
    has_mdolx = bool(mdolx_ref and str(mdolx_ref).strip())

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
                "Send received but no MDOLX within the 48h (biz-hours) cutoff — "
                "booking never confirmed (real wins confirm same/next business day)")
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
            return StatusDecision("LOSS", False, False, "NO_RESPONSE", "OL-USA never responded with a quote")
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
            "is past the decision window")

    hours_since = (now - resp_dt).total_seconds() / 3600.0
    # PENDING-Hilmar quote-decision window (Michael 2026-07-14): 48 CLOCK
    # hours from the OL quote → Q&L, 72 when OL quoted on a Friday (ET) so the
    # weekend lands Lonny on Monday, not Sunday. Supersedes the prior
    # 24h-biz + Tuesday-18:00 carve-out (which left 73–78h Friday quotes stuck
    # PENDING). SEND-signal aging above still uses is_business_stale.
    if not pending_hilmar_stale(resp_dt, now):
        _win = PENDING_HILMAR_LOSS_HOURS_FRIDAY if resp_dt.astimezone(ET).weekday() == 4 else PENDING_HILMAR_LOSS_HOURS
        return StatusDecision("PENDING", True, False, None,
                              f"Quoted {hours_since:.1f}h ago — Lonny still within "
                              f"the {_win}h decision window")

    # Quoted & Lost. Try to tag a reason.
    detail = f"Quoted {hours_since:.1f}h ago, no Send — Quoted & Lost"

    # ETD-miss wins first — a missed ETD is a concrete signal regardless
    # of price competitiveness.
    if etd_fit_days is not None and etd_fit_days >= 5:
        reason = "ETD_MISS"
        detail += f" (ETD missed Lonny's ask by {etd_fit_days}d)"
        return StatusDecision("LOSS", True, False, reason, detail)

    # Otherwise, did OL's rate actually clear above the winning lane
    # median? Only call PRICE when we have a real rate gap.
    rate_val = parse_rate(ol_rate) if isinstance(ol_rate, str) else (
        float(ol_rate) if isinstance(ol_rate, (int, float)) else None
    )
    lane_med = None
    if lane_winning_median and lane:
        lane_med = lane_winning_median.get(lane)
    if rate_val is not None and lane_med and lane_med > 0:
        if rate_val > lane_med * PRICE_GAP_THRESHOLD_MULT:
            reason = "PRICE"
            gap_pct = (rate_val - lane_med) / lane_med * 100.0
            detail += (
                f" (rate ${rate_val:.0f} is {gap_pct:.0f}% above lane "
                f"winning median ${lane_med:.0f} → rate-driven)"
            )
            return StatusDecision("LOSS", True, False, reason, detail)
        # Rate was at/below winning median — not a price story.
        reason = "UNDIFFERENTIATED"
        detail += (
            f" (rate ${rate_val:.0f} ≤ lane winning median "
            f"${lane_med:.0f} — competitive on price, root cause unclear)"
        )
        return StatusDecision("LOSS", True, False, reason, detail)

    # No signal to determine PRICE — be honest about the gap.
    reason = "UNDIFFERENTIATED"
    if rate_val is None:
        detail += " (no ol_rate to compare against lane winning median)"
    elif lane_med is None:
        detail += " (no lane winning history to benchmark against)"
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
