#!/usr/bin/env python3
"""
graph_client.py — Microsoft Graph wrapper using MSAL device-code (delegated) auth.

Auth model
----------
PublicClientApplication + device code flow on Michael's personal mailbox
(michael.deitchman@ol-usa.com). NO admin consent, NO service principal. First
run prompts the user at https://microsoft.com/devicelogin; subsequent runs
reuse the refresh token cached at ``HILMAR_TOKEN_CACHE`` (chmod 600).

Cron context
------------
``orchestrator`` calls ``authenticate(interactive_ok=False)``. If the silent
refresh fails (cache wiped, refresh-token expired after ~90d idle), this
raises ``GraphAuthError`` and the caller is responsible for surfacing the
failure (log + email Michael — never block the run on stdin).

Bootstrap
---------
The console script ``hilmar-auth-login`` (registered in pyproject.toml) calls
``authenticate(interactive_ok=True)`` so a human can complete device-code login
once after VM provisioning.

Design rules
------------
- requests-based HTTP. We do NOT pull msgraph-sdk.
- Retry on 429 / 503 / 504 with exponential backoff: 3 retries
  (4 attempts total), sleeping 2s/4s/8s between. Honor ``Retry-After`` on 429.
- Pagination via ``@odata.nextLink`` until exhausted or ``limit`` reached.
- Delegated-only: no ``mailbox_owner`` parameter. Search hits ``/me/messages``;
  to find OL→Lonny replies use ``recipient=lupfold@hilmaringredients.com``.
"""
from __future__ import annotations

import base64
import contextlib
import logging
import mimetypes
import os
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import msal
import requests

log = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
DEFAULT_CLIENT_ID = "14d82eec-204b-4c2f-b7e8-296a70dab67e"  # Microsoft public client
DEFAULT_TENANT = "common"
DEFAULT_SCOPES = [
    "Mail.Read",
    "Mail.Send",
    "Mail.ReadWrite",
    "Files.ReadWrite",
    # offline_access is implicit for PublicClientApplication; passing it via
    # the scope list raises in MSAL ("offline_access is a reserved scope").
    # The refresh-token grant is issued automatically.
]

# Inline (base64 in /sendMail body) attachments cap. Graph's documented limit
# is 4 MB per message; we leave headroom for the JSON envelope.
INLINE_ATTACHMENT_LIMIT_BYTES = 3 * 1024 * 1024

# Status codes we retry. 429 = throttled; 503/504 = transient upstream.
RETRYABLE_STATUS = {429, 503, 504}
RETRY_BACKOFFS_S = (2.0, 4.0, 8.0)  # waited BEFORE retry attempt 2 / 3 / (giveup)


# ─────────────────────────────────────────────────────────────────────
# Public types
# ─────────────────────────────────────────────────────────────────────

class GraphError(RuntimeError):
    """Any non-retryable Graph failure surfaces as this."""


class GraphAuthError(GraphError):
    """Silent auth failed and ``interactive_ok=False``. Caller must escalate."""


@dataclass
class MessageMeta:
    """Lightweight projection of a Graph message — the fields ingest needs."""
    id: str
    conversation_id: str
    subject: str
    from_address: str | None
    from_name: str | None
    to_addresses: list[str]
    cc_addresses: list[str]
    received_at: datetime          # aware UTC
    sent_at: datetime | None       # aware UTC, optional
    internet_message_id: str | None
    is_read: bool
    has_attachments: bool


@dataclass
class MessageBody:
    """Full body fetch — used by ingest after a hit on search_messages()."""
    id: str
    conversation_id: str
    subject: str
    body_content_type: str         # "html" or "text"
    body: str
    body_preview: str
    received_at: datetime
    from_address: str | None


# ─────────────────────────────────────────────────────────────────────
# Small helpers
# ─────────────────────────────────────────────────────────────────────

def _odata_quote(s: str) -> str:
    """Escape single quotes for OData literal usage."""
    return s.replace("'", "''")


