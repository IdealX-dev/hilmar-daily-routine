"""refresh_stage.py — Fetch new Hilmar-relevant emails via Microsoft Graph
and append to stage_emails.jsonl + stage_emails_bodies.jsonl.

Replaces what the Cowork-side `outlook_email_search` MCP previously did. Now
the laptop's Task Scheduler can run end-to-end without Claude Code being open.

Auth: reuses the MSAL token cache that outlook_send.py already established
(secrets/token-cache.bin). Silent refresh only — never prompts device-code
from a non-interactive context.

Search strategy: TWO Graph $search queries to make sure we catch both sides
of the conversation:

  Q1 (rate-request flow):
    "from:lupfold@hilmaringredients.com" OR "to:lupfold@hilmaringredients.com"
  Q2 (booking-confirmation flow — closes the structural gap where MDOLX
      bookings go to carriers, with Lonny only on CC):
    "from:MBD_OceanExportBookingShared@ol-usa.com AND HILMAR"

We $search rather than $filter because Graph rejects toRecipients lambdas
combined with $filter clauses (production VM ate this in 2026-04-27 — see
hilmar-tracker/src/hilmar/graph_client.py:404-451 for the post-mortem).

Bucket classification (sender + subject heuristics, from empirical
inspection of the 188-record corpus produced by the Cowork MCP):

  sender = lupfold@hilmaringredients.com:
    subject ^(Re|Fw|Fwd):  → lonny_reply
    else                   → lonny_outbound

  sender = MBD_OceanExportBookingShared@ol-usa.com:
    subject ^Re: <known origin> to  → mbd_rate_response
    else                        → mbd_inbound

  any other sender → drop (excluded mailboxes, off-topic, etc.)

Output is append-only — dedupe key is the Graph message `id` for stage_emails
and `imid` (internetMessageId) for stage_emails_bodies. Existing records are
never rewritten by this tool; ingest.py's idempotency depends on stable ids.

Usage:
  python scripts/refresh_stage.py                    # full run, last 14 days
  python scripts/refresh_stage.py --days-back 30
  python scripts/refresh_stage.py --since 2026-04-01
  python scripts/refresh_stage.py --dry              # show what'd be added, don't write
  python scripts/refresh_stage.py --verbose          # per-message classification trace
  python scripts/refresh_stage.py --no-bodies        # stage only, skip body fetch (debug)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

import msal
import requests

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import contextlib

import body_parser as BP  # noqa: E402  shared origin list + lane regexes
import fetch_bodies as FB  # noqa: E402  reuse upsert_body / _parse_all
import outlook_send as OS  # noqa: E402  reuse auth + token cache

GRAPH = "https://graph.microsoft.com/v1.0"
# 2026-05-06: renamed from .jsonl to .txt so SharePoint indexes the files
# (claude.ai routine's M365 MCP can fetch .txt via search; .jsonl is not
# indexed). Same JSON-Lines content. Fall back to legacy .jsonl if .txt
# doesn't exist yet.
def _resolve(name: str) -> Path:
    new = SCRIPTS / f"{name}.txt"
    legacy = SCRIPTS / f"{name}.jsonl"
    return new if new.exists() or not legacy.exists() else legacy
STAGE_PATH = _resolve("stage_emails")
BODIES_PATH = _resolve("stage_emails_bodies")

LONNY_EMAIL = "lupfold@hilmaringredients.com"
MBD_BOOKING_EMAIL = "MBD_OceanExportBookingShared@ol-usa.com"

# Drops — never stage from these senders even if they appear in the search.
#
# EMPTY SINCE 2026-08-12, AND THE REASON MATTERS MORE THAN THE LIST.
# This set used to hold MBD_Export_Pricing@ol-usa.com and caren.tobel@ol-usa.com,
# taken from config.json `ingest_scope.mailboxes_excluded`, whose provenance is
# Michael on 2026-04-30: "stop searching idealx, ignore MBD_Export_Pricing".
#
# That instruction is about MAILBOXES TO SCAN — do not go and read the
# MBD_Export_Pricing mailbox as a SOURCE. The key is literally named
# `mailboxes_excluded`. Applying it here turned it into a SENDER filter, which
# is a different and much worse thing: every quote the OL export pricing desk
# sent INTO the mailbox we do scan was discarded on arrival. That desk is where
# Hilmar rate quotes come from — it is on the daily report's own distribution
# list, and it is the sender in this repo's own OL-body test fixtures.
#
# Michael, 2026-08-12: "ol responded to everything ... they are in my mailbox
# ... where they always have been since day one". They were. We deleted them.
# Scope belongs at the layer it names: mailbox exclusions live in
# read_targets()/mailboxes_to_scan, never in classify().
EXCLUDED_SENDERS: set[str] = set()

# OL people who QUOTE Hilmar but never book. Michael 2026-08-07: "reno only
# quotes hilmar so she doesn't book."
#
# Every message from one of these in the Lonny flow is a rate response. Two
# reasons it is unconditional rather than subject-matched:
#
#   1. They have no booking role, so there is no mbd_inbound case to fall
#      through to — a non-quote from them is not a booking confirmation.
#   2. Their subjects do not follow the shared mailbox's "Re: <origin> to
#      <dest>" shape. The message that surfaced this was
#      "Re: Rates to a few destinations for a study", which
#      RATE_RESPONSE_SUBJECT_RX does not match and never will, because
#      "Rates" is not a known origin. Subject-matching them would drop the
#      quote a second time and look like it was handled.
#
# Before this, classify() returned None for any sender that is not Lonny or
# the shared mailbox, so three of Reno's messages were discarded on arrival
# and QC-057 separately flagged one of them as a silently dropped RFQ.
# 2026-08-12: the export pricing desk joins the list. These are the addresses
# that actually answer Lonny's rate requests; they were in EXCLUDED_SENDERS
# (see above for how a mailbox-scan exclusion became a sender filter), so every
# quote they sent was dropped before classification. Unconditional for the same
# two reasons as Reno: they quote rather than book, and their subjects do not
# follow the shared mailbox's "Re: <origin> to <dest>" shape.
#
# These desks serve every OL client, not just Hilmar. That is handled where it
# belongs and already is — ingest's out_of_scope gate drops numidia /
# agridairy / other_client rows (325 / 26 / 64 on the 2026-08-12 fire), and the
# lane+thread matcher only attaches a response to an ask it actually answers.
# Filtering by sender here would repeat the mistake this comment documents.
OL_QUOTE_ONLY_SENDERS = {
    "reno.gurusinghe@ol-usa.com",
    "mbd_export_pricing@ol-usa.com",
    "caren.tobel@ol-usa.com",
}

def graph_queries() -> list[tuple[str, str]]:
    """The Graph queries a fire runs. Named so a test can hold the list still.

    Two queries were the whole intake until 2026-08-11, and that was the hole:
      q1 reaches a message only when Lonny is ON it, and q2 only when the
      SHARED MAILBOX sent it with HILMAR in the subject. An OL pricer's quote
      reply that drops Lonny — Reno's normal reply shape — matched NEITHER, so
      it never reached classify() at all. The Aug 7 classify fix
      (OL_QUOTE_ONLY_SENDERS) was necessary but NOT sufficient: measured on
      live stage, mbd_rate_response stayed ZERO Aug 3→11 through two fires
      that ran with it. classify can only keep what a query fetched.

    q3 closes it: fetch BY the quote-only senders themselves. Same set that
    classify admits, so the two ends of the pipe cannot drift — a sender added
    to OL_QUOTE_ONLY_SENDERS is fetched AND kept, or neither.

    The HILMAR-bookings query MUST use `subject:HILMAR` (not bare-word
    HILMAR) — bare-word matches body content too, and NUMIDIA bookings
    frequently quote/forward HILMAR templates and trip a body match
    (2026-05-05: 6 NUMIDIA confirmations bled in as Unknown-destination
    HILMAR wins). KQL property restrictors limit scope to the parsed subject.
    """
    q1 = f'from:{LONNY_EMAIL} OR to:{LONNY_EMAIL}'
    q2 = f'from:{MBD_BOOKING_EMAIL} AND subject:HILMAR'
    q3 = " OR ".join(f"from:{s}" for s in sorted(OL_QUOTE_ONLY_SENDERS))
    return [
        ("lonny-flow", q1),
        ("hilmar-bookings", q2),
        ("ol-quote-senders", q3),
    ]


REPLY_PREFIX = re.compile(r"^\s*(re|fw|fwd)\s*:", re.I)
# Shared with ingest — built from body_parser.KNOWN_ORIGINS so a new Hilmar
# site (Dalhart was the 2026-06-11 miss) extends ONE list, not N regexes.
RATE_RESPONSE_SUBJECT = BP.RATE_RESPONSE_SUBJECT_RX


# ─────────────────────────────────────────────────────────────────────
# Auth — app-only (GH Actions) first, else outlook_send's token cache
# ─────────────────────────────────────────────────────────────────────

#: Mailbox app-only reads target. App-only tokens have no /me, so every
#: read goes to /users/{READ_MAILBOX}/... — it must be a mailbox the Entra
#: app's Application Access Policy covers. Defaults to the OL responder
#: shared mailbox (the thread endpoint: Lonny's RFQs are addressed to it
#: and OL replies from it; same mailbox outlook_send sends as). Override
#: via HILMAR_READ_MAILBOX if the thread lives elsewhere.
READ_MAILBOX = os.environ.get("HILMAR_READ_MAILBOX", OS.SEND_MAILBOX)

#: Graph mailbox root for every read in this module. Delegated (device-code)
#: uses /me; get_token() flips this to /users/{READ_MAILBOX} when app-only
#: credentials are configured.
_mailbox_base = f"{GRAPH}/me"

# ── reading MORE THAN ONE mailbox ────────────────────────────────────────
#
# 2026-08-07. diag_day, run 6, against production:
#
#     reading: https://graph.microsoft.com/v1.0/me
#       /me resolves to: Michael.Deitchman@ol-usa.com
#       >>> NOT the intended read target (MBD_OceanExportBookingShared@ol-usa.com)
#
# READ_MAILBOX above has ALWAYS documented the shared booking mailbox as the
# thread endpoint — "Lonny's RFQs are addressed to it and OL replies from it".
# But _mailbox_base only becomes that when GRAPH_APP_* is configured, and OL IT
# declined to register the app-only Entra app, so those secrets are empty and
# the delegated path reads /me instead. It never errored: it read a real
# mailbox with real mail in it, just not the one the RFQs go to. That is why
# Aug 6 reported zero, why Jul 27-28 returned nothing, and why
# mbd_rate_response sat at 0 for seven days against 299 historically.
#
# Michael, asked to choose: "1 and 3" — read the shared mailbox AND keep his
# own as a second source, merged. Both, because mail that reaches only him
# (an OL colleague replying direct, a forward) is real data we already have.
#
#: Every mailbox to read, in priority order. First writer wins on a dedup
#: collision, so the shared mailbox leads: it is the authoritative copy of a
#: thread that exists in both.
SHARED_MAILBOX = os.environ.get("HILMAR_SHARED_MAILBOX", OS.SEND_MAILBOX)

#: READ THIS BEFORE TRYING TO MAKE THE SHARED MAILBOX WORK. It is not a bug
#: and it is not a missing line of code.
#:
#: Delegated reads of another user's mailbox need Mail.Read.Shared. In the
#: ol-usa.com tenant that needs ADMIN CONSENT. Michael is not an admin there,
#: and OL IT declined to register an app for this workload — verified against
#: both directories on 2026-06-10 and written down in
#: docs/MOVE-OFF-CLOUDPC.md. The entire auth design is the no-IT path for
#: exactly this reason (outlook_send.SCOPES: "delegated; no admin consent").
#:
#: 2026-08-07 I added the scope anyway and dispatched a re-consent. Michael:
#: "these requires ol's it department to approve.. you have had these details
#: before.. why recreate the wheel here." He is right; it was in the repo.
#:
#: I THEN PROPOSED A REDIRECT RULE and that was also wrong. Michael:
#: "remember i'm already included in the group emails from ops, nothing has
#: changed." He is on the ops distribution, so mail involving Hilmar already
#: reaches michael.deitchman@ol-usa.com — there is nothing to reroute, and a
#: redirect rule would only duplicate what arrives.
#:
#: AND THE DAY THAT STARTED ALL OF THIS WAS FINE. diag_day measured 373
#: messages in that mailbox on 2026-08-06, of which exactly 3 involved
#: lupfold@hilmaringredients.com and all 3 were our own outbound report.
#: Michael, confirming: "sixth was quiet." So the Aug 6 report was CORRECT —
#: there was no activity to show, and no bug behind the zeros.
#:
#: WHICH MATTERS FOR WHAT THIS COMMENT IS: reading /me instead of the shared
#: mailbox is a real, measured fact, but it is NOT a data gap, because Michael
#: is on the ops distribution and the traffic reaches him anyway. Do not treat
#: the paragraphs above as a known bug waiting to be fixed. They are here so
#: that anyone who DOES find mail missing knows which mailbox is being read
#: and why it cannot simply be widened.
#:
#: The genuine intake defect found on 2026-08-07 was classify() silently
#: dropping OL quote-only senders — see OL_QUOTE_ONLY_SENDERS. That is what
#: took mbd_rate_response to 0 for seven days against 299 historically, and it
#: is fixed.
#:
#: If access to the shared mailbox ever IS granted (app registration, or a
#: tenant policy change), everything below starts working with no edit.
#:
#: Everything below stays because it costs nothing and is CORRECT the moment
#: access exists by any route — including the app-only path, where
#: _mailbox_base already points at READ_MAILBOX.
SHARED_READ_SCOPES = [*OS.SCOPES, "Mail.Read.Shared"]


def _app_only_token() -> str | None:
    """App-only (client credentials) token when GRAPH_APP_* is configured,
    else None. This is what lets the GH Actions fire read the mailbox with
    no signed-in user. On the Cloud PC (env vars unset) this returns None
    and the device-code cache path below is used unchanged."""
    try:
        sys.path.insert(0, str(ROOT / "src"))
        from hilmar.app_auth import (
            acquire_app_only_token,
            app_only_credentials_from_env,
        )
    except Exception:
        return None
    creds = app_only_credentials_from_env()
    if creds is None:
        return None
    return acquire_app_only_token(creds)


def get_token() -> str:
    """Acquire a Graph token: app-only when GRAPH_APP_* is set, else silent
    refresh from the device-code cache. Sets the mailbox base to match —
    the token type dictates whether /me exists."""
    global _mailbox_base
    token = _app_only_token()
    if token is not None:
        _mailbox_base = f"{GRAPH}/users/{READ_MAILBOX}"
        print(f"refresh_stage: app-only Graph auth (GRAPH_APP_*) — reading {READ_MAILBOX}")
        return token
    return get_token_silent()


def shared_token_silent() -> str | None:
    """A delegated token carrying Mail.Read.Shared, or None if not consented.

    None rather than an exception, on purpose: the fire must keep running on
    /me alone until the operator re-authenticates. A missing scope is a
    degraded read, not a broken pipeline — and the caller says so loudly.
    """
    try:
        cache = OS._load_cache()
        app = msal.PublicClientApplication(
            OS.CLIENT_ID,
            authority=f"https://login.microsoftonline.com/{OS.TENANT}",
            token_cache=cache,
        )
        accounts = app.get_accounts()
        if not accounts:
            return None
        result = app.acquire_token_silent(SHARED_READ_SCOPES, account=accounts[0])
    except Exception:
        return None
    if not result or "access_token" not in result:
        return None
    return result["access_token"]


def read_targets(token: str) -> list[tuple[str, str, str]]:
    """Every mailbox to read, as (label, base_url, token_for_that_base).

    App-only addresses READ_MAILBOX directly and has no /me, so it is a single
    target. Delegated reads /me always, and adds the shared mailbox when — and
    only when — a token with Mail.Read.Shared is available.

    The shared mailbox is FIRST in the delegated list so that a thread present
    in both dedupes to the shared copy, which is the authoritative one.
    """
    if not _mailbox_base.endswith("/me"):
        # app-only: _mailbox_base is already /users/{READ_MAILBOX}
        return [(READ_MAILBOX, _mailbox_base, token)]

    targets: list[tuple[str, str, str]] = []
    shared_tok = shared_token_silent()
    if shared_tok:
        targets.append((SHARED_MAILBOX, f"{GRAPH}/users/{SHARED_MAILBOX}", shared_tok))
    else:
        # Loud, because "Lonny sent nothing" and "we cannot see the mailbox
        # Lonny sends to" read identically for a week and cost us that week.
        #
        # The advice has to be ACTIONABLE or it is just recurring noise: this
        # said "re-run the auth workflow to consent" until 2026-08-07, which
        # is impossible — that consent needs OL IT. See SHARED_MAILBOX above.
        print(f"::warning::refresh_stage is reading only {_mailbox_base} and "
              f"NOT {SHARED_MAILBOX}, so RFQs addressed solely to the shared "
              f"mailbox are invisible. This is NOT fixable with a re-auth — "
              f"Mail.Read.Shared needs ol-usa admin consent, which OL IT "
              f"declined. Fix it by REDIRECTING (not forwarding) Lonny's mail "
              f"from the shared mailbox into this one; see refresh_stage."
              f"SHARED_MAILBOX for why redirect and not forward.")
    targets.append(("me", _mailbox_base, token))
    return targets


def get_token_silent() -> str:
    """Acquire a Graph token via silent refresh. Bail if cache is missing/expired."""
    cache = OS._load_cache()
    app = msal.PublicClientApplication(
        OS.CLIENT_ID,
        authority=f"https://login.microsoftonline.com/{OS.TENANT}",
        token_cache=cache,
    )
    accounts = app.get_accounts()
    if not accounts:
        raise SystemExit(
            "ERROR: no cached MSAL account. Run `python scripts/outlook_send.py auth` "
            "interactively once to seed the token cache."
        )
    result = app.acquire_token_silent(OS.SCOPES, account=accounts[0])
    if not result or "access_token" not in result:
        raise SystemExit(
            "ERROR: silent token refresh failed. Refresh token may have expired "
            "(>90 days idle). Run `python scripts/outlook_send.py auth` to re-authenticate."
        )
    OS._save_cache(cache)
    return result["access_token"]


# ─────────────────────────────────────────────────────────────────────
# HTTP helpers
# ─────────────────────────────────────────────────────────────────────

RETRYABLE_STATUS = {429, 503, 504}
RETRY_BACKOFFS_S = (2.0, 4.0, 8.0)


def graph_get(token: str, url: str, params: dict | None = None) -> dict:
    """GET with retry on 429/503/504."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }
    for attempt in range(len(RETRY_BACKOFFS_S) + 1):
        r = requests.get(url, params=params, headers=headers, timeout=60)
        if r.status_code in RETRYABLE_STATUS and attempt < len(RETRY_BACKOFFS_S):
            wait = RETRY_BACKOFFS_S[attempt]
            ra = r.headers.get("Retry-After")
            if ra:
                with contextlib.suppress(ValueError):
                    wait = float(ra)
            time.sleep(wait)
            continue
        if not (200 <= r.status_code < 300):
            raise RuntimeError(f"Graph GET {url} -> {r.status_code}: {r.text[:500]}")
        return r.json()
    raise RuntimeError("unreachable")


