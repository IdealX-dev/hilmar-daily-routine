"""Tests for hilmar.graph_client — all Graph HTTP is mocked via `responses`.

These tests must NEVER make a real Graph or Microsoft Identity call. MSAL is
mocked at the `msal.PublicClientApplication` boundary; HTTP is mocked at the
`requests` boundary via the `responses` library.
"""
from __future__ import annotations

import base64
import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlparse

import pytest
import responses

from hilmar import graph_client as gc
from hilmar.graph_client import (
    GRAPH_BASE,
    INLINE_ATTACHMENT_LIMIT_BYTES,
    GraphAuthError,
    GraphClient,
    GraphError,
    MessageBody,
    MessageMeta,
)

# ─────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────

@pytest.fixture
def fake_token():
    return "FAKE_ACCESS_TOKEN"


@pytest.fixture
def client(tmp_path, fake_token):
    """A GraphClient with auth pre-faked + sleep neutered."""
    c = GraphClient(token_cache_path=tmp_path / "cache.bin")
    c._access_token = fake_token
    c._sleep = lambda _s: None
    return c


def _msg(
    *,
    id: str = "AAMkAD-1",
    conv: str = "conv-1",
    subject: str = "Test",
    sender: str = "lupfold@hilmaringredients.com",
    sender_name: str = "Lonny Upfold",
    to: list[str] | None = None,
    received: str = "2026-04-25T14:30:00Z",
) -> dict:
    """Build a Graph /me/messages list-item payload."""
    return {
        "id": id,
        "conversationId": conv,
        "subject": subject,
        "from": {"emailAddress": {"address": sender, "name": sender_name}},
        "toRecipients": [{"emailAddress": {"address": a}} for a in (to or ["michael.deitchman@ol-usa.com"])],
        "ccRecipients": [],
        "receivedDateTime": received,
        "sentDateTime": received,
        "internetMessageId": f"<{id}@hilmaringredients.com>",
        "isRead": False,
        "hasAttachments": False,
    }


# ─────────────────────────────────────────────────────────────────────
# Auth
# ─────────────────────────────────────────────────────────────────────

class TestAuthentication:
    def test_silent_auth_with_cached_account_returns_token(self, tmp_path, fake_token):
        fake_app = MagicMock()
        fake_app.get_accounts.return_value = [{"username": "michael.deitchman@ol-usa.com"}]
        fake_app.acquire_token_silent.return_value = {"access_token": fake_token}

        with patch.object(gc.msal, "PublicClientApplication", return_value=fake_app):
            client = GraphClient(token_cache_path=tmp_path / "cache.bin")
            token = client.authenticate()

        assert token == fake_token
        fake_app.acquire_token_silent.assert_called_once()
        fake_app.initiate_device_flow.assert_not_called()

    def test_silent_auth_failure_without_interactive_raises(self, tmp_path):
        fake_app = MagicMock()
        fake_app.get_accounts.return_value = []  # no cached account
        fake_app.acquire_token_silent.return_value = None

        with patch.object(gc.msal, "PublicClientApplication", return_value=fake_app):
            client = GraphClient(token_cache_path=tmp_path / "cache.bin")
            with pytest.raises(GraphAuthError):
                client.authenticate(interactive_ok=False)

        fake_app.initiate_device_flow.assert_not_called()

    def test_device_flow_used_when_interactive_ok(self, tmp_path, fake_token, capsys):
        fake_app = MagicMock()
        fake_app.get_accounts.return_value = []
        fake_app.initiate_device_flow.return_value = {
            "user_code": "ABC-DEF",
            "message": "Go to https://microsoft.com/devicelogin and enter code ABC-DEF",
            "verification_uri": "https://microsoft.com/devicelogin",
        }
        fake_app.acquire_token_by_device_flow.return_value = {"access_token": fake_token}

        with patch.object(gc.msal, "PublicClientApplication", return_value=fake_app):
            client = GraphClient(token_cache_path=tmp_path / "cache.bin")
            token = client.authenticate(interactive_ok=True)

        assert token == fake_token
        fake_app.initiate_device_flow.assert_called_once()
        fake_app.acquire_token_by_device_flow.assert_called_once()
        # User-facing prompt actually gets printed
        out = capsys.readouterr().out
        assert "ABC-DEF" in out

    def test_device_flow_init_error_raises(self, tmp_path):
        fake_app = MagicMock()
        fake_app.get_accounts.return_value = []
        fake_app.initiate_device_flow.return_value = {
            "error": "invalid_client",
            "error_description": "client not registered",
        }

        with patch.object(gc.msal, "PublicClientApplication", return_value=fake_app):
            client = GraphClient(token_cache_path=tmp_path / "cache.bin")
            with pytest.raises(GraphAuthError, match="client not registered"):
                client.authenticate(interactive_ok=True)

    def test_token_cache_persisted_when_changed(self, tmp_path, fake_token):
        cache_path = tmp_path / "subdir" / "cache.bin"  # parent doesn't exist yet

        # Real SerializableTokenCache so .has_state_changed / .serialize work
        real_cache = gc.msal.SerializableTokenCache()
        real_cache._cache = {"AccessToken": {"k": "v"}}  # force has_state_changed=True
        real_cache.has_state_changed = True

        fake_app = MagicMock()
        fake_app.get_accounts.return_value = [{"username": "michael@..."}]
        fake_app.acquire_token_silent.return_value = {"access_token": fake_token}

        def make_app(**kwargs):
            # MSAL constructor call swaps in our pre-set cache so the persistence
            # path runs against a state-changed cache.
            return fake_app

        with patch.object(gc.msal, "PublicClientApplication", side_effect=make_app):
            client = GraphClient(token_cache_path=cache_path)
            client._cache = real_cache  # pre-seed (skips _build_msal_app's fresh cache)
            client._app = fake_app
            client.authenticate()

        assert cache_path.exists(), "cache file should be written"
        assert cache_path.read_text(encoding="utf-8")  # non-empty

    def test_ensure_token_uses_cached_token_no_reauth(self, client):
        """Once authenticated, repeated _ensure_token calls don't re-hit MSAL."""
        with patch.object(gc.msal, "PublicClientApplication") as pca:
            assert client._ensure_token() == client._access_token
            assert client._ensure_token() == client._access_token
        pca.assert_not_called()


