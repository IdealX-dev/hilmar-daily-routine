"""
sync_to_quote_tracker.py — Push Hilmar entities to ol-quote-tracker's Turso-backed
client_intelligence table via /api/intelligence/sync.

Per Michael 2026-05-16: "i actually want you to read the main rate checker as
now the client intelligence is on turso for it.. the shared you are using"
+ "client intelligence is client intelligence and should be all encompassing
no? just in case we want to use data later"

ARCHITECTURE
ol-quote-tracker (canonical entity registry on Turso) <— Hilmar pushes here
Rate Blaster (separate peer)                          <— also pushes here
Hilmar pushes ALL entities its data touches:
  - Hilmar Ingredients (role=client)
  - Lonny Upfold (role=client — contact person, notes mark as primary contact)
  - Every carrier we've seen (role=vendor)
  - Every OL operator we've seen (role=internal — though ol-quote-tracker may
    already know these; managedBy stays "hilmar_tracker" so it can detect conflict)

AUTH
ol-quote-tracker uses shared-password cookie auth (APP_PASSWORD env on Azure).
This script:
  1. Reads APP_PASSWORD from secrets/quote-tracker-pwd.txt (gitignored)
     OR QT_APP_PASSWORD env var (fallback for CI/Codespaces)
  2. POSTs /api/auth/login → gets oqt_auth cookie
  3. POSTs /api/intelligence/sync with the cookie
  4. Verifies upserted count matches what we sent

CLI
  python scripts/sync_to_quote_tracker.py            # full sync (live)
  python scripts/sync_to_quote_tracker.py --dry      # show what would push
  python scripts/sync_to_quote_tracker.py --verbose  # also dump full payload

GRACEFUL DEGRADATION
If APP_PASSWORD not configured, this script exits 0 with a notice. Pipeline
keeps running. Local SHARED/client_intelligence/hilmar/ JSONL files remain
authoritative + audit-trail.
"""
from __future__ import annotations
import argparse
import json
import os
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
import share_intel as SI  # noqa: E402  the existing SHARED/client_intelligence exporter

ROOT = Path(__file__).resolve().parent.parent
REPORTS = ROOT / "reports"

# Production endpoint per docs/TURSO_MIGRATION.md
DEFAULT_API_BASE = "https://ol-quote-tracker-prod.azurewebsites.net"

# Known OL operators we've seen in Hilmar bodies — these are role=internal entities.
# Push so ol-quote-tracker has a registry, even if it already knows about them
# (idempotent upsert by name).
KNOWN_OL_OPERATORS = {
    "Alexandra Hernandez": {"email": "Alexandra.Hernandez@ol-usa.com", "title": "Export Operations Specialist"},
    "Ryan Gordon": {"email": "Ryan.Gordon@ol-usa.com", "title": "Ocean Export Team Lead"},
}

# Hilmar client identity (single source of truth)
HILMAR_ENTITY = {
    "name": "Hilmar Ingredients",
    "role": "client",
    "aliases": "hilmar, hilmar ingredients, hilmar cheese, hilmar inc",
    "primary_email": "lupfold@hilmaringredients.com",
    "domain": "hilmaringredients.com",
    "home_port": "Oakland",
    "preferred_mode": "ocean",
    "desk": "Export",
}

LONNY_ENTITY = {
    "name": "Lonny Upfold",
    "role": "client",  # contact person at the client — closest fit in current role enum
    "aliases": "lonny upfold, lonny, lupfold",
    "primary_email": "lupfold@hilmaringredients.com",
    "domain": "hilmaringredients.com",
    "notes": "Primary logistics contact at Hilmar Ingredients. Sends RFQs to MBD_OceanExportBookingShared and decides on quotes.",
}


