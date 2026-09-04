"""diag_refs_all_only.py — size the win-count change BEFORE it reaches Lonny.

WHY THIS EXISTS. PR #254 makes `scripts/core.decide_status` read the UNION of
`mdolx_ref` and `mdolx_refs_all`, matching `src/hilmar/core.decide_status`,
`core.booking_count` and `core.is_confirmed_win`, all of which already did.
Production was calling a row a loss while it held an OL booking reference.

The fix is correct. What is NOT known is how many live rows it moves, and the
change is not cosmetic:

  * wins, win_rate and TEU shift, because `booking_count` gates on the STORED
    status — a row flipped WIN counts its refs, a row flipped LOSS counts 0.
  * `gen_client_email` renders `is_confirmed_win` rows under "Your confirmed
    bookings", so a row invisible to Lonny yesterday appears tomorrow.
  * `.github/workflows/daily.yml:381` resolves
    `SEND_TO: github.event_name == 'schedule' && 'full'` — a SCHEDULED fire is
    always the full distribution. The `send_to=test` default guards manual
    dispatch only. So merging is the send decision, and a send is
    irreversible.

CLAUDE.md: measure the thing before you write it up, and get written approval
before an irreversible send. This prints the number that decision needs.

READ-ONLY, and deliberately so:
  * pulls state into a temp dir; never writes the blob, never mutates the
    pulled file (every decision is computed on a copy),
  * sends no mail, touches no flag,
  * prints request_ids and MDOLX refs — the same provenance
    `diag_duplicate_mdolx.py` already prints — but NOT lanes, rates or
    addresses, because PR #254 established that the run log is not a
    PII-clean channel.

Fails LOUDLY (`::error::` + non-zero) rather than going green on no data:
a diagnostic that cannot fail is worse than none — the 2026-08-20 `|| true`.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import core  # noqa: E402


def is_hazard_shape(row: dict) -> bool:
    """The row shape the parity gap acted on: a booking reference present, but
    only in the list — so the OLD decide_status could not see it."""
    return (not (row.get("mdolx_ref") or "").strip()
            if isinstance(row.get("mdolx_ref"), str)
            else not row.get("mdolx_ref")) and bool(row.get("mdolx_refs_all"))


def _decide(row: dict, *, union: bool, lane_medians: dict):
    """Run production's classifier the OLD way (primary ref only) or the NEW
    way (union). Same call shape qc_selfheal's decide loop uses."""
    kw = dict(
        has_send=row.get("has_send", False),
        mdolx_ref=row.get("mdolx_ref"),
        response_timestamp=row.get("response_timestamp"),
        quoted=row.get("quoted", False),
        etd_fit_days=row.get("etd_fit_days"),
        request_timestamp=row.get("request_timestamp") or row.get("request_date"),
        ol_rate=row.get("ol_rate"),
        lane=row.get("lane"),
        lane_winning_median=lane_medians,
    )
    if union:
        kw["mdolx_refs_all"] = row.get("mdolx_refs_all")
    return core.decide_status(**kw)


def summarise(rows: list) -> dict:
    """Everything the send decision needs, computed both ways."""
    lane_medians = core.compute_lane_winning_medians(rows)

    hazard = [r for r in rows if is_hazard_shape(r)]
    by_status: dict = {}
    for r in hazard:
        by_status[r.get("status") or "(none)"] = \
            by_status.get(r.get("status") or "(none)", 0) + 1

    flips = []
    for r in hazard:
        old = _decide(r, union=False, lane_medians=lane_medians)
        new = _decide(r, union=True, lane_medians=lane_medians)
        if old.status != new.status:
            flips.append({
                "request_id": r.get("request_id"),
                "stored": r.get("status"),
                "old": old.status,
                "new": new.status,
                "refs": sorted(str(m) for m in (r.get("mdolx_refs_all") or [])),
                "teu": r.get("teu_won") or r.get("teu_requested") or 0,
            })

    # Headline movement, computed on COPIES so the pulled file is untouched.
    def _wins_teu(apply_new: bool) -> tuple:
        wins = teu = 0
        flip_map = {f["request_id"]: f["new"] for f in flips} if apply_new else {}
        for r in rows:
            status = flip_map.get(r.get("request_id"), r.get("status"))
            probe = dict(r, status=status)
            wins += core.booking_count(probe)
            if (status or "").upper() == "WIN":
                teu += r.get("teu_won") or 0
        return wins, teu

    wins_before, teu_before = _wins_teu(False)
    wins_after, teu_after = _wins_teu(True)
    entries = sum(core.shipment_count(r) for r in rows)

    return {
        "rows": len(rows),
        "hazard_rows": len(hazard),
        "hazard_by_status": by_status,
        "preserved_from_prior": sum(
            1 for r in hazard if r.get("preserved_from_prior")),
        "flips": flips,
        "wins_before": wins_before,
        "wins_after": wins_after,
        "teu_before": teu_before,
        "teu_after": teu_after,
        "entries": entries,
    }


def render(s: dict) -> str:
    out = [
        "",
        "=" * 70,
        "PARITY IMPACT — rows whose only booking ref is in mdolx_refs_all",
        "=" * 70,
        f"  tracking rows scanned              {s['rows']}",
        f"  rows in the hazard shape           {s['hazard_rows']}",
        f"    of those, preserved_from_prior   {s['preserved_from_prior']}",
    ]
    for st, n in sorted(s["hazard_by_status"].items()):
        out.append(f"    stored status {st:<12}     {n}")
    out += ["", f"  rows the union CHANGES             {len(s['flips'])}"]
    for f in s["flips"]:
        out.append(f"    {f['request_id']}  stored={f['stored']}  "
                   f"{f['old']} -> {f['new']}  refs={','.join(f['refs'])}  "
                   f"teu={f['teu']}")
    wb, wa = s["wins_before"], s["wins_after"]
    tb, ta = s["teu_before"], s["teu_after"]
    ent = s["entries"] or 1
    out += [
        "",
        "  HEADLINE MOVEMENT (what Lonny and the staff list would see):",
        f"    bookings counted   {wb}  ->  {wa}   ({wa - wb:+d})",
        f"    win rate           {wb / ent:.1%}  ->  {wa / ent:.1%}"
        f"   (over {ent} entries)",
        f"    TEU won            {tb}  ->  {ta}   ({ta - tb:+d})",
        "",
        "  A SCHEDULED FIRE IS ALWAYS send_to=full (daily.yml:381).",
        "  If these numbers are wrong, they are wrong IN THE CLIENT REPORT.",
        "=" * 70,
        "",
    ]
    return "\n".join(out)


def main() -> int:
    import state_store

    tmp = Path(tempfile.mkdtemp())
    try:
        pulled = state_store.pull(root=tmp)
    except Exception as e:
        print(f"::error::diag_refs_all_only: pull FAILED: {type(e).__name__}: {e}")
        return 2
    print(f"pulled {len(pulled)} file(s) into a temp dir")

    data_path = tmp / "tracking-data-v2.json"
    if not data_path.exists():
        print("::error::diag_refs_all_only: tracking-data-v2.json not in the store")
        return 2
    rows = json.loads(data_path.read_text(encoding="utf-8")).get("requests", [])
    if not rows:
        print("::error::diag_refs_all_only: zero requests in the pulled state")
        return 2

    print(render(summarise(rows)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
