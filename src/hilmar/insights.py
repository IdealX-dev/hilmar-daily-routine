"""
hilmar.insights — Daily insights engine.

Two halves (M3.11.a + M3.11.b):

  * M3.11.a: rule-based engine. Pure compute over the tracking-data
    snapshot. Builds an :class:`InsightsContext` dataclass: today's
    snapshot, deltas vs baseline, carrier/lane mix, anomalies, system-
    health metrics. NO LLM call.

  * M3.11.b (next): LLM-narrative engine. Calls
    :class:`hilmar.model_router.ModelRouter` once per task type with
    the InsightsContext + a structured prompt. Returns four sections
    (System / Design / Data / Business).

This file currently implements M3.11.a only — M3.11.b layers on top.
"""
from __future__ import annotations

import json
import logging
import statistics
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import baselines as baselines_mod
from .model_router import ModelResponse, ModelRouter

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────
# Types
# ─────────────────────────────────────────────────────────────────────


@dataclass
class Anomaly:
    """A single deviation from baseline worth narrating."""
    kind: str                 # "win_rate_drop" / "response_slow" / "carrier_shift" / ...
    label: str                # human-readable summary, e.g. "MAERSK win-rate dropped 30pp"
    severity: str             # "info" / "warn" / "alert"
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class InsightsContext:
    """The full structured input to the LLM narrative + the basis for the
    rule-based portion of the daily email's insights section.

    Every field is JSON-serialisable so we can persist the context
    alongside the LLM response in ``reports/insights/<date>.json``.
    """
    # ── Today's snapshot ──
    total: int = 0
    wins: int = 0
    quoted_lost: int = 0
    not_quoted: int = 0
    pending: int = 0
    win_rate_pct: float = 0.0

    # ── Deltas vs baseline (None if baseline unset) ──
    win_rate_delta_pp: float | None = None
    response_time_delta_pct: float | None = None
    volume_delta_pct: float | None = None

    # ── Patterns ──
    carrier_mix: dict[str, int] = field(default_factory=dict)
    lane_top_3: list[tuple[str, int]] = field(default_factory=list)
    biggest_wins_today: list[dict[str, Any]] = field(default_factory=list)
    aging_pendings: list[dict[str, Any]] = field(default_factory=list)

    # ── Anomalies (from baselines comparison) ──
    anomalies: list[Anomaly] = field(default_factory=list)

    # ── System-health ──
    qc_fixes_today: int = 0
    parser_miss_rates: dict[str, float] = field(default_factory=dict)
    parser_miss_top_patterns: list[dict[str, Any]] = field(default_factory=list)
    test_coverage_pct: float | None = None
    ingest_gap_flagged: bool = False

    # ── Provenance ──
    generated_at: str = ""
    data_window: dict[str, str] = field(default_factory=dict)


# ─────────────────────────────────────────────────────────────────────
# Compute
# ─────────────────────────────────────────────────────────────────────


def _today_str(now: datetime) -> str:
    return now.date().isoformat()


def _is_today(r: dict[str, Any], now: datetime) -> bool:
    ts_iso = r.get("request_timestamp") or ""
    return ts_iso[:10] == _today_str(now)


def _mean(values: list[float]) -> float | None:
    return round(statistics.fmean(values), 2) if values else None


def _safe_pct(num: int, denom: int) -> float:
    return round(100.0 * num / denom, 1) if denom else 0.0


def _carrier_mix(requests: list[dict[str, Any]]) -> dict[str, int]:
    counter: Counter[str] = Counter()
    for r in requests:
        c = (r.get("carrier_won") or r.get("carrier_quoted") or "").strip()
        if c:
            counter[c] += 1
    return dict(counter)


def _lane_top_3(requests: list[dict[str, Any]]) -> list[tuple[str, int]]:
    counter: Counter[str] = Counter()
    for r in requests:
        lane = (r.get("lane") or "").strip() or (r.get("destination") or "").strip()
        if lane:
            counter[lane] += 1
    return counter.most_common(3)


