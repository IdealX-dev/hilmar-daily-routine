"""Tests for hilmar.qc — 7-phase self-heal engine.

Runs against a tmp copy of golden_day.json so the canonical fixture stays
untouched. Verifies behavior parity with the original Cowork-mode QC engine:
data on disk gets healed, summary rebuilt, qc-result.json written.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from conftest import GOLDEN_DAY, SCHEMA_PATH  # pytest puts tests/ on sys.path

from hilmar import qc


@pytest.fixture
def workspace(tmp_path: Path) -> dict[str, Path]:
    """Tmp dir with the golden fixture cloned to tracking-data-v2.json."""
    data = tmp_path / "tracking-data-v2.json"
    shutil.copy2(GOLDEN_DAY, data)
    backups = tmp_path / "data-backups"
    reports = tmp_path / "reports"
    reports.mkdir()
    return {
        "tmp": tmp_path,
        "data": data,
        "schema": SCHEMA_PATH,
        "backups": backups,
        "result": reports / "qc-result.json",
    }


def test_run_qc_clean_status_on_golden_fixture(workspace):
    result, log = qc.run_qc(
        workspace["data"], workspace["schema"],
        workspace["backups"], workspace["result"],
    )
    assert result["status"] == "CLEAN", f"errors: {log.errors}"
    assert log.errors == []


def test_run_qc_writes_result_file(workspace):
    qc.run_qc(
        workspace["data"], workspace["schema"],
        workspace["backups"], workspace["result"],
    )
    assert workspace["result"].exists()
    res = json.loads(workspace["result"].read_text())
    assert res["status"] in ("CLEAN", "HAS_ERRORS")
    assert "counts" in res and "teu" in res and "rates" in res


def test_run_qc_creates_backup(workspace):
    qc.run_qc(
        workspace["data"], workspace["schema"],
        workspace["backups"], workspace["result"],
    )
    backups = list(workspace["backups"].glob("tracking-data-v2.*.json"))
    assert len(backups) == 1


def test_run_qc_skip_backup_flag(workspace):
    qc.run_qc(
        workspace["data"], workspace["schema"],
        workspace["backups"], workspace["result"],
        do_backup=False,
    )
    assert not workspace["backups"].exists() or not list(workspace["backups"].glob("*.json"))


def test_run_qc_rebuilds_summary_lane_carrier(workspace):
    qc.run_qc(
        workspace["data"], workspace["schema"],
        workspace["backups"], workspace["result"],
    )
    healed = json.loads(workspace["data"].read_text())
    # Wins are stable through QC (has_send=True + mdolx_ref both present).
    assert healed["summary"]["wins"] == 2
    # The fixture PENDING entry (response 2026-04-14) is well past the 24h
    # window when QC runs in 2026-04-26+, so decide_status re-ages it to
    # LOSS+quoted. After QC: 2 WIN, 1 NQ, 2 Q&L, 0 PENDING.
    assert healed["summary"]["pending_hilmar"] == 0
    assert healed["summary"]["quoted_lost"] == 2
    assert healed["summary"]["not_quoted"] == 1
    # Lanes + carriers always rebuilt from raw — list forms are the
    # only storage shape post-Phase-A.
    assert healed["lanes"], "lanes list must be populated"
    assert healed["carriers"], "carriers list must be populated"
    # Phase-A invariant: dict-form duplicates are gone.
    assert "lane_summary" not in healed
    assert "carrier_summary" not in healed
    # data["qc"] block dropped — qc-result.json is the canonical surface.
    assert "qc" not in healed


def test_run_qc_idempotent(workspace):
    """Running QC twice in a row should not change the data on the second pass
    (beyond the qc.last_run timestamp). We don't assert on backup-file count
    because rotate_backup uses minute-precision timestamps and same-minute
    runs overwrite — that's by design and tested separately."""
    qc.run_qc(
        workspace["data"], workspace["schema"],
        workspace["backups"], workspace["result"],
    )
    after_first = json.loads(workspace["data"].read_text())

    qc.run_qc(
        workspace["data"], workspace["schema"],
        workspace["backups"], workspace["result"],
    )
    after_second = json.loads(workspace["data"].read_text())

    # Counts stable across runs.
    for key in ("wins", "pending_hilmar", "quoted_lost", "not_quoted",
                "teu_requested", "teu_won"):
        assert after_first["summary"][key] == after_second["summary"][key], \
            f"{key} drifted between runs"
    assert len(after_first["requests"]) == len(after_second["requests"])
    # Statuses stable per-request.
    by_id_first = {r["request_id"]: r["status"] for r in after_first["requests"]}
    by_id_second = {r["request_id"]: r["status"] for r in after_second["requests"]}
    assert by_id_first == by_id_second


def test_run_qc_creates_skeleton_when_no_data(tmp_path):
    """When tracking-data-v2.json is absent, QC seeds an empty skeleton."""
    data = tmp_path / "tracking-data-v2.json"  # does NOT exist
    result_path = tmp_path / "reports" / "qc-result.json"
    backups = tmp_path / "backups"

    result, log = qc.run_qc(
        data, SCHEMA_PATH, backups, result_path,
    )
    assert data.exists()
    assert result["status"] == "CLEAN"
    assert result["counts"]["total"] == 0


def test_run_qc_blocks_on_corrupt_structure(tmp_path):
    """Garbage data → BLOCKED status, returns early without crashing."""
    data = tmp_path / "tracking-data-v2.json"
    data.write_text(json.dumps({"version": "x", "summary": {}, "requests": "not-an-array"}))
    backups = tmp_path / "backups"
    result_path = tmp_path / "reports" / "qc-result.json"

    result, log = qc.run_qc(
        data, SCHEMA_PATH, backups, result_path,
    )
    assert result["status"] == "BLOCKED"
    assert any("'requests' is not an array" in e for e in log.errors)


def test_phase_3_dedups_carrier_won_from_carrier_quoted(workspace):
    """If a WIN is missing carrier_won but has carrier_quoted, QC fills it."""
    data = json.loads(workspace["data"].read_text())
    # Force the WIN to drop carrier_won so phase_3 has work to do
    for r in data["requests"]:
        if r["status"] == "WIN":
            r.pop("carrier_won", None)
            r["carrier_quoted"] = "Maersk"
    workspace["data"].write_text(json.dumps(data))

    qc.run_qc(
        workspace["data"], workspace["schema"],
        workspace["backups"], workspace["result"],
    )
    healed = json.loads(workspace["data"].read_text())
    wins = [r for r in healed["requests"] if r["status"] == "WIN"]
    assert all(r.get("carrier_won") for r in wins), "phase_3 should backfill carrier_won"


def test_phase_3_heals_standalone_win_containers_from_subject(workspace):
    """Existing stand_* rows in production landed with containers=None /
    teu_won=0 because ingest pre-fix didn't mine the booking subject.
    Phase 3 now recovers the spec from subject and mirrors teu_won
    from teu_requested for these synthetic WINs.
    """
    data = json.loads(workspace["data"].read_text())
    data["requests"].append({
        "request_id": "stand_260420",
        "status": "WIN",
        "quoted": True, "has_send": True,
        "mdolx_ref": "260420",
        "carrier_quoted": "ONE",
        "carrier_won": "ONE",
        "containers": None,
        "container_count": 0,
        "teu_requested": 0,
        "teu_won": 0,
        "subject": "MDOLX260420_UPDATED ETA BOOKING CONFIRMATION// HILMAR 1X20'DV Oakland to Bangkok// ONE: RICGE7217600",
        "lane": "Oakland → Bangkok",
        "destination": "Bangkok",
        "request_date": "2026-04-16",
    })
    workspace["data"].write_text(json.dumps(data))

    qc.run_qc(
        workspace["data"], workspace["schema"],
        workspace["backups"], workspace["result"],
    )
    healed = json.loads(workspace["data"].read_text())
    row = next(r for r in healed["requests"] if r["request_id"] == "stand_260420")
    assert row["containers"] is not None and "20" in row["containers"]
    assert row["teu_requested"] == 1
    # Standalone-WIN heal: teu_won mirrors teu_requested when
    # both were zero on disk.
    assert row["teu_won"] == 1


