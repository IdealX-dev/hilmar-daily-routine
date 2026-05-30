"""Tests for the pure helper functions in hilmar.model_router — response
coercion, usage parsing, error translation, and cost-log path resolution.
These were uncovered branches surfaced by QC-052 / run_audit_tests.py as
part of the 2026-05-28 push toward fuller coverage. They're stub-friendly
by design (the SDK response shape varies), so the variants matter."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from hilmar import model_router as MR


# ── _coerce_text ─────────────────────────────────────────────────────────────

def test_coerce_text_plain_string():
    assert MR._coerce_text("hello") == "hello"


def test_coerce_text_from_dict_content_blocks():
    raw = {"content": [{"type": "text", "text": "part1"}, {"type": "text", "text": "part2"}]}
    assert MR._coerce_text(raw) == "part1part2"


def test_coerce_text_from_object_with_content_attr():
    class Block:
        def __init__(self, text):
            self.text = text

    class Resp:
        content = [Block("alpha"), Block("beta")]

    assert MR._coerce_text(Resp()) == "alphabeta"


def test_coerce_text_empty_content_returns_empty():
    assert MR._coerce_text({"content": []}) == ""
    assert MR._coerce_text({}) == ""


def test_coerce_text_dict_block_with_text_key_no_type():
    raw = {"content": [{"text": "just text, no type"}]}
    assert MR._coerce_text(raw) == "just text, no type"


# ── _coerce_usage ────────────────────────────────────────────────────────────

def test_coerce_usage_from_dict():
    raw = {"usage": {"input_tokens": 120, "output_tokens": 45}}
    assert MR._coerce_usage(raw) == (120, 45)


def test_coerce_usage_from_object():
    class Usage:
        input_tokens = 7
        output_tokens = 3

    class Resp:
        usage = Usage()

    assert MR._coerce_usage(Resp()) == (7, 3)


def test_coerce_usage_missing_returns_zeros():
    assert MR._coerce_usage({}) == (0, 0)
    assert MR._coerce_usage(object()) == (0, 0)


# ── _translate_anthropic_error ───────────────────────────────────────────────

def test_translate_rate_limit_by_type_name():
    class RateLimitError(Exception):
        pass

    out = MR._translate_anthropic_error(RateLimitError("slow down"))
    assert isinstance(out, MR._RetryableThrottle)


def test_translate_429_by_message():
    out = MR._translate_anthropic_error(Exception("HTTP 429 too many requests"))
    assert isinstance(out, MR._RetryableThrottle)


def test_translate_connection_error_to_unavailable():
    class APIConnectionError(Exception):
        pass

    out = MR._translate_anthropic_error(APIConnectionError("dns fail"))
    assert isinstance(out, MR._ApiUnavailable)


def test_translate_timeout_to_unavailable():
    class APITimeoutError(Exception):
        pass

    out = MR._translate_anthropic_error(APITimeoutError("deadline exceeded"))
    assert isinstance(out, MR._ApiUnavailable)


def test_translate_unknown_error_passes_through():
    err = ValueError("something else")
    assert MR._translate_anthropic_error(err) is err


# ── _default_cost_log_path ───────────────────────────────────────────────────

def test_cost_log_path_uses_explicit_env(monkeypatch, tmp_path):
    target = tmp_path / "custom-cost.jsonl"
    monkeypatch.setenv("HILMAR_LLM_COST_LOG", str(target))
    assert MR._default_cost_log_path() == target


def test_cost_log_path_falls_back_to_data_dir(monkeypatch, tmp_path):
    monkeypatch.delenv("HILMAR_LLM_COST_LOG", raising=False)
    monkeypatch.setenv("HILMAR_DATA_DIR", str(tmp_path))
    assert MR._default_cost_log_path() == tmp_path / "llm-cost-log.jsonl"
