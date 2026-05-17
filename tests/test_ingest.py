"""Tests for hilmar.ingest — Graph-backed ingest with idempotent merge.

Mocks ``GraphClient`` (no real Graph HTTP) using a stub that returns a
prebuilt list of ``MessageMeta`` + canned ``MessageBody`` objects.

Key behaviors covered:
  * Bucket classification (lonny_outbound / lonny_reply / mbd_rate_response /
    mbd_inbound).
  * Caren / MBD_Export_Pricing exclusion (memory:
    project_hilmar_rates_scope).
  * MDOLX booking → WIN promotion.
  * Idempotent merge: re-running on same window leaves request count and
    human-edited fields untouched.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from hilmar import ingest
from hilmar.graph_client import MessageBody, MessageMeta

UTC = timezone.utc


# ─────────────────────────────────────────────────────────────────────
# Stub GraphClient
# ─────────────────────────────────────────────────────────────────────

class StubGraphClient:
    """Minimal GraphClient stand-in with the two methods ingest calls."""

    def __init__(self, messages: list[MessageMeta], bodies: dict[str, MessageBody]):
        # Lonny-sender stash + Lonny-recipient stash. We fan out the SAME
        # message list for both searches and let dedup handle overlaps.
        self._messages = messages
        self._bodies = bodies
        self.search_calls: list[dict[str, Any]] = []
        self.body_calls: list[str] = []

    def search_messages(self, **kwargs: Any) -> list[MessageMeta]:
        self.search_calls.append(kwargs)
        sender = kwargs.get("sender")
        recipient = kwargs.get("recipient")
        out: list[MessageMeta] = []
        for m in self._messages:
            from_match = bool(sender) and (m.from_address or "").lower() == sender.lower()
            to_match = bool(recipient) and recipient.lower() in [a.lower() for a in m.to_addresses]
            if from_match or to_match:
                out.append(m)
        return out

    def get_message_body(self, message_id: str) -> MessageBody:
        self.body_calls.append(message_id)
        if message_id not in self._bodies:
            raise KeyError(f"no body stub for {message_id}")
        return self._bodies[message_id]


# ─────────────────────────────────────────────────────────────────────
# Fixture builders
# ─────────────────────────────────────────────────────────────────────

LONNY = "lupfold@hilmaringredients.com"
MICHAEL = "michael.deitchman@ol-usa.com"
MBD_SHARED = "mbd_oceanexportbookingshared@ol-usa.com"
CAREN = "caren.tobel@ol-usa.com"
MBD_PRICING = "mbd_export_pricing@ol-usa.com"


def _meta(
    *,
    msg_id: str,
    subject: str,
    from_addr: str,
    to_addrs: list[str],
    sent: datetime,
    cc_addrs: list[str] | None = None,
    imid: str | None = None,
) -> MessageMeta:
    return MessageMeta(
        id=msg_id,
        conversation_id=f"conv-{msg_id}",
        subject=subject,
        from_address=from_addr,
        from_name=from_addr.split("@")[0],
        to_addresses=to_addrs,
        cc_addresses=cc_addrs or [],
        received_at=sent,
        sent_at=sent,
        internet_message_id=imid or f"<{msg_id}@example.com>",
        is_read=True,
        has_attachments=False,
    )


def _body(
    msg_id: str,
    *,
    subject: str,
    text: str,
    sent: datetime,
    from_addr: str,
) -> MessageBody:
    return MessageBody(
        id=msg_id,
        conversation_id=f"conv-{msg_id}",
        subject=subject,
        body_content_type="text",
        body=text,
        body_preview=text[:200],
        received_at=sent,
        from_address=from_addr,
    )


@pytest.fixture
def cfg() -> ingest.IngestConfig:
    return ingest.IngestConfig(
        lonny_address=LONNY,
        mbd_shared_address=MBD_SHARED,
        sender_address=MICHAEL,
        window_start=datetime(2026, 4, 1, tzinfo=UTC),
        window_end=datetime(2026, 4, 30, tzinfo=UTC),
    )


# ─────────────────────────────────────────────────────────────────────
# Bucket classification
# ─────────────────────────────────────────────────────────────────────


def test_classify_lonny_outbound(cfg: ingest.IngestConfig):
    m = _meta(
        msg_id="m1",
        subject="Oakland to Shanghai",
        from_addr=LONNY,
        to_addrs=[MBD_SHARED],
        sent=datetime(2026, 4, 10, 14, 0, tzinfo=UTC),
    )
    assert ingest.classify_bucket(m, cfg) == "lonny_outbound"


def test_classify_lonny_reply(cfg: ingest.IngestConfig):
    m = _meta(
        msg_id="m2",
        subject="RE: Oakland to Shanghai",
        from_addr=LONNY,
        to_addrs=[MBD_SHARED],
        sent=datetime(2026, 4, 10, 16, 0, tzinfo=UTC),
    )
    assert ingest.classify_bucket(m, cfg) == "lonny_reply"


def test_classify_mbd_rate_response(cfg: ingest.IngestConfig):
    m = _meta(
        msg_id="m3",
        subject="RE: Oakland to Shanghai",
        from_addr=MBD_SHARED,
        to_addrs=[LONNY],
        sent=datetime(2026, 4, 10, 17, 0, tzinfo=UTC),
    )
    assert ingest.classify_bucket(m, cfg) == "mbd_rate_response"


def test_classify_mbd_booking_with_mdolx(cfg: ingest.IngestConfig):
    m = _meta(
        msg_id="m4",
        subject="MDOLX 123456 — Hilmar booking confirmation",
        from_addr=MBD_SHARED,
        to_addrs=[LONNY],
        sent=datetime(2026, 4, 11, 9, 0, tzinfo=UTC),
    )
    assert ingest.classify_bucket(m, cfg) == "mbd_inbound"


def test_caren_excluded(cfg: ingest.IngestConfig):
    m = _meta(
        msg_id="m5",
        subject="HILMAR pricing prep",
        from_addr=CAREN,
        to_addrs=[MICHAEL],
        sent=datetime(2026, 4, 11, 10, 0, tzinfo=UTC),
    )
    assert ingest.classify_bucket(m, cfg) is None


def test_mbd_export_pricing_excluded(cfg: ingest.IngestConfig):
    m = _meta(
        msg_id="m6",
        subject="HILMAR Oakland to Tokyo (10)",
        from_addr=MBD_PRICING,
        to_addrs=[LONNY],
        sent=datetime(2026, 4, 12, 11, 0, tzinfo=UTC),
    )
    assert ingest.classify_bucket(m, cfg) is None


def test_unknown_sender_returns_none(cfg: ingest.IngestConfig):
    m = _meta(
        msg_id="m7",
        subject="Oakland to Hamburg",
        from_addr="random@external.com",
        to_addrs=[MICHAEL],
        sent=datetime(2026, 4, 12, 11, 0, tzinfo=UTC),
    )
    assert ingest.classify_bucket(m, cfg) is None


def test_excluded_overrides_even_if_subject_looks_hilmar(cfg: ingest.IngestConfig):
    # Caren on a HILMAR subject is still excluded — exclusion is by address,
    # not by subject.
    m = _meta(
        msg_id="m8",
        subject="Oakland to Tokyo",
        from_addr=CAREN,
        to_addrs=[LONNY],
        sent=datetime(2026, 4, 12, 11, 0, tzinfo=UTC),
    )
    assert ingest.is_excluded(m) is True


# ─────────────────────────────────────────────────────────────────────
# fetch_window: end-to-end with stub
# ─────────────────────────────────────────────────────────────────────


def test_fetch_window_filters_excluded_and_buckets(cfg: ingest.IngestConfig):
    sent_a = datetime(2026, 4, 10, 14, 0, tzinfo=UTC)
    sent_b = datetime(2026, 4, 10, 17, 0, tzinfo=UTC)
    sent_c = datetime(2026, 4, 11, 9, 0, tzinfo=UTC)

    metas = [
        _meta(msg_id="A", subject="Oakland to Shanghai",
              from_addr=LONNY, to_addrs=[MBD_SHARED], sent=sent_a),
        _meta(msg_id="B", subject="RE: Oakland to Shanghai",
              from_addr=MBD_SHARED, to_addrs=[LONNY], sent=sent_b),
        _meta(msg_id="C", subject="MDOLX 999111 HILMAR booking",
              from_addr=MBD_SHARED, to_addrs=[LONNY], sent=sent_c),
        # Caren — must be filtered.
        _meta(msg_id="D", subject="HILMAR rates prep",
              from_addr=CAREN, to_addrs=[MICHAEL], sent=sent_a),
    ]
    bodies = {
        "A": _body("A", subject=metas[0].subject, text="Need ETA: 4/30/2026",
                   sent=sent_a, from_addr=LONNY),
        "B": _body("B", subject=metas[1].subject,
                   text="MSC OSCAR / 012E\nETD 4/15/2026\nETA 5/15/2026\nOL rate $2400",
                   sent=sent_b, from_addr=MBD_SHARED),
        "C": _body("C", subject=metas[2].subject, text="Booking confirmed.",
                   sent=sent_c, from_addr=MBD_SHARED),
    }
    client = StubGraphClient(metas, bodies)
    rows = ingest.fetch_window(client, cfg)
    buckets = sorted(r["bucket"] for r in rows)
    assert buckets == ["lonny_outbound", "mbd_inbound", "mbd_rate_response"]
    assert len(rows) == 3, "Caren row must have been filtered"


def test_fetch_window_continues_when_recipient_query_fails(cfg: ingest.IngestConfig):
    """Defensive: if Graph rejects the recipient-side query (e.g.
    InefficientFilter on toRecipients/any() + date), fetch_window logs a
    warning and ships the sender-side results rather than aborting the
    whole pipeline. Regression-guards the production failure on
    rate-blaster-v2 2026-04-26 where /me/messages 400'd."""
    sent = datetime(2026, 4, 10, 14, 0, tzinfo=UTC)

    class _RecipientFails(StubGraphClient):
        def search_messages(self, **kwargs):
            if kwargs.get("recipient"):
                raise RuntimeError("Graph 400 InefficientFilter")
            return super().search_messages(**kwargs)

    metas = [
        _meta(msg_id="A", subject="Oakland to Shanghai",
              from_addr=LONNY, to_addrs=[MBD_SHARED], sent=sent),
    ]
    bodies = {
        "A": _body("A", subject=metas[0].subject, text="Need ETA: 4/30/2026",
                   sent=sent, from_addr=LONNY),
    }
    client = _RecipientFails(metas, bodies)
    rows = ingest.fetch_window(client, cfg)
    # Sender query still landed → we get the one Lonny outbound row.
    assert len(rows) == 1
    assert rows[0]["bucket"] == "lonny_outbound"