def test_phase_3_heals_carrier_won_via_subject_token(workspace):
    """Last-resort heal: WIN row with neither carrier_won NOR carrier_quoted
    must still get filled if the subject contains a recognisable carrier token.

    Backstop for the 2026-04-27 Apr 14 Tokyo case (stand_260433): the booking
    email was outside today's Graph search window so no fresh standalone got
    generated to clobber the stale None via _RECOMPUTED_FIELDS — the heal has
    to act on the persisted row itself. Also exercises canonicalisation:
    'CMA' should normalise to 'CMA CGM', not stay as the raw 'Cma' token."""
    data = json.loads(workspace["data"].read_text())
    # Convert one WIN into the standalone-shape edge: no carrier_quoted,
    # no carrier_won, but a subject that mentions CMA.
    for r in data["requests"]:
        if r["status"] == "WIN":
            r.pop("carrier_won", None)
            r["carrier_quoted"] = None
            r["subject"] = "MDOLX260433_UPDATED BOOKING CONFIRMATION// HILMAR 3X40'RF Oakland to Tokyo// CMA: NAM8433582"
            r["reason_detail"] = "Standalone booking — MDOLX260433"
            break
    workspace["data"].write_text(json.dumps(data))

    qc.run_qc(
        workspace["data"], workspace["schema"],
        workspace["backups"], workspace["result"],
    )
    healed = json.loads(workspace["data"].read_text())
    target = next(r for r in healed["requests"] if "260433" in (r.get("subject") or ""))
    assert target.get("carrier_won") == "CMA CGM", (
        f"heal should normalise CMA → 'CMA CGM', got {target.get('carrier_won')!r}"
    )
    # carrier_quoted should also have been filled (was None).
    assert target.get("carrier_quoted") == "CMA CGM"


def test_phase_5_overwrites_stale_list_forms(workspace):
    """phase_5 is the SOLE writer of summary/lanes/carriers (Phase A
    invariant). Pre-seed stale list entries from a hypothetical earlier
    write; phase_5 must overwrite them with fresh aggregates derived
    from the request list, not preserve the stale data."""
    data = json.loads(workspace["data"].read_text())
    data["lanes"] = [{"lane": "STALE → STALE", "wins": 999, "requests": 999}]
    data["carriers"] = [{"carrier": "STALE_CARRIER", "wins": 999, "quotes": 999}]
    workspace["data"].write_text(json.dumps(data))

    qc.run_qc(
        workspace["data"], workspace["schema"],
        workspace["backups"], workspace["result"],
    )
    healed = json.loads(workspace["data"].read_text())

    lane_names = {ln.get("lane") for ln in healed["lanes"]}
    carrier_names = {c.get("carrier") for c in healed["carriers"]}
    assert "STALE → STALE" not in lane_names, "phase_5 left stale lanes list"
    assert "STALE_CARRIER" not in carrier_names, "phase_5 left stale carriers list"


def test_phase_5_drops_dead_dict_forms_and_qc_block(workspace):
    """Phase A migration cleanup: pre-existing data files persisted
    lane_summary/carrier_summary dicts and a data["qc"] block. phase_5
    pops these on first post-Phase-A run (idempotent on subsequent
    runs since they're already gone)."""
    data = json.loads(workspace["data"].read_text())
    data["lane_summary"] = {"OLD → LANE": {"wins": 1}}
    data["carrier_summary"] = {"OLD_CARRIER": {"wins": 1}}
    data["qc"] = {"last_run": "2026-04-01", "fix_log": ["legacy"]}
    data["mdolx_bookings"] = ["legacy"]
    data["escalations_sent"] = {"legacy": True}
    data["metadata"] = {"legacy": True}
    workspace["data"].write_text(json.dumps(data))

    qc.run_qc(
        workspace["data"], workspace["schema"],
        workspace["backups"], workspace["result"],
    )
    healed = json.loads(workspace["data"].read_text())

    for dead in ("lane_summary", "carrier_summary", "qc",
                 "mdolx_bookings", "escalations_sent", "metadata"):
        assert dead not in healed, f"phase_5 should have dropped {dead!r}"


def test_phase_3_heal_does_not_invent_carrier_when_subject_has_no_token(workspace):
    """Defensive: when no carrier token is present, heal leaves carrier_won
    None and QC-002 fires correctly. Prevents over-eager fabrication."""
    data = json.loads(workspace["data"].read_text())
    for r in data["requests"]:
        if r["status"] == "WIN":
            r.pop("carrier_won", None)
            r["carrier_quoted"] = None
            r["subject"] = "MDOLX260999_BOOKING CONFIRMATION// HILMAR Oakland to Tokyo"
            r["reason_detail"] = "Standalone booking — MDOLX260999"
            break
    workspace["data"].write_text(json.dumps(data))

    result, _log = qc.run_qc(
        workspace["data"], workspace["schema"],
        workspace["backups"], workspace["result"],
    )
    healed = json.loads(workspace["data"].read_text())
    target = next(r for r in healed["requests"] if "260999" in (r.get("subject") or ""))
    assert target.get("carrier_won") is None, "heal must not fabricate a carrier"


# ─────────────────────────────────────────────────────────────────────
# M3.9 — Phases 8/9/10 (parser regression / ingest gap / schema drift)
# ─────────────────────────────────────────────────────────────────────


def test_phase_10_schema_drift_adds_missing_fields(workspace):
    """Inject a request that's missing a field present on others; phase 10
    fills it as None on the deficient row + logs a selfheal_actions entry.
    """
    data = json.loads(workspace["data"].read_text())
    if data["requests"]:
        data["requests"][0]["new_field_added_by_ingest"] = "v1"
    workspace["data"].write_text(json.dumps(data))

    qc.run_qc(
        workspace["data"], workspace["schema"],
        workspace["backups"], workspace["result"],
    )

    healed = json.loads(workspace["data"].read_text())
    for r in healed["requests"]:
        assert "new_field_added_by_ingest" in r, "phase 10 should add the missing key"
    actions = healed.get("selfheal_actions") or []
    drift = [a for a in actions if a.get("kind") == "schema_drift"]
    assert drift, "phase 10 should log a schema_drift selfheal_action"
    assert "new_field_added_by_ingest" in drift[-1]["fields_added"]


def test_phase_10_no_action_when_schema_consistent():
    """If every request has the same keys, phase 10 records no drift action."""
    # Build a tiny synthetic consistent doc — every request has the same
    # keys. golden_day has natural drift so we can't reuse it here.
    data = {
        "requests": [
            {"request_id": "a", "status": "WIN"},
            {"request_id": "b", "status": "Q&L"},
        ],
    }
    log = qc.Log()
    qc.phase_10_schema_drift(log, data)

    actions = data.get("selfheal_actions") or []
    drift = [a for a in actions if a.get("kind") == "schema_drift"]
    assert drift == [], f"unexpected schema_drift on consistent data: {drift}"


def test_phase_9_ingest_gap_flagged_when_today_below_threshold(workspace):
    """With baseline P50=10 and today=2 requests, phase 9 must flag a gap."""
    from datetime import datetime, timezone
    today = datetime(2026, 4, 26, 12, 0, tzinfo=timezone.utc)
    today_iso = today.replace(hour=8, minute=0).isoformat()

    data = json.loads(workspace["data"].read_text())
    # Trim to 2 today-stamped requests so today_count = 2 < 0.4 * 10 = 4.
    data["requests"] = data["requests"][:2]
    for r in data["requests"]:
        r["request_timestamp"] = today_iso
    data["baselines"] = {"ingest_volume_p50": 10}
    workspace["data"].write_text(json.dumps(data))

    log = qc.Log()
    healed = json.loads(workspace["data"].read_text())
    qc.phase_9_ingest_gap(log, healed, today=today)

    actions = [a for a in (healed.get("selfheal_actions") or [])
               if a.get("kind") == "ingest_gap"]
    assert actions, "phase 9 should flag the gap"
    assert actions[-1]["today_count"] == 2
    assert actions[-1]["baseline_p50"] == 10


