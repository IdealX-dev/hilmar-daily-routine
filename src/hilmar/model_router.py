"""
hilmar.model_router — Anthropic model selection + cost telemetry.

Default model is Opus (per Michael's pick 2026-04-26). Per-task env
override (``HILMAR_INSIGHTS_MODEL_<task>``) lets us dial down to
Sonnet/Haiku without touching code. A global override
(``HILMAR_INSIGHTS_MODEL``) takes precedence for any task that doesn't
have a specific override set.

Cost telemetry: every call appends one JSONL line to
``HILMAR_LLM_COST_LOG`` (default ``data/llm-cost-log.jsonl``). The
``daily_cost_cents()`` helper sums today's spend; the orchestrator
uses it to append a banner to the daily email when spend exceeds
``HILMAR_INSIGHTS_COST_ALERT_CENTS`` (default 200 = $2.00). **No hard
cap** — we inform, we don't halt.

Cascade-down on errors:
  * 429 → retry once with same model, then fall back to Sonnet for
    THAT task only. Log it.
  * API down (connection error) → return ``ModelResponse(text="",
    skipped_reason="api_unavailable", ...)`` so caller can ship the
    rule-based metrics + a "LLM-narrative skipped" note.

Tests MUST mock the Anthropic client. Real API calls in CI are forbidden.
"""
from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


# Default model; tightly hard-coded — the env override is the dial.
# Migrated claude-opus-4-6 -> claude-opus-5 on 2026-07-27. Three Opus
# generations forward, and it changes two things this file has to handle:
#
#   1. THINKING IS ON BY DEFAULT. On Opus 4.6 (and 4.7/4.8) a request that
#      omits the `thinking` parameter ran WITHOUT thinking. On Opus 5 the
#      same request runs adaptive thinking — and `max_tokens` is a hard cap
#      on thinking PLUS response text together. `_invoke` sends no
#      `thinking` field, so every narrative call would silently start
#      spending its 4096-token budget on reasoning and truncate the answer.
#      Handled by MAX_TOKENS_FLOOR below.
#   2. The prompt-cache minimum drops 4096 -> 512 tokens, which is what
#      makes caching viable here at all (see CACHE_SYSTEM_PROMPT).
#
# Sampling parameters (`temperature`/`top_p`/`top_k`) and `budget_tokens`
# return 400 on Opus 4.7+; this file never sent any, so nothing to strip.
DEFAULT_MODEL = "claude-opus-5"

#: Floor for `max_tokens` on thinking-capable models. Opus 5 counts thinking
#: tokens against `max_tokens`, so a budget sized for the answer alone gets
#: eaten by reasoning and the response truncates mid-sentence (`stop_reason:
#: "max_tokens"`). The narrative sections are a few hundred tokens of prose;
#: 4096 was ample when none of it went to thinking and is not now. Callers
#: asking for less than this on a thinking model are raised to it.
MAX_TOKENS_FLOOR = 16000

#: Models that think by default when `thinking` is omitted. Kept explicit
#: rather than inferred from the name so a future model has to be added
#: deliberately — guessing wrong here truncates production output silently.
THINKING_BY_DEFAULT = frozenset({"claude-opus-5"})

#: Minimum cacheable prefix, in tokens, per model. NOT monotonic across
#: generations — Opus 4.6 needs 4096, Opus 5 only 512 — which is why the
#: Opus 5 migration is what makes caching viable rather than the marker
#: itself. A prefix below the model's minimum silently does not cache.
#: Models absent from this map are never marked.
CACHE_MIN_TOKENS = {
    "claude-opus-5":     512,
    "claude-sonnet-4-6": 1024,
    "claude-haiku-4-5":  4096,
}

#: Kill switch for the system-prompt cache breakpoint. On by default; set
#: HILMAR_LLM_CACHE=0 to send plain-string system prompts again if a cache
#: interaction is ever suspected in a live incident.
CACHE_SYSTEM_PROMPT = os.environ.get("HILMAR_LLM_CACHE", "1").strip() != "0"

# Per-task model defaults. Insights tasks (narrative, critique, etc.)
# default to Opus — quality-bound. Parser extraction is high-volume and
# structurally simple (extract a field from a few hundred lines of
# email text → JSON), so it defaults to Haiku for cost efficiency.
# Per-task env overrides (HILMAR_INSIGHTS_MODEL_<task>) still win.
_TASK_MODEL_DEFAULTS: dict[str, str] = {
    "parser_extraction": "claude-haiku-4-5",
}