def test_fetch_window_aborts_when_sender_query_fails(cfg: ingest.IngestConfig):
    """The mirror case: sender query failure IS fatal — that's the load-
    bearing source of Lonny's outbound rate requests. Re-raise."""

    class _SenderFails(StubGraphClient):
        def search_messages(self, **kwargs):
            if kwargs.get("sender"):
                raise RuntimeError("Graph 503 Service Unavailable")
            return []

    client = _SenderFails([], {})
    with pytest.raises(RuntimeError, match="Service Unavailable"):
        ingest.fetch_window(client, cfg)


# ─────────────────────────────────────────────────────────────────────
# run_ingest: full pipeline
# ─────────────────────────────────────────────────────────────────────


def _scenario_with_one_win() -> tuple[StubGraphClient, datetime, datetime]:
    """Lonny request → MBD quote → MDOLX booking → expect 1 WIN."""
    sent_req = datetime(2026, 4, 10, 14, 0, tzinfo=UTC)
    sent_quote = datetime(2026, 4, 10, 17, 0, tzinfo=UTC)
    sent_book = datetime(2026, 4, 11, 9, 0, tzinfo=UTC)

    metas = [
        _meta(msg_id="REQ", subject="Oakland to Shanghai",
              from_addr=LONNY, to_addrs=[MBD_SHARED], sent=sent_req),
        _meta(msg_id="QUO", subject="RE: Oakland to Shanghai",
              from_addr=MBD_SHARED, to_addrs=[LONNY], sent=sent_quote),
        _meta(msg_id="BOK", subject="MDOLX 123456 HILMAR Oakland to Shanghai",
              from_addr=MBD_SHARED, to_addrs=[LONNY], sent=sent_book),
    ]
    bodies = {
        "REQ": _body("REQ", subject=metas[0].subject,
                     text="1-40' HC Reefer; need by 5/15/2026",
                     sent=sent_req, from_addr=LONNY),
        "QUO": _body("QUO", subject=metas[1].subject,
                     text="Carrier: MSC\nOL rate: $2400\nETD: 4/15/2026\nETA: 5/15/2026",
                     sent=sent_quote, from_addr=MBD_SHARED),
        "BOK": _body("BOK", subject=metas[2].subject,
                     text="Booking confirmation MDOLX 123456",
                     sent=sent_book, from_addr=MBD_SHARED),
    }
    return (
        StubGraphClient(metas, bodies),
        datetime(2026, 4, 1, tzinfo=UTC),
        datetime(2026, 4, 30, tzinfo=UTC),
    )


def test_run_ingest_creates_tracking_data(tmp_path: Path):
    client, start, end = _scenario_with_one_win()
    data_path = tmp_path / "tracking-data-v2.json"
    out = ingest.run_ingest(
        client=client, data_path=data_path,
        window_start=start, window_end=end,
    )
    assert data_path.exists()
    on_disk = json.loads(data_path.read_text(encoding="utf-8"))
    assert on_disk == out
    assert len(out["requests"]) == 1, f"expected 1 request, got {out['requests']}"
    assert out["requests"][0]["status"] == "WIN"
    assert out["requests"][0]["mdolx_ref"] == "123456"


