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


def test_recent_rows_print_timestamps_not_booleans():
    """Run 1 printed resp=1 on 25 rows while zero rate responses were staged
    in their window — the boolean hid exactly the evidence that says where
    the timestamp came from. RESP<REQ is QC-066's impossible ordering."""
    assert "RESP<REQ" in SRC
    assert "resp={resp_d" in SRC, (
        "the row dump no longer prints the response date — a boolean cannot "
        "distinguish a staged reply from a stale-thread inheritance")


def test_it_investigates_wins_that_jumped_into_the_current_week():
    """Nine April-era wins carried a win-event date in the current week on
    run 1. Either the booking-rank change re-chose an August revision to
    represent an April booking (a shipped regression) or a heal re-stamped
    history — the status_history tail says which, so it must be printed."""
    assert "win-event date is within the last 7 days" in SRC
    assert "status_history" in SRC and "booking_ts=" in SRC


def test_the_jumped_wins_sort_cannot_die_on_tied_dates():
    """Run 2 crashed one line after announcing "9 row(s)": bare sorted() on
    (date, dict) tuples compares the dicts when dates tie. A diagnostic that
    dies on the very rows it exists to explain has negative value — the
    answer was in hand and withheld."""
    import diag_weekly  # noqa: F401 — the behavioural check is below
    rows = [("2026-08-11", {"request_id": "b"}), ("2026-08-11", {"request_id": "a"})]
    got = sorted(rows, key=lambda t: (t[0], str(t[1].get("request_id"))))
    assert [r["request_id"] for _d, r in got] == ["a", "b"]
    assert "key=lambda t:" in SRC, (
        "the jumped-wins sort has no key again — two wins on the same date "
        "will compare dicts and TypeError")


def test_it_reads_the_pipelines_own_audit():
    """QC-066 exists precisely for the impossible-ordering shape. If it fired
    today, that is the pipeline agreeing; if it stayed quiet on 25 such rows,
    the guard itself has a blind spot — both answers matter."""
    assert "qc-result.json" in SRC
    assert "QC-066" in SRC


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
