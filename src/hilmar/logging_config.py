"""
hilmar.logging_config — Structured logging for the daily run.

Two output modes:

  * **JSON-lines** (default in production): one JSON object per log line,
    parseable by anything that ships logs to a search engine. systemd's
    ``StandardOutput=append:/var/log/hilmar-tracker/run.log`` captures
    stdout, so this stream IS the persistent record.

  * **Plain text** (developer mode, when ``HILMAR_LOG_FORMAT=text``):
    human-readable colour-free single-line records. Used by the
    `hilmar-run` CLI when running locally for debugging.

Why JSON in prod:
  - Each daily run produces ~50–200 log lines. A grep against
    `qc_phase=10` or `level=ERROR` is the operator's first move when
    something looks off. JSON lines are trivially filterable.
  - The orchestrator's email panic-note (M4-bonus) can attach a tail of
    structured records by reading the JSONL — no parsing logic needed.

Configurable via env vars:
  HILMAR_LOG_LEVEL   — DEBUG / INFO / WARNING / ERROR. Default INFO.
  HILMAR_LOG_FORMAT  — "json" (default) or "text".
  HILMAR_LOG_FILE    — optional explicit path. If unset, logs go to
                       stdout only (systemd captures it).

Idempotent: ``configure()`` may be called multiple times; later calls
replace the prior handler instead of stacking.
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import sys
import time
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import IO, Any

# Sentinel: identifies handlers we own so re-configure can replace them.
_HILMAR_HANDLER_ATTR = "_hilmar_managed"


class JsonFormatter(logging.Formatter):
    """One JSON object per line. Always includes ts / level / logger / msg.

    Extra fields passed via ``logger.info("…", extra={…})`` are merged in
    at the top level. Exception info is rendered into ``exc_info``.
    """

    # Standard LogRecord attrs we don't want to leak into the JSON top
    # level. Anything NOT in this set + populated via ``extra=`` lands
    # in the output.
    _RESERVED = {
        "name", "msg", "args", "levelname", "levelno", "pathname",
        "filename", "module", "exc_info", "exc_text", "stack_info",
        "lineno", "funcName", "created", "msecs", "relativeCreated",
        "thread", "threadName", "processName", "process",
        "asctime", "message", "taskName",
    }

    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat()
        out: dict[str, Any] = {
            "ts": ts,
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        # Extras (caller-supplied via ``extra={}``).
        for key, value in record.__dict__.items():
            if key in self._RESERVED or key.startswith("_"):
                continue
            try:
                json.dumps(value)
            except (TypeError, ValueError):
                value = repr(value)
            out[key] = value
        if record.exc_info:
            out["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(out, ensure_ascii=False)


class TextFormatter(logging.Formatter):
    """Plain-text fallback used in dev. ISO ts + level + logger + message."""

    def format(self, record: logging.LogRecord) -> str:
        ts = time.strftime(
            "%Y-%m-%d %H:%M:%S",
            time.gmtime(record.created),
        )
        out = f"{ts}Z {record.levelname:7} {record.name} {record.getMessage()}"
        if record.exc_info:
            out += "\n" + self.formatException(record.exc_info)
        return out


def _resolve_level() -> int:
    raw = (os.environ.get("HILMAR_LOG_LEVEL") or "INFO").upper().strip()
    return {
        "DEBUG": logging.DEBUG, "INFO": logging.INFO,
        "WARNING": logging.WARNING, "WARN": logging.WARNING,
        "ERROR": logging.ERROR, "CRITICAL": logging.CRITICAL,
    }.get(raw, logging.INFO)


def _resolve_format() -> str:
    raw = (os.environ.get("HILMAR_LOG_FORMAT") or "json").lower().strip()
    return "text" if raw == "text" else "json"


def _resolve_file() -> Path | None:
    raw = os.environ.get("HILMAR_LOG_FILE")
    if not raw:
        return None
    return Path(raw)


def _make_formatter(fmt: str) -> logging.Formatter:
    return TextFormatter() if fmt == "text" else JsonFormatter()


def _strip_managed_handlers(target: logging.Logger) -> None:
    """Remove any handlers we previously installed."""
    for h in list(target.handlers):
        if getattr(h, _HILMAR_HANDLER_ATTR, False):
            target.removeHandler(h)
            with contextlib.suppress(Exception):
                h.close()


def configure(
    *,
    level: int | str | None = None,
    fmt: str | None = None,
    file_path: Path | None = None,
    stream: IO[str] | None = None,
) -> None:
    """Wire up logging for the package. Idempotent.

    Defaults come from env vars. The ``hilmar`` logger is configured (so
    every ``logging.getLogger(__name__)`` inside the package inherits)
    plus the root logger (so libraries we import — msal, requests — are
    captured too).

    :param level: int (e.g. ``logging.INFO``) or string ("INFO"). Falls
        back to ``HILMAR_LOG_LEVEL`` env, then INFO.
    :param fmt: "json" or "text". Falls back to ``HILMAR_LOG_FORMAT`` env,
        then "json".
    :param file_path: optional explicit log file. Falls back to
        ``HILMAR_LOG_FILE`` env. ``None`` = stdout only.
    :param stream: stdout/stderr override (mostly for tests).
    """
    if isinstance(level, str):
        level_int = {
            "DEBUG": logging.DEBUG, "INFO": logging.INFO,
            "WARNING": logging.WARNING, "WARN": logging.WARNING,
            "ERROR": logging.ERROR, "CRITICAL": logging.CRITICAL,
        }.get(level.upper(), logging.INFO)
    elif isinstance(level, int):
        level_int = level
    else:
        level_int = _resolve_level()

    fmt_resolved = (fmt or _resolve_format()).lower()
    if fmt_resolved not in ("json", "text"):
        fmt_resolved = "json"
    file_resolved = file_path or _resolve_file()
    out_stream = stream or sys.stdout
    formatter = _make_formatter(fmt_resolved)

    # Replace prior handlers we own; leave foreign handlers (e.g. pytest
    # capture handlers) alone.
    root = logging.getLogger()
    pkg = logging.getLogger("hilmar")
    _strip_managed_handlers(root)
    _strip_managed_handlers(pkg)

    stream_handler = logging.StreamHandler(out_stream)
    stream_handler.setFormatter(formatter)
    setattr(stream_handler, _HILMAR_HANDLER_ATTR, True)
    root.addHandler(stream_handler)

    if file_resolved is not None:
        file_resolved.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(str(file_resolved), encoding="utf-8")
        file_handler.setFormatter(formatter)
        setattr(file_handler, _HILMAR_HANDLER_ATTR, True)
        root.addHandler(file_handler)

    # Set both root + hilmar so library noise (msal at INFO) flows.
    root.setLevel(level_int)
    pkg.setLevel(level_int)


def runtime_summary() -> dict[str, Any]:
    """Return a small dict the orchestrator can ``logger.info(...)`` at
    start-of-run for traceability. Captures env that affects behavior
    without leaking secrets.
    """
    return {
        "log_level": logging.getLevelName(_resolve_level()),
        "log_format": _resolve_format(),
        "log_file": str(_resolve_file()) if _resolve_file() else None,
        "dry_run": os.environ.get("HILMAR_DRY_RUN", "true"),
        "data_dir": os.environ.get("HILMAR_DATA_DIR"),
        "reports_dir": os.environ.get("HILMAR_REPORTS_DIR"),
        "tenant_id": os.environ.get("HILMAR_TENANT_ID"),
        "client_id": os.environ.get("HILMAR_CLIENT_ID"),
        "sender": os.environ.get("HILMAR_SENDER_EMAIL"),
        "daily_cc": os.environ.get("HILMAR_DAILY_CC"),
        "insights_model": os.environ.get("HILMAR_INSIGHTS_MODEL"),
    }


# Ergonomic alias used by the orchestrator + console scripts.
def get_logger(name: str | None = None) -> logging.Logger:
    return logging.getLogger(name or "hilmar")


# Convenience for ad-hoc scripts not running under the orchestrator.
def configure_from_env(**overrides: Any) -> None:
    """Same as :func:`configure` but pulls every default from env.

    ``overrides`` lets callers set just one knob without restating the
    rest — e.g. ``configure_from_env(level="DEBUG")``.
    """
    configure(**{k: v for k, v in overrides.items() if v is not None})


_FILTER_KEYS_LOWER = (
    "secret", "password", "token", "apikey", "api_key", "key",
    "credential", "creds", "authorization", "bearer",
)


def redact(value: Mapping[str, Any]) -> dict[str, Any]:
    """Return a shallow copy with any obvious secret-looking keys
    replaced by "***". Use when logging context dicts that might
    accidentally include credentials.
    """
    out: dict[str, Any] = {}
    for k, v in value.items():
        kl = k.lower()
        if any(needle in kl for needle in _FILTER_KEYS_LOWER):
            out[k] = "***"
        else:
            out[k] = v
    return out
