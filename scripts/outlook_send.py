"""
outlook_send.py — Standalone Outlook send via Microsoft Graph (device-code auth).

Usage:
  # Daily email
  python3 scripts/outlook_send.py daily \
      --to-from-config \
      --subject-from-file reports/email-subject.txt \
      --body-from-file reports/email-body.html \
      --attach reports/hilmar-dashboard.html reports/hilmar-report.pdf

  # Busan nudge
  python3 scripts/outlook_send.py nudge \
      --to MBD_OceanExportBookingShared@ol-usa.com \
      --cc michael.deitchman@ol-usa.com \
      --subject "RE: Oakland to Busan — internal pulse check" \
      --body-from-file reports/busan-nudge-body.html

Auth: MSAL device-code flow on Microsoft public client (14d82eec-...). Token
cache persisted to `secrets/token-cache.bin` (chmod 600). First run prints a
URL + code; user authenticates as michael.deitchman@ol-usa.com.

Scopes: Mail.Send Mail.Read Files.ReadWrite (delegated; no admin consent).
"""
from __future__ import annotations
import argparse
import base64
import json
import os
import sys
import time
from pathlib import Path

import msal
import requests

ROOT = Path(__file__).resolve().parent.parent
# 2026-05-06: renamed from .bin to .json so SharePoint indexes the file
# (M365 MCP can search-and-fetch JSON files; .bin extension is not indexed).
# Falls back to legacy .bin if .json doesn't exist yet.
_LEGACY_BIN = ROOT / "secrets" / "token-cache.bin"
TOKEN_CACHE_PATH = (ROOT / "secrets" / "token-cache.json") if (ROOT / "secrets" / "token-cache.json").exists() or not _LEGACY_BIN.exists() else _LEGACY_BIN
CLIENT_ID = "14d82eec-204b-4c2f-b7e8-296a70dab67e"  # Microsoft public client
TENANT = "common"
SCOPES = ["Mail.Send", "Mail.Read", "Files.ReadWrite"]
GRAPH = "https://graph.microsoft.com/v1.0"
INLINE_ATTACH_LIMIT = 3 * 1024 * 1024  # 3 MB safety under Graph's 4 MB hard cap


def _load_cache() -> msal.SerializableTokenCache:
    cache = msal.SerializableTokenCache()
    if TOKEN_CACHE_PATH.exists():
        cache.deserialize(TOKEN_CACHE_PATH.read_text())
    return cache


def _save_cache(cache: msal.SerializableTokenCache) -> None:
    if cache.has_state_changed:
        TOKEN_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        TOKEN_CACHE_PATH.write_text(cache.serialize())
        try:
            os.chmod(TOKEN_CACHE_PATH, 0o600)
        except OSError:
            pass  # Windows / OneDrive — best-effort


def get_token() -> str:
    """Return a valid access token. Silent refresh if possible, else device-code."""
    cache = _load_cache()
    app = msal.PublicClientApplication(
        CLIENT_ID,
        authority=f"https://login.microsoftonline.com/{TENANT}",
        token_cache=cache,
    )
    accounts = app.get_accounts()
    result = None
    if accounts:
        result = app.acquire_token_silent(SCOPES, account=accounts[0])
        if result:
            _save_cache(cache)
            return result["access_token"]

    # Device-code flow (interactive)
    flow = app.initiate_device_flow(scopes=SCOPES)
    if "user_code" not in flow:
        raise RuntimeError(f"Device flow failed: {flow}")
    print("\n" + "═" * 70, file=sys.stderr)
    print("DEVICE CODE LOGIN REQUIRED", file=sys.stderr)
    print("═" * 70, file=sys.stderr)
    print(flow["message"], file=sys.stderr)
    print("═" * 70 + "\n", file=sys.stderr)
    sys.stderr.flush()

    result = app.acquire_token_by_device_flow(flow)
    if "access_token" not in result:
        raise RuntimeError(f"Auth failed: {result.get('error_description', result)}")
    _save_cache(cache)
    print(f"✅ Authenticated as {result.get('id_token_claims', {}).get('preferred_username', '?')}", file=sys.stderr)
    return result["access_token"]


def _content_type_for(path: Path) -> str:
    suffix_map = {
        ".html": "text/html",
        ".htm": "text/html",
        ".pdf": "application/pdf",
        ".json": "application/json",
        ".txt": "text/plain",
        ".csv": "text/csv",
    }
    return suffix_map.get(path.suffix.lower(), "application/octet-stream")


