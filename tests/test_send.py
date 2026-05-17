"""Tests for hilmar.send — outbound delivery wrappers.

Mocks GraphClient.send_mail / upload_to_onedrive. Asserts the
HILMAR_DAILY_CC contract: every send_daily_email call CCs the address
in HILMAR_DAILY_CC.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from hilmar import send


class StubClient:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.uploaded: list[tuple[str, Path]] = []

    def send_mail(
        self, *, to, cc, subject, html_body, attachments,
    ) -> str:
        self.sent.append({
            "to": list(to), "cc": list(cc), "subject": subject,
            "html_body": html_body, "attachments": list(attachments),
        })
        return "graph-msg-id-1"

    def upload_to_onedrive(self, *, folder_id: str, local_path: Path) -> str:
        self.uploaded.append((folder_id, local_path))
        return f"https://onedrive/{local_path.name}"

    def upload_to_onedrive_by_path(self, *, folder_path: str, local_path: Path) -> str:
        self.uploaded.append((f"path:{folder_path}", local_path))
        return f"https://onedrive{folder_path}/{local_path.name}"


def test_send_daily_email_always_ccs_hilmar_daily_cc(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("HILMAR_DAILY_CC", "michael.deitchman@idealx.us")
    c = StubClient()
    msg_id = send.send_daily_email(
        c,
        to=["recipients@ol-usa.com"],
        subject="test",
        html_body="<p>hi</p>",
        attachments=[],
    )
    assert msg_id == "graph-msg-id-1"
    assert len(c.sent) == 1
    cc = c.sent[0]["cc"]
    assert "michael.deitchman@idealx.us" in cc, f"DAILY_CC missing from cc list: {cc}"


def test_send_daily_email_dedups_cc_with_extra(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("HILMAR_DAILY_CC", "michael.deitchman@idealx.us")
    c = StubClient()
    send.send_daily_email(
        c, to=["t@x.com"], subject="s", html_body="<p>h</p>",
        cc=["someone@ol-usa.com", "michael.deitchman@idealx.us"],
    )
    cc = c.sent[0]["cc"]
    # Daily CC must appear exactly once (case-insensitive dedup).
    assert sum(1 for a in cc if a.lower() == "michael.deitchman@idealx.us") == 1


def test_send_daily_email_handles_multi_value_daily_cc(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("HILMAR_DAILY_CC", "a@x.com, b@y.com")
    c = StubClient()
    send.send_daily_email(c, to=["t@x.com"], subject="s", html_body="<p>h</p>")
    cc = c.sent[0]["cc"]
    assert "a@x.com" in cc
    assert "b@y.com" in cc


def test_send_daily_email_default_cc_is_michael_idealx(monkeypatch: pytest.MonkeyPatch):
    """Even without HILMAR_DAILY_CC set, the default falls back to Michael."""
    monkeypatch.delenv("HILMAR_DAILY_CC", raising=False)
    c = StubClient()
    send.send_daily_email(c, to=["t@x.com"], subject="s", html_body="<p>h</p>")
    cc = c.sent[0]["cc"]
    assert "michael.deitchman@idealx.us" in cc


def test_upload_artifacts_skips_missing_files(tmp_path: Path):
    c = StubClient()
    real = tmp_path / "exists.txt"
    real.write_text("hi", encoding="utf-8")
    missing = tmp_path / "ghost.txt"

    out = send.upload_artifacts(c, folder_id="folder-id-x", paths=[real, missing])

    assert real in out and "exists.txt" in out[real]
    assert missing not in out
    assert len(c.uploaded) == 1
    assert c.uploaded[0] == ("folder-id-x", real)


def test_upload_artifacts_returns_weburls_for_each_uploaded(tmp_path: Path):
    c = StubClient()
    a = tmp_path / "a.json"
    b = tmp_path / "b.pdf"
    a.write_text("{}", encoding="utf-8")
    b.write_bytes(b"%PDF-1.4 ...")

    out = send.upload_artifacts(c, folder_id="fid", paths=[a, b])

    assert out[a].endswith("a.json")
    assert out[b].endswith("b.pdf")
    assert len(c.uploaded) == 2


def test_upload_artifacts_prefers_path_over_id_when_both_passed(tmp_path: Path):
    """Path-based wins when both passed — survives folder rename/move."""
    c = StubClient()
    a = tmp_path / "a.json"
    a.write_text("{}", encoding="utf-8")
    out = send.upload_artifacts(
        c, folder_path="Hilmar Tracker Reports", folder_id="legacy-id", paths=[a],
    )
    assert "Hilmar Tracker Reports" in out[a]
    assert c.uploaded[0][0] == "path:Hilmar Tracker Reports"


def test_upload_artifacts_falls_back_to_id_when_no_path(tmp_path: Path):
    """Legacy folder_id-only callers still work."""
    c = StubClient()
    a = tmp_path / "a.json"
    a.write_text("{}", encoding="utf-8")
    out = send.upload_artifacts(c, folder_id="legacy-id", paths=[a])
    assert out[a].endswith("a.json")
    assert c.uploaded[0][0] == "legacy-id"


def test_upload_artifacts_requires_path_or_id():
    c = StubClient()
    with pytest.raises(ValueError, match="folder_path or folder_id"):
        send.upload_artifacts(c, paths=[])
