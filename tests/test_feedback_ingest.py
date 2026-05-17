"""Tests for hilmar.feedback_ingest — INSIGHT-FEEDBACK email parser +
log persistence + summary for the next-run LLM prompts.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from hilmar import feedback_ingest as fb
from hilmar.graph_client import MessageMeta

UTC = timezone.utc


# ─────────────────────────────────────────────────────────────────────
# Subject parsing + rating normalisation
# ─────────────────────────────────────────────────────────────────────


def test_parse_subject_thumbs_up_emoji():
    out = fb.parse_subject("INSIGHT-FEEDBACK abc123 👍")
    assert out == ("abc123", "up")


def test_parse_subject_thumbs_down_emoji():
    out = fb.parse_subject("INSIGHT-FEEDBACK xyz999 👎")
    assert out == ("xyz999", "down")


def test_parse_subject_noise_emoji():
    out = fb.parse_subject("INSIGHT-FEEDBACK foo.bar 💤")
    assert out == ("foo.bar", "noise")


def test_parse_subject_word_form():
    out = fb.parse_subject("INSIGHT-FEEDBACK abc thumbs up")
    assert out == ("abc", "up")


def test_parse_subject_underscored_keyword():
    out = fb.parse_subject("INSIGHT_FEEDBACK abc 👍")
    assert out == ("abc", "up")


def test_parse_subject_returns_none_on_unknown_rating():
    assert fb.parse_subject("INSIGHT-FEEDBACK abc maybe?") is None


def test_parse_subject_returns_none_on_non_feedback_subject():
    assert fb.parse_subject("Re: hello") is None
    assert fb.parse_subject("") is None


def test_normalise_rating_handles_plus_minus_one():
    assert fb.normalise_rating("+1") == "up"
    assert fb.normalise_rating("-1") == "down"


# ─────────────────────────────────────────────────────────────────────
# Log persistence
# ─────────────────────────────────────────────────────────────────────


def _rec(rid: str = "id1", rating: str = "up", subj: str | None = None,
         when: datetime | None = None, section: str | None = None) -> fb.FeedbackRecord:
    return fb.FeedbackRecord(
        insight_id=rid,
        rating=rating,
        received_at=(when or datetime(2026, 4, 26, 10, 0, tzinfo=UTC)).isoformat(),
        section=section,
        raw_subject=subj or f"INSIGHT-FEEDBACK {rid} 👍",
    )


def test_load_returns_empty_when_missing(tmp_path: Path):
    assert fb.load_log(tmp_path / "nope.json") == []


def test_load_handles_corrupt_file(tmp_path: Path):
    p = tmp_path / "feedback.json"
    p.write_text("not json", encoding="utf-8")
    assert fb.load_log(p) == []


def test_save_then_load_round_trip(tmp_path: Path):
    p = tmp_path / "feedback.json"
    fb.save_log([_rec(), _rec("id2", "down")], p)
    out = fb.load_log(p)
    assert len(out) == 2
    assert {r.insight_id for r in out} == {"id1", "id2"}


def test_upsert_replaces_same_key():
    base = [_rec("id1", "up", subj="S1")]
    updated = fb.upsert(base, _rec("id1", "down", subj="S1"))
    assert len(updated) == 1
    assert updated[0].rating == "down"


def test_upsert_appends_new_id():
    base = [_rec("id1", "up", subj="S1")]
    out = fb.upsert(base, _rec("id2", "noise", subj="S2"))
    assert len(out) == 2


# ─────────────────────────────────────────────────────────────────────
# ingest_from_graph (mocked GraphClient)
# ─────────────────────────────────────────────────────────────────────


class _StubGraph:
    def __init__(self, metas: list[MessageMeta]) -> None:
        self.metas = metas
        self.calls: list[dict] = []

    def search_messages(self, **kwargs) -> list[MessageMeta]:
        self.calls.append(kwargs)
        return self.metas


def _meta_for_subject(subject: str, msg_id: str = "m1",
                     when: datetime | None = None) -> MessageMeta:
    when = when or datetime(2026, 4, 26, 9, 0, tzinfo=UTC)
    return MessageMeta(
        id=msg_id, conversation_id="c", subject=subject,
        from_address="michael.deitchman@idealx.us",
        from_name="Michael",
        to_addresses=["michael.deitchman@idealx.us"],
        cc_addresses=[],
        received_at=when, sent_at=when,
        internet_message_id=f"<{msg_id}@x>",
        is_read=True, has_attachments=False,
    )


def test_ingest_from_graph_appends_new_records(tmp_path: Path):
    log = tmp_path / "feedback.json"
    metas = [
        _meta_for_subject("INSIGHT-FEEDBACK 2026-04-25.business.1 👍", "m1"),
        _meta_for_subject("INSIGHT-FEEDBACK 2026-04-25.system.2 💤",   "m2"),
    ]
    n = fb.ingest_from_graph(client=_StubGraph(metas), log_path=log)
    assert n == 2
    saved = fb.load_log(log)
    assert {r.insight_id for r in saved} == {"2026-04-25.business.1", "2026-04-25.system.2"}
    # section inferred from the dotted id.
    by_id = {r.insight_id: r for r in saved}
    assert by_id["2026-04-25.business.1"].section == "business"


def test_ingest_from_graph_ignores_non_feedback_subjects(tmp_path: Path):
    log = tmp_path / "feedback.json"
    metas = [
        _meta_for_subject("INSIGHT-FEEDBACK abc 👍", "m1"),
        _meta_for_subject("RE: lunch?", "m2"),  # ignored
        _meta_for_subject("INSIGHT-FEEDBACK def maybe?", "m3"),  # ignored — bad rating
    ]
    n = fb.ingest_from_graph(client=_StubGraph(metas), log_path=log)
    assert n == 1


def test_ingest_from_graph_is_idempotent(tmp_path: Path):
    log = tmp_path / "feedback.json"
    metas = [_meta_for_subject("INSIGHT-FEEDBACK abc 👍", "m1")]
    fb.ingest_from_graph(client=_StubGraph(metas), log_path=log)
    n2 = fb.ingest_from_graph(client=_StubGraph(metas), log_path=log)
    assert n2 == 0
    assert len(fb.load_log(log)) == 1


# ─────────────────────────────────────────────────────────────────────
# Mailto buttons
# ─────────────────────────────────────────────────────────────────────


def test_feedback_button_html_contains_mailto_with_subject(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("HILMAR_INSIGHTS_FEEDBACK_TO", "feedback@idealx.us")
    html = fb.feedback_button_html("abc123", "Helpful", "👍")
    assert "mailto:feedback@idealx.us" in html
    assert "INSIGHT-FEEDBACK%20abc123%20%F0%9F%91%8D" in html or "INSIGHT-FEEDBACK abc123" in html or "abc123" in html


def test_insights_feedback_strip_has_three_buttons():
    html = fb.insights_feedback_strip("today.system.1")
    # Three mailto links.
    assert html.count("mailto:") == 3
    assert "👍" in html
    assert "👎" in html
    assert "💤" in html


def test_make_insight_id_is_dotted():
    assert fb.make_insight_id(date="2026-04-26", section="business", idx=2) == "2026-04-26.business.2"


def test_infer_section_from_id_handles_dotted_format():
    assert fb.infer_section_from_id("2026-04-26.system.1") == "system"
    assert fb.infer_section_from_id("nondotted") is None


def test_infer_section_from_id_unknown_section_returns_none():
    assert fb.infer_section_from_id("2026.foobar.1") is None


# ─────────────────────────────────────────────────────────────────────
# load_feedback_summary
# ─────────────────────────────────────────────────────────────────────


def test_load_feedback_summary_empty_when_no_log(tmp_path: Path):
    out = fb.load_feedback_summary(tmp_path / "missing.json")
    assert out == ""


def test_load_feedback_summary_counts_per_section(tmp_path: Path):
    log = tmp_path / "feedback.json"
    now = datetime(2026, 4, 26, 12, 0, tzinfo=UTC)
    fb.save_log([
        fb.FeedbackRecord(insight_id="2026-04-25.business.1", rating="up",
                          received_at=(now - timedelta(days=1)).isoformat(),
                          section="business", raw_subject="…"),
        fb.FeedbackRecord(insight_id="2026-04-25.business.2", rating="down",
                          received_at=(now - timedelta(days=1)).isoformat(),
                          section="business", raw_subject="…"),
        fb.FeedbackRecord(insight_id="2026-04-25.system.1", rating="noise",
                          received_at=(now - timedelta(days=2)).isoformat(),
                          section="system", raw_subject="…"),
    ], log)
    summary = fb.load_feedback_summary(log, days=30, now=now)
    assert "business" in summary
    assert "system" in summary
    assert "1 👍" in summary or "1 \U0001f44d" in summary  # business 1 up
    assert "1 👎" in summary or "1 \U0001f44e" in summary  # business 1 down
    assert "1 💤" in summary or "1 \U0001f4a4" in summary  # system 1 noise


def test_load_feedback_summary_excludes_old_records(tmp_path: Path):
    log = tmp_path / "feedback.json"
    now = datetime(2026, 4, 26, 12, 0, tzinfo=UTC)
    fb.save_log([
        fb.FeedbackRecord(
            insight_id="ancient.business.1", rating="up",
            received_at=(now - timedelta(days=120)).isoformat(),
            section="business", raw_subject="…",
        ),
    ], log)
    summary = fb.load_feedback_summary(log, days=30, now=now)
    # No recent records → empty.
    assert summary == ""


def test_load_feedback_summary_lists_recent_negative_ids(tmp_path: Path):
    log = tmp_path / "feedback.json"
    now = datetime(2026, 4, 26, 12, 0, tzinfo=UTC)
    fb.save_log([
        fb.FeedbackRecord(insight_id=f"id-{i}", rating="down",
                          received_at=(now - timedelta(days=i)).isoformat(),
                          section="business", raw_subject="…")
        for i in range(7)
    ], log)
    summary = fb.load_feedback_summary(log, days=30, now=now)
    assert "Recent ids to AVOID" in summary
    # At most 5 ids surfaced.
    assert summary.count("id-") <= 5


# ─────────────────────────────────────────────────────────────────────
# Integration: insights.render_narrative_html with feedback strip
# ─────────────────────────────────────────────────────────────────────


def test_render_narrative_html_includes_feedback_buttons_when_today_label_set():
    from hilmar import insights
    from hilmar.model_router import ModelResponse

    bundle = insights.NarrativeBundle(
        system=ModelResponse(text="- bullet sys-1\n- bullet sys-2",
                             model="m", task_type="system_critique",
                             input_tokens=1, output_tokens=1, cost_cents=1),
        design=ModelResponse(text="- bullet des-1",
                             model="m", task_type="design_suggestions",
                             input_tokens=1, output_tokens=1, cost_cents=1),
        data=ModelResponse(text="- bullet dat-1",
                           model="m", task_type="data_suggestions",
                           input_tokens=1, output_tokens=1, cost_cents=1),
        business=ModelResponse(text="- bullet biz-1",
                               model="m", task_type="business_advice",
                               input_tokens=1, output_tokens=1, cost_cents=1),
    )
    html = insights.render_narrative_html(bundle, today_label="2026-04-26")
    assert "mailto:" in html
    # Insight ids per section/index encoded in mailto subject.
    assert "2026-04-26.system.1" in html
    assert "2026-04-26.business.1" in html


def test_render_narrative_html_without_today_label_has_no_feedback_buttons():
    from hilmar import insights
    from hilmar.model_router import ModelResponse

    resp = ModelResponse(
        text="- bullet", model="m", task_type="system_critique",
        input_tokens=1, output_tokens=1, cost_cents=1,
    )
    bundle = insights.NarrativeBundle(system=resp, design=resp, data=resp, business=resp)
    html = insights.render_narrative_html(bundle)
    assert "mailto:" not in html


# ─── End-to-end feedback loop (PR #29) ──────────────────────────────


def test_e2e_feedback_loop_round_trip(tmp_path: Path):
    """End-to-end: a feedback email comes in via Graph → gets parsed and
    persisted → next narrative call's load_feedback_summary returns
    a string that mentions both the section breakdown and the AVOID list.

    This is the round-trip per memory's "Insights feedback round-trip
    implemented but not verified end-to-end" follow-up.
    """
    log_path = tmp_path / "insights-feedback.json"
    now = datetime(2026, 4, 28, 12, 0, tzinfo=UTC)

    # Stage 1 — Michael clicks 👎 on a business bullet; Graph delivers the
    # email to feedback_ingest.
    received_meta = MessageMeta(
        id="msg-001", conversation_id="conv-1",
        subject="INSIGHT-FEEDBACK 2026-04-28.business.3 👎",
        from_address="michael.deitchman@idealx.us",
        from_name="Michael",
        to_addresses=["michael.deitchman@idealx.us"],
        cc_addresses=[],
        received_at=now - timedelta(hours=1), sent_at=now - timedelta(hours=1),
        internet_message_id="<msg-001@x>",
        is_read=True, has_attachments=False,
    )
    n = fb.ingest_from_graph(
        client=_StubGraph([received_meta]),
        log_path=log_path,
    )
    assert n == 1, "feedback email should ingest"

    # Stage 2 — next-run summary loaded from disk reads it back.
    summary = fb.load_feedback_summary(log_path, days=30, now=now)
    assert "business" in summary, f"section missing: {summary!r}"
    assert "1 👎" in summary, f"down rating missing: {summary!r}"
    assert "2026-04-28.business.3" in summary, "AVOID list missing the id"


def test_e2e_feedback_loop_threads_into_narrative_prompt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Continuation of the round-trip: the summary STRING reaches the
    LLM call's system prompt unchanged. Verifies the contract between
    feedback_ingest.load_feedback_summary and insights.generate_narrative
    that the orchestrator depends on.
    """
    from hilmar import insights as insights_mod
    log_path = tmp_path / "insights-feedback.json"
    now = datetime(2026, 4, 28, 12, 0, tzinfo=UTC)
    fb.save_log(
        [fb.FeedbackRecord(
            insight_id="2026-04-27.business.1", rating="down",
            received_at=(now - timedelta(hours=2)).isoformat(),
            section="business",
        )],
        log_path,
    )
    summary = fb.load_feedback_summary(log_path, days=30, now=now)
    assert summary  # non-empty

    captured: dict = {}

    class FakeRouter:
        def call(self, *, task_type, prompt, system=None, **kwargs):
            captured.setdefault("calls", []).append(
                {"task": task_type, "system": system or ""}
            )

            class _R:
                text = "ok"
                cost_cents = 0
                model = "fake"
                skipped_reason = None
            return _R()

    ctx = insights_mod.InsightsContext(total=1, wins=1)
    insights_mod.generate_narrative(
        ctx, router=FakeRouter(), feedback_summary=summary,
    )
    # Each of the 4 task calls received the feedback summary appended
    # to its system prompt.
    for call in captured["calls"]:
        assert summary in call["system"], (
            f"feedback_summary missing from {call['task']} system prompt"
        )
