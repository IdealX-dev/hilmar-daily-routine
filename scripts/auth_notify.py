"""auth_notify.py — re-seed the Graph token, and put the code where Michael is.

WHAT THIS IS FOR, since it was built for the wrong reason and kept for the
right one. It re-seeds the delegated token cache with the EXISTING scopes
(outlook_send.SCOPES — no admin consent, nothing new). That matters because
docs/MOVE-OFF-CLOUDPC.md says the cache was seeded once from the Cloud PC,
and the Cloud PC is now decommissioned: if the ~90-day refresh token ever
lapses, this is the only way back without it. That gap was real, undocumented,
and would have been discovered at the worst possible moment.

It does NOT widen access. Reading MBD_OceanExportBookingShared needs
Mail.Read.Shared, which needs ol-usa admin consent, which OL IT declined —
see refresh_stage.SHARED_MAILBOX for the constraint and the route that works
without them. I built this file believing otherwise on 2026-08-07; Michael:
"these requires ol's it department to approve.. you have had these details
before.. why recreate the wheel here."

2026-08-07. Michael, on being told to fetch a device code out of a GitHub
Actions log: "what am i doing from my phone and where.. you do it."

Fair. The sign-in itself cannot be delegated — device-code flow exists so that
only the person holding the credentials can complete it, and Microsoft does
not return `verification_uri_complete` for this client (checked against the
live endpoint: the flow dict has verification_uri and user_code and no
pre-filled variant), so the code must be typed somewhere. What CAN be removed
is the hunting: this emails the code to him the moment it exists, then blocks
until he approves.

Order matters and is the whole point:

    initiate flow  →  EMAIL THE CODE  →  block until approved  →  save cache

The email goes out BEFORE the blocking call, so the 15-minute clock starts
with the code already in his inbox instead of buried in a log he has to go
find. Sending uses the CURRENT token, which still works — outlook_send.SCOPES
is deliberately unchanged, so the silent refresh that sends the daily report
is the same one that sends this.

READS the token cache, WRITES it only on success. On failure or timeout the
existing cache is untouched, so an abandoned attempt costs nothing.

Usage:  python3 scripts/auth_notify.py --to michael.deitchman@ol-usa.com
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import msal

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import outlook_send as OS  # noqa: E402


def _body(code: str, uri: str, minutes: int) -> str:
    """Big code, one link, no prose above the fold.

    Outlook and Word ignore <style> and drop var()/flex/grid, so every rule
    here is an inline literal — the same constraint the daily report is built
    under (see gen_email.py).
    """
    return f"""\
<div style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;
            max-width:520px;margin:0 auto;padding:24px">
  <p style="margin:0 0 4px;font-size:13px;color:#5b6b7c">Hilmar tracker</p>
  <h2 style="margin:0 0 16px;font-size:19px;color:#1e3a5f;
             border-bottom:2px solid #1e3a5f;padding-bottom:6px">
    Reconnect the tracker</h2>

  <p style="margin:0 0 18px;font-size:14px;line-height:1.5;color:#243b53">
    Tap the button, enter the code, sign in as
    <strong>michael.deitchman@ol-usa.com</strong>. Takes about 30 seconds.
    Expires in {minutes} minutes.</p>

  <p style="margin:0 0 8px;font-size:12px;color:#5b6b7c">YOUR CODE</p>
  <p style="margin:0 0 20px;font-family:SFMono-Regular,Consolas,monospace;
            font-size:30px;font-weight:700;letter-spacing:4px;color:#1e3a5f;
            background-color:#eef3f8;background:#eef3f8;padding:14px 18px;
            border-radius:6px;text-align:center">{code}</p>

  <p style="margin:0 0 24px;text-align:center">
    <a href="{uri}" style="display:inline-block;background-color:#1e3a5f;
       background:#1e3a5f;color:#ffffff;text-decoration:none;font-size:15px;
       font-weight:600;padding:13px 30px;border-radius:6px">Open sign-in</a></p>

  <p style="margin:0;font-size:12px;line-height:1.6;color:#5b6b7c">
    This re-seeds the tracker's saved sign-in — the same permissions it
    already has (send mail, read your mail), nothing new and nothing needing
    IT. It is what lets the daily report keep going out now that the Cloud
    PC is gone.<br><br>
    Didn't expect this? Ignore it. The code is useless without your sign-in and
    expires on its own.</p>
</div>"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--to", action="append", required=True,
                    help="Where to send the code. Repeatable.")
    ap.add_argument("--dry", action="store_true",
                    help="Print the code, send nothing, wait for nothing.")
    args = ap.parse_args()

    cache = OS._load_cache()
    app = msal.PublicClientApplication(
        OS.CLIENT_ID,
        authority=f"https://login.microsoftonline.com/{OS.TENANT}",
        token_cache=cache,
    )

    # The token that SENDS the notification — acquired before the flow starts,
    # so a broken send fails fast instead of after the operator has already
    # signed in and is waiting on a job that cannot report back.
    send_token = None
    if not args.dry:
        accounts = app.get_accounts()
        if accounts:
            res = app.acquire_token_silent(OS.SCOPES, account=accounts[0])
            if res and "access_token" in res:
                send_token = res["access_token"]
        if send_token is None:
            print("::error::auth_notify: cannot acquire a token to SEND with. "
                  "The cached credential is dead, so there is no way to deliver "
                  "the code — run the auth workflow and read it from the log.")
            return 2

    flow = app.initiate_device_flow(scopes=OS.SCOPES)
    if "user_code" not in flow:
        print(f"::error::auth_notify: device flow failed to start: {flow}")
        return 2

    code = flow["user_code"]
    uri = flow.get("verification_uri") or "https://microsoft.com/devicelogin"
    minutes = max(1, int(flow.get("expires_in", 900)) // 60)
    print(f"auth_notify: code {code} at {uri} (expires in {minutes}m)")

    if args.dry:
        print("DRY — nothing sent, not waiting.")
        return 0

    try:
        rid = OS.send_mail(
            to=args.to,
            subject=f"Hilmar tracker — sign-in code {code}",
            html_body=_body(code, uri, minutes),
            token=send_token,
        )
        print(f"auth_notify: emailed {', '.join(args.to)} (request-id={rid})")
    except Exception as e:
        # Do NOT abort: the code is valid and printed above. A failed send
        # costs convenience, not the re-auth.
        print(f"::warning::auth_notify: could not email the code "
              f"({type(e).__name__}: {e}) — use the code printed above.")

    print(f"auth_notify: waiting up to {minutes}m for approval…")
    sys.stdout.flush()
    result = app.acquire_token_by_device_flow(flow)

    if "access_token" not in result:
        print(f"::error::auth_notify: not approved — "
              f"{result.get('error_description', result)}")
        return 1

    OS._save_cache(cache)
    who = result.get("id_token_claims", {}).get("preferred_username", "?")
    granted = result.get("scope") or ""
    print(f"auth_notify: authenticated as {who}")
    print(f"auth_notify: scopes granted: {granted}")
    missing = [s for s in OS.SCOPES if s.lower() not in granted.lower()]
    if missing:
        # Consent completed but short — a green run with a token that cannot
        # send is the one outcome worse than a failed one.
        print(f"::error::auth_notify: consent completed WITHOUT {missing} — "
              f"the pipeline cannot send with this token.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
