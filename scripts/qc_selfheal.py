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
import os
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

# Sentry observability — lazy import so QC works even without sentry-sdk.
try:
    import sentry_setup as _sentry
except ImportError:
    _sentry = None


def _extract_check_name(msg: str) -> str:
    """Pull the QC-NNN prefix from a log message so Sentry events
    group by check (one issue per check, not one per row)."""
    import re as _re
    m = _re.match(r"\s*(QC-\d+[a-z]?)", msg)
    return m.group(1) if m else "QC-unknown"


def _qc_phase_is_pre_patch() -> bool:
    """Is this qc_selfheal invocation the PRE-PATCH run in the pipeline?

    The pipeline runs qc_selfheal twice:
      1. pre-patch  — naturally has gaps (carriers / rates not yet filled)
      2. patch_carriers — backfills the gaps
      3. post-patch — represents the actual shipped state

    Pre-patch findings are EXPECTED to be incomplete. Surfacing them to
    Sentry creates false-positive alert noise (parser accuracy looks 14
    points lower than it really is, data completeness looks broken, etc).
    Only the post-patch run represents the actual quality of the shipped
    daily email — that's what should fire real-time alerts.

    Set HILMAR_QC_PHASE=pre-patch in the pipeline's pre-patch step to
    suppress Sentry event capture for THAT run. Post-patch + standalone
    runs default to firing events normally.
    """
    return os.environ.get("HILMAR_QC_PHASE", "").lower() == "pre-patch"