def _fmt_graph_dt(dt: datetime) -> str:
    """Graph wants ISO-8601 UTC with a Z suffix, second-precision."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ensure_utc(dt: datetime) -> datetime:
    """Treat naive datetimes as UTC; convert aware ones to UTC."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _parse_graph_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    raw = s.rstrip("Z")
    try:
        # Graph emits e.g. "2026-04-26T13:45:12Z" or with fractional seconds
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _addr(recipient: dict) -> tuple[str | None, str | None]:
    e = (recipient or {}).get("emailAddress") or {}
    return e.get("address"), e.get("name")


def _parse_message_meta(item: dict) -> MessageMeta:
    from_addr, from_name = _addr(item.get("from") or {})
    to = [a for a, _ in (_addr(r) for r in item.get("toRecipients") or []) if a]
    cc = [a for a, _ in (_addr(r) for r in item.get("ccRecipients") or []) if a]
    received = _parse_graph_dt(item.get("receivedDateTime"))
    if received is None:
        # Defensive: receivedDateTime is required for ingest. If Graph ever omits
        # it (e.g., draft), fall back to epoch — caller can filter.
        received = datetime(1970, 1, 1, tzinfo=timezone.utc)
    return MessageMeta(
        id=item["id"],
        conversation_id=item.get("conversationId", ""),
        subject=item.get("subject") or "",
        from_address=from_addr,
        from_name=from_name,
        to_addresses=to,
        cc_addresses=cc,
        received_at=received,
        sent_at=_parse_graph_dt(item.get("sentDateTime")),
        internet_message_id=item.get("internetMessageId"),
        is_read=bool(item.get("isRead", False)),
        has_attachments=bool(item.get("hasAttachments", False)),
    )


def _content_type_for(path: Path) -> str:
    ctype, _ = mimetypes.guess_type(str(path))
    return ctype or "application/octet-stream"


def _default_token_cache_path() -> Path:
    env = os.environ.get("HILMAR_TOKEN_CACHE")
    if env:
        return Path(env)
    return Path.home() / ".hilmar-tracker" / "token-cache.bin"


def _chmod_600(path: Path) -> None:
    """Best-effort chmod 600. POSIX-only; on Windows just sets read/write for owner."""
    try:
        os.chmod(path, 0o600)
    except OSError as e:
        log.debug("chmod 600 failed for %s: %s (non-fatal on Windows)", path, e)


# ─────────────────────────────────────────────────────────────────────
# GraphClient
# ─────────────────────────────────────────────────────────────────────

