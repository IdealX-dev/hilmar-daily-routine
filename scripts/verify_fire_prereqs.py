"""verify_fire_prereqs.py — validate the production-fire secrets BEFORE the
pipeline touches anything.

The workflow's first step checks the secrets are non-EMPTY, but the
2026-06-10 verification fires proved that's not enough: a wrong-field paste
(storage Key instead of Connection string, a non-GUID tenant id) sails
through the empty-check and dies mid-run as a cryptic SDK traceback. Each
check here does a real, cheap validation and prints exactly which secret is
wrong and where the right value lives. Read-only: no state is touched, no
email is sent, nothing is written.

Usage (in the GH Actions production-fire job, right after pip install):
    python scripts/verify_fire_prereqs.py
Exit 0 = all good; exit 1 = at least one named failure.
"""
from __future__ import annotations

import os
import re
import sys

OK = "OK  "
BAD = "FAIL"

GUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def check_client_id_shape(client_id: str) -> tuple[bool, str]:
    """AADSTS90013 ('Invalid input received from the user', live failure
    2026-06-10 run 27313454382) is what Azure returns when the client_id in
    the token request isn't a GUID at all — catch it before MSAL does."""
    if GUID_RE.match(client_id.strip()):
        return True, "GRAPH_APP_CLIENT_ID is a well-formed GUID"
    return False, (
        "GRAPH_APP_CLIENT_ID is not a GUID. Copy 'Application (client) ID' "
        "from Azure Portal > App registrations > the Hilmar app > Overview "
        "(NOT the app's display name, and not the 'Object ID')."
    )


def check_tenant(tenant: str, *, http_get=None) -> tuple[bool, str]:
    """A tenant id is valid iff Microsoft's public discovery endpoint
    resolves it. Accepts a GUID or a verified domain (e.g. ol-usa.com)."""
    if http_get is None:
        import requests

        def http_get(url):
            return requests.get(url, timeout=15)

    url = f"https://login.microsoftonline.com/{tenant.strip()}/v2.0/.well-known/openid-configuration"
    try:
        r = http_get(url)
    except Exception as e:
        return False, f"GRAPH_APP_TENANT_ID: could not reach login.microsoftonline.com ({e})"
    if r.status_code == 200:
        return True, "GRAPH_APP_TENANT_ID resolves to a real tenant"
    return False, (
        "GRAPH_APP_TENANT_ID is not a valid tenant GUID or domain "
        f"(discovery returned HTTP {r.status_code}). Find it at Azure Portal > "
        "App registrations > the Hilmar app > Overview > 'Directory (tenant) ID'. "
        "It must be the tenant the app is REGISTERED in (the mailbox's tenant, "
        "ol-usa.com — its GUID is publicly e8bc0287-e74f-47f1-a572-d83d32d60622); "
        "the verified domain itself also works."
    )


def check_client_credentials() -> tuple[bool, str]:
    """One real token acquisition — catches unknown client id, bad/expired
    secret, and missing admin consent, each with Microsoft's AADSTS code."""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
    from hilmar.app_auth import acquire_app_only_token, app_only_credentials_from_env

    creds = app_only_credentials_from_env()
    if creds is None:
        return False, "GRAPH_APP_TENANT_ID / _CLIENT_ID / _CLIENT_SECRET: not all set"
    try:
        acquire_app_only_token(creds)
    except Exception as e:
        msg = str(e)
        hint = ""
        if "AADSTS700016" in msg:
            hint = (" — the CLIENT_ID is not a registered app in this tenant "
                    "(check 'Application (client) ID' on the app's Overview page, "
                    "and that TENANT_ID is the tenant the app actually lives in)")
        elif "AADSTS7000215" in msg or "invalid_client" in msg.lower():
            hint = (" — the CLIENT_SECRET is wrong or expired (Certificates & "
                    "secrets > New client secret > copy the Value column, not the ID)")
        elif "AADSTS500011" in msg or "consent" in msg.lower():
            hint = " — the app's Application permissions may lack admin consent"
        elif "AADSTS90013" in msg:
            hint = (" — a malformed value in the request, usually a CLIENT_ID "
                    "that isn't a GUID, or stray whitespace/newline in a secret")
        return False, f"App-only token acquisition failed: {msg}{hint}"
    return True, "GRAPH_APP_* credentials acquire a token"


