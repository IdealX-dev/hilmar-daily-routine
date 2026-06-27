"""Environment-integrity checks — QC-060/061/062 + QC-054 self-heal.

These guard the BOX the pipeline runs on (interpreter version, installed deps,
no stale shadow dirs) — the gap behind the 2026-06 silent week (box drifted to
Python 3.14 with jinja2/sentry-sdk missing and stale tests/+src/ shadows, all
invisible). Tests drive the pure helpers directly and the real phase_6_rules
log paths, with the interpreter pin + REPO_ROOT monkeypatched so they don't
depend on the sandbox's own Python or layout.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import qc_selfheal as q  # noqa: E402


def _base_data():
    return {"version": "2", "requests": [],
            "summary": {"wins": 0, "quoted_lost": 0, "not_quoted": 0,
                        "pending_hilmar": 0, "win_rate": 0.0, "quote_rate": 0.0,
                        "teu_requested": 0, "teu_won": 0, "teu_quoted_lost": 0,
                        "teu_not_quoted": 0, "teu_pending": 0, "total_entries": 0}}


def _run(monkeypatch):
    monkeypatch.setenv("HILMAR_QC_NO_PIP", "1")
    log = q.Log()
    q.phase_6_rules(log, _base_data())
    return log


# ── QC-060: dependency-list consistency ──────────────────────────────────
def test_dep_consistency_passes_on_the_real_repo():
    ok, problems = q.check_dep_consistency()
    assert ok, problems


def test_dep_file_prefers_the_checkout_copy(monkeypatch, tmp_path):
    """On the Cloud PC, qc_selfheal runs from the deployed mirror where
    REPO_ROOT is the OneDrive parent and the dep lists are NOT deployed — only
    the git checkout subdir has them. _dep_file must read the checkout copy so
    QC-060/061 don't false-alarm on a stale/absent deployed copy (the box run
    flagged 4 pinned deps as "not pinned" + "no .python-version"). In CI the
    checkout subdir doesn't exist, so it falls back to REPO_ROOT."""
    monkeypatch.setattr(q, "REPO_ROOT", tmp_path)
    # No checkout subdir → falls back to REPO_ROOT/<name>.
    assert q._dep_file("requirements.txt") == tmp_path / "requirements.txt"
    # With a checkout subdir present → prefers it.
    checkout = tmp_path / "hilmar-daily-routine"
    checkout.mkdir()
    (checkout / "requirements.txt").write_text("requests>=2\n", encoding="utf-8")
    assert q._dep_file("requirements.txt") == checkout / "requirements.txt"


def test_dep_consistency_reads_checkout_when_root_is_bare(monkeypatch, tmp_path):
    """Simulate the box mirror: REPO_ROOT (the OneDrive parent) has NO dep
    lists, but the checkout subdir carries the real, complete ones. QC-060 must
    pass by reading the checkout — not false-flag every dep as unpinned."""
    checkout = tmp_path / "hilmar-daily-routine"
    checkout.mkdir()
    # Full, consistent dep set in the checkout only.
    reqs = "".join(f"{q._module_package(m)}>=1\n" for m in q.RUNTIME_IMPORT_REQUIRED)
    (checkout / "requirements.txt").write_text(reqs, encoding="utf-8")
    monkeypatch.setattr(q, "REPO_ROOT", tmp_path)
    ok, problems = q.check_dep_consistency()
    assert ok, problems


def test_dep_consistency_flags_a_missing_qc054_pin(monkeypatch, tmp_path):
    (tmp_path / "requirements.txt").write_text("requests>=2\n", encoding="utf-8")
    (tmp_path / "requirements-tracker.txt").write_text("requests>=2\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text(
        '[project]\ndependencies = ["requests>=2"]\n', encoding="utf-8")
    monkeypatch.setattr(q, "REPO_ROOT", tmp_path)
    ok, problems = q.check_dep_consistency()
    assert not ok
    assert any("jinja2" in p or "sentry" in p for p in problems), problems


def test_dep_consistency_flags_pyproject_tracker_drift(monkeypatch, tmp_path):
    # requirements.txt covers QC-054, but pyproject and tracker disagree.
    full = "\n".join(q._module_package(m) for m in q.RUNTIME_IMPORT_REQUIRED)
    (tmp_path / "requirements.txt").write_text(full + "\n", encoding="utf-8")
    (tmp_path / "requirements-tracker.txt").write_text("requests\njinja2\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text(
        '[project]\ndependencies = ["requests", "msal"]\n', encoding="utf-8")
    monkeypatch.setattr(q, "REPO_ROOT", tmp_path)
    ok, problems = q.check_dep_consistency()
    assert not ok
    assert any("requirements-tracker" in p for p in problems), problems


