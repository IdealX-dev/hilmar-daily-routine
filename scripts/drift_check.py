"""
drift_check.py — pre-QC sanity gates for tracking-data-v2.json

Runs BEFORE qc_selfheal.py in the daily pipeline to catch systemic drift before
it gets baked into reports + emails.

Phases:
  1. imid uniqueness — every internetMessageId attached to at most one request
  2. matcher quality — for each OL-reply imid currently attached to request A,
     is there a closer-in-time same-destination NQ record B? if yes → drift
     (today's bug: pass-1 FIFO matcher attached OL replies to wrong/older requests)
  3. quote-rate sanity — if quote_rate < threshold, FAIL the gate (block sends)
  4. NQ schema sanity — every NQ must have null response_timestamp + status=LOSS + loss_reason=NO_RESPONSE
  5. WIN schema sanity — every WIN must have carrier_won set
  6. covered-flag honor — any record with lonny_covered=True must be LOSS/OTHER (audit, not auto-heal)

Self-heals where safe (phase 1, 2). Reports where unsafe (phase 3-6).
Writes reports/drift-result.json and exits non-zero on FAIL.

Usage:
    python3 scripts/drift_check.py [--config config.json] [--auto-heal] [--dry]
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import core

# Quote-rate threshold below which the pipeline should HALT (configurable)
DEFAULT_QUOTE_RATE_FLOOR = 80.0

# Matcher quality: an OL reply attached to request A is "drifted" if a same-dest
# NQ record B exists where |B.request_ts − OL.response_ts| is materially smaller
# than |A.request_ts − OL.response_ts|. We use a 4× ratio guard to avoid
# false positives on close ties.
MATCHER_DRIFT_RATIO = 4.0
MATCHER_DRIFT_MIN_GAP_HOURS = 2.0  # only flag if difference > 2h
# Phase-2 matcher drift is a LOW-CONFIDENCE proximity heuristic flagged for
# operator review (it can't auto-heal — reattaching on a guess could corrupt a
# correct attachment). A handful of candidates is a WARN (surface + review);
# only a systemic count HALTS the client send. Before 2026-06-16 ANY single
# candidate blocked the whole daily email, which stranded the fire for days
# (HILMAR-DAILY-TRACKER-6) with no way to auto-recover. Genuine data-corruption
# gates (quote_rate, dup imids, NQ schema) still block on the first occurrence.
MATCHER_DRIFT_FAIL_FLOOR = 3


def norm_dest(s: str | None) -> str:
    import re
    s = (s or "").strip().lower()
    s = re.sub(r"\([^)]*\)", "", s).strip()
    return s.replace("'", "").replace('"', "")


def phase1_imid_uniqueness(data: dict, log: dict, auto_heal: bool) -> None:
    """Each internetMessageId should be attached to exactly one record."""
    imid_to_records: dict[str, list[dict]] = {}
    for r in data.get("requests", []):
        for imid in (r.get("source_imids") or []):
            imid_to_records.setdefault(imid, []).append(r)

    duplicates = {imid: recs for imid, recs in imid_to_records.items() if len(recs) > 1}
    log["phase1"] = {
        "duplicates_found": len(duplicates),
        "details": [],
    }
    for imid, recs in duplicates.items():
        log["phase1"]["details"].append({
            "imid": imid,
            "request_ids": [r.get("request_id") for r in recs],
            "destinations": [r.get("destination") for r in recs],
        })
        if auto_heal:
            # Keep the first; remove from the rest
            keep, *drop = recs
            for r in drop:
                r["source_imids"] = [i for i in r["source_imids"] if i != imid]
                log["phase1"].setdefault("healed", []).append(
                    f"removed dup imid from {r.get('request_id')} ({r.get('destination')})"
                )


def phase2_matcher_quality(data: dict, log: dict, auto_heal: bool) -> None:
    """For each OL reply currently attached, is there a better same-dest match
    among NQ records?

    Today's bug: FIFO matcher attached OL Apr-27 Xingang reply to Apr-22 Xingang
    request (5 days off) while Apr-27 Xingang request (17 min off) was starved.
    """
    # Index: imid → (current_record, current_delta_h)
    # We need response_timestamp for each attached OL reply
    drifted = []

    # Build an index of all NQ records by destination
    nq_by_dest: dict[str, list[dict]] = {}
    for r in data.get("requests", []):
        if r.get("status") == "LOSS" and not r.get("quoted"):
            d = norm_dest(r.get("destination"))
            nq_by_dest.setdefault(d, []).append(r)

    # For each currently-quoted record with a response_timestamp, check if any
    # NQ record on same destination has a CLOSER request_timestamp to that response
    for r in data.get("requests", []):
        if not r.get("quoted") or not r.get("response_timestamp"):
            continue
        if r.get("manual_locked"):
            continue  # skip explicitly-locked records
        resp_dt = core.parse_iso(r.get("response_timestamp"))
        req_dt = core.parse_iso(r.get("request_timestamp"))
        if not resp_dt or not req_dt:
            continue
        current_delta_h = abs((resp_dt - req_dt).total_seconds() / 3600.0)

        dest = norm_dest(r.get("destination"))
        for nq in nq_by_dest.get(dest, []):
            nq_req_dt = core.parse_iso(nq.get("request_timestamp"))
            if not nq_req_dt:
                continue
            nq_delta_h = abs((resp_dt - nq_req_dt).total_seconds() / 3600.0)
            if (
                nq_delta_h < current_delta_h / MATCHER_DRIFT_RATIO
                and (current_delta_h - nq_delta_h) > MATCHER_DRIFT_MIN_GAP_HOURS
            ):
                drifted.append({
                    "current_request_id": r.get("request_id"),
                    "current_destination": r.get("destination"),
                    "current_request_ts": r.get("request_timestamp"),
                    "current_delta_h": round(current_delta_h, 2),
                    "better_request_id": nq.get("request_id"),
                    "better_request_ts": nq.get("request_timestamp"),
                    "better_delta_h": round(nq_delta_h, 2),
                    "response_timestamp": r.get("response_timestamp"),
                    "ratio": round(current_delta_h / max(nq_delta_h, 0.01), 1),
                })
                break  # one drift per current record is enough

    log["phase2"] = {
        "matcher_drift_count": len(drifted),
        "details": drifted,
    }
    # Auto-heal phase 2 is risky because we'd need to re-extract rate/carrier from
    # the OL reply body. Report only — surface to operator for manual fix.


def phase3_quote_rate_floor(data: dict, log: dict, floor: float) -> bool:
    """Returns True if FAIL (quote rate too low)."""
    summary = data.get("summary") or {}
    qr = summary.get("quote_rate")
    log["phase3"] = {
        "quote_rate": qr,
        "floor": floor,
        "fail": qr is not None and qr < floor,
    }
    return log["phase3"]["fail"]


def phase4_nq_schema(data: dict, log: dict, auto_heal: bool) -> None:
    """Every NQ must have null response_timestamp + status=LOSS + loss_reason=NO_RESPONSE."""
    issues = []
    for r in data.get("requests", []):
        if r.get("status") == "LOSS" and not r.get("quoted"):
            if r.get("response_timestamp"):
                issues.append({"request_id": r.get("request_id"), "issue": "NQ but has response_timestamp"})
            if r.get("loss_reason") not in ("NO_RESPONSE", None):
                issues.append({"request_id": r.get("request_id"), "issue": f"NQ but loss_reason={r.get('loss_reason')}"})
            if auto_heal and r.get("loss_reason") in (None,):
                r["loss_reason"] = "NO_RESPONSE"
                r["reason_detail"] = r.get("reason_detail") or "OL did not respond in email — escalate"
    log["phase4"] = {"issues": issues}


def phase5_win_schema(data: dict, log: dict) -> None:
    """Every WIN should have carrier_won. Report only."""
    missing = []
    for r in data.get("requests", []):
        if r.get("status") == "WIN" and not r.get("carrier_won"):
            missing.append({"request_id": r.get("request_id"), "destination": r.get("destination")})
    log["phase5"] = {"wins_missing_carrier": len(missing), "details": missing}


def phase6_covered_honor(data: dict, log: dict) -> None:
    """Any lonny_covered=True must be LOSS / COVERED (or legacy OTHER) AND
    quoted=True. The COVERED loss_reason was added 2026-04+; pre-existing
    rows may still carry OTHER (kept as a valid honor target for back-
    compat). The quoted=True requirement was added 2026-06-02 (track 03
    finding C-2): a COVERED row without quoted=True bucketed as NQ and
    silently dropped out of win-rate."""
    _VALID_COVERED_REASONS = ("COVERED", "OTHER")
    issues = []
    for r in data.get("requests", []):
        if not r.get("lonny_covered"):
            continue
        if (r.get("status") != "LOSS"
                or r.get("loss_reason") not in _VALID_COVERED_REASONS
                or r.get("quoted") is not True):
            issues.append({
                "request_id": r.get("request_id"),
                "destination": r.get("destination"),
                "current_status": r.get("status"),
                "current_loss_reason": r.get("loss_reason"),
                "current_quoted": r.get("quoted"),
            })
    log["phase6"] = {"covered_honor_issues": issues}


def run(config_path: str, auto_heal: bool = False, dry: bool = False) -> int:
    cfg = core.load_config(config_path)
    data_path = Path(cfg["paths"]["data"])
    data = json.loads(data_path.read_text(encoding="utf-8"))

    log = {
        "ran_at": datetime.now(timezone.utc).isoformat(),
        "data_path": str(data_path),
        "auto_heal": auto_heal,
        "dry_run": dry,
    }

    phase1_imid_uniqueness(data, log, auto_heal)
    phase2_matcher_quality(data, log, auto_heal)
    phase3_fail = phase3_quote_rate_floor(data, log, DEFAULT_QUOTE_RATE_FLOOR)
    phase4_nq_schema(data, log, auto_heal)
    phase5_win_schema(data, log)
    phase6_covered_honor(data, log)

    # Compose verdict.
    # FAIL reasons HALT the pipeline (block daily send). Reserve only for
    # things that would corrupt the daily numbers if shipped:
    #   - quote_rate floor breach (data quality crash)
    #   - phase 1 dup imids without auto-heal (would double-count)
    #   - phase 2 matcher drift ONLY when systemic (>= MATCHER_DRIFT_FAIL_FLOOR);
    #     1–2 low-confidence candidates WARN + surface for operator reattach
    #     (a single review-flag must not black out the whole daily send)
    #   - phase 4 NQ schema without auto-heal (status confusion)
    #
    # WARN reasons are surfaced + logged but DON'T halt the pipeline. These
    # are mostly known structural data gaps (orchestrator.md intent for
    # phases 5 + 6 was "audit / report"):
    #   - phase 5 WINs missing carrier_won (qc_selfheal QC-002 also only WARNs;
    #     edge-case bookings like MDOLX260469 'DRAFT RATED FOR HILMAR' have
    #     no parseable carrier in subject/body)
    #   - phase 6 covered-flag honor (audit-only per orchestrator.md)
    fail_reasons = []
    warn_reasons = []
    if phase3_fail:
        fail_reasons.append(f"quote_rate {log['phase3']['quote_rate']}% < floor {log['phase3']['floor']}%")
    if log["phase1"]["duplicates_found"] > 0 and not auto_heal:
        fail_reasons.append(f"{log['phase1']['duplicates_found']} duplicate imid attachments")
    _drift = log["phase2"]["matcher_drift_count"]
    if _drift >= MATCHER_DRIFT_FAIL_FLOOR:
        # Systemic matcher breakage — block the send (numbers likely corrupted).
        fail_reasons.append(
            f"{_drift} matcher drift candidates (>= {MATCHER_DRIFT_FAIL_FLOOR}) — "
            "systemic; review and reattach")
    elif _drift > 0:
        # 1–2 low-confidence candidates: surface for operator review (in
        # drift-result.json + the audit) but DON'T black out the whole daily
        # email. Phase 2 is report-only and can't auto-heal, so a hard block
        # here is unrecoverable without manual intervention (HILMAR-DAILY-
        # TRACKER-6: 4 fires blocked on a single candidate, 2026-06-16).
        warn_reasons.append(
            f"{_drift} matcher drift candidate(s) — review + reattach "
            "(audit-only; see reports/drift-result.json)")
    if log["phase4"]["issues"] and not auto_heal:
        fail_reasons.append(f"{len(log['phase4']['issues'])} NQ schema issues")
    if log["phase5"]["wins_missing_carrier"] > 0:
        warn_reasons.append(f"{log['phase5']['wins_missing_carrier']} WINs missing carrier_won (audit-only — see qc_selfheal QC-002)")
    if log["phase6"]["covered_honor_issues"]:
        warn_reasons.append(f"{len(log['phase6']['covered_honor_issues'])} lonny_covered records not in LOSS/OTHER (audit-only)")

    log["fail_reasons"] = fail_reasons
    log["warn_reasons"] = warn_reasons
    log["status"] = "FAIL" if fail_reasons else ("WARN" if warn_reasons else "PASS")

    # Write report
    out_path = Path(cfg["paths"].get("reports") or (ROOT / "reports")) / "drift-result.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(log, indent=2, default=str), encoding="utf-8")

    # Persist data if any auto-heal happened (and not --dry)
    if auto_heal and not dry and (
        log["phase1"].get("healed") or log["phase4"].get("issues")
    ):
        data_path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
        log["persisted"] = True
    else:
        log["persisted"] = False

    # Console summary
    print("=" * 60)
    print(f"DRIFT CHECK — {log['status']}")
    print("=" * 60)
    print(f"  Phase 1 (imid uniqueness):     {log['phase1']['duplicates_found']} duplicates")
    print(f"  Phase 2 (matcher quality):     {log['phase2']['matcher_drift_count']} drift candidates")
    print(f"  Phase 3 (quote rate floor):    {log['phase3']['quote_rate']}% (floor {log['phase3']['floor']}%) — {'FAIL' if phase3_fail else 'OK'}")
    print(f"  Phase 4 (NQ schema):           {len(log['phase4']['issues'])} issues")
    print(f"  Phase 5 (WIN schema):          {log['phase5']['wins_missing_carrier']} WINs missing carrier")
    print(f"  Phase 6 (covered-flag honor):  {len(log['phase6']['covered_honor_issues'])} issues")
    if log.get("warn_reasons"):
        print()
        print("WARN REASONS (logged, pipeline continues):")
        for wr in log["warn_reasons"]:
            print(f"  - {wr}")

    if fail_reasons:
        print()
        print("FAIL REASONS:")
        for fr in fail_reasons:
            print(f"  - {fr}")
    print(f"\nReport: {out_path}")

    return 1 if log["status"] == "FAIL" else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(ROOT / "config.json"))
    ap.add_argument("--auto-heal", action="store_true",
                    help="Auto-heal phase-1 (dup imids) and phase-4 (NQ schema). "
                         "Phase-2 (matcher drift) is report-only — needs operator review.")
    ap.add_argument("--dry", action="store_true",
                    help="Don't persist auto-heal changes")
    args = ap.parse_args()
    return run(args.config, auto_heal=args.auto_heal, dry=args.dry)


if __name__ == "__main__":
    sys.exit(main())
