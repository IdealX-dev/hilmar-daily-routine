"""Tests for hilmar.paths — env-driven path resolution."""
from __future__ import annotations

from pathlib import Path

import pytest

from hilmar import paths


def test_data_dir_uses_env_when_set(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("HILMAR_DATA_DIR", "/tmp/hilmar-test")
    assert paths.data_dir() == Path("/tmp/hilmar-test")


def test_data_dir_default(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("HILMAR_DATA_DIR", raising=False)
    assert paths.data_dir() == Path(paths.DEFAULT_DATA_DIR)


def test_reports_dir_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("HILMAR_REPORTS_DIR", "/tmp/r")
    assert paths.reports_dir() == Path("/tmp/r")


def test_backup_dir_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("HILMAR_BACKUP_DIR", "/tmp/b")
    assert paths.backup_dir() == Path("/tmp/b")


def test_log_dir_default(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("HILMAR_LOG_DIR", raising=False)
    assert paths.log_dir() == Path(paths.DEFAULT_LOG_DIR)


def test_data_file_composition(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("HILMAR_DATA_DIR", "/x")
    assert paths.data_file() == Path("/x") / "tracking-data-v2.json"


def test_named_report_files(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("HILMAR_REPORTS_DIR", "/r")
    assert paths.qc_result_file() == Path("/r/qc-result.json")
    assert paths.dashboard_file() == Path("/r/hilmar-dashboard.html")
    assert paths.pdf_file() == Path("/r/hilmar-report.pdf")
    assert paths.email_body_file() == Path("/r/email-body.html")
    assert paths.carrier_scorecards_dir() == Path("/r/carrier-scorecards")
    assert paths.escalation_log_file() == Path("/r/escalation-log.json")


def test_schema_file_prefers_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    schema = tmp_path / "schema.json"
    schema.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("HILMAR_SCHEMA_PATH", str(schema))
    assert paths.schema_file() == schema


def test_schema_file_repo_root_fallback(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("HILMAR_SCHEMA_PATH", raising=False)
    # Repo root copy exists as part of the working tree.
    p = paths.schema_file()
    assert p.exists()
    assert p.name == "schema.json"


def test_schema_file_raises_when_neither_present(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
):
    # Make HILMAR_SCHEMA_PATH point at a non-existent file (function returns
    # the path even if missing — only the no-env fallback path raises). To
    # exercise the raise branch, we need to clear the env AND temporarily
    # convince paths.schema_file the repo-root copy isn't there. Easiest:
    # monkeypatch __file__ to a location where parents[2] != repo root.
    monkeypatch.delenv("HILMAR_SCHEMA_PATH", raising=False)
    fake_file = tmp_path / "deep" / "nest" / "paths.py"
    fake_file.parent.mkdir(parents=True)
    fake_file.write_text("# fake", encoding="utf-8")
    monkeypatch.setattr(paths, "__file__", str(fake_file))
    with pytest.raises(FileNotFoundError):
        paths.schema_file()


def test_backup_retention_default(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("HILMAR_BACKUP_RETENTION", raising=False)
    assert paths.backup_retention() == paths.DEFAULT_BACKUP_RETENTION


def test_backup_retention_env_int(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("HILMAR_BACKUP_RETENTION", "30")
    assert paths.backup_retention() == 30


def test_backup_retention_invalid_value_falls_back(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("HILMAR_BACKUP_RETENTION", "not-a-number")
    assert paths.backup_retention() == paths.DEFAULT_BACKUP_RETENTION


def test_backup_retention_zero_or_negative_falls_back(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("HILMAR_BACKUP_RETENTION", "0")
    assert paths.backup_retention() == paths.DEFAULT_BACKUP_RETENTION
    monkeypatch.setenv("HILMAR_BACKUP_RETENTION", "-3")
    assert paths.backup_retention() == paths.DEFAULT_BACKUP_RETENTION


def test_dry_run_true_default(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("HILMAR_DRY_RUN", raising=False)
    assert paths.dry_run() is True


@pytest.mark.parametrize("value", ["true", "TRUE", "1", "yes", "on", "Y"])
def test_dry_run_truthy_values(monkeypatch: pytest.MonkeyPatch, value: str):
    monkeypatch.setenv("HILMAR_DRY_RUN", value)
    assert paths.dry_run() is True


@pytest.mark.parametrize("value", ["false", "FALSE", "0", "no", "off"])
def test_dry_run_falsy_values(monkeypatch: pytest.MonkeyPatch, value: str):
    monkeypatch.setenv("HILMAR_DRY_RUN", value)
    assert paths.dry_run() is False
