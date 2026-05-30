"""Targeted tests for the uncovered branches in hilmar.parser_fallback —
cache hit short-circuit, budget exhaustion, ModelRouter init failure,
corrupt cache file recovery. The parser_fallback layer is what kicks in
when regex parsing fails; coverage there matters because failures cost
LLM tokens and we need to know the safety paths work."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from hilmar import parser_fallback as PF


def _ctx(tmp_path: Path, *, budget: int = 5, router=None, cache: dict | None = None) -> PF.ParserFallbackContext:
    return PF.ParserFallbackContext(
        cache_path=tmp_path / "parser_cache.json",
        miss_log_path=tmp_path / "parser_misses.jsonl",
        budget=budget,
        router=router,
        cache=cache or {},
    )


# ── extract_with_fallback short-circuit branches ────────────────────────────

def test_regex_value_present_short_circuits(tmp_path):
    """When the regex parser already returned a value, fallback returns it
    immediately — never touches cache, never calls the router."""
    ctx = _ctx(tmp_path)
    out = PF.extract_with_fallback("ol_rate", "any body text", "3500", ctx=ctx)
    assert out == "3500"
    assert ctx.calls_made == 0


def test_unknown_field_short_circuits(tmp_path):
    """Fields not in FALLBACK_FIELDS get no LLM lookup."""
    ctx = _ctx(tmp_path)
    out = PF.extract_with_fallback("not_a_real_field", "body", None, ctx=ctx)
    assert out is None
    assert ctx.calls_made == 0


def test_empty_body_short_circuits(tmp_path):
    """No body → no LLM call."""
    ctx = _ctx(tmp_path)
    out = PF.extract_with_fallback("ol_rate", "", None, ctx=ctx)
    assert out is None


def test_cache_hit_returns_cached_value(tmp_path):
    """Cache hit must short-circuit before any router call (line 240)."""
    body = "Rate confirmed: $3,500."
    key = PF._cache_key("ol_rate", body)
    ctx = _ctx(tmp_path, cache={key: {"value": 3500, "confidence": 0.95}})
    out = PF.extract_with_fallback("ol_rate", body, None, ctx=ctx)
    assert out == 3500
    assert ctx.calls_made == 0


def test_budget_exhausted_logs_skip_and_returns_regex(tmp_path):
    """Once calls_made >= budget, we skip — log a miss with result=skipped_budget
    and return the regex value (which might be None)."""
    ctx = _ctx(tmp_path, budget=2)
    ctx.calls_made = 2  # at budget
    out = PF.extract_with_fallback("ol_rate", "Long body text", None, ctx=ctx)
    assert out is None
    assert ctx.skips == 1
    # Miss log should have ONE skipped_budget entry
    missfile = ctx.miss_log_path
    assert missfile.exists()
    lines = [json.loads(ln) for ln in missfile.read_text().splitlines() if ln.strip()]
    assert any(r.get("result") == "skipped_budget" for r in lines)


def test_router_init_failure_returns_regex_value(tmp_path):
    """If ModelRouter() construction raises (no API key, etc.), we log a
    warning and return the regex value rather than crashing the pipeline."""
    ctx = _ctx(tmp_path)
    with patch.object(PF, "ModelRouter", side_effect=RuntimeError("no API key")):
        out = PF.extract_with_fallback("ol_rate", "Rate: see attachment", "fallback_default", ctx=ctx)
    # Regex value (not None this time) → returns it; router init never matters
    assert out == "fallback_default"


def test_router_init_failure_with_none_regex_returns_none(tmp_path):
    """Same path but regex returned nothing — we still return cleanly (None)."""
    ctx = _ctx(tmp_path)
    with patch.object(PF, "ModelRouter", side_effect=RuntimeError("no API key")):
        out = PF.extract_with_fallback("ol_rate", "Long body text", None, ctx=ctx)
    assert out is None


# ── from_data_dir + persist_cache ───────────────────────────────────────────

def test_from_data_dir_handles_corrupt_cache(tmp_path):
    """A corrupt parser_cache.json on disk must NOT crash startup — log
    and start fresh. Lines 151-152."""
    (tmp_path / "parser_cache.json").write_text("{not valid json")
    ctx = PF.ParserFallbackContext.from_data_dir(tmp_path)
    # Started fresh, no exception raised
    assert ctx.cache == {}


def test_from_data_dir_loads_existing_cache(tmp_path):
    """Valid cache file is restored on startup."""
    (tmp_path / "parser_cache.json").write_text('{"abc123": {"value": 42}}')
    ctx = PF.ParserFallbackContext.from_data_dir(tmp_path)
    assert ctx.cache == {"abc123": {"value": 42}}


def test_persist_cache_writes_disk(tmp_path):
    ctx = _ctx(tmp_path, cache={"k": {"value": "v"}})
    ctx.persist_cache()
    on_disk = json.loads(ctx.cache_path.read_text())
    assert on_disk == {"k": {"value": "v"}}


# ── _parse_llm_json edge cases ──────────────────────────────────────────────

def test_parse_llm_json_strips_code_fences():
    assert PF._parse_llm_json('```json\n{"value": 42}\n```') == {"value": 42}
    assert PF._parse_llm_json('```\n{"value": "x"}\n```') == {"value": "x"}


def test_parse_llm_json_returns_none_on_invalid():
    assert PF._parse_llm_json("") is None
    assert PF._parse_llm_json(None) is None  # type: ignore[arg-type]
    assert PF._parse_llm_json("no json here at all") is None
    assert PF._parse_llm_json("{this is not valid json}") is None


def test_parse_llm_json_rejects_non_dict():
    """An LLM returning a bare array '[]' is not a dict — return None."""
    assert PF._parse_llm_json("[1, 2, 3]") is None
