"""The pipeline reads the mailbox Lonny writes to, and keeps reading ours.

2026-08-07. diag_day, run 6, against production:

    reading: https://graph.microsoft.com/v1.0/me
      /me resolves to: Michael.Deitchman@ol-usa.com
      >>> NOT the intended read target (MBD_OceanExportBookingShared@ol-usa.com)

READ_MAILBOX has always documented the shared booking mailbox as the thread
endpoint. _mailbox_base only becomes that when GRAPH_APP_* is configured, and
those secrets are empty on this tenant, so the delegated path read /me. It
never errored — it read a real mailbox, just not the one the RFQs go to.

Michael, asked to choose: "1 and 3" — read the shared mailbox AND keep his own
as a second source.

What is pinned here is the part that must not regress quietly:
  - both mailboxes are read, shared FIRST so it wins a dedup collision
  - a message is fetched from the mailbox it was FOUND in (ids are
    mailbox-scoped; the wrong mailbox is a 404, not a fallback)
  - a missing Mail.Read.Shared DEGRADES to /me and says so — it must never
    take the fire down
  - SCOPES (silent refresh) stays narrow while AUTH_SCOPES (consent) goes wide
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

SRC = (ROOT / "scripts" / "refresh_stage.py").read_text(encoding="utf-8")


def test_silent_refresh_scopes_stay_narrow():
    """THE thing that must not break. outlook_send.SCOPES is what every silent
    refresh asks for. Adding Mail.Read.Shared to it would make the refresh fail
    against the already-consented cache and take the whole daily fire down — to
    fix a gap that currently only costs us data."""
    import outlook_send as OS
    assert "Mail.Read.Shared" not in OS.SCOPES, (
        "Mail.Read.Shared is in SCOPES — the next silent refresh will fail "
        "and the daily fire will stop sending")
    assert OS.SCOPES == ["Mail.Send", "Mail.Read", "Files.ReadWrite"]


def test_consent_scopes_go_wide():
    """A fresh consent must ask for the shared-read permission, or the
    re-auth accomplishes nothing."""
    import outlook_send as OS
    assert "Mail.Read.Shared" in OS.AUTH_SCOPES
    assert set(OS.SCOPES).issubset(set(OS.AUTH_SCOPES)), (
        "AUTH_SCOPES dropped a scope the send path needs")


def test_the_device_flow_requests_the_wide_set():
    """Both device-flow call sites, not just one — the interactive path and
    the headless auth-bg path both seed the same cache."""
    src = (ROOT / "scripts" / "outlook_send.py").read_text(encoding="utf-8")
    assert "initiate_device_flow(scopes=SCOPES)" not in src, (
        "a device-code flow still consents to the narrow set")
    assert src.count("initiate_device_flow(scopes=AUTH_SCOPES)") == 2


def test_auth_bg_silent_check_uses_the_wide_set():
    """If auth-bg checks silently against the NARROW set, an already-consented
    cache returns 'silent ok' and the re-consent never happens — the workflow
    would report success and change nothing."""
    src = (ROOT / "scripts" / "outlook_send.py").read_text(encoding="utf-8")
    block = src.split("def cmd_auth_bg", 1)[-1][:1500]
    assert "acquire_token_silent(AUTH_SCOPES" in block, (
        "auth-bg's silent check uses the narrow scope set, so it will short-"
        "circuit on the old cache and never widen the consent")


def test_shared_mailbox_is_read_first(monkeypatch):
    """Order is load-bearing: main() keeps the FIRST copy of a deduped
    message and fetches its body from that mailbox. The shared mailbox is the
    authoritative copy of a thread that exists in both."""
    import refresh_stage as RS
    monkeypatch.setattr(RS, "_mailbox_base", f"{RS.GRAPH}/me")
    monkeypatch.setattr(RS, "shared_token_silent", lambda: "shared-tok")

    targets = RS.read_targets("me-tok")
    assert [t[0] for t in targets] == [RS.SHARED_MAILBOX, "me"]
    assert targets[0][1] == f"{RS.GRAPH}/users/{RS.SHARED_MAILBOX}"
    assert targets[0][2] == "shared-tok"
    assert targets[1][1] == f"{RS.GRAPH}/me"
    assert targets[1][2] == "me-tok", "the /me read must use the /me token"


def test_missing_shared_scope_degrades_to_me_and_warns(monkeypatch, capsys):
    """Until the operator re-auths, the fire must keep running on /me alone.
    Silently is what we already had for a week, so it warns — and the warning
    names the consequence, not just the condition."""
    import refresh_stage as RS
    monkeypatch.setattr(RS, "_mailbox_base", f"{RS.GRAPH}/me")
    monkeypatch.setattr(RS, "shared_token_silent", lambda: None)

    targets = RS.read_targets("me-tok")
    assert [t[0] for t in targets] == ["me"], "degraded run lost the /me read"

    out = capsys.readouterr().out
    assert "::warning::" in out, "the degraded read is silent"
    assert RS.SHARED_MAILBOX in out
    assert "Mail.Read.Shared" in out
    assert "MISSES" in out, (
        "the warning states the condition but not what it costs")


def test_app_only_still_reads_its_single_target(monkeypatch):
    """If OL ever registers the Entra app, _mailbox_base becomes
    /users/{READ_MAILBOX} and there is no /me to add. Adding one would 400."""
    import refresh_stage as RS
    monkeypatch.setattr(RS, "_mailbox_base", f"{RS.GRAPH}/users/{RS.READ_MAILBOX}")
    targets = RS.read_targets("app-tok")
    assert len(targets) == 1
    assert targets[0][0] == RS.READ_MAILBOX
    assert "/me" not in targets[0][1]


def test_shared_token_never_raises(monkeypatch):
    """A broken or absent cache must yield None, not an exception — this runs
    inside the daily fire."""
    import refresh_stage as RS

    def boom():
        raise RuntimeError("cache is shredded")

    monkeypatch.setattr(RS.OS, "_load_cache", boom)
    assert RS.shared_token_silent() is None


def test_reads_are_addressed_to_the_mailbox_that_produced_the_message():
    """A Graph message id is mailbox-scoped. Fetching a shared-mailbox message
    from /me is a 404 — and body_failures would climb while the log said
    nothing about which mailbox. The `base` argument is what prevents it."""
    import inspect

    import refresh_stage as RS
    for fn in (RS.search_messages, RS.get_message_body, RS.fetch_pdf_attachments):
        assert "base" in inspect.signature(fn).parameters, (
            f"{fn.__name__} cannot be pointed at a specific mailbox")

    # and main() must actually pass it, per message, from the recorded source
    assert 'it["_src"] = mbox' in SRC, "the source mailbox is not recorded"
    assert "by_label.get(it.get(\"_src\")" in SRC, (
        "the body fetch does not look up the message's own mailbox")
    assert "get_message_body(_tok, it[\"id\"], base=_base)" in SRC
    # anchor on the CALL, not the def — "fetch_pdf_attachments(" matches the
    # signature first and would assert against the wrong 200 characters
    call = SRC.split("saved = fetch_pdf_attachments(", 1)
    assert len(call) == 2, "the PDF fetch call site moved"
    assert "base=_base" in call[-1][:200], (
        "PDF attachments are fetched from the default mailbox, not the "
        "message's own — every shared-mailbox booking PDF would 404")


def test_the_source_tag_never_reaches_the_stage_file():
    """`_src` is bookkeeping. build_stage_record writes an explicit dict, so it
    cannot leak — asserted, because a stray key in stage_emails would ride into
    every downstream consumer of that file."""
    import refresh_stage as RS
    rec = RS.build_stage_record(
        {"id": "AAMk", "internetMessageId": "<x@y>", "subject": "s",
         "receivedDateTime": "2026-08-06T18:00:00Z",
         "sentDateTime": "2026-08-06T17:00:00Z",
         "_src": "MBD_OceanExportBookingShared@ol-usa.com"},
        "lonny_outbound")
    assert "_src" not in rec


def test_one_unreadable_mailbox_does_not_cost_us_the_other():
    """Partial credit is the whole point of reading two. A 403 on the shared
    mailbox mid-run must not abort the /me pass that still works."""
    block = SRC.split("for mbox, base, mtoken in targets:", 1)[-1][:1200]
    assert "except Exception" in block, (
        "a failing mailbox query aborts the whole loop")
    assert "continue" in block
    assert "::warning::" in block, "a failed mailbox query is not surfaced"


def test_the_code_is_emailed_before_the_blocking_wait():
    """Michael: "what am i doing from my phone and where.. you do it."

    The sign-in cannot be delegated — device-code flow exists so only the
    credential holder can complete it, and Microsoft returns no
    verification_uri_complete for this client (verified against the live
    endpoint). What CAN go is the hunting.

    ORDER IS THE FEATURE: initiate → email → block. Emailing after the
    blocking call would deliver the code once it was already approved or
    expired, i.e. never usefully. This asserts the sequence in the source
    because the alternative is a 15-minute live test.
    """
    src = (ROOT / "scripts" / "auth_notify.py").read_text(encoding="utf-8")
    initiate = src.index("initiate_device_flow")
    email = src.index("OS.send_mail(")
    block = src.index("acquire_token_by_device_flow")
    assert initiate < email < block, (
        "the code is not emailed between starting the flow and waiting on it")


def test_a_failed_send_does_not_abort_the_reauth():
    """The code is valid and printed regardless. Losing the convenience of
    the email must not cost the consent — the operator can still read it."""
    src = (ROOT / "scripts" / "auth_notify.py").read_text(encoding="utf-8")
    tail = src.split("OS.send_mail(", 1)[-1][:600]
    assert "except Exception" in tail and "::warning::" in tail
    assert "return" not in tail.split("::warning::", 1)[0][-200:], (
        "a failed send returns early and abandons the device flow")


def test_it_refuses_when_it_cannot_deliver_the_code(monkeypatch):
    """If the cached credential is too dead to SEND with, starting a device
    flow strands the operator: a code exists, nothing delivers it, and the job
    blocks for 15 minutes. Fail before the flow starts, not after."""
    src = (ROOT / "scripts" / "auth_notify.py").read_text(encoding="utf-8")
    guard = src.index("cannot acquire a token to SEND with")
    flow = src.index("initiate_device_flow")
    assert guard < flow, (
        "the send-token check happens after the device flow starts")


def test_consent_without_the_shared_scope_is_an_error_not_a_success():
    """The one failure that would look like success: the operator signs in,
    the run goes green, and the shared mailbox is still unreadable."""
    src = (ROOT / "scripts" / "auth_notify.py").read_text(encoding="utf-8")
    assert 'if "Mail.Read.Shared" not in granted:' in src
    tail = src.split('if "Mail.Read.Shared" not in granted:', 1)[-1][:400]
    assert "::error::" in tail and "return 1" in tail


def test_the_notification_email_survives_outlook():
    """No var(), no flex/grid, no <style> — Outlook and Word drop all three,
    and a code the operator cannot read is the whole job wasted. Same
    constraint the daily report is built under.

    AST, not a source slice: _body's own docstring EXPLAINS the rule with the
    words "var()/flex/grid" in it, and a substring check matched the
    explanation and failed on correct code. Sixth time this session that an
    identifier in prose was indistinguishable from one in code.
    """
    import ast

    src = (ROOT / "scripts" / "auth_notify.py").read_text(encoding="utf-8")
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.FunctionDef) and n.name == "_body")
    # every string the function actually EMITS, docstring excluded
    emitted = "".join(
        n.value for n in ast.walk(fn)
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
        and n is not (fn.body[0].value if isinstance(fn.body[0], ast.Expr) else None)
    )
    assert emitted, "could not read _body's output"
    assert "var(" not in emitted, "the email uses CSS variables — Outlook drops them"
    assert "display:flex" not in emitted and "display:grid" not in emitted
    assert "<style" not in emitted
    # background shorthand AND background-color: Outlook needs the pair.
    # Collapse whitespace first — a style attribute legitimately wraps across
    # source lines, and asserting on the unwrapped form would fail on correct
    # HTML for the sake of where the newline happens to fall.
    import re as _re
    flat = _re.sub(r"\s+", "", emitted)
    for ground in ("#1e3a5f", "#eef3f8"):
        assert f"background-color:{ground};background:{ground}" in flat, (
            f"the {ground} ground is not doubled — Outlook renders it "
            f"transparent and the code becomes unreadable")


def test_the_auth_workflow_is_confirm_gated_and_pushes_only_the_cache():
    """It replaces a live credential. It must not fire on a stray click, and
    it must not round-trip the whole state set over fresher data — the exact
    bug state_store.push's `only` parameter was added for."""
    wf = (ROOT / ".github" / "workflows" / "auth-refresh.yml").read_text(encoding="utf-8")
    assert "workflow_dispatch:" in wf and "schedule:" not in wf
    assert "inputs.confirm == 'REAUTH'" in wf, "the re-auth is not confirm-gated"
    assert "only=['token-cache']" in wf, (
        "the auth job pushes the full state set — it can revert a day's ingest")
    assert "if: success()" in wf, "it pushes even when the consent failed"
    assert "azure-storage-blob" in wf