# ─────────────────────────────────────────────────────────────────────
# search_messages — filters, pagination, limit
# ─────────────────────────────────────────────────────────────────────

class TestSearchMessages:
    def _qs(self, request_url: str) -> dict[str, list[str]]:
        return parse_qs(urlparse(request_url).query)

    @responses.activate
    def test_sender_filter_built_correctly(self, client):
        responses.add(
            responses.GET,
            f"{GRAPH_BASE}/me/messages",
            json={"value": [_msg()]},
            status=200,
        )
        out = client.search_messages(sender="lupfold@hilmaringredients.com")
        assert len(out) == 1
        assert isinstance(out[0], MessageMeta)
        assert out[0].from_address == "lupfold@hilmaringredients.com"

        qs = self._qs(responses.calls[0].request.url)
        f = qs["$filter"][0]
        assert "from/emailAddress/address eq 'lupfold@hilmaringredients.com'" in f

    @responses.activate
    def test_recipient_uses_search_not_filter(self, client):
        """Regression 2026-04-27: recipient query MUST use $search, not $filter.

        Graph rejects ``toRecipients/any(...)`` lambdas combined with any other
        $filter clause on /me/messages with ErrorInvalidUrlQueryFilter. Switching
        the recipient lookup to KQL $search ("to:<addr>") avoids the lambda.
        Without this, OL-reply ingest is silently dropped — see
        ingest.fetch_window's exception handler that catches the 400 and
        continues with sender-only results.
        """
        responses.add(
            responses.GET,
            f"{GRAPH_BASE}/me/messages",
            json={"value": [_msg(sender="michael.deitchman@ol-usa.com",
                                 to=["lupfold@hilmaringredients.com"])]},
            status=200,
        )
        client.search_messages(recipient="lupfold@hilmaringredients.com")
        qs = self._qs(responses.calls[0].request.url)
        # MUST use $search, MUST NOT use $filter.
        assert "$search" in qs, "recipient query must use $search"
        assert "$filter" not in qs, "recipient query must NOT use $filter (Graph rejects the lambda combo)"
        # KQL syntax check — quoted "to:<addr>" payload.
        search_val = qs["$search"][0]
        assert "to:lupfold@hilmaringredients.com" in search_val
        assert search_val.startswith('"') and search_val.endswith('"'), \
            "$search value must be enclosed in double-quotes per Graph spec"

    @responses.activate
    def test_recipient_with_sender_combines_in_search(self, client):
        """When both sender + recipient given, both go into the $search KQL."""
        responses.add(
            responses.GET,
            f"{GRAPH_BASE}/me/messages",
            json={"value": []},
            status=200,
        )
        client.search_messages(
            sender="MBD_OceanExportBookingShared@ol-usa.com",
            recipient="lupfold@hilmaringredients.com",
        )
        qs = self._qs(responses.calls[0].request.url)
        search_val = qs["$search"][0]
        assert "to:lupfold@hilmaringredients.com" in search_val
        assert "from:MBD_OceanExportBookingShared@ol-usa.com" in search_val
        assert " AND " in search_val

    @responses.activate
    def test_recipient_with_dates_filters_client_side(self, client):
        """$search is mutually exclusive with $filter, so date constraints
        must be applied client-side. Items outside [after, before) drop.
        """
        in_window = _msg(id="m_in", received="2026-04-22T17:00:00Z",
                         to=["lupfold@hilmaringredients.com"])
        too_old   = _msg(id="m_old", received="2026-04-10T17:00:00Z",
                         to=["lupfold@hilmaringredients.com"])
        too_new   = _msg(id="m_new", received="2026-04-30T17:00:00Z",
                         to=["lupfold@hilmaringredients.com"])
        responses.add(
            responses.GET,
            f"{GRAPH_BASE}/me/messages",
            json={"value": [in_window, too_old, too_new]},
            status=200,
        )
        out = client.search_messages(
            recipient="lupfold@hilmaringredients.com",
            after=datetime(2026, 4, 13, tzinfo=timezone.utc),
            before=datetime(2026, 4, 27, tzinfo=timezone.utc),
        )
        # No $filter on URL (KQL path), no receivedDateTime constraints either.
        qs = self._qs(responses.calls[0].request.url)
        assert "$filter" not in qs
        # Client-side filter dropped the out-of-window items.
        assert [m.id for m in out] == ["m_in"]

    @responses.activate
    def test_after_and_before_date_filters(self, client):
        responses.add(
            responses.GET,
            f"{GRAPH_BASE}/me/messages",
            json={"value": []},
            status=200,
        )
        after = datetime(2026, 4, 1, 12, 0, tzinfo=timezone.utc)
        before = datetime(2026, 4, 26, 0, 0, tzinfo=timezone.utc)
        client.search_messages(after=after, before=before)
        f = self._qs(responses.calls[0].request.url)["$filter"][0]
        assert "receivedDateTime ge 2026-04-01T12:00:00Z" in f
        assert "receivedDateTime lt 2026-04-26T00:00:00Z" in f
        assert " and " in f

    @responses.activate
    def test_naive_datetime_treated_as_utc(self, client):
        responses.add(
            responses.GET,
            f"{GRAPH_BASE}/me/messages",
            json={"value": []},
            status=200,
        )
        client.search_messages(after=datetime(2026, 4, 1, 12, 0))  # naive
        f = self._qs(responses.calls[0].request.url)["$filter"][0]
        assert "2026-04-01T12:00:00Z" in f

    @responses.activate
    def test_pagination_follows_next_link(self, client):
        next_link = f"{GRAPH_BASE}/me/messages?$skiptoken=PAGE2"
        responses.add(
            responses.GET,
            f"{GRAPH_BASE}/me/messages",
            json={
                "value": [_msg(id="m1"), _msg(id="m2")],
                "@odata.nextLink": next_link,
            },
            status=200,
        )
        responses.add(
            responses.GET,
            next_link,
            json={"value": [_msg(id="m3")]},
            status=200,
        )
        out = client.search_messages(sender="lupfold@hilmaringredients.com", limit=10)
        assert [m.id for m in out] == ["m1", "m2", "m3"]
        assert len(responses.calls) == 2

    @responses.activate
    def test_pagination_stops_at_limit(self, client):
        next_link = f"{GRAPH_BASE}/me/messages?$skiptoken=PAGE2"
        responses.add(
            responses.GET,
            f"{GRAPH_BASE}/me/messages",
            json={
                "value": [_msg(id="m1"), _msg(id="m2"), _msg(id="m3")],
                "@odata.nextLink": next_link,
            },
            status=200,
        )
        out = client.search_messages(limit=2)
        assert [m.id for m in out] == ["m1", "m2"]
        # Should NOT have followed nextLink — limit hit mid-page
        assert len(responses.calls) == 1

    @responses.activate
    def test_apostrophe_in_address_is_escaped(self, client):
        responses.add(
            responses.GET,
            f"{GRAPH_BASE}/me/messages",
            json={"value": []},
            status=200,
        )
        client.search_messages(sender="o'reilly@example.com")
        f = self._qs(responses.calls[0].request.url)["$filter"][0]
        # Single apostrophe escaped to two (OData literal escape)
        assert "o''reilly@example.com" in f

    @responses.activate
    def test_select_includes_required_fields(self, client):
        responses.add(
            responses.GET,
            f"{GRAPH_BASE}/me/messages",
            json={"value": []},
            status=200,
        )
        client.search_messages()
        select = self._qs(responses.calls[0].request.url)["$select"][0]
        for field in ("conversationId", "from", "receivedDateTime",
                      "internetMessageId", "subject", "toRecipients"):
            assert field in select

    @responses.activate
    def test_message_meta_parses_addresses_and_timestamps(self, client):
        responses.add(
            responses.GET,
            f"{GRAPH_BASE}/me/messages",
            json={"value": [_msg(
                id="m1", conv="C1",
                sender="lupfold@hilmaringredients.com",
                sender_name="Lonny Upfold",
                to=["michael.deitchman@ol-usa.com", "team@ol-usa.com"],
                received="2026-04-25T14:30:00Z",
            )]},
            status=200,
        )
        out = client.search_messages()
        assert len(out) == 1
        m = out[0]
        assert m.id == "m1"
        assert m.conversation_id == "C1"
        assert m.from_address == "lupfold@hilmaringredients.com"
        assert m.from_name == "Lonny Upfold"
        assert m.to_addresses == ["michael.deitchman@ol-usa.com", "team@ol-usa.com"]
        assert m.received_at == datetime(2026, 4, 25, 14, 30, tzinfo=timezone.utc)
        assert m.received_at.tzinfo is timezone.utc

    def test_zero_limit_returns_empty_without_request(self, client):
        # No `responses.activate` — any HTTP call would fail loudly.
        assert client.search_messages(limit=0) == []


