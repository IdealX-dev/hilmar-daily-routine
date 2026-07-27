"""Opus 5 migration + prompt-cache breakpoint — ModelRouter.

TWO THINGS CHANGED WHEN DEFAULT_MODEL MOVED claude-opus-4-6 -> claude-opus-5
(2026-07-27), and both are silent failures if unhandled:

  1. THINKING IS ON BY DEFAULT. On Opus 4.6/4.7/4.8, omitting the `thinking`
     parameter meant no thinking. On Opus 5 the same request runs adaptive
     thinking, and `max_tokens` caps thinking PLUS response text together.
     `_invoke` sends no `thinking` field, so every narrative call would have
     started spending its 4096-token budget on reasoning and truncating the
     answer mid-sentence — no error, just a short response and
     `stop_reason: "max_tokens"`.

  2. THE PROMPT-CACHE MINIMUM DROPPED 4096 -> 512 TOKENS. That is what makes
     a `cache_control` breakpoint worth setting at all; below a model's
     minimum the marker is a silent no-op that still pays the write premium.
     The minimum is NOT monotonic across generations, so the per-model map is
     the load-bearing part, not the marker.

The cache marker must not change what the model is asked — it adds a field to
the system block and reorders nothing — so the request-shape tests below pin
that the prompt and message content are untouched.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from hilmar import model_router as mr  # noqa: E402


class _Recorder:
    """Stub Anthropic client that records the kwargs it was called with."""

    def __init__(self):
        self.calls = []
        self.messages = self

    def create(self, **kwargs):
        self.calls.append(kwargs)

        class _Usage:
            input_tokens, output_tokens = 100, 50

        class _Block:
            type, text = "text", "ok"

        class _Resp:
            content, usage = [_Block()], _Usage()

        return _Resp()


@pytest.fixture
def rec():
    return _Recorder()


def _router(rec, tmp_path, **kw):
    return mr.ModelRouter(cost_log_path=tmp_path / "cost.jsonl",
                          client_factory=lambda: rec, **kw)


# ── the migration itself ────────────────────────────────────────────────────

def test_default_model_is_opus_5():
    assert mr.DEFAULT_MODEL == "claude-opus-5"


def test_opus_5_is_priced():
    """An unpriced model silently logs cost_cents=0 and the spend banner goes
    quiet — the cost log's whole job is to notice spend."""
    assert mr.DEFAULT_PRICING_CPM["claude-opus-5"] == {"input": 500, "output": 2500}


def test_parser_extraction_stays_on_haiku():
    """The Haiku default is a deliberate cost choice for a high-volume,
    structurally simple task — the Opus migration must not sweep it up."""
    assert mr._TASK_MODEL_DEFAULTS["parser_extraction"] == "claude-haiku-4-5"


# ── (1) thinking-on-by-default vs max_tokens ────────────────────────────────

def test_max_tokens_is_raised_on_a_thinking_model(rec, tmp_path):
    """THE defect this guards: 4096 was ample when none of it went to
    thinking. On Opus 5 it is shared with reasoning."""
    _router(rec, tmp_path).call(task_type="business_advice", prompt="p",
                                max_tokens=4096)
    assert rec.calls[0]["max_tokens"] == mr.MAX_TOKENS_FLOOR
    assert mr.MAX_TOKENS_FLOOR >= 16000


def test_a_caller_asking_for_more_than_the_floor_keeps_it(rec, tmp_path):
    _router(rec, tmp_path).call(task_type="business_advice", prompt="p",
                                max_tokens=64000)
    assert rec.calls[0]["max_tokens"] == 64000


def test_non_thinking_models_are_left_alone(rec, tmp_path):
    """Haiku does not think by default — raising its budget would just widen
    the ceiling on a cheap, deliberately-small call."""
    _router(rec, tmp_path).call(task_type="parser_extraction", prompt="p",
                                max_tokens=200)
    assert rec.calls[0]["model"] == "claude-haiku-4-5"
    assert rec.calls[0]["max_tokens"] == 200