def send_mail(*, to: list[str], subject: str, html_body: str,
              cc: list[str] | None = None, attachments: list[Path] | None = None,
              token: str | None = None) -> str:
    """Send via Graph /me/sendMail. Returns the request-id header for log correlation."""
    token = token or get_token()
    cc = cc or []
    attachments = attachments or []

    attach_payload = []
    total = 0
    for p in attachments:
        data = p.read_bytes()
        total += len(data)
        attach_payload.append({
            "@odata.type": "#microsoft.graph.fileAttachment",
            "name": p.name,
            "contentType": _content_type_for(p),
            "contentBytes": base64.b64encode(data).decode("ascii"),
        })
    if total > INLINE_ATTACH_LIMIT:
        raise RuntimeError(
            f"Attachments {total:,} bytes exceed inline cap {INLINE_ATTACH_LIMIT:,}. "
            "Use upload session (not implemented here — file separately)."
        )

    message = {
        "subject": subject,
        "body": {"contentType": "HTML", "content": html_body},
        "toRecipients": [{"emailAddress": {"address": a}} for a in to],
        "ccRecipients": [{"emailAddress": {"address": a}} for a in cc],
    }
    if attach_payload:
        message["attachments"] = attach_payload

    payload = {"message": message, "saveToSentItems": True}
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    r = requests.post(f"{GRAPH}/me/sendMail", headers=headers, json=payload, timeout=30)
    if r.status_code not in (200, 202):
        raise RuntimeError(f"Graph sendMail failed {r.status_code}: {r.text[:500]}")
    req_id = r.headers.get("request-id") or r.headers.get("client-request-id") or "?"
    return req_id


def _load_distribution_from_config() -> tuple[list[str], list[str]]:
    """Pull (full_list, daily_cc) from config.json."""
    cfg_path = ROOT / "config.json"
    cfg = json.loads(cfg_path.read_text())
    full = cfg.get("distribution", {}).get("full_list", [])
    cc = []  # the daily email already targets full_list with Michael in it
    return full, cc


def cmd_daily(args) -> int:
    subject = Path(args.subject_from_file).read_text(encoding="utf-8").strip()
    body = Path(args.body_from_file).read_text(encoding="utf-8")
    if args.to_from_config:
        to, cc = _load_distribution_from_config()
    else:
        to, cc = args.to or [], args.cc or []
    attach = [Path(p) for p in (args.attach or [])]

    # IDEMPOTENCY (added 2026-05-08 per Michael "why so many emails"):
    # Each daily-distribution send writes a flag file `reports/sent-YYYY-MM-DD.flag`.
    # If the flag already exists, this script REFUSES to send unless --force is
    # passed. Catches the case where MBD-TRAVEL fires + Cloud PC fires on the
    # same day, OR a human runs the script manually after a scheduled fire
    # already shipped. The flag distinguishes the two send shapes:
    #   sent-YYYY-MM-DD.flag           = full distribution (10 recipients)
    #   improvements-sent-YYYY-MM-DD.flag = idealx.us audit (1 recipient)
    from datetime import datetime as _dt
    is_full_distribution = bool(args.to_from_config) or (
        len(to) > 1 and any(addr.endswith("@ol-usa.com") for addr in (to or []))
    )
    is_audit = (len(to) == 1 and to and to[0].endswith("@idealx.us"))
    today = _dt.now().strftime("%Y-%m-%d")
    flag_name = "sent" if is_full_distribution else ("improvements-sent" if is_audit else None)
    flag_path = ROOT / "reports" / f"{flag_name}-{today}.flag" if flag_name else None
    if flag_path and flag_path.exists() and not getattr(args, "force", False):
        print(f"⛔ IDEMPOTENCY: {flag_path.name} already exists.")
        print(f"   Today's {flag_name} email already shipped:")
        for line in flag_path.read_text(encoding='utf-8').splitlines()[:6]:
            print(f"     {line}")
        print(f"   Pass --force to send anyway (will append a new entry to flag).")
        return 0

    print(f"→ TO ({len(to)}): {to}")
    print(f"→ CC ({len(cc)}): {cc}")
    print(f"→ SUBJECT: {subject}")
    print(f"→ BODY: {len(body):,} bytes from {args.body_from_file}")
    print(f"→ ATTACH: {[a.name for a in attach]}")
    if args.dry:
        print("DRY — not sending"); return 0
    req_id = send_mail(to=to, cc=cc, subject=subject, html_body=body, attachments=attach)
    print(f"✅ Sent. request-id={req_id}")

    if flag_path:
        flag_path.parent.mkdir(parents=True, exist_ok=True)
        existing = flag_path.read_text(encoding='utf-8') if flag_path.exists() else ""
        new_line = f"Sent {_dt.now().strftime('%Y-%m-%d %H:%M ET')} req={req_id} to={len(to)} recipient(s)\n"
        flag_path.write_text(existing + new_line, encoding="utf-8")
    return 0