# ─────────────────────────────────────────────────────────────────────
# get_message_body
# ─────────────────────────────────────────────────────────────────────

class TestGetMessageBody:
    @responses.activate
    def test_fetches_full_body(self, client):
        responses.add(
            responses.GET,
            f"{GRAPH_BASE}/me/messages/m1",
            json={
                "id": "m1",
                "conversationId": "C1",
                "subject": "RE: HILMAR Rate Request — IFB-Iraq",
                "from": {"emailAddress": {"address": "lupfold@hilmaringredients.com",
                                          "name": "Lonny Upfold"}},
                "receivedDateTime": "2026-04-25T14:30:00Z",
                "bodyPreview": "Send.",
                "body": {"contentType": "html", "content": "<p>Send.</p>"},
            },
            status=200,
        )
        body = client.get_message_body("m1")
        assert isinstance(body, MessageBody)
        assert body.id == "m1"
        assert body.subject.startswith("RE: HILMAR")
        assert body.body_content_type == "html"
        assert body.body == "<p>Send.</p>"
        assert body.body_preview == "Send."
        assert body.from_address == "lupfold@hilmaringredients.com"
        assert body.received_at == datetime(2026, 4, 25, 14, 30, tzinfo=timezone.utc)


# ─────────────────────────────────────────────────────────────────────
# send_mail
# ─────────────────────────────────────────────────────────────────────