def test_run_ingest_is_idempotent(tmp_path: Path):
    """Running twice on the same window must not duplicate requests."""
    client, start, end = _scenario_with_one_win()
    data_path = tmp_path / "tracking-data-v2.json"

    ingest.run_ingest(
        client=client, data_path=data_path,
        window_start=start, window_end=end,
    )
    n1 = len(json.loads(data_path.read_text(encoding="utf-8"))["requests"])

    # Fresh client — same scenario.
    client2, _, _ = _scenario_with_one_win()
    ingest.run_ingest(
        client=client2, data_path=data_path,
        window_start=start, window_end=end,
    )
    n2 = len(json.loads(data_path.read_text(encoding="utf-8"))["requests"])
    assert n1 == n2 == 1, f"non-idempotent: first={n1} second={n2}"


def test_run_ingest_preserves_human_edits_on_existing_fields(tmp_path: Path):
    """After ingest, a human-set field (e.g. notes) must survive a re-ingest."""
    client, start, end = _scenario_with_one_win()
    data_path = tmp_path / "tracking-data-v2.json"
    ingest.run_ingest(
        client=client, data_path=data_path,
        window_start=start, window_end=end,
    )

    # Simulate Michael adding a manual annotation to the request.
    doc = json.loads(data_path.read_text(encoding="utf-8"))
    doc["requests"][0]["michael_note"] = "Customer happy — keep MSC."
    data_path.write_text(json.dumps(doc), encoding="utf-8")

    # Re-run ingest.
    client2, _, _ = _scenario_with_one_win()
    ingest.run_ingest(
        client=client2, data_path=data_path,
        window_start=start, window_end=end,
    )
    final = json.loads(data_path.read_text(encoding="utf-8"))
    assert final["requests"][0].get("michael_note") == "Customer happy — keep MSC."


def test_merge_idempotent_fills_none_fields_only():
    """ingest-derived fields recompute every run; non-ingest keys (notes /
    annotations / human-added) survive."""
    existing = [{
        "request_id": "R1",
        "carrier_quoted": None,        # was None on disk; fresh fills it
        "michael_note": "VIP",         # not in fresh — must persist
        "carrier_won": "OldCarrier",   # ingest-derived → fresh wins
    }]
    fresh = [{
        "request_id": "R1",
        "carrier_quoted": "MSC",       # SHOULD fill the None
        "carrier_won": "MSC",          # recomputed — overrides existing
        "status": "WIN",               # recomputed → fresh wins
    }]
    out = ingest.merge_idempotent(existing, fresh)
    assert len(out) == 1
    r = out[0]
    assert r["carrier_quoted"] == "MSC", "None field not filled"
    assert r["carrier_won"] == "MSC", "recomputed carrier_won not refreshed"
    assert r["status"] == "WIN", "recomputed status not refreshed"
    assert r["michael_note"] == "VIP", "non-fresh human key dropped"


def test_merge_idempotent_appends_new_request():
    existing = [{"request_id": "R1", "status": "WIN"}]
    fresh = [
        {"request_id": "R1", "status": "WIN"},
        {"request_id": "R2", "status": "PENDING"},
    ]
    out = ingest.merge_idempotent(existing, fresh)
    ids = sorted(r["request_id"] for r in out)
    assert ids == ["R1", "R2"]


def test_merge_idempotent_preserves_existing_not_in_fresh():
    """An older request that's outside today's window must not be dropped."""
    existing = [
        {"request_id": "OLD", "status": "WIN"},
        {"request_id": "NEW", "status": "PENDING"},
    ]
    fresh = [{"request_id": "NEW", "status": "WIN"}]  # only the new one's window
    out = ingest.merge_idempotent(existing, fresh)
    ids = sorted(r["request_id"] for r in out)
    assert ids == ["NEW", "OLD"], "old request dropped from history"


def test_merge_idempotent_promotes_stale_quoted_when_rate_response_arrives_late():
    """Production bug 2026-04-27: 26 rows had quoted=False on disk after a
    rate-response email arrived AFTER the Lonny-outbound row was already
    persisted (split across two daily runs). The fresh in-memory row had
    quoted=True / carrier_quoted=MSC / ol_rate=540 / "Rate responded by
    MBD ..." reason_detail, but the merge preserved the stale Day-N-1
    quoted=False because `quoted` wasn't in _RECOMPUTED_FIELDS. Then
    qc.phase_3_entries' NQ-contamination cleanup fired (status==NQ AND
    !quoted) and CLEARED carrier_quoted to None.

    With Bug 1 fix: rate-response fields are recomputed every merge, so
    the fresh quoted=True wins. (Status enum updated to four-state per
    2026-04-27 cutover: LOSS→NQ for the silent Day-N-1 verdict.)
    """
    existing = [{
        "request_id": "STALE",
        "status": "NQ",                   # Day-N-1 verdict (was LOSS pre-cutover)
        "quoted": False,                  # <-- this is the lie we need to clobber
        "response_timestamp": None,
        "carrier_quoted": None,
        "ol_rate": None,
        "loss_reason": "NO_RESPONSE",
        "reason_detail": "OL-USA never responded with a quote",
        "michael_note": "watch this lane",  # human edit — must survive
    }]
    fresh = [{
        "request_id": "STALE",
        "status": "PENDING",              # decide_status sees quoted+response → PENDING
        "quoted": True,
        "response_timestamp": "2026-04-13T18:32:00+00:00",
        "carrier_quoted": "MSC",
        "ol_rate": 540,
        "loss_reason": None,
        "reason_detail": "Rate responded by MBD 2026-04-13 — MSC @ $540 ETD ?",
    }]
    out = ingest.merge_idempotent(existing, fresh)
    assert len(out) == 1
    r = out[0]
    # Rate-response fields recompute — fresh wins.
    assert r["quoted"] is True, "quoted=True from fresh must clobber stale False"
    assert r["carrier_quoted"] == "MSC"
    assert r["ol_rate"] == 540
    assert r["response_timestamp"].startswith("2026-04-13")
    assert r["status"] == "PENDING"
    assert r["loss_reason"] is None
    assert "Rate responded by MBD" in r["reason_detail"]
    # Human edit not in fresh → preserved.
    assert r["michael_note"] == "watch this lane"