def test_phase_9_no_gap_when_today_normal(workspace):
    """today_count >= threshold — no flag."""
    from datetime import datetime, timezone
    today = datetime(2026, 4, 26, 12, 0, tzinfo=timezone.utc)
    today_iso = today.replace(hour=8, minute=0).isoformat()

    data = json.loads(workspace["data"].read_text())
    data["requests"] = data["requests"][:1]  # 1 today
    if data["requests"]:
        data["requests"][0]["request_timestamp"] = today_iso
    data["baselines"] = {"ingest_volume_p50": 2}  # 0.4*2 = 0.8, today=1 >= 0.8
    workspace["data"].write_text(json.dumps(data))

    log = qc.Log()
    healed = json.loads(workspace["data"].read_text())
    qc.phase_9_ingest_gap(log, healed, today=today)

    actions = [a for a in (healed.get("selfheal_actions") or [])
               if a.get("kind") == "ingest_gap"]
    assert actions == [], "phase 9 false-positive on normal volume"


def test_phase_9_skipped_without_baseline(workspace):
    """No baseline → no flag, no error."""
    data = json.loads(workspace["data"].read_text())
    data.pop("baselines", None)
    workspace["data"].write_text(json.dumps(data))

    log = qc.Log()
    healed = json.loads(workspace["data"].read_text())
    qc.phase_9_ingest_gap(log, healed)

    actions = healed.get("selfheal_actions") or []
    assert all(a.get("kind") != "ingest_gap" for a in actions)


def test_phase_9_skipped_when_baseline_below_floor(workspace):
    """Sparse baselines (P50 < 1.0) make any 0-day fire the threshold —
    suppress the alert until the rolling window holds enough volume.

    The 2026-04-29 production run had baseline P50=0.5 (14 calendar
    days dominated by weekends + ramp-up), and a 0-request Wednesday
    fired the alert spuriously.
    """
    from datetime import datetime, timezone
    today = datetime(2026, 4, 29, 12, 0, tzinfo=timezone.utc)

    data = json.loads(workspace["data"].read_text())
    data["requests"] = []  # today_count = 0
    data["baselines"] = {"ingest_volume_p50": 0.5}
    workspace["data"].write_text(json.dumps(data))

    log = qc.Log()
    healed = json.loads(workspace["data"].read_text())
    qc.phase_9_ingest_gap(log, healed, today=today)

    actions = [a for a in (healed.get("selfheal_actions") or [])
               if a.get("kind") == "ingest_gap"]
    assert not actions, (
        f"phase 9 should suppress alert when baseline is below floor; got {actions}"
    )


def test_phase_8_parser_regression_flags_when_above_baseline(workspace):
    """Build a scenario where ETA-offered miss-rate is 100%, baseline is 10%
    (ratio 10× > 2× threshold) → phase 8 flags it.
    """
    from datetime import datetime, timezone
    today = datetime(2026, 4, 26, 12, 0, tzinfo=timezone.utc)
    recent = today.replace(hour=10).isoformat()

    data = json.loads(workspace["data"].read_text())
    # Force every quoted row to have a parsed rate body but missing eta_offered.
    # ol_rate populated → row is "applicable" under phase 8's
    # has-rate-body gate; eta_offered=None → it's a "miss".
    for r in data["requests"]:
        r["quoted"] = True
        r["ol_rate"] = 3500.0
        r["eta_offered"] = None
        r["request_timestamp"] = recent
    data["baselines"] = {"parser_miss_rate": {"eta_offered": 10.0}}
    workspace["data"].write_text(json.dumps(data))

    log = qc.Log()
    healed = json.loads(workspace["data"].read_text())
    qc.phase_8_parser_regression(log, healed, today=today)

    actions = [a for a in (healed.get("selfheal_actions") or [])
               if a.get("kind") == "parser_regression"]
    assert actions, "phase 8 should flag the regression"
    flagged_parsers = {f["parser"] for f in actions[-1]["flagged"]}
    assert "eta_offered" in flagged_parsers


def test_phase_8_excludes_pure_mdolx_wins(workspace):
    """Regression guard: WINs synthesized from an MDOLX booking with no
    rate-quote email body must NOT be counted as parser misses for
    eta_offered / vessel_voyage / transshipment. They have quoted=True
    (the booking implies a quote happened) but no body to parse.

    The 2026-04-29 production run flagged a 11.1% spurious miss-rate on
    all three fields because 4 pure-MDOLX WINs (3 stand_*, 1 promoted)
    were being counted as applicable. Phase 8 now gates the trio on
    `ol_rate is not None`.
    """
    from datetime import datetime, timezone
    today = datetime(2026, 4, 26, 12, 0, tzinfo=timezone.utc)
    recent = today.replace(hour=10).isoformat()

    data = json.loads(workspace["data"].read_text())
    # 4 pure-MDOLX WINs (no ol_rate, no body-parsed fields) plus a
    # baseline that would otherwise easily flag them.
    data["requests"] = [
        {
            "request_id": f"stand_{i:06d}",
            "status": "WIN",
            "quoted": True,
            "has_send": True,
            "mdolx_ref": str(260000 + i),
            "carrier_quoted": "ONE",
            "carrier_won": "ONE",
            "ol_rate": None,
            "eta_offered": None,
            "vessel_voyage": None,
            "transshipment": None,
            "request_timestamp": recent,
            "response_timestamp": recent,
        }
        for i in range(4)
    ]
    data["baselines"] = {"parser_miss_rate": {
        "eta_offered": 1.0, "vessel_voyage": 1.0, "transshipment": 1.0,
    }}
    workspace["data"].write_text(json.dumps(data))

    log = qc.Log()
    healed = json.loads(workspace["data"].read_text())
    qc.phase_8_parser_regression(log, healed, today=today)

    actions = [a for a in (healed.get("selfheal_actions") or [])
               if a.get("kind") == "parser_regression"]
    assert not actions, (
        f"phase 8 should not flag pure-MDOLX WINs as parser misses; got {actions}"
    )


def test_phase_8_no_flag_when_no_baseline(workspace):
    """Without baselines.parser_miss_rate, phase 8 just records nothing."""
    data = json.loads(workspace["data"].read_text())
    data.pop("baselines", None)
    workspace["data"].write_text(json.dumps(data))

    log = qc.Log()
    healed = json.loads(workspace["data"].read_text())
    qc.phase_8_parser_regression(log, healed)

    actions = healed.get("selfheal_actions") or []
    assert all(a.get("kind") != "parser_regression" for a in actions)


def test_phases_8_9_10_idempotent(workspace):
    """Running run_qc twice must not double-log selfheal_actions for the
    same condition. (Phase 10 in particular should detect that fields are
    already filled and add no new entry.)"""
    # Inject schema drift once so phase 10 fires on first run.
    data = json.loads(workspace["data"].read_text())
    if data["requests"]:
        data["requests"][0]["once_drifted"] = "v"
    workspace["data"].write_text(json.dumps(data))

    qc.run_qc(workspace["data"], workspace["schema"],
              workspace["backups"], workspace["result"])
    after_first = json.loads(workspace["data"].read_text())
    drift_count_1 = sum(
        1 for a in (after_first.get("selfheal_actions") or [])
        if a.get("kind") == "schema_drift"
    )

    qc.run_qc(workspace["data"], workspace["schema"],
              workspace["backups"], workspace["result"])
    after_second = json.loads(workspace["data"].read_text())
    drift_count_2 = sum(
        1 for a in (after_second.get("selfheal_actions") or [])
        if a.get("kind") == "schema_drift"
    )

    # The second run shouldn't add a new schema_drift entry — fields
    # are now consistent.
    assert drift_count_2 == drift_count_1, (
        f"phase 10 not idempotent: drift entries went {drift_count_1} -> {drift_count_2}"
    )