def _biggest_wins_today(requests: list[dict[str, Any]], now: datetime, top: int = 5) -> list[dict[str, Any]]:
    today_wins = [
        r for r in requests
        if r.get("status") == "WIN" and _is_today(r, now)
    ]
    today_wins.sort(key=lambda r: -(r.get("teu_won") or r.get("teu_requested") or 0))
    return [
        {
            "request_id": r.get("request_id"),
            "lane": r.get("lane"),
            "carrier": r.get("carrier_won") or r.get("carrier_quoted"),
            "teu": r.get("teu_won") or r.get("teu_requested") or 0,
        }
        for r in today_wins[:top]
    ]


def _aging_pendings(requests: list[dict[str, Any]], threshold_biz_hours: float = 16.0) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for r in requests:
        if r.get("status") != "PENDING":
            continue
        biz = r.get("turnaround_biz_hours")
        if biz is None or biz < threshold_biz_hours:
            continue
        out.append({
            "request_id": r.get("request_id"),
            "lane": r.get("lane"),
            "biz_hours": biz,
            "carrier_quoted": r.get("carrier_quoted"),
        })
    out.sort(key=lambda r: -(r.get("biz_hours") or 0))
    return out


def _delta_pct(today: float | None, baseline: float | None) -> float | None:
    if today is None or baseline is None or baseline == 0:
        return None
    return round(100.0 * (today - baseline) / baseline, 1)


def _detect_anomalies(
    *,
    today_win_rate: float,
    today_response_p50: float | None,
    today_volume: int,
    baselines: dict[str, Any],
    today_carrier_lane: dict[str, float],
) -> list[Anomaly]:
    out: list[Anomaly] = []

    base_wr = baselines.get("win_rate_pct")
    base_wr_sd = baselines.get("win_rate_pct_stddev") or 0
    if base_wr is not None and base_wr_sd:
        delta = today_win_rate - base_wr
        if abs(delta) >= 2 * base_wr_sd and abs(delta) >= 5.0:
            out.append(Anomaly(
                kind="win_rate_shift",
                label=f"Win rate {today_win_rate}% vs baseline {base_wr}% "
                      f"(Δ {delta:+.1f}pp, baseline σ={base_wr_sd}pp)",
                severity="warn" if delta < 0 else "info",
                detail={"today": today_win_rate, "baseline": base_wr,
                        "stddev": base_wr_sd, "delta_pp": round(delta, 1)},
            ))

    base_resp = baselines.get("biz_hours_response_p50")
    if today_response_p50 is not None and base_resp:
        delta_pct = _delta_pct(today_response_p50, base_resp)
        if delta_pct is not None and delta_pct >= 50.0:
            out.append(Anomaly(
                kind="response_slow",
                label=f"Response time today P50={today_response_p50}h "
                      f"vs baseline {base_resp}h ({delta_pct:+.1f}%)",
                severity="warn",
                detail={"today_p50": today_response_p50, "baseline_p50": base_resp,
                        "delta_pct": delta_pct},
            ))

    base_vol = baselines.get("ingest_volume_p50")
    if base_vol:
        delta_pct = _delta_pct(float(today_volume), float(base_vol))
        if delta_pct is not None and delta_pct <= -60.0:
            out.append(Anomaly(
                kind="ingest_gap_suspected",
                label=f"Today volume {today_volume} vs baseline P50={base_vol} "
                      f"({delta_pct:+.1f}%)",
                severity="alert",
                detail={"today": today_volume, "baseline_p50": base_vol,
                        "delta_pct": delta_pct},
            ))

    # Carrier × lane regressions (90-day baseline).
    base_cl = baselines.get("carrier_lane_winrate") or {}
    for key, today_rate in today_carrier_lane.items():
        base_rate = base_cl.get(key)
        if base_rate is None:
            continue
        delta_pp = today_rate - base_rate
        if delta_pp <= -20.0:
            out.append(Anomaly(
                kind="carrier_lane_drop",
                label=f"{key} win-rate dropped {abs(delta_pp):.1f}pp "
                      f"({base_rate}% → {today_rate}%)",
                severity="warn",
                detail={"key": key, "today": today_rate, "baseline": base_rate,
                        "delta_pp": round(delta_pp, 1)},
            ))

    return out