def test_merge_idempotent_promotes_stale_has_send_when_book_arrives_late():
    """Same staleness pattern but for the WIN side. Day-N-1 the row was
    Q&L (quoted with no booking yet). Day-N the booking arrived;
    apply_send_signals / link_bookings_to_requests set has_send=True /
    mdolx_ref=N. Without has_send + mdolx_ref + carrier_won in
    _RECOMPUTED_FIELDS, the stale has_send=False and mdolx_ref=None
    would persist and the row would stay Q&L forever.
    (Status enum updated to four-state per 2026-04-27 cutover:
    LOSS+quoted→Q&L for the Day-N-1 verdict.)
    """
    existing = [{
        "request_id": "BOOKED",
        "status": "Q&L",
        "quoted": True,
        "response_timestamp": "2026-04-13T18:00:00+00:00",
        "carrier_quoted": "MSC",
        "ol_rate": 540,
        "has_send": False,
        "mdolx_ref": None,
        "carrier_won": None,
    }]
    fresh = [{
        "request_id": "BOOKED",
        "status": "WIN",
        "quoted": True,
        "response_timestamp": "2026-04-13T18:00:00+00:00",
        "carrier_quoted": "MSC",
        "ol_rate": 540,
        "has_send": True,
        "mdolx_ref": "MDOLX1234567",
        "carrier_won": "MSC",
        "booking_timestamp": "2026-04-15T14:22:00+00:00",
    }]
    out = ingest.merge_idempotent(existing, fresh)
    r = out[0]
    assert r["status"] == "WIN"
    assert r["has_send"] is True
    assert r["mdolx_ref"] == "MDOLX1234567"
    assert r["carrier_won"] == "MSC"
    assert r["booking_timestamp"].startswith("2026-04-15")


# ─────────────────────────────────────────────────────────────────────
# M3.4 hardening gates (per Michael's follow-up 2026-04-26)
# ─────────────────────────────────────────────────────────────────────


def test_no_direct_status_string_assignment_in_module():
    """Gate 3 — status flows ONLY through core.decide_status.

    Greps the ingest.py source for ``["status"] = "..."`` literals. There
    must be exactly zero — every status change has to call decide_status
    (via :func:`finalize_status`) which uses :func:`core.record_transition`.
    The variable assignment inside record_transition (``request["status"]
    = new_status``) does NOT match this pattern because the value is a
    variable, not a string literal.
    """
    src = Path(ingest.__file__).read_text(encoding="utf-8")
    pattern = re.compile(r'\["status"\]\s*=\s*"')
    matches = pattern.findall(src)
    assert not matches, (
        f"Direct status literal assignment found in ingest.py: {matches!r}. "
        "All status changes must route through core.decide_status() / "
        "core.record_transition()."
    )


def test_byte_identical_idempotent_rerun(tmp_path: Path):
    """Gate 1(b) — second run on same input produces byte-identical file.

    The ``generated_at`` timestamp would naturally change run-to-run, so
    we pin ``now`` to a fixed value via the kwarg the public API exposes.
    """
    client1, start, end = _scenario_with_one_win()
    data_path = tmp_path / "tracking-data-v2.json"
    pinned_now = datetime(2026, 4, 26, 12, 0, tzinfo=UTC)

    ingest.run_ingest(
        client=client1, data_path=data_path,
        window_start=start, window_end=end, now=pinned_now,
    )
    bytes_first = data_path.read_bytes()

    client2, _, _ = _scenario_with_one_win()
    ingest.run_ingest(
        client=client2, data_path=data_path,
        window_start=start, window_end=end, now=pinned_now,
    )
    bytes_second = data_path.read_bytes()

    assert bytes_first == bytes_second, (
        "Second ingest run produced different bytes. "
        f"Sizes: {len(bytes_first)} vs {len(bytes_second)}"
    )


def test_partial_update_does_not_clobber_populated_fields(tmp_path: Path):
    """Gate 1(c) — non-ingest-derived fields must survive a re-ingest.
    Ingest-extracted fields (status, ol_rate, carrier_*, etc.) are
    recomputed every run by design — that's the Bug 1 fix. The "human
    edit survives" contract therefore lives on FIELDS THE INGEST
    DOES NOT WRITE — like ``michael_note``."""
    # First run — produces the win with rate $2400 and carrier MSC.
    client1, start, end = _scenario_with_one_win()
    data_path = tmp_path / "tracking-data-v2.json"
    ingest.run_ingest(
        client=client1, data_path=data_path,
        window_start=start, window_end=end,
    )

    # Add a non-ingest annotation. michael_note is something only a human
    # writes; ingest never produces it. THAT contract is what survives.
    doc = json.loads(data_path.read_text(encoding="utf-8"))
    doc["requests"][0]["michael_note"] = "Rate negotiated down by phone"
    doc["requests"][0]["michael_priority"] = "high"
    data_path.write_text(json.dumps(doc), encoding="utf-8")

    # Re-run — fresh ingest emits ol_rate=2400 again from the same email.
    # ol_rate is now in _RECOMPUTED_FIELDS so it overwrites; that is
    # CORRECT behavior post Bug 1 — the source of truth for ol_rate is
    # the latest MBD email, not a stale value on disk. If Michael wants
    # to track a "negotiated_rate" different from the quoted one, that's
    # a separate field he can add — and being non-ingest, IT survives.
    client2, _, _ = _scenario_with_one_win()
    ingest.run_ingest(
        client=client2, data_path=data_path,
        window_start=start, window_end=end,
    )

    after = json.loads(data_path.read_text(encoding="utf-8"))
    final = after["requests"][0]
    # Non-ingest annotations preserved across re-merge.
    assert final["michael_note"] == "Rate negotiated down by phone"
    assert final["michael_priority"] == "high"
    # ol_rate now reflects the latest MBD email (recomputed) — this is
    # the post-Bug-1 contract.
    assert final["ol_rate"] == 2400, (
        "post-Bug-1 ol_rate must be recomputed from the live email, "
        f"got {final['ol_rate']!r}"
    )
    # Status flows through decide_status — for the stub-driven WIN
    # scenario above, that's still WIN after re-ingest.
    assert final["status"] == "WIN"


def test_excluded_traffic_never_lands_in_requests(tmp_path: Path):
    """Gate 5 — Caren / MBD_Export_Pricing emails do NOT produce request rows.

    Builds a scenario where Caren sends a HILMAR-flavored email AND
    MBD_Export_Pricing sends an Oakland-to-Tokyo blast. Real Lonny request
    is also present so the pipeline has something legitimate to process.
    Asserts neither excluded address bleeds through.

    Regression-guards HANDOFF.md constraint #9.
    """
    sent = datetime(2026, 4, 10, 14, 0, tzinfo=UTC)
    metas = [
        # Real Lonny request — should land.
        _meta(msg_id="REAL", subject="Oakland to Hamburg",
              from_addr=LONNY, to_addrs=[MBD_SHARED], sent=sent),
        # Caren impersonates Hilmar lane traffic — must be filtered.
        _meta(msg_id="CAREN1", subject="Oakland to Tokyo HILMAR",
              from_addr=CAREN, to_addrs=[LONNY], sent=sent),
        _meta(msg_id="CAREN2", subject="MDOLX 999888 HILMAR booking",
              from_addr=CAREN, to_addrs=[LONNY], sent=sent),
        # MBD_Export_Pricing rates-desk noise — must be filtered.
        _meta(msg_id="PRICING1", subject="HILMAR Q2 rate sheet",
              from_addr=MBD_PRICING, to_addrs=[LONNY], sent=sent),
        # Even with HILMAR subject, exclusion is by address.
        _meta(msg_id="PRICING2", subject="MDOLX 777666 HILMAR booking",
              from_addr=MBD_PRICING, to_addrs=[LONNY], sent=sent),
    ]
    bodies = {
        "REAL": _body("REAL", subject=metas[0].subject,
                      text="1-40' HC standard need.", sent=sent, from_addr=LONNY),
    }
    client = StubGraphClient(metas, bodies)
    data_path = tmp_path / "tracking-data-v2.json"
    out = ingest.run_ingest(
        client=client, data_path=data_path,
        window_start=datetime(2026, 4, 1, tzinfo=UTC),
        window_end=datetime(2026, 4, 30, tzinfo=UTC),
    )

    # Only the REAL Lonny request lands as a request.
    requests = out["requests"]
    assert len(requests) == 1, (
        f"expected exactly 1 (REAL) request, got {len(requests)}: "
        f"{[r['request_id'] for r in requests]}"
    )
    assert requests[0]["destination"] == "Hamburg"
    # The Caren MDOLX (999888) and pricing MDOLX (777666) MUST NOT have
    # become standalone wins.
    all_mdolx_seen = {r.get("mdolx_ref") for r in requests}
    assert "999888" not in all_mdolx_seen, "Caren MDOLX leaked through"
    assert "777666" not in all_mdolx_seen, "MBD_Export_Pricing MDOLX leaked through"