# ─────────────────────────────────────────────────────────────────────
# Search
# ─────────────────────────────────────────────────────────────────────

GRAPH_SELECT = (
    "id,conversationId,subject,from,toRecipients,ccRecipients,"
    "bodyPreview,receivedDateTime,sentDateTime,internetMessageId,isRead,hasAttachments,"
    # 2026-05-19 PM (Michael "you have to parse the booking team emails
    # for matches based on header meta data"): fetch the full email
    # header collection so we can read In-Reply-To + References. Those
    # link MDOLX booking confirmations back to the originating Lonny
    # RFQ thread even when conversation_id differs.
    "internetMessageHeaders"
)


def _rotate_stage(days_to_keep: int, dry: bool = False) -> dict:
    """Prune stage_emails.txt + stage_emails_bodies.txt of records older than
    `days_to_keep` days. Writes audit log to scripts/_stage_rotation.log.

    Added 2026-05-14 per best-practices batch — keeps stage bounded so it
    doesn't grow unbounded over time. Default retain = 90d (Outlook search
    catches anything we need re-pulled).
    """
    import json as _j
    from datetime import datetime as _dt
    from datetime import timedelta as _td
    from datetime import timezone as _tz
    scripts_dir = ROOT / "scripts"
    cutoff = (_dt.now(_tz.utc) - _td(days=days_to_keep)).isoformat()

    out = {"stage_pruned": 0, "bodies_pruned": 0, "cutoff": cutoff}
    stage_path = scripts_dir / "stage_emails.txt"
    bodies_path = scripts_dir / "stage_emails_bodies.txt"

    # Stage prune — keep records with received/sent ≥ cutoff
    if stage_path.exists():
        kept_lines = []
        pruned_imids = set()
        for line in stage_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                d = _j.loads(line)
            except Exception:
                kept_lines.append(line)
                continue
            ts = (d.get("received") or d.get("sent") or d.get("sent_ts") or "")
            if ts and ts < cutoff:
                pruned_imids.add((d.get("imid") or "").strip("<>"))
                out["stage_pruned"] += 1
            else:
                kept_lines.append(line)
        if not dry and out["stage_pruned"] > 0:
            # Write atomic — temp file then rename
            tmp = stage_path.with_suffix(".tmp")
            tmp.write_text("\n".join(kept_lines) + "\n", encoding="utf-8")
            tmp.replace(stage_path)

    # Bodies prune — drop bodies for imids that got rotated out
    if bodies_path.exists() and out["stage_pruned"] > 0:
        kept_bodies = []
        for line in bodies_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                d = _j.loads(line)
            except Exception:
                kept_bodies.append(line)
                continue
            imid = (d.get("imid") or "").strip("<>")
            if imid and imid in pruned_imids:
                out["bodies_pruned"] += 1
            else:
                kept_bodies.append(line)
        if not dry and out["bodies_pruned"] > 0:
            tmp = bodies_path.with_suffix(".tmp")
            tmp.write_text("\n".join(kept_bodies) + "\n", encoding="utf-8")
            tmp.replace(bodies_path)

    # Audit log
    if not dry:
        log_path = scripts_dir / "_stage_rotation.log"
        with log_path.open("a", encoding="utf-8") as f:
            f.write(f"{_dt.now(_tz.utc).isoformat()} cutoff={cutoff} "
                    f"stage_pruned={out['stage_pruned']} "
                    f"bodies_pruned={out['bodies_pruned']}\n")
    return out


