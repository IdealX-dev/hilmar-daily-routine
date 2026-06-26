"""app_auth.py — App-only (client credentials) Microsoft Graph auth.

Today's auth uses MSAL device-code flow against a real user account.
Tokens live on disk (`secrets/token-cache.bin`, non-indexed) and need re-acquisition
roughly every 80 days — QC-023 warns at 60d, errors at 80d. When the cache
expires there's nobody to re-auth on the Cloud PC and the pipeline silently
fails refresh_stage.

App-only auth fixes that AND the bigger structural problem (a daily,
unattended pipeline shouldn't own a human's mailbox token). The trade-off:

  device-code:   acts as the user, has the user's mailbox by default,
                 token cache on disk, expires.
  app-only:      acts as a registered app, must be explicitly granted
                 access to specific mailboxes via Application Access
                 Policy, NO token cache (acquires fresh per-process),
                 NEVER expires (just rotate the client secret).

For the Hilmar pipeline, app-only is the right model because:
  - It only needs to read Lonny ↔ MBD threads (one shared mailbox)
  - It runs unattended
  - It should NOT be able to read any other mailbox in the tenant

DEPLOYMENT REQUIREMENTS (handled by OL IT, not this code):

  1. Register a new app in Entra ID: "Hilmar Daily Tracker (app-only)"
  2. Grant Application permission (NOT Delegated): Mail.Read.Shared
  3. Admin consent the permission
  4. Apply an Application Access Policy scoping the app to a single
     mailbox: `MBD_OceanExportBookingShared@ol-usa.com`
     (PowerShell: New-ApplicationAccessPolicy ...)
  5. Generate a client secret (or upload a certificate — preferred for
     production), store as GRAPH_APP_CLIENT_SECRET in the runtime
     environment (GH Actions secrets, Azure Key Vault, etc.)
  6. Set GRAPH_APP_TENANT_ID + GRAPH_APP_CLIENT_ID in env too.

When all three env vars are present, GraphClient picks the app-only path.
Otherwise it falls back to the existing device-code flow.

WHY NO CACHE FILE: ConfidentialClientApplication acquires fresh client
credentials each call (cheap — no user interaction), so persisting tokens
adds no value and one MORE file on disk to manage.

See docs/MOVE-OFF-CLOUDPC.md for the full migration sequence.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass

log = logging.getLogger(__name__)

#: Standard Graph scope for app-only client credentials — always ".default"
#: per Microsoft's docs (the granted Application permissions are implicit).
APP_ONLY_SCOPE = ["https://graph.microsoft.com/.default"]

#: Env var names. Kept as constants so tests can monkeypatch a single source.
ENV_TENANT_ID = "GRAPH_APP_TENANT_ID"
ENV_CLIENT_ID = "GRAPH_APP_CLIENT_ID"
ENV_CLIENT_SECRET = "GRAPH_APP_CLIENT_SECRET"


@dataclass(frozen=True)
class AppOnlyCredentials:
    """The three values needed to acquire an app-only Graph token."""
    tenant_id: str
    client_id: str
    client_secret: str


def app_only_credentials_from_env() -> AppOnlyCredentials | None:
    """Return AppOnlyCredentials if ALL three env vars are set; else None.

    Returning None is the explicit signal to GraphClient that app-only
    isn't configured on this host — fall back to device-code. This is the
    contract that lets the same code run on the Cloud PC (no env vars,
    device-code wins) and in GH Actions (env vars set, app-only wins)
    without a config flag.
    """
    tenant = os.environ.get(ENV_TENANT_ID)
    client = os.environ.get(ENV_CLIENT_ID)
    secret = os.environ.get(ENV_CLIENT_SECRET)
    if tenant and client and secret:
        return AppOnlyCredentials(tenant_id=tenant, client_id=client, client_secret=secret)
    return None


def is_app_only_configured() -> bool:
    """Cheap predicate for telemetry / audit display."""
    return app_only_credentials_from_env() is not None


def acquire_app_only_token(
    creds: AppOnlyCredentials,
    *,
    scopes: list[str] | None = None,
) -> str:
    """Acquire an access token using MSAL client credentials flow.

    Returns the bare access_token string. Raises RuntimeError on any
    failure (MSAL error response, network problem, invalid credentials)
    — the caller (GraphClient.authenticate) translates this into the
    project's GraphAuthError so callers don't have to know about MSAL.
    """
    # Local import so the module loads cleanly in environments without
    # msal installed (e.g. minimal scripts using only the dataclass).
    import msal

    app = msal.ConfidentialClientApplication(
        client_id=creds.client_id,
        authority=f"https://login.microsoftonline.com/{creds.tenant_id}",
        client_credential=creds.client_secret,
    )
    result = app.acquire_token_for_client(scopes=scopes or APP_ONLY_SCOPE)
    if not isinstance(result, dict):
        raise RuntimeError(f"MSAL returned non-dict response: {type(result).__name__}")
    if "access_token" in result:
        log.info("Acquired app-only Graph token (expires_in=%s)", result.get("expires_in"))
        return result["access_token"]
    # Error case — surface MSAL's error detail
    raise RuntimeError(
        f"Client credentials auth failed: "
        f"{result.get('error')}: {result.get('error_description', '')[:200]}"
    )