def test_request_id_stable_across_runs(tmp_path: Path):
    """Gate 1(a) extension — the same request_id is generated for the same
    Lonny outbound on a re-run. Idempotency depends on this; if the id
    drifted between runs we'd duplicate every Lonny ask.
    """
    client1, start, end = _scenario_with_one_win()
    data_path = tmp_path / "tracking-data-v2.json"
    out1 = ingest.run_ingest(
        client=client1, data_path=data_path,
        window_start=start, window_end=end,
    )
    rid1 = out1["requests"][0]["request_id"]

    # Re-run from scratch on a fresh data file (no merge).
    data_path.unlink()
    client2, _, _ = _scenario_with_one_win()
    out2 = ingest.run_ingest(
        client=client2, data_path=data_path,
        window_start=start, window_end=end,
    )
    rid2 = out2["requests"][0]["request_id"]
    assert rid1 == rid2, f"request_id drift: {rid1!r} vs {rid2!r}"


def test_dry_diff_does_not_write(tmp_path: Path):
    """Gate 4 — run_ingest_dry_diff must NEVER touch the data file."""
    client, start, end = _scenario_with_one_win()
    data_path = tmp_path / "tracking-data-v2.json"
    # Pre-seed with a known-empty document so we can detect any write.
    seed = {"requests": [], "summary": {}}
    data_path.write_text(json.dumps(seed), encoding="utf-8")
    seed_bytes = data_path.read_bytes()

    diff = ingest.run_ingest_dry_diff(
        client=client, data_path=data_path,
        window_start=start, window_end=end,
    )

    after_bytes = data_path.read_bytes()
    assert seed_bytes == after_bytes, "dry-diff wrote to disk"
    # The diff says "would add 1 request".
    assert len(diff.requests_added) == 1
    assert not diff.is_empty()


def test_dry_diff_against_existing_doc_only_lists_changes(tmp_path: Path):
    """Dry-diff distinguishes added / changed / unchanged."""
    client1, start, end = _scenario_with_one_win()
    data_path = tmp_path / "tracking-data-v2.json"
    ingest.run_ingest(
        client=client1, data_path=data_path,
        window_start=start, window_end=end,
    )

    # Same scenario re-run as a dry-diff — no adds, ideally no changes.
    client2, _, _ = _scenario_with_one_win()
    diff = ingest.run_ingest_dry_diff(
        client=client2, data_path=data_path,
        window_start=start, window_end=end,
    )
    assert diff.requests_added == [], "dry-diff invented new requests"
    # ``generated_at`` lives at the doc level, not per-request, so per-request
    # changes should be empty when the underlying scenario is unchanged.
    assert diff.requests_changed == [], (
        f"dry-diff thinks fields changed when nothing did: {diff.requests_changed!r}"
    )
    assert diff.requests_unchanged == 1


# ─────────────────────────────────────────────────────────────────────
# Bug 3 secondary — carrier extraction on standalone bookings
# ─────────────────────────────────────────────────────────────────────

def test_collect_bookings_extracts_carrier_from_subject():
    """Bug 3 secondary: ``collect_bookings`` must guess carrier from the
    booking subject so standalone-booking rows don't trigger QC-002
    (WIN with no carrier_won) when the rate response email isn't in the
    window. Uses ``body_parser._find_carrier`` token list."""
    rows = [{
        "bucket": "mbd_inbound",
        "subject": "MDOLX260460 // HILMAR -> SHANGHAI // MSC OSCAR booking confirmed",
        "summary_preview": "Booking confirmed with MSC, ETD 2026-04-29.",
        "is_hilmar": True,
        "mdolx": "260460",
        "sent": "2026-04-25T12:00:00+00:00",
        "imid": "imid-1",
        "id": "id-1",
    }]
    bookings = ingest.collect_bookings(rows)
    assert "260460" in bookings
    assert bookings["260460"]["carrier"] == "MSC"


def test_collect_bookings_carrier_none_when_no_token_match():
    """When the booking subject/preview doesn't mention any known carrier
    token, ``carrier`` is None — QC-002 will still flag it (correctly)."""
    rows = [{
        "bucket": "mbd_inbound",
        "subject": "MDOLX260999 // HILMAR -> SHANGHAI // booking confirmed",
        "summary_preview": "Booking confirmed.",
        "is_hilmar": True,
        "mdolx": "260999",
        "sent": "2026-04-25T12:00:00+00:00",
        "imid": "imid-1",
        "id": "id-1",
    }]
    bookings = ingest.collect_bookings(rows)
    assert bookings["260999"]["carrier"] is None


def test_link_bookings_falls_back_to_booking_carrier_when_quoted_missing():
    """When the rate response hasn't been parsed yet, the matched WIN row
    used to get carrier_won=None because ``best.carrier_quoted`` was not
    set. Post Bug 3 secondary fix: fall back to booking-extracted carrier."""
    requests = [{
        "request_id": "req-1",
        "destination": "Shanghai",
        "request_timestamp": "2026-04-20T15:00:00+00:00",
        "carrier_quoted": None,        # rate response not yet ingested
        "mdolx_refs_all": [],
    }]
    bookings = {
        "260460": {
            "mdolx": "260460",
            "subject": "MDOLX260460 // HILMAR -> SHANGHAI // MSC booking",
            "sent": "2026-04-25T12:00:00+00:00",
            "carrier": "MSC",          # extracted by collect_bookings
            "source_bucket": "mbd_inbound",
            "source_imid": "imid-1",
            "source_id": "id-1",
        },
    }
    out_reqs, standalones = ingest.link_bookings_to_requests(requests, bookings)
    assert len(out_reqs) == 1
    assert out_reqs[0]["mdolx_ref"] == "260460"
    assert out_reqs[0]["carrier_won"] == "MSC"
    assert standalones == []


