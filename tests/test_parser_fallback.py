"""Tests for hilmar.parser_fallback — LLM-augmented field extraction.

Covers the four operational paths: regex hit (no LLM), cache hit
(no LLM), LLM call (one call, cached + logged), budget exhausted
(no LLM, skip logged). The MarkRouter stub avoids real Anthropic
calls in CI."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from hilmar import parser_fallback as pf


@dataclass
class StubResp:
    text: str
    model: str = "claude-haiku-4-5"
    task_type: str = "parser_extraction"
    input_tokens: int = 0
    output_tokens: int = 0
    cost_cents: int = 0
    skipped_reason: str | None = None


class StubRouter:
    """No-network ModelRouter substitute. Records every call."""

    def __init__(self, responses: list[str] | None = None) -> None:
        self.responses = responses or []
        self.calls: list[dict] = []

    def call(self, *, task_type, prompt, system=None, max_tokens=200):
        self.calls.append({"task_type": task_type, "prompt": prompt})
        text = self.responses.pop(0) if self.responses else '{"value": null, "confidence": "low"}'
        return StubResp(text=text)


def _ctx(tmp_path: Path, *, budget: int = 20, router: StubRouter | None = None) -> pf.ParserFallbackContext:
    return pf.ParserFallbackContext(
        cache_path=tmp_path / "parser_cache.json",
        miss_log_path=tmp_path / "parser_misses.jsonl",
        budget=budget,
        router=router,
    )


def test_regex_hit_passes_through_no_llm_call(tmp_path: Path):
    """When regex returned a non-empty value, return it as-is. No LLM
    call, no cache write, no miss-log entry."""
    router = StubRouter()
    ctx = _ctx(tmp_path, router=router)
    out = pf.extract_with_fallback("ol_rate", "body content", 540.0, ctx=ctx)
    assert out == 540.0
    assert router.calls == []
    assert ctx.calls_made == 0
    assert not ctx.miss_log_path.exists()


def test_unknown_field_passes_regex_through_even_when_none(tmp_path: Path):
    """Fields outside FALLBACK_FIELDS should not trigger LLM (avoids
    using the fallback on prompts we haven't vetted)."""
    router = StubRouter()
    ctx = _ctx(tmp_path, router=router)
    out = pf.extract_with_fallback("not_a_real_field", "body", None, ctx=ctx)
    assert out is None
    assert router.calls == []


def test_regex_miss_triggers_llm_extraction_and_caches(tmp_path: Path):
    """Regex returned None → LLM called → result returned + cached +
    logged to misses."""
    router = StubRouter(responses=['{"value": 5400, "confidence": "high"}'])
    ctx = _ctx(tmp_path, router=router)
    body = "Rate quote: $5400 for 40HC, ETD 2026-05-15"
    out = pf.extract_with_fallback("ol_rate", body, None, ctx=ctx)
    assert out == 5400
    assert ctx.calls_made == 1
    # Cache written for next run
    assert len(ctx.cache) == 1
    cache_entry = next(iter(ctx.cache.values()))
    assert cache_entry["value"] == 5400
    assert cache_entry["confidence"] == "high"
    # Miss log appended
    assert ctx.miss_log_path.exists()
    log_lines = ctx.miss_log_path.read_text().strip().splitlines()
    assert len(log_lines) == 1
    rec = json.loads(log_lines[0])
    assert rec["field"] == "ol_rate"
    assert rec["value"] == 5400
    assert rec["result"] == "llm_extracted"


def test_cache_hit_avoids_llm_on_repeat_call(tmp_path: Path):
    """Same body + field → cache hit, no second LLM call."""
    router = StubRouter(responses=['{"value": "MSC", "confidence": "high"}'])
    ctx = _ctx(tmp_path, router=router)
    body = "Booking confirmed via MSC OSCAR"
    first = pf.extract_with_fallback("carrier_quoted", body, None, ctx=ctx)
    assert first == "MSC"
    assert ctx.calls_made == 1
    second = pf.extract_with_fallback("carrier_quoted", body, None, ctx=ctx)
    assert second == "MSC"
    assert ctx.calls_made == 1, "second call should be served by cache"


def test_budget_exhausted_skips_llm_and_logs(tmp_path: Path):
    """Once budget is consumed, fallback is a no-op + logs the skip."""
    router = StubRouter()
    ctx = _ctx(tmp_path, budget=0, router=router)
    out = pf.extract_with_fallback("ol_rate", "some body", None, ctx=ctx)
    assert out is None
    assert router.calls == []
    assert ctx.skips == 1
    log_lines = ctx.miss_log_path.read_text().strip().splitlines()
    rec = json.loads(log_lines[0])
    assert rec["result"] == "skipped_budget"


def test_low_confidence_result_not_cached(tmp_path: Path):
    """Low-confidence extractions get returned but NOT cached — gives
    next run a chance to re-extract with potentially better context."""
    router = StubRouter(responses=['{"value": "maybe-msc", "confidence": "low"}'])
    ctx = _ctx(tmp_path, router=router)
    body = "ambiguous rate response"
    out = pf.extract_with_fallback("carrier_quoted", body, None, ctx=ctx)
    assert out == "maybe-msc"
    assert len(ctx.cache) == 0  # NOT cached


def test_llm_error_falls_back_to_regex_value_quietly(tmp_path: Path):
    """If the LLM call raises, the function returns the original regex
    value (None) and logs the error. NEVER raises — daily run must not
    die on a parser fallback exception."""
    class ExplodingRouter:
        def call(self, **kwargs):
            raise RuntimeError("simulated API outage")

    ctx = _ctx(tmp_path, router=ExplodingRouter())
    out = pf.extract_with_fallback("ol_rate", "body", None, ctx=ctx)
    assert out is None
    log_lines = ctx.miss_log_path.read_text().strip().splitlines()
    rec = json.loads(log_lines[0])
    assert rec["result"] == "llm_error"


def test_malformed_llm_response_returns_regex_value(tmp_path: Path):
    """LLM might wrap JSON in prose or return garbage. Fallback must
    handle gracefully — no exception, return regex value."""
    router = StubRouter(responses=["I think the rate is $5400 maybe"])
    ctx = _ctx(tmp_path, router=router)
    out = pf.extract_with_fallback("ol_rate", "body", None, ctx=ctx)
    # No JSON in response → can't parse → fall back to regex value (None).
    assert out is None


def test_llm_response_with_code_fences_parses_correctly(tmp_path: Path):
    """LLMs sometimes wrap JSON in markdown code fences. Strip them."""
    router = StubRouter(responses=[
        '```json\n{"value": "Maersk", "confidence": "high"}\n```'
    ])
    ctx = _ctx(tmp_path, router=router)
    out = pf.extract_with_fallback("carrier_quoted", "body", None, ctx=ctx)
    assert out == "Maersk"


def test_persist_cache_writes_then_reload(tmp_path: Path):
    """Persisted cache survives across ParserFallbackContext instances —
    tomorrow's run picks up today's cache."""
    router = StubRouter(responses=['{"value": "MSC", "confidence": "high"}'])
    ctx1 = pf.ParserFallbackContext.from_data_dir(tmp_path)
    ctx1.router = router
    pf.extract_with_fallback("carrier_quoted", "body content", None, ctx=ctx1)
    ctx1.persist_cache()

    ctx2 = pf.ParserFallbackContext.from_data_dir(tmp_path)
    # No router needed — should hit the cache.
    out = pf.extract_with_fallback("carrier_quoted", "body content", None, ctx=ctx2)
    assert out == "MSC"