def test_qc_007_skips_awaiting_mdolx_pending(workspace, capsys):
    """QC-007 (PENDING past 24h) was written for the original PENDING
    semantics — quoted-within-Lonny's-24h-window. Reading B (commit
    ee392d5) added two new PENDING sub-states (AWAITING_MDOLX,
    MDOLX_NO_SEND) where exceeding 24h is EXPECTED, not an error.

    This test seeds an AWAITING_MDOLX row whose response_timestamp is
    72h old and asserts QC-007 does NOT fire on it. Without the skip,
    every daily run reports HAS_ERRORS until the row matures."""
    from datetime import datetime, timedelta, timezone
    data = json.loads(workspace["data"].read_text())
    # Pick the existing PENDING row from golden_day, mutate to AWAITING_MDOLX
    # with a 72h-old response_timestamp so QC-007's 24h check would otherwise
    # fire.
    # 120h aged so the row is stale under BOTH the original 24h check and
    # the new 48h+Friday-rule check, regardless of which weekday pytest
    # runs on. 72h was previously safe but breaks on Mondays under the
    # Friday rule (Fri quote, Mon morning = still inside biz window).
    aged_ts = (datetime.now(timezone.utc) - timedelta(hours=120)).isoformat()
    seeded = False
    for r in data["requests"]:
        if r["status"] == "PENDING":
            r["loss_reason"] = "AWAITING_MDOLX"
            r["response_timestamp"] = aged_ts
            r["has_send"] = True
            seeded = True
            break
    assert seeded, "fixture must have at least one PENDING row to mutate"
    workspace["data"].write_text(json.dumps(data))

    result, _log = qc.run_qc(
        workspace["data"], workspace["schema"],
        workspace["backups"], workspace["result"],
    )
    qc007_errors = [e for e in result.get("error_details", []) if "QC-007" in e]
    assert qc007_errors == [], (
        f"QC-007 should skip AWAITING_MDOLX rows; got: {qc007_errors}"
    )


def test_qc_007_does_not_fire_inside_business_window(monkeypatch, workspace):
    """The 2026-06-01 bug: a Friday-quoted PENDING row 30h old at Monday
    morning. Under the new 48h+Friday-rule classifier (PR #14), this row
    legitimately stays PENDING — the weekend doesn't count. QC-007's
    hardcoded 24h check fired anyway and surfaced 2 false-positive ERRORs
    in Monday's audit email. Fixed 2026-06-01.
    """
    from datetime import datetime, timedelta, timezone
    # Lock "now" so this test is deterministic regardless of when it runs.
    fixed_now = datetime(2026, 6, 1, 14, 0, tzinfo=timezone.utc)   # Mon 10:00 ET
    # Quote sent late Friday afternoon, ~30h before "now".
    fri_quote = datetime(2026, 5, 29, 20, 0, tzinfo=timezone.utc)  # Fri ~16:00 ET
    monkeypatch.setattr(qc.core, "now_utc", lambda: fixed_now)

    data = json.loads(workspace["data"].read_text())
    seeded = False
    for r in data["requests"]:
        if r["status"] == "PENDING":
            r["loss_reason"] = None
            r["response_timestamp"] = fri_quote.isoformat()
            r["has_send"] = False
            r["mdolx_ref"] = None
            r["quoted"] = True
            r["status"] = "PENDING"
            seeded = True
            break
    assert seeded
    workspace["data"].write_text(json.dumps(data))

    log = qc.Log()
    test_data = json.loads(workspace["data"].read_text())
    qc.phase_6_rules(log, test_data)
    qc007_errors = [e for e in log.errors if "QC-007" in e]
    assert not qc007_errors, (
        f"QC-007 must NOT fire on Friday-quoted rows still inside the "
        f"weekend-aware window (deadline Tuesday 18:00 ET). "
        f"Fired: {qc007_errors}"
    )


def test_qc_007_fires_when_friday_window_expires_tuesday_evening(monkeypatch, workspace):
    """Defensive complement: a Friday-quoted row IS stale once we're past
    Tuesday 18:00 ET. Per Michael 2026-06-04 — 'by Tuesday' deadline.
    The Friday rule extends the window — it doesn't eliminate it."""
    from datetime import datetime, timedelta, timezone
    fixed_now = datetime(2026, 6, 2, 23, 0, tzinfo=timezone.utc)   # Tue 19:00 ET
    fri_quote = datetime(2026, 5, 29, 20, 0, tzinfo=timezone.utc)  # Fri ~16:00 ET
    monkeypatch.setattr(qc.core, "now_utc", lambda: fixed_now)

    data = json.loads(workspace["data"].read_text())
    seeded = False
    for r in data["requests"]:
        if r["status"] == "PENDING":
            r["loss_reason"] = None
            r["response_timestamp"] = fri_quote.isoformat()
            r["has_send"] = False
            r["mdolx_ref"] = None
            r["quoted"] = True
            r["status"] = "PENDING"
            seeded = True
            break
    assert seeded
    workspace["data"].write_text(json.dumps(data))

    log = qc.Log()
    test_data = json.loads(workspace["data"].read_text())
    qc.phase_6_rules(log, test_data)
    qc007_errors = [e for e in log.errors if "QC-007" in e]
    assert qc007_errors, (
        "QC-007 must fire on Friday-quoted rows past Tuesday 18:00 ET "
        "(weekend-aware window has elapsed)."
    )


@pytest.mark.parametrize("quote_iso,now_iso,should_fire", [
    # Normal weekday: quoted Tue 8 AM ET, viewed Thu 9 AM (49h → STALE @ 24h)
    ("2026-04-21T12:00:00Z", "2026-04-23T13:00:00Z", True),
    # Normal weekday: quoted Tue 8 AM, viewed Wed 11 AM (27h → STALE @ 24h)
    ("2026-04-21T12:00:00Z", "2026-04-22T15:00:00Z", True),
    # Normal weekday: quoted Tue 8 AM, viewed Tue 11 AM (3h → INSIDE)
    ("2026-04-21T12:00:00Z", "2026-04-21T15:00:00Z", False),
    # Friday/weekend rule: Fri 4 PM ET, viewed Mon 7 PM (75h wall) → INSIDE
    # because new deadline is Tuesday 18:00 ET (Michael 2026-06-04)
    ("2026-04-24T20:00:00Z", "2026-04-27T23:00:00Z", False),
    # Friday/weekend rule: Fri 4 PM ET, viewed Tue 7 PM (99h wall) → STALE
    ("2026-04-24T20:00:00Z", "2026-04-28T23:00:00Z", True),
    # Saturday quote: Sat 10 AM ET → Tue 18 ET deadline; viewed Tue 17 ET → INSIDE
    ("2026-04-25T14:00:00Z", "2026-04-28T21:00:00Z", False),
])
def test_qc_007_matches_decide_status_business_stale(
    monkeypatch, workspace, quote_iso, now_iso, should_fire,
):
    """Parity guard — for any quote timestamp + viewing time, QC-007's
    fire decision MUST match what decide_status would do (i.e. is the
    row still PENDING or should it have aged to Q&L). The hardcoded 24h
    check was the original drift; this parametrized check is the lock
    against the next drift."""
    from datetime import datetime, timezone
    fixed_now = datetime.fromisoformat(now_iso.replace("Z", "+00:00"))
    monkeypatch.setattr(qc.core, "now_utc", lambda: fixed_now)

    data = json.loads(workspace["data"].read_text())
    seeded = False
    for r in data["requests"]:
        if r["status"] == "PENDING":
            r["loss_reason"] = None
            r["response_timestamp"] = quote_iso
            r["has_send"] = False
            r["mdolx_ref"] = None
            r["quoted"] = True
            r["status"] = "PENDING"
            seeded = True
            break
    assert seeded
    workspace["data"].write_text(json.dumps(data))

    log = qc.Log()
    test_data = json.loads(workspace["data"].read_text())
    qc.phase_6_rules(log, test_data)
    qc007_fired = any("QC-007" in e for e in log.errors)
    assert qc007_fired == should_fire, (
        f"QC-007 fire decision drifted from decide_status. "
        f"Quote at {quote_iso}, now={now_iso}: "
        f"expected fire={should_fire}, got fire={qc007_fired}"
    )