def test_matched_booking_backfills_ol_rate_from_body_when_missing():
    """LOSS-then-WIN-via-MDOLX promoted case: the matched request was
    promoted to WIN by the booking confirmation but never had a
    rate-quote email body parsed (ol_rate=None). Same fix as the
    standalone path — fetch the booking body and pull the rate.
    """
    from datetime import datetime, timezone

    from hilmar.graph_client import MessageBody

    class _StubClient:
        def __init__(self):
            self.calls = []

        def get_message_body(self, message_id):
            self.calls.append(message_id)
            return MessageBody(
                id=message_id, conversation_id="cv",
                subject="MDOLX260317_BOOKING CONFIRMATION// HILMAR",
                body_content_type="text",
                body="Carrier: ONE\nRate: $4,250.00 per 40' RF",
                body_preview="", received_at=datetime.now(timezone.utc),
                from_address="ops@ol-usa.com",
            )

    requests = [{
        "request_id": "req_promoted",
        "destination": "Durban",
        "request_timestamp": "2026-04-15T20:07:22+00:00",
        "carrier_quoted": None,  # never quoted via email
        "ol_rate": None,         # the rate hole this PR closes
        "mdolx_refs_all": [],
    }]
    bookings = {
        "260317": {
            "mdolx": "260317",
            "subject": "MDOLX260317 // HILMAR -> DURBAN // ONE booking",
            "sent": "2026-04-20T21:36:11+00:00",
            "carrier": "ONE",
            "source_bucket": "mbd_inbound",
            "source_imid": "imid-X",
            "source_id": "graph-msg-promoted",
        },
    }
    client = _StubClient()
    out_reqs, _ = ingest.link_bookings_to_requests(requests, bookings, client=client)
    assert out_reqs[0]["ol_rate"] == 4250.0
    assert "graph-msg-promoted" in client.calls


def test_matched_booking_no_fetch_when_ol_rate_already_set():
    """If the rate-response email already populated ol_rate, don't
    waste a Graph call — the existing value wins."""
    from datetime import datetime, timezone

    from hilmar.graph_client import MessageBody

    class _StubClient:
        def __init__(self):
            self.calls = []

        def get_message_body(self, message_id):
            self.calls.append(message_id)
            return MessageBody(
                id=message_id, conversation_id="cv", subject="",
                body_content_type="text", body="Rate: $9,999",
                body_preview="", received_at=datetime.now(timezone.utc),
                from_address=None,
            )

    requests = [{
        "request_id": "req_normal",
        "destination": "Shanghai",
        "request_timestamp": "2026-04-20T15:00:00+00:00",
        "carrier_quoted": "MSC",
        "ol_rate": 540.0,  # already extracted from quote email
        "mdolx_refs_all": [],
    }]
    bookings = {
        "260460": {
            "mdolx": "260460",
            "subject": "MDOLX260460 // HILMAR -> SHANGHAI // MSC booking",
            "sent": "2026-04-25T12:00:00+00:00",
            "carrier": "MSC",
            "source_bucket": "mbd_inbound",
            "source_imid": "imid-1",
            "source_id": "graph-msg-1",
        },
    }
    client = _StubClient()
    out_reqs, _ = ingest.link_bookings_to_requests(requests, bookings, client=client)
    assert out_reqs[0]["ol_rate"] == 540.0  # untouched
    # No call should have been made for this row's booking
    assert client.calls == []


def test_standalone_booking_inherits_carrier_from_bk_carrier():
    """Standalone booking row (no Lonny ask in window) must carry
    carrier_quoted + carrier_won populated from the booking's parsed
    carrier so QC-002 doesn't fire on the synthetic WIN."""
    bookings = {
        "260999": {
            "mdolx": "260999",
            "subject": "MDOLX260999 // HILMAR -> Shanghai // ZIM booking",
            "sent": "2026-04-25T12:00:00+00:00",
            "carrier": "ZIM",
            "source_bucket": "mbd_inbound",
            "source_imid": "imid-1",
            "source_id": "id-1",
        },
    }
    # Empty requests list → forces standalone path.
    _, standalones = ingest.link_bookings_to_requests([], bookings)
    assert len(standalones) == 1
    s = standalones[0]
    assert s["request_id"] == "stand_260999"
    assert s["carrier_quoted"] == "ZIM"
    assert s["carrier_won"] == "ZIM"


def test_standalone_booking_recovers_containers_and_teu_from_subject():
    """Subject "…HILMAR 1X20'DV Oakland to Bangkok…" must yield
    containers="1X20'DV", container_count=1, teu_won=teu_requested=1.

    Without this, standalone WINs land with teu_won=0 and contribute
    nothing to the trade-region TEU/value-won columns — the bug
    Michael flagged 2026-04-29.
    """
    bookings = {
        "260420": {
            "mdolx": "260420",
            "subject": "MDOLX260420_UPDATED ETA BOOKING CONFIRMATION// HILMAR 1X20'DV Oakland to Bangkok// ONE: RICGE7217600",
            "sent": "2026-04-16T21:33:49+00:00",
            "carrier": "ONE",
            "source_bucket": "mbd_inbound",
            "source_imid": "imid-2",
            "source_id": "id-2",
        },
    }
    _, standalones = ingest.link_bookings_to_requests([], bookings)
    s = standalones[0]
    assert s["containers"] is not None and "20" in s["containers"]
    assert s["container_count"] == 1
    assert s["teu_requested"] == 1
    assert s["teu_won"] == 1


def test_standalone_booking_reefer_4x40():
    """4X40'RF must yield container_count=4, teu=8."""
    bookings = {
        "260460": {
            "mdolx": "260460",
            "subject": "Re: MDOLX260460_BOOKING CONFIRMATION// HILMAR 4X40'RF Oakland to Tokyo// CMA: NAM8400958",
            "sent": "2026-04-20T16:15:44+00:00",
            "carrier": "CMA CGM",
            "source_bucket": "mbd_inbound",
            "source_imid": "imid-3",
            "source_id": "id-3",
        },
    }
    _, standalones = ingest.link_bookings_to_requests([], bookings)
    s = standalones[0]
    assert s["container_count"] == 4
    assert s["teu_won"] == 8


