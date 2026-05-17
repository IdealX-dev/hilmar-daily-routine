"""Path + config resolution from env vars.

All hilmar modules read paths through this module so deploy/setup-vm.sh and
local dev share one source of truth. Defaults match the systemd unit's
``ReadWritePaths=`` in deploy/systemd/hilmar-tracker.service.
"""
from __future__ import annotations

import os
from pathlib import Path

# ── Defaults match deploy/setup-vm.sh layout on the C3 VM. ────────────
DEFAULT_DATA_DIR = "/opt/hilmar-tracker/data"
DEFAULT_REPORTS_DIR = "/opt/hilmar-tracker/reports"
DEFAULT_BACKUP_DIR = "/opt/hilmar-tracker/data-backups"
DEFAULT_LOG_DIR = "/var/log/hilmar-tracker"

# Backup retention — the original config.json had 14.
DEFAULT_BACKUP_RETENTION = 14

DATA_FILENAME = "tracking-data-v2.json"


def data_dir() -> Path:
    return Path(os.environ.get("HILMAR_DATA_DIR", DEFAULT_DATA_DIR))


def reports_dir() -> Path:
    return Path(os.environ.get("HILMAR_REPORTS_DIR", DEFAULT_REPORTS_DIR))


def backup_dir() -> Path:
    return Path(os.environ.get("HILMAR_BACKUP_DIR", DEFAULT_BACKUP_DIR))


def log_dir() -> Path:
    return Path(os.environ.get("HILMAR_LOG_DIR", DEFAULT_LOG_DIR))


def data_file() -> Path:
    return data_dir() / DATA_FILENAME


def qc_result_file() -> Path:
    return reports_dir() / "qc-result.json"


def dashboard_file() -> Path:
    return reports_dir() / "hilmar-dashboard.html"


def pdf_file() -> Path:
    return reports_dir() / "hilmar-report.pdf"


def email_body_file() -> Path:
    return reports_dir() / "email-body.html"


def carrier_scorecards_dir() -> Path:
    return reports_dir() / "carrier-scorecards"


def escalation_log_file() -> Path:
    return reports_dir() / "escalation-log.json"


def schema_file() -> Path:
    """schema.json. Override with HILMAR_SCHEMA_PATH; otherwise look up the
    repo-root copy from the package install location."""
    env = os.environ.get("HILMAR_SCHEMA_PATH")
    if env:
        return Path(env)
    # src/hilmar/paths.py → repo root is parents[2]
    here = Path(__file__).resolve()
    candidate = here.parents[2] / "schema.json"
    if candidate.exists():
        return candidate
    raise FileNotFoundError(
        "schema.json not found; set HILMAR_SCHEMA_PATH or install the package "
        "with package_data."
    )


def backup_retention() -> int:
    raw = os.environ.get("HILMAR_BACKUP_RETENTION")
    if not raw:
        return DEFAULT_BACKUP_RETENTION
    try:
        n = int(raw)
        return n if n > 0 else DEFAULT_BACKUP_RETENTION
    except ValueError:
        return DEFAULT_BACKUP_RETENTION


def dry_run() -> bool:
    return os.environ.get("HILMAR_DRY_RUN", "true").strip().lower() in (
        "1", "true", "yes", "y", "on",
    )
