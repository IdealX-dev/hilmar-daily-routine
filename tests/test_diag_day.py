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


def test_it_pulls_into_the_repo_root_not_a_temp_dir():
    """The first version pulled into a temp dir and died on Graph auth.

    This tenant has no app-only Entra app (OL IT declined), so GRAPH_APP_* is
    empty on the runner and auth falls back to the delegated MSAL cache at
    secrets/token-cache.bin. outlook_send resolves that from module constants,
    so a temp dir is invisible to it — and monkeypatching those constants
    would give the tracer a private copy of the pipeline's auth, the one thing
    it must not have. A temp dir here is a silent regression to a run that
    cannot authenticate.
    """
    assert "state_store.pull(root=root)" in SRC
    assert "mkdtemp" not in SRC, (
        "diag_day pulls into a temp dir again — Graph auth will fail")


def test_every_workflow_that_pulls_state_installs_the_storage_sdk():
    """azure-storage-blob is deliberately absent from requirements.txt, so a
    workflow that runs a state_store-importing script must name it.

    2026-08-07: diag-day.yml's first run installed requirements.txt and died
    on `No module named 'azure'` before printing one useful line. The rule is
    general, so this is checked across all workflows rather than for the one
    that happened to break.
    """
    import re

    scripts = ROOT / "scripts"
    pulls_state = {
        p.name for p in scripts.glob("*.py")
        if re.search(r"^\s*(import state_store|from state_store)",
                     p.read_text(encoding="utf-8"), re.M)
    }
    assert "diag_day.py" in pulls_state, "diag_day no longer pulls state"

    checked = 0
    for wf in sorted((ROOT / ".github" / "workflows").glob("*.yml")):
        text = wf.read_text(encoding="utf-8")
        run = set(re.findall(r"scripts/([A-Za-z0-9_]+\.py)", text))
        if not (run & pulls_state):
            continue
        checked += 1
        assert "azure-storage-blob" in text, (
            f"{wf.name} runs {sorted(run & pulls_state)} but never installs "
            f"azure-storage-blob — the run dies before printing anything")
    assert checked, "no workflow runs a state_store script — check the regex"


def test_it_reads_stage_and_bodies_through_the_pipelines_loaders():
    """The first live run printed `stage_emails: 1273 records (0 with an imid)`.

    diag_day had built its own index keyed on "internetMessageId" — the GRAPH
    field name. build_stage_record writes `imid`. So the staged and body
    columns read NO for every message in existence, which is indistinguishable
    from "nothing was ever staged" and would have sent the next investigation
    at the wrong link. Same defect as a private copy of classify(), one level
    down: a private copy of a field NAME.
    """
    assert "RS.load_existing_stage_keys()" in SRC
    assert "RS.load_existing_body_imids()" in SRC
    assert "QC._load_bodies_index()" in SRC
    # No hand-rolled index over stage/body records. Graph items legitimately
    # carry internetMessageId, so the ban is on the comprehension, not the name.
    assert 'for r in stage' not in SRC and 'for b in bodies' not in SRC, (
        "diag_day indexes stage/body records itself again")


def test_the_stage_writer_and_the_tracers_reader_agree_on_the_imid_field():
    """A BINDING test: build a record with the real writer, read it with the
    real reader, through a redirected STAGE_PATH.

    Not `assert "imid" in source` — that passes just as happily when the two
    sides disagree. This fails if build_stage_record ever renames the field,
    which is precisely the drift that produced `0 with an imid`.
    """
    import json

    import refresh_stage as RS

    item = {
        "id": "AAMkAGRAWID=",
        "internetMessageId": "<binding-test@ol-usa.com>",
        "receivedDateTime": "2026-08-06T18:00:00Z",
        "sentDateTime": "2026-08-06T17:59:00Z",
        "subject": "Ocean rate request — Hilmar to Yokohama",
        "bodyPreview": "please quote",
        "conversationId": "conv-1",
    }
    rec = RS.build_stage_record(item, "lonny_outbound")

    import tempfile
    from pathlib import Path as P
    tmp = P(tempfile.mkdtemp()) / "stage_emails.txt"
    tmp.write_text(json.dumps(rec) + "\n", encoding="utf-8")

    old = RS.STAGE_PATH
    try:
        RS.STAGE_PATH = tmp
        ids, imids = RS.load_existing_stage_keys()
    finally:
        RS.STAGE_PATH = old

    assert item["id"] in ids
    assert item["internetMessageId"] in imids, (
        "build_stage_record and load_existing_stage_keys disagree about the "
        "imid field — diag_day's staged column will read NO for everything")


def test_it_parses_and_the_module_imports_clean():
    """Cheap, and it catches the NameError-after-a-rename class of defect that
    has shipped in this repo before — a diagnostic that crashes on import is
    discovered at the worst possible moment."""
    import importlib
    mod = importlib.import_module("diag_day")
    assert callable(mod.main)