def cmd_nudge(args) -> int:
    body = Path(args.body_from_file).read_text(encoding="utf-8") if args.body_from_file else args.body
    print(f"→ TO: {args.to}\n→ CC: {args.cc}\n→ SUBJECT: {args.subject}\n→ BODY: {len(body):,} bytes")
    if args.dry:
        print("DRY — not sending"); return 0
    req_id = send_mail(
        to=args.to or [], cc=args.cc or [],
        subject=args.subject, html_body=body,
    )
    print(f"✅ Sent. request-id={req_id}")
    return 0


def cmd_auth(args) -> int:
    """Just authenticate and cache the token. Useful first-run."""
    tok = get_token()
    print(f"✅ Token acquired ({len(tok)} chars). Cache: {TOKEN_CACHE_PATH}")
    return 0


def cmd_auth_bg(args) -> int:
    """Background auth: emit device code to a status file, poll until success."""
    cache = _load_cache()
    app = msal.PublicClientApplication(
        CLIENT_ID,
        authority=f"https://login.microsoftonline.com/{TENANT}",
        token_cache=cache,
    )
    accounts = app.get_accounts()
    status_path = ROOT / "secrets" / "auth-status.json"
    status_path.parent.mkdir(parents=True, exist_ok=True)

    if accounts:
        result = app.acquire_token_silent(SCOPES, account=accounts[0])
        if result:
            _save_cache(cache)
            status_path.write_text(json.dumps({"status": "ok", "method": "silent"}))
            print("silent ok")
            return 0

    flow = app.initiate_device_flow(scopes=SCOPES)
    if "user_code" not in flow:
        status_path.write_text(json.dumps({"status": "error", "message": str(flow)}))
        return 1
    pending = {
        "status": "pending",
        "user_code": flow["user_code"],
        "verification_uri": flow["verification_uri"],
        "message": flow["message"],
        "expires_at": int(time.time()) + flow.get("expires_in", 900),
    }
    status_path.write_text(json.dumps(pending))
    print(f"DEVICE_CODE={flow['user_code']} URL={flow['verification_uri']}")
    sys.stdout.flush()
    result = app.acquire_token_by_device_flow(flow)
    if "access_token" in result:
        _save_cache(cache)
        status_path.write_text(json.dumps({
            "status": "ok",
            "user": result.get("id_token_claims", {}).get("preferred_username"),
        }))
        return 0
    status_path.write_text(json.dumps({
        "status": "error",
        "message": result.get("error_description", str(result)),
    }))
    return 1




def main() -> int:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)

    pa = sub.add_parser("auth", help="Run device-code auth and cache token")
    pa.set_defaults(func=cmd_auth)

    pab = sub.add_parser("auth-bg", help="Background auth (writes status file)")
    pab.set_defaults(func=cmd_auth_bg)

    pd = sub.add_parser("daily", help="Send the daily Hilmar email")
    pd.add_argument("--to", nargs="+")
    pd.add_argument("--cc", nargs="+")
    pd.add_argument("--to-from-config", action="store_true")
    pd.add_argument("--subject-from-file", required=True)
    pd.add_argument("--body-from-file", required=True)
    pd.add_argument("--attach", nargs="*")
    pd.add_argument("--dry", action="store_true")
    pd.add_argument("--force", action="store_true",
                    help="Override idempotency flag (re-send even if today's flag exists)")
    pd.set_defaults(func=cmd_daily)

    pn = sub.add_parser("nudge", help="Send a one-off internal nudge")
    pn.add_argument("--to", nargs="+", required=True)
    pn.add_argument("--cc", nargs="*")
    pn.add_argument("--subject", required=True)
    pn.add_argument("--body-from-file")
    pn.add_argument("--body")
    pn.add_argument("--dry", action="store_true")
    pn.set_defaults(func=cmd_nudge)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