def _warn_search_cap(kql: str, got: int, cap: int) -> None:
    """A $search query hit the result cap with MORE messages available.

    This matters because Graph KQL $search ranks by RELEVANCE, not date, and
    $search CANNOT be combined with $orderby (Graph rejects the request), so
    we can't force newest-first. A cap-hit therefore drops an ARBITRARY tail —
    possibly recent mail — silently. Make it LOUD: stderr (captured in the
    fire's run-log) + a best-effort Sentry message so it surfaces in the audit
    rather than vanishing. Mitigation when this fires: re-run with a tighter
    --since (narrower window => fewer matches) or a higher --max-results-per-query.
    """
    msg = (f"refresh_stage: WARNING — $search hit the {cap}-result cap with more "
           f"available for query [{kql[:80]}]; relevance-ranked truncation may have "
           f"dropped recent messages. Narrow --since or raise --max-results-per-query.")
    print(msg, file=sys.stderr)
    print(f"::warning::{msg}")  # GitHub Actions annotation when run there
    with contextlib.suppress(Exception):
        import sentry_setup  # best-effort; no-op if SDK/DSN absent
        sentry_setup.init("refresh_stage")
        import sentry_sdk
        sentry_sdk.capture_message(msg, level="warning")


def search_messages(token: str, kql: str, max_results: int = 500,
                    base: str | None = None) -> list[dict]:
    """Run a $search query against a mailbox's /messages, paginate nextLink.

    `base` defaults to the module's mailbox so every existing caller is
    unchanged; main() passes one base per mailbox in read_targets().

    Cap raised 250->500 (2026-06-24): the cap only ever bounds work when a
    query matches more than `max_results` in the window (the loop stops at the
    last nextLink otherwise), so the higher ceiling is free headroom against a
    silent truncation. If the cap IS hit with more available, _warn_search_cap
    makes it visible — see that function for why $orderby can't fix this.
    """
    url: str | None = f"{base or _mailbox_base}/messages"
    # Graph requires the $search value to be a quoted string.
    params: dict | None = {
        "$top": "50",
        "$search": f'"{kql}"',
        "$select": GRAPH_SELECT,
    }
    out: list[dict] = []
    truncated = False
    while url and len(out) < max_results:
        data = graph_get(token, url, params=params)
        for item in data.get("value", []):
            if len(out) >= max_results:
                truncated = True   # this page held more than the cap allows
                break
            out.append(item)
        url = data.get("@odata.nextLink")
        params = None  # nextLink already carries the query string
        if len(out) >= max_results and url:
            truncated = True       # cap reached and Graph still has more pages
    if truncated:
        _warn_search_cap(kql, len(out), max_results)
    return out


