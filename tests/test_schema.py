"""
Schema-conformance tests — verifies tracking-data-v2.json (represented by
tests/fixtures/golden_day.json) matches schema.json's required keys and
enum constraints.

Ported 2026-05-17 from the dormant `hilmar-tracker` repo. Adapted from
pytest fixtures to plain-Python (the current CI runs tests as scripts,
not via pytest).

Schema lives at repo root (schema.json) — the canonical contract for
tracking-data-v2.json. Adding/removing a field there is the deliberate
way to evolve the data shape.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = ROOT / "schema.json"
GOLDEN_PATH = ROOT / "tests" / "fixtures" / "golden_day.json"


def _load_schema():
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def _load_golden():
    return json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))


def test_schema_file_exists():
    """schema.json must be at repo root — it's the canonical contract."""
    assert SCHEMA_PATH.exists(), f"schema.json not found at {SCHEMA_PATH}"


def test_schema_is_valid_json():
    """schema.json must parse as valid JSON."""
    schema = _load_schema()
    assert isinstance(schema, dict), "schema.json must be a JSON object"
    assert "$schema" in schema, "missing $schema declaration"
    assert "definitions" in schema, "missing definitions section"


def test_golden_day_fixture_exists():
    """tests/fixtures/golden_day.json must exist — the canonical sample
    we validate against."""
    assert GOLDEN_PATH.exists(), f"golden_day.json not found at {GOLDEN_PATH}"


def test_top_level_required_keys_present():
    """Every key listed as schema.required[] must be present in golden."""
    schema = _load_schema()
    golden = _load_golden()
    for key in schema.get("required", []):
        assert key in golden, f"missing top-level key: {key}"


def test_every_request_has_required_fields():
    """Each request in golden_day.json has all fields the schema marks
    required at the request level."""
    schema = _load_schema()
    golden = _load_golden()
    req_fields = schema["definitions"]["request"]["required"]
    for i, r in enumerate(golden.get("requests", [])):
        for field in req_fields:
            assert field in r, f"request[{i}] missing required field: {field}"


def test_status_values_are_from_enum():
    """Storage uses 3-state classifier (WIN/LOSS/PENDING) per scripts/core.py
    VALID_STATUSES. The 4-state display (WIN / Q&L / NQ / PENDING) is
    DERIVED at render time from LOSS + quoted boolean — not stored."""
    valid = {"WIN", "LOSS", "PENDING"}
    golden = _load_golden()
    for i, r in enumerate(golden.get("requests", [])):
        s = r.get("status")
        assert s in valid, f"request[{i}] has unexpected status: {s!r} (allowed: {valid})"


def test_loss_reason_enum_if_present():
    """When loss_reason is set, it must be one of core.LOSS_REASONS
    (uppercase canonical values). Always null on WIN."""
    valid_loss_reasons = {"NO_RESPONSE", "PRICE", "ETD_MISS", "COVERED", "DRAFT_ONLY", "OTHER", None}
    golden = _load_golden()
    for i, r in enumerate(golden.get("requests", [])):
        lr = r.get("loss_reason")
        if lr is None:
            continue
        assert lr in valid_loss_reasons, (
            f"request[{i}] has unexpected loss_reason: {lr!r} (allowed: {valid_loss_reasons - {None}})"
        )


def test_win_rows_have_no_loss_reason():
    """A WIN by definition can't have a loss_reason — that's a category
    error from a status-rollback that didn't clean up."""
    golden = _load_golden()
    for i, r in enumerate(golden.get("requests", [])):
        if r.get("status") == "WIN":
            assert not r.get("loss_reason"), (
                f"request[{i}] is WIN but has loss_reason={r.get('loss_reason')!r}"
            )


def test_teu_fields_are_non_negative_integers():
    """teu_requested and teu_won (when present) must be non-negative ints."""
    golden = _load_golden()
    for i, r in enumerate(golden.get("requests", [])):
        teu_req = r.get("teu_requested")
        assert isinstance(teu_req, int) and teu_req >= 0, (
            f"request[{i}].teu_requested = {teu_req!r} not non-negative int"
        )
        teu_won = r.get("teu_won")
        if teu_won is not None:
            assert isinstance(teu_won, int) and teu_won >= 0, (
                f"request[{i}].teu_won = {teu_won!r} not non-negative int"
            )


if __name__ == "__main__":
    import inspect
    mod = sys.modules[__name__]
    funcs = [(n, f) for n, f in inspect.getmembers(mod, inspect.isfunction)
             if n.startswith("test_")]
    passed = failed = 0
    for name, fn in funcs:
        try:
            fn()
            passed += 1
        except AssertionError as e:
            failed += 1
            print(f"  FAIL {name}: {e}")
        except Exception as e:
            failed += 1
            print(f"  ERROR {name}: {type(e).__name__}: {e}")
    print(f"\n{passed} passed, {failed} failed of {len(funcs)} schema tests")
    sys.exit(0 if failed == 0 else 1)
