"""CI must run on every branch push, not only via the pull_request event.

WHAT HAPPENED, 2026-08-15. `test.yml` triggered on `push: branches: [main]`
plus `pull_request: branches: [main]`, so a working branch's only route to CI
was the PR event — and that event does not reliably produce a run. Same repo,
same afternoon:

    PR #209  test check created automatically   success
    PR #210  test check created automatically   success
    PR #211  NO check run at all                total_count 0, "pending"
    PR #212  NO check run at all                total_count 0, "pending"

Both silent ones had to be dispatched by hand. #211 carried the fix for the
insights engine reporting a 100% win rate into the CEO's daily email; merged
on the PR's own signal it would have gone in unverified.

CLAUDE.md says never push red. That is only enforceable if the suite actually
runs, so `push` is now unfiltered and this file holds it there.

WHY REGEX AND NOT PyYAML. The first version of this file opened with
`pytest.importorskip("yaml")`. PyYAML is NOT in test.yml's install list — so
in CI every test here would have SKIPPED, reporting green while protecting
nothing. That is the same failure that let the 100% win rate live for months
(a test whose fixtures never matched production). The repo's other
workflow-reading tests use `re` for this reason; so does this one.

THE SECOND HALF is branch protection on main requiring the `test` check. That
lives in repo settings, not in this file, and cannot be asserted here. Without
it an unfiltered push trigger still RUNS the suite — it just does not BLOCK
the merge.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEST_WF = ROOT / ".github" / "workflows" / "test.yml"
SRC = TEST_WF.read_text(encoding="utf-8")


def _trigger_block() -> str:
    """The `on:` mapping, up to the next top-level key."""
    m = re.search(r"^on:\s*$(.*?)^\S", SRC, re.M | re.S)
    assert m, "test.yml has no top-level `on:` block"
    return m.group(1)


def _push_branches() -> str:
    """The `branches:` line belonging to `push:` inside `on:`."""
    m = re.search(r"^  push:\s*$\s*^    branches:\s*(.+)$",
                  _trigger_block(), re.M)
    assert m, "test.yml `on.push` has no `branches:` line"
    return m.group(1).strip()


def test_push_trigger_is_not_restricted_to_main():
    """The regression itself: `push: branches: [main]` left every working
    branch depending on the PR event, which twice produced no run."""
    branches = _push_branches()
    assert branches != "[main]", (
        "test.yml pushes only run CI on main again. A working branch then "
        "gets CI only if the pull_request event fires, and on 2026-08-15 it "
        "silently did not for PRs #211 and #212."
    )
    assert "*" in branches, (
        f"on.push.branches={branches} — expected a wildcard so every branch "
        "runs the suite on its own trigger."
    )


def test_pull_request_trigger_is_kept():
    """The PR event is unreliable, not useless: it is what names the `test`
    check that branch protection requires."""
    assert re.search(r"^  pull_request:\s*$\s*^    branches:\s*\[main\]\s*$",
                     _trigger_block(), re.M), (
        "test.yml no longer runs on pull_request against main — branch "
        "protection has no `test` check to require."
    )


def test_manual_dispatch_stays_available():
    """The escape hatch that rescued #211 and #212."""
    assert re.search(r"^  workflow_dispatch:", _trigger_block(), re.M)


def test_push_and_pull_request_do_not_share_a_concurrency_group():
    """A shared group lets a push cancel the run a required status check is
    waiting on — and a cancelled check blocks the merge it exists to
    protect. Keying on event_name keeps them apart."""
    m = re.search(r"^concurrency:\s*$\s*^  group:\s*(.+)$", SRC, re.M)
    assert m, "test.yml has no concurrency group"
    group = m.group(1)
    assert "github.event_name" in group, (
        f"concurrency.group={group} — without event_name the push run and "
        "the pull_request run collide and one cancels the other."
    )
    assert "github.ref" in group, (
        f"concurrency.group={group} — without ref, unrelated branches "
        "cancel each other's runs."
    )


def test_this_file_needs_no_dependency_ci_lacks():
    """The trap this file already fell into once. Every import here must be
    satisfied by test.yml's install list, or these tests skip in CI and
    protect nothing while reporting green."""
    imports = set(re.findall(r"^(?:from|import)\s+(\w+)",
                             Path(__file__).read_text(encoding="utf-8"), re.M))
    stdlib_only = {"re", "pathlib", "__future__"}
    assert imports <= stdlib_only, (
        f"non-stdlib imports {sorted(imports - stdlib_only)} — confirm they "
        "are in test.yml's pip install list before adding them, or this file "
        "silently skips in CI."
    )
