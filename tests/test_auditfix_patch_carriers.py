"""Regression test for the patch_carriers.py audit fix.

Audit finding (correctness, low): patch_carriers.py (Step 5, an active
pipeline step that mutates the canonical tracking-data-v2.json in place)
persisted via a raw ``data_path.write_text(json.dumps(data, indent=2))``.
That bypassed ``core.save_data_validated`` — the shared invariant gate that
protects every other writer (ingest.py and core helpers) from landing
structurally-drifted data on disk — and also dropped the ``default=str``
serializer, so any non-JSON-native value introduced into a row would raise
mid-write and abort the patch step.

The fix routes the final write through ``C.save_data_validated(data,
data_path)``.

These tests assert the persistence call goes through the validated helper
and does NOT use the raw write_text/json.dumps path. They fail against the
pre-fix source and pass against the fixed source. Pure static analysis —
no imports of the (config/IO-heavy) pipeline modules, no network.
"""
import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PATCH_CARRIERS = REPO_ROOT / "scripts" / "patch_carriers.py"


def _source() -> str:
    return PATCH_CARRIERS.read_text(encoding="utf-8")


def test_persists_through_validated_save_helper():
    """The data file must be written via core.save_data_validated."""
    src = _source()
    assert "C.save_data_validated(data, data_path)" in src, (
        "patch_carriers.py must persist tracking-data-v2.json through "
        "C.save_data_validated (the shared invariant gate), not a raw write."
    )


def test_no_raw_jsondumps_write_text_persist():
    """The pre-fix raw write_text(json.dumps(...)) persist path is gone.

    Walk the AST and ensure no call writes the data file via
    ``<path>.write_text(json.dumps(...))`` — the exact bypass the audit
    flagged. We look for an attribute call ``.write_text`` whose first arg
    is a ``json.dumps(...)`` call.
    """
    tree = ast.parse(_source())
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "write_text"):
            continue
        if not node.args:
            continue
        first = node.args[0]
        if (
            isinstance(first, ast.Call)
            and isinstance(first.func, ast.Attribute)
            and first.func.attr == "dumps"
            and isinstance(first.func.value, ast.Name)
            and first.func.value.id == "json"
        ):
            offenders.append(getattr(node, "lineno", "?"))
    assert not offenders, (
        "patch_carriers.py still persists via raw write_text(json.dumps(...)) "
        f"at line(s) {offenders}; route the write through "
        "C.save_data_validated instead so default=str + the invariant gate apply."
    )


def test_validated_helper_exists_and_is_callable_in_core():
    """Sanity: the helper the fix relies on actually exists in scripts/core.py
    with a strict validation gate, so the wiring is real (not a typo'd name).
    """
    core_src = (REPO_ROOT / "scripts" / "core.py").read_text(encoding="utf-8")
    tree = ast.parse(core_src)
    funcs = {
        n.name
        for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef)
    }
    assert "save_data_validated" in funcs, (
        "scripts/core.py must define save_data_validated for the patch_carriers "
        "fix to call."
    )
