"""A row means the same thing in both storage forms, or the report lies.

THE GAP THIS CLOSES. The tracking file stores a loss in one of two forms:

    LEGACY  status="LOSS"  + quoted=True/False   (scripts/ingest.py)
    STRICT  status="Q&L"   /  status="NQ"        (src/hilmar/ingest.py)

core.py has carried `display_status` / `is_quoted_and_lost` / `is_not_quoted`
/ `is_loss` since 2026-06-02 precisely so nothing has to re-derive that, and
its own comment says the point is that no cross-form check "has to inline the
same logic with subtle drift risk". The renderers never got converted. They
kept comparing `status == "LOSS"` — which is *half* the vocabulary — so every
STRICT row fell out of every bucket with no error, no warning, and no gap in
the layout.

Michael saw the result on 2026-08-05 and called it, in two words, "bad data":

  - the Week-over-Week column for the week holding the Q&L and NQ rows drew
    NOTHING, under a label that said "2req". The stack summed to zero because
    neither row landed in a segment, and zero segments render as blank space
    rather than as an error.
  - the Not Quoted header read "0 listed • 0 total • 10 TEU" — a section
    announcing no rows and ten TEU of them in the same breath. The counts came
    from the dropped-on-the-floor row filter; the TEU came from the summary
    block, which was computed upstream and was right all along.

Both numbers were individually defensible and jointly impossible, which is the
signature of two derivations of one fact.

WHY THESE TESTS ARE SHAPED THIS WAY. The behavioural tests below convert one
fixture between the two forms and assert the derived numbers are IDENTICAL.
That holds any renderer to the invariant regardless of how it spells the
check, so a new bucketing loop written next month is covered by a test written
today. The source scan underneath is the cheap backstop: it names the file and
line, which a value mismatch cannot.
"""
from __future__ import annotations

import ast
import json
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import core  # noqa: E402

GOLDEN = ROOT / "tests" / "fixtures" / "golden_day.json"


# ── form converters ──────────────────────────────────────────────────────────

def _to_legacy(r: dict) -> dict:
    """STRICT → LEGACY. Q&L/NQ collapse to LOSS with `quoted` carrying the
    distinction."""
    out = dict(r)
    if out.get("status") == "Q&L":
        out["status"], out["quoted"] = "LOSS", True
    elif out.get("status") == "NQ":
        out["status"], out["quoted"] = "LOSS", False
    return out


def _to_strict(r: dict) -> dict:
    """LEGACY → STRICT. `quoted` is preserved: it stays meaningful on its own
    (it drives the pending split), and dropping it here would make the two
    fixtures differ in a way that has nothing to do with status form."""
    out = dict(r)
    if out.get("status") == "LOSS":
        out["status"] = "Q&L" if out.get("quoted") else "NQ"
    return out


