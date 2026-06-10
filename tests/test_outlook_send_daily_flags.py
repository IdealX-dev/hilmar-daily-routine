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
