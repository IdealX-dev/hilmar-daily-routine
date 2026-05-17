"""Tests for hilmar.logging_config — JSON / text formatters, env wiring,
idempotent re-configure, redaction.
"""
from __future__ import annotations

import io
import json
import logging
from pathlib import Path

import pytest

from hilmar import logging_config


@pytest.fixture(autouse=True)
def _reset_logging():
    """Strip our handlers between tests so state doesn't leak."""
    yield
    logging_config._strip_managed_handlers(logging.getLogger())
    logging_config._strip_managed_handlers(logging.getLogger("hilmar"))


def _capture_stream() -> io.StringIO:
    return io.StringIO()


# ─────────────────────────────────────────────────────────────────────
# JSON formatter
# ─────────────────────────────────────────────────────────────────────


def test_json_formatter_emits_required_fields():
    buf = _capture_stream()
    logging_config.configure(level="DEBUG", fmt="json", stream=buf)
    log = logging.getLogger("hilmar.test")
    log.info("hello world")

    line = buf.getvalue().strip().splitlines()[-1]
    rec = json.loads(line)
    assert rec["level"] == "INFO"
    assert rec["logger"] == "hilmar.test"
    assert rec["msg"] == "hello world"
    assert "ts" in rec and rec["ts"].endswith("+00:00")


def test_json_formatter_includes_extras():
    buf = _capture_stream()
    logging_config.configure(level="INFO", fmt="json", stream=buf)
    log = logging.getLogger("hilmar.test")
    log.info("step done", extra={"phase": 7, "fixes": 3})

    rec = json.loads(buf.getvalue().strip().splitlines()[-1])
    assert rec["phase"] == 7
    assert rec["fixes"] == 3


def test_json_formatter_renders_unjsonable_via_repr():
    buf = _capture_stream()
    logging_config.configure(fmt="json", stream=buf)
    log = logging.getLogger("hilmar.test")
    # set() isn't JSON-safe.
    log.info("weird", extra={"thing": {1, 2, 3}})

    rec = json.loads(buf.getvalue().strip().splitlines()[-1])
    assert isinstance(rec["thing"], str)
    # repr of a set: starts with "{" and ends with "}".
    assert rec["thing"].startswith("{") and rec["thing"].endswith("}")


def test_json_formatter_attaches_exc_info():
    buf = _capture_stream()
    logging_config.configure(fmt="json", stream=buf)
    log = logging.getLogger("hilmar.test")
    try:
        raise ValueError("boom")
    except ValueError:
        log.exception("crashed")
    rec = json.loads(buf.getvalue().strip().splitlines()[-1])
    assert "ValueError: boom" in rec.get("exc_info", "")


# ─────────────────────────────────────────────────────────────────────
# Text formatter
# ─────────────────────────────────────────────────────────────────────


