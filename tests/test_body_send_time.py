"""The body-cache reader must find what the body-cache writer actually wrote.

THE FAILURE THIS EXISTS FOR. On 2026-08-05 a heal shipped to date the quotes
QC-077 reports — rows carrying a rate with no response_timestamp, invisible to
every date-bucketed section of the report forever. It was described as fixed.
The count went 29 (07-30) → 41 (08-05) → 43 (08-06), THROUGH the fix.

The heal never dated one row, and could not have:

    fetch_bodies.upsert_body writes      "sent_ts" / "received_ts"
    qc_selfheal._body_send_time read     "sent" / "sentDateTime" / "received"
    patch_carriers read the same wrong three

stage_emails.txt genuinely uses sent/received; stage_emails_bodies.txt uses
sent_ts/received_ts. Two file schemas for one concept, and both healers reached
for the other file's spelling. Every lookup returned None — silently, because a
missing key is not an error — so the QC-077 set became MONOTONIC: rate recovery
kept adding undated rows and nothing could ever remove one. That is the 29 → 41
→ 43 shape exactly.

WHY THIS TEST IS SHAPED THIS WAY. A test that asserts the reader handles a
hardcoded list of spellings proves only that someone wrote the same list twice.
These build a record through the REAL writer and assert the REAL reader finds
its timestamp — so the reader is pinned to the writer, and renaming the field
in fetch_bodies fails here instead of silently switching the heal off again.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import core  # noqa: E402


@pytest.fixture
def written_record(tmp_path, monkeypatch):
    """A record produced by the REAL writer, into a temp cache file.

    fetch_bodies.upsert_body is the only thing that creates rows in
    stage_emails_bodies.txt. BODIES_PATH is redirected so the test never
    touches the repo's own cache.
    """
    import fetch_bodies as FB
    monkeypatch.setattr(FB, "BODIES_PATH", tmp_path / "stage_emails_bodies.txt")
    return FB.upsert_body(
        imid="AAA@ol-usa.com",
        bucket="mbd_rate_response",
        uri="https://graph.microsoft.com/v1.0/messages/AAA",
        subject="RE: Ocean rate request — Oakland → Shanghai",
        html_body="<p>CMA CGM | $3,150 | ETD 2026-04-10</p>",
        sent_ts="2026-04-08T15:04:12Z",
        received_ts="2026-04-08T15:04:40Z",
    )


def test_the_reader_finds_what_the_writer_wrote(written_record):
    """The binding assertion, and it has to go through the real writer.

    Its first draft called a `build_body_record` that does not exist, guarded
    by `hasattr`, so it silently fell back to a hand-written dict and asserted
    a fact about itself. It would NOT have failed if upsert_body renamed its
    timestamp keys — the exact regression it is named for. Caught by Copilot
    on PR #152.

    That is the same defect this whole file documents, committed while writing
    about not committing it: a check that looks like it binds two things and
    actually binds one thing to a copy of itself.
    """
    assert core.body_send_time(written_record) == "2026-04-08T15:04:12Z", (
        "the body-cache reader cannot see the send time the writer stored — "
        "this is the exact break that made the 08-05 heal a no-op"
    )


def test_the_written_record_survives_the_real_loader(tmp_path, monkeypatch):
    """Writer → JSONL on disk → loader → reader, end to end.

    The heal reads through _load_bodies_index, which parses the file line by
    line. Asserting on the in-memory return value alone would miss anything
    lost in serialisation.
    """
    import fetch_bodies as FB
    import qc_selfheal as QC
    path = tmp_path / "stage_emails_bodies.txt"
    monkeypatch.setattr(FB, "BODIES_PATH", path)
    FB.upsert_body(
        imid="BBB@ol-usa.com",
        bucket="mbd_rate_response",
        uri="uri",
        subject="RE: rate",
        html_body="<p>MSC | $2,900</p>",
        sent_ts="2026-04-09T11:00:00Z",
    )
    monkeypatch.setattr(QC, "_load_bodies_index",
                        lambda: {r["imid"]: r for r in
                                 [json.loads(x) for x in
                                  path.read_text(encoding="utf-8").splitlines() if x.strip()]})
    idx = QC._load_bodies_index()
    row = {"request_id": "r1", "ol_rate": 2900.0, "source_imids": ["BBB@ol-usa.com"]}
    assert QC._stamp_response_time_from_bodies(row, idx) is True
    assert row["response_timestamp"] == "2026-04-09T11:00:00Z"


def test_a_renamed_timestamp_key_is_caught(written_record):
    """Prove the binding actually binds. Rename the writer's key on the real
    record and the reader must lose it — if this still resolves, the test
    above is passing for the wrong reason."""
    mangled = {k: v for k, v in written_record.items() if k not in ("sent_ts", "received_ts")}
    mangled["sent_at_renamed"] = "2026-04-08T15:04:12Z"
    assert core.body_send_time(mangled) is None


def test_the_writers_field_names_are_covered():
    """Read fetch_bodies' source and assert every timestamp key it writes is a
    key the reader knows. Catches a rename at the source rather than waiting
    for a count to drift upward for two days."""
    src = (ROOT / "scripts" / "fetch_bodies.py").read_text(encoding="utf-8")
    written = {k for k in ("sent_ts", "received_ts", "sent", "sentDateTime",
                           "received", "receivedDateTime")
               if f'"{k}":' in src}
    assert written, "fetch_bodies writes no recognisable timestamp key"
    missing = written - set(core.BODY_SEND_TIME_FIELDS)
    assert not missing, (
        f"fetch_bodies writes {sorted(missing)}, which core.body_send_time "
        f"does not read — the heal will silently stop dating rows")


@pytest.mark.parametrize("field", ["sent_ts", "sentDateTime", "sent",
                                   "received_ts", "receivedDateTime", "received"])
def test_every_supported_spelling_resolves(field):
    """Both file schemas, because rows predating either rename are still on
    disk and a heal that skips them leaves them undateable forever."""
    assert core.body_send_time({field: "2026-04-08T15:04:12Z"}) == "2026-04-08T15:04:12Z"


def test_send_beats_received():
    """When OL quoted is the SEND time. Received is a delivery-hop fallback,
    used only when nothing better is on the record."""
    rec = {"received_ts": "2026-04-08T16:00:00Z", "sent_ts": "2026-04-08T15:04:12Z"}
    assert core.body_send_time(rec) == "2026-04-08T15:04:12Z"


def test_a_timeless_record_returns_none_not_a_guess():
    """The heal must decline rather than invent. QC-077's job is to report
    what cannot be dated; fabricating a timestamp would make the number clean
    and the data wrong."""
    assert core.body_send_time({"imid": "x", "text_body": "..."}) is None
    assert core.body_send_time({}) is None
    assert core.body_send_time(None) is None


def test_both_healers_go_through_the_shared_reader():
    """Two rate-recovery routes, one definition. #140 fixed one route and left
    the other; 08-05 fixed the other and both read the wrong schema. The only
    durable answer is that neither owns a copy."""
    for name in ("qc_selfheal.py", "patch_carriers.py"):
        src = (ROOT / "scripts" / name).read_text(encoding="utf-8")
        assert ("core.body_send_time" in src or "C.body_send_time" in src
                or "core.BODY_SEND_TIME_FIELDS" in src), (
            f"{name} reads the body send time itself instead of via core")
        assert 'd.get("sent") or d.get("sentDateTime")' not in src, (
            f"{name} still hand-rolls the stage_emails.txt spelling")


def test_the_heal_dates_a_row_end_to_end():
    """The behaviour the count depends on, exercised through the real heal
    rather than through the reader alone."""
    import qc_selfheal as QC
    row = {"request_id": "r1", "ol_rate": 3150.0, "source_imids": ["AAA@ol"]}
    bodies = {"AAA@ol": {"imid": "AAA@ol", "sent_ts": "2026-04-08T15:04:12Z"}}
    assert QC._stamp_response_time_from_bodies(row, bodies) is True
    assert row["response_timestamp"] == "2026-04-08T15:04:12Z"


def test_a_dated_row_leaves_the_qc077_set():
    """The property that makes the count able to go DOWN. Before this fix the
    set was monotonic — recovery added members and nothing removed them."""
    import qc_selfheal as QC
    bodies = {"AAA@ol": {"imid": "AAA@ol", "sent_ts": "2026-04-08T15:04:12Z"}}
    row = {"request_id": "r1", "ol_rate": 3150.0, "source_imids": ["AAA@ol"]}
    assert QC._undated_reason(row, bodies) != "no_send_time"
    QC._stamp_response_time_from_bodies(row, bodies)
    assert row.get("response_timestamp"), "the row is still undated after the heal"


def test_the_json_schema_of_a_written_record_round_trips():
    """A record written and re-read from a JSONL line must still be readable —
    _load_bodies_index parses each line with json.loads."""
    rec = {"imid": "AAA@ol", "sent_ts": "2026-04-08T15:04:12Z"}
    assert core.body_send_time(json.loads(json.dumps(rec))) == "2026-04-08T15:04:12Z"