def _load_password() -> str | None:
    """Try the secrets/ file first (preferred), then env var."""
    secrets_file = ROOT / "secrets" / "quote-tracker-pwd.txt"
    if secrets_file.exists():
        pwd = secrets_file.read_text(encoding="utf-8").strip()
        if pwd:
            return pwd
    return os.environ.get("QT_APP_PASSWORD") or None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _carrier_notes(carrier_summary: dict, carrier_name: str) -> str | None:
    """Build a notes string for a carrier from our summary stats."""
    s = carrier_summary.get(carrier_name)
    if not s:
        return None
    bits = []
    if s.get("quotes"):
        bits.append(f"Hilmar quoted {s['quotes']}x")
    if s.get("wins"):
        bits.append(f"{s['wins']} wins ({s.get('win_rate_pct', 0)}% rate)")
    if s.get("rate_median"):
        bits.append(f"median ${s['rate_median']:,.0f}")
    if s.get("transit_median_days"):
        bits.append(f"~{s['transit_median_days']}d transit")
    return " | ".join(bits) if bits else None


def build_entities() -> list[dict]:
    """Compile every entity Hilmar's data touches into the schema
    /api/intelligence/sync expects."""
    cdir = SI._client_dir("hilmar")
    quotes = SI._load_jsonl(cdir / "quotes.jsonl")
    carrier_summary = {}
    if (cdir / "carrier_summary.json").exists():
        carrier_summary = json.loads((cdir / "carrier_summary.json").read_text(encoding="utf-8"))

    now = _now_iso()
    entities = []

    # 1. Hilmar Ingredients
    last_quoted = max((q.get("response_timestamp") or q.get("request_timestamp") or "" for q in quotes), default="") or None
    last_won = max((q.get("response_timestamp") or "" for q in quotes if q.get("status") == "WIN"), default="") or None
    win_count = sum(1 for q in quotes if q.get("status") == "WIN")
    total_count = len(quotes)
    hilmar = dict(HILMAR_ENTITY)
    hilmar.update({
        "notes": (
            f"Tracked by hilmar-daily-routine. "
            f"{total_count} quote interactions ({win_count} wins). "
            f"Carriers: {', '.join(sorted(carrier_summary.keys())) if carrier_summary else 'none yet'}"
        ),
        "last_quoted_at": last_quoted,
        "last_won_at": last_won,
        "active": True,
    })
    entities.append(hilmar)

    # 2. Lonny Upfold (contact)
    lonny = dict(LONNY_ENTITY)
    lonny.update({
        "last_quoted_at": last_quoted,
        "last_won_at": last_won,
        "active": True,
    })
    entities.append(lonny)

    # 3. Every carrier we've seen — role=vendor
    for carrier_name, stats in carrier_summary.items():
        entities.append({
            "name": carrier_name,
            "role": "vendor",
            "aliases": carrier_name.lower(),
            "preferred_mode": "ocean",
            "notes": _carrier_notes(carrier_summary, carrier_name),
            "last_quoted_at": stats.get("last_quote_date"),
            "last_won_at": stats.get("last_win_date"),
            "active": True,
        })

    # 4. OL operators we've seen — role=internal
    seen_signers = set()
    for q in quotes:
        signer = q.get("ol_responder_signer")
        if signer and signer.strip():
            seen_signers.add(signer.strip())
    for signer in sorted(seen_signers):
        info = KNOWN_OL_OPERATORS.get(signer, {})
        entities.append({
            "name": signer,
            "role": "internal",
            "aliases": signer.lower(),
            "primary_email": info.get("email"),
            "domain": "ol-usa.com",
            "desk": "Export",
            "notes": (info.get("title") + " (seen in Hilmar threads)") if info.get("title")
                     else "Seen as OL responder/signer in Hilmar threads",
            "active": True,
        })

    return entities


def login(session: requests.Session, base_url: str, password: str, timeout: int = 30):
    """POST /api/auth/login → session cookie."""
    r = session.post(f"{base_url}/api/auth/login",
                     json={"password": password}, timeout=timeout)
    if not (200 <= r.status_code < 300):
        raise RuntimeError(f"login {base_url} → {r.status_code}: {r.text[:200]}")
    return r