def check_delegated_cache() -> tuple[bool, str]:
    """The no-IT auth path (OL declined to register an app-only Entra app,
    2026-06-10): the device-code token cache synced through the blob store.
    A real acquire_token_silent here is also the Conditional Access verdict —
    if OL's tenant blocks token refresh from non-corporate IPs, THIS is
    where it shows up, in plain English, before anything fires."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import msal
    import outlook_send as OS

    if not OS.TOKEN_CACHE_PATH.exists():
        return False, (
            f"No delegated token cache at {OS.TOKEN_CACHE_PATH.name}. Seed it once "
            "from the Cloud PC: set AZURE_STORAGE_CONNECTION_STRING and run "
            "`python scripts/state_store.py push` (uploads the existing cache "
            "+ pipeline state to the blob store)."
        )
    cache = OS._load_cache()
    app = msal.PublicClientApplication(
        OS.CLIENT_ID, authority=f"https://login.microsoftonline.com/{OS.TENANT}",
        token_cache=cache)
    accounts = app.get_accounts()
    if not accounts:
        return False, "Token cache present but holds no account — re-seed from the Cloud PC."
    result = app.acquire_token_silent(OS.SCOPES, account=accounts[0])
    if result and "access_token" in result:
        OS._save_cache(cache)
        return True, f"Delegated token refresh works (account: {accounts[0].get('username', '?')})"
    return False, (
        "Silent token refresh FAILED from this runner. If the same cache works "
        "on the Cloud PC, OL's Conditional Access is likely rejecting sign-ins "
        "from GitHub's IP ranges — off-Cloud-PC firing then needs OL to either "
        "register the app-only Entra app or exempt this workload. "
        f"MSAL detail: {result!r}"
    )


def check_storage(conn: str) -> tuple[bool, str]:
    """Parse + ping the storage account. Catches the Key-instead-of-
    Connection-string paste and a revoked/typo'd AccountKey."""
    try:
        from azure.storage.blob import BlobServiceClient
    except Exception as e:
        return False, f"azure-storage-blob not installed: {e}"
    try:
        svc = BlobServiceClient.from_connection_string(conn)
    except ValueError:
        return False, (
            "AZURE_STORAGE_CONNECTION_STRING is not a connection string. It must "
            "be the FULL 'Connection string' field from Storage account > Access "
            "keys (starts with 'DefaultEndpointsProtocol=https;AccountName='), "
            "not the bare Key."
        )
    try:
        svc.get_service_properties()
    except Exception as e:
        return False, (
            f"Storage account unreachable with this connection string ({type(e).__name__}: {e}). "
            "Re-copy it from Access keys (the key may have been rotated)."
        )
    return True, "AZURE_STORAGE_CONNECTION_STRING parses and the account responds"


def main() -> int:
    results: list[tuple[bool, str]] = []

    # Plain non-empty checks first (no network).
    for var in ("SENTRY_DSN", "ANTHROPIC_API_KEY"):
        val = os.environ.get(var, "")
        results.append((bool(val), f"{var} is {'set' if val else 'NOT set'}"))
    dsn = os.environ.get("SENTRY_DSN", "")
    if dsn and not (dsn.startswith("https://") and "@" in dsn):
        results.append((False, "SENTRY_DSN doesn't look like a DSN (expected https://<key>@<org>.ingest...)"))

    # Graph auth — either mode works; exactly one must validate:
    #   app-only (GRAPH_APP_* secrets)         — requires the OL Entra app
    #   delegated (token cache via blob store) — the no-IT path
    tenant = os.environ.get("GRAPH_APP_TENANT_ID", "")
    client_id = os.environ.get("GRAPH_APP_CLIENT_ID", "")
    secret = os.environ.get("GRAPH_APP_CLIENT_SECRET", "")
    if tenant or client_id or secret:
        if not tenant:
            results.append((False, "GRAPH_APP_TENANT_ID is NOT set (other GRAPH_APP_* are)"))
        else:
            results.append(check_tenant(tenant))
        if not client_id:
            results.append((False, "GRAPH_APP_CLIENT_ID is NOT set (other GRAPH_APP_* are)"))
        else:
            results.append(check_client_id_shape(client_id))
        # Only try a real token once tenant + client-id shape pass — otherwise
        # MSAL throws the unhelpful errors this script exists to replace.
        if tenant and client_id and all(ok for ok, _ in results[-2:]):
            results.append(check_client_credentials())
    else:
        results.append(check_delegated_cache())

    conn = os.environ.get("AZURE_STORAGE_CONNECTION_STRING", "")
    if not conn:
        results.append((False, "AZURE_STORAGE_CONNECTION_STRING is NOT set"))
    else:
        results.append(check_storage(conn))

    failed = 0
    for ok, msg in results:
        print(f"{OK if ok else BAD}  {msg}")
        if not ok:
            failed += 1
    if failed:
        print(f"\n{failed} prerequisite(s) failed — fix the named secret(s) in "
              "Settings > Secrets and variables > Actions, then re-run.")
        return 1
    print("\nAll prerequisites validated — the fire can proceed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
