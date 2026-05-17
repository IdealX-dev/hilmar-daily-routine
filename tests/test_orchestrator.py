"""Tests for hilmar.orchestrator — the 8-step daily pipeline.

Uses an empty Graph stub (no new emails) and the golden fixture as the
starting tracking-data so the run produces the same artifacts as
test_pipeline. Verifies:

  * dry-run gate halts at step 6 (no send / no upload)
  * dry-run=false triggers send + upload via StubClient
  * archive (step 10) copies today's outputs into reports/history/<date>
  * snapshot (step 2) creates a backup before ingest
"""
from __future__ import annotations

import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from conftest import GOLDEN_DAY, SCHEMA_PATH  # pytest puts tests/ on sys.path

from hilmar import orchestrator
from hilmar.graph_client import MessageBody, MessageMeta


class EmptyGraphClient:
    """No new emails — re-using existing tracking-data unchanged."""

    sent: list[dict[str, Any]]
    uploaded: list[tuple[str, Path]]

    def __init__(self) -> None:
        self.sent = []
        self.uploaded = []

    # GraphClient API surface used by ingest
    def search_messages(self, **_: Any) -> list[MessageMeta]:
        return []

    def get_message_body(self, _: str) -> MessageBody:  # pragma: no cover
        raise RuntimeError("EmptyGraphClient.get_message_body should not be called")

    # GraphClient API surface used by send.py
    def send_mail(self, *, to, cc, subject, html_body, attachments) -> str:
        self.sent.append({
            "to": list(to), "cc": list(cc), "subject": subject,
            "attachments": list(attachments),
        })
        return "msg-id-fake"

    def upload_to_onedrive(self, *, folder_id: str, local_path: Path) -> str:
        self.uploaded.append((folder_id, local_path))
        return f"https://onedrive.fake/{local_path.name}"


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Real on-VM-ish layout under tmp_path; orchestrator's path-helpers
    are env-driven so HILMAR_DATA_DIR/REPORTS_DIR/BACKUP_DIR get pointed
    at this scratch tree.
    """
    data_d = tmp_path / "data"
    reports_d = tmp_path / "reports"
    backup_d = tmp_path / "data-backups"
    data_d.mkdir()
    reports_d.mkdir()
    backup_d.mkdir()
    shutil.copy2(GOLDEN_DAY, data_d / "tracking-data-v2.json")
    shutil.copy2(SCHEMA_PATH, data_d / "schema.json")
    monkeypatch.setenv("HILMAR_DATA_DIR", str(data_d))
    monkeypatch.setenv("HILMAR_REPORTS_DIR", str(reports_d))
    monkeypatch.setenv("HILMAR_BACKUP_DIR", str(backup_d))
    return tmp_path


def test_dry_run_default_halts_after_render(
    workspace: Path, monkeypatch: pytest.MonkeyPatch,
):
    """With HILMAR_DRY_RUN unset (or "true"), the run must NOT send / upload."""
    monkeypatch.setenv("HILMAR_DRY_RUN", "true")
    client = EmptyGraphClient()
    result = orchestrator.run(
        client_factory=lambda: client,
        skip_llm=True,
        now=datetime(2026, 4, 26, 12, 0, tzinfo=timezone.utc),
    )
    assert result["dry_run"] is True
    assert client.sent == [], "send_mail called in dry-run"
    assert client.uploaded == [], "upload called in dry-run"
    # All four artifacts still rendered.
    artifacts = result["artifacts"]
    assert Path(artifacts["dashboard"]).exists()
    assert Path(artifacts["pdf"]).exists()
    assert Path(artifacts["email"]).exists()
    assert artifacts["scorecards"], "no scorecards generated"


def test_dry_run_false_triggers_send_and_upload(
    workspace: Path, monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("HILMAR_DRY_RUN", "false")
    monkeypatch.setenv("HILMAR_ONEDRIVE_FOLDER_ID", "folder-id-test")
    monkeypatch.setenv("HILMAR_DAILY_TO", "ol-group@ol-usa.com")
    monkeypatch.setenv("HILMAR_DAILY_CC", "michael.deitchman@idealx.us")

    client = EmptyGraphClient()
    result = orchestrator.run(
        client_factory=lambda: client,
        skip_llm=True,
        now=datetime(2026, 4, 26, 12, 0, tzinfo=timezone.utc),
    )

    assert result["dry_run"] is False
    assert result["message_id"] == "msg-id-fake"
    # Two sends now: (1) staff daily to TO+CC, (2) internal review to
    # HILMAR_INTERNAL_TO (defaults to HILMAR_DAILY_CC = idealx). The
    # split is per Michael's 2026-04-28 directive — operational
    # narrative goes to him only, not to the staff distribution.
    assert len(client.sent) == 2
    staff = next(s for s in client.sent if "ol-group@ol-usa.com" in s.get("to", []))
    internal = next(s for s in client.sent if "[INTERNAL]" in s.get("subject", ""))
    # Staff daily — recipients + CC contract.
    assert staff["to"] == ["ol-group@ol-usa.com"]
    assert "michael.deitchman@idealx.us" in staff["cc"]
    attach_names = {Path(a).name for a in staff["attachments"]}
    assert "hilmar-report.pdf" in attach_names
    # Per Michael 2026-04-28: dashboard HTML must NOT be attached
    # (Outlook forces download). Inlined into email body instead.
    assert "hilmar-dashboard.html" not in attach_names
    # Internal review — Michael only, no CC, no attachments.
    assert internal["to"] == ["michael.deitchman@idealx.us"]
    assert internal["cc"] == []
    assert internal["attachments"] == []

    # Upload pushed at minimum the data file + dashboard + pdf.
    upload_names = {p.name for _, p in client.uploaded}
    assert "tracking-data-v2.json" in upload_names
    assert "hilmar-dashboard.html" in upload_names
    assert "hilmar-report.pdf" in upload_names


def test_snapshot_creates_backup_before_ingest(
    workspace: Path, monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("HILMAR_DRY_RUN", "true")
    client = EmptyGraphClient()
    backup_d = workspace / "data-backups"
    pre = list(backup_d.glob("tracking-data-v2.*.json"))
    assert pre == []

    orchestrator.run(client_factory=lambda: client, skip_llm=True)

    post = list(backup_d.glob("tracking-data-v2.*.json"))
    assert len(post) == 1, f"expected one snapshot, got {post}"


def test_archive_copies_outputs_into_history(
    workspace: Path, monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("HILMAR_DRY_RUN", "false")
    monkeypatch.setenv("HILMAR_ONEDRIVE_FOLDER_ID", "fid")
    client = EmptyGraphClient()
    pinned = datetime(2026, 4, 26, 12, 0, tzinfo=timezone.utc)
    result = orchestrator.run(client_factory=lambda: client, skip_llm=True, now=pinned)

    history = workspace / "reports" / "history" / "2026-04-26"
    assert history.exists()
    assert (history / "hilmar-dashboard.html").exists()
    assert (history / "hilmar-report.pdf").exists()
    assert (history / "email-body.html").exists()
    assert result["archive"] == history


def test_is_dry_run_default_is_true(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("HILMAR_DRY_RUN", raising=False)
    assert orchestrator.is_dry_run() is True


def test_is_dry_run_explicit_false(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("HILMAR_DRY_RUN", "false")
    assert orchestrator.is_dry_run() is False


def test_is_dry_run_anything_else_is_dry_run(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("HILMAR_DRY_RUN", "yes")
    assert orchestrator.is_dry_run() is True


# ─────────────────────────────────────────────────────────────────────
# M3.11.c — insights integration
# ─────────────────────────────────────────────────────────────────────


def test_insights_integration_dry_run_writes_json_and_html(
    workspace: Path, monkeypatch: pytest.MonkeyPatch,
):
    """Dry-run + skip_llm: orchestrator still updates baselines + writes
    reports/insights/<date>.{json,html}."""
    monkeypatch.setenv("HILMAR_DRY_RUN", "true")
    client = EmptyGraphClient()
    pinned = datetime(2026, 4, 26, 12, 0, tzinfo=timezone.utc)
    result = orchestrator.run(
        client_factory=lambda: client, skip_llm=True, now=pinned,
    )

    insights = result.get("insights")
    assert insights is not None
    assert Path(insights["json"]).exists()
    assert Path(insights["html"]).exists()
    bl = workspace / "data" / "baselines.json"
    assert bl.exists(), "baselines.json not written by orchestrator"


def test_insights_integration_runs_llm_when_router_provided(
    workspace: Path, monkeypatch: pytest.MonkeyPatch,
):
    """When skip_llm=False and a router_factory is supplied, the LLM
    narrative is generated and embedded in the email body."""
    from hilmar.model_router import ModelResponse

    class StubRouter:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def call(self, *, task_type, prompt, system=None, max_tokens=4096):
            self.calls.append(task_type)
            return ModelResponse(
                text=f"- {task_type} bullet",
                model="claude-opus-4-6", task_type=task_type,
                input_tokens=10, output_tokens=10, cost_cents=5,
            )

        def should_alert_cost(self) -> bool:
            return False

    stub = StubRouter()
    monkeypatch.setenv("HILMAR_DRY_RUN", "true")
    client = EmptyGraphClient()
    pinned = datetime(2026, 4, 26, 12, 0, tzinfo=timezone.utc)
    result = orchestrator.run(
        client_factory=lambda: client,
        router_factory=lambda: stub,
        skip_llm=False,
        now=pinned,
    )

    assert {"system_critique", "design_suggestions", "data_suggestions", "business_advice"} <= set(stub.calls)
    email_html = Path(result["artifacts"]["email"]).read_text(encoding="utf-8")
    # Per Michael's 2026-04-28 split: staff email contains ONLY the
    # business advice section. System/Design/Data are operational-
    # internal (parser miss-rates, schema drift, banner placement
    # tweaks) and don't belong in the daily distribution.
    assert "business_advice bullet" in email_html
    assert "system_critique bullet" not in email_html
    assert "design_suggestions bullet" not in email_html
    assert "data_suggestions bullet" not in email_html
    # The internal version, however, must contain all four — verifiable
    # via the on-disk internal HTML written by step_insights.
    internal_html = Path(result["insights"]["json"]).parent / "2026-04-26.internal.html"
    if internal_html.exists():
        text = internal_html.read_text(encoding="utf-8")
        assert "system_critique bullet" in text
        assert "business_advice bullet" in text


def test_insights_integration_router_call_failure_does_not_abort_run(
    workspace: Path, monkeypatch: pytest.MonkeyPatch,
):
    """A router that constructs OK but throws on first .call() (e.g. the
    anthropic SDK is missing on the deploy host) MUST NOT kill the daily
    run. The orchestrator should ship rule-based metrics with a
    "LLM-narrative skipped" note in the email body.

    Regression-guards rate-blaster-v2 first-run 2026-04-26-23:25 where
    `ModuleNotFoundError: anthropic` made the whole run exit 1 after
    QC + baselines had already succeeded.
    """

    class ThrowingRouter:
        def __init__(self) -> None:
            self.calls = 0

        def call(self, *, task_type, prompt, system=None, max_tokens=4096):
            self.calls += 1
            raise ModuleNotFoundError("anthropic SDK not installed; pip install anthropic")

        def should_alert_cost(self) -> bool:
            return False

    monkeypatch.setenv("HILMAR_DRY_RUN", "true")
    client = EmptyGraphClient()
    pinned = datetime(2026, 4, 26, 12, 0, tzinfo=timezone.utc)

    # Should NOT raise.
    result = orchestrator.run(
        client_factory=lambda: client,
        router_factory=lambda: ThrowingRouter(),
        skip_llm=False,
        now=pinned,
    )

    assert result["dry_run"] is True
    # Insights JSON exists, marked LLM-narrative skipped in email.
    email_html = Path(result["artifacts"]["email"]).read_text(encoding="utf-8")
    assert "LLM narrative skipped" in email_html or "LLM-narrative skipped" in email_html
    insights_html = Path(result["insights"]["html"]).read_text(encoding="utf-8")
    assert "LLM narrative skipped" in insights_html or "LLM-narrative skipped" in insights_html


def test_failure_webhook_no_op_when_unset(monkeypatch):
    """Without HILMAR_FAILURE_WEBHOOK env, the webhook helper is a
    quiet no-op — no network call, no exception."""
    monkeypatch.delenv("HILMAR_FAILURE_WEBHOOK", raising=False)
    orchestrator._try_failure_webhook("synthetic\ntraceback")


def test_failure_webhook_swallows_its_own_errors(monkeypatch):
    """Webhook failure must NEVER mask the original orchestrator
    exception. We simulate this by pointing the webhook at an
    unreachable URL and asserting the helper returns cleanly."""
    monkeypatch.setenv("HILMAR_FAILURE_WEBHOOK", "http://127.0.0.1:1/never-listens")
    orchestrator._try_failure_webhook("synthetic\ntraceback")


def test_failure_webhook_posts_payload_when_set(monkeypatch):
    """When the webhook is set and reachable, _try_failure_webhook
    POSTs a JSON payload with the traceback tail."""
    captured: dict[str, Any] = {}

    class FakeResponse:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["data"] = req.data
        captured["headers"] = dict(req.header_items())
        return FakeResponse()

    import urllib.request as ur
    monkeypatch.setattr(ur, "urlopen", fake_urlopen)
    monkeypatch.setenv("HILMAR_FAILURE_WEBHOOK", "http://example.invalid/hook")
    orchestrator._try_failure_webhook("Traceback (most recent call last):\n  Boom!")

    assert captured["url"] == "http://example.invalid/hook"
    import json as _json
    body = _json.loads(captured["data"].decode("utf-8"))
    assert "Boom!" in body["traceback_tail"]
    assert body["service"] == "hilmar-tracker"
    assert body["severity"] == "error"
    assert "text" in body  # Slack-compat key


def test_failure_email_no_op_when_disabled(monkeypatch):
    """Setting HILMAR_FAILURE_EMAIL='' explicitly disables the email path."""
    monkeypatch.setenv("HILMAR_FAILURE_EMAIL", "")
    # No GraphClient should be touched. Must not raise.
    orchestrator._try_failure_email("synthetic\ntraceback")


def test_failure_email_sends_via_graph_when_set(monkeypatch):
    """When the recipient is set and Graph auth works, the email helper
    sends a message via GraphClient.send_mail. We mock GraphClient so
    the test is hermetic."""
    captured: dict[str, Any] = {}

    class FakeGraphClient:
        def authenticate(self, interactive_ok=False):
            captured["authed"] = True
        def send_mail(self, *, to, cc, subject, html_body, attachments):
            captured["to"] = to
            captured["subject"] = subject
            captured["html_body"] = html_body
            return "fake-msg-id-123"

    import hilmar.send as _send_mod
    monkeypatch.setattr("hilmar.graph_client.GraphClient", lambda: FakeGraphClient())
    monkeypatch.setattr(_send_mod, "GraphClient", lambda: FakeGraphClient())
    monkeypatch.setenv("HILMAR_FAILURE_EMAIL", "michael.deitchman@idealx.us")

    orchestrator._try_failure_email("Traceback (most recent call last):\n  RuntimeError: boom!")

    assert captured["authed"] is True
    assert captured["to"] == ["michael.deitchman@idealx.us"]
    assert "FAILED" in captured["subject"]
    assert "boom!" in captured["html_body"]


def test_failure_email_swallows_graph_errors(monkeypatch):
    """If Graph auth itself is the failure (the dual-channel design's
    motivation), the email helper logs and returns — must not raise."""
    class BrokenGraphClient:
        def authenticate(self, interactive_ok=False):
            raise RuntimeError("auth refused")
        def send_mail(self, **kw):
            raise AssertionError("should never be called")

    monkeypatch.setattr("hilmar.graph_client.GraphClient", lambda: BrokenGraphClient())
    monkeypatch.setenv("HILMAR_FAILURE_EMAIL", "michael.deitchman@idealx.us")
    # Must NOT raise.
    orchestrator._try_failure_email("synthetic\ntraceback")


def test_page_on_failure_calls_both_channels(monkeypatch):
    """The unified entrypoint dispatches to both email and webhook
    helpers — neither path's failure blocks the other."""
    calls: list[str] = []
    monkeypatch.setattr(orchestrator, "_try_failure_email",
                        lambda tb: calls.append(f"email:{tb[:5]}"))
    monkeypatch.setattr(orchestrator, "_try_failure_webhook",
                        lambda tb: calls.append(f"webhook:{tb[:5]}"))
    orchestrator._page_on_failure("hello world")
    assert calls == ["email:hello", "webhook:hello"]