def test_standalone_booking_fetches_rate_from_body():
    """Pure-MDOLX WINs no longer leave ol_rate=None. When a GraphClient
    is wired in, link_bookings_to_requests fetches the booking-
    confirmation body and runs the rate parser. Per Michael 2026-04-29
    'OL RATE HOLES YES FIX' — backfill the missing rates from the
    confirmation email body.
    """
    class _StubClient:
        def __init__(self, body):
            self._body = body
            self.calls = []

        def get_message_body(self, message_id):
            from datetime import datetime, timezone

            from hilmar.graph_client import MessageBody
            self.calls.append(message_id)
            return MessageBody(
                id=message_id, conversation_id="cv",
                subject="MDOLX260420_BOOKING CONFIRMATION// HILMAR 1X20'DV Oakland to Bangkok",
                body_content_type="text", body=self._body,
                body_preview=self._body[:200],
                received_at=datetime.now(timezone.utc),
                from_address="ops@ol-usa.com",
            )

    body = (
        "Booking confirmed.\n"
        "Carrier: ONE\nRate: $1,950.00 per 20'DV\n"
        "ETD Oakland 2026-05-15  ETA Bangkok 2026-06-08\n"
    )
    client = _StubClient(body)
    bookings = {
        "260420": {
            "mdolx": "260420",
            "subject": "MDOLX260420_BOOKING CONFIRMATION// HILMAR 1X20'DV Oakland to Bangkok// ONE: RICGE7217600",
            "sent": "2026-04-16T21:33:49+00:00",
            "carrier": "ONE",
            "source_bucket": "mbd_inbound",
            "source_imid": "imid-2",
            "source_id": "graph-msg-260420",
        },
    }
    _, standalones = ingest.link_bookings_to_requests([], bookings, client=client)
    assert standalones[0]["ol_rate"] == 1950.0
    assert client.calls == ["graph-msg-260420"]


def test_standalone_booking_no_client_leaves_rate_none():
    """No GraphClient (test/CI path) → ol_rate stays None, no crash."""
    bookings = {
        "260999": {
            "mdolx": "260999",
            "subject": "MDOLX260999 // HILMAR 1X20'DV -> Shanghai",
            "sent": "2026-04-25T12:00:00+00:00",
            "carrier": "ZIM",
            "source_bucket": "mbd_inbound",
            "source_imid": "imid-1",
            "source_id": "graph-msg-1",
        },
    }
    _, standalones = ingest.link_bookings_to_requests([], bookings, client=None)
    assert standalones[0]["ol_rate"] is None


def test_standalone_booking_body_fetch_error_is_swallowed():
    """A Graph fetch error shouldn't abort ingest — log and proceed."""
    class _BoomClient:
        def get_message_body(self, message_id):
            raise RuntimeError("Graph 500")

    bookings = {
        "260999": {
            "mdolx": "260999",
            "subject": "MDOLX260999 // HILMAR 1X20'DV -> Shanghai",
            "sent": "2026-04-25T12:00:00+00:00",
            "carrier": "ZIM",
            "source_bucket": "mbd_inbound",
            "source_imid": "imid-1",
            "source_id": "graph-msg-1",
        },
    }
    _, standalones = ingest.link_bookings_to_requests([], bookings, client=_BoomClient())
    assert standalones[0]["ol_rate"] is None  # graceful


def test_backfill_standalone_rates_heals_existing_rows():
    """Persisted standalone WINs missing any booking-body field get a
    backfill pass on each ingest run — capped at max_calls so a backlog
    can't burn the Graph quota. Gate skips only when all four fields
    (ol_rate, eta_offered, vessel_voyage, transshipment) are already
    populated."""
    class _StubClient:
        def __init__(self, body_for):
            self._body_for = body_for
            self.calls = []

        def get_message_body(self, message_id):
            from datetime import datetime, timezone

            from hilmar.graph_client import MessageBody
            self.calls.append(message_id)
            body = self._body_for.get(message_id, "")
            return MessageBody(
                id=message_id, conversation_id="cv", subject="",
                body_content_type="text", body=body, body_preview=body[:200],
                received_at=datetime.now(timezone.utc), from_address=None,
            )

    client = _StubClient({
        "g-460": "Carrier: CMA CGM\nRate: $4,200 per 40' RF",
        "g-420": "Carrier: ONE\nRate: $1,950 per 20' DV",
    })
    rows = [
        {"request_id": "stand_260460", "status": "WIN", "ol_rate": None,
         "source_ids": ["g-460"]},
        {"request_id": "stand_260420", "status": "WIN", "ol_rate": None,
         "source_ids": ["g-420"]},
        # All four booking-body fields already populated → skipped
        {"request_id": "stand_260999", "status": "WIN", "ol_rate": 999.0,
         "eta_offered": "2026-05-15", "vessel_voyage": "EVER GIVEN / V.123W",
         "transshipment": "Direct", "source_ids": ["g-skip"]},
        # Not a standalone → skipped
        {"request_id": "req_abc", "status": "WIN", "ol_rate": None,
         "source_ids": ["g-not-standalone"]},
    ]
    healed = ingest.backfill_standalone_rates(rows, client)
    assert healed == 2
    assert rows[0]["ol_rate"] == 4200.0
    assert rows[1]["ol_rate"] == 1950.0
    assert rows[2]["ol_rate"] == 999.0  # untouched
    assert rows[3]["ol_rate"] is None    # untouched
    assert "g-skip" not in client.calls
    assert "g-not-standalone" not in client.calls


def test_backfill_standalone_rates_respects_max_calls():
    """A 100-row backlog must not burn 100 Graph calls — cap and let
    the rest wait for the next run."""
    class _StubClient:
        def __init__(self):
            self.calls = []

        def get_message_body(self, message_id):
            from datetime import datetime, timezone

            from hilmar.graph_client import MessageBody
            self.calls.append(message_id)
            return MessageBody(
                id=message_id, conversation_id="cv", subject="",
                body_content_type="text", body="Rate: $1,000.00",
                body_preview="", received_at=datetime.now(timezone.utc),
                from_address=None,
            )

    rows = [
        {"request_id": f"stand_{i:06d}", "status": "WIN", "ol_rate": None,
         "source_ids": [f"g-{i}"]}
        for i in range(50)
    ]
    client = _StubClient()
    healed = ingest.backfill_standalone_rates(rows, client, max_calls=5)
    assert healed == 5
    assert len(client.calls) == 5


def test_standalone_booking_fetches_eta_vessel_transshipment_from_body():
    """Standalone construction must fill eta_offered, vessel_voyage,
    transshipment (not just ol_rate) from the booking-confirmation
    body. Pre-fix, only ol_rate was extracted — the other three fields
    landed None and dragged the Phase 8 parser hit-rate baseline. Per
    Michael 2026-04-29 'standalone WINs are missing booking-body fields,
    fix it'."""
    class _StubClient:
        def __init__(self, body):
            self._body = body
            self.calls = []

        def get_message_body(self, message_id):
            from datetime import datetime, timezone

            from hilmar.graph_client import MessageBody
            self.calls.append(message_id)
            return MessageBody(
                id=message_id, conversation_id="cv",
                subject="MDOLX260420_BOOKING CONFIRMATION",
                body_content_type="text", body=self._body,
                body_preview=self._body[:200],
                received_at=datetime.now(timezone.utc),
                from_address="ops@ol-usa.com",
            )

    body = (
        "Booking confirmed.\n"
        "Carrier: ONE\nRate: $1,950.00 per 20'DV\n"
        "ETA: 2026-06-08\n"
        "Vessel: APL CHARLESTON / V.123W\n"
        "Via: Singapore\n"
    )
    client = _StubClient(body)
    bookings = {
        "260420": {
            "mdolx": "260420",
            "subject": "MDOLX260420_BOOKING CONFIRMATION// HILMAR 1X20'DV Oakland to Bangkok// ONE: RICGE7217600",
            "sent": "2026-04-16T21:33:49+00:00",
            "carrier": "ONE",
            "source_bucket": "mbd_inbound",
            "source_imid": "imid-2",
            "source_id": "graph-msg-260420",
        },
    }
    _, standalones = ingest.link_bookings_to_requests([], bookings, client=client)
    s = standalones[0]
    assert s["ol_rate"] == 1950.0
    assert s["eta_offered"] is not None
    assert s["vessel_voyage"] is not None
    assert s["transshipment"] is not None
    assert s["transshipment"].lower() == "singapore"


