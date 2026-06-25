"""Shared pytest fixtures + path constants for the hilmar test suite.

Ported 2026-05-17 from the consolidated hilmar-tracker. The tests use
`from hilmar import X` (Python package layout under src/hilmar/), so
we add the src/ directory to sys.path here for both pytest runs AND
plain-Python script runs (the CI uses `python tests/test_*.py`).
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"
GOLDEN_DAY = FIXTURES / "golden_day.json"
SCHEMA_PATH = REPO_ROOT / "schema.json"
SRC_DIR = REPO_ROOT / "src"

# Make `from hilmar import X` work without pip install -e .
# Ensures both pytest runs and plain-Python script runs find the package.
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

# Disable the parser_fallback LLM layer for the entire test session.
# Tests that specifically exercise the fallback build their own
# ParserFallbackContext with a stub router (see test_parser_fallback.py)
# and don't depend on this env flag. The guard here just prevents
# accidental real Anthropic API calls from any ingest-path test that
# doesn't otherwise mock the network.
os.environ.setdefault("HILMAR_PARSER_FALLBACK_DISABLE", "1")

# Never let QC-054's dependency self-heal shell out to `pip install` during a
# test run (the sandbox legitimately lacks some optional deps like sentry_sdk).
# Tests that exercise the self-heal path set/unset this explicitly.
os.environ.setdefault("HILMAR_QC_NO_PIP", "1")

# pytest is optional — fall through cleanly if missing (CI uses plain python).
try:
    import pytest

    @pytest.fixture(scope="session")
    def golden_day() -> dict:
        """The canonical day-of-data fixture used across schema + pipeline tests."""
        return json.loads(GOLDEN_DAY.read_text(encoding="utf-8"))

    @pytest.fixture(scope="session")
    def schema() -> dict:
        return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))

    @pytest.fixture(autouse=True)
    def _hermetic_blob_host(monkeypatch):
        """Force qc_selfheal._BLOB_HOST = False for every test.

        _BLOB_HOST is read from AZURE_STORAGE_CONNECTION_STRING at import.
        That var is UNSET in CI (so the suite was green) but SET inside the
        production-fire job — where the same suite also runs as the QC-052
        in-pipeline audit. There it flips _BLOB_HOST True, turning
        QC-011/012/026/028's "file not present" WARN into an "ephemeral
        runner" skip, and test_qc_011's absent-file test went red in
        production while CI stayed green (2026-06-15 audit red flag).

        Tests must be hermetic — independent of the deploy env they run in.
        This pins the dev/CI default; the dedicated blob-host tests set it
        True explicitly. Best-effort no-op if qc_selfheal isn't importable
        in a given minimal test context.
        """
        import contextlib
        scripts_dir = REPO_ROOT / "scripts"
        if str(scripts_dir) not in sys.path:
            sys.path.insert(0, str(scripts_dir))
        with contextlib.suppress(Exception):
            import qc_selfheal  # noqa: F401  ensure the standard module is loaded
        # Patch EVERY already-loaded module that carries a _BLOB_HOST flag —
        # not just `qc_selfheal`. test_qc_011 loads it under a private name
        # (`qc_sh_under_test`) via a bespoke loader, so a single-name patch
        # would miss it. Iterating sys.modules makes the fixture import-style
        # agnostic.
        for mod in list(sys.modules.values()):
            if mod is not None and getattr(mod, "_BLOB_HOST", None) is not None:
                monkeypatch.setattr(mod, "_BLOB_HOST", False, raising=False)

except ImportError:
    pytest = None  # type: ignore
