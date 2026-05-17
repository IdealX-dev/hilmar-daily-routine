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

except ImportError:
    pytest = None  # type: ignore
