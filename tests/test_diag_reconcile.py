"""Reconciling against OL's own system of record.

Michael, 2026-08-12, forwarding Linda Echevarria's recap: "ALSO A REPORT
ATTACHED SHOWING ALL OUR WINS" — 35 Hilmar bookings, Jun 1 to Aug 12, pulled
from OL's operational system.

Every diagnostic before this one compared the tracker either to itself or to
the mail we managed to stage, and both share the blind spot being measured:
mail that never arrived cannot be missed by a tool that only reads what
arrived. A list produced OUTSIDE the pipeline breaks that circularity — a
booking we never saw shows up as ABSENT instead of as silence.

Both directions matter. Missing wins understate OL; invented wins overstate
them, and this session has already shipped one report full of quotes that
never existed.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
for p in (str(SCRIPTS), str(ROOT / "src")):
    if p not in sys.path:
        sys.path.insert(0, p)

import diag_reconcile as D  # noqa: E402

SRC = (SCRIPTS / "diag_reconcile.py").read_text(encoding="utf-8")


def test_it_parses_refs_however_ol_writes_them():
    """The recap arrives as spreadsheet text: bare numbers, MDOLX-prefixed,
    zero-padded, comma or newline separated. A parser that only accepts one
    shape sends the operator to reformat 35 rows by hand."""
    got = D.parse_refs("MDOLX261070, 261071\nMDOLX 0260963;260905")
    assert got == ["261070", "261071", "260963", "260905"]


def test_it_reads_secondary_refs_not_just_the_primary():
    """A request accumulates mdolx_refs_all; the primary is only the
    last-linked one. Matching on the primary alone reports a booking absent
    when the row is sitting right there carrying it as a secondary."""
    row = {"mdolx_ref": "260999", "mdolx_refs_all": ["261070", "260963"]}
    assert D.row_refs(row) == {"260999", "261070", "260963"}


def test_ref_matching_ignores_prefix_and_padding():
    row = {"mdolx_ref": "MDOLX261070", "mdolx_refs_all": []}
    assert "261070" in D.row_refs(row)


def test_it_reports_absent_bookings_as_an_intake_finding():
    """The whole point: a booking in OL's system with no row here is mail that
    never reached this mailbox. Saying so is what separates it from a parser
    bug, which is where four hours went today."""
    assert "ABSENT" in SRC
    assert "never reached the mailbox" in SRC


def test_it_checks_the_reverse_direction_too():
    """Invented wins are as damaging as missing ones — this session already
    shipped a report full of quotes that never existed."""
    assert "does not list" in SRC
    assert "invented" in SRC


def test_the_reverse_check_is_scoped_to_the_recaps_range():
    """The recap covers Jun 1 - Aug 12. Calling every April win a phantom
    because it is not in a June-to-August list would be a false alarm, and
    false alarms are how the real ones get ignored."""
    assert "lo <= ref <= hi" in SRC


def test_it_is_read_only():
    """AST, not grep — a mention in the docstring is not a call."""
    called = set()
    for node in ast.walk(ast.parse(SRC)):
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Attribute):
                called.add(f.attr)
            elif isinstance(f, ast.Name):
                called.add(f.id)
    for forbidden in ("push", "save_data", "send", "send_email", "write_text",
                      "save_data_validated", "backup"):
        assert forbidden not in called, f"diag_reconcile calls {forbidden}()"


def test_the_workflow_is_manual_and_takes_the_refs_as_input():
    wf = (ROOT / ".github/workflows/diag-reconcile.yml").read_text(encoding="utf-8")
    assert "workflow_dispatch:" in wf
    assert "schedule:" not in wf, "a diagnostic must not fire on a schedule"
    assert "contents: read" in wf
    assert "azure-storage-blob" in wf
    assert "DIAG_MDOLX" in wf
