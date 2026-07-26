#!/usr/bin/env python3
"""
Hilmar Tracker — self-healing QC engine (port of ../scripts/qc_selfheal.py).

Runs BEFORE and AFTER every daily processing cycle. Validates the data file,
auto-heals what it can, flags what it can't.

Safety:
  - Creates a timestamped backup BEFORE any mutation.
  - Idempotent — safe to run twice in a row.
  - Non-blocking — writes a qc-result.json and exits 0 even with errors;
    the orchestrator decides what to do.

Behavior parity with the original Cowork-mode script. The only differences are:
  - Paths come from env vars (via :mod:`hilmar.paths`) instead of config.json.
  - Imports from the installed ``hilmar`` package instead of relative scripts/.
  - ``run_qc()`` is exposed as a library entry point for tests + orchestrator.

Usage:
    hilmar-qc [--data PATH] [--no-backup] [--retention N]
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from collections import Counter
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from hilmar import body_parser, core, paths

# ─────────────────────────────────────────────────────────────────────
# Field constants — the contamination/clear lists used in Phase 3.
# Pinned to schema.json by an invariant test (test_qc.py) so a typo
# (like the historical "vessel_offered") fails CI before merge.
# ─────────────────────────────────────────────────────────────────────

#: Fields wiped on rows whose ``status`` is NQ (not-quoted). These are
#: rate-quote-stage fields that should never appear on a non-quoted row.
NQ_CONTAMINATION_FIELDS = (
    "carrier_quoted",
    "vessel_voyage",
    "etd_offered",
    "eta_offered",
    "transshipment",
)

#: Fields wiped on rows whose ``status`` is not WIN. ``carrier_won`` is
#: the booking-confirmed carrier; it must not appear on Q&L / PENDING /
#: NQ rows because no booking was confirmed there. ``awarded_carrier``
#: is its alias (Phase 4.5 mirrors carrier_won → awarded_carrier).
NON_WIN_CARRIER_FIELDS = (
    "carrier_won",
    "awarded_carrier",
)

# ─────────────────────────────────────────────────────────────────────
# Poisoned-placeholder healing (2026-07-14, run 29292014093 root cause).
# Mirror of scripts/qc_selfheal.py's entry-heal (QC-040 spirit — the paired
# phase_3_entries must not drift). A row persisted before the
# pdf_parser._clean_port source-fix can carry the LITERAL string "Unknown" (or
# other placeholder junk) in a lane-defining field — pod / destination /
# origin. Left as a string it (a) DISPLAYS as a real value in the staff AND
# client emails, (b) defeats the POD→destination recovery (a truthy "Unknown"
# pod looks resolved), and (c) lands the row in the "Unmapped" trade region.
# Coercing it to None at entry-heal time — BEFORE lane derivation — kills all
# three at the source and stops the drift re-deriving unresolved every fire.
_GARBAGE_PLACEHOLDERS = frozenset({
    "unknown", "n/a", "na", "none", "null", "tbd", "-", "—", "",
})
#: Lane-defining fields swept for the poisoned placeholder above.
_PLACEHOLDER_FIELDS = ("pod", "destination", "origin")


def _is_placeholder(v) -> bool:
    """True when `v` is a garbage placeholder (case-insensitive) that must be
    coerced to None rather than treated as a real port/lane value. Non-strings
    (None, ints) are NOT placeholders here — None is already the target
    state, and a numeric field is out of scope for this heal."""
    return isinstance(v, str) and v.strip().lower() in _GARBAGE_PLACEHOLDERS

# ─────────────────────────────────────────────────────────────────────
# Backup
# ─────────────────────────────────────────────────────────────────────

def rotate_backup(data_path: Path, backups_dir: Path, keep: int = 14) -> Path:
    backups_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(core.ET).strftime("%Y-%m-%d-%H%M")
    dest = backups_dir / f"tracking-data-v2.{ts}.json"
    shutil.copy2(data_path, dest)
    # Rotate: keep newest N. Defensive against session-mount constraints
    # where files created in a prior session can fail to unlink even when
    # owned by current user; in that case, rename with .stale- prefix so
    # the next rotation pass doesn't recount them.
    existing = sorted(backups_dir.glob("tracking-data-v2.*.json"))
    while len(existing) > keep:
        victim = existing[0]
        try:
            victim.unlink()
        except (PermissionError, OSError) as e:
            try:
                victim.rename(backups_dir / f".stale-{victim.name}")
                print(f"  ⚠️  rotate_backup: unlink blocked on {victim.name} ({e}); renamed to .stale-")
            except Exception as e2:
                print(f"  ⚠️  rotate_backup: could not remove or rename {victim.name} ({e2}); skipping")
        existing = existing[1:]
    return dest


# ─────────────────────────────────────────────────────────────────────
# Log helpers
# ─────────────────────────────────────────────────────────────────────

class Log:
    def __init__(self):
        self.fixes: list[str] = []
        self.warnings: list[str] = []
        self.errors: list[str] = []

    def fix(self, msg: str) -> None:
        self.fixes.append(msg)
        print(f"  🔧 FIX: {msg}")

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)
        print(f"  ⚠️  WARN: {msg}")

    def error(self, msg: str) -> None:
        self.errors.append(msg)
        print(f"  🔴 ERROR: {msg}")

    def ok(self, msg: str) -> None:
        print(f"  ✅ {msg}")

    def section(self, title: str) -> None:
        print("\n" + "═" * 60)
        print(title)
        print("═" * 60)


# ─────────────────────────────────────────────────────────────────────
# Phase 1 — file health
# ─────────────────────────────────────────────────────────────────────

def phase_1_files(log: Log, data_path: Path, schema_path: Path) -> None:
    log.section("PHASE 1: FILE HEALTH")
    for label, p in [("Data JSON", data_path), ("Schema", schema_path)]:
        if not p.exists():
            log.error(f"{label} MISSING at {p}")
            continue
        size = p.stat().st_size
        if size < 50:
            log.error(f"{label} exists but suspiciously small ({size} bytes)")
        else:
            log.ok(f"{label} present ({size:,} bytes)")


# ─────────────────────────────────────────────────────────────────────
# Phase 2 — schema + structure
# ─────────────────────────────────────────────────────────────────────

def phase_2_structure(log: Log, data: dict) -> bool:
    log.section("PHASE 2: STRUCTURE")
    # Post-Phase-A: ``summary`` is no longer required pre-QC. Ingest writes
    # raw shape only (`requests` + immutable metadata); ``phase_5_summaries``
    # is the sole writer of derived aggregates. ``requests`` and ``version``
    # are the only genuinely-required keys at any phase boundary.
    for key in ("requests", "version"):
        if key not in data:
            log.error(f"Missing top-level key: '{key}'")
            if key == "requests":
                return False
        else:
            log.ok(f"Top-level key '{key}' present")
    if not isinstance(data["requests"], list):
        log.error("'requests' is not an array")
        return False
    log.ok(f"{len(data['requests'])} requests")
    return True


# ─────────────────────────────────────────────────────────────────────
# Phase 3 — entry-level healing
# ─────────────────────────────────────────────────────────────────────

def phase_3_entries(log: Log, data: dict) -> None:
    log.section("PHASE 3: ENTRY-LEVEL HEALING")
    # Top-level legacy field migration: 'data_range' was a long-running
    # typo for 'date_range' (semantically a date range, not a data
    # range). Code was renamed 2026-04-29; this heal migrates persisted
    # data on the first run after deploy. Idempotent — once migrated,
    # the legacy key is gone.
    if "data_range" in data and "date_range" not in data:
        data["date_range"] = data.pop("data_range")
        log.fix("Migrated legacy top-level 'data_range' -> 'date_range'")
    elif "data_range" in data and "date_range" in data:
        # Both present — keep the canonical, drop the legacy.
        data.pop("data_range")
        log.fix("Dropped duplicate legacy top-level 'data_range' (kept 'date_range')")

    requests = data["requests"]

    # Compute lane winning medians ONCE before the per-row decide loop.
    # decide_status uses this to determine PRICE vs UNDIFFERENTIATED on
    # Q&L rows — see core.decide_status docstring (2026-06-02 rewrite).
    # Computed BEFORE the loop because each row's decision is per-row pure
    # but needs book-wide WIN-rate context for the gap calc.
    _lane_winning_median = core.compute_lane_winning_medians(requests)

    for i, r in enumerate(requests):
        rid_label = f"[{i}] {r.get('request_date') or r.get('date','?')} {r.get('destination','?')}"
        # HEAL poisoned placeholder literals in lane-defining fields BEFORE any
        # lane derivation (2026-07-14, run 29292014093). A persisted
        # "Unknown"/"N/A"/… in pod/destination/origin becomes None so it can
        # never display, defeat POD→destination recovery, or bucket the row as
        # "Unmapped". Mirror of scripts/qc_selfheal.py phase_3_entries.
        for _pf in _PLACEHOLDER_FIELDS:
            if _is_placeholder(r.get(_pf)):
                _bad = r.get(_pf)
                r[_pf] = None
                log.fix(f"{r.get('request_id') or rid_label}: cleaned poisoned "
                        f"placeholder {_pf}={_bad!r} → None (garbage literal, "
                        f"pre lane-derivation)")

        if not r.get("request_id"):
            r["request_id"] = core.request_id(
                r.get("conversationId"),
                r.get("request_timestamp"),
                r.get("destination"),
            )
            log.fix(f"{rid_label}: Assigned request_id={r['request_id']}")

        if not r.get("request_date"):
            if r.get("date"):
                r["request_date"] = r["date"]
                log.fix(f"{rid_label}: request_date copied from legacy 'date'")
            elif r.get("request_timestamp"):
                dt = core.parse_iso(r["request_timestamp"])
                if dt:
                    r["request_date"] = core.to_pt(dt).strftime("%Y-%m-%d")
                    log.fix(f"{rid_label}: Derived request_date from request_timestamp")
        if r.get("request_date") and not r.get("date"):
            r["date"] = r["request_date"]

        # Heal garbage `containers` strings from older runs. Pre-fix
        # ingest fell back to the entire email preview when it couldn't
        # parse a clean container spec — leaking CAUTION/EXTERNAL banners
        # and raw body text into the dashboard column. Detect that
        # pattern (long string with no n-x-size match) and clear it.
        existing = (r.get("containers") or "").strip()
        if existing and (
            len(existing) > 60
            or not re.search(r"\d+\s*[-x×]\s*\d{2}", existing)
        ):
            log.fix(f"{rid_label}: Cleared garbage containers={existing[:40]!r}…")
            r["containers"] = None
            existing = ""
        # Subject → containers recovery for standalone-WIN rows persisted
        # before ingest learned to mine the booking subject. The MDOLX
        # confirmation subject usually carries the spec verbatim
        # ("…HILMAR 1X20'DV Oakland to Bangkok…"); body_parser pulls it
        # out. Idempotent (already-populated rows are no-ops).
        if not existing and r.get("subject"):
            recovered = body_parser.parse_container_spec_from_subject(r["subject"])
            if recovered:
                r["containers"] = recovered
                existing = recovered
                log.fix(f"{rid_label}: Recovered containers={recovered!r} from subject")
        c_count, teu = core.parse_teu(existing)
        if (
            existing
            and (not r.get("teu_requested") or r["teu_requested"] == 0)
            and teu > 0
        ):
            r["teu_requested"] = teu
            r.setdefault("container_count", c_count)
            log.fix(f"{rid_label}: Recalculated teu_requested={teu}")
        if not r.get("container_count") and c_count:
            r["container_count"] = c_count
        # Standalone WIN rows persisted before subject-recovery existed
        # have teu_requested newly populated above but teu_won still 0
        # (link_bookings_to_requests no longer touches them — they're
        # outside the search window). Mirror it so the trade-region
        # TEU/value-won columns aggregate correctly.
        if (
            str(r.get("request_id", "")).startswith("stand_")
            and r.get("status") == "WIN"
            and (not r.get("teu_won") or r["teu_won"] == 0)
            and (r.get("teu_requested") or 0) > 0
        ):
            r["teu_won"] = r["teu_requested"]
            log.fix(f"{rid_label}: Filled teu_won={r['teu_won']} from teu_requested (standalone WIN)")

        if not r.get("lane") and r.get("origin") and r.get("destination"):
            r["lane"] = f"{r['origin']} → {r['destination']}"

        if "quoted" not in r:
            r["quoted"] = bool(
                r.get("response_timestamp") or r.get("carrier_quoted") or r.get("ol_rate")
            )
            log.fix(f"{rid_label}: Defaulted quoted={r['quoted']}")

        if "has_send" not in r:
            r["has_send"] = False

        if not isinstance(r.get("after_hours_request"), bool):
            req_dt = core.parse_iso(r.get("request_timestamp"))
            r["after_hours_request"] = core.is_after_hours_et(req_dt) if req_dt else False

        if isinstance(r.get("turnaround"), dict):
            ta = r.pop("turnaround")
            for src, dst in [
                ("hours", "turnaround_hours"),
                ("biz_hours", "turnaround_biz_hours"),
                ("response_timestamp", "response_timestamp"),
            ]:
                if src in ta and not r.get(dst):
                    r[dst] = ta[src]
            log.fix(f"{rid_label}: Flattened nested turnaround object")

        req_dt = core.parse_iso(r.get("request_timestamp"))
        resp_dt = core.parse_iso(r.get("response_timestamp"))
        if req_dt and resp_dt:
            if r.get("turnaround_hours") is None:
                r["turnaround_hours"] = core.clock_hours_between(req_dt, resp_dt)
            if r.get("turnaround_biz_hours") is None:
                r["turnaround_biz_hours"] = core.biz_hours_between(req_dt, resp_dt)
            if not r.get("lonny_time_pt"):
                r["lonny_time_pt"] = core.fmt_pt(req_dt, with_date=False)
            if not r.get("olusa_time_et"):
                r["olusa_time_et"] = core.fmt_et(resp_dt, with_date=False)

        if r.get("etd_fit_days") is None and r.get("quoted"):
            lonny_ask = r.get("eta_requested") or r.get("requested_dates") or r.get("cutoff_requested")
            ol_offer = r.get("eta_offered") or r.get("etd_offered")
            if lonny_ask and ol_offer:
                fit = core.etd_fit_days(lonny_ask, ol_offer)
                if fit is not None:
                    r["etd_fit_days"] = fit

        prior_status = r.get("status")
        decision = core.decide_status(
            has_send=r.get("has_send", False),
            mdolx_ref=r.get("mdolx_ref"),
            response_timestamp=r.get("response_timestamp"),
            quoted=r.get("quoted", False),
            etd_fit_days=r.get("etd_fit_days"),
            request_timestamp=r.get("request_timestamp") or r.get("request_date"),
            send_signal_events=r.get("send_signal_events"),
            mdolx_refs_all=r.get("mdolx_refs_all"),
            ol_rate=r.get("ol_rate"),
            lane=r.get("lane"),
            lane_winning_median=_lane_winning_median,
        )
        if prior_status != decision.status:
            core.record_transition(r, decision.status, decision.reason_detail)
            log.fix(f"{rid_label}: Status {prior_status} → {decision.status} ({decision.reason_detail})")
        else:
            r["status"] = decision.status
        r["has_send"] = decision.has_send
        r["loss_reason"] = decision.loss_reason

        # Heal stale ol_responder_signer that's actually the shared
        # mailbox label. Pre-fix the rejection set was an exact-match
        # against "MBD Ocean Export Booking" — so "MBD Ocean Export
        # Booking (Shared)" slipped through and showed up as the human
        # signer in the dashboard. Clear it; later runs re-extract
        # via body_parser + LLM fallback.
        from .ingest import _is_shared_mailbox_label as _is_shared
        if _is_shared(r.get("ol_responder_signer")):
            if r.get("ol_responder_signer"):
                log.fix(
                    f"{rid_label}: Cleared shared-mailbox signer "
                    f"{r['ol_responder_signer']!r}"
                )
            r["ol_responder_signer"] = None

        # Carrier name canonicalization sweep. body_parser._find_carrier
        # returns title-cased tokens like "Cma" / "Maersk" — without
        # normalization, a row with carrier_won="Cma" buckets separately
        # from a row with carrier_won="CMA CGM" in the carrier scoreboard
        # (cf. stand_260460 vs stand_260433 split in the 2026-04-27 audit).
        # core.normalize_carrier maps the alias family to a single canonical
        # form. Idempotent — already-canonical names pass through unchanged.
        for cf in ("carrier_won", "carrier_quoted"):
            raw = r.get(cf)
            if raw:
                canon = core.normalize_carrier(raw)
                if canon and canon != raw:
                    r[cf] = canon
                    log.fix(f"{rid_label}: {cf} canonicalized {raw!r} → {canon!r}")

        if r["status"] == core.STATUS_WIN:
            if not r.get("carrier_won"):
                if r.get("carrier_quoted"):
                    r["carrier_won"] = r["carrier_quoted"]
                    log.fix(f"{rid_label}: WIN carrier_won copied from carrier_quoted")
                else:
                    # Last-resort heal: token-extract from the subject only.
                    # Catches standalone-booking rows whose booking email was outside
                    # today's Graph search window — no fresh standalone got generated,
                    # so _RECOMPUTED_FIELDS had no fresh row to clobber the stale None
                    # with via merge. This backstop runs against the persisted row
                    # itself, not just at ingest time.
                    #
                    # Word-boundary regex (not body_parser._find_carrier's substring
                    # match): we MUST NOT match "ONE" inside "stANdAlONE" of the
                    # auto-generated reason_detail. We also intentionally limit the
                    # search to ``subject`` because that's where the carrier
                    # genuinely appears in booking confirmations (e.g. "CMA: NAM..."
                    # or "MSC BKG #...") — reason_detail is text we generated
                    # ourselves and is full of false-positive substrings.
                    subj_up = (r.get("subject") or "").upper()
                    cw_raw = None
                    for tok in body_parser._CARRIER_TOKENS:
                        if re.search(rf"\b{re.escape(tok.upper())}\b", subj_up):
                            cw_raw = tok if tok in ("MSC", "ONE", "HMM", "OOCL", "ZIM") else tok.title()
                            break
                    if cw_raw:
                        cw = core.normalize_carrier(cw_raw) or cw_raw
                        r["carrier_won"] = cw
                        if not r.get("carrier_quoted"):
                            r["carrier_quoted"] = cw
                        log.fix(f"{rid_label}: WIN carrier_won healed from subject token: {cw}")
                    else:
                        log.error(f"{rid_label}: WIN with no carrier_won")
            if not r.get("mdolx_ref") and not r.get("has_send"):
                log.warn(f"{rid_label}: WIN has no chain-send signal AND no MDOLX ref — verify booking")
            if not r.get("teu_won"):
                r["teu_won"] = r.get("teu_requested", 0) or 0
                log.fix(f"{rid_label}: WIN teu_won defaulted to teu_requested")
            r["quoted"] = True

        # NQ contamination cleanup. Post 2026-04-27 the NQ status is
        # mutually exclusive with quoted=True at the classifier level —
        # but we still defensively wipe rate/carrier fields here so that
        # a row whose `status` got demoted to NQ via a re-classifier
        # pass cannot ship into the dashboard with a stale carrier_quoted
        # bleeding in from the prior day.
        if r["status"] == core.STATUS_NQ:
            for k in NQ_CONTAMINATION_FIELDS:
                if r.get(k) and r[k] not in ("", None, "N/A"):
                    log.fix(f"{rid_label}: NQ contamination — cleared {k}={r[k]!r}")
                    r[k] = None
            # ol_rate must stay None on NQ rows — clean float|None
            # type contract. The "Not Quoted" display label is computed
            # at template time (see dashboard.html.j2) so storage stays
            # type-stable; this lets downstream consumers like
            # baselines.has_rate_body rely on `is not None` meaning
            # "we have a numeric rate". Pre-fix the heal stored the
            # string "Not Quoted" here, breaking that contract.
            if r.get("ol_rate") is not None:
                log.fix(f"{rid_label}: NQ contamination — cleared ol_rate={r['ol_rate']!r}")
                r["ol_rate"] = None

        # has_send is a WIN-only signal; never carry it on a non-WIN row.
        if r["status"] != core.STATUS_WIN and r.get("has_send"):
            r["has_send"] = False
            log.fix(f"{rid_label}: Cleared has_send on non-WIN status {r['status']}")

        # carrier_won / awarded_carrier are booking-confirmed signals;
        # never carry them on a non-WIN row. Audit on 2026-04-29 caught
        # 2 rows leaking these fields (req_d72835b5341716c7 Q&L w/ ONE,
        # req_47eda86d98477ca6 PENDING w/ CMA CGM) — neither had a
        # booking confirmation yet, so the "winner" labelling was
        # misleading on the dashboard.
        if r["status"] != core.STATUS_WIN:
            for k in NON_WIN_CARRIER_FIELDS:
                if r.get(k):
                    log.fix(f"{rid_label}: Cleared {k}={r[k]!r} on non-WIN status {r['status']}")
                    r[k] = None


# ─────────────────────────────────────────────────────────────────────
# Phase 4 — duplicate detection
# ─────────────────────────────────────────────────────────────────────

def phase_4_duplicates(log: Log, data: dict) -> None:
    log.section("PHASE 4: DUPLICATE DETECTION")
    requests = data["requests"]
    seen: Counter[str] = Counter()
    for r in requests:
        seen[r.get("request_id", "")] += 1
    dupes = {k: v for k, v in seen.items() if v > 1 and k}
    if not dupes:
        log.ok("No duplicate request_ids")
        return
    keepers: list[dict] = []
    by_id: dict[str, list[dict]] = {}
    for r in requests:
        by_id.setdefault(r.get("request_id", ""), []).append(r)
    for rid, group in by_id.items():
        if len(group) == 1:
            keepers.append(group[0])
            continue
        canonical = max(
            group,
            key=lambda r: sum(1 for v in r.values() if v not in (None, "", [])),
        )
        keepers.append(canonical)
        log.fix(f"Deduped request_id={rid} — kept richest, dropped {len(group)-1}")
    data["requests"] = keepers


# ─────────────────────────────────────────────────────────────────────
# Phase 4.5 — derived fields
# ─────────────────────────────────────────────────────────────────────


def phase_4_5_derived_fields(log: Log, data: dict) -> None:
    """Compute derived fields for analytics — equipment_size,
    rate_per_feu, trade_region, awarded_carrier, validity_window.

    Re-derived every run from canonical source fields (containers,
    ol_rate, destination, carrier_won, rate_expiry). No staleness
    risk — these fields aren't ingested directly, so the merge layer
    in :mod:`hilmar.ingest` doesn't need to worry about them.
    """
    log.section("PHASE 4.5: DERIVED FIELDS")
    requests = data["requests"]
    derived = 0
    for r in requests:
        r["equipment_size"] = core.equipment_size(r.get("containers"))
        r["rate_per_feu"] = core.parse_rate_per_feu(
            r.get("ol_rate"), r.get("containers"),
        )
        r["trade_region"] = core.trade_region(r.get("destination"))
        # awarded_carrier is the canonical "who got the booking" surface —
        # alias of carrier_won so analytics can reference either.
        r["awarded_carrier"] = r.get("carrier_won")
        # validity_window — prefer ingest's already-extracted rate_expiry,
        # else try a direct regex pass on the body if present.
        r["validity_window"] = (
            r.get("rate_expiry")
            or core.parse_validity_window(r.get("body") or r.get("response_body"))
        )
        derived += 1
    log.ok(f"Derived 5 fields x {derived} requests "
           "(equipment_size, rate_per_feu, trade_region, "
           "awarded_carrier, validity_window)")


# ─────────────────────────────────────────────────────────────────────
# Phase 5 — summaries
# ─────────────────────────────────────────────────────────────────────

def phase_5_summaries(log: Log, data: dict) -> None:
    """Phase A invariant: phase_5 is the SOLE writer of `summary`, `lanes`,
    and `carriers`. Ingest no longer pre-computes them — it writes raw
    `requests` only, and we derive aggregates here, after `phase_3_entries`
    has done its healing. One writer, one storage shape per aggregate
    (lists, which is what render reads), no drift surface.
    """
    log.section("PHASE 5: SUMMARY RECALCULATION")
    old_summary = data.get("summary") or {}
    computed = core.aggregate_summary(data["requests"])
    if "dod" in old_summary and "dod" not in computed:
        computed["dod"] = old_summary["dod"]
    drift = any(old_summary.get(k) != v for k, v in computed.items())
    data["summary"] = computed
    data["lanes"] = list(core.aggregate_lanes(data["requests"]).values())
    data["carriers"] = list(core.aggregate_carriers(data["requests"]).values())

    # One-time migration: clean up fields the pre-Phase-A pipeline
    # persisted that have no readers anywhere in src/. Idempotent —
    # `pop(key, None)` is a no-op once the field is gone.
    #   * lane_summary / carrier_summary: dict twins of lanes/carriers,
    #     phase_5 used to write both, render only ever read the lists.
    #   * data["qc"]: duplicate of qc-result.json — that file is the
    #     canonical surface QC consumers go to.
    #   * escalations_sent / metadata / mdolx_bookings: schema-only
    #     fields with no writer or reader anywhere; cruft from earlier
    #     scope.
    for dead in ("lane_summary", "carrier_summary", "qc",
                 "escalations_sent", "metadata", "mdolx_bookings"):
        if dead in data:
            data.pop(dead)
            log.fix(f"Dropped dead field data['{dead}'] (Phase A cleanup)")

    log.fix(
        "summary / lanes / carriers rebuilt from raw data"
        + (" (drift detected)" if drift else "")
    )


# ─────────────────────────────────────────────────────────────────────
# Phase 6 — cross-check rules
# ─────────────────────────────────────────────────────────────────────

def phase_6_rules(log: Log, data: dict) -> None:
    log.section("PHASE 6: CROSS-CHECK RULES")
    requests = data["requests"]
    # Post 2026-04-27 four-state classifier: bucket on `status` directly,
    # not status+quoted (the latter masked Bug 1 regressions for months).
    wins = [r for r in requests if r["status"] == core.STATUS_WIN]
    ql = [r for r in requests if r["status"] == core.STATUS_Q_AND_L]
    nq = [r for r in requests if r["status"] == core.STATUS_NQ]
    pending = [r for r in requests if r["status"] == core.STATUS_PENDING]

    if len(requests) > 10 and len(ql) == 0:
        log.warn(f"QC-001: 0 Quoted & Lost among {len(requests)} entries — verify")
    else:
        log.ok(f"QC-001: {len(ql)} Q&L — plausible")

    bad = [r for r in wins if not r.get("carrier_won")]
    if bad:
        log.error(f"QC-002: {len(bad)} WIN(s) with no carrier_won")
    else:
        log.ok("QC-002: All WINs have carrier_won")

    unverified = [r for r in wins if not r.get("mdolx_ref") and not r.get("has_send")]
    if unverified:
        log.warn(f"QC-003: {len(unverified)} WIN(s) have no chain-send AND no MDOLX — verify")
    else:
        log.ok("QC-003: All WINs have chain-send signal or MDOLX ref")

    contam = [r for r in nq if r.get("carrier_quoted") and r["carrier_quoted"] not in (None, "", "N/A")]
    if contam:
        log.error(f"QC-004: {len(contam)} NQ entries still contaminated")
    else:
        log.ok("QC-004: No NQ contamination")

    non_win = [r for r in requests if r["status"] != core.STATUS_WIN]
    cw_leaked = [
        r for r in non_win
        if any(r.get(f) for f in NON_WIN_CARRIER_FIELDS)
    ]
    if cw_leaked:
        offenders = ", ".join(r["request_id"] for r in cw_leaked[:5])
        log.error(
            f"QC-008: {len(cw_leaked)} non-WIN row(s) with carrier_won/awarded_carrier set "
            f"(should clear in Phase 3): {offenders}"
        )
    else:
        log.ok("QC-008: No carrier_won leak on non-WIN rows")

    for r in requests:
        bh = r.get("turnaround_biz_hours")
        if bh and bh > 100:
            log.warn(f"QC-005: {r['request_id']} biz-hrs {bh} — suspicious")

    for r in requests:
        t = r.get("teu_requested", 0)
        if t and t > 30:
            log.warn(f"QC-006: {r['request_id']} TEU={t} — verify large request")

    now = core.now_utc()
    for r in pending:
        # QC-007 was written for the original PENDING semantics — quoted
        # within Lonny's response window. Reading B (commit ee392d5)
        # introduced two new PENDING sub-states (AWAITING_MDOLX,
        # MDOLX_NO_SEND) where exceeding the window is EXPECTED, not an
        # error:
        #   AWAITING_MDOLX  — waiting for OL to generate MDOLX after
        #                     Lonny's send; can legitimately take days
        #   MDOLX_NO_SEND   — anomaly, separately surfaced; doesn't need
        #                     QC-007 to also fire on it
        # Skip both so QC-007 only fires on the original "quoted, within
        # window, but didn't age" failure mode it was designed to catch.
        if r.get("loss_reason") in ("AWAITING_MDOLX", "MDOLX_NO_SEND"):
            continue
        rt = core.parse_iso(r.get("response_timestamp"))
        # Must use is_business_stale + PENDING_WINDOW_HOURS so this check
        # stays aligned with decide_status. Hardcoded 24h drifted from the
        # classifier's 48h+Friday-rule on 2026-06-01 (PR #14 updated
        # decide_status; QC-007 was missed). Result: 2 Friday-quoted rows
        # fired QC-007 ERRORs even though decide_status correctly kept
        # them PENDING.
        if rt and core.is_business_stale(rt, now, hours=core.PENDING_WINDOW_HOURS):
            log.error(
                f"QC-007: {r['request_id']} still PENDING past "
                f"{core.PENDING_WINDOW_HOURS}h biz-window — state "
                f"machine should have aged this"
            )


# ─────────────────────────────────────────────────────────────────────
# Phase 7 — persist + QC result file
# ─────────────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────────────
# Phases 8/9/10 — Insights/Self-Heal extensions (M3.9)
# Each writes structured records to data["selfheal_actions"][] so the
# insights engine (M3.11) can narrate them downstream. None are
# destructive — Phase 10 mutates by adding `null` for missing keys
# only.
# ─────────────────────────────────────────────────────────────────────


def _selfheal_record(data: dict, kind: str, payload: dict[str, Any]) -> None:
    """Append one entry to ``data["selfheal_actions"]`` with timestamp.
    Idempotent — caller is expected to skip if no action was taken.
    """
    data.setdefault("selfheal_actions", []).append({
        "at": core.now_utc().isoformat(),
        "kind": kind,
        **payload,
    })


def phase_8_parser_regression(
    log: Log,
    data: dict,
    *,
    baseline_threshold: float = 2.0,
    today: datetime | None = None,
) -> None:
    """Phase 8 — Parser-regression detection.

    Scans the last 14 days of requests and computes a "miss rate" per
    parser (``rate_table``, ``eta_offered``, ``vessel_voyage``,
    ``transshipment``, ``mdolx_ref``) — % of rows where the parser
    output is missing among rows that *should* have produced one.

    Compares against ``data["baselines"]["parser_miss_rate"]`` (written
    by M3.10 :mod:`hilmar.baselines`). Flags any parser whose miss-rate
    is more than ``baseline_threshold`` × the baseline value.

    No mutation — only logs warnings + appends a ``selfheal_actions``
    record so the insights engine can narrate it.
    """
    log.section("PHASE 8: PARSER-REGRESSION DETECTION")

    today = today or core.now_utc()
    cutoff = today - timedelta(days=14)
    requests = data.get("requests") or []

    def in_window(r: dict) -> bool:
        ts = core.parse_iso(r.get("request_timestamp")) or core.parse_iso(r.get("response_timestamp"))
        return bool(ts and ts >= cutoff)

    recent = [r for r in requests if in_window(r)]
    if not recent:
        log.ok("Phase 8: no requests in last 14d — skipping parser-regression scan")
        return

    # Define each parser's "applicable" predicate + "miss" predicate.
    # eta_offered / vessel_voyage / transshipment live in the rate-quote
    # email body — gate them on `ol_rate is not None` so pure-MDOLX WINs
    # (synthesized from a booking with no quote email) aren't counted as
    # parser misses. They have `quoted=True` because the booking implies
    # a quote happened, but no body to parse.
    has_rate_body = lambda r: r.get("ol_rate") is not None  # noqa: E731
    parser_specs: dict[str, tuple[Callable[[dict], bool], Callable[[dict], bool]]] = {
        "rate_table": (lambda r: bool(r.get("quoted")),
                       lambda r: r.get("ol_rate") is None and r.get("carrier_quoted") is None),
        "eta_offered": (has_rate_body,
                        lambda r: not r.get("eta_offered")),
        "vessel_voyage": (has_rate_body,
                          lambda r: not r.get("vessel_voyage")),
        "transshipment": (has_rate_body,
                          lambda r: not r.get("transshipment")),
        "mdolx_ref": (lambda r: r.get("status") == "WIN",
                      lambda r: not r.get("mdolx_ref")),
    }

    baselines = ((data.get("baselines") or {}).get("parser_miss_rate") or {})
    miss_rates: dict[str, float] = {}
    flagged: list[dict[str, Any]] = []

    for parser_name, (applicable, missed) in parser_specs.items():
        applicable_rows = [r for r in recent if applicable(r)]
        if not applicable_rows:
            continue
        missed_rows = [r for r in applicable_rows if missed(r)]
        rate = round(100.0 * len(missed_rows) / len(applicable_rows), 1)
        miss_rates[parser_name] = rate
        baseline = baselines.get(parser_name)
        if baseline is not None and baseline > 0 and rate > baseline_threshold * baseline:
            flagged.append({
                "parser": parser_name,
                "miss_rate_today": rate,
                "miss_rate_baseline": baseline,
                "ratio": round(rate / baseline, 2),
                "applicable": len(applicable_rows),
                "missed": len(missed_rows),
            })

    if flagged:
        for f in flagged:
            log.warn(
                f"Parser regression: {f['parser']} miss-rate={f['miss_rate_today']}% "
                f"(baseline {f['miss_rate_baseline']}%, ratio {f['ratio']}×)",
            )
        _selfheal_record(data, "parser_regression", {
            "threshold": baseline_threshold, "flagged": flagged, "all_rates": miss_rates,
        })
    else:
        log.ok(f"Phase 8: parser miss-rates within baseline ({len(miss_rates)} parsers checked)")


def phase_9_ingest_gap(
    log: Log,
    data: dict,
    *,
    gap_threshold: float = 0.4,
    min_baseline: float = 1.0,
    today: datetime | None = None,
) -> None:
    """Phase 9 — Ingest-gap detection.

    Compares today's Lonny-outbound message volume against the rolling
    14-day median (read from ``data["baselines"]["ingest_volume_p50"]``).
    Flags as "possible ingest gap" if today < ``gap_threshold`` × p50.

    Skipped when ``baseline_p50 < min_baseline`` — a sparse baseline
    (e.g. 0.5 inbound/day on a 14-day calendar window dominated by
    weekends + ramp-up) makes any 0-day fire the alert. The
    floor suppresses that noise; once the rolling window holds enough
    real-volume days, the signal becomes meaningful again.

    No mutation — appends a ``selfheal_actions`` record with the count
    so :mod:`hilmar.insights` can call attention to it.
    """
    log.section("PHASE 9: INGEST-GAP DETECTION")

    today = today or core.now_utc()
    today_start = today.replace(hour=0, minute=0, second=0, microsecond=0)
    requests = data.get("requests") or []

    def is_today_request(r: dict) -> bool:
        ts = core.parse_iso(r.get("request_timestamp"))
        return bool(ts and ts >= today_start)

    today_count = sum(1 for r in requests if is_today_request(r))
    baseline_p50 = ((data.get("baselines") or {}).get("ingest_volume_p50"))

    if baseline_p50 is None:
        log.ok("Phase 9: no baseline yet — skipping ingest-gap scan")
        return

    if baseline_p50 < min_baseline:
        log.ok(f"Phase 9: baseline P50={baseline_p50} below floor {min_baseline} — skipping (data too sparse)")
        return

    threshold = gap_threshold * baseline_p50
    if today_count < threshold:
        log.warn(
            f"Possible ingest gap: today={today_count} requests, "
            f"baseline P50={baseline_p50} (threshold {gap_threshold}× = {threshold:.1f})",
        )
        _selfheal_record(data, "ingest_gap", {
            "today_count": today_count,
            "baseline_p50": baseline_p50,
            "gap_threshold": gap_threshold,
            "threshold_value": round(threshold, 1),
        })
    else:
        log.ok(f"Phase 9: today={today_count} requests vs baseline P50={baseline_p50} — within range")


def _matches_jsonschema_type(value: Any, declared: Any) -> bool:
    """Check if ``value``'s Python type matches a JSON-schema ``type`` declaration.

    Handles both scalar declarations (``"string"``) and union arrays
    (``["string", "null"]``). Unknown declarations are treated as
    pass-through so unrecognized schema constructs don't cause false
    drift warnings.
    """
    if isinstance(declared, list):
        return any(_matches_jsonschema_type(value, d) for d in declared)
    if declared == "null":
        return value is None
    if declared == "string":
        return isinstance(value, str)
    if declared == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if declared == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if declared == "boolean":
        return isinstance(value, bool)
    if declared == "array":
        return isinstance(value, list)
    if declared == "object":
        return isinstance(value, dict)
    return True


def _check_drift(
    log: Log,
    data: dict,
    items: list[dict[str, Any]],
    declared_props: dict[str, Any],
    scope: str,
) -> None:
    """Run the bidirectional drift check on a list of dict-shaped items
    against a JSON-schema properties block.

    - Logs a warning + records ``schema_undeclared_fields`` for fields
      present in ``items`` but not in ``declared_props``.
    - Logs a warning + records ``schema_type_drift`` for values whose
      Python type contradicts the declared JSON-schema type.

    ``scope`` (e.g. ``"request"``, ``"summary"``, ``"lane"``,
    ``"carrier"``) labels the records and log messages so the daily
    insights pass can surface drift per schema level.
    """
    if not items or not declared_props:
        return

    declared_fields = set(declared_props.keys())
    actual_fields: set[str] = set()
    for it in items:
        actual_fields.update(it.keys())

    undeclared = sorted(actual_fields - declared_fields)
    if undeclared:
        log.warn(
            f"Phase 10: {len(undeclared)} field(s) in {scope} data but not "
            f"declared in schema.json: {', '.join(undeclared)}"
        )
        _selfheal_record(data, "schema_undeclared_fields", {
            "scope": scope,
            "fields": undeclared,
            "fix_hint": f"add to schema.json definitions.{scope}.properties",
        })

    type_violations: list[dict[str, Any]] = []
    for fname, fspec in declared_props.items():
        declared_type = fspec.get("type")
        if declared_type is None:
            continue
        for it in items:
            if fname not in it:
                continue
            v = it[fname]
            if not _matches_jsonschema_type(v, declared_type):
                type_violations.append({
                    "scope": scope,
                    "field": fname,
                    "declared": declared_type,
                    "actual_type": type(v).__name__,
                })

    if type_violations:
        by_field: dict[str, int] = {}
        for tv in type_violations:
            by_field[tv["field"]] = by_field.get(tv["field"], 0) + 1
        details = ", ".join(f"{f}({n})" for f, n in sorted(by_field.items()))
        log.warn(
            f"Phase 10: type drift in {scope} on {len(by_field)} field(s) "
            f"({len(type_violations)} instances): {details}"
        )
        _selfheal_record(data, "schema_type_drift", {
            "scope": scope,
            "by_field": by_field,
            "samples": type_violations[:10],
            "total": len(type_violations),
        })


def phase_10_schema_drift(log: Log, data: dict, schema_path: Path | None = None) -> None:
    """Phase 10 — Schema-drift detection (bidirectional, all levels).

    Three checks, run against every level of the schema:

    1. **Intra-data consistency** (mutating, request-level only):
       Walks every request and computes the union of field names. Any
       field present on some entries but missing on others is filled
       in (with a schema-aware default — [] for arrays, {} for
       objects, None otherwise) so all rows are shape-equivalent.

    2. **Data → Schema drift** (informational): Fields present in the
       data but not declared in ``schema.json`` surface as warnings.
       Runs on top-level + ``summary`` + every ``lane`` + every
       ``carrier`` + every ``request``.

    3. **Schema type drift** (informational): Values whose Python type
       contradicts the declared JSON-schema type surface as warnings.
       Runs on the same scope as Check 2.

    Only check (1) mutates. (2) and (3) write to ``selfheal_actions``
    so the daily insights pass surfaces them in the email — but they
    never auto-fix the schema, since the right answer is human review
    (declare the field, fix the type, or remove it).

    Audit on 2026-04-29 caught 14 request-level fields the data had
    but schema didn't declare, plus 4 type mismatches and a top-level
    field-name typo (``data_range`` vs ``date_range``). Without
    bidirectional all-level coverage, the next drift would also live
    silent for months.
    """
    log.section("PHASE 10: SCHEMA-DRIFT DETECTION")

    requests = data.get("requests") or []
    if len(requests) < 2:
        log.ok("Phase 10: <2 requests — schema-drift scan needs more data")
        return

    # Load schema once for use by all checks below.
    schema: dict[str, Any] = {}
    req_props: dict[str, Any] = {}
    summary_props: dict[str, Any] = {}
    lane_props: dict[str, Any] = {}
    carrier_props: dict[str, Any] = {}
    top_props: dict[str, Any] = {}
    if schema_path is not None:
        try:
            schema = json.loads(schema_path.read_text())
            defs = schema.get("definitions", {})
            req_props = defs.get("request", {}).get("properties", {})
            summary_props = defs.get("summary", {}).get("properties", {})
            lane_props = defs.get("lane", {}).get("properties", {})
            carrier_props = defs.get("carrier", {}).get("properties", {})
            top_props = schema.get("properties", {})
        except Exception as e:  # noqa: BLE001
            log.warn(f"Phase 10: schema parse failed, skipping schema-vs-data check: {e}")

    def _schema_default(field_name: str) -> Any:
        """Pick a schema-appropriate default for a missing field. Array
        fields get [], object fields get {}, everything else None.
        Avoids type-drift false positives on the fill itself."""
        spec = req_props.get(field_name) or {}
        declared = spec.get("type")
        if isinstance(declared, list):
            if "array" in declared:
                return []
            if "object" in declared:
                return {}
            return None
        if declared == "array":
            return []
        if declared == "object":
            return {}
        return None

    # ── Check 1: intra-data consistency (request-level only) ────────
    all_keys: set[str] = set()
    for r in requests:
        all_keys.update(r.keys())

    drifted: dict[str, int] = {}
    for key in sorted(all_keys):
        missing = sum(1 for r in requests if key not in r)
        if 0 < missing < len(requests):
            drifted[key] = missing

    if drifted:
        for r in requests:
            for key in drifted:
                if key not in r:
                    r[key] = _schema_default(key)
        log.fix(
            f"Schema-drift fix applied: filled {sum(drifted.values())} missing-key "
            f"slots across {len(drifted)} fields ({list(drifted)})",
        )
        _selfheal_record(data, "schema_drift", {
            "fields_added": list(drifted),
            "missing_counts_before": drifted,
            "rows_total": len(requests),
        })
    else:
        log.ok(f"Phase 10: schema consistent across {len(requests)} requests "
               f"({len(all_keys)} keys)")

    # ── Checks 2 + 3: schema vs data drift on every level ───────────
    if not schema:
        return

    # Top-level: drift checks against schema.properties (treating the
    # data dict itself as one "item"). Pure declarations check; type
    # check applies to scalar top-level fields.
    if top_props:
        _check_drift(log, data, [data], top_props, "top-level")

    # Summary: singleton dict
    summary = data.get("summary")
    if isinstance(summary, dict) and summary_props:
        _check_drift(log, data, [summary], summary_props, "summary")

    # Lanes: list of dicts
    lanes = data.get("lanes") or []
    if lanes and lane_props:
        _check_drift(log, data, lanes, lane_props, "lane")

    # Carriers: list of dicts
    carriers = data.get("carriers") or []
    if carriers and carrier_props:
        _check_drift(log, data, carriers, carrier_props, "carrier")

    # Requests: list of dicts (the existing detailed coverage)
    if req_props:
        _check_drift(log, data, requests, req_props, "request")


def phase_7_save(log: Log, data: dict, data_path: Path, result_path: Path) -> dict:
    log.section("PHASE 7: PERSIST")
    # Post-Phase-A: ``data["qc"]`` was a duplicate of ``qc-result.json``
    # with no readers in src/ (consumers went to the result file).
    # Dropped — the result file below is the canonical QC surface.
    data["last_updated"] = core.now_utc().isoformat()
    core.save_data(data, data_path)
    log.ok(f"Wrote {data_path}")

    requests = data["requests"]
    summary = data["summary"]
    result = {
        "status": "CLEAN" if not log.errors else "HAS_ERRORS",
        "fixes": len(log.fixes),
        "warnings": len(log.warnings),
        "errors": len(log.errors),
        "error_details": log.errors,
        "warning_details": log.warnings,
        "counts": {
            "total": len(requests),
            "wins": summary["wins"],
            "ql": summary["quoted_lost"],
            "nq": summary["not_quoted"],
            "pending": summary["pending_hilmar"],
        },
        "teu": {
            "requested": summary["teu_requested"],
            "won": summary["teu_won"],
            "ql": summary["teu_quoted_lost"],
            "nq": summary["teu_not_quoted"],
            "pending": summary["teu_pending"],
        },
        "rates": {
            "win_rate": summary["win_rate"],
            "quote_rate": summary["quote_rate"],
        },
    }
    result_path.parent.mkdir(parents=True, exist_ok=True)
    with open(result_path, "w") as f:
        json.dump(result, f, indent=2)
    log.ok(f"Wrote {result_path}")
    return result


# ─────────────────────────────────────────────────────────────────────
# Library entry point
# ─────────────────────────────────────────────────────────────────────

def _empty_skeleton() -> dict:
    """Cold-start fallback when the data file is missing entirely.
    Phase A shape: raw `requests` only — phase_5 derives summary/lanes/
    carriers on its first run."""
    return {
        "version": "8.0-aggregates-post-qc",
        "client": "Hilmar Ingredients",
        "contact": "lupfold@hilmaringredients.com",
        "provider": "OL-USA",
        "last_updated": core.now_utc().isoformat(),
        "requests": [],
    }


def run_qc(
    data_path: Path,
    schema_path: Path,
    backups_dir: Path,
    result_path: Path,
    *,
    do_backup: bool = True,
    retention: int = 14,
) -> tuple[dict[str, Any], Log]:
    """Run all 7 QC phases. Returns ``(result_dict, log)``.

    The orchestrator inspects ``result["status"]`` ("CLEAN" / "HAS_ERRORS" /
    "BLOCKED") to decide whether to escalate.
    """
    log = Log()

    if not data_path.exists():
        core.save_data(_empty_skeleton(), data_path)
        log.fix(f"Created new empty data file at {data_path}")

    phase_1_files(log, data_path, schema_path)

    if do_backup:
        dest = rotate_backup(data_path, backups_dir, keep=retention)
        log.ok(f"Backup → {dest.name}")

    data = core.load_data(data_path)

    if not phase_2_structure(log, data):
        log.error("BLOCKING: structural integrity failure")
        result = {"status": "BLOCKED", "errors": log.errors}
        result_path.parent.mkdir(parents=True, exist_ok=True)
        with open(result_path, "w") as f:
            json.dump(result, f, indent=2)
        return result, log

    phase_3_entries(log, data)
    phase_4_duplicates(log, data)
    phase_4_5_derived_fields(log, data)
    phase_5_summaries(log, data)
    phase_6_rules(log, data)
    # Phases 8/9/10 — self-heal extensions for the insights engine (M3.9).
    # Run BEFORE phase_7_save so any selfheal_actions[] records get persisted.
    phase_8_parser_regression(log, data)
    phase_9_ingest_gap(log, data)
    phase_10_schema_drift(log, data, schema_path)
    result = phase_7_save(log, data, data_path, result_path)
    return result, log


# ─────────────────────────────────────────────────────────────────────
# CLI: hilmar-qc
# ─────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description="Hilmar tracker self-healing QC")
    parser.add_argument(
        "--data", type=Path, default=None,
        help="Path to tracking-data-v2.json (default: $HILMAR_DATA_DIR/tracking-data-v2.json)",
    )
    parser.add_argument(
        "--schema", type=Path, default=None,
        help="Path to schema.json (default: package schema.json)",
    )
    parser.add_argument(
        "--backups", type=Path, default=None,
        help="Backup directory (default: $HILMAR_BACKUP_DIR)",
    )
    parser.add_argument(
        "--result", type=Path, default=None,
        help="qc-result.json output (default: $HILMAR_REPORTS_DIR/qc-result.json)",
    )
    parser.add_argument("--no-backup", action="store_true")
    parser.add_argument(
        "--retention", type=int, default=None,
        help="Backup retention count (default: $HILMAR_BACKUP_RETENTION or 14)",
    )
    args = parser.parse_args()

    data_path = args.data or paths.data_file()
    schema_path = args.schema or paths.schema_file()
    backups = args.backups or paths.backup_dir()
    result_path = args.result or paths.qc_result_file()
    retention = args.retention if args.retention is not None else paths.backup_retention()

    result, log = run_qc(
        data_path, schema_path, backups, result_path,
        do_backup=not args.no_backup, retention=retention,
    )

    print("\n" + "═" * 60)
    print("QC SELF-HEAL COMPLETE")
    print("═" * 60)
    print(f"  Status:      {result['status']}")
    if result["status"] == "BLOCKED":
        return 1
    print(f"  🔧 Fixes:     {result['fixes']}")
    print(f"  ⚠️  Warnings: {result['warnings']}")
    print(f"  🔴 Errors:    {result['errors']}")
    c = result["counts"]
    print(f"  📊 {c['total']} entries: {c['wins']}W | {c['ql']} Q&L | {c['nq']} NQ | {c['pending']} P")
    print(f"  📈 Win rate: {result['rates']['win_rate']}% | Quote rate: {result['rates']['quote_rate']}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
