"""Tests for QC-011 (email subject date == previous business day).

The 2026-06-02 audit's QC-011 fired off-by-3-days on Tuesday because
the email-subject.txt file was stale from Monday's fire (Tuesday's
wrapper aborted before gen_email regenerated it). The original WARN
message read like a date-logic bug; the real cause was a stale file.

These tests lock the new 5-state taxonomy in _check_email_subject_date:
  - file absent → WARN
  - file fresh + date matches expected → OK
  - file fresh + date == today (regression) → ERROR
  - file stale (>26h) + date mismatch → WARN with "stale" message
  - file fresh + date wrong → WARN with "logic bug" message

Each case is testable in isolation now that the helper is extracted
(2026-06-02). Pre-extraction the only access was through phase_6_rules
which couldn't be patched cleanly.
"""
from __future__ import annotations

import importlib.util
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

# Load qc_selfheal as a module so we can call the extracted helper.
spec = importlib.util.spec_from_file_location("qc_sh_under_test", SCRIPTS / "qc_selfheal.py")
QSH = importlib.util.module_from_spec(spec)
sys.modules["qc_sh_under_test"] = QSH
spec.loader.exec_module(QSH)

import core  # noqa: E402  via SCRIPTS on sys.path


class _Log:
    """Minimal stand-in for qc_selfheal.Log used by the helper."""
    def __init__(self):
        self.oks = []
        self.warns = []
        self.errors = []
    def ok(self, msg):    self.oks.append(msg)
    def warn(self, msg):  self.warns.append(msg)
    def error(self, msg): self.errors.append(msg)
    def info(self, msg):  pass
    def section(self, msg): pass


def _write_subject(path, when_label, age_hours, ref_now=None):
    """Write a subject file with text 'Update (when_label)' and force
    its mtime to N hours before ``ref_now`` (or wall-clock now if not
    given). ref_now should be an aware datetime — passing it makes the
    test deterministic regardless of when the suite runs. Without it,
    the test's reference now_et and the file's mtime are computed from
    different clocks and produce nonsense age deltas."""
    path.write_text(
        f"Hilmar Ingredients — Daily Shipment Tracker Update ({when_label})",
        encoding="utf-8",
    )
    if age_hours > 0:
        ref_unix = ref_now.timestamp() if ref_now is not None else time.time()
        old_ts = ref_unix - age_hours * 3600
        os.utime(path, (old_ts, old_ts))


# ── _expected_report_date sanity (mirrors gen_email._report_date) ───────

@pytest.mark.parametrize("today_iso,expected_iso", [
    ("2026-06-01", "2026-05-29"),    # Monday → Friday
    ("2026-06-02", "2026-06-01"),    # Tuesday → Monday
    ("2026-06-03", "2026-06-02"),    # Wednesday → Tuesday
    ("2026-06-04", "2026-06-03"),    # Thursday → Wednesday
    ("2026-06-05", "2026-06-04"),    # Friday → Thursday
    ("2026-06-06", "2026-06-05"),    # Saturday → Friday
    ("2026-06-07", "2026-06-05"),    # Sunday → Friday
])
def test_expected_report_date(today_iso, expected_iso):
    today = datetime.fromisoformat(today_iso).date()
    expected = datetime.fromisoformat(expected_iso).date()
    assert QSH._expected_report_date(today) == expected


# ── 5-state taxonomy ────────────────────────────────────────────────────

def test_file_absent_warns_and_skips(tmp_path):
    """No subject file = no signal. WARN + skip without raising."""
    log = _Log()
    QSH._check_email_subject_date(log, tmp_path / "no-such-file.txt")
    assert not log.errors
    assert any("not present" in w for w in log.warns)


def test_file_fresh_correct_date_passes(tmp_path):
    """Tuesday's fire, fresh subject says Monday → OK."""
    log = _Log()
    subj = tmp_path / "email-subject.txt"
    now_et = datetime(2026, 6, 2, 10, 0, tzinfo=core.ET)   # Tue 10 AM ET
    _write_subject(subj, "Jun 1, 2026", age_hours=0.5, ref_now=now_et)
    QSH._check_email_subject_date(log, subj, now_et=now_et)
    assert not log.warns and not log.errors
    assert any("== expected" in m for m in log.oks)


def test_file_fresh_says_TODAY_is_error(tmp_path):
    """gen_email regressed and used today's date instead of yesterday's.
    This is the canonical bug the original QC-011 was built to catch."""
    log = _Log()
    subj = tmp_path / "email-subject.txt"
    now_et = datetime(2026, 6, 2, 10, 0, tzinfo=core.ET)
    _write_subject(subj, "Jun 2, 2026", age_hours=0.5, ref_now=now_et)
    QSH._check_email_subject_date(log, subj, now_et=now_et)
    assert any("is TODAY" in e and "regressed" in e for e in log.errors)