class TestSendMail:
    @responses.activate
    def test_send_with_attachment_inlines_base64(self, client, tmp_path):
        pdf = tmp_path / "report.pdf"
        pdf.write_bytes(b"%PDF-1.4 hello world")

        responses.add(
            responses.POST,
            f"{GRAPH_BASE}/me/sendMail",
            status=202,
            headers={"request-id": "req-xyz"},
        )

        rid = client.send_mail(
            to=["team@ol-usa.com"],
            cc=["michael.deitchman@idealx.us"],
            subject="Hilmar Daily Tracker — 2026-04-25",
            html_body="<h1>Daily</h1>",
            attachments=[pdf],
        )
        assert rid == "req-xyz"

        body = json.loads(responses.calls[0].request.body)
        msg = body["message"]
        assert body["saveToSentItems"] is True
        assert msg["subject"].startswith("Hilmar Daily Tracker")
        assert msg["body"]["contentType"] == "HTML"
        assert msg["toRecipients"] == [{"emailAddress": {"address": "team@ol-usa.com"}}]
        assert msg["ccRecipients"] == [{"emailAddress": {"address": "michael.deitchman@idealx.us"}}]
        assert len(msg["attachments"]) == 1
        att = msg["attachments"][0]
        assert att["@odata.type"] == "#microsoft.graph.fileAttachment"
        assert att["name"] == "report.pdf"
        assert att["contentType"] == "application/pdf"
        assert base64.b64decode(att["contentBytes"]) == b"%PDF-1.4 hello world"

    @responses.activate
    def test_send_without_attachments_omits_key(self, client):
        responses.add(
            responses.POST,
            f"{GRAPH_BASE}/me/sendMail",
            status=202,
        )
        client.send_mail(
            to=["team@ol-usa.com"],
            subject="Quick note",
            html_body="<p>hi</p>",
        )
        body = json.loads(responses.calls[0].request.body)
        msg = body["message"]
        assert "attachments" not in msg
        assert msg["ccRecipients"] == []

    def test_send_attachment_too_large_raises(self, client, tmp_path):
        big = tmp_path / "big.bin"
        big.write_bytes(b"x" * (INLINE_ATTACHMENT_LIMIT_BYTES + 1))

        # No HTTP mock — send_mail must reject before issuing a request.
        with pytest.raises(GraphError, match="exceeds inline cap"):
            client.send_mail(
                to=["team@ol-usa.com"],
                subject="Oversized",
                html_body="<p>nope</p>",
                attachments=[big],
            )


