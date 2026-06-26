"""Audit-fix regression test for scripts/qc_actions_from_sentry.py.

Finding (security, low): _do_claude_diagnose forwards the Sentry issue
`title` and `culprit` to the Anthropic API (a second external provider)
and re-posts a comment back to Sentry. It relied entirely on the
fail-open capture-time scrubber (sentry_setup._before_send). This test
pins the defense-in-depth egress scrub: an issue whose title/culprit
contain an email address and an MDOLX booking ref must NOT reach the
Anthropic prompt verbatim — they must be redacted by
sentry_setup._scrub_string before the prompt is built.

Without the fix the raw PII lands in the prompt and the assertions fail.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import qc_actions_from_sentry as QA  # noqa: E402, I001


_RAW_EMAIL = "lupfold@hilmaringredients.com"
_RAW_MDOLX = "MDOLX1234567"


class _FakeUsage:
    input_tokens = 10
    output_tokens = 20


class _FakeContentBlock:
    text = "Likely root cause: parser miss. Fix: harden body_parser."


class _FakeMessages:
    def __init__(self, captured: dict):
        self._captured = captured

    def create(self, *, model, max_tokens, messages):
        # Capture the exact prompt text sent to Anthropic.
        self._captured["prompt"] = messages[0]["content"]

        class _Resp:
            content = [_FakeContentBlock()]
            usage = _FakeUsage()

        return _Resp()


class _FakeAnthropicClient:
    def __init__(self, captured: dict, **kwargs):
        self.messages = _FakeMessages(captured)


class _FakeAnthropicModule:
    def __init__(self, captured: dict):
        self._captured = captured

    def Anthropic(self, **kwargs):
        return _FakeAnthropicClient(self._captured, **kwargs)


class _FakeApi:
    def __init__(self):
        self.posted_comments = []

    def _request(self, method, path, json=None):
        self.posted_comments.append({"method": method, "path": path, "json": json})
        return {}


def _run_diagnose(monkeypatch, *, title: str, culprit: str) -> tuple[dict, _FakeApi]:
    captured: dict = {}

    # Force a usable API key + fake the anthropic SDK so no real call happens.
    import pdf_llm_rescue
    monkeypatch.setattr(pdf_llm_rescue, "_load_api_key", lambda: "fake-key")
    monkeypatch.setitem(sys.modules, "anthropic", _FakeAnthropicModule(captured))

    api = _FakeApi()
    issue = {
        "id": "abc123",
        "shortId": "TRACKER-99",
        "title": title,
        "culprit": culprit,
        "level": "error",
        "count": 3,
        "platform": "python",
    }
    result = QA._do_claude_diagnose(api, issue, {}, dry_run=False)
    return captured, api, result


def test_claude_diagnose_scrubs_pii_from_anthropic_prompt(monkeypatch):
    captured, api, result = _run_diagnose(
        monkeypatch,
        title=f"KeyError for {_RAW_EMAIL}",
        culprit=f"booking {_RAW_MDOLX} not found",
    )

    assert result.get("ok") is True
    prompt = captured["prompt"]

    # The raw PII must NOT survive into the prompt sent to Anthropic.
    assert _RAW_EMAIL not in prompt
    assert _RAW_MDOLX not in prompt
    # And the redaction tokens prove the scrub actually ran.
    assert "[EMAIL_REDACTED]" in prompt
    assert "[MDOLX_REDACTED]" in prompt


def test_claude_diagnose_posts_comment_without_raw_pii(monkeypatch):
    captured, api, result = _run_diagnose(
        monkeypatch,
        title=f"KeyError for {_RAW_EMAIL}",
        culprit=f"booking {_RAW_MDOLX} not found",
    )

    assert len(api.posted_comments) == 1
    comment_text = api.posted_comments[0]["json"]["text"]
    # The posted Sentry comment carries the model diagnosis + tokens only;
    # it must never echo the raw title/culprit PII.
    assert _RAW_EMAIL not in comment_text
    assert _RAW_MDOLX not in comment_text


def test_clean_title_passes_through_unchanged(monkeypatch):
    # A PII-free title is untouched (scrub is identity on safe text).
    captured, api, result = _run_diagnose(
        monkeypatch,
        title="ValueError in gen_dashboard",
        culprit="gen_dashboard.py in render",
    )
    prompt = captured["prompt"]
    assert "ValueError in gen_dashboard" in prompt
    assert "gen_dashboard.py in render" in prompt