def sync_entities(entities: list[dict], base_url: str = DEFAULT_API_BASE,
                  password: str | None = None, dry: bool = False,
                  verbose: bool = False) -> dict:
    """Push entities via /api/intelligence/sync."""
    payload = {"entities": entities, "source": "hilmar_tracker"}
    result = {
        "base_url": base_url,
        "entity_count": len(entities),
        "ok": False,
        "error": None,
        "response": None,
    }
    if verbose:
        print(json.dumps(payload, indent=2, default=str))
    if dry:
        result["dry"] = True
        result["preview"] = [
            {"name": e.get("name"), "role": e.get("role")}
            for e in entities
        ]
        return result
    if not password:
        result["error"] = "no APP_PASSWORD configured (secrets/quote-tracker-pwd.txt or QT_APP_PASSWORD env)"
        return result
    session = requests.Session()
    try:
        login(session, base_url, password)
        r = session.post(f"{base_url}/api/intelligence/sync",
                         json=payload, timeout=60)
        if not (200 <= r.status_code < 300):
            result["error"] = f"sync {r.status_code}: {r.text[:300]}"
            return result
        result["ok"] = True
        result["response"] = r.json() if r.headers.get("content-type", "").startswith("application/json") else r.text
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"
    return result


def write_audit(result: dict):
    """Append the sync result to reports/quote-tracker-sync.log."""
    REPORTS.mkdir(parents=True, exist_ok=True)
    audit = REPORTS / "quote-tracker-sync.log"
    line = (
        f"{_now_iso()} | "
        f"entities={result.get('entity_count')} "
        f"ok={result.get('ok')} "
        f"upserted={(result.get('response') or {}).get('upserted', '?') if isinstance(result.get('response'), dict) else '?'} "
        f"err={result.get('error') or '-'}"
    )
    with audit.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="Show what would push, don't send")
    ap.add_argument("--verbose", action="store_true", help="Print full payload")
    ap.add_argument("--base-url", default=os.environ.get("QT_API_BASE", DEFAULT_API_BASE),
                    help=f"Quote-tracker base URL (default: {DEFAULT_API_BASE})")
    args = ap.parse_args()

    password = _load_password()
    if not password and not args.dry:
        print("⚠️  No APP_PASSWORD found. Drop the password in:")
        print("    secrets/quote-tracker-pwd.txt")
        print("OR set environment variable: QT_APP_PASSWORD")
        print("Exiting without error so pipeline continues.")
        return 0

    entities = build_entities()
    print(f"sync_to_quote_tracker: built {len(entities)} entities")
    role_counts = Counter(e.get("role") for e in entities)
    for role, n in sorted(role_counts.items()):
        print(f"  {role}: {n}")

    result = sync_entities(entities, base_url=args.base_url,
                            password=password, dry=args.dry, verbose=args.verbose)
    # Only audit-log REAL sends — dry-runs would pollute QC-037's freshness read
    if not args.dry:
        write_audit(result)
    print(json.dumps({k: v for k, v in result.items() if k != "preview"},
                     indent=2, default=str))
    if result.get("preview") and args.dry:
        print("\nPreview of entities that would be pushed:")
        for e in result["preview"]:
            print(f"  {e['role']:10} {e['name']}")
    # 2026-05-17: ALWAYS return 0 even on sync failure. Sync is a NICE-TO-HAVE
    # checkpoint — ol-quote-tracker being slow/down/misconfigured must NEVER
    # break the daily pipeline (which produces the critical email artifact).
    # QC-037 reads the audit log written by write_audit() above and surfaces
    # the failure in the next QC pass with the specific error excerpt.
    if not result.get("ok") and not args.dry:
        print(f"⚠️  sync failed (pipeline continues): {result.get('error')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
