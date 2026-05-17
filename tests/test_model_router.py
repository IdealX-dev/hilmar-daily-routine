"""Tests for hilmar.model_router — task-keyed model selection + cost telemetry.

NEVER calls the real Anthropic API. We inject a stub client whose
``messages.create`` returns a synthetic response shaped like the SDK.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from hilmar import model_router as mr

UTC = timezone.utc


# ─────────────────────────────────────────────────────────────────────
# Stub client + helpers
# ─────────────────────────────────────────────────────────────────────


class _Block:
    def __init__(self, text: str) -> None:
        self.text = text
        self.type = "text"


class _Usage:
    def __init__(self, input_tokens: int, output_tokens: int) -> None:
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class _Response:
    def __init__(self, text: str, in_tok: int, out_tok: int) -> None:
        self.content = [_Block(text)]
        self.usage = _Usage(in_tok, out_tok)


class _MessagesAPI:
    def __init__(self, behavior) -> None:
        self.behavior = behavior
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return self.behavior(self, kwargs)


class StubClient:
    def __init__(self, behavior) -> None:
        self.messages = _MessagesAPI(behavior)


def _ok_response(text: str = "narrative output", in_tok: int = 100, out_tok: int = 50):
    def behavior(_api, _kwargs):
        return _Response(text, in_tok, out_tok)
    return behavior


def _throttle_then_ok(*, throttle_count: int):
    counter = {"n": 0}

    class _RateLimitError(Exception):
        pass

    def behavior(_api, _kwargs):
        counter["n"] += 1
        if counter["n"] <= throttle_count:
            err = _RateLimitError("429 too many requests")
            err.__class__.__name__ = "RateLimitError"  # type: ignore[misc]
            raise err
        return _Response("ok-after-retry", 100, 50)
    return behavior


def _api_unavailable():
    class _APIConnectionError(Exception):
        pass

    def behavior(_api, _kwargs):
        err = _APIConnectionError("connection refused")
        err.__class__.__name__ = "APIConnectionError"  # type: ignore[misc]
        raise err
    return behavior


@pytest.fixture
def cost_log_path(tmp_path: Path) -> Path:
    return tmp_path / "llm-cost-log.jsonl"


def _fixed_now(*, year: int = 2026, month: int = 4, day: int = 26) -> datetime:
    return datetime(year, month, day, 12, 0, tzinfo=UTC)


# ─────────────────────────────────────────────────────────────────────
# select() — model selection
# ─────────────────────────────────────────────────────────────────────


def test_select_default_is_opus(monkeypatch: pytest.MonkeyPatch, cost_log_path: Path):
    monkeypatch.delenv("HILMAR_INSIGHTS_MODEL", raising=False)
    for t in mr.TASK_TYPES:
        monkeypatch.delenv(f"HILMAR_INSIGHTS_MODEL_{t}", raising=False)
    router = mr.ModelRouter(
        cost_log_path=cost_log_path,
        client_factory=lambda: StubClient(_ok_response()),
    )
    assert router.select("business_advice") == "claude-opus-4-6"


def test_select_global_override(monkeypatch: pytest.MonkeyPatch, cost_log_path: Path):
    monkeypatch.setenv("HILMAR_INSIGHTS_MODEL", "claude-sonnet-4-6")
    router = mr.ModelRouter(
        cost_log_path=cost_log_path,
        client_factory=lambda: StubClient(_ok_response()),
    )
    assert router.select("business_advice") == "claude-sonnet-4-6"


def test_select_per_task_override_wins(monkeypatch: pytest.MonkeyPatch, cost_log_path: Path):
    monkeypatch.setenv("HILMAR_INSIGHTS_MODEL", "claude-sonnet-4-6")
    monkeypatch.setenv("HILMAR_INSIGHTS_MODEL_business_advice", "claude-haiku-4-5")
    router = mr.ModelRouter(
        cost_log_path=cost_log_path,
        client_factory=lambda: StubClient(_ok_response()),
    )
    assert router.select("business_advice") == "claude-haiku-4-5"
    # Other tasks still use the global.
    assert router.select("system_critique") == "claude-sonnet-4-6"


def test_select_unknown_task_type_raises(cost_log_path: Path):
    router = mr.ModelRouter(
        cost_log_path=cost_log_path,
        client_factory=lambda: StubClient(_ok_response()),
    )
    with pytest.raises(ValueError):
        router.call(task_type="unknown_task", prompt="hi")


# ─────────────────────────────────────────────────────────────────────
# call() — happy path + cost log
# ─────────────────────────────────────────────────────────────────────


def test_call_happy_path_writes_cost_log(
    monkeypatch: pytest.MonkeyPatch, cost_log_path: Path,
):
    monkeypatch.delenv("HILMAR_INSIGHTS_MODEL", raising=False)
    router = mr.ModelRouter(
        cost_log_path=cost_log_path,
        client_factory=lambda: StubClient(_ok_response("hello", 1000, 500)),
        now_factory=_fixed_now,
    )
    resp = router.call(task_type="business_advice", prompt="give me advice")
    assert resp.text == "hello"
    assert resp.model == "claude-opus-4-6"
    assert resp.input_tokens == 1000
    assert resp.output_tokens == 500
    assert resp.cost_cents > 0  # opus is not free
    assert resp.skipped_reason is None

    # One JSONL line written.
    assert cost_log_path.exists()
    lines = [ln for ln in cost_log_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["task"] == "business_advice"
    assert rec["model"] == "claude-opus-4-6"
    assert rec["input_tokens"] == 1000
    assert rec["cost_cents"] == resp.cost_cents


def test_call_uses_per_task_override(
    monkeypatch: pytest.MonkeyPatch, cost_log_path: Path,
):
    monkeypatch.setenv("HILMAR_INSIGHTS_MODEL_design_suggestions", "claude-haiku-4-5")
    captured: dict[str, Any] = {}

    def behavior(_api, kwargs):
        captured.update(kwargs)
        return _Response("ok", 10, 10)

    router = mr.ModelRouter(
        cost_log_path=cost_log_path,
        client_factory=lambda: StubClient(behavior),
    )
    resp = router.call(task_type="design_suggestions", prompt="p")
    assert resp.model == "claude-haiku-4-5"
    assert captured["model"] == "claude-haiku-4-5"


# ─────────────────────────────────────────────────────────────────────
# Error cascade
# ─────────────────────────────────────────────────────────────────────


def test_call_429_then_success_retries_once(
    monkeypatch: pytest.MonkeyPatch, cost_log_path: Path,
):
    """First call raises RateLimitError; second succeeds. Should retry
    on the same model — not cascade to Sonnet."""
    monkeypatch.delenv("HILMAR_INSIGHTS_MODEL", raising=False)
    router = mr.ModelRouter(
        cost_log_path=cost_log_path,
        client_factory=lambda: StubClient(_throttle_then_ok(throttle_count=1)),
    )
    resp = router.call(task_type="business_advice", prompt="p")
    assert resp.model == "claude-opus-4-6"
    assert resp.text == "ok-after-retry"


def test_call_double_throttle_cascades_to_sonnet(
    monkeypatch: pytest.MonkeyPatch, cost_log_path: Path,
):
    """First two calls 429 → cascade to claude-sonnet-4-6. The cascade
    response is OK."""
    monkeypatch.delenv("HILMAR_INSIGHTS_MODEL", raising=False)
    router = mr.ModelRouter(
        cost_log_path=cost_log_path,
        client_factory=lambda: StubClient(_throttle_then_ok(throttle_count=2)),
    )
    resp = router.call(task_type="business_advice", prompt="p")
    assert resp.model == "claude-sonnet-4-6"
    assert resp.metadata.get("cascade") is True


def test_call_api_unavailable_returns_skipped_response(
    monkeypatch: pytest.MonkeyPatch, cost_log_path: Path,
):
    monkeypatch.delenv("HILMAR_INSIGHTS_MODEL", raising=False)
    router = mr.ModelRouter(
        cost_log_path=cost_log_path,
        client_factory=lambda: StubClient(_api_unavailable()),
    )
    resp = router.call(task_type="business_advice", prompt="p")
    assert resp.skipped_reason == "api_unavailable"
    assert resp.text == ""
    # Cost log should NOT have been written for a skipped call.
    assert not cost_log_path.exists() or cost_log_path.read_text(encoding="utf-8") == ""


# ─────────────────────────────────────────────────────────────────────
# daily_cost_cents() + alert
# ─────────────────────────────────────────────────────────────────────


def test_daily_cost_cents_sums_today_only(
    monkeypatch: pytest.MonkeyPatch, cost_log_path: Path,
):
    # Pre-seed log with two entries today + one yesterday.
    today = "2026-04-26T08:00:00+00:00"
    yesterday = "2026-04-25T22:00:00+00:00"
    cost_log_path.write_text(
        "\n".join([
            json.dumps({"ts": today,     "cost_cents": 50}),
            json.dumps({"ts": today,     "cost_cents": 70}),
            json.dumps({"ts": yesterday, "cost_cents": 9999}),
        ]) + "\n",
        encoding="utf-8",
    )
    router = mr.ModelRouter(
        cost_log_path=cost_log_path,
        client_factory=lambda: StubClient(_ok_response()),
        now_factory=_fixed_now,
    )
    assert router.daily_cost_cents() == 120


def test_daily_cost_cents_returns_zero_when_no_log(cost_log_path: Path):
    router = mr.ModelRouter(
        cost_log_path=cost_log_path,
        client_factory=lambda: StubClient(_ok_response()),
        now_factory=_fixed_now,
    )
    assert router.daily_cost_cents() == 0


def test_should_alert_cost_uses_env_threshold(
    monkeypatch: pytest.MonkeyPatch, cost_log_path: Path,
):
    monkeypatch.setenv("HILMAR_INSIGHTS_COST_ALERT_CENTS", "100")
    today = "2026-04-26T08:00:00+00:00"
    cost_log_path.write_text(
        json.dumps({"ts": today, "cost_cents": 250}) + "\n",
        encoding="utf-8",
    )
    router = mr.ModelRouter(
        cost_log_path=cost_log_path,
        client_factory=lambda: StubClient(_ok_response()),
        now_factory=_fixed_now,
    )
    assert router.daily_cost_cents() == 250
    assert router.should_alert_cost() is True


def test_should_alert_cost_below_threshold(
    monkeypatch: pytest.MonkeyPatch, cost_log_path: Path,
):
    monkeypatch.setenv("HILMAR_INSIGHTS_COST_ALERT_CENTS", "300")
    today = "2026-04-26T08:00:00+00:00"
    cost_log_path.write_text(
        json.dumps({"ts": today, "cost_cents": 50}) + "\n",
        encoding="utf-8",
    )
    router = mr.ModelRouter(
        cost_log_path=cost_log_path,
        client_factory=lambda: StubClient(_ok_response()),
        now_factory=_fixed_now,
    )
    assert router.should_alert_cost() is False


def test_alert_threshold_invalid_env_falls_back_to_200(
    monkeypatch: pytest.MonkeyPatch, cost_log_path: Path,
):
    monkeypatch.setenv("HILMAR_INSIGHTS_COST_ALERT_CENTS", "not-a-number")
    router = mr.ModelRouter(
        cost_log_path=cost_log_path,
        client_factory=lambda: StubClient(_ok_response()),
    )
    assert router.cost_alert_threshold_cents() == 200


def test_pricing_lookup_uses_default_for_unknown_model(cost_log_path: Path):
    router = mr.ModelRouter(
        cost_log_path=cost_log_path,
        client_factory=lambda: StubClient(_ok_response()),
    )
    cents = router._cost_cents("totally-unknown-model", 1_000_000, 1_000_000)
    # Falls back to DEFAULT_MODEL pricing — opus (1500/7500).
    assert cents == 1500 + 7500
