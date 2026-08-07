"""The day-tracer must stay read-only, and must stay honest about the links.

2026-08-07. Michael, asked whether Lonny genuinely sent nothing on Aug 6:
"he did." Every prior investigation this week inspected ONE link in the
intake chain and inferred the rest, and inferring was wrong twice. diag_day
exists so the chain is READ.

Two things are worth pinning and one thing is not:

  PINNED — it cannot write. A diagnostic that can mutate production state is
  a diagnostic nobody dares run on production, which is where the bug is.

  PINNED — it queries with the SAME predicates the pipeline does. A tracer
  with its own copy of the KQL or its own classify() would clear a day the
  pipeline still drops, which is worse than no tracer.

  NOT PINNED — the exact wording of the output. That is prose for a human and
  it should change freely.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

SRC_PATH = ROOT / "scripts" / "diag_day.py"
SRC = SRC_PATH.read_text(encoding="utf-8")
TREE = ast.parse(SRC)


def _called_attrs() -> set[str]:
    """Every `x.y(...)` called anywhere in the module, as "x.y".

    AST, not a grep: "state_store.push" appears in this file's own docstring
    prose and in diag_day's, and an identifier in prose is indistinguishable
    from an identifier in code to a regex. That confusion has produced a false
    guard in this repo four times.
    """
    out = set()
    for node in ast.walk(TREE):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            val = node.func.value
            if isinstance(val, ast.Name):
                out.add(f"{val.id}.{node.func.attr}")
    return out


def test_it_never_writes_state():
    """pull, and nothing else. push/backup/restore all mutate the container."""
    called = _called_attrs()
    for forbidden in ("state_store.push", "state_store.backup",
                      "state_store.restore"):
        assert forbidden not in called, f"diag_day calls {forbidden}"
    assert "state_store.pull" in called, "diag_day no longer pulls state"


def test_it_never_sends_and_never_fetches_a_body():
    """Sending would email nine people from a diagnostic. Fetching bodies
    would write into the very stage file being inspected."""
    imported = set()
    for node in ast.walk(TREE):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    for forbidden in ("outlook_send", "fetch_bodies", "ingest", "merge_ingest"):
        assert forbidden not in imported, f"diag_day imports {forbidden}"

    called = _called_attrs()
    assert "RS.get_message_body" not in called
    assert "RS.append_stage_record" not in called
    assert "RS.fetch_pdf_attachments" not in called


def test_it_opens_no_file_for_writing():
    """The only filesystem it may touch is the temp dir the pull lands in,
    and it may only read from there."""
    for node in ast.walk(TREE):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            assert node.func.attr not in ("write_text", "write_bytes"), (
                "diag_day writes a file")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                and node.func.id == "open":
            mode = node.args[1].value if len(node.args) > 1 else "r"
            assert "w" not in str(mode) and "a" not in str(mode), (
                "diag_day opens a file for writing")


def test_the_mailboxes_come_from_refresh_stage():
    """Not a second copy of the addresses. If Lonny's address changes, the
    tracer must follow the pipeline rather than quietly trace the old one."""
    import refresh_stage as RS
    assert "RS.LONNY_EMAIL" in SRC and "RS.MBD_BOOKING_EMAIL" in SRC
    assert RS.LONNY_EMAIL not in SRC, (
        "diag_day hardcodes Lonny's address instead of importing it")
    assert RS.MBD_BOOKING_EMAIL not in SRC, (
        "diag_day hardcodes the booking mailbox instead of importing it")


def test_it_classifies_with_the_pipeline_and_not_its_own_rule():
    """The whole point is to see what the PIPELINE does with a message."""
    assert "RS.classify(" in SRC, "diag_day no longer uses refresh_stage.classify"
    for node in ast.walk(TREE):
        if isinstance(node, ast.FunctionDef):
            assert node.name != "classify", (
                "diag_day defines its own classify() — it would diverge")


def test_day_bucketing_uses_the_canonical_et_date():
    """core.et_date_of is THE clock every day bucket runs on. A tracer using
    a UTC slice would put a 5:30 PM PT Friday RFQ on the wrong day and then
    report the day as empty — the exact class of bug it is here to find."""
    assert "core.et_date_of" in SRC
    assert "[:10]" not in SRC, "diag_day slices a raw timestamp for a date"


def test_the_undated_reason_comes_from_qc_selfheal():
    """One verdict, one implementation. A second opinion here is a second bug
    waiting — and QC-077 is what the operator sees in the audit."""
    import qc_selfheal as QC
    assert "QC._undated_reason(" in SRC
    assert callable(QC._undated_reason)


def test_undated_reasons_stay_exhaustive():
    """_undated_reason promises exactly one label per row. If a branch is ever
    added without a label, the breakdown silently stops summing to the total."""
    import qc_selfheal as QC
    row_no_imids = {}
    row_no_body = {"source_imids": ["<a>"]}
    row_no_time = {"source_imids": ["<a>"]}
    idx = {"<a>": {"internetMessageId": "<a>"}}
    assert QC._undated_reason(row_no_imids, {}) == "no_imids"
    assert QC._undated_reason(row_no_body, {}) == "no_body"
    assert QC._undated_reason(row_no_time, idx) == "no_send_time"
    assert QC._undated_reason({"source_imids": ["<a>"]},
                              {"<a>": {"sent_ts": "2026-08-06T18:00:00Z"}}) == "unexplained"


def test_the_workflow_is_manual_only_and_read_only():
    """A diagnostic on a schedule is a diagnostic that burns Graph quota
    forever and that nobody reads."""
    wf = (ROOT / ".github" / "workflows" / "diag-day.yml").read_text(encoding="utf-8")
    assert "workflow_dispatch:" in wf
    assert "schedule:" not in wf, "diag-day is on a schedule"
    assert "permissions:\n  contents: read" in wf, (
        "diag-day asks for more than read permission")


def test_the_workflow_passes_the_credentials_the_script_needs():
    """Graph AND the blob. Missing either turns the run into a stack trace
    that looks like the bug being hunted."""
    wf = (ROOT / ".github" / "workflows" / "diag-day.yml").read_text(encoding="utf-8")
    for secret in ("GRAPH_APP_TENANT_ID", "GRAPH_APP_CLIENT_ID",
                   "GRAPH_APP_CLIENT_SECRET", "AZURE_STORAGE_CONNECTION_STRING"):
        assert secret in wf, f"diag-day.yml does not pass {secret}"


def test_it_parses_and_the_module_imports_clean():
    """Cheap, and it catches the NameError-after-a-rename class of defect that
    has shipped in this repo before — a diagnostic that crashes on import is
    discovered at the worst possible moment."""
    import importlib
    mod = importlib.import_module("diag_day")
    assert callable(mod.main)