def test_backfill_standalone_rates_heals_eta_vessel_transshipment():
    """Pre-fix stand_* rows that have ol_rate but lack the
    booking-confirmation fields (eta_offered, vessel_voyage,
    transshipment) get healed on the next ingest run. The fetch is
    triggered when ANY of the four fields is missing."""
    class _StubClient:
        def __init__(self, body):
            self._body = body
            self.calls = []

        def get_message_body(self, message_id):
            from datetime import datetime, timezone

            from hilmar.graph_client import MessageBody
            self.calls.append(message_id)
            return MessageBody(
                id=message_id, conversation_id="cv", subject="",
                body_content_type="text", body=self._body,
                body_preview=self._body[:200],
                received_at=datetime.now(timezone.utc), from_address=None,
            )

    body = (
        "Carrier: CMA CGM\nRate: $3,600 per 40' RF\n"
        "ETA: 2026-05-20\n"
        "Vessel: MARCO POLO / V.456E\n"
        "Via: Hong Kong\n"
    )
    client = _StubClient(body)
    rows = [
        # Pre-fix shape: ol_rate populated by old code, other 3 fields None
        {"request_id": "stand_260460", "status": "WIN",
         "ol_rate": 3600.0, "eta_offered": None,
         "vessel_voyage": None, "transshipment": None,
         "source_ids": ["g-460"]},
    ]
    healed = ingest.backfill_standalone_rates(rows, client)
    assert healed == 1
    assert rows[0]["ol_rate"] == 3600.0  # preserved (not overwritten)
    assert rows[0]["eta_offered"] is not None
    assert rows[0]["vessel_voyage"] is not None
    assert rows[0]["transshipment"] is not None


def test_standalone_booking_no_spec_in_subject_falls_back_to_zero():
    """If the subject doesn't carry a parseable spec (e.g. a free-time
    issue follow-up that just mentions an MDOLX#), teu stays 0 — no
    crash, no spurious data."""
    bookings = {
        "260062": {
            "mdolx": "260062",
            "subject": "RE: MDOLX260062_ *FREE-TIME ISSUE - MSC BKG # EBKG14800694 // HILMAR",
            "sent": "2026-04-22T13:59:51+00:00",
            "carrier": "MSC",
            "source_bucket": "mbd_inbound",
            "source_imid": "imid-4",
            "source_id": "id-4",
        },
    }
    _, standalones = ingest.link_bookings_to_requests([], bookings)
    s = standalones[0]
    assert s["containers"] is None
    assert s["container_count"] == 0
    assert s["teu_won"] == 0


# ─────────────────────────────────────────────────────────────────────
# Container-spec recovery from rate-response body / persisted rows.
# Audit on 2026-04-29 caught 2 Q&L rows with containers=None where the
# Lonny outbound subject was just "Oakland to <dest>" — preview missed
# the spec and the rate-response body's mention was never harvested.
# ─────────────────────────────────────────────────────────────────────


def test_parse_container_spec_works_on_body_text():
    """The generalized parser should match a spec embedded in prose body
    text, not just structured booking-confirmation subjects."""
    from hilmar import body_parser as BP
    body = (
        "Hi Lonny,\n\n"
        "Quoting your 1x40HC Oakland to HCMC at $4,200 with ONE.\n"
        "ETD 2026-05-15.\n"
    )
    spec = BP.parse_container_spec(body)
    assert spec is not None
    assert "40" in spec
    # Backwards-compat alias must produce the same result
    assert BP.parse_container_spec_from_subject(body) == spec


def test_backfill_quoted_containers_heals_q_and_l_row():
    """Persisted Q&L row with containers=None gets healed when its
    source-email body carries the spec. Catches the 2026-04-29 audit
    finding (req_d72835b5341716c7 / req_6534213992e4c08e)."""
    class _StubClient:
        def __init__(self, body):
            self._body = body
            self.calls: list[str] = []

        def get_message_body(self, message_id):
            from datetime import datetime, timezone

            from hilmar.graph_client import MessageBody
            self.calls.append(message_id)
            return MessageBody(
                id=message_id, conversation_id="cv", subject="",
                body_content_type="text", body=self._body,
                body_preview=self._body[:200],
                received_at=datetime.now(timezone.utc),
                from_address=None,
            )

    client = _StubClient(
        "Quoting your 1x40HC Oakland to Osaka at $3,880 with ONE.\nETD 2026-05-15."
    )
    rows = [
        {"request_id": "req_q_and_l_no_containers", "status": "Q&L",
         "containers": None, "teu_requested": 0, "container_count": 0,
         "source_ids": ["g-q1"]},
        # Already has containers → skipped
        {"request_id": "req_already_filled", "status": "Q&L",
         "containers": "1x20'DV", "source_ids": ["g-skip"]},
        # NQ status → skipped (not in scope)
        {"request_id": "req_nq", "status": "NQ", "containers": None,
         "source_ids": ["g-nq"]},
    ]
    healed = ingest.backfill_quoted_containers(rows, client)
    assert healed == 1
    assert rows[0]["containers"] is not None
    assert "40" in rows[0]["containers"]
    assert rows[0]["container_count"] == 1
    assert rows[0]["teu_requested"] == 2  # 40' = 2 TEU
    assert rows[1]["containers"] == "1x20'DV"  # untouched
    assert rows[2]["containers"] is None  # untouched (NQ skipped)
    assert "g-skip" not in client.calls
    assert "g-nq" not in client.calls


def test_backfill_quoted_containers_no_client_is_noop():
    """No GraphClient → no-op, no crash. Tests / CI safety."""
    rows = [{"request_id": "r1", "status": "Q&L", "containers": None,
             "source_ids": ["g-1"]}]
    healed = ingest.backfill_quoted_containers(rows, client=None)
    assert healed == 0
    assert rows[0]["containers"] is None


def test_backfill_quoted_containers_swallows_fetch_errors():
    """A Graph fetch error must not abort ingest — log + skip."""
    class _BoomClient:
        def get_message_body(self, message_id):
            raise RuntimeError("Graph 500")

    rows = [{"request_id": "r1", "status": "Q&L", "containers": None,
             "source_ids": ["g-1"]}]
    healed = ingest.backfill_quoted_containers(rows, _BoomClient())
    assert healed == 0
    assert rows[0]["containers"] is None