@pytest.fixture(scope="module")
def golden() -> dict:
    return json.loads(GOLDEN.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def cfg() -> dict:
    return json.loads((ROOT / "config.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def both_forms(golden):
    """The same five rows, once in each storage form."""
    rows = golden["requests"]
    legacy = [_to_legacy(r) for r in rows]
    strict = [_to_strict(r) for r in rows]
    return legacy, strict


def test_the_fixture_actually_exercises_both_forms(both_forms):
    """Guard the guard. If the converters ever no-op — a renamed status, a
    fixture rewritten to one vocabulary — every test below would pass by
    comparing a thing to itself and prove nothing."""
    legacy, strict = both_forms
    assert [r["status"] for r in legacy] != [r["status"] for r in strict], (
        "the two 'forms' are identical, so the cross-form assertions are vacuous"
    )
    assert {"LOSS"} <= {r["status"] for r in legacy}
    assert {"Q&L", "NQ"} <= {r["status"] for r in strict}


# ── the accessors themselves ─────────────────────────────────────────────────

def test_accessors_agree_across_forms(both_forms):
    legacy, strict = both_forms
    for a, b in zip(legacy, strict, strict=True):
        assert core.display_status(a) == core.display_status(b)
        assert core.is_win(a) == core.is_win(b)
        assert core.is_pending(a) == core.is_pending(b)
        assert core.is_quoted_and_lost(a) == core.is_quoted_and_lost(b)
        assert core.is_not_quoted(a) == core.is_not_quoted(b)
        assert core.is_loss(a) == core.is_loss(b)


def test_every_row_lands_in_exactly_one_bucket(both_forms):
    """Totality. The bucketers are written as if/elif chains over these four
    predicates, so a row matching none of them is a row that silently
    disappears — which is exactly what produced the empty WoW column."""
    for rows in both_forms:
        for r in rows:
            hits = sum([core.is_win(r), core.is_pending(r),
                        core.is_quoted_and_lost(r), core.is_not_quoted(r)])
            assert hits == 1, f"{r.get('status')!r} matched {hits} buckets, want exactly 1"


# ── dashboard ────────────────────────────────────────────────────────────────

def test_wow_bars_identical_across_forms(both_forms):
    """The Week-over-Week chart. Pure function of the rows, so this compares
    cleanly with no clock in the way."""
    import gen_dashboard as GD
    legacy, strict = both_forms
    assert GD.wow_bars(legacy) == GD.wow_bars(strict)


def test_wow_segments_account_for_every_request(both_forms):
    """The specific defect: a column labelled "2req" that drew no bar.

    The segment heights are a fraction of the column's own request count, so
    if the parts don't sum to the whole, the picture contradicts the caption
    printed directly beneath it."""
    import gen_dashboard as GD
    for rows in both_forms:
        for week, b in GD.wow_bars(rows):
            parts = b["wins"] + b["ql"] + b["nq"] + b["pending"] + b["unclassified"]
            assert parts == b["requests"], (
                f"{week}: segments sum to {parts} but the column is labelled "
                f"{b['requests']}req"
            )
            assert b["unclassified"] == 0, (
                f"{week}: {b['unclassified']} row(s) matched no status bucket"
            )


def test_dashboard_not_quoted_header_cannot_contradict_itself(cfg, golden):
    """"0 listed • 0 total • 10 TEU" must be unreachable: a section that
    reports no rows cannot also report TEU belonging to them."""
    import re

    import gen_dashboard as GD
    for rows in (
        [_to_legacy(r) for r in golden["requests"]],
        [_to_strict(r) for r in golden["requests"]],
    ):
        html = GD.render(cfg, {**golden, "requests": rows})
        m = re.search(r"Not Quoted — Last \d+ Days\s*\((\d+) listed • (\d+) total • (\d+) TEU\)", html)
        assert m, "the Not Quoted header changed shape — update this guard"
        listed, total, teu = (int(g) for g in m.groups())
        assert not (total == 0 and teu > 0), (
            f"header claims {total} not-quoted rows carrying {teu} TEU"
        )
        assert listed <= total


def test_dashboard_headline_counts_identical_across_forms(cfg, golden):
    """Whole-render check. Whatever else differs between two renders, the
    counts a reader acts on must not."""
    import re

    import gen_dashboard as GD

    def _counts(rows):
        html = GD.render(cfg, {**golden, "requests": rows})
        return {
            "wins": re.search(r"Confirmed Wins — (\d+) bookings", html).group(1),
            "nq": re.search(r"(\d+) listed • (\d+) total", html).groups(),
        }

    assert _counts([_to_legacy(r) for r in golden["requests"]]) == \
           _counts([_to_strict(r) for r in golden["requests"]])


# ── weekly summary ───────────────────────────────────────────────────────────

def test_weekly_analyze_week_identical_across_forms(both_forms):
    import gen_weekly_summary as GWS
    legacy, strict = both_forms
    assert GWS.analyze_week(legacy) == GWS.analyze_week(strict)


# ── daily email ──────────────────────────────────────────────────────────────

def test_email_day_summary_identical_across_forms(both_forms):
    import gen_email as GE
    legacy, strict = both_forms
    rd = date(2026, 4, 8)   # the fixture's NQ row
    assert GE._today_summary(legacy, report_date=rd) == \
           GE._today_summary(strict, report_date=rd)


# ── source backstop ──────────────────────────────────────────────────────────

# Renderers, not the classifier. core.py is where the two vocabularies are
# ALLOWED to be named — that is what the accessors are made of.
_RENDERERS = ["gen_dashboard.py", "gen_email.py", "gen_pdf.py",
              "gen_weekly_summary.py", "gen_client_email.py"]

_LOSS_LITERALS = {"LOSS", "Q&L", "NQ"}


def _status_literal_comparisons(path: Path):
    """Find `<anything>["status"] == "LOSS"` and friends via the AST.

    Deliberately not a regex over the text: half of this file's own docstrings
    contain the string `status == "LOSS"`, and a scanner that cannot tell a
    comparison from a sentence about one is a scanner that goes red on prose.
    That mistake has now been made three times in this repo.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        for comparator in node.comparators:
            if not (isinstance(comparator, ast.Constant)
                    and comparator.value in _LOSS_LITERALS):
                continue
            src = ast.unparse(node.left)
            if "status" in src.lower():
                yield node.lineno, ast.unparse(node)


@pytest.mark.parametrize("name", _RENDERERS)
def test_no_renderer_compares_status_to_a_loss_literal(name):
    """Naming one form is choosing one form. Every such site is a place where
    the other form's rows go missing without a sound."""
    hits = list(_status_literal_comparisons(ROOT / "scripts" / name))
    assert not hits, (
        f"{name} compares status against a loss literal at "
        + "; ".join(f"line {ln}: {src}" for ln, src in hits)
        + " — use core.is_quoted_and_lost / is_not_quoted / is_loss, which "
          "read both storage forms"
    )


def test_the_scanner_catches_a_planted_violation(tmp_path):
    """A scanner nobody has seen fail is a scanner nobody knows works."""
    p = tmp_path / "planted.py"
    p.write_text(
        '"""A docstring mentioning status == "LOSS" must NOT trip this."""\n'
        "def f(r):\n"
        '    return r["status"] == "LOSS" and r.get("quoted")\n',
        encoding="utf-8",
    )
    hits = list(_status_literal_comparisons(p))
    assert len(hits) == 1 and hits[0][0] == 3, hits
