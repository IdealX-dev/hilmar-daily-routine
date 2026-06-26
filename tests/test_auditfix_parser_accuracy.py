"""Regression test for the parser_accuracy docs/gate-consistency audit fix.

The module previously advertised a "98%" parser-accuracy gate throughout its
docstring and comments while the enforced constant ACCURACY_THRESHOLD was
actually 0.95 — a 3-point contradiction across the authoritative surface a
maintainer reads first. This test pins the gate at 0.95 and asserts the
module's own docstring/comments no longer claim a stricter gate figure that
disagrees with the constant.

It fails against the pre-fix module (which contained a bare "THRESHOLD: 98%"
gate statement and a "< 98%" SystemExit example) and passes after the fix.
"""
from __future__ import annotations

import inspect
import re
from pathlib import Path

import hilmar.parser_accuracy as pa


def test_gate_constant_is_095():
    # CLAUDE.md rule #2 fixes the gate at 0.95 — never raise it to silence docs.
    assert pa.ACCURACY_THRESHOLD == 0.95


def test_module_docstring_advertises_95_not_98():
    doc = pa.__doc__ or ""
    # The reconciled docstring states the operative gate as 95% / 0.95.
    assert "≥95%" in doc or "95%" in doc
    # The pre-fix contradictory gate statement must be gone.
    assert "THRESHOLD: 98%" not in doc


def test_no_stale_98_pct_gate_in_source():
    """The source must not assert a 98% gate (e.g. the old "THRESHOLD: 98%"
    line or the "< 98%" SystemExit example) outside the verbatim historical
    Michael quote, which is explicitly retained and contextualized."""
    src = Path(inspect.getfile(pa)).read_text(encoding="utf-8")
    # Strip the protected verbatim historical quote so it doesn't trip the
    # check (it says "98 percent", not "98%", but be defensive).
    protected = (
        '"this parser and your system have to run at minimum\n'
        'of 98 percent accuracy no matter COST."'
    )
    remainder = src.replace(protected, "")
    # The pre-fix stale gate statement and example must be gone. The "98" in
    # the ACCURACY_THRESHOLD comment ("raised back from 0.98 ...") is legit
    # history about why the constant is 0.95, so we target "98%" specifically.
    assert "< 98%" not in remainder
    assert "THRESHOLD: 98%" not in remainder
    assert not re.search(r"\b98\s*%", remainder)
