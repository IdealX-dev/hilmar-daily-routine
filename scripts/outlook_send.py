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
import contextlib
import json
import os
import sys
import time
from pathlib import Path

import msal
import requests

ROOT = Path(__file__).resolve().parent.parent
# SECURITY (2026-06-26): the token cache holds a LIVE delegated OAuth refresh
# token (Mail.Send/Read, Files.ReadWrite). It must NEVER be search-indexed. The
# 2026-05-06 ".json so SharePoint indexes it" change made the credential
# discoverable via tenant search and is REVERTED here. Canonical is the
# non-indexed .bin. We still READ a legacy .json if it is the ONLY cache present
# (so a mid-migration box can't break the fire), but we always WRITE .bin.
# OPERATOR migration: once secrets/token-cache.bin exists, delete
# secrets/token-cache.json (local AND the Azure Blob state store) and ROTATE the
# delegated token — the old .json one must be treated as potentially exposed.
_CACHE_BIN = ROOT / "secrets" / "token-cache.bin"
_CACHE_JSON_LEGACY = ROOT / "secrets" / "token-cache.json"
#: Canonical (WRITE) path — always the non-indexed .bin.
TOKEN_CACHE_PATH = _CACHE_BIN


def _token_cache_read_path() -> Path:
    """Prefer the non-indexed .bin; fall back to a legacy .json only when it is
    the sole cache present, so a box still mid-migration keeps authenticating."""
    if _CACHE_BIN.exists():
        return _CACHE_BIN
    if _CACHE_JSON_LEGACY.exists():
        return _CACHE_JSON_LEGACY
    return _CACHE_BIN
CLIENT_ID = "14d82eec-204b-4c2f-b7e8-296a70dab67e"  # Microsoft public client
TENANT = "common"
SCOPES = ["Mail.Send", "Mail.Read", "Files.ReadWrite"]
GRAPH = "https://graph.microsoft.com/v1.0"
INLINE_ATTACH_LIMIT = 3 * 1024 * 1024  # 3 MB safety under Graph's 4 MB hard cap

#: Mailbox the app-only path sends AS. App-only auth has no /me context, so
#: it must POST to /users/{mailbox}/sendMail. Defaults to the OL responder
#: shared mailbox (the same one ingest reads); override via env on the
#: Cloud PC if outbound should originate elsewhere. The app's Application
#: Access Policy must grant Mail.Send.Shared scoped to this mailbox.
SEND_MAILBOX = os.environ.get("HILMAR_SEND_MAILBOX", "MBD_OceanExportBookingShared@ol-usa.com")

# Every verification/test send carries this. The mailbox guard keys on EXACT
# subject, so an untagged test copy is indistinguishable from the real thing
# and silently consumes its idempotency — see cmd_daily's --verification
# handling for the two live incidents this prevents. Trailing space is part
# of it: the subject reads "[VERIFY] Hilmar — ...".
VERIFY_PREFIX = "[VERIFY] "


def _app_only_send_context():
    """Return (token, send_url) when app-only Graph auth is configured via
    the GRAPH_APP_* env vars, else None.

    This is what makes outbound work off the Cloud PC (GH Actions): there's
    no signed-in user, so /me/sendMail is unavailable — send as the shared
    mailbox via /users/{mailbox}/sendMail using an app-only token. When the
    env vars are unset (Cloud PC today), returns None and the caller uses
    the existing device-code + /me path unchanged.
    """
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
        from hilmar.app_auth import (
            acquire_app_only_token,
            app_only_credentials_from_env,
        )
    except Exception:
        return None
    creds = app_only_credentials_from_env()
    if creds is None:
        return None
    token = acquire_app_only_token(creds)
    return token, f"{GRAPH}/users/{SEND_MAILBOX}/sendMail"


def _load_cache() -> msal.SerializableTokenCache:
    cache = msal.SerializableTokenCache()
    read_path = _token_cache_read_path()
    if read_path.exists():
        cache.deserialize(read_path.read_text(encoding="utf-8"))
    return cache