# ─────────────────────────────────────────────────────────────────────
# Retry on 429 / 503 / 504
# ─────────────────────────────────────────────────────────────────────

class TestRetry:
    @responses.activate
    def test_429_then_success_with_retry_after(self, client, monkeypatch):
        slept: list[float] = []
        client._sleep = lambda s: slept.append(s)

        responses.add(
            responses.GET,
            f"{GRAPH_BASE}/me/messages",
            status=429,
            headers={"Retry-After": "3"},
            json={"error": {"code": "TooManyRequests"}},
        )
        responses.add(
            responses.GET,
            f"{GRAPH_BASE}/me/messages",
            json={"value": [_msg(id="m1")]},
            status=200,
        )

        out = client.search_messages()
        assert len(out) == 1
        assert slept == [3.0], f"Retry-After=3 should override default backoff; got {slept}"
        assert len(responses.calls) == 2

    @responses.activate
    def test_503_retry_then_success(self, client):
        slept: list[float] = []
        client._sleep = lambda s: slept.append(s)

        responses.add(
            responses.GET,
            f"{GRAPH_BASE}/me/messages",
            status=503,
        )
        responses.add(
            responses.GET,
            f"{GRAPH_BASE}/me/messages",
            json={"value": []},
            status=200,
        )
        client.search_messages()
        assert slept == [2.0]  # default first-retry backoff

    @responses.activate
    def test_all_retries_exhausted_raises(self, client):
        slept: list[float] = []
        client._sleep = lambda s: slept.append(s)

        # 3 retries means 4 total attempts; per spec backoffs are 2/4/8.
        for _ in range(4):
            responses.add(
                responses.GET,
                f"{GRAPH_BASE}/me/messages",
                status=503,
            )

        with pytest.raises(GraphError, match="503"):
            client.search_messages()
        assert slept == [2.0, 4.0, 8.0]

    @responses.activate
    def test_retry_after_unparseable_falls_back_to_default(self, client):
        slept: list[float] = []
        client._sleep = lambda s: slept.append(s)

        responses.add(
            responses.GET,
            f"{GRAPH_BASE}/me/messages",
            status=429,
            headers={"Retry-After": "Wed, 26 Apr 2026 13:00:00 GMT"},  # HTTP-date form
        )
        responses.add(
            responses.GET,
            f"{GRAPH_BASE}/me/messages",
            json={"value": []},
            status=200,
        )
        client.search_messages()
        assert slept == [2.0]

    @responses.activate
    def test_send_mail_retries_on_throttle(self, client):
        slept: list[float] = []
        client._sleep = lambda s: slept.append(s)

        responses.add(
            responses.POST,
            f"{GRAPH_BASE}/me/sendMail",
            status=429,
            headers={"Retry-After": "1"},
        )
        responses.add(
            responses.POST,
            f"{GRAPH_BASE}/me/sendMail",
            status=202,
            headers={"request-id": "req-2"},
        )
        rid = client.send_mail(
            to=["team@ol-usa.com"], subject="x", html_body="<p>x</p>",
        )
        assert rid == "req-2"
        assert slept == [1.0]