def test_qc_007_still_fires_on_unaged_original_pending(workspace):
    """Defensive: QC-007 must still catch the original failure mode it
    was designed for — a row that's PENDING with no Reading-B sub-state
    but whose response_timestamp is past 24h. That's a real state-machine
    bug (decide_status should have aged it to Q&L)."""
    from datetime import datetime, timedelta, timezone
    data = json.loads(workspace["data"].read_text())
    # 120h aged so the row is stale under BOTH the original 24h check and
    # the new 48h+Friday-rule check, regardless of which weekday pytest
    # runs on. 72h was previously safe but breaks on Mondays under the
    # Friday rule (Fri quote, Mon morning = still inside biz window).
    aged_ts = (datetime.now(timezone.utc) - timedelta(hours=120)).isoformat()
    # Mutate a row that classifies under Reading B's 24h window — quoted=True,
    # has_send=False, mdolx_ref=None — but force its STATUS to PENDING and
    # leave loss_reason=None so the AWAITING_MDOLX skip doesn't apply.
    seeded = False
    for r in data["requests"]:
        if r["status"] == "PENDING":
            r["loss_reason"] = None  # explicitly not a Reading-B sub-state
            r["response_timestamp"] = aged_ts
            r["has_send"] = False
            r["mdolx_ref"] = None
            r["quoted"] = True
            # Force status post-decide_status by setting it after-the-fact
            # via the persisted file; phase_3's decide_status will reclassify
            # it to Q&L (the bug QC-007 is designed to catch usually means
            # decide_status DIDN'T run, but the test here just confirms QC-007
            # is still active for non-Reading-B rows).
            r["status"] = "PENDING"
            seeded = True
            break
    assert seeded
    workspace["data"].write_text(json.dumps(data))

    # Run only phase_6 directly to bypass phase_3's reclassification —
    # we want to test phase_6 itself, not the full pipeline.
    log = qc.Log()
    test_data = json.loads(workspace["data"].read_text())
    qc.phase_6_rules(log, test_data)
    qc007_errors = [e for e in log.errors if "QC-007" in e]
    assert qc007_errors, "QC-007 must still fire on non-AWAITING_MDOLX aged-PENDING rows"


def test_phase_3_canonicalizes_carrier_names(workspace):
    """body_parser._find_carrier returns title-cased tokens like 'Cma'
    or 'Maersk'. Without canonicalization, a row with carrier_won='Cma'
    buckets separately from one with carrier_won='CMA CGM' in the
    scoreboard (cf. stand_260460 vs stand_260433 split, 2026-04-27 audit).
    phase_3's sweep maps every carrier_won/carrier_quoted through
    core.normalize_carrier so the scoreboard collapses to a single
    bucket per steamship line."""
    data = json.loads(workspace["data"].read_text())
    # Pick the first WIN and force its carrier_won to the un-normalized form.
    seeded = False
    for r in data["requests"]:
        if r["status"] == "WIN":
            r["carrier_won"] = "Cma"
            r["carrier_quoted"] = "Cma"
            seeded = True
            break
    assert seeded, "fixture must have at least one WIN to mutate"
    workspace["data"].write_text(json.dumps(data))

    qc.run_qc(
        workspace["data"], workspace["schema"],
        workspace["backups"], workspace["result"],
    )
    healed = json.loads(workspace["data"].read_text())
    win = next(r for r in healed["requests"] if r["status"] == "WIN")
    assert win["carrier_won"] == "CMA CGM", (
        f"expected canonical 'CMA CGM', got {win['carrier_won']!r}"
    )
    assert win["carrier_quoted"] == "CMA CGM"


def test_phase_4_5_derives_all_5_fields(workspace):
    """Phase 4.5 populates equipment_size, rate_per_feu, trade_region,
    awarded_carrier, validity_window for every request — derived from
    canonical source fields, no staleness risk."""
    qc.run_qc(
        workspace["data"], workspace["schema"],
        workspace["backups"], workspace["result"],
    )
    healed = json.loads(workspace["data"].read_text())
    derived_fields = ("equipment_size", "rate_per_feu", "trade_region",
                      "awarded_carrier", "validity_window")
    for r in healed["requests"]:
        for f in derived_fields:
            assert f in r, f"missing {f} on request {r.get('request_id')}"
    # trade_region is non-null for any request with a destination
    decoded = [r for r in healed["requests"] if r.get("destination")]
    assert all(r["trade_region"] for r in decoded)
    # awarded_carrier mirrors carrier_won
    for r in healed["requests"]:
        assert r["awarded_carrier"] == r.get("carrier_won")



def test_qc_heals_garbage_containers_field(workspace):
    """QC's phase_3_entries clears containers fields that look like
    body text (CAUTION banner leak from older runs)."""
    data = json.loads(workspace["data"].read_text())
    data["requests"][0]["containers"] = (
        "CAUTION: THIS EMAIL ORIGINATED FROM OUTSIDE OF OUR COMPANY. "
        "DO NOT CLICK LINKS. Customer changed their mind here i need 2x40HC"
    )
    workspace["data"].write_text(json.dumps(data))
    qc.run_qc(
        workspace["data"], workspace["schema"],
        workspace["backups"], workspace["result"],
    )
    healed = json.loads(workspace["data"].read_text())
    assert healed["requests"][0]["containers"] is None, \
        "garbage containers must be cleared"


def test_qc_keeps_valid_containers_string(workspace):
    """A valid container spec like '2x40HC' must NOT be cleared."""
    data = json.loads(workspace["data"].read_text())
    data["requests"][0]["containers"] = "2x40'HC"
    workspace["data"].write_text(json.dumps(data))
    qc.run_qc(
        workspace["data"], workspace["schema"],
        workspace["backups"], workspace["result"],
    )
    healed = json.loads(workspace["data"].read_text())
    assert healed["requests"][0]["containers"] == "2x40'HC"


def test_qc_clears_shared_mailbox_signer(workspace):
    """ol_responder_signer that's actually 'MBD Ocean Export Booking
    (Shared)' (or any variant) must be cleared by QC so future runs
    re-extract a real human name."""
    data = json.loads(workspace["data"].read_text())
    # Pin a few WINs/Q&Ls with the bogus signer string.
    n_set = 0
    for r in data["requests"]:
        if r.get("status") in ("WIN", "Q&L") and n_set < 3:
            r["ol_responder_signer"] = "MBD Ocean Export Booking (Shared)"
            n_set += 1
    workspace["data"].write_text(json.dumps(data))
    qc.run_qc(
        workspace["data"], workspace["schema"],
        workspace["backups"], workspace["result"],
    )
    healed = json.loads(workspace["data"].read_text())
    assert all(
        r.get("ol_responder_signer") != "MBD Ocean Export Booking (Shared)"
        for r in healed["requests"]
    ), "shared-mailbox signer must be cleared by QC"


