"""
share_intel.py — Export Hilmar rate/lane intelligence to the SHARED
cross-project client-intelligence store.

Per Michael 2026-05-13: "i love all of this and want it now   it's also
what i will want in the rate tracker system we are building where all the
data for moves also is shared (for the win/s losses/etc)"

DESIGN: a single shared OneDrive folder structure that ANY of Michael's
client-tracking systems (Hilmar Tracker, Rate Tracker, Rate Blaster, future
clients) can read/write. Each client gets a folder; each folder holds the
same shape of artifacts; cross-client insights become trivial.

  SHARED/client_intelligence/
      _meta.json                  — registry + schema version
      hilmar/
          quotes.jsonl            — append-only every quote received
          wins.jsonl              — append-only every confirmed booking
          carrier_summary.json    — rolled-up carrier performance (current state)
          lane_summary.json       — rolled-up lane performance (current state)
          _client_meta.json       — Hilmar-specific config + last-updated stamp
      akamai/  (when rate-blaster integrates)
          ...same shape...
      henco/   (when ready)
          ...same shape...

The append-only logs let us reconstruct history. The rolled-up summaries
are derived (rebuildable from logs) for fast consumption.

LOCATION: %USERPROFILE%\\OneDrive - IdealX\\SHARED\\client_intelligence\\
(falls back to %USERPROFILE%\\OneDrive\\SHARED if IdealX-renamed OneDrive
isn't present). OneDrive sync means Cloud PC, MBD-TRAVEL, and the rate-
tracker machines all see the same data.

CLI:
    python scripts/share_intel.py export       # run after each pipeline fire
    python scripts/share_intel.py validate     # check schema integrity
    python scripts/share_intel.py read <client> # dump a client's summaries
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import core  # noqa: E402

# Hilmar local tracker location (this project)
HILMAR_ROOT = Path(__file__).resolve().parent.parent
HILMAR_DATA = HILMAR_ROOT / "tracking-data-v2.json"

CLIENT_ID = "hilmar"  # this script writes Hilmar's data; rate-blaster's mirror writes its own clients
SCHEMA_VERSION = 1


def _shared_root() -> Path:
    """Resolve the shared cross-project intel folder. Tries IdealX-renamed
    OneDrive first, falls back to the default OneDrive."""
    home = Path(os.environ.get("USERPROFILE", os.path.expanduser("~")))
    candidates = [
        home / "OneDrive - IdealX" / "SHARED" / "client_intelligence",
        home / "OneDrive" / "SHARED" / "client_intelligence",
        # Fallback to local if no OneDrive folder exists (for CI / Codespaces)
        HILMAR_ROOT.parent / "SHARED" / "client_intelligence",
    ]
    # Pick first existing parent (OneDrive folder must exist; SHARED is created)
    for c in candidates:
        if c.parent.parent.exists():  # the OneDrive folder above SHARED
            c.mkdir(parents=True, exist_ok=True)
            return c
    # Last resort
    fallback = candidates[-1]
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


def _client_dir(client_id: str = CLIENT_ID) -> Path:
    d = _shared_root() / client_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_fingerprint(r: dict) -> str:
    """Stable hash for dedup — same request_id + status + response_timestamp
    yields the same fingerprint."""
    key = "|".join([
        r.get("request_id") or "",
        r.get("status") or "",
        r.get("response_timestamp") or "",
        r.get("carrier_quoted") or "",
        str(r.get("ol_rate") or ""),
    ])
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def _load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line:
            continue
        with contextlib.suppress(Exception):
            out.append(json.loads(line))
    return out


def _append_jsonl(path: Path, rows: list[dict]) -> int:
    """Append rows to a JSONL file, deduping by fingerprint against existing."""
    existing_fps = {r.get("_fp") for r in _load_jsonl(path)}
    new_rows = [r for r in rows if r.get("_fp") not in existing_fps]
    if not new_rows:
        return 0
    with path.open("a", encoding="utf-8") as f:
        for r in new_rows:
            f.write(json.dumps(r, default=str) + "\n")
    return len(new_rows)


def export_from_hilmar() -> dict:
    """Read Hilmar's tracking-data-v2.json and export to the shared store.
    Returns a summary dict for the pipeline log."""
    if not HILMAR_DATA.exists():
        return {"error": "tracking-data-v2.json not found", "client": CLIENT_ID}

    data = json.loads(HILMAR_DATA.read_text(encoding="utf-8"))
    requests = data.get("requests", []) or []
    cdir = _client_dir(CLIENT_ID)

    # 1. Append every quoted/won row to quotes.jsonl
    quote_rows = []
    win_rows = []
    for r in requests:
        if r.get("status") not in ("WIN", "LOSS", "PENDING"):
            continue
        fp = _row_fingerprint(r)
        quote_row = {
            "_fp": fp,
            "_client": CLIENT_ID,
            "request_id": r.get("request_id"),
            "status": r.get("status"),
            "loss_reason": r.get("loss_reason"),
            "request_date": r.get("request_date"),
            "request_timestamp": r.get("request_timestamp"),
            "response_timestamp": r.get("response_timestamp"),
            "origin": r.get("origin"),
            "destination": r.get("destination"),
            "lane": r.get("lane"),
            "pol": r.get("pol"),
            "pod": r.get("pod"),
            "containers": r.get("containers"),
            "container_size": r.get("container_size"),
            "container_count": r.get("container_count"),
            "teu_requested": r.get("teu_requested"),
            "teu_won": r.get("teu_won"),
            "carrier_quoted": r.get("carrier_quoted"),
            "carrier_won": r.get("carrier_won"),
            "ol_rate": r.get("ol_rate"),
            "etd_offered": r.get("etd_offered"),
            "eta_offered": r.get("eta_offered"),
            "etd_requested": r.get("etd_requested"),
            "eta_requested": r.get("eta_requested"),
            "vessel_voyage": r.get("vessel_voyage"),
            "transshipment": r.get("transshipment"),
            "turnaround_biz_hours": r.get("turnaround_biz_hours"),
            "ol_responder": r.get("ol_responder"),
            "ol_responder_signer": r.get("ol_responder_signer"),
            "mdolx_ref": r.get("mdolx_ref"),
            "exported_at": _now_iso(),
        }
        quote_rows.append(quote_row)
        if r.get("status") == "WIN":
            win_rows.append(quote_row)

    new_quotes = _append_jsonl(cdir / "quotes.jsonl", quote_rows)
    new_wins = _append_jsonl(cdir / "wins.jsonl", win_rows)

    # 2. Rebuild rolled-up summaries (overwrites, since they're derived)
    carrier_stats = _build_carrier_summary(quote_rows)
    lane_stats = _build_lane_summary(quote_rows)
    (cdir / "carrier_summary.json").write_text(
        json.dumps(carrier_stats, indent=2, default=str), encoding="utf-8"
    )
    (cdir / "lane_summary.json").write_text(
        json.dumps(lane_stats, indent=2, default=str), encoding="utf-8"
    )

    # 3. Update client meta
    meta = {
        "client_id": CLIENT_ID,
        "last_updated": _now_iso(),
        "schema_version": SCHEMA_VERSION,
        "row_count": len(quote_rows),
        "win_count": len(win_rows),
        "source_system": "hilmar-daily-routine",
        "source_data": str(HILMAR_DATA),
        "carrier_count": len(carrier_stats),
        "lane_count": len(lane_stats),
    }
    (cdir / "_client_meta.json").write_text(
        json.dumps(meta, indent=2), encoding="utf-8"
    )

    # 4. Update global registry _meta.json
    _update_global_meta()

    return {
        "client": CLIENT_ID,
        "shared_dir": str(cdir),
        "new_quotes_appended": new_quotes,
        "new_wins_appended": new_wins,
        "total_quotes": len(quote_rows),
        "total_wins": len(win_rows),
        "carriers": len(carrier_stats),
        "lanes": len(lane_stats),
    }


def _parse_loose_date(s: str | None):
    """Kept as a name; the RULE now lives in core.offered_date.

    2026-08-10: this was the THIRD parser for OL's offered ETD/ETA cells —
    core._parse_loose_date, this one, and gen_email._iso_date — and the three
    disagreed. The client-facing renderers held the strict ISO one, so a
    `26-May-26` ETA counted as populated for QC-027 and was invisible in
    Lonny's "Currently in transit" table, while THIS feed parsed it fine.
    One field, three readers, and the one that reached the customer was wrong.

    Its extra fallbacks (non-zero-padded M/D/YY) were folded into
    core.offered_date, so nothing this module could parse before is lost.
    """
    return core.offered_date(s)


def _transit_days(row: dict) -> int | None:
    """ETA - ETD in days. Returns None if either date missing or unparseable."""
    etd = _parse_loose_date(row.get("etd_offered"))
    eta = _parse_loose_date(row.get("eta_offered"))
    if etd and eta:
        d = (eta - etd).days
        if 1 <= d <= 90:  # sanity bounds — anything outside is suspect
            return d
    return None


def _build_carrier_summary(rows: list[dict]) -> dict:
    """Per-carrier rollup. Used by rate-negotiation cheat sheet."""
    by_carrier: dict[str, dict] = defaultdict(lambda: {
        "quotes": 0,
        "wins": 0,
        "losses": 0,
        "teu_won": 0,
        "teu_lost": 0,
        "rates": [],
        "transit_days": [],   # NEW 2026-05-14: ETA - ETD per quote
        "lanes": set(),
        "last_quote_date": None,
        "last_win_date": None,
    })
    for r in rows:
        c = r.get("carrier_quoted") or r.get("carrier_won")
        if not c:
            continue
        b = by_carrier[c]
        b["quotes"] += 1
        b["lanes"].add(r.get("lane"))
        _rate = r.get("ol_rate")
        if isinstance(_rate, (int, float)):
            b["rates"].append(float(_rate))
        elif _rate:
            # ol_rate is occasionally a non-numeric string ("Not Quoted") on
            # rows where the rate never resolved — coerce when possible, skip
            # otherwise (was crashing the best-effort share step, 2026-06-16).
            with contextlib.suppress(ValueError, TypeError):
                b["rates"].append(float(str(_rate).replace("$", "").replace(",", "").strip()))
        td = _transit_days(r)
        if td is not None:
            b["transit_days"].append(td)
        if r["status"] == "WIN":
            b["wins"] += 1
            b["teu_won"] += int(r.get("teu_won") or r.get("teu_requested") or 0)
            d = r.get("request_date")
            if d and (b["last_win_date"] is None or d > b["last_win_date"]):
                b["last_win_date"] = d
        elif r["status"] == "LOSS":
            b["losses"] += 1
            b["teu_lost"] += int(r.get("teu_requested") or 0)
        d = r.get("request_date")
        if d and (b["last_quote_date"] is None or d > b["last_quote_date"]):
            b["last_quote_date"] = d

    out = {}
    for c, b in by_carrier.items():
        win_rate = (b["wins"] / b["quotes"] * 100) if b["quotes"] else 0
        rates = sorted(b["rates"])
        td_sorted = sorted(b["transit_days"])
        out[c] = {
            "quotes": b["quotes"],
            "wins": b["wins"],
            "losses": b["losses"],
            "win_rate_pct": round(win_rate, 1),
            "teu_won": b["teu_won"],
            "teu_lost": b["teu_lost"],
            "rate_count": len(rates),
            "rate_min": rates[0] if rates else None,
            "rate_median": rates[len(rates) // 2] if rates else None,
            "rate_max": rates[-1] if rates else None,
            "transit_count": len(td_sorted),
            "transit_min_days": td_sorted[0] if td_sorted else None,
            "transit_median_days": td_sorted[len(td_sorted) // 2] if td_sorted else None,
            "transit_max_days": td_sorted[-1] if td_sorted else None,
            "lane_count": len(b["lanes"]),
            "last_quote_date": b["last_quote_date"],
            "last_win_date": b["last_win_date"],
        }
    return out


def _build_lane_summary(rows: list[dict]) -> dict:
    """Per-lane rollup. Used by rate-negotiation cheat sheet."""
    by_lane: dict[str, dict] = defaultdict(lambda: {
        "quotes": 0,
        "wins": 0,
        "losses": 0,
        "teu_won": 0,
        "teu_requested": 0,
        "winning_carriers": set(),
        "all_carriers": set(),
        "rates_won": [],
        "rates_lost": [],
        "transit_days": [],   # NEW 2026-05-14: ETA - ETD across all quotes on this lane
        "last_request_date": None,
    })
    for r in rows:
        lane = r.get("lane")
        if not lane:
            continue
        b = by_lane[lane]
        b["quotes"] += 1
        b["teu_requested"] += int(r.get("teu_requested") or 0)
        c = r.get("carrier_quoted") or r.get("carrier_won")
        if c:
            b["all_carriers"].add(c)
        td = _transit_days(r)
        if td is not None:
            b["transit_days"].append(td)
        if r["status"] == "WIN":
            b["wins"] += 1
            b["teu_won"] += int(r.get("teu_won") or r.get("teu_requested") or 0)
            if c:
                b["winning_carriers"].add(c)
            if r.get("ol_rate"):
                b["rates_won"].append(float(r["ol_rate"]))
        elif r["status"] == "LOSS" and r.get("loss_reason") != "NO_RESPONSE":
            b["losses"] += 1
            if r.get("ol_rate"):
                b["rates_lost"].append(float(r["ol_rate"]))
        d = r.get("request_date")
        if d and (b["last_request_date"] is None or d > b["last_request_date"]):
            b["last_request_date"] = d

    out = {}
    for lane, b in by_lane.items():
        win_rate = (b["wins"] / b["quotes"] * 100) if b["quotes"] else 0
        rw = sorted(b["rates_won"])
        rl = sorted(b["rates_lost"])
        td = sorted(b["transit_days"])
        out[lane] = {
            "quotes": b["quotes"],
            "wins": b["wins"],
            "losses": b["losses"],
            "win_rate_pct": round(win_rate, 1),
            "teu_won": b["teu_won"],
            "teu_requested": b["teu_requested"],
            "winning_carriers": sorted(b["winning_carriers"]),
            "all_carriers": sorted(b["all_carriers"]),
            "rate_won_min": rw[0] if rw else None,
            "rate_won_median": rw[len(rw) // 2] if rw else None,
            "rate_won_max": rw[-1] if rw else None,
            "rate_lost_min": rl[0] if rl else None,
            "rate_lost_median": rl[len(rl) // 2] if rl else None,
            "rate_lost_max": rl[-1] if rl else None,
            "price_gap_median": (rl[len(rl) // 2] - rw[len(rw) // 2])
                if (rw and rl) else None,
            "transit_median_days": td[len(td) // 2] if td else None,
            "transit_min_days": td[0] if td else None,
            "transit_max_days": td[-1] if td else None,
            "last_request_date": b["last_request_date"],
        }
    return out


def _ensure_schema_doc(root: Path) -> bool:
    """Bootstrap SHARED/client_intelligence/SCHEMA.md from the in-repo source.

    QC-031 watches for this file. It's the contract cross-project integrators
    read to consume the shared store. The canonical copy lives at
    docs/SHARED_CLIENT_INTELLIGENCE_SCHEMA.md in this repo; we mirror it into
    the shared folder so any project pointed at the shared folder finds it
    without a separate repo clone.

    Returns True if the file was created or refreshed, False if unchanged.
    """
    src = HILMAR_ROOT / "docs" / "SHARED_CLIENT_INTELLIGENCE_SCHEMA.md"
    dst = root / "SCHEMA.md"
    if not src.exists():
        return False
    src_bytes = src.read_bytes()
    if dst.exists() and dst.read_bytes() == src_bytes:
        return False
    dst.write_bytes(src_bytes)
    return True


def _update_global_meta():
    """Update SHARED/client_intelligence/_meta.json with the registry of
    known clients and their freshness."""
    root = _shared_root()
    _ensure_schema_doc(root)
    clients = {}
    for cdir in root.iterdir():
        if not cdir.is_dir() or cdir.name.startswith("_"):
            continue
        meta_file = cdir / "_client_meta.json"
        if meta_file.exists():
            with contextlib.suppress(Exception):
                clients[cdir.name] = json.loads(meta_file.read_text(encoding="utf-8"))
    global_meta = {
        "schema_version": SCHEMA_VERSION,
        "last_updated": _now_iso(),
        "clients": list(clients.keys()),
        "client_metadata": clients,
    }
    (root / "_meta.json").write_text(json.dumps(global_meta, indent=2), encoding="utf-8")


def validate() -> dict:
    """Schema + freshness check across all clients in the shared store."""
    root = _shared_root()
    results = {"root": str(root), "clients": {}}
    for cdir in root.iterdir():
        if not cdir.is_dir() or cdir.name.startswith("_"):
            continue
        c = cdir.name
        results["clients"][c] = {
            "quotes_jsonl_rows": len(_load_jsonl(cdir / "quotes.jsonl")),
            "wins_jsonl_rows": len(_load_jsonl(cdir / "wins.jsonl")),
            "carrier_summary_present": (cdir / "carrier_summary.json").exists(),
            "lane_summary_present": (cdir / "lane_summary.json").exists(),
            "meta_present": (cdir / "_client_meta.json").exists(),
        }
    return results


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["export", "validate", "read"],
                    help="export = push Hilmar data; validate = check schema; "
                         "read = dump a client's summaries")
    ap.add_argument("--client", default=CLIENT_ID, help="client_id for read command")
    args = ap.parse_args()

    if args.cmd == "export":
        result = export_from_hilmar()
        print(json.dumps(result, indent=2, default=str))
        return 0 if "error" not in result else 1
    elif args.cmd == "validate":
        print(json.dumps(validate(), indent=2, default=str))
        return 0
    elif args.cmd == "read":
        cdir = _client_dir(args.client)
        for fname in ("carrier_summary.json", "lane_summary.json", "_client_meta.json"):
            fp = cdir / fname
            if fp.exists():
                print(f"=== {fname} ===")
                print(fp.read_text(encoding="utf-8"))
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