def list_messages_since(token: str, since_iso: str, max_results: int = 30000,
                        base: str | None = None) -> list[dict]:
    """EVERY message in the mailbox since `since_iso`, newest first.

    THE 2026-08-12 DEFECT, and why $search cannot be the primary intake.
    Michael: "they are in my mailbox ... where they always have been since day
    one". They were. The verification fire's own log:

        query 'lonny-flow':       got 275 results
        query 'hilmar-bookings':  got 275 results
        query 'ol-quote-senders': got 275 results

    Three semantically unrelated queries cannot each match exactly 275
    messages. Graph stopped paginating $search at a service-side ceiling —
    below our 500 cap, so `truncated` stayed False and _warn_search_cap never
    fired. Worse, $search ranks by RELEVANCE and cannot be combined with
    $orderby (see _warn_search_cap), so the 275 we kept were an ARBITRARY
    slice and the tail it dropped included the recent OL quote replies. That
    is how "OL responded to everything" and "no reply in our data" were both
    true, and why it degraded gradually: as the mailbox grew, the relevance
    slice stopped reaching the current week.

    $filter + $orderby on receivedDateTime is the opposite in every respect:
    date-ordered, deterministic, and complete for the window — pagination ends
    because the window ends, not because a ranker lost interest. Classification
    stays where it belongs (classify_email, client-side), which is what
    main()'s "apply client-side date filter, classify" step already assumes.

    max_results is a runaway guard, not a business limit; hitting it is loud.
    """
    url: str | None = f"{base or _mailbox_base}/messages"
    params: dict | None = {
        # 500/page, not 100. Michael's OL mailbox carries every other client
        # too (TTS, Hoogwegt, Numidia, Hilldrup...), so a 21-day window is
        # thousands of messages; at 100/page the first run spent two minutes
        # and still stopped short. Graph allows up to 1000 here.
        "$top": "500",
        "$filter": f"receivedDateTime ge {since_iso}",
        "$orderby": "receivedDateTime desc",
        "$select": GRAPH_SELECT,
    }
    out: list[dict] = []
    while url and len(out) < max_results:
        data = graph_get(token, url, params=params)
        out.extend(data.get("value", []))
        url = data.get("@odata.nextLink")
        params = None  # nextLink carries the query string
    # COVERAGE IS THE THING TO REPORT, not the raw count. Ordering is newest
    # first, so a guard-stop truncates the OLDEST end — the safe direction
    # (today's mail is never the part lost), but it silently shortens the
    # window, which is how the first run of this sweep still missed Jul 29-31.
    # State the floor we actually reached and compare it to the one asked for;
    # "reached back to Aug 7" against a Jul 22 cutoff is the honest headline.
    floor = min((m.get("receivedDateTime") or "" for m in out), default="")
    print(f"refresh_stage:   sweep read {len(out)} message(s); oldest reached "
          f"{floor or 'n/a'} (window floor requested: {since_iso[:19]})")
    if len(out) >= max_results or (floor and floor[:19] > since_iso[:19]):
        msg = (f"refresh_stage: WARNING — date sweep did NOT reach the window "
               f"floor: read {len(out)} message(s) back to {floor or 'n/a'}, "
               f"but the window starts {since_iso[:19]} (guard "
               f"{max_results}). Mail older than the reached point is missing "
               "from this run. Raise --max-window-messages.")
        print(msg, file=sys.stderr)
        print(f"::warning::{msg}")
    return out