# Phase 10 — bidirectional schema/data drift detection.
# Audit on 2026-04-29 found 14 fields code wrote that schema didn't
# declare, plus 4 type mismatches. Phase 10 was extended to catch this
# automatically on every run.
# ─────────────────────────────────────────────────────────────────────


def test_phase_10_detects_undeclared_data_fields(workspace, tmp_path):
    """A field present in data but not declared in schema must surface
    as a Phase 10 warning + a selfheal_actions[] record."""
    # Bare-bones schema that's missing the field "secret_runtime_attr".
    minimal_schema = tmp_path / "minimal-schema.json"
    minimal_schema.write_text(json.dumps({
        "definitions": {
            "request": {
                "properties": {
                    "request_id": {"type": "string"},
                    "status": {"type": "string"},
                }
            }
        }
    }))
    data = json.loads(workspace["data"].read_text())
    for r in data["requests"]:
        r["secret_runtime_attr"] = "value"
    workspace["data"].write_text(json.dumps(data))

    qc.run_qc(
        workspace["data"], minimal_schema,
        workspace["backups"], workspace["result"],
    )
    healed = json.loads(workspace["data"].read_text())
    actions = healed.get("selfheal_actions", [])
    undeclared_records = [a for a in actions if a.get("kind") == "schema_undeclared_fields"]
    assert undeclared_records, "Phase 10 must record schema_undeclared_fields"
    assert "secret_runtime_attr" in undeclared_records[-1]["fields"]


def test_phase_10_detects_type_drift(workspace, tmp_path):
    """A value whose Python type contradicts the schema's declared type
    must surface as a Phase 10 warning + a selfheal_actions[] record."""
    # Schema declaring teu_won as integer-only (no null), but data has
    # the canonical real-world shape with None on Q&L/PENDING/NQ rows.
    strict_schema = tmp_path / "strict-schema.json"
    strict_schema.write_text(json.dumps({
        "definitions": {
            "request": {
                "properties": {
                    "request_id": {"type": "string"},
                    "status": {"type": "string"},
                    "teu_won": {"type": "integer"},
                }
            }
        }
    }))
    data = json.loads(workspace["data"].read_text())
    # Force a non-WIN row to have teu_won=None so the strict schema fires.
    for r in data["requests"]:
        if r.get("status") != "WIN":
            r["teu_won"] = None
    workspace["data"].write_text(json.dumps(data))

    qc.run_qc(
        workspace["data"], strict_schema,
        workspace["backups"], workspace["result"],
    )
    healed = json.loads(workspace["data"].read_text())
    actions = healed.get("selfheal_actions", [])
    type_drift_records = [a for a in actions if a.get("kind") == "schema_type_drift"]
    assert type_drift_records, "Phase 10 must record schema_type_drift"
    assert "teu_won" in type_drift_records[-1]["by_field"]


def test_phase_10_clean_when_schema_matches_data(workspace):
    """Running QC with the project's actual schema.json against the
    golden fixture must not flag any drift. This is the regression
    guard: if anyone re-introduces the typo (e.g. vessel_voyage →
    vessel_offered) or forgets to declare a new field, this fails."""
    qc.run_qc(
        workspace["data"], workspace["schema"],
        workspace["backups"], workspace["result"],
    )
    healed = json.loads(workspace["data"].read_text())
    actions = healed.get("selfheal_actions", [])
    undeclared = [a for a in actions if a.get("kind") == "schema_undeclared_fields"]
    type_drift = [a for a in actions if a.get("kind") == "schema_type_drift"]
    assert not undeclared, (
        "Golden fixture must not have undeclared fields. Drift from current "
        f"selfheal_actions: {undeclared}"
    )
    assert not type_drift, (
        "Golden fixture must not have type drift. Current type drift: "
        f"{type_drift}"
    )


def test_phase_10_helper_matches_jsonschema_type():
    """Sanity unit test for the type-matcher used in drift detection."""
    m = qc._matches_jsonschema_type
    # Scalar types
    assert m("hi", "string")
    assert m(42, "integer")
    assert m(42.5, "number")
    assert m(42, "number")  # integer is a number
    assert m(True, "boolean")
    assert m([], "array")
    assert m({}, "object")
    assert m(None, "null")
    # Booleans must NOT match number/integer (Python int subtype trap)
    assert not m(True, "integer")
    assert not m(False, "number")
    # Union types
    assert m(None, ["string", "null"])
    assert m("hi", ["string", "null"])
    assert not m(42, ["string", "null"])
    # Unknown declarations pass through (don't false-flag)
    assert m("anything", "weird-type-not-in-spec")


# ─────────────────────────────────────────────────────────────────────
# Top-level + sub-object drift detection (Phase 10 extended).
# Audit on 2026-04-29 found drift wasn't only at request-level — the
# top-level data_range/date_range typo, and undeclared carrier fields
# (loss_reasons / loss_reason_summary) had been silent for months.
# ─────────────────────────────────────────────────────────────────────


def test_phase_3_migrates_data_range_to_date_range(workspace):
    """Legacy 'data_range' top-level field (typo) gets renamed to
    canonical 'date_range' on first run. Idempotent."""
    data = json.loads(workspace["data"].read_text())
    data.pop("date_range", None)
    data["data_range"] = "2026-04-01 to 2026-04-15"
    workspace["data"].write_text(json.dumps(data))

    qc.run_qc(
        workspace["data"], workspace["schema"],
        workspace["backups"], workspace["result"],
    )
    healed = json.loads(workspace["data"].read_text())
    assert "data_range" not in healed
    assert healed.get("date_range") == "2026-04-01 to 2026-04-15"


def test_phase_3_drops_legacy_data_range_when_both_present(workspace):
    """If both 'data_range' (legacy) AND 'date_range' (canonical) are
    present, drop the legacy and keep the canonical — defensive against
    a half-migrated state."""
    data = json.loads(workspace["data"].read_text())
    data["date_range"] = "canonical"
    data["data_range"] = "legacy"
    workspace["data"].write_text(json.dumps(data))

    qc.run_qc(
        workspace["data"], workspace["schema"],
        workspace["backups"], workspace["result"],
    )
    healed = json.loads(workspace["data"].read_text())
    assert "data_range" not in healed
    assert healed.get("date_range") == "canonical"


def test_phase_10_detects_top_level_undeclared(workspace, tmp_path):
    """Top-level field present in data but not declared in schema must
    surface — caught the data_range/date_range typo on 2026-04-29."""
    minimal_schema = tmp_path / "minimal-schema.json"
    minimal_schema.write_text(json.dumps({
        "properties": {"version": {"type": "string"}, "requests": {"type": "array"}},
        "definitions": {
            "request": {"properties": {
                "request_id": {"type": "string"},
                "status": {"type": "string"},
            }}
        }
    }))
    data = json.loads(workspace["data"].read_text())
    data["mystery_top_level_key"] = "leaked"
    workspace["data"].write_text(json.dumps(data))

    qc.run_qc(
        workspace["data"], minimal_schema,
        workspace["backups"], workspace["result"],
    )
    healed = json.loads(workspace["data"].read_text())
    actions = healed.get("selfheal_actions", [])
    top_records = [a for a in actions
                   if a.get("kind") == "schema_undeclared_fields"
                   and a.get("scope") == "top-level"]
    assert top_records, f"expected top-level undeclared record; got {actions}"
    assert "mystery_top_level_key" in top_records[-1]["fields"]


