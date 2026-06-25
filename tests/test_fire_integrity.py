"""Prove-or-scream: fire_alert + assert_fire_integrity + preflight_env.

These are the keystone of the reliability fix — the system proves the daily
report shipped and screams OUT-OF-BAND (never the Outlook path it's alarming
about) when the env drifts or the send didn't happen. The 2026-06 silent week
existed because the wrapper assumed success and its only alarm rode the broken
channel.
"""
from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import fire_alert  # noqa: E402
import preflight_env  # noqa: E402

# assert_fire_integrity lives in deploy/, load it by path.
_afi_spec = importlib.util.spec_from_file_location(
    "assert_fire_integrity", ROOT / "deploy" / "assert_fire_integrity.py")
AFI = importlib.util.module_from_spec(_afi_spec)
sys.modules["assert_fire_integrity"] = AFI
_afi_spec.loader.exec_module(AFI)


# ── fire_alert: always-on channels, best-effort, never raises ────────────
def test_send_alert_writes_queue_and_stderr(monkeypatch, tmp_path):
    q = tmp_path / "alerts-queue.json"
    monkeypatch.setattr(fire_alert, "REPORTS", tmp_path)
    monkeypatch.setattr(fire_alert, "ALERTS_QUEUE", q)
    # github/teams naturally no-op in the sandbox (no gh, no token, no webhook);
    # pin them off so the test never touches the network.
    monkeypatch.setattr(fire_alert, "_github_issue", lambda *a, **k: False)
    monkeypatch.setattr(fire_alert, "_teams", lambda *a, **k: False)
    res = fire_alert.send_alert("test title", "test body", level="critical")
    assert res["queue"] is True and res["stderr"] is True
    import json
    queued = json.loads(q.read_text(encoding="utf-8"))
    assert queued[-1]["title"] == "test title"
    assert queued[-1]["level"] == "critical"


def test_send_alert_never_raises_when_all_channels_fail(monkeypatch, tmp_path):
    monkeypatch.setattr(fire_alert, "REPORTS", tmp_path / "nope")
    monkeypatch.setattr(fire_alert, "ALERTS_QUEUE", tmp_path / "nope" / "q.json")
    monkeypatch.setattr(fire_alert, "_github_issue", lambda *a, **k: False)
    monkeypatch.setattr(fire_alert, "_teams", lambda *a, **k: False)
    res = fire_alert.send_alert("t", "b")   # must not raise
    assert isinstance(res, dict)


# ── assert_fire_integrity: prove the report shipped ──────────────────────
def _good_reports(tmp_path, today):
    rep = tmp_path / "reports"
    rep.mkdir()
    for name in ("email-subject.txt", "email-body.html", "hilmar-report.pdf"):
        (rep / name).write_text("x", encoding="utf-8")
    (rep / f"sent-{today}.flag").write_text("sent", encoding="utf-8")
    sec = tmp_path / "secrets"
    sec.mkdir()
    (sec / "token-cache.json").write_text("{}", encoding="utf-8")
    return rep, sec


def test_integrity_passes_when_report_shipped(tmp_path):
    today = AFI._et_today()
    rep, sec = _good_reports(tmp_path, today)
    assert AFI.check_integrity(pipeline_rc=0, today=today, reports=rep, secrets=sec) == []


def test_integrity_flags_pipeline_failure(tmp_path):
    today = AFI._et_today()
    rep, sec = _good_reports(tmp_path, today)
    v = AFI.check_integrity(pipeline_rc=1, today=today, reports=rep, secrets=sec)
    assert any("rc=1" in x for x in v)


def test_integrity_flags_missing_send_proof(tmp_path):
    today = AFI._et_today()
    rep, sec = _good_reports(tmp_path, today)
    (rep / f"sent-{today}.flag").unlink()    # the email did NOT ship
    v = AFI.check_integrity(pipeline_rc=0, today=today, reports=rep, secrets=sec)
    assert any("NO send proof" in x for x in v)


def test_integrity_flags_stale_artifact(tmp_path):
    today = AFI._et_today()
    rep, sec = _good_reports(tmp_path, today)
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).timestamp()
    import os
    os.utime(rep / "email-body.html", (yesterday, yesterday))
    v = AFI.check_integrity(pipeline_rc=0, today=today, reports=rep, secrets=sec)
    assert any("STALE" in x and "email-body.html" in x for x in v)


def test_integrity_no_require_send_skips_flag(tmp_path):
    today = AFI._et_today()
    rep, sec = _good_reports(tmp_path, today)
    (rep / f"sent-{today}.flag").unlink()
    v = AFI.check_integrity(pipeline_rc=0, require_send=False, today=today,
                            reports=rep, secrets=sec)
    assert not any("send proof" in x for x in v)


# ── preflight_env: abort loud on interpreter drift ───────────────────────
def test_preflight_hard_fails_on_interpreter_mismatch(monkeypatch):
    import qc_selfheal as q
    monkeypatch.setattr(q, "check_interpreter_parity", lambda: (False, "3.11", "3.12"))
    monkeypatch.setattr(q, "RUNTIME_IMPORT_REQUIRED", ["sys"])  # present
    monkeypatch.setattr(preflight_env, "_git_behind", lambda: 0)
    hard, soft = preflight_env.run_preflight()
    assert any("interpreter drift" in h for h in hard)


def test_preflight_soft_flags_missing_deps(monkeypatch):
    import qc_selfheal as q
    monkeypatch.setattr(q, "check_interpreter_parity", lambda: (True, "3.12", "3.12"))
    monkeypatch.setattr(q, "RUNTIME_IMPORT_REQUIRED", ["definitely_not_a_real_module_zzz"])
    monkeypatch.setattr(preflight_env, "_git_behind", lambda: 0)
    hard, soft = preflight_env.run_preflight()
    assert not hard
    assert any("not importable" in s for s in soft)


def test_preflight_clean_when_pinned_and_present(monkeypatch):
    import qc_selfheal as q
    monkeypatch.setattr(q, "check_interpreter_parity", lambda: (True, "3.12", "3.12"))
    monkeypatch.setattr(q, "RUNTIME_IMPORT_REQUIRED", ["sys", "json"])
    monkeypatch.setattr(preflight_env, "_git_behind", lambda: 0)
    hard, soft = preflight_env.run_preflight()
    assert hard == [] and soft == []


# ── env fingerprint (the sentinel's input) ───────────────────────────────
def test_fingerprint_ok(tmp_path):
    p = tmp_path / "fp.txt"
    line = preflight_env.write_fingerprint([], [], path=p)
    assert "health=ok" in line
    assert f"running={sys.version_info[0]}.{sys.version_info[1]}" in line
    assert "pinned=" in line
    # LF-only, no stray CR (would break `set /p` → heartbeat parsing).
    assert b"\r" not in p.read_bytes()
    assert p.read_text(encoding="utf-8").strip() == line


def test_fingerprint_soft_lists_missing(tmp_path):
    p = tmp_path / "fp.txt"
    line = preflight_env.write_fingerprint(
        [], ["runtime deps not importable: jinja2, msal (QC-054 will self-heal)"], path=p)
    assert "health=soft" in line
    assert "missing=jinja2,msal" in line


def test_fingerprint_drift(tmp_path):
    p = tmp_path / "fp.txt"
    line = preflight_env.write_fingerprint(["interpreter drift: 3.14 != 3.12"], [], path=p)
    assert "health=drift" in line