def get_message_body(token: str, message_id: str, base: str | None = None) -> dict:
    """Fetch a single message including body.

    `base` MUST be the mailbox the message was found in — a Graph message id
    is mailbox-scoped, so asking the wrong mailbox for it is a 404, not a
    fallback to the right one.
    """
    url = f"{base or _mailbox_base}/messages/{message_id}"
    params = {
        "$select": (
            "id,conversationId,subject,from,toRecipients,ccRecipients,"
            "body,bodyPreview,receivedDateTime,sentDateTime,internetMessageId"
        ),
    }
    return graph_get(token, url, params=params)


def fetch_pdf_attachments(token: str, message_id: str, imid: str, dest_dir: Path,
                          base: str | None = None) -> list[str]:
    """Download PDF attachments for a message. Returns list of saved filenames.

    Added 2026-05-13 per Michael "90 percent for all is the bare minimum".
    Booking-confirmation emails have signature-only bodies; the actual
    vessel/ETD/rate data is in the attached PDF. pdf_parser.py reads these.
    Saves to scripts/stage_pdfs/<safe_imid>.pdf. Idempotent — skips if
    already downloaded.
    """
    import base64

    dest_dir.mkdir(parents=True, exist_ok=True)
    saved = []
    safe_imid = re.sub(r"[^A-Za-z0-9._-]+", "_", imid.strip("<>"))[:100]
    target_pdf = dest_dir / f"{safe_imid}.pdf"
    if target_pdf.exists():
        return [target_pdf.name]  # already cached
    # List attachments
    list_url = f"{base or _mailbox_base}/messages/{message_id}/attachments?$select=id,name,contentType,size"
    try:
        listing = graph_get(token, list_url)
    except Exception:
        return saved
    for att in listing.get("value", []) or []:
        name = (att.get("name") or "").lower()
        ctype = (att.get("contentType") or "").lower()
        if not (name.endswith(".pdf") or "pdf" in ctype):
            continue
        att_id = att.get("id")
        if not att_id:
            continue
        # Fetch content
        content_url = f"{_mailbox_base}/messages/{message_id}/attachments/{att_id}"
        try:
            data = graph_get(token, content_url)
        except Exception:
            continue
        bytes_b64 = data.get("contentBytes")
        if not bytes_b64:
            continue
        try:
            pdf_bytes = base64.b64decode(bytes_b64)
        except Exception:
            continue
        target_pdf.write_bytes(pdf_bytes)
        saved.append(target_pdf.name)
        break  # one PDF per message is enough
    return saved


# ─────────────────────────────────────────────────────────────────────
# Classification
# ─────────────────────────────────────────────────────────────────────

def classify(item: dict) -> str | None:
    """Return bucket name or None if message should be dropped."""
    sender = ((item.get("from") or {}).get("emailAddress") or {}).get("address") or ""
    sender_l = sender.lower()
    subject = item.get("subject") or ""

    if sender_l in {s.lower() for s in EXCLUDED_SENDERS}:
        return None

    if sender_l == LONNY_EMAIL.lower():
        if REPLY_PREFIX.match(subject):
            return "lonny_reply"
        return "lonny_outbound"

    if sender_l == MBD_BOOKING_EMAIL.lower():
        if RATE_RESPONSE_SUBJECT.match(subject):
            return "mbd_rate_response"
        return "mbd_inbound"

    # Quote-only OL senders — everything they send is a rate response. See
    # OL_QUOTE_ONLY_SENDERS for why this is not subject-matched.
    if sender_l in {s.lower() for s in OL_QUOTE_ONLY_SENDERS}:
        return "mbd_rate_response"

    return None  # any other sender → drop


# ─────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────

def parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    s = ts.rstrip("Z")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def load_existing_stage_keys() -> tuple[set[str], set[str]]:
    """Return (existing_ids, existing_imids) so dedup catches both keys.

    Cowork's MCP wrote `id` (Graph message id) — a folder-scoped opaque
    string. When we re-fetch the same email from a different folder, Graph
    gives it a different id but the same internetMessageId. Index both so
    refresh_stage doesn't double-stage emails that already exist under their
    other-folder id.
    """
    if not STAGE_PATH.exists():
        return set(), set()
    ids: set[str] = set()
    imids: set[str] = set()
    with open(STAGE_PATH, encoding="utf-8") as f:
        for line in f:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("id"):
                ids.add(rec["id"])
            if rec.get("imid"):
                imids.add(rec["imid"])
    return ids, imids


def load_existing_body_imids() -> set[str]:
    if not BODIES_PATH.exists():
        return set()
    out = set()
    with open(BODIES_PATH, encoding="utf-8") as f:
        for line in f:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("imid"):
                out.add(rec["imid"])
    return out