TASK_TYPES = (
    "metrics_narrative",
    "system_critique",
    "design_suggestions",
    "data_suggestions",
    "business_advice",
    "feedback_synthesis",
    # Per-field structured extraction from email bodies when the regex
    # parsers in body_parser.py return None. Cheap model by design —
    # high call volume, simple JSON output, latency matters per call.
    "parser_extraction",
)

# Cost table: cents per million tokens. Approximate Anthropic public list
# pricing as of 2026-04-26 — overridable via env if it shifts.
DEFAULT_PRICING_CPM = {
    # Claude Opus 5 — $5.00 / $25.00 per MTok (list, 2026-07-27).
    "claude-opus-5":        {"input":  500, "output": 2500},
    # NOTE (2026-07-27): this claude-opus-4-6 row says $15/$75 per MTok.
    # Anthropic's published price for Opus 4.6 is $5.00/$25.00 — the same as
    # Opus 5 — so this entry is ~3x high and every cost_cents figure logged
    # against Opus 4.6 has been overstated by that factor. Left as-is
    # deliberately: correcting it retroactively would make historical
    # llm-cost-log entries incomparable, and that is Michael's call, not a
    # silent edit. Flagged in CHANGELOG; the new Opus 5 row above is correct.
    "claude-opus-4-6":      {"input": 1500, "output": 7500},
    "claude-sonnet-4-6":    {"input":  300, "output": 1500},
    "claude-haiku-4-5":     {"input":   80, "output":  400},
}


# ─────────────────────────────────────────────────────────────────────
# Public types
# ─────────────────────────────────────────────────────────────────────


@dataclass
class ModelResponse:
    text: str
    model: str
    task_type: str
    input_tokens: int
    output_tokens: int
    cost_cents: int
    skipped_reason: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class ModelRouterError(RuntimeError):
    """Hard error — neither primary nor cascade-down model could respond."""


# ─────────────────────────────────────────────────────────────────────
# Anthropic client adapter
# ─────────────────────────────────────────────────────────────────────


def _default_anthropic_client() -> Any:
    """Lazy-import the Anthropic client — keeps the dependency optional
    for tests that mock the call entirely.
    """
    try:
        import anthropic  # type: ignore[import-not-found]
    except ImportError as e:  # pragma: no cover
        raise ModelRouterError(
            "anthropic SDK not installed; pip install anthropic, or pass a "
            "client to ModelRouter() in tests.",
        ) from e
    return anthropic.Anthropic()


# ─────────────────────────────────────────────────────────────────────
# ModelRouter
# ─────────────────────────────────────────────────────────────────────