def test_text_formatter_one_line(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("HILMAR_LOG_LEVEL", raising=False)
    monkeypatch.setenv("HILMAR_LOG_FORMAT", "text")
    buf = _capture_stream()
    logging_config.configure(stream=buf)
    log = logging.getLogger("hilmar.test")
    log.info("hello")
    line = buf.getvalue().strip().splitlines()[-1]
    assert "INFO" in line
    assert "hilmar.test" in line
    assert line.endswith("hello")


# ─────────────────────────────────────────────────────────────────────
# File handler
# ─────────────────────────────────────────────────────────────────────


def test_configure_writes_to_file(tmp_path: Path):
    log_file = tmp_path / "subdir" / "run.log"
    logging_config.configure(fmt="json", file_path=log_file)
    log = logging.getLogger("hilmar.test")
    log.info("on disk", extra={"phase": 1})
    # Close handlers so we can read.
    logging_config._strip_managed_handlers(logging.getLogger())

    assert log_file.exists()
    rec = json.loads(log_file.read_text(encoding="utf-8").strip().splitlines()[-1])
    assert rec["msg"] == "on disk"
    assert rec["phase"] == 1


# ─────────────────────────────────────────────────────────────────────
# Idempotent re-configure
# ─────────────────────────────────────────────────────────────────────


def test_configure_idempotent_does_not_double_log():
    buf = _capture_stream()
    logging_config.configure(fmt="json", stream=buf)
    logging_config.configure(fmt="json", stream=buf)
    logging_config.configure(fmt="json", stream=buf)

    log = logging.getLogger("hilmar.test")
    log.info("once")

    lines = [ln for ln in buf.getvalue().strip().splitlines() if ln.strip()]
    # Exactly one record per logger.info call regardless of re-configure count.
    assert len(lines) == 1


# ─────────────────────────────────────────────────────────────────────
# Env wiring
# ─────────────────────────────────────────────────────────────────────


def test_configure_picks_up_env_level(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("HILMAR_LOG_LEVEL", "DEBUG")
    buf = _capture_stream()
    logging_config.configure(stream=buf)
    log = logging.getLogger("hilmar.test")
    log.debug("verbose")
    assert "verbose" in buf.getvalue()


def test_configure_unknown_level_falls_back_to_info(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("HILMAR_LOG_LEVEL", "MAGIC")
    buf = _capture_stream()
    logging_config.configure(stream=buf)
    log = logging.getLogger("hilmar.test")
    log.debug("should-not-appear")
    log.info("should-appear")
    text = buf.getvalue()
    assert "should-appear" in text
    assert "should-not-appear" not in text


def test_configure_unknown_format_falls_back_to_json(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("HILMAR_LOG_FORMAT", "yaml")
    buf = _capture_stream()
    logging_config.configure(stream=buf)
    log = logging.getLogger("hilmar.test")
    log.info("hi")
    rec = json.loads(buf.getvalue().strip().splitlines()[-1])
    assert rec["msg"] == "hi"


def test_configure_from_env_passes_overrides_through(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("HILMAR_LOG_LEVEL", "DEBUG")
    buf = _capture_stream()
    logging_config.configure_from_env(stream=buf, level="WARNING")
    log = logging.getLogger("hilmar.test")
    log.info("hidden")
    log.warning("shown")
    out = buf.getvalue()
    assert "shown" in out
    assert "hidden" not in out


# ─────────────────────────────────────────────────────────────────────
# get_logger / runtime_summary / redact
# ─────────────────────────────────────────────────────────────────────


def test_get_logger_default_is_hilmar():
    assert logging_config.get_logger().name == "hilmar"


def test_get_logger_named():
    assert logging_config.get_logger("hilmar.thing").name == "hilmar.thing"


def test_runtime_summary_keys(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("HILMAR_DRY_RUN", "false")
    monkeypatch.setenv("HILMAR_DATA_DIR", "/tmp/data")
    monkeypatch.setenv("HILMAR_DAILY_CC", "michael@idealx.us")
    s = logging_config.runtime_summary()
    assert s["dry_run"] == "false"
    assert s["data_dir"] == "/tmp/data"
    assert s["daily_cc"] == "michael@idealx.us"
    # Always present even when unset:
    assert "log_level" in s
    assert "log_format" in s


def test_redact_masks_secret_keys():
    cleaned = logging_config.redact({
        "client_id": "abc",
        "client_secret": "should-be-hidden",
        "api_key": "k",
        "Authorization": "Bearer xyz",
        "harmless": "value",
    })
    assert cleaned["client_id"] == "abc"
    assert cleaned["client_secret"] == "***"
    assert cleaned["api_key"] == "***"
    assert cleaned["Authorization"] == "***"
    assert cleaned["harmless"] == "value"


def test_redact_is_case_insensitive():
    cleaned = logging_config.redact({"AnTHropic_API_KEY": "k"})
    assert cleaned["AnTHropic_API_KEY"] == "***"