def test_step_upload_swallows_graph_errors_post_send():
    """step_upload runs AFTER step_send — by the time it executes, the
    email is already in recipients' inboxes. A Graph PUT failure (stale
    OneDrive folder, permissions, etc.) must NOT raise into the run().
    Returns empty dict so downstream steps continue."""
    from pathlib import Path as _Path
    monkeypatch_set = "HILMAR_ONEDRIVE_FOLDER_ID" in os.environ
    if not monkeypatch_set:
        os.environ["HILMAR_ONEDRIVE_FOLDER_ID"] = "fake-folder-id"

    class ExplodingUploader:
        def upload_to_onedrive(self, *, folder_id, local_path):
            raise RuntimeError("simulated 404 from Graph")

    try:
        out = orchestrator.step_upload(
            client=ExplodingUploader(),
            data_path=_Path("/tmp/nonexistent.json"),
            artifacts={"dashboard": "/tmp/dash.html", "pdf": "/tmp/report.pdf"},
        )
        assert out == {}  # graceful degrade
    finally:
        if not monkeypatch_set:
            os.environ.pop("HILMAR_ONEDRIVE_FOLDER_ID", None)


def test_step_upload_404_logs_info_not_warning(monkeypatch, caplog):
    """Stale-folder 404 is the known noise pattern (Michael's OneDrive
    folder was renamed/moved). Step downgrades to log.info so daily
    journalctl stays clean; non-404 errors keep the warning level."""
    import logging
    from pathlib import Path as _Path

    from hilmar import send as send_mod
    monkeypatch.setenv("HILMAR_ONEDRIVE_FOLDER_ID", "stale-folder-id")

    def boom(**kw):
        raise RuntimeError("Graph PUT /me/drive/items/abc:/file failed: 404 itemNotFound")
    monkeypatch.setattr(send_mod, "upload_artifacts", boom)

    caplog.set_level(logging.INFO, logger="hilmar.orchestrator")
    out = orchestrator.step_upload(
        client=object(),
        data_path=_Path("/tmp/x.json"),
        artifacts={"dashboard": "/tmp/d.html", "pdf": "/tmp/r.pdf"},
    )
    assert out == {}
    levels = [(r.levelname, r.getMessage()) for r in caplog.records
              if "step_upload" in r.getMessage()]
    assert any(level == "INFO" and "stale OneDrive folder" in msg
               for level, msg in levels), f"expected INFO log, got {levels}"


def test_step_upload_non_404_errors_still_warn(monkeypatch, caplog):
    """Real auth/permission regressions stay at WARNING so they're
    visible. Only the 404/itemNotFound stale-folder pattern gets the
    INFO downgrade."""
    import logging
    from pathlib import Path as _Path

    from hilmar import send as send_mod
    monkeypatch.setenv("HILMAR_ONEDRIVE_FOLDER_ID", "fake-id")

    def boom(**kw):
        raise RuntimeError("401 InvalidAuthenticationToken")
    monkeypatch.setattr(send_mod, "upload_artifacts", boom)

    caplog.set_level(logging.INFO, logger="hilmar.orchestrator")
    orchestrator.step_upload(
        client=object(),
        data_path=_Path("/tmp/x.json"),
        artifacts={"dashboard": "/tmp/d.html", "pdf": "/tmp/r.pdf"},
    )
    levels = [(r.levelname, r.getMessage()) for r in caplog.records
              if "step_upload" in r.getMessage()]
    assert any(level == "WARNING" for level, _ in levels), \
        f"expected WARNING log for non-404, got {levels}"