class ModelRouter:
    """Selects a model per task and records cost.

    Tests inject ``client_factory=lambda: stub`` so we never make real
    API calls.
    """

    def __init__(
        self,
        *,
        cost_log_path: Path | None = None,
        client_factory: Callable[[], Any] | None = None,
        pricing: dict[str, dict[str, int]] | None = None,
        now_factory: Callable[[], datetime] | None = None,
    ) -> None:
        self._cost_log_path = cost_log_path or _default_cost_log_path()
        self._client_factory = client_factory or _default_anthropic_client
        self._pricing = pricing or DEFAULT_PRICING_CPM
        self._now = now_factory or (lambda: datetime.now(timezone.utc))
        self._client_cache: Any | None = None

    # ── model selection ──────────────────────────────────────────────

    def select(self, task_type: str) -> str:
        """Resolve the model name for ``task_type``. Resolution order:
        per-task env override → global env override → per-task default
        in _TASK_MODEL_DEFAULTS → DEFAULT_MODEL."""
        env_specific = os.environ.get(f"HILMAR_INSIGHTS_MODEL_{task_type}")
        if env_specific:
            return env_specific.strip()
        env_global = os.environ.get("HILMAR_INSIGHTS_MODEL")
        if env_global:
            return env_global.strip()
        if task_type in _TASK_MODEL_DEFAULTS:
            return _TASK_MODEL_DEFAULTS[task_type]
        return DEFAULT_MODEL

    # ── cost helpers ─────────────────────────────────────────────────

    def _system_param(self, model: str, system: str):
        """Render `system` as either a plain string or a cache-marked block.

        Prompt caching is a PREFIX match with a model-specific minimum, and
        below that minimum `cache_control` is a silent no-op — no error, no
        cache, just the ~1.25x write premium. The minimum is not monotonic
        across generations: 4096 tokens on Opus 4.6, 512 on Opus 5. That 8x
        drop is the only reason a breakpoint is worth setting here at all.

        Marking the system block caches `tools` + `system` together (render
        order is tools -> system -> messages). It does NOT reorder anything,
        so model output is unchanged — the request bytes are identical apart
        from the marker.

        WHAT THIS DOES AND DOESN'T BUY (measured 2026-07-27, not estimated
        from the docs): the four insights tasks each send a DIFFERENT system
        prompt, so they do not share a prefix with each other, and the daily
        pipeline runs once every 24h while the cache TTL is 5 minutes (1h
        max). So there is no cross-task and no cross-run hit. What it does
        cover is the retry and Sonnet-cascade paths in `call`, which re-send
        an identical prefix seconds apart — exactly the case a cache is for.
        The larger win needs the shared context moved ahead of the per-task
        instruction so all four calls share a prefix; that changes prompt
        structure and therefore output, so it is a separate decision.
        """
        if not CACHE_SYSTEM_PROMPT or model not in CACHE_MIN_TOKENS:
            return system
        # Rough gate so we don't mark blocks that cannot possibly clear the
        # model's minimum. Deliberately conservative (4 chars/token) — a
        # marker below the minimum is harmless but misleading to read.
        if len(system) < CACHE_MIN_TOKENS[model] * 4:
            return system
        return [{"type": "text", "text": system,
                 "cache_control": {"type": "ephemeral"}}]

    def _cost_cents(self, model: str, in_tok: int, out_tok: int) -> int:
        prices = self._pricing.get(model) or self._pricing.get(DEFAULT_MODEL) or {"input": 0, "output": 0}
        cents = (in_tok / 1_000_000) * prices["input"] + (out_tok / 1_000_000) * prices["output"]
        return int(round(cents))

    def _append_cost_log(self, entry: dict[str, Any]) -> None:
        self._cost_log_path.parent.mkdir(parents=True, exist_ok=True)
        with self._cost_log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    # ── primary call ─────────────────────────────────────────────────

    def call(
        self,
        *,
        task_type: str,
        prompt: str,
        system: str | None = None,
        max_tokens: int = 4096,
    ) -> ModelResponse:
        """Run ``prompt`` against the model selected for ``task_type``.

        Returns a :class:`ModelResponse`. On 429 → retries once same
        model, then cascades to ``claude-sonnet-4-6`` for THIS task.
        On connection error → returns a ``skipped_reason`` response so
        the orchestrator can ship rule-based output anyway.
        """
        if task_type not in TASK_TYPES:
            raise ValueError(f"unknown task_type: {task_type!r} (allowed: {TASK_TYPES})")

        primary_model = self.select(task_type)
        try:
            return self._invoke(primary_model, task_type, prompt, system, max_tokens)
        except _RetryableThrottle:
            log.warning("rate-limited on %s for %s — retrying once", primary_model, task_type)
            try:
                return self._invoke(primary_model, task_type, prompt, system, max_tokens)
            except _RetryableThrottle:
                fallback = "claude-sonnet-4-6"
                log.warning(
                    "rate-limited twice on %s — cascading %s to %s",
                    primary_model, task_type, fallback,
                )
                try:
                    return self._invoke(fallback, task_type, prompt, system, max_tokens, cascade=True)
                except _RetryableThrottle as e:
                    raise ModelRouterError(
                        f"throttled on both {primary_model} and {fallback}",
                    ) from e
        except _ApiUnavailable as e:
            log.warning("anthropic API unavailable: %s — skipping LLM narrative", e)
            return ModelResponse(
                text="", model=primary_model, task_type=task_type,
                input_tokens=0, output_tokens=0, cost_cents=0,
                skipped_reason="api_unavailable",
                metadata={"error": str(e)},
            )

    # ── single-attempt invocation ────────────────────────────────────

    def _invoke(
        self,
        model: str,
        task_type: str,
        prompt: str,
        system: str | None,
        max_tokens: int,
        *,
        cascade: bool = False,
    ) -> ModelResponse:
        if self._client_cache is None:
            self._client_cache = self._client_factory()
        client = self._client_cache

        # Opus 5 counts thinking against max_tokens (see DEFAULT_MODEL note).
        # Raise the ceiling rather than disabling thinking: disabling it is
        # capped at effort<=high on Opus 5, and it carries two documented
        # failure modes (tool calls emitted as plain text; <thinking> tags
        # leaking into the response). Thinking on with a real budget is the
        # safe configuration.
        effective_max = (max(max_tokens, MAX_TOKENS_FLOOR)
                         if model in THINKING_BY_DEFAULT else max_tokens)

        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": effective_max,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system is not None:
            kwargs["system"] = self._system_param(model, system)

        try:
            raw = client.messages.create(**kwargs)
        except Exception as e:  # noqa: BLE001 — translate library errors
            translated = _translate_anthropic_error(e)
            raise translated from e

        # Pull text + token usage in a stub-friendly way.
        text = _coerce_text(raw)
        in_tok, out_tok = _coerce_usage(raw)
        cost = self._cost_cents(model, in_tok, out_tok)

        self._append_cost_log({
            "ts": self._now().isoformat(),
            "task": task_type,
            "model": model,
            "input_tokens": in_tok,
            "output_tokens": out_tok,
            "cost_cents": cost,
            "cascade": cascade,
        })
        return ModelResponse(
            text=text, model=model, task_type=task_type,
            input_tokens=in_tok, output_tokens=out_tok, cost_cents=cost,
            metadata={"cascade": cascade},
        )

    # ── daily cost telemetry ─────────────────────────────────────────

    def daily_cost_cents(self, *, day: datetime | None = None) -> int:
        """Sum cost_cents for entries in the cost log dated ``day`` (UTC).
        Default: today (UTC). Returns 0 if log file doesn't exist."""
        if not self._cost_log_path.exists():
            return 0
        anchor = (day or self._now()).date().isoformat()
        total = 0
        with self._cost_log_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if (rec.get("ts") or "").startswith(anchor):
                    total += int(rec.get("cost_cents", 0))
        return total

    # ── alert helpers ────────────────────────────────────────────────

    def cost_alert_threshold_cents(self) -> int:
        raw = os.environ.get("HILMAR_INSIGHTS_COST_ALERT_CENTS", "200")
        try:
            return max(0, int(raw))
        except ValueError:
            return 200

    def should_alert_cost(self) -> bool:
        return self.daily_cost_cents() > self.cost_alert_threshold_cents()