def append_stage_record(rec: dict) -> None:
    with open(STAGE_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _extract_thread_headers(item: dict) -> dict:
    """Extract In-Reply-To + References from Graph's internetMessageHeaders.

    2026-05-19 PM (Michael "parse the booking team emails for matches
    based on header meta data and you'll find them"): the booking-
    confirmation email is sent by OL in a NEW conversation_id (different
    from the original Lonny RFQ thread), but its In-Reply-To / References
    headers point back to the prior thread's Message-ID. We capture those
    so ingest.link_bookings_to_requests can match a booking back to its
    originating RFQ even when conversation_id differs.

    Returns: {"in_reply_to": "<...>" | None, "references": ["<...>", ...]}
    """
    headers = item.get("internetMessageHeaders") or []
    out = {"in_reply_to": None, "references": []}
    for h in headers:
        name = (h.get("name") or "").lower()
        value = (h.get("value") or "").strip()
        if name == "in-reply-to":
            out["in_reply_to"] = value
        elif name == "references":
            # References is space-or-CRLF separated list of <message-id>'s
            out["references"] = [m for m in value.replace("\n", " ").replace("\r", " ").split() if m]
    return out


def build_stage_record(item: dict, bucket: str) -> dict:
    """Same shape Cowork's MCP wrote — id / imid / bucket / received / sent / subject / summary_preview / uri.

    2026-05-19 PM: now also includes `in_reply_to` + `references` (parsed
    from internetMessageHeaders) so the booking-link matcher can use them.
    Backward-compat: rows without these fields still work (matcher falls
    back to conv_id + lane+time).
    """
    threading = _extract_thread_headers(item)
    return {
        "id": item["id"],
        "imid": item.get("internetMessageId"),
        "bucket": bucket,
        "received": item.get("receivedDateTime"),
        "sent": item.get("sentDateTime"),
        "subject": item.get("subject") or "",
        "summary_preview": (item.get("bodyPreview") or "")[:300],
        "uri": f"mail:///messages/{item['id']}",
        # Threading metadata for header-based booking matching
        "in_reply_to": threading["in_reply_to"],
        "references": threading["references"],
        "conversation_id": item.get("conversationId"),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--days-back", type=int, default=14,
                    help="Lower bound for receivedDateTime (default 14)")
    ap.add_argument("--since", type=str,
                    help="Explicit lower bound, ISO date e.g. 2026-04-01. Overrides --days-back.")
    ap.add_argument("--max-window-messages", type=int, default=30000,
                    help="Runaway guard for the date sweep (default 30000). Not a "
                         "business limit — the sweep must read the whole window; "
                         "hitting it means the window was NOT fully read and is a "
                         "loud warning. The 4000 first tried was too low: a 21-day "
                         "window of this mailbox is thousands of messages because "
                         "every other client shares it.")
    ap.add_argument("--dry", action="store_true",
                    help="Don't write — log what would be added")
    ap.add_argument("--verbose", action="store_true",
                    help="Per-message classification trace")
    ap.add_argument("--no-bodies", action="store_true",
                    help="Stage metadata only — skip body fetch (debug)")
    ap.add_argument("--rotate-stage-older-than", type=int, default=None,
                    help="Prune stage_emails.txt + bodies of records older than N days. "
                         "Keeps live data fast and bounded. Per Michael 2026-05-14 "
                         "best-practices batch. Recommended: 90.")
    ap.add_argument("--pdf-backfill", action="store_true",
                    help="Also fetch PDF attachments for existing mbd_inbound bodies that don't have them on disk yet (one-time catch-up).")
    ap.add_argument("--max-results-per-query", type=int, default=500,
                    help="Cap results per search query (default 500); a cap-hit "
                         "with more available emits a loud WARN + Sentry event")
    args = ap.parse_args()

    if args.since:
        cutoff = datetime.fromisoformat(args.since)
        if cutoff.tzinfo is None:
            cutoff = cutoff.replace(tzinfo=timezone.utc)
    else:
        cutoff = datetime.now(timezone.utc) - timedelta(days=args.days_back)
    print(f"refresh_stage: cutoff = {cutoff.isoformat()}")

    print("refresh_stage: acquiring Graph token…")
    token = get_token()
    print("refresh_stage: token OK")

    existing_ids, existing_stage_imids = load_existing_stage_keys()
    existing_body_imids = load_existing_body_imids()
    print(f"refresh_stage: existing stage records = {len(existing_ids)} (by id), "
          f"{len(existing_stage_imids)} (by imid)")
    print(f"refresh_stage: existing body records  = {len(existing_body_imids)}")

    # Query semantics and the 2026-08-11 third query: see graph_queries().
    queries = graph_queries()

    # Dedupe across queries AND across folder-copies (Inbox vs Sent Items vs
    # archive subfolders all carry distinct Graph `id`s for the same email).
    # internetMessageId (imid) is the RFC822 Message-ID header — stable across
    # folders. We keep the first copy we see; if the email lacks an imid (rare,
    # Stage rotation — prune old records before fetch so we don't carry stale
    # state through the pipeline. Audit log written to scripts/_stage_rotation.log.
    if args.rotate_stage_older_than:
        rotated = _rotate_stage(args.rotate_stage_older_than, dry=args.dry)
        print(f"refresh_stage: rotation pruned {rotated['stage_pruned']} stage records "
              f"+ {rotated['bodies_pruned']} bodies older than {args.rotate_stage_older_than}d "
              f"({'DRY' if args.dry else 'live'})")

    # mostly drafts) we fall back to Graph id to avoid silently dropping it.
    # Every mailbox, every query. A Graph message id is mailbox-scoped, so
    # each item remembers which mailbox produced it (`_src`) and the body and
    # attachment fetches below address that same mailbox — asking the wrong
    # one for an id is a 404, not a silent fallback to the right one.
    targets = read_targets(token)
    by_label = {label: (base, tok) for label, base, tok in targets}
    print(f"refresh_stage: reading {len(targets)} mailbox(es): "
          f"{', '.join(label for label, _, _ in targets)}")

    all_items: dict[str, dict] = {}  # imid (or id) -> Graph item

    # PRIMARY INTAKE — the complete, date-ordered window (2026-08-12).
    # $search is relevance-ranked and silently ceilinged (see
    # list_messages_since); this sweep is the one that cannot miss a message
    # inside the window. The $search queries still run below as a SUPPLEMENT
    # for mail older than the cutoff that a thread may need; anything they
    # return that is already here dedupes on imid.
    for mbox, base, mtoken in targets:
        try:
            swept = list_messages_since(mtoken, cutoff.isoformat(), base=base,
                                        max_results=args.max_window_messages)
        except Exception as e:
            print(f"::error::refresh_stage: [{mbox}] date sweep FAILED: "
                  f"{type(e).__name__}: {e} — falling back to $search only, "
                  "which is known to drop recent mail")
            continue
        print(f"refresh_stage: [{mbox}] date sweep since {cutoff.date()}: "
              f"{len(swept)} message(s) in window")
        for it in swept:
            key = it.get("internetMessageId") or it["id"]
            if key not in all_items:
                it["_src"] = mbox
                all_items[key] = it
    _swept_total = len(all_items)

    _search_counts: dict[str, int] = {}
    for mbox, base, mtoken in targets:
        for label, kql in queries:
            print(f"refresh_stage: [{mbox}] query {label!r}: {kql}")
            try:
                items = search_messages(mtoken, kql, base=base,
                                        max_results=args.max_results_per_query)
            except Exception as e:
                # One unreadable mailbox must not cost us the other one.
                print(f"::warning::refresh_stage: [{mbox}] query {label!r} "
                      f"FAILED: {type(e).__name__}: {e}")
                continue
            print(f"refresh_stage:   got {len(items)} results from {label}")
            _search_counts[f"{mbox}/{label}"] = len(items)
            for it in items:
                key = it.get("internetMessageId") or it["id"]
                if key not in all_items:
                    it["_src"] = mbox
                    all_items[key] = it
    # THE DETECTOR THAT FAILED. _warn_search_cap only fires when our own cap is
    # reached; Graph's service-side $search ceiling sits BELOW it and is
    # invisible — on 2026-08-12 three unrelated queries each returned exactly
    # 275 and nothing complained. Identical counts across semantically
    # different queries is that ceiling's fingerprint, so say so out loud. The
    # date sweep above already makes this non-fatal; the warning exists so the
    # next person does not spend a day proving it again.
    _dupe = {n for n in _search_counts.values() if list(_search_counts.values()).count(n) > 1}
    for n in sorted(_dupe):
        _who = [k for k, v in _search_counts.items() if v == n]
        print(f"::warning::refresh_stage: {len(_who)} unrelated $search queries "
              f"each returned exactly {n} results ({', '.join(_who)}) — that is "
              "Graph's relevance-ranked service ceiling, not a real count. The "
              "date sweep is the authoritative intake; $search is supplementary.")
    print(f"refresh_stage: total unique results across queries: {len(all_items)} "
          f"({_swept_total} from the date sweep, "
          f"{len(all_items) - _swept_total} added by $search outside it)")
    if len(targets) > 1:
        per_src: Counter = Counter(it.get("_src") or "?" for it in all_items.values())
        for mbox, n in per_src.most_common():
            print(f"refresh_stage:   {n:>5} first seen in {mbox}")

    # Apply client-side date filter, classify, dedupe vs existing stage.
    new_stage: list[tuple[dict, str]] = []  # (item, bucket)
    bucket_counts: dict[str, int] = {}
    skipped_old = 0
    skipped_unclassified = 0
    skipped_excluded = 0
    skipped_existing = 0
    dropped_senders: Counter = Counter()
    dropped_examples: list[tuple[str, str, str]] = []
    for it in all_items.values():
        ts = parse_iso(it.get("receivedDateTime")) or parse_iso(it.get("sentDateTime"))
        if ts and ts < cutoff:
            skipped_old += 1
            continue
        bucket = classify(it)
        if bucket is None:
            sender = ((it.get("from") or {}).get("emailAddress") or {}).get("address") or ""
            if sender.lower() in {s.lower() for s in EXCLUDED_SENDERS}:
                skipped_excluded += 1
                if args.verbose:
                    print(f"  EXCL {sender} | {(it.get('subject') or '')[:80]}")
            else:
                skipped_unclassified += 1
                dropped_senders[sender.lower() or "<no sender>"] += 1
                # Keep a few whole examples so the LOG can answer "which
                # message?" without a re-run. Before 2026-08-07 the sender was
                # printed only under --verbose, which the daily fire does not
                # pass — so a week of dropped mail produced the single word
                # "unclassified" and nothing else to go on.
                if len(dropped_examples) < 8:
                    dropped_examples.append(
                        (sender or "<no sender>", (it.get("subject") or "")[:110],
                         it.get("receivedDateTime") or it.get("sentDateTime") or "?"))
                if args.verbose:
                    print(f"  DROP {sender} | {(it.get('subject') or '')[:80]}")
            continue
        # Two-key dedup: skip if this Graph id was previously staged OR if
        # any prior stage record has the same internetMessageId (folder-copy
        # of an already-seen message).
        imid = it.get("internetMessageId")
        if it["id"] in existing_ids or (imid and imid in existing_stage_imids):
            skipped_existing += 1
            continue
        new_stage.append((it, bucket))
        bucket_counts[bucket] = bucket_counts.get(bucket, 0) + 1
        if args.verbose:
            print(f"  NEW  {bucket:<22} {(it.get('subject') or '')[:80]!r}")

    print()
    print(f"refresh_stage: NEW staged records: {len(new_stage)}")
    for b, c in sorted(bucket_counts.items()):
        print(f"  {b}: {c}")
    print(f"refresh_stage: skipped {skipped_old} pre-cutoff, "
          f"{skipped_excluded} excluded, "
          f"{skipped_unclassified} unclassified, "
          f"{skipped_existing} already-staged")

    # NAME the drops. `classify` returns None for any sender that is not Lonny
    # or the shared booking mailbox, and the 'lonny-flow' Graph query returns
    # mail TO Lonny as well as FROM him — so an OL reply from an individual's
    # mailbox is silently discarded. On 2026-08-06 that was 12 messages, and
    # the only trace in the log was the word "unclassified".
    if dropped_senders:
        print()
        print("refresh_stage: DROPPED as unclassified — by sender:")
        for s, n in dropped_senders.most_common(12):
            print(f"    {n:>4}  {s}")
        print("refresh_stage: examples (sender | received | subject):")
        for s, subj, when in dropped_examples:
            print(f"    {s} | {when} | {subj!r}")
        # Loud when a whole fire staged nothing while throwing mail away. The
        # daily pipeline is best-effort here and exits 0 either way, so this
        # annotation is what surfaces in the run summary rather than sitting
        # 400 lines up in stdout.
        if not new_stage:
            print(f"::error::refresh_stage staged 0 new records while dropping "
                  f"{skipped_unclassified} as unclassified — the classifier is "
                  f"rejecting live mail. Senders: "
                  f"{', '.join(s for s, _ in dropped_senders.most_common(6))}")

    if args.dry:
        # Sample-by-bucket so the operator can sanity-check classification
        # before committing writes — especially important when the broader
        # HILMAR-bookings query may pull in operational/ops emails for
        # other clients that incidentally mention HILMAR.
        print("\nSamples per bucket (up to 8 each) — sanity-check before running for real:")
        samples_by_bucket: dict[str, list[str]] = {}
        for it, bucket in new_stage:
            samples_by_bucket.setdefault(bucket, []).append(
                f"{(it.get('subject') or '')[:100]}"
            )
        for b in sorted(samples_by_bucket.keys()):
            print(f"\n  --- {b} ({len(samples_by_bucket[b])}) ---")
            for s in samples_by_bucket[b][:8]:
                print(f"    {s!r}")
        print("\nDRY RUN — no writes")
        return 0

    if not new_stage and not args.pdf_backfill:
        print("\nNothing new to stage.")
        return 0
    if not new_stage and args.pdf_backfill:
        print("\nNothing new to stage — proceeding to PDF backfill of existing bodies.")
        # Fall through to backfill block below by skipping the new-body fetch loop
        body_count = 0
        body_failures = 0
        pdf_count = 0
        pdf_dir = ROOT / "scripts" / "stage_pdfs"
        # Jump to backfill block via a guard
        _skip_to_backfill = True
    else:
        _skip_to_backfill = False

    if not _skip_to_backfill:
        # Append stage records (atomic — we hold the file open ourselves)
        print(f"\nAppending {len(new_stage)} records to {STAGE_PATH.name}...")
        for it, bucket in new_stage:
            append_stage_record(build_stage_record(it, bucket))

        if args.no_bodies:
            print("--no-bodies set — skipping body fetch.")
            return 0

    # Fetch bodies for new entries (skipping any whose imid is already in bodies file)
    if not _skip_to_backfill:
        body_count = 0
        body_failures = 0
        pdf_count = 0
        pdf_dir = ROOT / "scripts" / "stage_pdfs"
    _body_iter = [] if _skip_to_backfill else new_stage
    for it, bucket in _body_iter:
        imid = it.get("internetMessageId")
        if imid and imid in existing_body_imids:
            continue
        _base, _tok = by_label.get(it.get("_src"), (None, token))
        try:
            full = get_message_body(_tok, it["id"], base=_base)
        except Exception as e:
            body_failures += 1
            print(f"  body fetch FAIL {(it.get('subject') or '')[:60]!r}: {e}")
            continue
        body = full.get("body") or {}
        sender = ((full.get("from") or {}).get("emailAddress") or {}).get("address")
        msg_imid = full.get("internetMessageId") or full["id"]
        FB.upsert_body(
            imid=msg_imid,
            bucket=bucket,
            uri=f"mail:///messages/{full['id']}",
            subject=full.get("subject") or "",
            html_body=body.get("content") or "",
            conversation_id=full.get("conversationId"),
            sender_email=sender,
            sent_ts=full.get("sentDateTime"),
            received_ts=full.get("receivedDateTime"),
        )
        body_count += 1
        # Booking-confirmation bodies are signature-only — pull the PDF
        # attachment for pdf_parser.py to extract vessel/ETD/rate from.
        # Only mbd_inbound (booking confirmations) — the other buckets
        # don't carry useful PDFs.
        if bucket == "mbd_inbound" and it.get("hasAttachments"):
            try:
                saved = fetch_pdf_attachments(_tok, full["id"], msg_imid, pdf_dir,
                                              base=_base)
                if saved:
                    pdf_count += 1
                    if args.verbose:
                        print(f"  PDF  {saved[0]}")
            except Exception as e:
                if args.verbose:
                    print(f"  pdf fetch FAIL {imid[:40]}: {e}")
        if args.verbose:
            print(f"  BODY {bucket:<22} {(full.get('subject') or '')[:60]!r}")

    print(f"\nrefresh_stage: fetched {body_count} new bodies, {body_failures} failures, "
          f"{pdf_count} PDF attachments")

    # Backfill PDF attachments for existing mbd_inbound bodies. The PDF
    # download was added 2026-05-13 — prior fires never pulled PDFs.
    # This catches up the historical booking confirmations so pdf_parser
    # can extract their data. Idempotent: skips imids whose PDF is
    # already on disk.
    if args.pdf_backfill:
        print("\nrefresh_stage: PDF backfill for existing mbd_inbound bodies…")
        bodies_path = ROOT / "scripts" / "stage_emails_bodies.txt"
        pdf_backfilled = 0
        pdf_skipped = 0
        pdf_fail = 0
        if bodies_path.exists():
            import re as _re
            for line in bodies_path.read_text(encoding="utf-8", errors="ignore").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                if d.get("bucket") != "mbd_inbound":
                    continue
                imid = (d.get("imid") or "").strip("<>")
                if not imid:
                    continue
                safe_imid = _re.sub(r"[^A-Za-z0-9._-]+", "_", imid)[:100]
                target = pdf_dir / f"{safe_imid}.pdf"
                if target.exists():
                    pdf_skipped += 1
                    continue
                # Reconstruct the Graph message_id from the URI saved in body record
                uri = d.get("uri") or ""
                m = _re.search(r"messages/([^/]+)", uri)
                if not m:
                    continue
                msg_id = m.group(1)
                try:
                    saved = fetch_pdf_attachments(token, msg_id, imid, pdf_dir)
                    if saved:
                        pdf_backfilled += 1
                    if args.verbose:
                        print(f"  backfill {('PDF ' + saved[0]) if saved else 'no-attach'} {imid[:50]}")
                except Exception as e:
                    pdf_fail += 1
                    if args.verbose:
                        print(f"  backfill FAIL {imid[:50]}: {e}")
        print(f"refresh_stage: PDF backfill — {pdf_backfilled} new, {pdf_skipped} already-cached, {pdf_fail} failed")

    return body_fetch_exit_code(body_count, body_failures)


def body_fetch_exit_code(body_count: int, body_failures: int) -> int:
    """Non-zero ONLY when body fetching is DEAD — failures with not one
    success, the signature of broken auth or no network.

    Until 2026-08-12 ANY body-fetch failure returned 1, and the whole fire
    step runs under bash -e: the first verification fire after the phantom-
    quote fixes died because ONE message (an Evergreen GRI ALERT) failed its
    Graph GET — 46 of 47 bodies fetched, pipeline never ran, no email. Worse
    than disproportionate, it was a permanent trap: a fetch-failed message
    never lands in the bodies file, so it is retried every run, and a message
    DELETED from the mailbox would fail every fire forever.

    A failed body is not lost data — the staged record stays and the fetch
    retries next run. Same rule as the 2026-07-30 snapshot-backup lesson in
    daily.yml: a safety net must never hold the client report hostage. The
    per-message FAIL lines above stay loud, and QC-009/QC-077 independently
    catch the downstream symptoms if bodies go systematically missing.
    """
    return 1 if (body_failures > 0 and body_count == 0) else 0


if __name__ == "__main__":
    sys.exit(main())
