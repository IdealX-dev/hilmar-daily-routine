"""The diagnostic for "your data is consistently wrong" must not itself be a
second opinion.

Michael, 2026-08-10, on the dashboard's weekly table. Every prior diagnostic
defect this session came from the tool modelling the pipeline with LESS
fidelity than the pipeline has — no bodies attached, wrong filter order, a
re-typed denominator. This one's contract is therefore: reuse the renderer's
own bucketer, reuse core's own predicates, and never write anything.
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

import diag_weekly as D  # noqa: E402

SRC = (SCRIPTS / "diag_weekly.py").read_text(encoding="utf-8")


def test_it_renders_through_wow_bars_not_a_private_copy():
    """A diagnostic with its own idea of the rollup answers questions about a
    table nobody renders. It must call gen_dashboard.wow_bars."""
    assert "GD.wow_bars(requests)" in SRC, (
        "the rollup is recomputed privately — it can drift from the dashboard "
        "and then the two disagree about what Michael is looking at")


def test_week_keys_match_the_renderers_construction():
    """Same ISO year-Wnn shape as gen_dashboard.wow_bars line ~131. A bucketer
    that disagrees on week boundaries mis-shelves every edge row."""
    assert D._week_key("2026-08-03") == "2026-W32"
    assert D._week_key("2026-06-16") == "2026-W25"
    assert D._week_key(None) is None
    assert D._week_key("garbage") is None


def test_it_shows_wins_by_both_request_week_and_win_week():
    """The renderer credits a win to the week Lonny ASKED; Michael counts the
    week OL CONFIRMED. Where those differ the table reads wrong while being
    internally consistent — the diagnostic must show both or the semantics gap
    stays invisible."""
    assert "by_req_week" in SRC and "by_win_week" in SRC
    assert "win_event_date" in SRC, (
        "the confirmed-week bucketing does not use core.win_event_date — "
        "whatever it uses instead is a fourth opinion about when a win happened")


def test_the_premature_loss_flag_uses_cores_own_window():
    """QC-067's shape: LOSS/NO_RESPONSE while the ask is still inside the
    pending window. The window test must be core.pending_ol_overdue, not a
    re-typed day count."""
    assert "pending_ol_overdue" in SRC
    assert "PREMATURE-LOSS" in SRC


def test_it_flags_the_known_stale_row_classes():
    """The stored rows were built by pre-fix code. The two classes fixed on
    2026-08-10 must be countable, or 'wrong' cannot be split into 'stale until
    the next fire' vs 'still broken'."""
    assert "teu_won" in SRC and "booking-rank class" in SRC
    assert "quoted but no response_timestamp" in SRC


def test_it_calls_out_zero_quote_intake_days():
    """The lead hypothesis is that OL quote replies stopped being staged. A
    day table that does not flag zero-mbd_rate_response days makes the reader
    do the one comparison the tool exists to do."""
    assert "ZERO quote intake" in SRC


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
                      "save_data_validated"):
        assert forbidden not in called, f"diag_weekly calls {forbidden}()"


def test_the_workflow_is_manual_and_installs_the_storage_sdk():
    wf = (ROOT / ".github/workflows/diag-weekly.yml").read_text(encoding="utf-8")
    assert "workflow_dispatch:" in wf
    assert "schedule:" not in wf, "a diagnostic must not fire on a schedule"
    assert "contents: read" in wf
    assert "azure-storage-blob" in wf
    assert "scripts/diag_weekly.py" in wf