# ─────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────


class _RetryableThrottle(Exception):
    """429-ish — caller decides to retry / cascade."""


class _ApiUnavailable(Exception):
    """Connection-level error; cannot reach the API at all."""


def _translate_anthropic_error(e: Exception) -> Exception:
    """Map library exceptions to our internal ones in a stub-friendly way."""
    type_name = type(e).__name__.lower()
    msg = str(e).lower()
    if "ratelimit" in type_name or "429" in msg:
        return _RetryableThrottle(str(e))
    if any(t in type_name for t in ("apiconnection", "apitimeout", "connectionerror")):
        return _ApiUnavailable(str(e))
    return e


def _coerce_text(raw: Any) -> str:
    """Anthropic SDK responses have ``content`` (list of blocks). The
    first ``text`` block is what we want. Stub-friendly — also accepts
    a plain string or a dict.
    """
    if isinstance(raw, str):
        return raw
    content = getattr(raw, "content", None)
    if content is None and isinstance(raw, dict):
        content = raw.get("content")
    if not content:
        return ""
    out_parts: list[str] = []
    for block in content:
        if isinstance(block, dict):
            if block.get("type") == "text" or "text" in block:
                out_parts.append(block.get("text") or "")
        else:
            block_text = getattr(block, "text", None)
            if block_text:
                out_parts.append(block_text)
    return "".join(out_parts)


def _coerce_usage(raw: Any) -> tuple[int, int]:
    usage = getattr(raw, "usage", None)
    if usage is None and isinstance(raw, dict):
        usage = raw.get("usage")
    if usage is None:
        return 0, 0
    if isinstance(usage, dict):
        return int(usage.get("input_tokens", 0)), int(usage.get("output_tokens", 0))
    return int(getattr(usage, "input_tokens", 0)), int(getattr(usage, "output_tokens", 0))


def _default_cost_log_path() -> Path:
    env = os.environ.get("HILMAR_LLM_COST_LOG")
    if env:
        return Path(env)
    # Fallback aligns with paths.data_dir() but keeps this module
    # importable without paths to avoid circular deps in early bootstraps.
    base = os.environ.get("HILMAR_DATA_DIR", "/opt/hilmar-tracker/data")
    return Path(base) / "llm-cost-log.jsonl"