def _save_cache(cache: msal.SerializableTokenCache) -> None:
    if cache.has_state_changed:
        TOKEN_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        TOKEN_CACHE_PATH.write_text(cache.serialize(), encoding="utf-8")
        with contextlib.suppress(OSError):
            os.chmod(TOKEN_CACHE_PATH, 0o600)  # Windows / OneDrive — best-effort


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

    # Device-code flow (interactive). On a headless runner this would print
    # a code and block until the job times out — fail loudly instead.
    if os.environ.get("HILMAR_NONINTERACTIVE"):
        raise RuntimeError(
            "Silent token refresh failed and HILMAR_NONINTERACTIVE is set — "
            "refusing to start device-code flow on a headless runner. Re-seed "
            "the token cache from the Cloud PC (state_store.py push)."
        )
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
    """Send via Graph sendMail. Returns the request-id header for log correlation.

    Endpoint selection:
      - explicit token passed in        → /me/sendMail (caller owns auth)
      - GRAPH_APP_* env set (app-only)   → /users/{SEND_MAILBOX}/sendMail
      - otherwise (device-code, Cloud PC)→ /me/sendMail
    """
    if token is not None:
        send_url = f"{GRAPH}/me/sendMail"
    else:
        _appctx = _app_only_send_context()
        if _appctx is not None:
            token, send_url = _appctx
        else:
            token = get_token()
            send_url = f"{GRAPH}/me/sendMail"
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

    # Inline-CID logo embedding — auto-attach the Hilmar logo PNG with
    # contentId so the email body's <img src="cid:hilmar-logo"> resolves
    # at delivery time. Outlook ALWAYS renders inline-CID images regardless
    # of external-image / data-URI blocking — which is why gen_email.py
    # switched from data: URIs (blocked) to CID (renders).
    # Per Michael 2026-05-17 ("hilmar logo not showing up").
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import branding
        logo_path = branding.logo_png_path()
        if logo_path and ("cid:" + branding.LOGO_CID) in (html_body or ""):
            logo_data = logo_path.read_bytes()
            total += len(logo_data)
            attach_payload.append({
                "@odata.type": "#microsoft.graph.fileAttachment",
                "name": logo_path.name,
                "contentType": "image/png",
                "contentBytes": base64.b64encode(logo_data).decode("ascii"),
                "contentId": branding.LOGO_CID,
                "isInline": True,
            })
    except Exception as _e:
        # Never let logo-attach failure break the send. Worst case: email
        # arrives without inline logo image (header still has fallback text).
        print(f"⚠️  logo CID attach skipped: {type(_e).__name__}: {_e}")

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
    r = requests.post(send_url, headers=headers, json=payload, timeout=30)

    # Sentry metric: success/failure counter tagged by recipient type
    # (full distribution / audit / test) so the dashboard shows which
    # send channel is failing if there are any.
    _recipient_type = "full" if len(to) >= 5 else (
        "audit" if (len(to) == 1 and to[0].endswith("@idealx.us")) else "test"
    )
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import sentry_setup as _sentry
        if r.status_code in (200, 202):
            _sentry.metric_increment(
                "send.success", 1,
                recipient_type=_recipient_type,
                attach_count=str(len(attach_payload)),
            )
        else:
            _sentry.metric_increment(
                "send.failure", 1,
                recipient_type=_recipient_type,
                status_code=str(r.status_code),
            )
    except Exception:
        pass  # observability never breaks the send

    if r.status_code not in (200, 202):
        raise RuntimeError(f"Graph sendMail failed {r.status_code}: {r.text[:500]}")
    req_id = r.headers.get("request-id") or r.headers.get("client-request-id") or "?"
    return req_id


def _load_distribution_from_config() -> tuple[list[str], list[str]]:
    """Pull (full_list, daily_cc) from config.json."""
    cfg_path = ROOT / "config.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    full = cfg.get("distribution", {}).get("full_list", [])
    cc = []  # the daily email already targets full_list with Michael in it
    return full, cc