def test_phase_10_detects_carrier_undeclared(tmp_path):
    """Undeclared field in carrier object must surface — caught
    loss_reasons / loss_reason_summary on 2026-04-29.

    Tests phase_10_schema_drift directly because Phase 5 rebuilds
    lanes/carriers from raw before Phase 10 in the orchestrated
    pipeline, which would clobber any injected test fixture."""
    minimal_schema = tmp_path / "minimal-schema.json"
    minimal_schema.write_text(json.dumps({
        "properties": {},
        "definitions": {
            "request": {"properties": {"request_id": {"type": "string"}}},
            "carrier": {"properties": {"carrier": {"type": "string"}}},
        }
    }))
    data = {
        "version": "8.0",
        "requests": [{"request_id": "r1"}, {"request_id": "r2"}],
        "carriers": [{"carrier": "MSC", "phantom_field": "leaked"}],
    }
    log = qc.Log()
    qc.phase_10_schema_drift(log, data, minimal_schema)
    actions = data.get("selfheal_actions", [])
    car_records = [a for a in actions
                   if a.get("kind") == "schema_undeclared_fields"
                   and a.get("scope") == "carrier"]
    assert car_records, f"expected carrier undeclared record; got {actions}"
    assert "phantom_field" in car_records[-1]["fields"]


def test_phase_10_detects_lane_type_drift(tmp_path):
    """Type drift in a lane object must surface."""
    minimal_schema = tmp_path / "minimal-schema.json"
    minimal_schema.write_text(json.dumps({
        "properties": {},
        "definitions": {
            "request": {"properties": {"request_id": {"type": "string"}}},
            "lane": {"properties": {
                "lane": {"type": "string"},
                "wins": {"type": "integer"},
            }},
        }
    }))
    data = {
        "version": "8.0",
        "requests": [{"request_id": "r1"}, {"request_id": "r2"}],
        "lanes": [{"lane": "Oakland -> Tokyo", "wins": "two"}],
    }
    log = qc.Log()
    qc.phase_10_schema_drift(log, data, minimal_schema)
    actions = data.get("selfheal_actions", [])
    lane_records = [a for a in actions
                    if a.get("kind") == "schema_type_drift"
                    and a.get("scope") == "lane"]
    assert lane_records, f"expected lane type-drift record; got {actions}"
    assert "wins" in lane_records[-1]["by_field"]


def test_phase_10_detects_summary_undeclared(tmp_path):
    """Undeclared field in summary singleton dict must surface."""
    minimal_schema = tmp_path / "minimal-schema.json"
    minimal_schema.write_text(json.dumps({
        "properties": {},
        "definitions": {
            "request": {"properties": {"request_id": {"type": "string"}}},
            "summary": {"properties": {"wins": {"type": "integer"}}},
        }
    }))
    data = {
        "version": "8.0",
        "requests": [{"request_id": "r1"}, {"request_id": "r2"}],
        "summary": {"wins": 2, "phantom_summary_metric": "leaked"},
    }
    log = qc.Log()
    qc.phase_10_schema_drift(log, data, minimal_schema)
    actions = data.get("selfheal_actions", [])
    sum_records = [a for a in actions
                   if a.get("kind") == "schema_undeclared_fields"
                   and a.get("scope") == "summary"]
    assert sum_records, f"expected summary undeclared record; got {actions}"
    assert "phantom_summary_metric" in sum_records[-1]["fields"]


def test_phase_10_clean_at_all_levels_on_golden_fixture(workspace):
    """Regression guard — the project's actual schema.json + golden
    fixture must produce zero drift across every level (top-level,
    summary, lane, carrier, request)."""
    qc.run_qc(
        workspace["data"], workspace["schema"],
        workspace["backups"], workspace["result"],
    )
    healed = json.loads(workspace["data"].read_text())
    actions = healed.get("selfheal_actions", [])
    drift_kinds = {"schema_undeclared_fields", "schema_type_drift"}
    drift_records = [a for a in actions if a.get("kind") in drift_kinds]
    assert not drift_records, (
        f"Golden fixture must be drift-clean across all schema levels. "
        f"Got: {drift_records}"
    )


def test_ol_rate_storage_is_number_or_none_post_qc(workspace):
    """Type contract: after QC, ol_rate must be number-or-None across
    all rows. Pre-fix the NQ heal stored the string 'Not Quoted' here,
    breaking baselines.has_rate_body and other downstream consumers
    that test for `is not None`. The display label is now computed at
    template time, not stored."""
    data = json.loads(workspace["data"].read_text())
    # Inject contamination: an NQ row with a stale numeric ol_rate.
    nq_row = next((r for r in data["requests"] if r.get("status") == "NQ"), None)
    assert nq_row is not None
    nq_row["ol_rate"] = 9999.0
    workspace["data"].write_text(json.dumps(data))

    qc.run_qc(
        workspace["data"], workspace["schema"],
        workspace["backups"], workspace["result"],
    )
    healed = json.loads(workspace["data"].read_text())
    for r in healed["requests"]:
        rate = r.get("ol_rate")
        assert rate is None or isinstance(rate, (int, float)), (
            f"ol_rate must be number or None on {r.get('request_id')}; "
            f"got {rate!r} (type {type(rate).__name__})"
        )
        if r.get("status") == "NQ":
            assert rate is None, (
                f"NQ row {r.get('request_id')} must have ol_rate=None "
                f"(display label computed at render time); got {rate!r}"
            )


# ─────────────────────────────────────────────────────────────────────
# Phase 4 — duplicate detection
# ─────────────────────────────────────────────────────────────────────


def test_phase_4_keeps_richest_duplicate():
    """When two rows share a request_id, dedup must keep the one with the
    most populated fields and drop the rest — never drop both, never
    average them."""
    rich = {
        "request_id": "dup1", "status": "WIN", "carrier_won": "MAERSK",
        "ol_rate": 1234.5, "destination": "Tokyo", "has_send": True,
        "mdolx_ref": "MDOLX260001",
    }
    sparse = {"request_id": "dup1", "status": "WIN"}
    unique = {"request_id": "solo", "status": "NQ"}
    data = {"requests": [sparse, rich, unique]}
    log = qc.Log()
    qc.phase_4_duplicates(log, data)
    assert len(data["requests"]) == 2
    keepers_by_id = {r["request_id"]: r for r in data["requests"]}
    # Richest version of dup1 survives — its booking signal intact.
    assert keepers_by_id["dup1"]["carrier_won"] == "MAERSK"
    assert keepers_by_id["solo"]["status"] == "NQ"
    assert any("Deduped request_id=dup1" in f for f in log.fixes)


def test_phase_4_silent_when_no_duplicates():
    """The fast-path: no duplicates → no fixes, no mutation of requests."""
    data = {"requests": [
        {"request_id": "a", "status": "WIN"},
        {"request_id": "b", "status": "NQ"},
    ]}
    log = qc.Log()
    qc.phase_4_duplicates(log, data)
    assert log.fixes == []
    assert len(data["requests"]) == 2


# ─────────────────────────────────────────────────────────────────────
# Phase 6 cross-check rules — error-path coverage (QC-001..QC-008)
#
# These tests invoke phase_6_rules() directly with crafted in-memory
# data so each rule's error/warn branch can be exercised without
# fighting phase_3's heal layer (which clears NON_WIN_CARRIER_FIELDS,
# heals teu_won, etc. before phase_6 ever sees the row).
# ─────────────────────────────────────────────────────────────────────


def _phase_6(requests: list[dict]) -> qc.Log:
    log = qc.Log()
    qc.phase_6_rules(log, {"requests": requests})
    return log


def test_qc_001_warns_when_zero_ql_in_large_dataset():
    """Phase 6 warns if a dataset of >10 requests has 0 Q&L — that's an
    implausible distribution (Lonny quotes daily; some always lose).
    Catches classifier regressions that collapse Q&L into NQ."""
    rows = [
        {"request_id": f"r{i}", "status": "WIN", "carrier_won": "MAERSK"}
        for i in range(11)
    ]
    log = _phase_6(rows)
    assert any("QC-001" in w for w in log.warnings)


def test_qc_001_silent_below_threshold():
    """Below 10 rows, an empty Q&L bucket is not enough signal to warn —
    avoid noise on small samples (early morning, light traffic days)."""
    rows = [
        {"request_id": "r1", "status": "WIN", "carrier_won": "MAERSK"},
        {"request_id": "r2", "status": "NQ"},
    ]
    log = _phase_6(rows)
    assert not any("QC-001" in w for w in log.warnings)