class Log:
    def __init__(self):
        self.fixes, self.warnings, self.errors = [], [], []
        # Snapshot the phase once at construction so subsequent env changes
        # mid-run don't surprise us
        self._pre_patch = _qc_phase_is_pre_patch()

    def fix(self, msg):
        self.fixes.append(msg); print(f"  🔧 FIX: {msg}")

    def warn(self, msg):
        self.warnings.append(msg); print(f"  ⚠️  WARN: {msg}")
        # Pre-patch QC findings are EXPECTED incomplete — patch_carriers
        # backfills them next. Suppress Sentry alerts on pre-patch run
        # to avoid false-positive noise. (Findings still log locally +
        # surface in the daily audit email, which is the audit channel.)
        if self._pre_patch:
            return
        # Fire Sentry warning for parser-accuracy + drift checks (high signal)
        if _sentry is not None and any(
            tag in msg for tag in ("QC-039", "QC-040", "QC-041", "PARSER ACCURACY")
        ):
            try:
                _sentry.capture_qc_warning(_extract_check_name(msg), msg)
            except Exception:
                pass

    def error(self, msg):
        self.errors.append(msg); print(f"  🔴 ERROR: {msg}")
        # Same pre-patch suppression — patch_carriers will run next and
        # fix the data gaps that pre-patch QC is flagging. Only post-patch
        # ERRORs represent the real shipped state.
        if self._pre_patch:
            return
        # Every ERROR-severity QC finding goes to Sentry — these gate the
        # daily pipeline ship and demand immediate operator attention.
        if _sentry is not None:
            try:
                _sentry.capture_qc_error(_extract_check_name(msg), msg)
            except Exception:
                pass

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

    # ─────────────────────────────────────────────────────────────────────
    # QC-021 through QC-025 added 2026-05-13 per Michael "all errors must be
    # fixed in the system not to happen again and added to qc and audit self
    # healing etc etc etc". One check per class of error this session hit.
    # ─────────────────────────────────────────────────────────────────────

    # QC-021: today's wrapper actually shipped the email.
    # Cause: 5/8 + 5/11 wrappers exited 255 between "Pipeline exit code: 0"
    # and the send step. Pipeline succeeded, no email shipped. This QC
    # parses the run-log for today's date and asserts that a successful
    # send line follows the pipeline section.
    try:
        from datetime import datetime as _dt
        _log_path = Path(__file__).resolve().parent.parent / "reports" / "run-log.txt"
        if _log_path.exists():
            _tail = _log_path.read_text(encoding="utf-8", errors="ignore")[-40000:]
            _today_us = _dt.now().strftime("%m/%d/%Y")  # 05/13/2026
            _today_iso = _dt.now().strftime("%Y-%m-%d")
            # Find today's wrapper header
            if _today_us in _tail or _today_iso in _tail:
                # Look for "Sent. request-id=" AFTER today's marker
                _idx = max(_tail.find(_today_us), _tail.find(_today_iso))
                _after = _tail[_idx:] if _idx >= 0 else _tail
                if "Sent. request-id=" in _after:
                    log.ok("QC-021: today's wrapper completed send step")
                elif "Pipeline exit code: 0" in _after:
                    log.warn(
                        "QC-021: today's pipeline completed BUT no 'Sent. request-id='"
                        " line follows. Wrapper may have exited before sending. Check"
                        " idempotency flag — if today's flag exists, email already went"
                        " out (manual fire or earlier scheduled fire). If not, send is"
                        " missing — investigate run-log."
                    )
                else:
                    log.warn("QC-021: today's wrapper started but pipeline never completed")
            else:
                # No fire today yet — only WARN on weekday afternoons
                _now_et = _dt.now(core.ET)
                if _now_et.weekday() < 5 and _now_et.hour >= 11:
                    log.warn(
                        f"QC-021: no wrapper fire for {_today_iso} in run-log "
                        f"(past 11 AM ET on a weekday — Cloud PC should have fired by now)"
                    )
                else:
                    log.ok(f"QC-021: no wrapper fire yet for {_today_iso} (off-hours)")
    except Exception as _e:
        log.warn(f"QC-021: check failed with exception: {_e}")

    # QC-022: distribution list invariants — must include michael.deitchman@idealx.us,
    # must be exactly 10 recipients, must NOT include external (non-ol-usa, non-idealx)
    # domains. Catches accidental edits to config.json that could leak emails.
    try:
        _cfg_path = Path(__file__).resolve().parent.parent / "config.json"
        if _cfg_path.exists():
            import json as _json
            _cfg = _json.loads(_cfg_path.read_text(encoding="utf-8"))
            _full = _cfg.get("distribution", {}).get("full_list", []) or []
            _missing = []
            if "michael.deitchman@idealx.us" not in [a.lower() for a in _full]:
                _missing.append("michael.deitchman@idealx.us")
            _external = [a for a in _full
                         if not (a.lower().endswith("@ol-usa.com") or a.lower().endswith("@idealx.us"))]
            _problems = []
            if _missing:
                _problems.append(f"missing: {_missing}")
            if _external:
                _problems.append(f"external domain(s): {_external}")
            if len(_full) < 8 or len(_full) > 12:
                _problems.append(f"unexpected count: {len(_full)}")
            if _problems:
                log.error("QC-022: distribution list invariant violations: " + "; ".join(_problems))
            else:
                log.ok(f"QC-022: distribution list OK ({len(_full)} recipients, idealx.us + ol-usa only)")
    except Exception as _e:
        log.warn(f"QC-022: check failed with exception: {_e}")

    # QC-023: MSAL token cache freshness. Tokens silently refresh up to ~90d
    # but the refresh-token TTL eventually expires and silent refresh fails,
    # causing send to error out. Warn at 60d so we have time to re-auth.
    try:
        _cache_paths = [
            Path(__file__).resolve().parent.parent / "secrets" / "token-cache.json",
            Path(__file__).resolve().parent.parent / "secrets" / "token-cache.bin",
        ]
        _found = next((p for p in _cache_paths if p.exists()), None)
        if _found:
            from datetime import datetime as _dt, timezone as _tz
            _age = (_dt.now(_tz.utc).timestamp() - _found.stat().st_mtime) / 86400.0
            if _age > 80:
                log.error(
                    f"QC-023: MSAL token cache is {_age:.0f}d old (>80d) — silent refresh "
                    "will fail soon. Re-auth: `python scripts/outlook_send.py auth`."
                )
            elif _age > 60:
                log.warn(
                    f"QC-023: MSAL token cache {_age:.0f}d old (>60d). Plan a re-auth "
                    "soon before silent refresh fails."
                )
            else:
                log.ok(f"QC-023: MSAL token cache fresh ({_age:.0f}d, file: {_found.name})")
        else:
            log.warn("QC-023: no MSAL token cache found — sends will fail")
    except Exception as _e:
        log.warn(f"QC-023: check failed with exception: {_e}")

    # QC-024: stage-path consistency. Multiple scripts read stage_emails — they
    # must all read the SAME file. Bug surfaced 5/13: gen_improvements_report
    # was reading legacy .jsonl while qc_selfheal read .txt, causing a phantom
    # "stage stale 192h" red flag. This QC asserts the two sources agree on
    # which file is current (compares mtime — the live file must be newer).
    try:
        _scripts_dir = Path(__file__).resolve().parent
        _txt = _scripts_dir / "stage_emails.txt"
        _jsonl = _scripts_dir / "stage_emails.jsonl"
        if _txt.exists() and _jsonl.exists():
            _txt_age = _txt.stat().st_mtime
            _jsonl_age = _jsonl.stat().st_mtime
            if _jsonl_age > _txt_age + 3600:  # .jsonl newer by 1+ hour
                log.warn(
                    "QC-024: stage_emails.jsonl is NEWER than .txt — refresh_stage may "
                    "have reverted to legacy format. Investigate."
                )
            else:
                log.ok(f"QC-024: stage path consistent (.txt is current source)")
        elif _txt.exists():
            log.ok("QC-024: stage_emails.txt is the sole source (no legacy .jsonl)")
        elif _jsonl.exists():
            log.error("QC-024: only legacy .jsonl exists — refresh_stage should write .txt")
        else:
            log.warn("QC-024: neither stage_emails.txt nor .jsonl found")
    except Exception as _e:
        log.warn(f"QC-024: check failed with exception: {_e}")

    # QC-037: ol-quote-tracker sync freshness + success. After each pipeline
    # fire, sync_to_quote_tracker.py POSTs entities (Hilmar, Lonny, carriers,
    # OL operators) to ol-quote-tracker's Turso client_intelligence table.
    # Audit log written to reports/quote-tracker-sync.log. This QC parses
    # the most-recent line — WARN if last sync >36h ago or last sync errored.
    try:
        from datetime import datetime as _dt
        _sync_log = Path(__file__).resolve().parent.parent / "reports" / "quote-tracker-sync.log"
        if not _sync_log.exists():
            log.warn("QC-037: quote-tracker-sync.log not found — sync_to_quote_tracker may never have run")
        else:
            _lines = _sync_log.read_text(encoding="utf-8", errors="ignore").splitlines()
            _last = next((ln for ln in reversed(_lines) if ln.strip()), None)
            if not _last:
                log.warn("QC-037: quote-tracker-sync.log empty")
            else:
                # Format: "<iso_ts> | entities=N ok=True upserted=N err=-"
                _ts_str = _last.split(" | ", 1)[0].strip()
                try:
                    _ts = _dt.fromisoformat(_ts_str.replace("Z", "+00:00"))
                    _age_h = (_dt.now(_ts.tzinfo) - _ts).total_seconds() / 3600.0
                except Exception:
                    _age_h = None
                if "ok=True" in _last:
                    if _age_h is None:
                        log.ok(f"QC-037: ol-quote-tracker sync succeeded ({_last[:80]}...)")
                    elif _age_h > 36:
                        log.warn(f"QC-037: last sync was {_age_h:.0f}h ago (>36h) — pipeline may not be running")
                    else:
                        log.ok(f"QC-037: ol-quote-tracker sync fresh ({_age_h:.1f}h ago)")
                elif "no APP_PASSWORD configured" in _last:
                    log.warn("QC-037: APP_PASSWORD not configured — sync skipped each fire. "
                             "Drop password in secrets/quote-tracker-pwd.txt to enable.")
                else:
                    log.warn(f"QC-037: last sync failed: {_last[:120]}")
    except Exception as _e:
        log.warn(f"QC-037: check failed with exception: {_e}")

    # QC-038: ol-quote-tracker reconciliation freshness + drift detection.
    # Per Michael 2026-05-16 "you see hilmar data is also on there as a good
    # check point for won bookings". Both systems independently ingest the
    # same OL inbox emails — Hilmar wins SHOULD match ol-quote-tracker wins
    # under clientCompany=Hilmar Ingredients. reconcile_with_quote_tracker.py
    # writes reports/quote-tracker-reconcile.log on every pipeline fire.
    # Format: "<iso_ts> | ok=True delta=N qt=M hilmar=K" or "<iso_ts> | error=... ok=False"
    # WARN if: missing log / stale (>36h) / drift >2 / fetch failed.
    # OK if: drift ∈ {-2..+2} (small drift OK — same-day intake differences).
    try:
        from datetime import datetime as _dt
        _rec_log = Path(__file__).resolve().parent.parent / "reports" / "quote-tracker-reconcile.log"
        if not _rec_log.exists():
            log.warn("QC-038: quote-tracker-reconcile.log not found — "
                     "reconcile_with_quote_tracker may never have run")
        else:
            _lines = _rec_log.read_text(encoding="utf-8", errors="ignore").splitlines()
            _last = next((ln for ln in reversed(_lines) if ln.strip()), None)
            if not _last:
                log.warn("QC-038: quote-tracker-reconcile.log empty")
            else:
                _ts_str = _last.split(" | ", 1)[0].strip()
                try:
                    _ts = _dt.fromisoformat(_ts_str.replace("Z", "+00:00"))
                    _age_h = (_dt.now(_ts.tzinfo) - _ts).total_seconds() / 3600.0
                except Exception:
                    _age_h = None
                if "ok=True" in _last:
                    # Extract drift delta from line if present
                    _delta = None
                    for _tok in _last.split():
                        if _tok.startswith("delta="):
                            try:
                                _delta = int(_tok.split("=", 1)[1])
                            except Exception:
                                pass
                            break
                    if _age_h is not None and _age_h > 36:
                        log.warn(f"QC-038: reconcile stale ({_age_h:.0f}h ago, >36h) — "
                                 "ol-quote-tracker checkpoint not updating")
                    elif _delta is not None and abs(_delta) > 2:
                        log.warn(f"QC-038: win count drift = {_delta:+d} (>2) — "
                                 "Hilmar vs ol-quote-tracker out of sync — "
                                 "see reports/reconcile-quote-tracker.json")
                    else:
                        _age_str = f"{_age_h:.1f}h ago" if _age_h is not None else "recent"
                        _drift_str = f"Δ={_delta:+d}" if _delta is not None else "no drift parsed"
                        log.ok(f"QC-038: ol-quote-tracker reconcile fresh ({_age_str}, {_drift_str})")
                elif "ok=False" in _last or "error=" in _last:
                    log.warn(f"QC-038: last reconcile errored: {_last[:160]}")
                else:
                    # No APP_PASSWORD case — reconcile exits 0 silently
                    log.warn(f"QC-038: reconcile inconclusive (likely no APP_PASSWORD): {_last[:120]}")
    except Exception as _e:
        log.warn(f"QC-038: check failed with exception: {_e}")

    # QC-039: PARSER ACCURACY GATE — per Michael 2026-05-17 "this parser and
    # your system have to run at minimum of 98 percent accuracy no matter
    # COST." Measures per-field % populated against applicability predicates
    # in src/hilmar/parser_accuracy.py. Computes:
    #   - Overall rate (equal-weight mean across fields)
    #   - Weighted rate (by applicable-row count)
    # ERROR if overall < 98% OR any CRITICAL field falls below 98%.
    # WARN if overall ≥ 98% but a non-critical field falls below.
    # Critical fields: origin, destination, lane, container_count,
    # teu_requested, carrier_quoted, carrier_won, ol_rate.
    try:
        import sys as _sys
        _src_dir = Path(__file__).resolve().parent.parent / "src"
        if str(_src_dir) not in _sys.path:
            _sys.path.insert(0, str(_src_dir))
        from hilmar.parser_accuracy import compute_accuracy, ACCURACY_THRESHOLD, CRITICAL_FIELDS
        _acc = compute_accuracy(data.get("requests", []))
        _pct = f"{_acc['overall_rate']:.1%}"
        _wpct = f"{_acc['weighted_rate']:.1%}"
        if _acc["critical_failing"]:
            log.error(
                f"QC-039: parser accuracy {_pct} (weighted {_wpct}) with "
                f"{len(_acc['critical_failing'])} CRITICAL field(s) below "
                f"{ACCURACY_THRESHOLD:.0%}: " +
                ", ".join(
                    f"{f}={_acc['field_stats'][f]['populated']}/"
                    f"{_acc['field_stats'][f]['applicable']} "
                    f"({_acc['field_stats'][f]['rate']:.1%})"
                    for f in _acc["critical_failing"]
                )
            )
        elif _acc["failing_fields"]:
            log.warn(
                f"QC-039: parser accuracy {_pct} overall (weighted {_wpct}); "
                f"{len(_acc['failing_fields'])} non-critical field(s) below "
                f"{ACCURACY_THRESHOLD:.0%}: " +
                ", ".join(
                    f"{f}={_acc['field_stats'][f]['rate']:.1%}"
                    for f in _acc["failing_fields"]
                )
            )
        elif _acc["overall_rate"] < ACCURACY_THRESHOLD:
            # All individual fields ≥ threshold but the equal-weight mean
            # falls below (e.g. one big-applicable field at 95% pulls down
            # several 100%s). Warn — investigate distribution.
            log.warn(
                f"QC-039: parser accuracy {_pct} below threshold "
                f"{ACCURACY_THRESHOLD:.0%} (weighted {_wpct}) — "
                "no single field failed but equal-weight mean is low"
            )
        else:
            log.ok(
                f"QC-039: parser accuracy {_pct} (weighted {_wpct}) "
                f"≥ {ACCURACY_THRESHOLD:.0%} on all {len([f for f in _acc['field_stats'].values() if not f.get('n_a')])} measured fields"
            )
    except Exception as _e:
        log.warn(f"QC-039: check failed with exception: {_e}")

    # QC-040: CROSS-FOLDER DRIFT — per Michael 2026-05-17 "never to allow
    # drift like this as standard." Detects when scripts/core.py and
    # src/hilmar/core.py disagree on enums/constants that should be aligned.
    # Documented intentional drift (e.g. 4-state Q&L/NQ in src/hilmar/ STRICT
    # vs 3-state in scripts/) goes through ALLOWED_CROSS_FOLDER_DRIFT below.
    # NEW drift triggers WARN — operator must either align or document.
    try:
        import sys as _sys
        _src_dir = Path(__file__).resolve().parent.parent / "src"
        if str(_src_dir) not in _sys.path:
            _sys.path.insert(0, str(_src_dir))
        from hilmar import core as _h_core

        # Allowed intentional drift — documented in src/hilmar/core.py
        # comments. Adding to this list requires a deliberate code review.
        _ALLOWED = {
            # The 3-state vs 4-state classifier divide is INTENTIONAL —
            # src/hilmar/ has VALID_STATUSES_STRICT for the 4-state form
            # and VALID_STATUSES_LEGACY for the 3-state form. The union
            # `VALID_STATUSES` accepts both. scripts/core.VALID_STATUSES
            # is the 3-state legacy form only. They are EQUAL on the
            # intersection {WIN, PENDING} and differ deliberately on
            # {LOSS, Q&L, NQ}.
            "VALID_STATUSES": True,
        }

        _drift_findings = []
        # Compare VALID_STATUSES — special-cased because of intentional drift
        scripts_statuses = set(getattr(core, "VALID_STATUSES", set()))
        hilmar_legacy = set(getattr(_h_core, "VALID_STATUSES_LEGACY", set()))
        if scripts_statuses != hilmar_legacy:
            _drift_findings.append(
                f"scripts/core.VALID_STATUSES {sorted(scripts_statuses)} != "
                f"src/hilmar/core.VALID_STATUSES_LEGACY {sorted(hilmar_legacy)} — "
                "the LEGACY view in src/hilmar/ must mirror scripts/ exactly"
            )

        # Compare LOSS_REASONS — strict equality required (no allowed drift)
        scripts_reasons = set(getattr(core, "LOSS_REASONS", set()))
        hilmar_reasons = set(getattr(_h_core, "LOSS_REASONS", set()))
        if scripts_reasons and hilmar_reasons:
            _missing_in_scripts = hilmar_reasons - scripts_reasons
            _missing_in_hilmar = scripts_reasons - hilmar_reasons
            if _missing_in_scripts or _missing_in_hilmar:
                _drift_findings.append(
                    f"LOSS_REASONS drift: only in src/hilmar/ = {sorted(_missing_in_scripts)}; "
                    f"only in scripts/ = {sorted(_missing_in_hilmar)}"
                )

        if _drift_findings:
            log.warn(
                f"QC-040: {len(_drift_findings)} undocumented cross-folder drift "
                f"finding(s) between scripts/core.py and src/hilmar/core.py: " +
                " | ".join(_drift_findings)
            )
        else:
            log.ok("QC-040: scripts/core ↔ src/hilmar/core enums aligned "
                   "(VALID_STATUSES via LEGACY view; LOSS_REASONS strict)")
    except Exception as _e:
        log.warn(f"QC-040: check failed with exception: {_e}")

    # QC-042: EMAIL-BODY DATA-URI GUARD — per Michael 2026-05-17 ("hilmar
    # logo not showing up"). Outlook blocks `<img src="data:image/...">`
    # references in HTML email bodies as a security measure, so any logo
    # or inline image embedded via data: URI renders as the broken-image
    # icon in the recipient's inbox. The fix is CID attachment (see
    # branding.logo_html_cid + outlook_send.py auto-attach with
    # contentId=hilmar-logo + isInline=true).
    #
    # This QC scans both email-bound HTML files (the tracker email body
    # AND the daily audit email body) for `data:image` substrings. ANY
    # occurrence = ERROR — it means someone reintroduced a data URI
    # somewhere and Outlook will block it next fire.
    #
    # Browser-opened artifacts (hilmar-dashboard.html, weekly-summary.html)
    # are EXEMPT — those are opened directly in browsers where data: URIs
    # render fine. Only files that go through outlook_send.py as the
    # message body are gated.
    try:
        _email_bodies = [
            Path(__file__).resolve().parent.parent / "reports" / "email-body.html",
            Path(__file__).resolve().parent.parent / "reports" / "improvements-report.html",
        ]
        _offenders = []
        for _path in _email_bodies:
            if not _path.exists():
                continue
            _text = _path.read_text(encoding="utf-8", errors="ignore")
            if "data:image" in _text:
                # Count occurrences for the error message
                _n = _text.count("data:image")
                _offenders.append(f"{_path.name} ({_n} data:image URI{'s' if _n > 1 else ''})")
        if _offenders:
            log.error(
                f"QC-042: {len(_offenders)} email-body file(s) contain data:image URIs "
                "(Outlook will block them — switch to cid:hilmar-logo via "
                "branding.logo_html_cid + outlook_send auto-attach): " +
                ", ".join(_offenders)
            )
        else:
            log.ok("QC-042: email bodies use CID logo references (no data:image URIs)")
    except Exception as _e:
        log.warn(f"QC-042: check failed with exception: {_e}")

    # QC-041: CLASSIFIER FORM CONSISTENCY — tracking-data-v2.json must use
    # ONE classifier form across all rows. Mixed strict (Q&L/NQ) and legacy
    # (LOSS) is a parser bug — at least one ingest pass mis-classified.
    try:
        import sys as _sys
        _src_dir = Path(__file__).resolve().parent.parent / "src"
        if str(_src_dir) not in _sys.path:
            _sys.path.insert(0, str(_src_dir))
        from hilmar.core import detect_classifier_form
        _form = detect_classifier_form(data.get("requests", []))
        if _form == "mixed":
            _strict_rows = [r for r in data.get("requests", [])
                             if r.get("status") in ("Q&L", "NQ")]
            _legacy_rows = [r for r in data.get("requests", [])
                             if r.get("status") == "LOSS"]
            log.error(
                f"QC-041: CLASSIFIER DRIFT — tracking-data-v2.json has "
                f"{len(_strict_rows)} STRICT rows (Q&L/NQ) and "
                f"{len(_legacy_rows)} LEGACY rows (LOSS). Mixed-form data "
                "is a parser bug. All ingest must write one consistent form."
            )
        elif _form == "empty":
            log.ok("QC-041: no LOSS/Q&L/NQ rows to evaluate classifier form")
        else:
            log.ok(f"QC-041: classifier form consistent ({_form.upper()})")
    except Exception as _e:
        log.warn(f"QC-041: check failed with exception: {_e}")

    # QC-034: tracking-data-v2.json schema validity gate. Added 2026-05-14
    # per best-practices batch. Calls core.validate_data_shape() and reports
    # structural issues (missing keys, wrong types, invalid status/loss_reason
    # enums). ERROR if invalid — pipeline shouldn't ship a malformed file.
    try:
        ok, issues = core.validate_data_shape(data, strict=False)
        if not ok:
            log.error(f"QC-034: data shape invalid: " + "; ".join(issues[:3])
                       + (f" + {len(issues)-3} more" if len(issues) > 3 else ""))
        else:
            log.ok("QC-034: tracking-data-v2.json shape valid (top-level keys + req fields)")
    except Exception as _e:
        log.warn(f"QC-034: check failed with exception: {_e}")

    # QC-035: stage file size cap. Without rotation, stage_emails.txt grows
    # unbounded. Warn at 5MB, ERROR at 20MB — those thresholds mean
    # `--rotate-stage-older-than 90` should be called.
    try:
        stage_path = Path(__file__).resolve().parent / "stage_emails.txt"
        if stage_path.exists():
            size_mb = stage_path.stat().st_size / 1_000_000
            if size_mb > 20:
                log.error(f"QC-035: stage_emails.txt is {size_mb:.1f}MB > 20MB — "
                          "run `refresh_stage.py --rotate-stage-older-than 90`")
            elif size_mb > 5:
                log.warn(f"QC-035: stage_emails.txt at {size_mb:.1f}MB — consider rotation soon")
            else:
                log.ok(f"QC-035: stage_emails.txt {size_mb:.2f}MB (well under cap)")
    except Exception as _e:
        log.warn(f"QC-035: check failed with exception: {_e}")

    # QC-036: unit test suite presence. The `tests/` folder should contain
    # at least one test_*.py file. Empty test folder means a regression net
    # got removed and won't catch future breakage.
    try:
        tests_dir = Path(__file__).resolve().parent.parent / "tests"
        if tests_dir.exists():
            test_files = list(tests_dir.glob("test_*.py"))
            if not test_files:
                log.error("QC-036: tests/ folder exists but no test_*.py files — "
                          "regression net is gone")
            elif len(test_files) < 3:
                log.warn(f"QC-036: only {len(test_files)} test files — "
                         "coverage thin (target ≥3 modules tested)")
            else:
                log.ok(f"QC-036: {len(test_files)} test files in tests/")
        else:
            log.warn("QC-036: no tests/ folder — regression net absent")
    except Exception as _e:
        log.warn(f"QC-036: check failed with exception: {_e}")

    # QC-033: brand logo presence + sanity. Per Michael 2026-05-14
    # "can you save this in your schema/data base and also add it to the
    # system fo rhilmar?" — wired branding.py module across all artifacts.
    # If logo file is missing or zero-bytes, the headers gracefully fall
    # back to emoji + text but Michael loses the brand identity. WARN.
    # Prefer SVG (vector) > PNG (raster). Either is acceptable.
    try:
        _brand_dir = Path(__file__).resolve().parent.parent / "assets" / "branding"
        _png = _brand_dir / "hilmar-logo.png"
        _svg = _brand_dir / "hilmar-logo.svg"
        if _svg.exists() and _svg.stat().st_size > 100:
            log.ok(f"QC-033: brand logo (SVG vector) present "
                   f"({_svg.stat().st_size:,} bytes)")
        elif _png.exists() and _png.stat().st_size > 100:
            # Verify it's actually a PNG by checking magic bytes
            with open(_png, "rb") as _f:
                _magic = _f.read(8)
            if _magic[:4] == b"\x89PNG":
                log.ok(f"QC-033: brand logo (PNG raster) present "
                       f"({_png.stat().st_size:,} bytes)")
            else:
                log.error(f"QC-033: assets/branding/hilmar-logo.png exists but "
                          f"isn't a valid PNG (magic bytes wrong)")
        else:
            log.warn("QC-033: no logo file at assets/branding/hilmar-logo.{svg,png} "
                     "— artifacts will fall back to emoji + text header")
    except Exception as _e:
        log.warn(f"QC-033: check failed with exception: {_e}")

    # QC-032: offline backup freshness. Daily backup runs at wrapper Step 4.9
    # to two targets (secondary OneDrive folder + local offline folder). If
    # the most recent backup in EITHER target is >36h old, defense-in-depth
    # is broken. WARN at >36h, ERROR at >72h.
    try:
        from datetime import datetime as _dt, timezone as _tz
        import os as _os
        import json as _j
        _cfg_path = Path(__file__).resolve().parent.parent / "config.json"
        _cfg_data = _j.loads(_cfg_path.read_text(encoding="utf-8")) if _cfg_path.exists() else {}
        _cfg = _cfg_data.get("backup", {}) or {}
        home = Path(_os.environ.get("USERPROFILE", _os.path.expanduser("~")))
        # Use raw config strings (may have %USERPROFILE% — expand)
        def _expand_p(p):
            return Path(_os.path.expandvars(p))
        targets = []
        sec = _cfg.get("secondary_onedrive_dir", "%USERPROFILE%/OneDrive - IdealX/HILMAR_BACKUPS")
        loc = _cfg.get("local_offline_dir", "%USERPROFILE%/hilmar-local-backups")
        for label, p in [("secondary", _expand_p(sec)), ("offline", _expand_p(loc))]:
            if p.exists():
                latest = max(
                    (f.stat().st_mtime for f in p.glob("hilmar-*.tar.gz")),
                    default=None,
                )
                if latest:
                    age_h = (_dt.now().timestamp() - latest) / 3600.0
                    targets.append((label, age_h, p))
                else:
                    targets.append((label, None, p))
            else:
                targets.append((label, "missing", p))

        ok_count = sum(1 for _, age, _p in targets if isinstance(age, (int, float)) and age <= 36)
        if ok_count == 2:
            log.ok(f"QC-032: backup fresh at both targets ({targets[0][1]:.1f}h secondary, "
                   f"{targets[1][1]:.1f}h offline)")
        elif ok_count == 1:
            log.warn(f"QC-032: backup fresh at only 1 of 2 targets — "
                     + "; ".join(f"{l}={'missing' if a == 'missing' else 'no archives' if a is None else f'{a:.1f}h'}"
                                  for l, a, _p in targets))
        else:
            log.error(f"QC-032: NO backup target is fresh — defense-in-depth broken: "
                      + "; ".join(f"{l}={'missing' if a == 'missing' else 'no archives' if a is None else f'{a:.1f}h'}"
                                   for l, a, _p in targets))
    except Exception as _e:
        log.warn(f"QC-032: check failed with exception: {_e}")

    # QC-030: transit-time data coverage. ETD + ETA together yield transit
    # days — Hilmar's carrier-comparison metric. We need both fields populated
    # on ≥80% of WIN/Q&L rows to produce a useful Carrier Rate + Transit
    # Ranges table in the daily rate intelligence section.
    try:
        _eligible = [r for r in requests
                     if r.get("status") in ("WIN", "LOSS")
                     and r.get("response_timestamp")]
        if _eligible:
            _with_both = sum(1 for r in _eligible
                             if r.get("etd_offered") and r.get("eta_offered"))
            _pct = _with_both * 100 / len(_eligible)
            if _pct < 70:
                log.error(
                    f"QC-030: transit-time pair (ETD+ETA) on only {_pct:.0f}% of "
                    f"rows — carrier transit-time analytics will be sparse"
                )
            elif _pct < 85:
                log.warn(f"QC-030: transit-time pair on {_pct:.0f}% (target 85%+)")
            else:
                log.ok(f"QC-030: transit-time pair on {_pct:.0f}% of {len(_eligible)} active rows")
    except Exception as _e:
        log.warn(f"QC-030: check failed with exception: {_e}")

    # QC-031: cross-project schema doc presence. Ensures
    # SHARED/client_intelligence/SCHEMA.md exists — rate-tracker integrators
    # need this to know the contract. Missing doc means future Claude
    # sessions / engineers won't know the schema and will diverge.
    try:
        import os as _os
        home = Path(_os.environ.get("USERPROFILE", _os.path.expanduser("~")))
        for c in [home / "OneDrive - IdealX" / "SHARED" / "client_intelligence",
                  home / "OneDrive" / "SHARED" / "client_intelligence"]:
            schema = c / "SCHEMA.md"
            if schema.exists():
                log.ok(f"QC-031: shared store SCHEMA.md present ({schema.stat().st_size:,} bytes)")
                break
        else:
            log.warn("QC-031: SHARED/client_intelligence/SCHEMA.md not found — "
                     "cross-project integrators will need to reverse-engineer the schema")
    except Exception as _e:
        log.warn(f"QC-031: check failed with exception: {_e}")

    # QC-028: rate-intelligence artifact freshness. The cross-project rate-
    # negotiation cheat sheet (gen_rate_intelligence.py) runs each fire and
    # writes reports/rate-intelligence.json. If this file is missing or
    # stale, the daily audit lost its negotiation section.
    try:
        from datetime import datetime as _dt, timezone as _tz, timedelta as _td
        _ri_path = Path(__file__).resolve().parent.parent / "reports" / "rate-intelligence.json"
        if not _ri_path.exists():
            log.warn("QC-028: reports/rate-intelligence.json missing — gen_rate_intelligence didn't run this fire")
        else:
            _age_h = (_dt.now().timestamp() - _ri_path.stat().st_mtime) / 3600.0
            if _age_h > 26:  # daily fire + 2h slack
                log.warn(f"QC-028: rate-intelligence.json is {_age_h:.0f}h stale")
            else:
                import json as _j
                _ri = _j.loads(_ri_path.read_text(encoding="utf-8"))
                log.ok(f"QC-028: rate intel fresh — {len(_ri.get('lane_cheat_sheet', []))} lanes, "
                       f"{len(_ri.get('carrier_cooling', []))} cooling, "
                       f"{len(_ri.get('lane_regression', []))} regressing")
    except Exception as _e:
        log.warn(f"QC-028: check failed with exception: {_e}")

    # QC-029: shared cross-project client_intelligence store integrity.
    # Hilmar pipeline exports to SHARED/client_intelligence/hilmar/ each fire.
    # If the export failed silently or the row count drops vs. tracker count,
    # the rate-tracker for cross-client insights is reading stale/incomplete
    # data. Surface the freshness + count delta here.
    try:
        import os as _os
        from datetime import datetime as _dt
        home = Path(_os.environ.get("USERPROFILE", _os.path.expanduser("~")))
        shared_candidates = [
            home / "OneDrive - IdealX" / "SHARED" / "client_intelligence" / "hilmar",
            home / "OneDrive" / "SHARED" / "client_intelligence" / "hilmar",
        ]
        shared = next((c for c in shared_candidates if c.exists()), None)
        if not shared:
            log.warn("QC-029: shared client_intelligence/hilmar/ folder not found — export likely never ran")
        else:
            _meta_path = shared / "_client_meta.json"
            if not _meta_path.exists():
                log.warn("QC-029: shared store missing _client_meta.json — schema broken")
            else:
                import json as _j
                _meta = _j.loads(_meta_path.read_text(encoding="utf-8"))
                _age_h = (_dt.now().timestamp() - _meta_path.stat().st_mtime) / 3600.0
                _shared_rows = _meta.get("row_count", 0)
                _local_rows = sum(1 for r in requests
                                  if r.get("status") in ("WIN", "LOSS", "PENDING"))
                _diff = _local_rows - _shared_rows
                if _age_h > 26:
                    log.warn(f"QC-029: shared store {_age_h:.0f}h stale — export didn't run today")
                elif abs(_diff) > 5:
                    log.warn(f"QC-029: shared store row count {_shared_rows} differs from local {_local_rows} (delta {_diff})")
                else:
                    log.ok(f"QC-029: shared store fresh ({_age_h:.1f}h, {_shared_rows} rows, {_meta.get('carrier_count')} carriers, {_meta.get('lane_count')} lanes)")
    except Exception as _e:
        log.warn(f"QC-029: check failed with exception: {_e}")

    # QC-027: data completeness across key fields. Per Michael 2026-05-13
    # "90 percent for all is the bare minimum". Measures REACHABLE rows
    # only — i.e. rows whose rate-response body is in current stage. WIN
    # rows whose only available body is the booking confirmation (data
    # lives in the PDF attachment) are tracked separately as "PDF-only"
    # so the gap is visible without breaking the 90% gate on parseable rows.
    try:
        _active = [r for r in requests if r.get("status") in ("WIN", "LOSS", "PENDING")
                   and r.get("response_timestamp")]
        # A row is "reachable" if at least one of its source_imids points
        # to an mbd_rate_response body. We approximate by checking that
        # the row has ETD or vessel populated — if patch_carriers' two
        # passes + cross-thread lookup couldn't fill either, the data is
        # in a PDF attachment we don't parse.
        _reachable = [r for r in _active if r.get("etd_offered") or r.get("vessel_voyage") or r.get("ol_rate")]
        _pdf_only = [r for r in _active if r not in _reachable]
        _checks = [
            ("etd_offered",   "ETD"),
            ("eta_offered",   "ETA"),
            ("vessel_voyage", "Vessel/Voyage"),
            ("ol_rate",       "Rate"),
            ("carrier_quoted","Carrier"),
            ("pol",           "POL"),
            ("pod",           "POD"),
        ]
        if _reachable:
            _problems = []
            _ok_count = 0
            for fld, label in _checks:
                _present = sum(1 for r in _reachable if r.get(fld))
                _pct = _present * 100 / len(_reachable)
                if _pct < 90:
                    _problems.append(f"{label}={_pct:.0f}% (ERROR <90%)")
                elif _pct < 95:
                    _problems.append(f"{label}={_pct:.0f}% (WARN 90-95%)")
                else:
                    _ok_count += 1
            _pdf_note = (f" — {len(_pdf_only)} PDF-only WIN(s) excluded (data in attachment)"
                         if _pdf_only else "")
            if any("ERROR" in p for p in _problems):
                log.error(f"QC-027: data completeness on {len(_reachable)} reachable rows — "
                          + "; ".join(_problems) + _pdf_note)
            elif _problems:
                log.warn(f"QC-027: data completeness on {len(_reachable)} reachable rows — "
                         + "; ".join(_problems) + _pdf_note)
            else:
                log.ok(f"QC-027: data completeness OK on {len(_reachable)} reachable rows "
                       f"({_ok_count}/{len(_checks)} fields ≥95%){_pdf_note}")
        # Track PDF-only rows separately so they're visible — they
        # need either PDF parsing or stage extension to surface
        if len(_pdf_only) > 5:
            log.warn(
                f"QC-027b: {len(_pdf_only)} WIN(s) have rate data only in PDF attachment "
                "— consider PDF parsing (pdfplumber) to lift completeness for confirmed bookings"
            )
    except Exception as _e:
        log.warn(f"QC-027: check failed with exception: {_e}")

    # QC-026: script-sync drift between OneDrive (live) and git repo (remote).
    # Per Michael 2026-05-13 "i need this to become a remote app as well so i
    # can use code from my phone and other laptops". The wrapper now git-pulls
    # latest scripts into OneDrive each fire. This QC verifies the two are
    # in sync — drift means either the pull failed, or a manual edit landed
    # in OneDrive that isn't in git yet (will be overwritten next pull).
    try:
        _live_scripts = Path(__file__).resolve().parent
        _repo_scripts = _live_scripts.parent / "hilmar-daily-routine" / "scripts"
        if _repo_scripts.exists():
            _diffs = []
            for live in _live_scripts.glob("*.py"):
                repo = _repo_scripts / live.name
                if not repo.exists():
                    _diffs.append(f"{live.name} (not in repo)")
                    continue
                if live.read_bytes() != repo.read_bytes():
                    _diffs.append(live.name)
            if len(_diffs) > 3:
                log.warn(
                    f"QC-026: {len(_diffs)} scripts drift between OneDrive and repo: "
                    + ", ".join(_diffs[:5])
                    + (f" + {len(_diffs)-5} more" if len(_diffs) > 5 else "")
                    + " — next wrapper git-pull will overwrite local edits"
                )
            elif _diffs:
                log.ok(
                    f"QC-026: {len(_diffs)} script(s) differ — minor drift, "
                    f"will sync on next fire: {', '.join(_diffs)}"
                )
            else:
                log.ok("QC-026: scripts in OneDrive match git repo (no drift)")
        else:
            log.warn("QC-026: hilmar-daily-routine/scripts not found — git remote sync disabled")
    except Exception as _e:
        log.warn(f"QC-026: check failed with exception: {_e}")

    # QC-025: per-day flag integrity. Each daily send writes a line to
    # reports/sent-YYYY-MM-DD.flag. Multiple lines are normal (manual fire +
    # scheduled fire same day) BUT >3 lines in one day means something is
    # looping or auto-retrying — investigate.
    try:
        from datetime import datetime as _dt
        _today = _dt.now().strftime("%Y-%m-%d")
        _flag = Path(__file__).resolve().parent.parent / "reports" / f"sent-{_today}.flag"
        if _flag.exists():
            _lines = [ln for ln in _flag.read_text(encoding="utf-8").splitlines()
                      if ln.strip().startswith("Sent ")]
            if len(_lines) > 5:
                log.error(
                    f"QC-025: {len(_lines)} 'Sent' entries in today's flag — "
                    "something is looping. Check scheduled tasks + manual fires."
                )
            elif len(_lines) > 3:
                log.warn(
                    f"QC-025: {len(_lines)} 'Sent' entries in today's flag — "
                    "more than expected (manual + scheduled = 2 max usually)."
                )
            else:
                log.ok(f"QC-025: today's flag has {len(_lines)} send entries (healthy)")
        else:
            log.ok(f"QC-025: today's flag not present (no send yet — normal pre-10AM)")
    except Exception as _e:
        log.warn(f"QC-025: check failed with exception: {_e}")

    # QC-020: stale-NQ display cutoff is enforced AND aggregates remain whole.
    # Per Michael 2026-05-13: 'after 2 weeks with items that have no reply..
    # just remove them from system that says not quoted but keep it on the
    # talley of volumes that hilmar moves for rate negotiation'.
    # Two assertions:
    #   (a) every NQ row in reports/email-body.html has request_date within
    #       the last 14 days (the display cutoff)
    #   (b) summary.not_quoted + summary.teu_not_quoted still count ALL NQ
    #       rows regardless of age (volume tally preserved for rate-neg)
    try:
        from datetime import datetime as _dt, timedelta as _td
        import re as _re
        NQ_WINDOW = 14
        _cutoff = (_dt.now(core.ET).date() - _td(days=NQ_WINDOW)).isoformat()
        # Aggregate check (b)
        _all_nq = [r for r in requests
                   if r.get("status") == "LOSS" and (r.get("loss_reason") or "") == "NO_RESPONSE"]
        _summary_nq = (data.get("summary") or {}).get("not_quoted", 0)
        if _summary_nq != len(_all_nq):
            log.error(
                f"QC-020b: summary.not_quoted={_summary_nq} but raw count={len(_all_nq)}. "
                "Display-window filter leaked into the aggregate — volume tally broken."
            )
        else:
            log.ok(f"QC-020b: NQ aggregate intact ({len(_all_nq)} total — full tally for rate-neg)")
        # Display check (a)
        _body_path = Path(__file__).resolve().parent.parent / "reports" / "email-body.html"
        if _body_path.exists():
            _body = _body_path.read_text(encoding="utf-8")
            # NQ table rows have dates in YYYY-MM-DD format in the first <td>
            # Extract dates that appear between the NQ section header and its </table>
            _nq_section = _body.split("Not Quoted")[1] if "Not Quoted" in _body else ""
            _nq_dates = _re.findall(r"\b(202\d-\d{2}-\d{2})\b", _nq_section[:8000])
            _stale = [d for d in _nq_dates if d < _cutoff]
            if _stale:
                log.warn(
                    f"QC-020a: NQ section has {len(_stale)} rows older than {NQ_WINDOW}d "
                    f"({_stale[0]}..{_stale[-1]}). Display cutoff not applied."
                )
            else:
                log.ok(f"QC-020a: NQ section display rows all within last {NQ_WINDOW} days")
    except Exception as _e:
        log.warn(f"QC-020: check failed with exception: {_e}")

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
    # Initialize Sentry early so any failure in subsequent setup is captured.
    if _sentry is not None:
        try:
            _sentry.init(component="qc_selfheal")
        except Exception:
            pass
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
