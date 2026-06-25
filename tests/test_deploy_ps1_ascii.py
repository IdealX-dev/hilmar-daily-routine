"""Deploy PowerShell scripts must be pure ASCII — Windows PowerShell 5.1 safety.

Root-cause guard for the 2026-06-25 Cloud-PC setup failure. `setup_cloudpc.ps1`
carried em-dash characters (U+2014). Windows PowerShell 5.1 reads a `.ps1` with
no BOM as the system ANSI code page (cp1252), so a UTF-8 em-dash's trailing
byte (0x94) decodes to U+201D (a "smart" right double-quote) — which PowerShell
treats as a string delimiter. An em-dash *inside a double-quoted string* thus
terminated the string early and broke parsing ("The string is missing the
terminator" / "Missing closing '}'") the first time the operator re-ran setup
on the box. CI/Linux parse it fine, so it was invisible until then.

ASCII-only `.ps1` is immune regardless of file encoding or BOM. Scope is
deploy/*.ps1 (the scripts that actually run under Windows PowerShell). Use
`-`/`--` instead of em-dashes, and plain `'`/`"` instead of smart quotes.
"""
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PS1_FILES = sorted((ROOT / "deploy").glob("*.ps1"))


def test_deploy_ps1_files_exist():
    # Guard the glob itself: a wrong path would make the parametrized test
    # silently pass with zero cases.
    assert PS1_FILES, "no deploy/*.ps1 found — the test glob is wrong"


@pytest.mark.parametrize("ps1", PS1_FILES, ids=lambda p: p.name)
def test_deploy_ps1_is_ascii(ps1):
    raw = ps1.read_bytes()
    offenders = [(i, b) for i, b in enumerate(raw) if b > 0x7F]
    if offenders:
        first_off = offenders[0][0]
        line = raw[:first_off].count(b"\n") + 1
        bytes_preview = ", ".join(f"0x{b:02X}" for _, b in offenders[:8])
        pytest.fail(
            f"{ps1.name} has {len(offenders)} non-ASCII byte(s); first at line "
            f"{line} (bytes: {bytes_preview}...). Windows PowerShell 5.1 mis-decodes "
            "these as cp1252 and can break parsing (em-dash -> smart-quote string "
            "terminator). Replace with ASCII: '-'/'--' for dashes, straight quotes."
        )
