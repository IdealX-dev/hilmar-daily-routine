"""cmd_daily idempotency-flag behavior in scripts/outlook_send.py.

The flag files are the only thing standing between the client and a
duplicate daily email, so their semantics are pinned here:
  - ET-dated, regardless of host clock (a GH runner is UTC; a bare .now()
    would roll the date at 8 PM ET and stamp UTC times labeled "ET")
  - an existing flag blocks a re-send unless --force
  - --no-flag (verification/test sends) neither reads nor writes the flag
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import outlook_send as os_send  # noqa: E402


def _today_et() -> str:
    return datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")


def _args(tmp_path, **over):
    subj = tmp_path / "subject.txt"
    body = tmp_path / "body.html"
    subj.write_text("Daily Tracker", encoding="utf-8")
    body.write_text("<p>hi</p>", encoding="utf-8")
    base = dict(
        to=None, cc=None, to_from_config=False,
        subject_from_file=str(subj), body_from_file=str(body),
        attach=None, dry=False, force=False, no_flag=False,
    )
    base.update(over)
    return SimpleNamespace(**base)


def _wire(tmp_path, monkeypatch):
    """Point flag writes at tmp and stub the network. Returns the send log."""
    sends: list[list[str]] = []
    monkeypatch.setattr(os_send, "ROOT", tmp_path)
    monkeypatch.setattr(os_send, "send_mail",
                        lambda **kw: sends.append(kw["to"]) or "req-test")
    monkeypatch.setattr(os_send, "_load_distribution_from_config",
                        lambda: (["a@hilmar.com", "b@ol-usa.com"], []))
    monkeypatch.setattr(os_send, "_sent_today_in_mailbox", lambda s: None)
    return sends


def test_full_distribution_writes_et_dated_flag(tmp_path, monkeypatch):
    sends = _wire(tmp_path, monkeypatch)
    assert os_send.cmd_daily(_args(tmp_path, to_from_config=True)) == 0
    assert sends == [["a@hilmar.com", "b@ol-usa.com"]]
    flag = tmp_path / "reports" / f"sent-{_today_et()}.flag"
    assert flag.exists()
    assert " ET req=req-test" in flag.read_text(encoding="utf-8")


def test_existing_flag_blocks_resend_without_force(tmp_path, monkeypatch):
    sends = _wire(tmp_path, monkeypatch)
    args = _args(tmp_path, to_from_config=True)
    assert os_send.cmd_daily(args) == 0
    assert os_send.cmd_daily(args) == 0  # second call: refused, still rc=0
    assert len(sends) == 1


def test_no_flag_skips_read_and_write(tmp_path, monkeypatch):
    sends = _wire(tmp_path, monkeypatch)
    # Pre-existing flag must not block a --no-flag send...
    flag = tmp_path / "reports" / f"sent-{_today_et()}.flag"
    flag.parent.mkdir(parents=True)
    flag.write_text("Sent earlier\n", encoding="utf-8")
    assert os_send.cmd_daily(_args(tmp_path, to_from_config=True, no_flag=True)) == 0
    assert len(sends) == 1
    # ...and the send must not have appended to it either.
    assert flag.read_text(encoding="utf-8") == "Sent earlier\n"


def test_single_idealx_recipient_writes_audit_flag(tmp_path, monkeypatch):
    _wire(tmp_path, monkeypatch)
    assert os_send.cmd_daily(_args(tmp_path, to=["michael.deitchman@idealx.us"])) == 0
    assert (tmp_path / "reports" / f"improvements-sent-{_today_et()}.flag").exists()


def test_noninteractive_guard_blocks_device_code(monkeypatch, tmp_path):
    # On a headless runner a failed silent refresh must raise, not print a
    # device code and hang until the job times out.
    monkeypatch.setenv("HILMAR_NONINTERACTIVE", "1")
    monkeypatch.setattr(os_send, "TOKEN_CACHE_PATH", tmp_path / "absent.json")

    class _App:
        def __init__(self, *a, **k): pass
        def get_accounts(self): return []
        def acquire_token_silent(self, *a, **k): return None
        def initiate_device_flow(self, **k):
            raise AssertionError("device flow must not start when non-interactive")

    monkeypatch.setattr(os_send.msal, "PublicClientApplication", _App)
    import pytest
    with pytest.raises(RuntimeError, match="HILMAR_NONINTERACTIVE"):
        os_send.get_token()


def test_mailbox_guard_blocks_cross_machine_double_send(tmp_path, monkeypatch):
    # 2026-06-11: the Cloud PC's still-enabled task sent at 10:02 ET; the GH
    # fire at 10:07 had no local/blob flag and would have sent the client
    # email AGAIN. The mailbox is the one shared truth across machines.
    sends = _wire(tmp_path, monkeypatch)
    monkeypatch.setattr(os_send, "_sent_today_in_mailbox",
                        lambda s: "2026-06-11T14:02:56Z")
    assert os_send.cmd_daily(_args(tmp_path, to_from_config=True)) == 0
    assert sends == []  # refused — nothing sent


def test_mailbox_guard_fails_open(tmp_path, monkeypatch):
    # A Graph hiccup must never block the real send.
    sends = _wire(tmp_path, monkeypatch)
    monkeypatch.setattr(os_send, "_sent_today_in_mailbox", lambda s: None)
    assert os_send.cmd_daily(_args(tmp_path, to_from_config=True)) == 0
    assert len(sends) == 1


def test_mailbox_guard_skipped_for_test_and_audit_sends(tmp_path, monkeypatch):
    sends = _wire(tmp_path, monkeypatch)

    def _boom(s):
        raise AssertionError("guard must not run for non-full sends")
    monkeypatch.setattr(os_send, "_sent_today_in_mailbox", _boom)
    assert os_send.cmd_daily(_args(tmp_path, to=["michael.deitchman@idealx.us"])) == 0
    assert len(sends) == 1


def test_mailbox_guard_bypassed_by_force(tmp_path, monkeypatch):
    sends = _wire(tmp_path, monkeypatch)

    def _boom(s):
        raise AssertionError("guard must not run under --force")
    monkeypatch.setattr(os_send, "_sent_today_in_mailbox", _boom)
    assert os_send.cmd_daily(_args(tmp_path, to_from_config=True, force=True)) == 0
    assert len(sends) == 1