def _sent_today_in_mailbox(subject: str) -> str | None:
    """Return the sentDateTime if a message with this exact subject already
    left this account today (ET midnight onward), else None.

    Best-effort BY DESIGN: any failure returns None — a Graph hiccup must
    never block the real client send. Subject comparison happens client-side
    to dodge $filter string-escaping (the subject contains an em-dash).
    """
    try:
        appctx = _app_only_send_context()
        if appctx is not None:
            token, base = appctx[0], f"{GRAPH}/users/{SEND_MAILBOX}"
        else:
            token, base = get_token(), f"{GRAPH}/me"
        from datetime import datetime as _dt2
        from datetime import time as _time
        from zoneinfo import ZoneInfo as _zi2
        _et = _zi2("America/New_York")
        midnight_et = _dt2.combine(_dt2.now(_et).date(), _time.min, tzinfo=_et)
        since = midnight_et.astimezone(_zi2("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ")
        r = requests.get(
            f"{base}/mailFolders/sentitems/messages",
            params={
                "$filter": f"sentDateTime ge {since}",
                "$orderby": "sentDateTime desc",
                "$select": "subject,sentDateTime",
                "$top": "100",
            },
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
        )
        if r.status_code != 200:
            print(f"⚠️  mailbox guard: Graph → {r.status_code}; proceeding unguarded")
            return None
        for m in r.json().get("value", []):
            if (m.get("subject") or "").strip() == subject.strip():
                return m.get("sentDateTime")
        return None
    except Exception as e:
        print(f"⚠️  mailbox guard: check failed ({type(e).__name__}: {e}); proceeding unguarded")
        return None


def _flag_date(now_et) -> str:
    """The YYYY-MM-DD the daily idempotency flags are keyed to: the REPORT
    business day (core.report_business_day), not the wall-clock calendar day.

    They only differ off-hours, and that's exactly when it matters: a
    12:38 AM Thursday fire REPORTS Wednesday (core's wee-hours rule), so its
    flag must be Wednesday's — deduping against Wednesday-evening's real send
    — instead of writing Thursday's flag and blocking Thursday's real evening
    send before Thursday even happened (live failure, run #76). Falls back to
    the calendar day if core can't import (minimal auth-only environments)."""
    try:
        import core as _core
        return _core.report_business_day(now_et).strftime("%Y-%m-%d")
    except Exception:
        return now_et.strftime("%Y-%m-%d")


def cmd_daily(args) -> int:
    subject = Path(args.subject_from_file).read_text(encoding="utf-8").strip()
    body = Path(args.body_from_file).read_text(encoding="utf-8")

    # VERIFICATION SENDS MUST BE DISTINGUISHABLE IN THE MAILBOX.
    #
    # The mailbox guard below dedupes on EXACT SUBJECT across hosts. A test
    # send that reuses the real subject therefore consumes the real send's
    # guard: the staff run finds "already sent today", returns 0, and writes a
    # flag recording a delivery that never happened. That is not theory —
    # 2026-07-30 a verification fire blocked the real staff send, and on
    # 2026-08-04 the 21:04 catch-up preview blocked its own staff run.
    #
    # --force alone does NOT fix it: forcing the real send past the guard
    # means the guard is off for the send that actually matters. The fix is to
    # make a test copy a DIFFERENT message. The prefix travels with
    # --force/--no-flag as one flag so the three can never be half-applied.
    if getattr(args, "verification", False):
        args.force = True
        args.no_flag = True
        if not subject.startswith(VERIFY_PREFIX):
            subject = f"{VERIFY_PREFIX}{subject}"
        print(f"🔎 VERIFICATION SEND — subject tagged '{VERIFY_PREFIX.strip()}', "
              f"idempotency untouched. It cannot consume a real send's guard.")
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
    from zoneinfo import ZoneInfo as _zi
    is_full_distribution = bool(args.to_from_config) or (
        len(to) > 1 and any(addr.endswith("@ol-usa.com") for addr in (to or []))
    )
    is_audit = (len(to) == 1 and to and to[0].endswith("@idealx.us"))
    # Flag dates/timestamps are ET, explicitly — the operational day of the
    # 6 PM ET fire. A GH Actions runner's clock is UTC; bare .now() there
    # would date the flag wrong after 8 PM ET and stamp UTC times labeled
    # "ET". Keyed to the REPORT business day via _flag_date (wee-hours rule)
    # so an after-midnight fire dedupes against the evening it belongs to.
    # state_store.py syncs the same report-day flags.
    _now_et = _dt.now(_zi("America/New_York"))
    today = _flag_date(_now_et)
    flag_name = "sent" if is_full_distribution else ("improvements-sent" if is_audit else None)
    # --flag-name overrides the derived name so a NEW send shape can get its
    # own idempotency namespace (the client-facing email uses "client-sent" —
    # it must never share a flag with the staff distribution or the audit).
    # Absent, behavior is exactly as before. --no-flag still wins below.
    if getattr(args, "flag_name", None):
        flag_name = args.flag_name
    if getattr(args, "no_flag", False):
        # Verification/test sends must never touch production idempotency
        # state: don't let a flag block the send, don't write one after.
        flag_name = None
    flag_path = ROOT / "reports" / f"{flag_name}-{today}.flag" if flag_name else None
    if flag_path and flag_path.exists() and not getattr(args, "force", False):
        print(f"⛔ IDEMPOTENCY: {flag_path.name} already exists.")
        print(f"   Today's {flag_name} email already shipped:")
        for line in flag_path.read_text(encoding='utf-8').splitlines()[:6]:
            print(f"     {line}")
        print("   Pass --force to send anyway (will append a new entry to flag).")
        return 0

    # MACHINE-INDEPENDENT GUARD (2026-06-11): flag files only protect hosts
    # that share a disk or the blob store. The day after the GH Actions
    # cutover, a forgotten scheduler on MBD-TRAVEL fired at 10:02 ET — its
    # flag synced to OneDrive, invisible to the GH runner, which would have
    # sent the client email AGAIN at 10:07. The mailbox itself is the one
    # shared source of truth: before any full-distribution send, ask Graph
    # whether a message with this exact subject already left the account
    # today. Best-effort — a Graph hiccup must never block the real send.
    # The client-facing send (--flag-name client-sent) goes to an EXTERNAL
    # client recipient, so it gets the same mailbox guard as the staff
    # distribution — a double-send to the client is worse than one to staff.
    # Its subject differs from the staff subject, so the Graph lookup keys on
    # the right message. (Samples use --force --no-flag and skip this.)
    if (is_full_distribution or flag_name == "client-sent") \
            and not getattr(args, "force", False):
        prior = _sent_today_in_mailbox(subject)
        if prior:
            print(f"⛔ MAILBOX GUARD: '{subject}' already sent today at {prior}")
            print("   (sent by another machine — flags can't see across hosts).")
            print("   Pass --force to send anyway.")
            # Record a local sent-flag for today even though THIS host didn't
            # emit: the client email demonstrably shipped (Graph confirms a
            # message with this subject left the mailbox today). Without this,
            # assert_fire_integrity sees no flag on the guarded host and screams
            # a false "no verified report shipped" page after a real delivery.
            # The marker keeps the per-day flag's invariant honest — "a flag
            # for today means the email shipped today by SOME host" — without
            # weakening idempotency (it's the same per-day flag, so a later
            # same-day run still no-ops).
            if flag_path and not flag_path.exists():
                try:
                    flag_path.parent.mkdir(parents=True, exist_ok=True)
                    marker = (
                        f"Sent (mailbox guard: already sent by another host) "
                        f"{_dt.now(_zi('America/New_York')).strftime('%Y-%m-%d %H:%M ET')} "
                        f"prior={prior}\n"
                    )
                    flag_path.write_text(marker, encoding="utf-8")
                except Exception as _e:
                    print(f"   (could not write cross-host marker flag: {_e})")
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
        new_line = f"Sent {_dt.now(_zi('America/New_York')).strftime('%Y-%m-%d %H:%M ET')} req={req_id} to={len(to)} recipient(s)\n"
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
            status_path.write_text(json.dumps({"status": "ok", "method": "silent"}), encoding="utf-8")
            print("silent ok")
            return 0

    flow = app.initiate_device_flow(scopes=SCOPES)
    if "user_code" not in flow:
        status_path.write_text(json.dumps({"status": "error", "message": str(flow)}), encoding="utf-8")
        return 1
    pending = {
        "status": "pending",
        "user_code": flow["user_code"],
        "verification_uri": flow["verification_uri"],
        "message": flow["message"],
        "expires_at": int(time.time()) + flow.get("expires_in", 900),
    }
    status_path.write_text(json.dumps(pending), encoding="utf-8")
    print(f"DEVICE_CODE={flow['user_code']} URL={flow['verification_uri']}")
    sys.stdout.flush()
    result = app.acquire_token_by_device_flow(flow)
    if "access_token" in result:
        _save_cache(cache)
        status_path.write_text(json.dumps({
            "status": "ok",
            "user": result.get("id_token_claims", {}).get("preferred_username"),
        }), encoding="utf-8")
        return 0
    status_path.write_text(json.dumps({
        "status": "error",
        "message": result.get("error_description", str(result)),
    }), encoding="utf-8")
    return 1




def main() -> int:
    # Sentry observability — silent no-op if not configured
    try:
        import sentry_setup
        sentry_setup.init(component="outlook_send")
    except ImportError:
        pass

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
    pd.add_argument("--no-flag", action="store_true",
                    help="Don't read or write the idempotency flag (verification/test "
                         "sends must never touch production send state)")
    pd.add_argument("--flag-name",
                    help="Override the derived idempotency flag name so a distinct "
                         "send shape gets its own namespace (e.g. 'client-sent' for "
                         "the client-facing email). Absent = derived as before.")
    pd.add_argument("--verification", action="store_true",
                    help="This is a TEST send. Prefixes the subject with "
                         f"'{VERIFY_PREFIX.strip()}' and implies --force --no-flag. "
                         "Use it for every send_to=test path — see VERIFY_PREFIX.")
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