def test_thinking_is_never_disabled(rec, tmp_path):
    """Disabling thinking is capped at effort<=high on Opus 5 and carries two
    documented failure modes (tool calls emitted as plain text; <thinking>
    tags leaking into output). Raising max_tokens is the safe lever."""
    _router(rec, tmp_path).call(task_type="business_advice", prompt="p")
    assert "thinking" not in rec.calls[0]


def test_no_removed_sampling_parameters_are_sent(rec, tmp_path):
    """temperature / top_p / top_k / budget_tokens all return 400 on Opus
    4.7+. This router never sent them; the test keeps it that way."""
    _router(rec, tmp_path).call(task_type="business_advice", prompt="p")
    sent = rec.calls[0]
    for banned in ("temperature", "top_p", "top_k", "budget_tokens"):
        assert banned not in sent


# ── (2) the cache breakpoint ────────────────────────────────────────────────

_BIG = "x" * (512 * 4 + 10)     # clears the Opus 5 minimum on the 4-chars/token gate
_SMALL = "you are a helpful assistant."


def test_a_large_system_prompt_gets_a_breakpoint(rec, tmp_path):
    _router(rec, tmp_path).call(task_type="business_advice", prompt="p",
                                system=_BIG)
    assert rec.calls[0]["system"] == [
        {"type": "text", "text": _BIG, "cache_control": {"type": "ephemeral"}}
    ]


def test_a_short_system_prompt_is_left_as_a_plain_string(rec, tmp_path):
    """Below the model's minimum a marker is a silent no-op that still pays
    the ~1.25x write premium — worse than not marking at all."""
    _router(rec, tmp_path).call(task_type="business_advice", prompt="p",
                                system=_SMALL)
    assert rec.calls[0]["system"] == _SMALL


def test_haiku_needs_a_far_larger_prefix_before_marking(rec, tmp_path):
    """The minimum is NOT monotonic across generations: Haiku 4.5 needs 4096
    tokens where Opus 5 needs 512. A prompt that caches on Opus 5 does not
    cache on Haiku, and marking it anyway would be misleading to read."""
    _router(rec, tmp_path).call(task_type="parser_extraction", prompt="p",
                                system=_BIG)
    assert rec.calls[0]["model"] == "claude-haiku-4-5"
    assert rec.calls[0]["system"] == _BIG


def test_no_system_prompt_sends_no_system_field(rec, tmp_path):
    _router(rec, tmp_path).call(task_type="business_advice", prompt="p")
    assert "system" not in rec.calls[0]


def test_the_marker_does_not_change_what_the_model_is_asked(rec, tmp_path):
    """Caching is a prefix match — the marker adds a field and reorders
    nothing. If this ever fails, the change stopped being behaviour-neutral."""
    _router(rec, tmp_path).call(task_type="business_advice",
                                prompt="the actual question", system=_BIG)
    sent = rec.calls[0]
    assert sent["messages"] == [{"role": "user", "content": "the actual question"}]
    assert sent["system"][0]["text"] == _BIG


def test_kill_switch_reverts_to_plain_strings(rec, tmp_path, monkeypatch):
    """A live-incident escape hatch is only worth having if it works."""
    monkeypatch.setattr(mr, "CACHE_SYSTEM_PROMPT", False)
    _router(rec, tmp_path).call(task_type="business_advice", prompt="p",
                                system=_BIG)
    assert rec.calls[0]["system"] == _BIG


def test_every_routable_model_has_a_cache_minimum():
    """A model missing from the map is never marked — safe, but silent. This
    fails when a new model is routed to without deciding its minimum."""
    routed = {mr.DEFAULT_MODEL, *mr._TASK_MODEL_DEFAULTS.values(),
              "claude-sonnet-4-6"}  # the cascade target
    assert routed <= set(mr.CACHE_MIN_TOKENS), routed - set(mr.CACHE_MIN_TOKENS)
