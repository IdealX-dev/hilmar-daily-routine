"""The SessionStart hook must install what CI installs.

A web session that cannot run the suite is a session that ships untested work,
and the failure is quiet: an ImportError two hours in, long after the point
where anyone would connect it to a missing package.

CI's install list in .github/workflows/test.yml is the authoritative one — it
is the list that has kept the suite green, and it deliberately does NOT use
`pip install -e .` because pyproject declares weasyprint, which needs GTK/pango
system libs the runner does not have. The hook mirrors it. These tests fail
when the two drift, in either direction, so adding a dependency to CI without
adding it to the hook is caught here rather than by a confused session next
week.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
HOOK = ROOT / ".claude" / "hooks" / "session-start.sh"
CI = ROOT / ".github" / "workflows" / "test.yml"


def _code_only(text: str) -> str:
    """Strip shell/YAML comments and join line continuations.

    Comments must go FIRST, and this test learned that the hard way: its own
    first draft read the hook's prose — comments explaining that the hook does
    NOT run `pip install -e .` and does NOT upgrade pip — and failed on the
    explanation of the correct behaviour. Four times in two days something in
    this repo has confused a sentence about code with the code.
    """
    stripped = "\n".join(re.sub(r"(?<!\\)#.*$", "", ln) for ln in text.splitlines())
    return stripped.replace("\\\n", " ")


def _pip_packages(text: str) -> set[str]:
    """Package names from every `pip install` in a shell/YAML block, ignoring
    flags, comments and pip upgrading itself."""
    out: set[str] = set()
    for m in re.finditer(r"pip install([^\n]*)", _code_only(text)):
        for tok in m.group(1).split():
            if tok.startswith("-"):
                continue
            name = re.split(r"[<>=!\[]", tok)[0].strip()
            if name and name != "pip":
                out.add(name.lower())
    return out


def test_the_hook_exists_and_is_executable():
    assert HOOK.exists(), "the SessionStart hook is gone"
    assert HOOK.stat().st_mode & 0o111, f"{HOOK.name} is not executable"


def test_the_hook_is_registered_in_settings():
    import json
    settings = json.loads((ROOT / ".claude" / "settings.json").read_text(encoding="utf-8"))
    hooks = settings.get("hooks", {}).get("SessionStart", [])
    cmds = [h.get("command", "") for entry in hooks for h in entry.get("hooks", [])]
    assert any("session-start.sh" in c for c in cmds), (
        "the hook file exists but nothing runs it")


def test_the_hook_installs_everything_ci_installs():
    """The direction that actually bites: a dep added to CI, forgotten in the
    hook, and the next web session cannot import it."""
    missing = _pip_packages(CI.read_text(encoding="utf-8")) - _pip_packages(
        HOOK.read_text(encoding="utf-8"))
    assert not missing, (
        f"CI installs {sorted(missing)} but the SessionStart hook does not — a "
        f"web session will fail to import them")


def test_the_hook_does_not_install_the_project_itself():
    """`pip install -e .` pulls weasyprint, which needs GTK/pango. CI documents
    this at length; the hook must not quietly reintroduce it."""
    code = _code_only(HOOK.read_text(encoding="utf-8"))
    assert "install -e ." not in code and "install ." not in code, (
        "the hook installs the project, which drags in weasyprint and breaks "
        "every import on a runner without GTK")
    assert "weasyprint" not in code


def test_the_hook_is_web_only_and_idempotent_by_construction():
    """Two properties worth pinning: it must not install into a local checkout
    behind the operator's back, and it must be safe on resume/clear/compact,
    which fire the same hook again."""
    code = _code_only(HOOK.read_text(encoding="utf-8"))
    assert "CLAUDE_CODE_REMOTE" in code, "the hook runs outside web sessions too"
    assert "pip install" in code and " ci " not in code, (
        "use `pip install` (satisfied requirements are skipped), not a "
        "clean-slate install — the container caches after the hook runs")


def test_the_hook_does_not_upgrade_pip():
    """This runner's pip is Debian-managed with no RECORD file, so
    `pip install --upgrade pip` fails outright — and under `set -e` that took
    the whole hook down before it installed anything. Verified 2026-08-06."""
    code = _code_only(HOOK.read_text(encoding="utf-8"))
    assert not re.search(r"pip install[^\n]*--upgrade[^\n]*\bpip\b", code), (
        "upgrading pip fails on this runner and aborts the hook")


def test_the_hook_sets_utf8_mode():
    """core.load_data and ~70 other sites read text files. On a non-UTF-8
    default locale a utf-8 file decoded as cp1252 SUCCEEDS silently and mangles
    every arrow and multiplication sign — that shipped on 2026-08-05. Every
    workflow in .github/ exports these; a session running the same scripts by
    hand needs them too."""
    code = _code_only(HOOK.read_text(encoding="utf-8"))
    assert "PYTHONUTF8=1" in code
    assert "CLAUDE_ENV_FILE" in code, (
        "the UTF-8 exports are set but not persisted to the session env")


@pytest.mark.parametrize("pkg", ["reportlab", "msal", "requests", "tzdata",
                                 "pdfplumber", "sentry-sdk", "jsonschema",
                                 "python-dateutil", "jinja2", "anthropic",
                                 "pytest", "pytest-cov", "responses", "ruff"])
def test_each_expected_package_is_named(pkg):
    """Per-package so a failure says which one went missing."""
    assert pkg in _pip_packages(HOOK.read_text(encoding="utf-8")), (
        f"the hook no longer installs {pkg}")