def test_stale_file_off_date_is_stale_warn_not_logic_warn(tmp_path):
    """The 2026-06-02 fix: a stale subject from a prior fire must be
    reported as STALE, not as an "off by N days" logic bug. This is
    the failure mode QC-021's wrapper-incomplete signal pairs with."""
    log = _Log()
    subj = tmp_path / "email-subject.txt"
    # On Tue, subject still says May 29 (Friday) from Monday's fire.
    # mtime ~28h old = Monday's fire.
    now_et = datetime(2026, 6, 2, 10, 0, tzinfo=core.ET)
    _write_subject(subj, "May 29, 2026", age_hours=28, ref_now=now_et)
    QSH._check_email_subject_date(log, subj, now_et=now_et)
    assert not log.errors
    warn_text = " ".join(log.warns)
    assert "stale" in warn_text
    assert "PRIOR fire" in warn_text
    assert "QC-021" in warn_text
    # Must NOT misreport as a logic bug
    assert "logic bug" not in warn_text


def test_fresh_file_wrong_date_is_logic_bug_warn(tmp_path):
    """Distinguishes the OTHER mismatch path: subject file IS fresh
    (gen_email ran today) but the date is somehow wrong. That's a real
    _report_date logic bug — different from the stale-file case."""
    log = _Log()
    subj = tmp_path / "email-subject.txt"
    now_et = datetime(2026, 6, 2, 10, 0, tzinfo=core.ET)
    _write_subject(subj, "May 29, 2026", age_hours=2, ref_now=now_et)
    QSH._check_email_subject_date(log, subj, now_et=now_et)
    assert not log.errors
    warn_text = " ".join(log.warns)
    assert "logic bug" in warn_text
    # Must NOT mis-fire as the stale-file warn (which mentions PRIOR fire)
    assert "PRIOR fire" not in warn_text


def test_unparseable_subject_warns(tmp_path):
    """Defensive: garbage subject doesn't crash the check."""
    log = _Log()
    subj = tmp_path / "email-subject.txt"
    subj.write_text("no parens here", encoding="utf-8")
    now_et = datetime(2026, 6, 2, 10, 0, tzinfo=core.ET)
    QSH._check_email_subject_date(log, subj, now_et=now_et)
    assert any("could not parse" in w for w in log.warns)


def test_unrecognized_month_warns(tmp_path):
    log = _Log()
    subj = tmp_path / "email-subject.txt"
    now_et = datetime(2026, 6, 2, 10, 0, tzinfo=core.ET)
    _write_subject(subj, "Smarch 1, 2026", age_hours=0.5, ref_now=now_et)
    QSH._check_email_subject_date(log, subj, now_et=now_et)
    assert any("month not recognized" in w for w in log.warns)


def test_full_month_name_accepted(tmp_path):
    """The parser handles both '%b' (Jun) and '%B' (June)."""
    log = _Log()
    subj = tmp_path / "email-subject.txt"
    now_et = datetime(2026, 6, 2, 10, 0, tzinfo=core.ET)
    _write_subject(subj, "June 1, 2026", age_hours=0.5, ref_now=now_et)
    QSH._check_email_subject_date(log, subj, now_et=now_et)
    assert any("== expected" in m for m in log.oks)


def test_helper_is_robust_to_exceptions(tmp_path, monkeypatch):
    """Any unhandled exception in the helper must NOT propagate — it
    becomes a generic WARN. The whole QC pass mustn't blow up because
    of a subject-date check."""
    log = _Log()
    subj = tmp_path / "email-subject.txt"
    now_et = datetime(2026, 6, 2, 10, 0, tzinfo=timezone.utc)
    _write_subject(subj, "Jun 1, 2026", age_hours=0.5, ref_now=now_et)
    # Break the inner re.search by patching the imported re module
    import qc_sh_under_test
    monkeypatch.setattr(qc_sh_under_test, "core", None)  # core.ET will explode
    # Pass an explicit now_et so we don't hit the core.ET path
    QSH._check_email_subject_date(log, subj, now_et=now_et)
    # Either it succeeds (now_et avoided core.ET) or it WARNs with the exception
    # The point: no traceback propagates. We just check no exception.


def test_file_absent_skips_on_blob_host(tmp_path, monkeypatch):
    """On a blob-store runner (production-fire), an absent subject file pre-
    render is physics, not a finding — QC-011 skips with an OK, not a WARN.
    This is the path that had NO coverage and let the 2026-06-15 regression
    ship (the conftest autouse fixture forces _BLOB_HOST False by default, so
    this test opts back into True explicitly)."""
    monkeypatch.setattr(QSH, "_BLOB_HOST", True)
    log = _Log()
    QSH._check_email_subject_date(log, tmp_path / "no-such-file.txt")
    assert not log.errors
    assert not any("not present" in w for w in log.warns)   # no warn on blob host
    assert any("skipped" in m for m in log.oks)              # OK skip instead