def _today_carrier_lane(requests: list[dict[str, Any]]) -> dict[str, float]:
    """Quick recomputation of carrier-lane win rate over the same data
    we're scoring today, for anomaly comparison vs the 90-day baseline."""
    bucket_won: Counter[str] = Counter()
    bucket_total: Counter[str] = Counter()
    for r in requests:
        carrier = (r.get("carrier_won") or r.get("carrier_quoted") or "").strip()
        dest = (r.get("destination") or "").strip()
        if not carrier or not dest or dest.lower() == "unknown":
            continue
        # Decided = WIN + Q&L + NQ (PENDING excluded — alive). Mirrors
        # baselines._carrier_lane_winrates so today's recomputation is
        # comparable to the persisted baseline.
        if r.get("status") not in ("WIN", "Q&L", "NQ"):
            continue
        key = f"{carrier}.{dest}"
        bucket_total[key] += 1
        if r.get("status") == "WIN":
            bucket_won[key] += 1
    return {
        k: round(100.0 * bucket_won[k] / total, 1)
        for k, total in bucket_total.items() if total >= 2
    }


def compute_parser_miss_patterns(
    miss_log_path: Path,
    *,
    top_n: int = 5,
    window_days: int = 7,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Group entries from ``parser_misses.jsonl`` by field, surface the
    top-N noisiest fields with one representative body excerpt each.

    Closes the parser learning loop: the system_critique prompt receives
    these patterns so the LLM can recommend a regex fix targeting a
    concrete excerpt instead of generic "audit the parser" advice.

    Returns a list of dicts, ordered by ``count`` descending::

        [{"field": "etd_offered", "count": 16,
          "llm_extracted": 12, "budget_skipped": 3, "llm_error": 1,
          "example_excerpt": "...first 240 chars..."}, ...]

    Quietly returns ``[]`` when the log is missing or empty (typical on
    fresh deploys before the LLM fallback has fired).
    """
    miss_log_path = Path(miss_log_path)
    if not miss_log_path.exists():
        return []
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=window_days)
    cutoff_iso = cutoff.isoformat()

    by_field: dict[str, dict[str, Any]] = {}
    try:
        with miss_log_path.open(encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if (rec.get("ts") or "") < cutoff_iso:
                    continue
                field_name = rec.get("field") or "unknown"
                slot = by_field.setdefault(field_name, {
                    "field": field_name,
                    "count": 0,
                    "llm_extracted": 0,
                    "budget_skipped": 0,
                    "llm_error": 0,
                    "example_excerpt": "",
                })
                slot["count"] += 1
                result = rec.get("result") or ""
                if result == "llm_extracted":
                    slot["llm_extracted"] += 1
                elif result == "skipped_budget":
                    slot["budget_skipped"] += 1
                elif result == "llm_error":
                    slot["llm_error"] += 1
                if not slot["example_excerpt"]:
                    slot["example_excerpt"] = (rec.get("body_excerpt") or "")[:240]
    except OSError:
        return []

    return sorted(by_field.values(), key=lambda d: -d["count"])[:top_n]


def build_context(
    *,
    tracking_data: dict[str, Any],
    qc_result: dict[str, Any] | None = None,
    baselines: dict[str, Any] | None = None,
    test_coverage_pct: float | None = None,
    parser_miss_log_path: Path | None = None,
    now: datetime | None = None,
) -> InsightsContext:
    """Pure compute. Builds the full :class:`InsightsContext` from the
    tracking-data snapshot + (optionally) qc-result.json + baselines
    subset. No LLM call.

    ``baselines`` should be the flattened dict written by
    :func:`hilmar.baselines.graft_into_tracking_data` — the qc-friendly
    subset.
    """
    now = now or datetime.now(timezone.utc)
    requests = tracking_data.get("requests") or []
    summary = tracking_data.get("summary") or {}
    qc_result = qc_result or {}
    baselines = baselines or (tracking_data.get("baselines") or {})

    # 14-day window for "delta vs baseline".
    cutoff_14 = now - timedelta(days=14)
    recent_14 = [
        r for r in requests
        if (baselines_mod.core.parse_iso(r.get("request_timestamp")) or now) >= cutoff_14
    ]

    # 4-state classifier: decided = WIN + Q&L + NQ (PENDING excluded).
    decided_14 = [r for r in recent_14 if r.get("status") in ("WIN", "Q&L", "NQ")]
    today_wins = sum(1 for r in decided_14 if r.get("status") == "WIN")
    today_win_rate = _safe_pct(today_wins, len(decided_14))

    biz_hours_today = [
        float(r["turnaround_biz_hours"]) for r in recent_14
        if isinstance(r.get("turnaround_biz_hours"), (int, float))
    ]
    today_response_p50 = (
        round(statistics.median(biz_hours_today), 2) if biz_hours_today else None
    )

    today_volume = sum(1 for r in requests if _is_today(r, now))
    today_cl = _today_carrier_lane(recent_14)

    anomalies = _detect_anomalies(
        today_win_rate=today_win_rate,
        today_response_p50=today_response_p50,
        today_volume=today_volume,
        baselines=baselines,
        today_carrier_lane=today_cl,
    )

    selfheal_actions = tracking_data.get("selfheal_actions") or []
    ingest_gap_flagged = any(a.get("kind") == "ingest_gap" for a in selfheal_actions)
    parser_miss_rates = dict(baselines.get("parser_miss_rate") or {})
    parser_miss_top_patterns = (
        compute_parser_miss_patterns(parser_miss_log_path, now=now)
        if parser_miss_log_path else []
    )

    return InsightsContext(
        total=int(summary.get("total_entries", 0)),
        wins=int(summary.get("wins", 0)),
        quoted_lost=int(summary.get("quoted_lost", 0)),
        not_quoted=int(summary.get("not_quoted", 0)),
        pending=int(summary.get("pending_hilmar", 0)),
        win_rate_pct=float(summary.get("win_rate", 0.0)),
        win_rate_delta_pp=(
            round(today_win_rate - baselines["win_rate_pct"], 1)
            if baselines.get("win_rate_pct") is not None else None
        ),
        response_time_delta_pct=_delta_pct(
            today_response_p50, baselines.get("biz_hours_response_p50"),
        ),
        volume_delta_pct=_delta_pct(
            float(today_volume), float(baselines["ingest_volume_p50"]),
        ) if baselines.get("ingest_volume_p50") else None,
        carrier_mix=_carrier_mix(recent_14),
        lane_top_3=_lane_top_3(recent_14),
        biggest_wins_today=_biggest_wins_today(requests, now),
        aging_pendings=_aging_pendings(requests),
        anomalies=anomalies,
        qc_fixes_today=int(qc_result.get("fixes", 0)),
        parser_miss_rates=parser_miss_rates,
        parser_miss_top_patterns=parser_miss_top_patterns,
        test_coverage_pct=test_coverage_pct,
        ingest_gap_flagged=ingest_gap_flagged,
        generated_at=now.isoformat(),
        data_window=tracking_data.get("date_range") or {},
    )


# ─────────────────────────────────────────────────────────────────────
# JSON-friendly output (for reports/insights/<date>.json)
# ─────────────────────────────────────────────────────────────────────


PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"

# Maps task type → prompt filename. Each prompt file is a system-style
# instruction; the user-message is the JSON-serialised InsightsContext.
TASK_PROMPTS: dict[str, str] = {
    "system_critique":     "system_critique.md",
    "design_suggestions":  "design_suggestions.md",
    "data_suggestions":    "data_suggestions.md",
    "business_advice":     "business_advice.md",
    # feedback_synthesis is wired separately because its input is the
    # feedback log, not the InsightsContext.
}


@dataclass
class NarrativeBundle:
    """All four LLM-narrative sections + cost / model metadata."""
    system: ModelResponse
    design: ModelResponse
    data: ModelResponse
    business: ModelResponse

    def total_cost_cents(self) -> int:
        return sum(r.cost_cents for r in (self.system, self.design, self.data, self.business))

    def any_skipped(self) -> bool:
        return any(r.skipped_reason for r in (self.system, self.design, self.data, self.business))


def _load_prompt(name: str) -> str:
    """Load a prompt file from src/hilmar/prompts. Returns "" if missing
    so the caller can degrade gracefully."""
    p = PROMPTS_DIR / name
    if not p.exists():
        log.warning("prompt file missing: %s", p)
        return ""
    return p.read_text(encoding="utf-8")


def generate_narrative(
    ctx: InsightsContext,
    *,
    router: ModelRouter,
    feedback_summary: str | None = None,
) -> NarrativeBundle:
    """Run the four task-keyed LLM calls and return a :class:`NarrativeBundle`.

    Each call is independent — failures in one task don't block the others
    (each gets its own ``ModelResponse`` with optional ``skipped_reason``).

    ``feedback_summary`` (optional) is the most recent
    :func:`feedback_synthesis` output; when provided it's appended to each
    task's system prompt as "what worked / what didn't" so the model
    biases toward signal Michael cares about.
    """
    ctx_json = json.dumps(context_to_dict(ctx), ensure_ascii=False, indent=2)

    def _call(task: str) -> ModelResponse:
        prompt_file = TASK_PROMPTS[task]
        system_prompt = _load_prompt(prompt_file)
        if feedback_summary:
            system_prompt += (
                "\n\n---\n\n"
                "## Recent feedback summary (use this to bias what you surface)\n\n"
                + feedback_summary
            )
        return router.call(
            task_type=task,
            prompt=ctx_json,
            system=system_prompt or None,
        )

    return NarrativeBundle(
        system=_call("system_critique"),
        design=_call("design_suggestions"),
        data=_call("data_suggestions"),
        business=_call("business_advice"),
    )


# Section subsets for staff-facing vs. internal-only narrative slices.
# Per Michael's 2026-04-28 directive: staff (Lonny + rate desk) should
# only see Business — the strategic/negotiation actions. System / Design
# / Data are operational-internal (parser miss rates, schema additions,
# carrier-rollup tweaks etc.) — those are for me and Michael to act on,
# not staff to read.
STAFF_SECTIONS: tuple[str, ...] = ("business",)
INTERNAL_SECTIONS: tuple[str, ...] = ("system", "design", "data", "business")


def render_narrative_html(
    bundle: NarrativeBundle,
    *,
    today_label: str | None = None,
    sections: tuple[str, ...] = INTERNAL_SECTIONS,
) -> str:
    """Render the narrative bundle as collapsible-friendly HTML.

    ``sections`` is the ordered subset of section keys to include.
    Default = all four (preserved for backward compatibility); callers
    pass ``STAFF_SECTIONS`` for the public daily email and
    ``INTERNAL_SECTIONS`` for the internal-only review.

    When ``today_label`` is provided (YYYY-MM-DD), each bullet is decorated
    with the 👍 / 👎 / 💤 mailto strip from
    :func:`hilmar.feedback_ingest.insights_feedback_strip` (M3.11.d).
    """
    from . import feedback_ingest as fb

    section_specs = {
        "system":   ("System",   bundle.system),
        "design":   ("Design",   bundle.design),
        "data":     ("Data",     bundle.data),
        "business": ("Business", bundle.business),
    }
    selected = [
        (slug, *section_specs[slug])
        for slug in sections
        if slug in section_specs
    ]
    sections = selected  # rebind to the iterable consumed below
    parts: list[str] = []
    for slug, label, resp in sections:
        if resp.skipped_reason:
            body = f"<em>(LLM-narrative skipped: {resp.skipped_reason})</em>"
        else:
            body = _markdown_bullets_to_html(
                resp.text or "",
                feedback_id_prefix=(
                    f"{today_label}.{slug}" if today_label else None
                ),
                feedback_strip_fn=fb.insights_feedback_strip,
            )
        parts.append(
            f"<h4 style='margin: 14px 0 6px 0; color: #0b3d91;'>{label}</h4>"
            f"<div style='font-size: 13px; line-height: 1.6;'>{body}</div>"
        )
    if bundle.any_skipped():
        parts.insert(
            0,
            "<p style='font-size: 12px; color: #92400e;'>"
            "⚠️ Some narrative sections fell back to rule-based output "
            "(see notes below).</p>",
        )
    return "\n".join(parts)


def _markdown_bullets_to_html(
    md: str,
    *,
    feedback_id_prefix: str | None = None,
    feedback_strip_fn=None,
) -> str:
    """Minimal Markdown → HTML for our narrative output. Handles top-level
    `-` bullets and inline ``**bold**`` / ``*italic*``. We don't need a
    full parser — the prompts ask for bullet lists only.

    If ``feedback_id_prefix`` and ``feedback_strip_fn`` are provided, each
    bullet is augmented with the M3.11.d 👍/👎/💤 mailto strip. The
    insight id is ``<prefix>.<idx>`` (1-based).
    """
    if not md.strip():
        return "<p>(no output)</p>"
    lines = md.strip().split("\n")
    out: list[str] = []
    in_ul = False
    bullet_idx = 0
    for raw in lines:
        line = raw.strip()
        if not line:
            if in_ul:
                out.append("</ul>")
                in_ul = False
            continue
        if line.startswith(("- ", "* ", "• ")):
            text = line[2:].strip()
            text = _inline_md(text)
            if not in_ul:
                out.append("<ul style='margin: 4px 0 8px 18px; padding: 0;'>")
                in_ul = True
            bullet_idx += 1
            strip = ""
            if feedback_id_prefix and feedback_strip_fn:
                insight_id = f"{feedback_id_prefix}.{bullet_idx}"
                strip = " " + feedback_strip_fn(insight_id)
            out.append(f"<li>{text}{strip}</li>")
        else:
            if in_ul:
                out.append("</ul>")
                in_ul = False
            out.append(f"<p style='margin: 6px 0;'>{_inline_md(line)}</p>")
    if in_ul:
        out.append("</ul>")
    return "\n".join(out)


def _inline_md(s: str) -> str:
    import re

    # Escape HTML first (very minimal — these come from Anthropic which
    # already returns plain text, but defence-in-depth).
    s = (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
    s = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"(?<!\w)\*([^*]+)\*(?!\w)", r"<em>\1</em>", s)
    s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
    return s


def context_to_dict(ctx: InsightsContext) -> dict[str, Any]:
    """Coerce InsightsContext to a JSON-safe dict (lane_top_3 is a
    list[tuple], turn that into list[list] for JSON)."""
    return {
        "total": ctx.total,
        "wins": ctx.wins,
        "quoted_lost": ctx.quoted_lost,
        "not_quoted": ctx.not_quoted,
        "pending": ctx.pending,
        "win_rate_pct": ctx.win_rate_pct,
        "win_rate_delta_pp": ctx.win_rate_delta_pp,
        "response_time_delta_pct": ctx.response_time_delta_pct,
        "volume_delta_pct": ctx.volume_delta_pct,
        "carrier_mix": dict(ctx.carrier_mix),
        "lane_top_3": [list(p) for p in ctx.lane_top_3],
        "biggest_wins_today": ctx.biggest_wins_today,
        "aging_pendings": ctx.aging_pendings,
        "anomalies": [
            {"kind": a.kind, "label": a.label, "severity": a.severity, "detail": a.detail}
            for a in ctx.anomalies
        ],
        "qc_fixes_today": ctx.qc_fixes_today,
        "parser_miss_rates": dict(ctx.parser_miss_rates),
        "parser_miss_top_patterns": list(ctx.parser_miss_top_patterns),
        "test_coverage_pct": ctx.test_coverage_pct,
        "ingest_gap_flagged": ctx.ingest_gap_flagged,
        "generated_at": ctx.generated_at,
        "data_window": ctx.data_window,
    }