# ─────────────────────────────────────────────────────────────────────
# upload_to_onedrive
# ─────────────────────────────────────────────────────────────────────

class TestOneDriveUpload:
    @responses.activate
    def test_upload_simple_put_returns_weburl(self, client, tmp_path):
        pdf = tmp_path / "daily.pdf"
        pdf.write_bytes(b"%PDF-1.4 small")

        folder_id = "01JZE2M6FTFSWA3VUUKNAKRUE56FNH44ZP"
        responses.add(
            responses.PUT,
            f"{GRAPH_BASE}/me/drive/items/{folder_id}:/daily.pdf:/content",
            json={
                "id": "item123",
                "name": "daily.pdf",
                "webUrl": "https://onedrive.example/daily.pdf",
            },
            status=201,
        )

        url = client.upload_to_onedrive(folder_id=folder_id, local_path=pdf)
        assert url == "https://onedrive.example/daily.pdf"

        sent = responses.calls[0].request
        assert sent.body == b"%PDF-1.4 small"
        assert sent.headers["Content-Type"] == "application/pdf"
        assert sent.headers["Authorization"] == "Bearer FAKE_ACCESS_TOKEN"

    def test_upload_missing_file_raises(self, client, tmp_path):
        with pytest.raises(GraphError, match="does not exist"):
            client.upload_to_onedrive(
                folder_id="x", local_path=tmp_path / "nope.pdf",
            )

    @responses.activate
    def test_upload_by_path_simple_put_returns_weburl(self, client, tmp_path):
        """Path-based upload PUTs to /me/drive/root:/{path}/{name}:/content,
        which auto-creates folders. Replaces the stale-folder-ID problem."""
        pdf = tmp_path / "daily.pdf"
        pdf.write_bytes(b"%PDF-1.4 small")
        responses.add(
            responses.PUT,
            f"{GRAPH_BASE}/me/drive/root:/Hilmar Tracker Reports/daily.pdf:/content",
            json={
                "id": "item456", "name": "daily.pdf",
                "webUrl": "https://onedrive.example/Hilmar/daily.pdf",
            },
            status=201,
        )
        url = client.upload_to_onedrive_by_path(
            folder_path="Hilmar Tracker Reports", local_path=pdf,
        )
        assert url == "https://onedrive.example/Hilmar/daily.pdf"

    @responses.activate
    def test_upload_by_path_strips_leading_and_trailing_slashes(self, client, tmp_path):
        pdf = tmp_path / "daily.pdf"
        pdf.write_bytes(b"%PDF-1.4 small")
        responses.add(
            responses.PUT,
            f"{GRAPH_BASE}/me/drive/root:/Reports/2026/daily.pdf:/content",
            json={"id": "x", "webUrl": "https://onedrive.example/x"},
            status=201,
        )
        url = client.upload_to_onedrive_by_path(
            folder_path="/Reports/2026/", local_path=pdf,
        )
        assert "x" in url

    def test_upload_by_path_missing_file_raises(self, client, tmp_path):
        with pytest.raises(GraphError, match="does not exist"):
            client.upload_to_onedrive_by_path(
                folder_path="X", local_path=tmp_path / "ghost.pdf",
            )

    def test_upload_by_path_empty_path_raises(self, client, tmp_path):
        pdf = tmp_path / "x.pdf"
        pdf.write_bytes(b"")
        with pytest.raises(GraphError, match="folder_path is empty"):
            client.upload_to_onedrive_by_path(folder_path="/", local_path=pdf)


# ─────────────────────────────────────────────────────────────────────
# odata helpers + datetime formatting (smoke)
# ─────────────────────────────────────────────────────────────────────