class GraphClient:
    """Delegated-auth Microsoft Graph wrapper.

    Typical use:
        gc = GraphClient()
        gc.authenticate()                       # silent, raises if no cache
        msgs = gc.search_messages(sender="lupfold@hilmaringredients.com",
                                  after=yesterday)
        body = gc.get_message_body(msgs[0].id)
    """

    def __init__(
        self,
        client_id: str | None = None,
        tenant_id: str | None = None,
        token_cache_path: Path | None = None,
        scopes: list[str] | None = None,
        *,
        session: requests.Session | None = None,
    ):
        # Args take precedence (tests pass explicit values), then env
        # (production reads .env via systemd EnvironmentFile or `set -a;
        # source .env`), then the package defaults. The tenant value MUST
        # be honoured because tenant=common lets MSAL accept any account
        # — pinning to e.g. "ol-usa.com" forces device-code login to a
        # specific tenant and prevents accidental SSO into another tenant.
        self.client_id = client_id or os.environ.get("HILMAR_CLIENT_ID") or DEFAULT_CLIENT_ID
        self.tenant_id = tenant_id or os.environ.get("HILMAR_TENANT_ID") or DEFAULT_TENANT
        self.token_cache_path = Path(token_cache_path) if token_cache_path else _default_token_cache_path()
        if scopes is not None:
            self.scopes = list(scopes)
        else:
            env_scopes = os.environ.get("HILMAR_GRAPH_SCOPES")
            self.scopes = env_scopes.split() if env_scopes else list(DEFAULT_SCOPES)

        self._session = session or requests.Session()
        self._sleep: Callable[[float], None] = time.sleep   # injectable for tests
        self._access_token: str | None = None

        # Lazily initialised — built the first time we need to talk to AAD.
        self._cache: msal.SerializableTokenCache | None = None
        self._app: msal.PublicClientApplication | None = None

    # ── auth ──────────────────────────────────────────────────────────

    def _build_msal_app(self) -> msal.PublicClientApplication:
        cache = msal.SerializableTokenCache()
        if self.token_cache_path.exists():
            try:
                cache.deserialize(self.token_cache_path.read_text(encoding="utf-8"))
            except Exception as e:  # corrupt cache — log and start fresh
                log.warning("Token cache at %s unreadable (%s); starting fresh.",
                            self.token_cache_path, e)
        self._cache = cache
        return msal.PublicClientApplication(
            client_id=self.client_id,
            authority=f"https://login.microsoftonline.com/{self.tenant_id}",
            token_cache=cache,
        )

    def _persist_cache(self) -> None:
        if not self._cache or not self._cache.has_state_changed:
            return
        self.token_cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.token_cache_path.write_text(self._cache.serialize(), encoding="utf-8")
        _chmod_600(self.token_cache_path)

    def authenticate(self, *, interactive_ok: bool = False) -> str:
        """Acquire a Graph access token.

        Tries silent (refresh-token) first. Falls back to device flow only when
        ``interactive_ok=True``; otherwise raises :class:`GraphAuthError`.
        """
        if self._app is None:
            self._app = self._build_msal_app()
        app = self._app

        result: dict | None = None
        accounts = app.get_accounts()
        if accounts:
            result = app.acquire_token_silent(self.scopes, account=accounts[0])

        if not result:
            if not interactive_ok:
                raise GraphAuthError(
                    "Silent auth failed and interactive_ok=False. "
                    "Run `hilmar-auth-login` from a TTY to refresh the token cache."
                )
            flow = app.initiate_device_flow(scopes=self.scopes)
            if "user_code" not in flow:
                raise GraphAuthError(
                    f"Device flow init failed: {flow.get('error_description') or flow}"
                )
            # The MSAL message includes the URL + code. Print + log so it shows
            # up in both interactive shells and journalctl.
            sys.stdout.write(flow["message"] + "\n")
            sys.stdout.flush()
            log.info("Device-code prompt issued: %s", flow.get("message"))
            result = app.acquire_token_by_device_flow(flow)

        if not result or "access_token" not in result:
            err = (result or {}).get("error_description") or (result or {}).get("error") or "unknown"
            raise GraphAuthError(f"Token acquisition failed: {err}")

        self._persist_cache()
        self._access_token = result["access_token"]
        return self._access_token

    def _ensure_token(self) -> str:
        if self._access_token:
            return self._access_token
        return self.authenticate(interactive_ok=False)

    # ── HTTP core ────────────────────────────────────────────────────

    def _request(
        self,
        method: str,
        url: str,
        *,
        params: dict | None = None,
        json: Any = None,
        data: bytes | None = None,
        headers: dict | None = None,
        expect_json: bool = True,
    ) -> dict:
        """Authenticated Graph request with retry on 429/503/504.

        Returns the parsed JSON body, or {} for empty 2xx responses.
        """
        token = self._ensure_token()
        merged_headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        }
        if json is not None and data is None:
            merged_headers["Content-Type"] = "application/json"
        if headers:
            merged_headers.update(headers)

        last_response: requests.Response | None = None
        for attempt in range(len(RETRY_BACKOFFS_S) + 1):  # 3 attempts total
            resp = self._session.request(
                method, url,
                params=params, json=json, data=data,
                headers=merged_headers,
                timeout=60,
            )
            last_response = resp
            if resp.status_code in RETRYABLE_STATUS and attempt < len(RETRY_BACKOFFS_S):
                wait = RETRY_BACKOFFS_S[attempt]
                ra = resp.headers.get("Retry-After")
                if ra:
                    # HTTP-date form (e.g. "Wed, 26 Apr 2026 13:00:00 GMT") is
                    # legal too; on parse failure we keep the schedule wait.
                    with contextlib.suppress(ValueError):
                        wait = float(ra)
                log.info("Graph %s %s -> %s; retrying in %.1fs (attempt %d)",
                         method, url, resp.status_code, wait, attempt + 2)
                self._sleep(wait)
                continue
            break

        assert last_response is not None  # loop always assigns
        if not (200 <= last_response.status_code < 300):
            raise GraphError(
                f"Graph {method} {url} failed: {last_response.status_code} {last_response.text[:500]}"
            )

        if not expect_json or not last_response.content:
            return {}
        ctype = last_response.headers.get("Content-Type", "")
        if "application/json" not in ctype:
            return {}
        return last_response.json()

    # ── high-level ops ───────────────────────────────────────────────

    def search_messages(
        self,
        *,
        sender: str | None = None,
        recipient: str | None = None,
        after: datetime | None = None,
        before: datetime | None = None,
        limit: int = 50,
    ) -> list[MessageMeta]:
        """Search the authenticated user's mailbox.

        Use ``recipient=lupfold@hilmaringredients.com`` to find OL→Lonny replies
        (we don't have shared-mailbox visibility under delegated auth).

        Walks ``@odata.nextLink`` until ``limit`` is reached or pages exhausted.

        Two query modes — Graph forces our hand here:

        - ``$filter`` path (sender-only or no filters): canonical OData filter.
          Used when ``recipient`` is not set.
        - ``$search`` path (any time ``recipient`` is set): KQL-style Outlook
          search. ``$search`` is mutually exclusive with ``$filter``/``$orderby``
          on ``/me/messages``, so date constraints are applied client-side.

        Why two paths: Graph's ``/me/messages`` rejects
        ``toRecipients/any(r: r/emailAddress/address eq '<addr>')`` combined
        with any other ``$filter`` clause with ``ErrorInvalidUrlQueryFilter``
        ("query filter contains one or more invalid nodes"). This silently
        broke OL-reply ingest in production 2026-04-27 — every Lonny request
        looked NQ because the recipient query failed and the exception was
        swallowed in ``ingest.fetch_window``. The KQL ``to:<addr>`` syntax
        avoids the lambda entirely.
        """
        if limit <= 0:
            return []

        # Route on whether recipient is set — that's the trigger for $search.
        use_search = bool(recipient)

        params: dict[str, str] = {
            "$top": str(min(limit, 50)),
            "$select": (
                "id,conversationId,subject,from,toRecipients,ccRecipients,"
                "receivedDateTime,sentDateTime,internetMessageId,isRead,hasAttachments"
            ),
            # NB: no $orderby. /me/messages rejects $filter combined with
            # $orderby on the same property unless the orderby column is
            # ALSO the first $filter clause — see Graph "InefficientFilter"
            # error from production 2026-04-26 when we had both
            # `from/... eq` AND `receivedDateTime ge/lt` AND
            # `$orderby=receivedDateTime desc`. Order isn't load-bearing
            # downstream (fetch_window dedups by id), so the simplest fix
            # is to omit $orderby entirely. If we ever need ordering, we
            # can sort client-side after the fetch.
        }

        if use_search:
            # KQL-style Outlook search. Property restrictors (to:, from:) work
            # against the parsed recipient/sender headers — not a lambda over
            # a collection — so Graph accepts the combination cleanly.
            terms = [f'to:{recipient}']
            if sender:
                terms.append(f'from:{sender}')
            # Graph requires $search value to be a quoted string.
            params["$search"] = '"' + " AND ".join(terms) + '"'
        else:
            filters: list[str] = []
            if sender:
                filters.append(f"from/emailAddress/address eq '{_odata_quote(sender)}'")
            if after:
                filters.append(f"receivedDateTime ge {_fmt_graph_dt(after)}")
            if before:
                filters.append(f"receivedDateTime lt {_fmt_graph_dt(before)}")
            if filters:
                params["$filter"] = " and ".join(filters)

        url: str | None = f"{GRAPH_BASE}/me/messages"
        next_params: dict | None = params

        # When using $search with date constraints, fetch a wider window than
        # the caller's limit — $search returns by relevance not date, so
        # post-filtering by date can drop arbitrary fractions of the result.
        # Cap at 5x or 250 to keep the worst case bounded.
        max_fetch = (
            min(max(limit * 5, 50), 250)
            if use_search and (after or before)
            else limit
        )

        raw_results: list[MessageMeta] = []
        while url and len(raw_results) < max_fetch:
            data = self._request("GET", url, params=next_params)
            for item in data.get("value", []):
                raw_results.append(_parse_message_meta(item))
                if len(raw_results) >= max_fetch:
                    break
            url = data.get("@odata.nextLink")
            next_params = None  # nextLink already carries the query string

        # Client-side date filter — only needed on the $search path.
        if use_search and (after or before):
            after_utc = _ensure_utc(after) if after else None
            before_utc = _ensure_utc(before) if before else None
            filtered: list[MessageMeta] = []
            for m in raw_results:
                ts = m.received_at or m.sent_at
                if ts is None:
                    continue
                ts = _ensure_utc(ts)
                if after_utc and ts < after_utc:
                    continue
                if before_utc and ts >= before_utc:
                    continue
                filtered.append(m)
            raw_results = filtered

        return raw_results[:limit]

    def get_message_body(self, message_id: str) -> MessageBody:
        """Fetch a single message including its full body."""
        url = f"{GRAPH_BASE}/me/messages/{message_id}"
        params = {
            "$select": (
                "id,conversationId,subject,from,receivedDateTime,"
                "body,bodyPreview"
            ),
        }
        data = self._request("GET", url, params=params)
        body = data.get("body") or {}
        from_addr, _ = _addr(data.get("from") or {})
        received = _parse_graph_dt(data.get("receivedDateTime")) \
            or datetime(1970, 1, 1, tzinfo=timezone.utc)
        return MessageBody(
            id=data["id"],
            conversation_id=data.get("conversationId", ""),
            subject=data.get("subject") or "",
            body_content_type=(body.get("contentType") or "text").lower(),
            body=body.get("content") or "",
            body_preview=data.get("bodyPreview") or "",
            received_at=received,
            from_address=from_addr,
        )

    def send_mail(
        self,
        *,
        to: list[str],
        subject: str,
        html_body: str,
        cc: list[str] | None = None,
        attachments: list[Path] | None = None,
    ) -> str:
        """Send an HTML email from the authenticated user (Michael).

        Attachments are inlined as base64. Total payload must stay under
        ``INLINE_ATTACHMENT_LIMIT_BYTES`` (3 MB) — Graph's hard cap is 4 MB
        per message including the JSON envelope. If the daily PDF ever
        exceeds this, switch to createUploadSession.

        Returns the ``request-id`` response header for log correlation
        (Graph's /sendMail returns 202 Accepted with no body).
        """
        cc = cc or []
        attachments = attachments or []

        attach_payload = []
        total_bytes = 0
        for p in attachments:
            content = p.read_bytes()
            total_bytes += len(content)
            attach_payload.append({
                "@odata.type": "#microsoft.graph.fileAttachment",
                "name": p.name,
                "contentType": _content_type_for(p),
                "contentBytes": base64.b64encode(content).decode("ascii"),
            })
        if total_bytes > INLINE_ATTACHMENT_LIMIT_BYTES:
            raise GraphError(
                f"Attachment payload {total_bytes:,} bytes exceeds inline cap "
                f"{INLINE_ATTACHMENT_LIMIT_BYTES:,}. Use upload session."
            )

        message = {
            "subject": subject,
            "body": {"contentType": "HTML", "content": html_body},
            "toRecipients": [{"emailAddress": {"address": a}} for a in to],
            "ccRecipients": [{"emailAddress": {"address": a}} for a in cc],
        }
        if attach_payload:
            message["attachments"] = attach_payload

        # Re-implement via _request but capture the request-id header. Easiest:
        # do the call here, share the retry/auth machinery via a thin wrapper.
        token = self._ensure_token()
        url = f"{GRAPH_BASE}/me/sendMail"
        payload = {"message": message, "saveToSentItems": True}
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

        last_response: requests.Response | None = None
        for attempt in range(len(RETRY_BACKOFFS_S) + 1):
            resp = self._session.post(url, json=payload, headers=headers, timeout=60)
            last_response = resp
            if resp.status_code in RETRYABLE_STATUS and attempt < len(RETRY_BACKOFFS_S):
                wait = RETRY_BACKOFFS_S[attempt]
                ra = resp.headers.get("Retry-After")
                if ra:
                    with contextlib.suppress(ValueError):
                        wait = float(ra)
                self._sleep(wait)
                continue
            break

        assert last_response is not None
        if not (200 <= last_response.status_code < 300):
            raise GraphError(
                f"sendMail failed: {last_response.status_code} {last_response.text[:500]}"
            )
        return last_response.headers.get("request-id") \
            or last_response.headers.get("client-request-id") \
            or ""

    def upload_to_onedrive(self, *, folder_id: str, local_path: Path) -> str:
        """Upload a file into Michael's OneDrive under the given folder.

        Returns the new item's webUrl (or id if webUrl is missing).

        Uses simple-upload PUT (good for files up to 250 MB; daily PDFs are
        a few MB at most). Pass ``folder_id`` from
        ``HILMAR_ONEDRIVE_FOLDER_ID``.

        Prefer :meth:`upload_to_onedrive_by_path` when wiring new
        callers — folder IDs go stale (move / rename / 404), paths
        survive both and auto-create on first use.
        """
        local_path = Path(local_path)
        if not local_path.exists():
            raise GraphError(f"upload_to_onedrive: {local_path} does not exist")

        url = f"{GRAPH_BASE}/me/drive/items/{folder_id}:/{local_path.name}:/content"
        data = self._request(
            "PUT", url,
            data=local_path.read_bytes(),
            headers={"Content-Type": _content_type_for(local_path)},
        )
        return data.get("webUrl") or data.get("id") or ""

    def upload_to_onedrive_by_path(self, *, folder_path: str, local_path: Path) -> str:
        """Upload a file into Michael's OneDrive at a path-based folder.

        Auto-creates intermediate folders. Survives folder rename/move
        (we re-resolve the path each upload), unlike the folder-id-based
        :meth:`upload_to_onedrive` which 404s when the target folder is
        deleted or moved.

        ``folder_path`` is relative to the drive root, with or without a
        leading slash (e.g. ``"Hilmar Tracker Reports"`` or
        ``"/Hilmar Tracker Reports/2026"``).
        Returns the new item's webUrl.
        """
        local_path = Path(local_path)
        if not local_path.exists():
            raise GraphError(f"upload_to_onedrive_by_path: {local_path} does not exist")
        normalized = "/" + folder_path.strip("/").strip()
        if not normalized.strip("/"):
            raise GraphError("upload_to_onedrive_by_path: folder_path is empty")
        # PUT /me/drive/root:/{path}/{file}:/content auto-creates the
        # folder hierarchy when missing — no explicit "create folder"
        # step needed for fresh paths. Documented Graph behavior since 2017.
        url = f"{GRAPH_BASE}/me/drive/root:{normalized}/{local_path.name}:/content"
        data = self._request(
            "PUT", url,
            data=local_path.read_bytes(),
            headers={"Content-Type": _content_type_for(local_path)},
        )
        return data.get("webUrl") or data.get("id") or ""


# ─────────────────────────────────────────────────────────────────────
# Console entry point: hilmar-auth-login
# ─────────────────────────────────────────────────────────────────────

def cli_main() -> int:
    """Console script `hilmar-auth-login` — run device-code flow once."""
    logging.basicConfig(
        level=os.environ.get("HILMAR_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    cache = os.environ.get("HILMAR_TOKEN_CACHE")
    client = GraphClient(token_cache_path=Path(cache) if cache else None)
    try:
        client.authenticate(interactive_ok=True)
    except GraphAuthError as e:
        sys.stderr.write(f"Auth failed: {e}\n")
        return 1
    sys.stdout.write(f"Token cache written to {client.token_cache_path}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(cli_main())
