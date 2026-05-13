#!/usr/bin/env python3
"""
Hilmar Tracker — self-healing QC engine.

Runs BEFORE and AFTER every daily processing cycle. Validates the data file,
auto-heals what it can, flags what it can't.

Safety:
  - Creates a timestamped backup BEFORE any mutation.
  - Idempotent — safe to run twice in a row.
  - Non-blocking — writes a qc-result.json and exits 0 even with errors;
    the orchestrator decides what to do.

Usage:
  python3 qc_selfheal.py [--data PATH] [--config PATH] [--no-backup]
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import core
import body_parser as BP

# ─────────────────────────────────────────────────────────────────────
# COVERED-loss reason heuristics — promote OTHER → COVERED when we have
# direct text evidence. Keeps loss_reason taxonomy honest.
# ─────────────────────────────────────────────────────────────────────
_COVERED_HINTS = ("covered", "competitor", "another forwarder", "going with",
                  "going with another", "used another")

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
                pass
        existing = existing[1:]
    return dest


# ─────────────────────────────────────────────────────────────────────
# Log helpers
# ─────────────────────────────────────────────────────────────────────

class Log:
    def __init__(self):
        self.fixes, self.warnings, self.errors = [], [], []

    def fix(self, msg):
        self.fixes.append(msg); print(f"  🔧 FIX: {msg}")

    def warn(self, msg):
        self.warnings.append(msg); print(f"  ⚠️  WARN: {msg}")

    def error(self, msg):
        self.errors.append(msg); print(f"  🔴 ERROR: {msg}")

    def ok(self, msg):
        print(f"  ✅ {msg}")

    def section(self, title):
        print("\n" + "═" * 60)
        print(title)
        print("═" * 60)


# ─────────────────────────────────────────────────────────────────────
# Phase 1 — file health
# ─────────────────────────────────────────────────────────────────────

def phase_1_files(log: Log, data_path: Path, schema_path: Path):
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
    for key in ("requests", "summary", "version"):
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

def _heal_carrier_won(log: Log, rid_label: str, r: dict) -> None:
    """Best-effort fill carrier_won on a WIN. Order:
       1) copy carrier_quoted   2) parse from subject (MDOLX patterns)
       3) leave None and leave a per-record warning
    Always records what we did so the QC log is auditable."""
    if r.get("carrier_won"):
        return
    if r.get("carrier_quoted"):
        r["carrier_won"] = r["carrier_quoted"]
        log.fix(f"{rid_label}: WIN carrier_won copied from carrier_quoted={r['carrier_quoted']}")
        return
    # Subject-based fallback (standalone wins from MDOLX confirmations)
    subj = r.get("subject") or ""
    sc = BP.parse_subject_carrier(subj)
    if sc:
        sc = core.normalize_carrier(sc) or sc
        r["carrier_won"] = sc
        r["carrier_quoted"] = r.get("carrier_quoted") or sc
        log.fix(f"{rid_label}: WIN carrier_won={sc} parsed from subject")
        return
    log.warn(f"{rid_label}: WIN with no carrier_won AND no subject signal — flagged for manual review")


def _reclassify_covered(log: Log, rid_label: str, r: dict) -> None:
    """If a LOSS has loss_reason='OTHER' but the reason_detail or lonny_covered
    flag says it was covered by a competitor, promote it to 'COVERED' for clarity."""
    lr = (r.get("loss_reason") or "").upper()
    if lr in ("COVERED",):
        return
    if r.get("status") != "LOSS":
        return
    rd = (r.get("reason_detail") or "").lower()
    is_covered = bool(r.get("lonny_covered")) or any(h in rd for h in _COVERED_HINTS)
    if not is_covered:
        return
    if lr in ("OTHER", "", "NONE"):
        prior = r.get("loss_reason")
        r["loss_reason"] = "COVERED"
        log.fix(f"{rid_label}: loss_reason {prior!r} → COVERED (text/flag evidence)")


# ─────────────────────────────────────────────────────────────────────
# Containers hygiene — strip email-body bleed (CAUTION banners, identical-
# bookings phrases, etc.) so the equipment column stays clean.
# Added 2026-05-04 after parser bleed surfaced in CMA CGM scorecard.
# ─────────────────────────────────────────────────────────────────────
import re as _re
_DIRTY_CONTAINERS_HINTS = (
    "caution", "outside of", "originated from", "i need ", "identical bookings",
    "i need two", "i need three", "i need four", "do not click",
    "this email", "confidential",
)
_CONTAINER_PATTERN = _re.compile(
    r"(\d+\s*[\-xX]\s*\d{1,3}\s*['’]?\s*(?:HC|DV|RF|GP|Reefer|Flex)?(?:\s+Reefer)?)",
    _re.IGNORECASE,
)
_INEED_PATTERN = _re.compile(
    r"I\s+need\s+(two|three|four|five)\s+(?:identical\s+bookings?\.?\s*)?(?:\n|$)?\s*"
    r"(?:[\d\-x'\"’\s]*?(\d+)['’]?\s*(HC|DV|RF|Reefer|HC\s+Reefer)?)?",
    _re.IGNORECASE,
)


def _heal_containers(log: Log, rid_label: str, r: dict, bodies_idx: dict) -> None:
    """Detect and clean dirty `containers` values (email-body bleed). Try in
    order: regex on the dirty value itself, lookup from cached source body,
    finally placeholder + warn. Idempotent: a clean value is a no-op."""
    c = r.get("containers") or ""
    low = c.lower()
    if not c or not any(h in low for h in _DIRTY_CONTAINERS_HINTS):
        return  # already clean
    # 1) Try local regex on dirty value
    m = _CONTAINER_PATTERN.search(c)
    if m and "identical" not in m.group(0).lower():
        new_c = m.group(1).strip()
        r["containers"] = new_c
        log.fix(f"{rid_label}: containers cleaned from email-body bleed → {new_c!r}")
        return
    # 2) Try cached source body — Lonny's outbound usually has a clean equipment line
    for imid in (r.get("source_imids") or []):
        body = (bodies_idx.get(imid) or {}).get("text_body") or ""
        if not body:
            continue
        # "I need three identical bookings\n\n1-40' HC Reefer ..." → 3-40' HC Reefer
        ineed = _INEED_PATTERN.search(body)
        if ineed and ineed.group(2):
            qty_word = ineed.group(1).lower()
            qty = {"two": 2, "three": 3, "four": 4, "five": 5}[qty_word]
            size = ineed.group(2)
            eq = (ineed.group(3) or "").strip()
            new_c = f"{qty}-{size}' {eq}".strip()
            r["containers"] = new_c
            log.fix(f"{rid_label}: containers recovered from body via 'I need {qty_word}' → {new_c!r}")
            return
        # Plain pattern in body
        m2 = _CONTAINER_PATTERN.search(body[:200])
        if m2:
            new_c = m2.group(1).strip()
            r["containers"] = new_c
            log.fix(f"{rid_label}: containers recovered from body → {new_c!r}")
            return
    # 3) Could not recover — flag for manual review (don't leave junk in display)
    r["containers"] = "— (manual review)"
    log.warn(f"{rid_label}: containers had email bleed but no clean equipment found")


# ─────────────────────────────────────────────────────────────────────
# Rate backfill — when a LOSS/PRICE has carrier_quoted but no ol_rate,
# re-parse the cached OL response body via body_parser.parse_rate_table.
# Added 2026-05-04 after Port Klang surfaced as $rate=None in scorecard.
# ─────────────────────────────────────────────────────────────────────

def _heal_missing_rate(log: Log, rid_label: str, r: dict, bodies_idx: dict) -> None:
    """If a quoted LOSS has no ol_rate, try to recover from cached OL body."""
    if r.get("ol_rate") is not None:
        return
    if not (r.get("status") == "LOSS" and r.get("quoted")):
        return
    if not r.get("carrier_quoted"):
        return  # nothing to anchor against
    for imid in (r.get("source_imids") or []):
        body = (bodies_idx.get(imid) or {}).get("text_body") or ""
        if not body or " | " not in body:
            continue
        try:
            parsed = BP.parse_rate_table(body)
        except Exception:
            continue
        rate = parsed.get("ol_rate")
        if rate is not None:
            r["ol_rate"] = rate
            # Also fill any missing structured fields
            for k in ("vessel_voyage", "etd_offered", "eta_offered", "transshipment"):
                if not r.get(k) and parsed.get(k):
                    r[k] = parsed[k]
            log.fix(f"{rid_label}: ol_rate ${rate} backfilled from cached OL body")
            return


def _load_bodies_index() -> dict:
    """Load scripts/stage_emails_bodies.txt keyed by imid. Empty dict if missing.
    2026-05-06: prefer .txt for SharePoint indexing; fall back to legacy .jsonl."""
    here = Path(__file__).resolve().parent
    path = here / "stage_emails_bodies.txt"
    if not path.exists():
        path = here / "stage_emails_bodies.jsonl"
    out = {}
    if not path.exists():
        return out
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("imid"):
                    out[rec["imid"]] = rec
    except Exception:
        return {}
    return out


def phase_3_entries(log: Log, data: dict):
    log.section("PHASE 3: ENTRY-LEVEL HEALING")
    requests = data["requests"]
    # Load source bodies once for healers that need them (containers cleanup,
    # rate backfill). Safe if file is missing — healers gracefully skip.
    bodies_idx = _load_bodies_index()
    for i, r in enumerate(requests):
        rid_label = f"[{i}] {r.get('request_date') or r.get('date','?')} {r.get('destination','?')}"
        # Hygiene healers — run on every record regardless of status/lock.
        _heal_containers(log, rid_label, r, bodies_idx)
        _heal_missing_rate(log, rid_label, r, bodies_idx)

        if not r.get("request_id"):
            r["request_id"] = core.request_id(
                r.get("conversationId"), r.get("request_timestamp"),
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

        c_count, teu = core.parse_teu(r.get("containers", ""))
        if r.get("containers") and (not r.get("teu_requested") or r["teu_requested"] == 0):
            if teu > 0:
                r["teu_requested"] = teu
                r.setdefault("container_count", c_count)
                log.fix(f"{rid_label}: Recalculated teu_requested={teu}")
        if not r.get("container_count") and c_count:
            r["container_count"] = c_count

        if not r.get("lane") and r.get("origin") and r.get("destination"):
            r["lane"] = f"{r['origin']} → {r['destination']}"

        if "quoted" not in r:
            r["quoted"] = bool(r.get("response_timestamp") or r.get("carrier_quoted") or r.get("ol_rate"))
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

        # HARD LOCK: skip status decisions on manually-locked records (Michael's audit pass 2026-05-01)
        # Even on locked records: heal carrier_won (WIN only) + reclassify OTHER -> COVERED (LOSS only).
        # _heal_carrier_won is meaningless on LOSS records — only call when locked record is WIN.
        if r.get("manual_locked"):
            if r.get("status") == "WIN":
                _heal_carrier_won(log, rid_label, r)
            if r.get("status") == "LOSS":
                _reclassify_covered(log, rid_label, r)
            continue
        # Honor explicit lonny_covered flag (Lonny replied "Covered" = lost to competitor)
        if r.get("lonny_covered"):
            prior_status = r.get("status")
            if prior_status != "LOSS" or r.get("loss_reason") != "COVERED":
                r["status"] = "LOSS"
                r["loss_reason"] = "COVERED"
                log.fix(f"{rid_label}: lonny_covered → LOSS/COVERED")
            continue
        prior_status = r.get("status")
        decision = core.decide_status(
            has_send=r.get("has_send", False),
            mdolx_ref=r.get("mdolx_ref"),
            response_timestamp=r.get("response_timestamp"),
            quoted=r.get("quoted", False),
            etd_fit_days=r.get("etd_fit_days"),
        )
        if prior_status != decision.status:
            core.record_transition(r, decision.status, decision.reason_detail)
            log.fix(f"{rid_label}: Status {prior_status} → {decision.status} ({decision.reason_detail})")
        else:
            r["status"] = decision.status
        r["has_send"] = decision.has_send
        r["loss_reason"] = decision.loss_reason

        if r["status"] == "WIN":
            _heal_carrier_won(log, rid_label, r)
            if not r.get("mdolx_ref") and not r.get("has_send"):
                log.warn(f"{rid_label}: WIN has no chain-send signal AND no MDOLX ref — verify booking")
            if not r.get("teu_won"):
                r["teu_won"] = r.get("teu_requested", 0) or 0
                log.fix(f"{rid_label}: WIN teu_won defaulted to teu_requested")
            r["quoted"] = True

        if r["status"] == "LOSS":
            _reclassify_covered(log, rid_label, r)

        if r["status"] == "LOSS" and not r.get("quoted"):
            for k in ("carrier_quoted", "vessel_offered", "etd_offered", "eta_offered", "transshipment"):
                if r.get(k) and r[k] not in ("", None, "N/A"):
                    log.fix(f"{rid_label}: NQ contamination — cleared {k}={r[k]!r}")
                    r[k] = None
            if r.get("ol_rate") and r["ol_rate"] not in (None, "", "Not Quoted", "N/A"):
                log.fix(f"{rid_label}: NQ contamination — cleared ol_rate={r['ol_rate']!r}")
                r["ol_rate"] = "Not Quoted"

        if r["status"] == "LOSS" and r.get("has_send"):
            r["has_send"] = False
            log.fix(f"{rid_label}: Cleared has_send on LOSS")


# ─────────────────────────────────────────────────────────────────────
# Phase 4 — duplicate detection
# ─────────────────────────────────────────────────────────────────────

def phase_4_duplicates(log: Log, data: dict):
    log.section("PHASE 4: DUPLICATE DETECTION")
    requests = data["requests"]
    seen = Counter()
    for r in requests:
        seen[r.get("request_id", "")] += 1
    dupes = {k: v for k, v in seen.items() if v > 1 and k}
    if not dupes:
        log.ok("No duplicate request_ids")
        return
    keepers = []
    dropped = []
    by_id: dict[str, list[dict]] = {}
    for r in requests:
        by_id.setdefault(r.get("request_id", ""), []).append(r)
    for rid, group in by_id.items():
        if len(group) == 1:
            keepers.append(group[0])
            continue
        canonical = max(group, key=lambda r: sum(1 for v in r.values() if v not in (None, "", [])))
        for other in group:
            if other is canonical:
                continue
            dropped.append(other)
        keepers.append(canonical)
        log.fix(f"Deduped request_id={rid} — kept richest, dropped {len(group)-1}")
    data["requests"] = keepers


# ─────────────────────────────────────────────────────────────────────
# Phase 5 — summaries
# ─────────────────────────────────────────────────────────────────────

def phase_5_summaries(log: Log, data: dict):
    log.section("PHASE 5: SUMMARY RECALCULATION")
    old_summary = data.get("summary", {}) or {}
    computed = core.aggregate_summary(data["requests"])
    if "dod" in old_summary and "dod" not in computed:
        computed["dod"] = old_summary["dod"]
    drift = False
    for k, v in computed.items():
        if old_summary.get(k) != v:
            drift = True
    data["summary"] = computed
    data["lane_summary"] = core.aggregate_lanes(data["requests"])
    data["carrier_summary"] = core.aggregate_carriers(data["requests"])
    log.fix("Summary, lane_summary, carrier_summary rebuilt from raw data" + (" (drift detected)" if drift else ""))


# ─────────────────────────────────────────────────────────────────────
# Phase 6 — cross-check rules
# ─────────────────────────────────────────────────────────────────────

def phase_6_rules(log: Log, data: dict):
    log.section("PHASE 6: CROSS-CHECK RULES")
    requests = data["requests"]
    wins = [r for r in requests if r["status"] == "WIN"]
    ql = [r for r in requests if r["status"] == "LOSS" and r.get("quoted")]
    nq = [r for r in requests if r["status"] == "LOSS" and not r.get("quoted")]
    pending = [r for r in requests if r["status"] == "PENDING"]

    if len(requests) > 10 and len(ql) == 0:
        log.warn(f"QC-001: 0 Quoted & Lost among {len(requests)} entries — verify")
    else:
        log.ok(f"QC-001: {len(ql)} Q&L — plausible")

    bad = [r for r in wins if not r.get("carrier_won")]
    if bad:
        # phase_3 already attempted: copy from carrier_quoted, parse from subject.
        # If we still have unfilled values here, they're truly unrecoverable from
        # current data — surface as warn (manual review), not error (pipeline-fail).
        log.warn(f"QC-002: {len(bad)} WIN(s) with no carrier_won after auto-heal — manual review")
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
        rt = core.parse_iso(r.get("response_timestamp"))
        if rt and (now - rt).total_seconds() / 3600 > 24:
            log.error(f"QC-007: {r['request_id']} still PENDING past 24h — state machine should have aged this")

    # ─────────────────────────────────────────────────────────────────
    # QC-008/009/010 — paired with refresh_stage.py and the additive
    # ingest merge added 2026-05-05. Per Michael's standing rule, every
    # new code pattern ships with QC + self-heal in the same commit.
    # ─────────────────────────────────────────────────────────────────

    # QC-008: stage freshness
    # Stage files live next to the script directory. If max(received) is more
    # than 36h old on a weekday morning, refresh_stage probably hasn't run
    # (Task Scheduler missed, MSAL refresh expired, Graph quota, etc.).
    # 2026-05-06: prefer .txt; fall back to legacy .jsonl
    here = Path(__file__).resolve().parent
    stage_path = here / "stage_emails.txt"
    if not stage_path.exists():
        stage_path = here / "stage_emails.jsonl"
    if stage_path.exists():
        max_received = None
        try:
            with open(stage_path, encoding="utf-8") as f:
                for line in f:
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    rcv = rec.get("received") or rec.get("sent")
                    if rcv:
                        if max_received is None or rcv > max_received:
                            max_received = rcv
        except OSError as e:
            log.warn(f"QC-008: stage file unreadable: {e}")
            max_received = None
        if max_received:
            ts = core.parse_iso(max_received)
            if ts:
                age_hours = (now - ts).total_seconds() / 3600
                # Stale-stage threshold: 36h (covers a weekend gap of 60h on
                # Monday morning would still alert; tune if Sunday refresh
                # ever lands in scope).
                if age_hours > 36 and now.weekday() < 5:
                    log.warn(
                        f"QC-008: stage_emails.jsonl latest received is "
                        f"{age_hours:.1f}h old — refresh_stage may have stopped firing"
                    )
                else:
                    log.ok(f"QC-008: stage freshness {age_hours:.1f}h — fresh")
        else:
            log.warn("QC-008: stage has no received timestamps to evaluate")
    else:
        log.warn("QC-008: stage_emails.jsonl not present (laptop pin without refresh?)")

    # QC-009: stage bucket classification drift
    # If any expected bucket has 0 records in the last 7 days while others
    # have meaningful counts, the sender-rule classifier in refresh_stage
    # is probably missing a sender pattern. WARN — no auto-heal (changes
    # to classification require human judgement).
    if stage_path.exists():
        try:
            cutoff7d = (now - core.parse_iso("1970-01-01T00:00:00Z").__class__(
                year=now.year, month=now.month, day=now.day,
                tzinfo=now.tzinfo
            ).replace(day=now.day) - core.parse_iso("1970-01-01T00:00:00Z"))
        except Exception:
            cutoff7d = None
        # Simpler: compute cutoff inline
        from datetime import timedelta as _td
        cutoff = now - _td(days=7)
        bucket_counts_7d = Counter()
        with open(stage_path, encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                rcv = rec.get("received") or rec.get("sent")
                ts = core.parse_iso(rcv) if rcv else None
                if ts and ts >= cutoff:
                    bucket_counts_7d[rec.get("bucket") or "?"] += 1
        expected_buckets = {"lonny_outbound", "lonny_reply", "mbd_inbound", "mbd_rate_response"}
        empty_buckets = [
            b for b in expected_buckets
            if bucket_counts_7d.get(b, 0) == 0 and sum(bucket_counts_7d.values()) > 5
        ]
        if empty_buckets:
            log.warn(
                f"QC-009: bucket(s) with zero stage records in last 7d while "
                f"others have data — classifier may be dropping a sender: {sorted(empty_buckets)} "
                f"(7d counts: {dict(bucket_counts_7d)})"
            )
        else:
            log.ok(f"QC-009: stage bucket distribution healthy (7d: {dict(bucket_counts_7d)})")

    # QC-010: preserved-wins growth
    # The additive merge in ingest.py carries forward old WINs that the
    # fresh stage didn't reproduce. A small steady set is fine (genuine
    # off-channel bookings). A growing one means new bookings are
    # silently failing to land — refresh_stage's Outlook search is too
    # narrow.
    preserved = [r for r in requests if r.get("preserved_from_prior")]
    PRESERVED_THRESHOLD = 10
    if len(preserved) > PRESERVED_THRESHOLD:
        log.warn(
            f"QC-010: {len(preserved)} preserved-from-prior WIN(s) — "
            f"exceeds threshold {PRESERVED_THRESHOLD}. Refresh_stage may be "
            f"missing booking emails. Investigate before this set keeps growing."
        )
    elif preserved:
        log.ok(f"QC-010: {len(preserved)} preserved-from-prior WIN(s) — within threshold")
    else:
        log.ok("QC-010: No preserved-from-prior WINs (fresh stage covers everything)")

    # QC-011: email subject date == previous business day
    # Per Michael 2026-05-07 'yesterday kpi run' — the daily email fires at
    # 10 AM ET, before Lonny's California (PT) office opens. The subject
    # line and KPIs MUST report on the previous full business day, not
    # literal today. This QC parses email-subject.txt and confirms the
    # date in the subject matches the expected report date. Catches
    # regressions if gen_email.py drifts back to using today's date.
    try:
        from datetime import datetime as _dt, timedelta as _td
        import re as _re
        # Compute expected report date (mirror of _report_date in gen_email.py)
        _now_et = _dt.now(core.ET).date()
        _wd = _now_et.weekday()  # Mon=0..Sun=6
        if _wd == 0:    _delta = 3   # Mon → Fri
        elif _wd == 5:  _delta = 1   # Sat → Fri
        elif _wd == 6:  _delta = 2   # Sun → Fri
        else:           _delta = 1   # Tue–Fri → yesterday
        _expected = _now_et - _td(days=_delta)
        # Parse subject like 'Hilmar Ingredients — Daily Shipment Tracker Update (May 6, 2026)'
        _subj_path = Path(__file__).resolve().parent.parent / "reports" / "email-subject.txt"
        if not _subj_path.exists():
            log.warn("QC-011: reports/email-subject.txt not present — skip date check")
        else:
            _subj = _subj_path.read_text(encoding="utf-8").strip()
            _m = _re.search(r"\(([A-Za-z]+)\s+(\d+),\s+(\d{4})\)", _subj)
            if not _m:
                log.warn(f"QC-011: could not parse date from subject: {_subj!r}")
            else:
                _mo, _day, _yr = _m.group(1), int(_m.group(2)), int(_m.group(3))
                # Parse month name to number
                try:
                    _parsed = _dt.strptime(f"{_mo} {_day} {_yr}", "%b %d %Y").date()
                except ValueError:
                    try:
                        _parsed = _dt.strptime(f"{_mo} {_day} {_yr}", "%B %d %Y").date()
                    except ValueError:
                        _parsed = None
                if _parsed is None:
                    log.warn(f"QC-011: subject month not recognized: {_mo!r}")
                elif _parsed == _expected:
                    log.ok(f"QC-011: email subject date {_parsed.isoformat()} == expected previous biz day")
                elif _parsed == _now_et:
                    log.error(
                        f"QC-011: email subject date is TODAY ({_parsed.isoformat()}) but should be "
                        f"previous biz day ({_expected.isoformat()}). gen_email.py regressed — "
                        f"Lonny's PT office isn't open at 10 AM ET fire."
                    )
                else:
                    log.warn(
                        f"QC-011: email subject date {_parsed.isoformat()} != expected "
                        f"{_expected.isoformat()} (off by {(_parsed - _expected).days} days)"
                    )
    except Exception as _e:
        log.warn(f"QC-011: check failed with exception: {_e}")

    # QC-012: weekly bucket labels are Mon–Fri (5 weekdays), not Mon–Sun
    # Per Michael 2026-05-07: 'the dating on the weekly should be based on
    # weekdays'. Pre-existing labels were 'W19 (May 4–10)' (Mon-Sun); current
    # spec is 'W19 (May 4–8)' (Mon-Fri) with cross-month clarity for
    # 'W14 (Mar 30–Apr 3)'. This QC parses week labels in email-body.html
    # and confirms the start→end span is exactly 4 days (Mon to Fri),
    # never 6 (Mon to Sun).
    try:
        from datetime import datetime as _dt
        import re as _re
        _body_path = Path(__file__).resolve().parent.parent / "reports" / "email-body.html"
        if not _body_path.exists():
            log.warn("QC-012: reports/email-body.html not present — skip week label check")
        else:
            _body = _body_path.read_text(encoding="utf-8")
            # Match labels like 'W15 (Apr 6–10)' or 'W14 (Mar 30–Apr 3)'
            _wk_pat = _re.compile(
                r"W(\d+)\s*\(([A-Za-z]+)\s+(\d+)[–\-]([A-Za-z]+\s+)?(\d+)\)"
            )
            _bad = []
            _checked = 0
            _seen = set()
            for _m in _wk_pat.finditer(_body):
                _label = _m.group(0)
                if _label in _seen:
                    continue
                _seen.add(_label)
                _checked += 1
                _wk_n   = int(_m.group(1))
                _start_mo = _m.group(2)
                _start_d  = int(_m.group(3))
                _end_mo   = (_m.group(4) or _m.group(2)).strip()
                _end_d    = int(_m.group(5))
                # Try parsing both ends in 2026 (current data range).
                try:
                    _ds = _dt.strptime(f"{_start_mo} {_start_d} 2026", "%b %d %Y").date()
                    _de = _dt.strptime(f"{_end_mo} {_end_d} 2026", "%b %d %Y").date()
                    if _de < _ds:
                        # Cross-year edge — skip rather than misreport
                        continue
                    _span = (_de - _ds).days
                    if _span != 4:
                        _bad.append(f"{_label} span={_span}d (expected 4d Mon-Fri)")
                except Exception:
                    pass
            if _bad:
                log.error(
                    f"QC-012: {len(_bad)} week label(s) not Mon-Fri format: "
                    + "; ".join(_bad[:3]) + (f" + {len(_bad)-3} more" if len(_bad) > 3 else "")
                )
            elif _checked > 0:
                log.ok(f"QC-012: all {_checked} week labels Mon-Fri (4-day span)")
            else:
                log.ok("QC-012: no week labels found to check")
    except Exception as _e:
        log.warn(f"QC-012: check failed with exception: {_e}")

    # QC-013: email body header reflects the report-date framing — must NOT
    # say literal 'What Happened Today' (the regression case where gen_email.py
    # reverts to using today's date). The fixed framing reads e.g. 'What
    # Happened — Wednesday May 6, 2026' — present tense day/date.
    try:
        _body_path = Path(__file__).resolve().parent.parent / "reports" / "email-body.html"
        if _body_path.exists():
            _body = _body_path.read_text(encoding="utf-8")
            if "What Happened Today" in _body:
                log.error(
                    "QC-013: email body has 'What Happened Today' — gen_email.py "
                    "regressed to today framing. Should be 'What Happened — <Day Date>' "
                    "(previous business day, since 10 AM ET fire is before Lonny's PT office)."
                )
            elif "What Happened —" in _body:
                log.ok("QC-013: email body uses report-date framing")
            else:
                log.warn("QC-013: 'What Happened' header not found — body may be malformed")
    except Exception as _e:
        log.warn(f"QC-013: check failed with exception: {_e}")

    # QC-014: carrier coverage thresholds — added 2026-05-07 per Michael
    # 'handle all suggestions'. WIN coverage must be ≥95% (only 'Unknown' or
    # genuinely off-channel WINs should be missing). Q&L coverage soft-floor
    # at 60% (parser drift signal — WARN at 60-70%, FAIL below 60%).
    # The improvements report flags 50% Q&L coverage; this QC makes that
    # observation a hard threshold the system itself enforces.
    try:
        _wins  = [r for r in requests if r.get("status") == "WIN"]
        _wcarr = [r for r in _wins if r.get("carrier_won")]
        _wpct  = (len(_wcarr) / len(_wins) * 100) if _wins else 100
        _qls   = [r for r in requests if r.get("status") == "LOSS" and r.get("quoted")]
        _qlcarr = [r for r in _qls if r.get("carrier_quoted")]
        _qlpct = (len(_qlcarr) / len(_qls) * 100) if _qls else 100

        if _wpct < 90:
            log.error(
                f"QC-014a: WIN carrier coverage {_wpct:.1f}% < 90% threshold "
                f"({len(_wins)-len(_wcarr)}/{len(_wins)} WINs missing carrier). "
                "Patch_carriers auto-discovery + manual fallback should keep this >=95%."
            )
        elif _wpct < 95:
            log.warn(f"QC-014a: WIN carrier coverage {_wpct:.1f}% (target 95%+)")
        else:
            log.ok(f"QC-014a: WIN carrier coverage {_wpct:.1f}% ({len(_wcarr)}/{len(_wins)})")

        # Q&L coverage thresholds calibrated against current state 2026-05-07:
        # baseline ~48% (parser only catches table-format quotes; prose-format
        # quotes go uncaptured). ERROR triggers on regression below baseline,
        # WARN at baseline-target gap, OK at target. Parser improvement is a
        # multi-step effort — see improvements report for the suggestion.
        if _qlpct < 40:
            log.error(
                f"QC-014b: Q&L carrier coverage {_qlpct:.1f}% < 40% — REGRESSED "
                "below historical baseline. Recent parser change may have broken "
                "carrier extraction. Investigate body_parser.parse_rate_table."
            )
        elif _qlpct < 60:
            log.warn(
                f"QC-014b: Q&L carrier coverage {_qlpct:.1f}% (baseline ~48%, target 60%+). "
                "Parser only catches pipe-table quotes — prose quotes uncaught. See "
                "improvements report for the body-text fallback suggestion."
            )
        else:
            log.ok(f"QC-014b: Q&L carrier coverage {_qlpct:.1f}% ({len(_qlcarr)}/{len(_qls)})")
    except Exception as _e:
        log.warn(f"QC-014: check failed with exception: {_e}")

    # QC-015: unmapped trade-region destinations are bounded. Lonny ships to
    # the same handful of trade lanes; Unmapped should stay near zero. >5
    # means TRADE_REGION_MAP needs extension (see core._TRADE_REGION_MAP).
    try:
        _tr = _trade_region_reconciliation(data)
        _unmapped = _tr.get("unmapped_destinations", []) if isinstance(_tr, dict) else []
        if len(_unmapped) > 10:
            log.error(
                f"QC-015: {len(_unmapped)} unmapped destinations — extend "
                f"core._TRADE_REGION_MAP. First 5: {_unmapped[:5]}"
            )
        elif len(_unmapped) > 5:
            log.warn(f"QC-015: {len(_unmapped)} unmapped destinations — consider extending map: {_unmapped[:5]}")
        else:
            log.ok(f"QC-015: {len(_unmapped)} unmapped destination(s) (within tolerance)")
    except Exception as _e:
        log.warn(f"QC-015: check failed with exception: {_e}")

    # QC-019: status-change rows on the report date must have carrier_quoted.
    # Surfaced 2026-05-13 — Michael "status change of pending to quoted with no
    # carrier and no rate". When OL responds with a rate quote, the row's
    # status transitions PENDING -> QUOTED/WIN/LOSS, and that response body
    # carries the carrier+rate. If the parser fails to extract them (new
    # multi-line pipe-table template surfaced this week), the status change
    # appears in the email body but the carrier/rate columns are empty —
    # broken UX and broken negotiation depth. This QC catches the failure
    # at the data level so the email doesn't ship with empty cells.
    try:
        from datetime import datetime as _dt, timedelta as _td
        _now_et = _dt.now(core.ET).date()
        _wd = _now_et.weekday()
        if _wd == 0: _delta = 3
        elif _wd == 5: _delta = 1
        elif _wd == 6: _delta = 2
        else: _delta = 1
        _report_iso = (_now_et - _td(days=_delta)).isoformat()
        _missing = []
        for r in requests:
            for h in (r.get("status_history") or []):
                at = (h.get("at") or "")[:10]
                if (at == _report_iso and h.get("from") and h.get("to")
                        and h["from"] != h["to"] and h["to"] in ("QUOTED","WIN","LOSS")):
                    if not r.get("carrier_quoted") and not r.get("carrier_won"):
                        _missing.append(f"{(h.get('at') or '')[11:19]} {r.get('lane','?')}")
                    break
        if _missing:
            log.error(
                f"QC-019: {len(_missing)} status-change(s) on {_report_iso} have no "
                f"carrier — parser missed extraction. Rows: " + "; ".join(_missing[:5])
                + (f" + {len(_missing)-5} more" if len(_missing) > 5 else "")
            )
        else:
            log.ok(f"QC-019: all status changes on {_report_iso} have carrier attribution")
    except Exception as _e:
        log.warn(f"QC-019: check failed with exception: {_e}")

    # QC-018: day-row math reconciliation. The KPI day-row in email + dashboard
    # showed Requests vs W/QL/NQ but hid Pending — Michael 2026-05-08 caught
    # 2 Requests vs 0W+0QL+1NQ = 1, off-by-one. Now Pending is shown as a 5th
    # card and Total = W + QL + NQ + Pending. This QC enforces it.
    try:
        from datetime import datetime as _dt, timedelta as _td
        # Compute report date (mirror gen_email._report_date)
        _now_et = _dt.now(core.ET).date()
        _wd = _now_et.weekday()
        if _wd == 0: _delta = 3
        elif _wd == 5: _delta = 1
        elif _wd == 6: _delta = 2
        else: _delta = 1
        _report_iso = (_now_et - _td(days=_delta)).isoformat()
        _day = [r for r in requests
                if (r.get("request_date") == _report_iso) or (r.get("date") == _report_iso)]
        _w  = sum(1 for r in _day if r.get("status") == "WIN")
        _ql = sum(1 for r in _day if r.get("status") == "LOSS" and r.get("quoted"))
        _nq = sum(1 for r in _day if r.get("status") == "LOSS" and not r.get("quoted"))
        _p  = sum(1 for r in _day if r.get("status") == "PENDING")
        _t  = len(_day)
        _sum = _w + _ql + _nq + _p
        if _t != _sum:
            log.error(
                f"QC-018: day-row math broken — total={_t} but W+QL+NQ+P={_sum} "
                f"(W={_w}, QL={_ql}, NQ={_nq}, P={_p}). Some rows have unknown status."
            )
        else:
            log.ok(f"QC-018: day-row math reconciled ({_t} = {_w}W + {_ql}QL + {_nq}NQ + {_p}P)")
    except Exception as _e:
        log.warn(f"QC-018: check failed with exception: {_e}")

    # QC-017: carrier over-attribution. Calibrated 2026-05-08 against actual
    # Hilmar data where CMA CGM legitimately holds ~54% of quotes (CMA is
    # Hilmar's primary carrier). ERROR > 75% catches the CMA-boilerplate
    # false-positive bug (pre-fix it ran ~55% but now we know that level is
    # legit; only >75% indicates over-attribution). WARN > 65% triggers
    # a check-the-data prompt without crying wolf.
    try:
        from collections import Counter as _Counter
        _q = _Counter()
        for r in requests:
            c = r.get("carrier_quoted") or r.get("carrier_won")
            if c and (r.get("status") in ("WIN", "LOSS")) and (r.get("quoted") or r.get("status") == "WIN"):
                _q[c] += 1
        _tot = sum(_q.values())
        if _tot > 20:
            top_carrier, top_count = _q.most_common(1)[0]
            top_pct = top_count / _tot * 100
            if top_pct > 75:
                log.error(
                    f"QC-017: {top_carrier} has {top_count}/{_tot} quotes ({top_pct:.0f}%) > 75% — "
                    "over-attribution. Body scanner may be matching boilerplate / vessel-name "
                    "text. Investigate patch_carriers + parse_rate_table column accuracy."
                )
            elif top_pct > 65:
                log.warn(
                    f"QC-017: {top_carrier} holds {top_count}/{_tot} quotes ({top_pct:.0f}%) > 65%. "
                    "Sample 3 bodies with parse_rate_table to confirm carrier column matches."
                )
            else:
                log.ok(f"QC-017: top carrier {top_carrier} = {top_count}/{_tot} ({top_pct:.0f}%) — healthy spread")
    except Exception as _e:
        log.warn(f"QC-017: check failed with exception: {_e}")

    # QC-016: backup retention cap. backup.py prune step must catch BOTH naming
    # formats (tracking-data-v2_T...Z.json from backup.py + tracking-data-v2.YYYY-...
    # from qc_selfheal Phase 1). Pre-fix the period-format files grew unbounded.
    # If we ever exceed retention*2 the prune step is broken again.
    try:
        from pathlib import Path as _P
        _bdir = _P(__file__).resolve().parent.parent / "data-backups"
        if _bdir.exists():
            _all_bk = list(_bdir.glob("tracking-data-v2*.json"))
            # cfg not in scope here; use the same default as backup.py
            _retain = 14
            if len(_all_bk) > _retain * 2:
                log.error(
                    f"QC-016: {len(_all_bk)} backup files > 2x retention ({_retain*2}). "
                    "backup._list_snapshots glob is missing a naming format again."
                )
            elif len(_all_bk) > _retain + 5:
                log.warn(
                    f"QC-016: {len(_all_bk)} backups (retention {_retain}) — prune is "
                    "running but a few stragglers remain. Likely .stale- prefixed files."
                )
            else:
                log.ok(f"QC-016: {len(_all_bk)} backup file(s) (retention {_retain})")
    except Exception as _e:
        log.warn(f"QC-016: check failed with exception: {_e}")


# ─────────────────────────────────────────────────────────────────────
# Phase 7 — persist + QC result file
# ─────────────────────────────────────────────────────────────────────

def _carrier_coverage(requests):
    """Compute carrier-attribution coverage for QC report."""
    losses_quoted = [r for r in requests if r.get("status") == "LOSS" and r.get("quoted")]
    wins = [r for r in requests if r.get("status") == "WIN"]
    losses_with_carrier = [r for r in losses_quoted if r.get("carrier_quoted")]
    wins_with_carrier = [r for r in wins if r.get("carrier_won")]
    return {
        "ql_with_carrier": len(losses_with_carrier),
        "ql_total": len(losses_quoted),
        "ql_coverage_pct": round(len(losses_with_carrier) * 100 / max(1, len(losses_quoted)), 1),
        "win_with_carrier": len(wins_with_carrier),
        "win_total": len(wins),
        "win_coverage_pct": round(len(wins_with_carrier) * 100 / max(1, len(wins)), 1),
    }


def _trade_region_reconciliation(data):
    """Verify trade-region rollup matches summary KPIs."""
    requests = data.get("requests", []) or []
    summary = data.get("summary", {}) or {}
    try:
        regions = core.aggregate_trade_regions(requests)
    except Exception:
        return {"reconciled": False, "error": "aggregator failed"}
    sum_req = sum(m["requests"] for m in regions.values())
    sum_w   = sum(m["wins"] for m in regions.values())
    sum_ql  = sum(m["quoted_lost"] for m in regions.values())
    sum_nq  = sum(m["not_quoted"] for m in regions.values())
    reconciled = (
        sum_req == summary.get("total_entries", 0)
        and sum_w == summary.get("wins", 0)
        and sum_ql == summary.get("quoted_lost", 0)
        and sum_nq == summary.get("not_quoted", 0)
    )
    unmapped = next((m for m in regions.values() if m["region"] == "Unmapped"), None)
    return {
        "reconciled": reconciled,
        "regions": [
            {"region": m["region"], "requests": m["requests"], "wins": m["wins"],
             "ql": m["quoted_lost"], "nq": m["not_quoted"], "teu": m["teu_requested"],
             "win_rate": m["win_rate"]}
            for m in sorted(regions.values(), key=lambda m: m["teu_requested"], reverse=True)
        ],
        "unmapped_destinations": unmapped["destinations"] if unmapped else [],
        "summary_totals": {
            "requests": summary.get("total_entries", 0),
            "wins": summary.get("wins", 0),
            "ql": summary.get("quoted_lost", 0),
            "nq": summary.get("not_quoted", 0),
        },
    }


def _parser_sweep_audit(requests):
    """Pull parser-sweep history from per-record status_history entries."""
    sweep_count = 0
    sweep_records = []
    for r in requests:
        for h in (r.get("status_history") or []):
            reason = (h.get("reason") or "").lower()
            if "parser sweep" in reason or "backfilled" in reason:
                sweep_count += 1
                sweep_records.append({
                    "request_id": r.get("request_id"),
                    "lane": r.get("lane"),
                    "request_date": r.get("request_date"),
                    "at": h.get("at"),
                    "reason": h.get("reason"),
                })
                break
    return {
        "total_sweep_fixes": sweep_count,
        "records_touched": sweep_records[:30],  # cap for readability
    }



def _per_carrier_breakdown(requests):
    """Per-carrier W/L/TEU summary for the QC report."""
    try:
        carriers = core.aggregate_carriers(requests)
    except Exception:
        return []
    return [
        {
            "carrier": c, "quotes": cm.get("quotes", 0), "wins": cm.get("wins", 0),
            "losses": cm.get("losses", 0), "win_rate": cm.get("win_rate", 0),
            "teu_won": cm.get("teu_won", 0), "teu_lost": cm.get("teu_lost", 0),
            "lanes": cm.get("lanes_quoted", 0),
        }
        for c, cm in sorted(carriers.items(), key=lambda x: x[1].get("quotes", 0), reverse=True)
    ]


def phase_7_save(log: Log, data: dict, data_path: Path, result_path: Path):
    log.section("PHASE 7: PERSIST")
    data["qc"] = {
        "last_run": core.now_utc().isoformat(),
        "fixes_applied": len(log.fixes),
        "warnings": len(log.warnings),
        "errors": len(log.errors),
        "fix_log": log.fixes,
        "warning_log": log.warnings,
        "error_log": log.errors,
    }
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
        "fix_details": log.fixes,
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
        "carrier_coverage": _carrier_coverage(requests),
        "trade_region_reconciliation": _trade_region_reconciliation(data),
        "parser_sweep_audit": _parser_sweep_audit(requests),
        "per_carrier_breakdown": _per_carrier_breakdown(requests),
        "data_freshness": {
            "data_last_updated": data.get("last_updated"),
            "qc_run_at": core.now_utc().isoformat(),
        },
    }
    result_path.parent.mkdir(parents=True, exist_ok=True)
    with open(result_path, "w") as f:
        json.dump(result, f, indent=2)
    log.ok(f"Wrote {result_path}")
    return result


# ─────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=str(core.CONFIG_PATH))
    parser.add_argument("--no-backup", action="store_true")
    args = parser.parse_args()
    cfg = core.load_config(args.config)
    data_path = Path(cfg["paths"]["data"])
    schema_path = Path(cfg["paths"]["schema"])
    backups_dir = Path(cfg["paths"]["backups"])
    result_path = Path(cfg["paths"]["qc_result"])
    log = Log()
    if not data_path.exists():
        skeleton = {"version":cfg["version"],"client":cfg["client"]["name"],"contact":cfg["client"]["primary_contact"]["email"],"provider":cfg["provider"]["name"],"last_updated":core.now_utc().isoformat(),"requests":[],"summary":{"total_entries":0,"wins":0,"quoted_lost":0,"not_quoted":0,"pending_hilmar":0,"win_rate":0.0,"quote_rate":0.0,"teu_requested":0,"teu_won":0,"teu_quoted_lost":0,"teu_not_quoted":0,"teu_pending":0,"turnaround_entries":0,"turnaround_avg_biz_hours":0.0,"turnaround_avg_clock_hours":0.0},"lane_summary":{},"carrier_summary":{}}
        core.save_data(skeleton, data_path)
        log.fix(f"Created new empty data file at {data_path}")
    phase_1_files(log, data_path, schema_path)
    if not args.no_backup:
        dest = rotate_backup(data_path, backups_dir, keep=cfg["rules"].get("backup_retention_count", 14))
        log.ok(f"Backup -> {dest.name}")
    data = core.load_data(data_path)
    if not phase_2_structure(log, data):
        log.error("BLOCKING: structural integrity failure")
        result = {"status": "BLOCKED", "errors": log.errors}
        with open(result_path, "w") as f:
            json.dump(result, f, indent=2)
        return 1
    phase_3_entries(log, data)
    phase_4_duplicates(log, data)
    phase_5_summaries(log, data)
    phase_6_rules(log, data)
    result = phase_7_save(log, data, data_path, result_path)
    print("\n" + "=" * 60)
    print("QC SELF-HEAL COMPLETE")
    print("=" * 60)
    print(f"  Status:      {result['status']}")
    print(f"  Fixes:       {result['fixes']}")
    print(f"  Warnings:    {result['warnings']}")
    print(f"  Errors:      {result['errors']}")
    c = result["counts"]
    print(f"  {c['total']} entries: {c['wins']}W | {c['ql']} Q&L | {c['nq']} NQ | {c['pending']} P")
    print(f"  Win rate: {result['rates']['win_rate']}% | Quote rate: {result['rates']['quote_rate']}%")
    cc = result.get("carrier_coverage", {})
    print(f"  Carrier coverage: WIN {cc.get('win_with_carrier')}/{cc.get('win_total')} ({cc.get('win_coverage_pct')}%) | Q&L {cc.get('ql_with_carrier')}/{cc.get('ql_total')} ({cc.get('ql_coverage_pct')}%)")
    tr = result.get("trade_region_reconciliation", {})
    print(f"  Trade region reconciled: {tr.get('reconciled')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