class TestHelpers:
    def test_odata_quote_escapes_apostrophes(self):
        assert gc._odata_quote("o'reilly") == "o''reilly"
        assert gc._odata_quote("plain") == "plain"

    def test_fmt_graph_dt_emits_z_suffix(self):
        dt = datetime(2026, 4, 26, 9, 30, tzinfo=timezone.utc)
        assert gc._fmt_graph_dt(dt) == "2026-04-26T09:30:00Z"

    def test_fmt_graph_dt_normalises_to_utc(self):
        from datetime import timedelta
        et = datetime(2026, 4, 26, 5, 30, tzinfo=timezone(timedelta(hours=-4)))
        # 05:30 ET (UTC-4) == 09:30 UTC
        assert gc._fmt_graph_dt(et) == "2026-04-26T09:30:00Z"


class TestEnvFallback:
    """GraphClient must read HILMAR_TENANT_ID / HILMAR_CLIENT_ID /
    HILMAR_GRAPH_SCOPES from env when explicit kwargs aren't passed.
    Regression-guards rate-blaster-v2 first-auth-attempt 2026-04-26 where
    .env had HILMAR_TENANT_ID set but the code used DEFAULT_TENANT='common'
    anyway, allowing MSAL to accept the wrong tenant's account."""

    def test_tenant_id_falls_back_to_env(self, monkeypatch):
        monkeypatch.setenv("HILMAR_TENANT_ID", "ol-usa.com")
        client = gc.GraphClient()
        assert client.tenant_id == "ol-usa.com"

    def test_tenant_id_explicit_kwarg_wins_over_env(self, monkeypatch):
        monkeypatch.setenv("HILMAR_TENANT_ID", "ol-usa.com")
        client = gc.GraphClient(tenant_id="contoso.com")
        assert client.tenant_id == "contoso.com"

    def test_tenant_id_default_when_no_env_no_kwarg(self, monkeypatch):
        monkeypatch.delenv("HILMAR_TENANT_ID", raising=False)
        client = gc.GraphClient()
        assert client.tenant_id == gc.DEFAULT_TENANT

    def test_client_id_falls_back_to_env(self, monkeypatch):
        monkeypatch.setenv("HILMAR_CLIENT_ID", "11111111-2222-3333-4444-555555555555")
        client = gc.GraphClient()
        assert client.client_id == "11111111-2222-3333-4444-555555555555"

    def test_client_id_explicit_kwarg_wins_over_env(self, monkeypatch):
        monkeypatch.setenv("HILMAR_CLIENT_ID", "from-env")
        client = gc.GraphClient(client_id="from-arg")
        assert client.client_id == "from-arg"

    def test_scopes_fall_back_to_env(self, monkeypatch):
        monkeypatch.setenv("HILMAR_GRAPH_SCOPES", "Mail.Read Files.Read")
        client = gc.GraphClient()
        assert client.scopes == ["Mail.Read", "Files.Read"]

    def test_scopes_default_when_no_env(self, monkeypatch):
        monkeypatch.delenv("HILMAR_GRAPH_SCOPES", raising=False)
        client = gc.GraphClient()
        assert client.scopes == list(gc.DEFAULT_SCOPES)

    def test_authority_built_from_resolved_tenant(self, monkeypatch):
        """Building the MSAL app with a non-default tenant must succeed —
        MSAL canonicalizes domain identifiers (e.g. ol-usa.com) into the
        tenant GUID at discovery time, so we just verify (a) self.tenant_id
        is what we passed AND (b) MSAL accepted it without error and gave
        us back an authority distinct from the 'common' default."""
        monkeypatch.setenv("HILMAR_TENANT_ID", "common")
        common_app = gc.GraphClient()._build_msal_app()
        common_authority = common_app.authority.authorization_endpoint

        monkeypatch.setenv("HILMAR_TENANT_ID", "ol-usa.com")
        ol_client = gc.GraphClient()
        assert ol_client.tenant_id == "ol-usa.com"
        ol_app = ol_client._build_msal_app()
        ol_authority = ol_app.authority.authorization_endpoint
        # MSAL replaces the literal string with the resolved tenant GUID.
        # The two authorities must DIFFER — proves env routing is plumbed.
        assert ol_authority != common_authority, (
            f"tenant pinning ineffective: ol={ol_authority} vs common={common_authority}"
        )