def test_qc_002_errors_on_win_without_carrier():
    """A WIN row that has no carrier_won is a logical impossibility —
    the booking can't be 'confirmed' without naming the carrier. Phase 6
    errors so the daily run stamps HAS_ERRORS until the row is fixed."""
    rows = [
        {"request_id": "bad_win", "status": "WIN", "carrier_won": None,
         "mdolx_ref": "MDOLX260999", "has_send": True},
    ]
    log = _phase_6(rows)
    qc002 = [e for e in log.errors if "QC-002" in e]
    assert len(qc002) == 1
    assert "1 WIN" in qc002[0]


def test_qc_003_warns_on_win_with_no_send_and_no_mdolx():
    """A WIN without either a chain-send confirmation OR an MDOLX ref is
    unverified — could be a parser hallucination. Warn (not error) since
    the row might just be early in its confirmation lifecycle."""
    rows = [
        {"request_id": "unverified_win", "status": "WIN", "carrier_won": "ONE",
         "mdolx_ref": None, "has_send": False},
    ]
    log = _phase_6(rows)
    assert any("QC-003" in w for w in log.warnings)


def test_qc_003_silent_when_either_signal_present():
    """Either signal is sufficient — MDOLX-only or chain-send-only are
    both well-formed WINs."""
    rows = [
        {"request_id": "w1", "status": "WIN", "carrier_won": "ONE",
         "mdolx_ref": "MDOLX260999", "has_send": False},
        {"request_id": "w2", "status": "WIN", "carrier_won": "MAERSK",
         "mdolx_ref": None, "has_send": True},
    ]
    log = _phase_6(rows)
    assert not any("QC-003" in w for w in log.warnings)


def test_qc_004_errors_on_nq_contamination():
    """NQ status is mutually exclusive with having quoted a carrier —
    if carrier_quoted is set on an NQ row, the classifier or heal layer
    left a stale field behind."""
    rows = [
        {"request_id": "contaminated_nq", "status": "NQ",
         "carrier_quoted": "MAERSK"},
    ]
    log = _phase_6(rows)
    assert any("QC-004" in e for e in log.errors)


def test_qc_004_ignores_sentinel_values():
    """N/A and empty-string aren't real contamination — heal layer
    intentionally stores these as 'we tried, nobody answered' markers."""
    rows = [
        {"request_id": "nq1", "status": "NQ", "carrier_quoted": "N/A"},
        {"request_id": "nq2", "status": "NQ", "carrier_quoted": ""},
        {"request_id": "nq3", "status": "NQ", "carrier_quoted": None},
    ]
    log = _phase_6(rows)
    assert not any("QC-004" in e for e in log.errors)


def test_qc_005_warns_on_implausible_business_hours():
    """turnaround_biz_hours > 100 means either the timestamp is wrong or
    the row crossed a long weekend / holiday gap. Worth a human look."""
    rows = [
        {"request_id": "slow", "status": "WIN", "carrier_won": "ONE",
         "has_send": True, "mdolx_ref": "MDOLX260999",
         "turnaround_biz_hours": 250},
    ]
    log = _phase_6(rows)
    qc005 = [w for w in log.warnings if "QC-005" in w]
    assert len(qc005) == 1
    assert "slow" in qc005[0]
    assert "250" in qc005[0]


def test_qc_006_warns_on_large_teu_request():
    """teu_requested > 30 is rare for Hilmar lanes (typical 1-4 TEU per
    request). Could indicate a parser misread (e.g. read 40 instead of 1
    of '40HQ' as a TEU count)."""
    rows = [
        {"request_id": "huge", "status": "PENDING", "teu_requested": 50,
         "response_timestamp": None},
    ]
    log = _phase_6(rows)
    assert any("QC-006" in w and "huge" in w for w in log.warnings)


def test_qc_008_errors_on_non_win_with_carrier_won_set():
    """Per 2026-04-29 audit (req_d72835b5341716c7 / req_47eda86d98477ca6),
    Q&L or PENDING rows must NEVER carry carrier_won or awarded_carrier —
    those are booking-confirmed signals. Phase 3 normally clears them;
    QC-008 is the safety net if the heal pass missed."""
    rows = [
        {"request_id": "leak_ql", "status": "Q&L", "carrier_won": "MAERSK"},
        {"request_id": "leak_pending", "status": "PENDING",
         "awarded_carrier": "CMA CGM",
         "response_timestamp": None},
        {"request_id": "clean_win", "status": "WIN",
         "carrier_won": "ONE", "mdolx_ref": "M1", "has_send": True},
    ]
    log = _phase_6(rows)
    qc008 = [e for e in log.errors if "QC-008" in e]
    assert len(qc008) == 1
    # Both offenders should be named in the error message (it lists up to 5).
    assert "leak_ql" in qc008[0]
    assert "leak_pending" in qc008[0]
    assert "clean_win" not in qc008[0]


# ─────────────────────────────────────────────────────────────────────
# qc.main() — CLI entry point
# ─────────────────────────────────────────────────────────────────────


def test_main_clean_run_returns_zero(workspace, monkeypatch, capsys):
    """The CLI on a clean golden fixture exits 0 and prints the summary
    block. Argument plumbing: --data / --schema / --backups / --result
    all flow through to run_qc."""
    monkeypatch.setattr("sys.argv", [
        "hilmar-qc",
        "--data", str(workspace["data"]),
        "--schema", str(workspace["schema"]),
        "--backups", str(workspace["backups"]),
        "--result", str(workspace["result"]),
    ])
    rc = qc.main()
    assert rc == 0
    out = capsys.readouterr().out
    assert "QC SELF-HEAL COMPLETE" in out
    assert "Status:" in out
    # Counts/rates summary block must render on success.
    assert "Win rate" in out


def test_main_blocked_run_returns_one(workspace, monkeypatch):
    """When phase_2 fails the structural-integrity check (missing required
    top-level key 'requests'), run_qc returns BLOCKED and main() exits 1
    so systemd / cron registers the failure."""
    # Valid JSON but missing the required `requests` key → phase 2 returns
    # False → result["status"] == "BLOCKED".
    workspace["data"].write_text(json.dumps({
        "version": "v2",
        "summary": {},
    }))
    monkeypatch.setattr("sys.argv", [
        "hilmar-qc",
        "--data", str(workspace["data"]),
        "--schema", str(workspace["schema"]),
        "--backups", str(workspace["backups"]),
        "--result", str(workspace["result"]),
    ])
    rc = qc.main()
    assert rc == 1
    res = json.loads(workspace["result"].read_text())
    assert res["status"] == "BLOCKED"


def test_main_no_backup_flag(workspace, monkeypatch):
    """--no-backup must reach run_qc and skip the backups dir entirely."""
    monkeypatch.setattr("sys.argv", [
        "hilmar-qc",
        "--data", str(workspace["data"]),
        "--schema", str(workspace["schema"]),
        "--backups", str(workspace["backups"]),
        "--result", str(workspace["result"]),
        "--no-backup",
    ])
    assert qc.main() == 0
    # Backups dir must be empty (or absent) when --no-backup is honored.
    assert not workspace["backups"].exists() or not list(
        workspace["backups"].glob("*.json")
    )


def test_main_custom_retention(workspace, monkeypatch):
    """--retention threads through to rotate_backup. Sanity-check by
    asserting the run completes cleanly when a non-default value is given."""
    monkeypatch.setattr("sys.argv", [
        "hilmar-qc",
        "--data", str(workspace["data"]),
        "--schema", str(workspace["schema"]),
        "--backups", str(workspace["backups"]),
        "--result", str(workspace["result"]),
        "--retention", "3",
    ])
    assert qc.main() == 0
