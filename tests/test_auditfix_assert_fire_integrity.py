"""Regression test for the assert_fire_integrity severity-separation audit fix.

Bug: a transiently-absent MSAL token cache was appended to the same
`violations` list as real delivery-proof failures. Because main() escalated
ANY non-empty `violations` to a CRITICAL "no verified report shipped" page +
exit 1, a successful send (fresh artifacts + present sent-flag + pipeline rc=0)
with merely a missing secrets/token-cache.json produced a factually-false
critical page that turned the wrapper red and tripped liveness.

Fix: the token-cache absence is now a WARNING (check_warnings), kept out of the
`violations` gate. It never drives the exit code or the critical alarm.

These tests fail against the pre-fix code (token cache absence appeared in
`violations`) and pass with the fix.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

# assert_fire_integrity lives in deploy/, load it by path.
_afi_spec = importlib.util.spec_from_file_location(
    "assert_fire_integrity", ROOT / "deploy" / "assert_fire_integrity.py")
AFI = importlib.util.module_from_spec(_afi_spec)
sys.modules["assert_fire_integrity"] = AFI
_afi_spec.loader.exec_module(AFI)


def _shipped_reports(tmp_path, today):
    """Fresh artifacts + present sent-flag == the report genuinely shipped."""
    rep = tmp_path / "reports"
    rep.mkdir()
    for name in ("email-subject.txt", "email-body.html", "hilmar-report.pdf"):
        (rep / name).write_text("x", encoding="utf-8")
    (rep / f"sent-{today}.flag").write_text("sent", encoding="utf-8")
    return rep


def test_missing_token_cache_does_not_gate_when_report_shipped(tmp_path):
    """The core regression: a report that genuinely shipped must NOT be flagged
    as a delivery-proof violation just because the token cache is absent."""
    today = AFI._et_today()
    rep = _shipped_reports(tmp_path, today)
    empty_secrets = tmp_path / "secrets"  # deliberately absent token-cache.json

    violations = AFI.check_integrity(
        pipeline_rc=0, today=today, reports=rep, secrets=empty_secrets)

    # The delivery-proof gate must be clean — the report shipped.
    assert violations == []
    # And nothing in the gate should mention the token cache.
    assert not any("token cache" in v.lower() for v in violations)


def test_missing_token_cache_surfaces_as_warning(tmp_path):
    """The token-cache absence is still surfaced — on the non-gating channel."""
    empty_secrets = tmp_path / "secrets"
    warnings = AFI.check_warnings(secrets=empty_secrets)
    assert any("token cache" in w.lower() for w in warnings)


def test_present_token_cache_yields_no_warning(tmp_path):
    sec = tmp_path / "secrets"
    sec.mkdir()
    (sec / "token-cache.json").write_text("{}", encoding="utf-8")
    assert AFI.check_warnings(secrets=sec) == []


def test_real_delivery_failures_still_gate(tmp_path):
    """Sanity: genuine non-delivery still drives violations (gate intact)."""
    today = AFI._et_today()
    rep = _shipped_reports(tmp_path, today)
    (rep / f"sent-{today}.flag").unlink()  # the email did NOT ship
    empty_secrets = tmp_path / "secrets"

    violations = AFI.check_integrity(
        pipeline_rc=1, today=today, reports=rep, secrets=empty_secrets)
    assert any("rc=1" in v for v in violations)
    assert any("NO send proof" in v for v in violations)