# ── QC-061: interpreter parity ───────────────────────────────────────────
def test_interpreter_parity_matches(monkeypatch):
    running = f"{sys.version_info[0]}.{sys.version_info[1]}"
    monkeypatch.setattr(q, "_read_pinned_python", lambda: running)
    ok, run, pin = q.check_interpreter_parity()
    assert ok and run == running and pin == running


def test_interpreter_parity_mismatch(monkeypatch):
    monkeypatch.setattr(q, "_read_pinned_python", lambda: "3.99")
    ok, run, pin = q.check_interpreter_parity()
    assert not ok and pin == "3.99"


def test_interpreter_parity_no_pin_is_ok(monkeypatch):
    monkeypatch.setattr(q, "_read_pinned_python", lambda: None)
    ok, run, pin = q.check_interpreter_parity()
    assert ok and pin is None


def test_qc061_errors_on_interpreter_mismatch(monkeypatch):
    monkeypatch.setattr(q, "_read_pinned_python", lambda: "3.99")
    log = _run(monkeypatch)
    assert any("QC-061" in m for m in log.errors), log.errors


# ── QC-062: stale shadow-dir hygiene ─────────────────────────────────────
def test_no_shadow_dirs_in_dev_layout(monkeypatch, tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "src" / "hilmar").mkdir(parents=True)
    monkeypatch.setattr(q, "REPO_ROOT", tmp_path)
    assert q.find_stale_shadow_dirs() == []   # REPO_ROOT IS the checkout


def test_shadow_dirs_found_in_cloudpc_layout(monkeypatch, tmp_path):
    # Only tests/ is a true stale shadow now — REPO_ROOT/src is an INTENTIONAL
    # QC-039 deploy target (the wrapper xcopies src\hilmar to the runtime root
    # so qc_selfheal can import hilmar.parser_accuracy), so it must NOT be swept.
    repo = tmp_path / "hilmar-daily-routine"
    (repo / "tests").mkdir(parents=True)
    (repo / "src" / "hilmar").mkdir(parents=True)
    (tmp_path / "tests").mkdir()
    (tmp_path / "src").mkdir()
    monkeypatch.setattr(q, "REPO_ROOT", tmp_path)
    assert {d.name for d in q.find_stale_shadow_dirs()} == {"tests"}


def test_qc062_self_heals_stale_shadow_dirs(monkeypatch, tmp_path):
    repo = tmp_path / "hilmar-daily-routine"
    (repo / "tests").mkdir(parents=True)
    (repo / "src" / "hilmar").mkdir(parents=True)
    (tmp_path / "tests").mkdir()
    (tmp_path / "src").mkdir()
    monkeypatch.setattr(q, "REPO_ROOT", tmp_path)
    log = _run(monkeypatch)
    assert any("QC-062" in m for m in log.fixes), log.fixes
    assert not (tmp_path / "tests").exists()
    # The deployed src/ is a required runtime dir — QC-062 must leave it intact.
    assert (tmp_path / "src").exists()


def test_qc062_preserves_deployed_src(monkeypatch, tmp_path):
    """Cloud-PC layout with REPO_ROOT/src/hilmar present (the wrapper-deployed
    QC-039 target): find_stale_shadow_dirs returns ONLY the stale tests/ shadow
    — never src, which the wrapper deploys on purpose."""
    repo = tmp_path / "hilmar-daily-routine"
    (repo / "tests").mkdir(parents=True)
    (repo / "src" / "hilmar").mkdir(parents=True)
    (tmp_path / "tests").mkdir()
    (tmp_path / "src" / "hilmar").mkdir(parents=True)
    monkeypatch.setattr(q, "REPO_ROOT", tmp_path)
    stale = q.find_stale_shadow_dirs()
    assert {d.name for d in stale} == {"tests"}
    assert all(d.name != "src" for d in stale)


# ── QC-054: dependency self-heal (no-pip path) ───────────────────────────
def test_qc054_errors_when_dep_missing_and_pip_disabled(monkeypatch):
    monkeypatch.setattr(q, "RUNTIME_IMPORT_REQUIRED", ["definitely_not_real_mod_xyz"])
    log = _run(monkeypatch)   # HILMAR_QC_NO_PIP=1 set in _run
    assert any("QC-054" in m and "definitely_not_real_mod_xyz" in m for m in log.errors), log.errors
