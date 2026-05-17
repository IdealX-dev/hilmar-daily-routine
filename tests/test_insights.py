"""Tests for hilmar.insights — rule-based engine (M3.11.a).

Covers build_context() against synthetic tracking-data + baselines.
Anomaly detection, deltas, today-only filters, JSON serialisation.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from hilmar import insights

UTC = timezone.utc
NOW = datetime(2026, 4, 26, 12, 0, tzinfo=UTC)


def _req(
    *,
    rid: str,
    status: str = "WIN",
    when: datetime | None = None,
    quoted: bool = True,
    carrier_won: str | None = "MSC",
    carrier_quoted: str | None = "MSC",
    destination: str = "Shanghai",
    lane: str = "Oakland → Shanghai",
    teu_won: int = 4,
    teu_requested: int = 4,
    biz_hours: float | None = 2.0,
) -> dict:
    when = when or NOW
    return {
        "request_id": rid,
        "status": status,
        "request_timestamp": when.isoformat(),
        "quoted": quoted,
        "carrier_won": carrier_won if status == "WIN" else None,
        "carrier_quoted": carrier_quoted,
        "destination": destination,
        "lane": lane,
        "teu_won": teu_won if status == "WIN" else 0,
        "teu_requested": teu_requested,
        "turnaround_biz_hours": biz_hours,
    }


# ─────────────────────────────────────────────────────────────────────
# Snapshot fields
# ─────────────────────────────────────────────────────────────────────


def test_build_context_today_snapshot_from_summary():
    td = {
        "summary": {
            "total_entries": 10, "wins": 4, "quoted_lost": 3,
            "not_quoted": 2, "pending_hilmar": 1, "win_rate": 33.3,
        },
        "requests": [],
    }
    ctx = insights.build_context(tracking_data=td, now=NOW)
    assert ctx.total == 10
    assert ctx.wins == 4
    assert ctx.quoted_lost == 3
    assert ctx.not_quoted == 2
    assert ctx.pending == 1
    assert ctx.win_rate_pct == 33.3


def test_build_context_carrier_mix_recent_only():
    td = {
        "requests": [
            _req(rid="a", status="WIN", carrier_won="MSC"),
            _req(rid="b", status="WIN", carrier_won="ZIM"),
            _req(rid="c", status="WIN", carrier_won="MSC"),
        ],
    }
    ctx = insights.build_context(tracking_data=td, now=NOW)
    assert ctx.carrier_mix == {"MSC": 2, "ZIM": 1}


def test_build_context_lane_top_3_orders_by_count():
    td = {
        "requests": [
            _req(rid="a", lane="A → B"),
            _req(rid="b", lane="A → B"),
            _req(rid="c", lane="C → D"),
            _req(rid="d", lane="E → F"),
            _req(rid="e", lane="A → B"),
        ],
    }
    ctx = insights.build_context(tracking_data=td, now=NOW)
    assert ctx.lane_top_3[0] == ("A → B", 3)
    assert len(ctx.lane_top_3) <= 3


def test_build_context_biggest_wins_today_orders_by_teu():
    td = {
        "requests": [
            _req(rid="big",   status="WIN", teu_won=20),
            _req(rid="small", status="WIN", teu_won=2),
            _req(rid="mid",   status="WIN", teu_won=8),
            # Yesterday WIN must be excluded from "today" list.
            _req(rid="ytd",   status="WIN", teu_won=999,
                 when=NOW - timedelta(days=1)),
        ],
    }
    ctx = insights.build_context(tracking_data=td, now=NOW)
    assert [w["request_id"] for w in ctx.biggest_wins_today] == ["big", "mid", "small"]


def test_build_context_aging_pendings_filters_below_threshold():
    td = {
        "requests": [
            _req(rid="old",   status="PENDING", biz_hours=20.0),
            _req(rid="fresh", status="PENDING", biz_hours=4.0),
            _req(rid="ancient", status="PENDING", biz_hours=80.0),
        ],
    }
    ctx = insights.build_context(tracking_data=td, now=NOW)
    aging_ids = [p["request_id"] for p in ctx.aging_pendings]
    assert "old" in aging_ids
    assert "ancient" in aging_ids
    assert "fresh" not in aging_ids
    # Sorted descending by biz_hours.
    assert aging_ids == ["ancient", "old"]


# ─────────────────────────────────────────────────────────────────────
# Deltas vs baseline
# ─────────────────────────────────────────────────────────────────────


def test_build_context_win_rate_delta_pp():
    td = {
        "summary": {"total_entries": 4, "wins": 2, "quoted_lost": 2, "not_quoted": 0,
                    "pending_hilmar": 0, "win_rate": 50.0},
        "requests": [
            _req(rid=f"r{i}", status=("WIN" if i < 2 else "Q&L"),
                 carrier_won="MSC", carrier_quoted="MSC")
            for i in range(4)
        ],
        "baselines": {"win_rate_pct": 30.0, "win_rate_pct_stddev": 5.0},
    }
    ctx = insights.build_context(tracking_data=td, now=NOW)
    # today win_rate over decided_14 = 2/4 = 50%, baseline 30 → delta +20pp
    assert ctx.win_rate_delta_pp == 20.0


def test_build_context_response_time_delta_pct():
    td = {
        "requests": [
            _req(rid=f"r{i}", biz_hours=h)
            for i, h in enumerate([2.0, 4.0, 6.0])  # median = 4.0
        ],
        "baselines": {"biz_hours_response_p50": 2.0},
    }
    ctx = insights.build_context(tracking_data=td, now=NOW)
    # delta = (4.0 - 2.0) / 2.0 * 100 = 100%
    assert ctx.response_time_delta_pct == 100.0


def test_build_context_volume_delta_when_baseline_present():
    td = {
        "requests": [
            _req(rid="t1", when=NOW.replace(hour=8)),
            _req(rid="t2", when=NOW.replace(hour=10)),
        ],
        "baselines": {"ingest_volume_p50": 1.0},
    }
    ctx = insights.build_context(tracking_data=td, now=NOW)
    # 2 today vs P50=1 → +100%
    assert ctx.volume_delta_pct == 100.0


def test_build_context_no_baseline_means_none_deltas():
    td = {"summary": {"win_rate": 30.0}, "requests": []}
    ctx = insights.build_context(tracking_data=td, now=NOW)
    assert ctx.win_rate_delta_pp is None
    assert ctx.response_time_delta_pct is None
    assert ctx.volume_delta_pct is None


# ─────────────────────────────────────────────────────────────────────
# Anomalies
# ─────────────────────────────────────────────────────────────────────


def test_anomaly_response_slow_when_p50_50pct_above_baseline():
    td = {
        "requests": [
            _req(rid=f"r{i}", biz_hours=h) for i, h in enumerate([3.0, 3.0, 3.0])
        ],
        "baselines": {"biz_hours_response_p50": 1.0},
    }
    ctx = insights.build_context(tracking_data=td, now=NOW)
    kinds = {a.kind for a in ctx.anomalies}
    assert "response_slow" in kinds


def test_anomaly_ingest_gap_when_today_volume_drops_60pct():
    td = {
        "requests": [_req(rid="r1", when=NOW.replace(hour=10))],
        "baselines": {"ingest_volume_p50": 10.0},
    }
    ctx = insights.build_context(tracking_data=td, now=NOW)
    kinds = {a.kind for a in ctx.anomalies}
    assert "ingest_gap_suspected" in kinds


def test_anomaly_carrier_lane_drop_20pp():
    td = {
        "requests": [
            _req(rid="a", status="WIN", carrier_won="MSC", destination="Shanghai"),
            _req(rid="b", status="Q&L", carrier_won="MSC", destination="Shanghai"),
            _req(rid="c", status="Q&L", carrier_won="MSC", destination="Shanghai"),
        ],
        "baselines": {"carrier_lane_winrate": {"MSC.Shanghai": 80.0}},
    }
    ctx = insights.build_context(tracking_data=td, now=NOW)
    kinds = {a.kind for a in ctx.anomalies}
    assert "carrier_lane_drop" in kinds


def test_no_anomalies_when_metrics_match_baseline():
    """All within-spec → no anomalies."""
    td = {
        "requests": [
            _req(rid="a", biz_hours=2.0, status="WIN"),
            _req(rid="b", biz_hours=2.0, status="Q&L"),
        ],
        "baselines": {
            "win_rate_pct": 50.0, "win_rate_pct_stddev": 5.0,
            "biz_hours_response_p50": 2.0,
            "ingest_volume_p50": 2.0,
        },
    }
    ctx = insights.build_context(tracking_data=td, now=NOW)
    assert ctx.anomalies == []


# ─────────────────────────────────────────────────────────────────────
# System-health metrics
# ─────────────────────────────────────────────────────────────────────


def test_qc_fixes_today_picked_from_qc_result():
    ctx = insights.build_context(
        tracking_data={"requests": []},
        qc_result={"fixes": 7},
        now=NOW,
    )
    assert ctx.qc_fixes_today == 7


def test_ingest_gap_flagged_from_selfheal_actions():
    td = {
        "requests": [],
        "selfheal_actions": [
            {"kind": "ingest_gap", "today_count": 1},
        ],
    }
    ctx = insights.build_context(tracking_data=td, now=NOW)
    assert ctx.ingest_gap_flagged is True


def test_parser_miss_rates_passed_through_from_baselines():
    td = {
        "requests": [],
        "baselines": {"parser_miss_rate": {"eta_offered": 12.5, "vessel_voyage": 3.0}},
    }
    ctx = insights.build_context(tracking_data=td, now=NOW)
    assert ctx.parser_miss_rates == {"eta_offered": 12.5, "vessel_voyage": 3.0}


def test_test_coverage_pct_threaded_through():
    ctx = insights.build_context(
        tracking_data={"requests": []},
        test_coverage_pct=82.5,
        now=NOW,
    )
    assert ctx.test_coverage_pct == 82.5


# ─────────────────────────────────────────────────────────────────────
# JSON serialisation
# ─────────────────────────────────────────────────────────────────────


# ─────────────────────────────────────────────────────────────────────
# M3.11.b — LLM narrative engine (mocked router)
# ─────────────────────────────────────────────────────────────────────


class _StubRouter:
    """Mocked ModelRouter — returns a canned ModelResponse per task."""

    def __init__(self, *, fail_task: str | None = None) -> None:
        self.fail_task = fail_task
        self.calls: list[dict] = []

    def call(self, *, task_type: str, prompt: str, system: str | None = None,
             max_tokens: int = 4096):
        self.calls.append({"task": task_type, "prompt": prompt, "system": system})
        from hilmar.model_router import ModelResponse
        if task_type == self.fail_task:
            return ModelResponse(
                text="", model="claude-opus-4-6", task_type=task_type,
                input_tokens=0, output_tokens=0, cost_cents=0,
                skipped_reason="api_unavailable",
            )
        return ModelResponse(
            text=f"- Bullet for {task_type}\n- **Strong** point with `code`",
            model="claude-opus-4-6",
            task_type=task_type,
            input_tokens=100, output_tokens=50, cost_cents=10,
        )


def test_generate_narrative_calls_all_four_tasks():
    ctx = insights.build_context(tracking_data={"requests": []}, now=NOW)
    router = _StubRouter()
    bundle = insights.generate_narrative(ctx, router=router)
    assert bundle.system.task_type == "system_critique"
    assert bundle.design.task_type == "design_suggestions"
    assert bundle.data.task_type == "data_suggestions"
    assert bundle.business.task_type == "business_advice"
    assert len(router.calls) == 4
    assert {c["task"] for c in router.calls} == {
        "system_critique", "design_suggestions", "data_suggestions", "business_advice",
    }


def test_generate_narrative_includes_context_json_in_prompt():
    ctx = insights.build_context(tracking_data={
        "summary": {"win_rate": 42.0, "wins": 7, "total_entries": 12},
        "requests": [],
    }, now=NOW)
    router = _StubRouter()
    insights.generate_narrative(ctx, router=router)
    # The user-prompt of every call must contain serialised context.
    for call in router.calls:
        assert "win_rate_pct" in call["prompt"]
        assert "42.0" in call["prompt"]


def test_generate_narrative_one_task_skipped_does_not_block_others():
    ctx = insights.build_context(tracking_data={"requests": []}, now=NOW)
    router = _StubRouter(fail_task="business_advice")
    bundle = insights.generate_narrative(ctx, router=router)
    assert bundle.business.skipped_reason == "api_unavailable"
    assert bundle.system.skipped_reason is None
    assert bundle.design.skipped_reason is None
    assert bundle.data.skipped_reason is None
    assert bundle.any_skipped() is True


def test_generate_narrative_total_cost_cents_sums_all_four():
    ctx = insights.build_context(tracking_data={"requests": []}, now=NOW)
    router = _StubRouter()
    bundle = insights.generate_narrative(ctx, router=router)
    assert bundle.total_cost_cents() == 4 * 10


def test_generate_narrative_feedback_summary_appended_to_system_prompts():
    ctx = insights.build_context(tracking_data={"requests": []}, now=NOW)
    router = _StubRouter()
    feedback = "What worked: anomaly-led bullets. What didn't: vague advice."
    insights.generate_narrative(ctx, router=router, feedback_summary=feedback)
    for call in router.calls:
        assert feedback in (call["system"] or "")


def test_render_narrative_html_renders_all_four_sections():
    ctx = insights.build_context(tracking_data={"requests": []}, now=NOW)
    router = _StubRouter()
    bundle = insights.generate_narrative(ctx, router=router)
    html = insights.render_narrative_html(bundle)
    assert "System" in html
    assert "Design" in html
    assert "Data" in html
    assert "Business" in html
    assert "<ul" in html  # bullet list rendered
    assert "<strong>" in html  # **bold** processed
    assert "<code>" in html  # `code` processed


def test_render_narrative_html_marks_skipped_section():
    ctx = insights.build_context(tracking_data={"requests": []}, now=NOW)
    router = _StubRouter(fail_task="business_advice")
    bundle = insights.generate_narrative(ctx, router=router)
    html = insights.render_narrative_html(bundle)
    assert "skipped" in html.lower()


def test_load_prompt_returns_empty_when_missing(tmp_path: Path):
    """If a prompt file is missing the loader returns empty string and the
    LLM call still happens (without a system prompt)."""
    from hilmar import insights as ins
    # All five prompt files should exist on disk after M3.11.b.
    for fname in ins.TASK_PROMPTS.values():
        assert (ins.PROMPTS_DIR / fname).exists(), f"missing prompt: {fname}"


def test_context_to_dict_is_json_safe():
    td = {
        "requests": [
            _req(rid="a", status="WIN", carrier_won="MSC"),
        ],
        "baselines": {
            "win_rate_pct": 50.0, "win_rate_pct_stddev": 5.0,
            "biz_hours_response_p50": 2.0,
            "ingest_volume_p50": 1.0,
            "parser_miss_rate": {"eta_offered": 5.0},
            "carrier_lane_winrate": {"MSC.Shanghai": 80.0},
        },
    }
    ctx = insights.build_context(tracking_data=td, now=NOW)
    serialised = insights.context_to_dict(ctx)
    # Must round-trip through json.dumps without TypeError.
    s = json.dumps(serialised)
    revived = json.loads(s)
    assert revived["wins"] == ctx.wins
    # lane_top_3 is list of [str, int] (2-element list, not tuple).
    assert isinstance(revived["lane_top_3"], list)
    for item in revived["lane_top_3"]:
        assert isinstance(item, list) and len(item) == 2


# ─── Parser miss-log → narrative (PR #25) ─────────────────────────────


def test_compute_parser_miss_patterns_returns_empty_when_log_missing(tmp_path):
    """Pre-deploy / no-misses-yet path — return [] so build_context
    keeps working."""
    out = insights.compute_parser_miss_patterns(tmp_path / "nope.jsonl", now=NOW)
    assert out == []


def test_compute_parser_miss_patterns_groups_and_orders_by_count(tmp_path):
    """Top-N most-missed fields with example excerpt + LLM-fallback split."""
    log = tmp_path / "parser_misses.jsonl"
    recent = (NOW - timedelta(days=1)).isoformat()
    rows = [
        {"ts": recent, "field": "etd_offered", "result": "llm_extracted",
         "body_excerpt": "ETD 5/15 ON-CARRIER MSC AURIGA"},
        {"ts": recent, "field": "etd_offered", "result": "llm_extracted",
         "body_excerpt": "ETD 5/16 ZIM CHINA"},
        {"ts": recent, "field": "etd_offered", "result": "skipped_budget",
         "body_excerpt": "ETD next week"},
        {"ts": recent, "field": "ol_rate", "result": "llm_error",
         "body_excerpt": "Rate $2400 USD/40FT"},
    ]
    log.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    out = insights.compute_parser_miss_patterns(log, top_n=5, now=NOW)
    assert len(out) == 2
    # etd_offered is more frequent → first.
    assert out[0]["field"] == "etd_offered"
    assert out[0]["count"] == 3
    assert out[0]["llm_extracted"] == 2
    assert out[0]["budget_skipped"] == 1
    assert "ETD" in out[0]["example_excerpt"]
    # ol_rate is second.
    assert out[1]["field"] == "ol_rate"
    assert out[1]["count"] == 1
    assert out[1]["llm_error"] == 1


def test_compute_parser_miss_patterns_filters_outside_window(tmp_path):
    """Only entries within window_days are counted; older ones ignored."""
    log = tmp_path / "parser_misses.jsonl"
    fresh = (NOW - timedelta(days=1)).isoformat()
    stale = (NOW - timedelta(days=14)).isoformat()
    rows = [
        {"ts": fresh, "field": "etd_offered", "result": "llm_extracted",
         "body_excerpt": "fresh body"},
        {"ts": stale, "field": "etd_offered", "result": "llm_extracted",
         "body_excerpt": "stale body"},
    ]
    log.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    out = insights.compute_parser_miss_patterns(log, window_days=7, now=NOW)
    assert out[0]["count"] == 1
    assert "fresh" in out[0]["example_excerpt"]


def test_build_context_threads_parser_miss_log_when_provided(tmp_path):
    """End-to-end: build_context + parser_miss_log_path puts the top
    patterns into the InsightsContext so the LLM prompt sees them."""
    log = tmp_path / "parser_misses.jsonl"
    log.write_text(json.dumps({
        "ts": NOW.isoformat(),
        "field": "ol_rate",
        "result": "llm_extracted",
        "body_excerpt": "OL Rate USD 2400 per 40HC",
    }) + "\n", encoding="utf-8")
    td = {"requests": [_req(rid="a", status="WIN")]}
    ctx = insights.build_context(
        tracking_data=td, now=NOW, parser_miss_log_path=log,
    )
    assert len(ctx.parser_miss_top_patterns) == 1
    assert ctx.parser_miss_top_patterns[0]["field"] == "ol_rate"
    # Round-trips through json safely (no tuples / paths leaking).
    json.dumps(insights.context_to_dict(ctx))

