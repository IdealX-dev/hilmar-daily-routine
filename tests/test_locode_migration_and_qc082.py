"""The migration the UN/LOCODE merge requires, and the alarm that outlives it.

`core.request_id` hashes the destination, so renaming a stored destination
re-keys the row. `operator_corrections.json` — the ONLY durable human state in
a system that rebuilds every row from staged mail each fire — is matched by
that key, and `ingest.apply_operator_corrections` handles a miss with a
`print(...)` and carries on. So the failure is total and silent: Michael's
verdict simply stops applying.

Two things ship for that. `scripts/migrate_locode_rekey.py` is the one-time
repair (scripted, reversible, logged, per CLAUDE.md §3) and QC-082 is the
standing detector, because the NEXT rename will do the same thing and nobody
will be watching for it either.
"""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import core as core  # noqa: E402
import migrate_locode_rekey as mig  # noqa: E402
import qc_selfheal as qc  # noqa: E402

IMID = "<AAA@ol-usa.com>"
TS = "2026-08-20T17:04:00Z"


def _row(dest, imid=IMID, ts=TS, **kw):
    """A row whose stored id is genuinely derived from its own fields — the
    same relationship ingest.build_requests creates."""
    r = {
        "request_id": core.request_id(imid, ts, dest),
        "destination": dest,
        "request_timestamp": ts,
        "source_imids": [imid],
        "status": "WIN",
    }
    r.update(kw)
    return r


# ── the migration ─────────────────────────────────────────────────────────

def test_plan_finds_the_row_and_the_correction_it_orphans():
    rows = [_row("Jpyok", mdolx_ref="261099"), _row("Yokohama", imid="<B>")]
    corrections = [{"request_id": rows[0]["request_id"], "set": {"status": "LOSS"},
                    "note": "Linda audit"}]
    p = mig.plan(rows, corrections)
    assert len(p["affected"]) == 1
    a = p["affected"][0]
    assert a["old_destination"] == "Jpyok"
    assert a["new_destination"] == "Yokohama"
    assert a["new_request_id"] == core.request_id(IMID, TS, "Yokohama")
    assert a["new_request_id"] != a["request_id"]
    # A row that already reads "Yokohama" is not touched at all.
    assert all(m["request_id"] != rows[1]["request_id"] for m in p["moves"])


def test_ids_that_do_not_hash_the_destination_are_renamed_but_never_rekeyed():
    """`stand_<mdolx>` and `ol_<ref>` are derived from the booking number. Their
    DISPLAY changes with the merge; their key does not, so their corrections
    stay valid and must not be rewritten."""
    rows = [{"request_id": "stand_261100", "destination": "JPYOK", "status": "WIN"}]
    p = mig.plan(rows, [{"request_id": "stand_261100", "set": {"status": "WIN"}}])
    assert p["affected"] == []
    assert p["moves"][0]["new_request_id"] == "stand_261100"
    assert p["moves"][0]["new_destination"] == "Yokohama"


def test_a_row_whose_id_is_not_reproducible_is_refused_not_guessed():
    """If recomputing the OLD id from the row's own fields does not reproduce
    the stored id, then the row's key was not derived from those fields — a
    carry-forward, or a destination some later heal rewrote without re-keying.
    Computing a NEW id the same way would be a guess, and a guess here writes a
    human's verdict onto the wrong shipment."""
    rows = [{"request_id": "req_deadbeefdeadbeef", "destination": "Jpyok",
             "request_timestamp": TS, "source_imids": ["<C>"], "status": "LOSS"}]
    p = mig.plan(rows, [])
    assert len(p["unverifiable"]) == 1
    assert p["affected"] == []


def test_pre_existing_staleness_is_reported_separately():
    """A correction that already matched nothing BEFORE this migration must not
    be blamed on it — and must not be quietly re-keyed onto some other row."""
    rows = [_row("Yokohama")]
    p = mig.plan(rows, [{"request_id": "req_neverseen0000", "set": {"status": "WIN"},
                         "note": "orphan"}])
    assert [s["request_id"] for s in p["already_stale"]] == ["req_neverseen0000"]
    assert p["affected"] == []


def test_apply_then_revert_is_an_exact_inverse():
    """CLAUDE.md §3: migrations are REVERSIBLE. The reverse map is not a
    separate artefact that can be lost — it is recorded in the file itself as
    `superseded_request_id`."""
    rows = [_row("Jpyok")]
    corrections = [{"request_id": rows[0]["request_id"], "set": {"status": "LOSS"},
                    "note": "Linda audit"}]
    original = copy.deepcopy(corrections)
    doc = {"corrections": corrections}
    p = mig.plan(rows, corrections)

    assert mig.apply_rekey(doc, p["affected"]) == 1
    assert doc["corrections"][0]["request_id"] == core.request_id(IMID, TS, "Yokohama")
    assert doc["corrections"][0]["superseded_request_id"] == original[0]["request_id"]

    assert mig.revert_rekey(doc) == 1
    assert doc["corrections"] == original, "revert left residue — not an inverse"


def test_apply_is_idempotent_once_the_rows_carry_the_merged_spelling():
    """After the fire rebuilds rows as "Yokohama", the migration has nothing
    left to do. Re-running it must be a no-op, not a second re-key."""
    p = mig.plan([_row("Yokohama")], [{"request_id": core.request_id(IMID, TS, "Yokohama"),
                                       "set": {"status": "LOSS"}}])
    assert p["moves"] == [] and p["affected"] == [] and p["already_stale"] == []


def test_merged_destination_defers_to_the_ingest_normalizer():
    """The migration must not carry its own spelling of the merge rule — two
    spellings of one rule is how the last defect shipped."""
    assert mig.merged_destination("Jpyok") == "Yokohama"
    assert mig.merged_destination("JPYOK") == "Yokohama"
    assert mig.merged_destination("Yokohama") is None    # nothing to do
    assert mig.merged_destination("Busan") is None       # real port, untouched
    assert mig.merged_destination("Unknown") is None
    assert mig.merged_destination(None) is None


def test_the_tracked_corrections_file_is_still_loadable_and_keyed():
    """Guards the --apply output shape against the real file: every entry keeps
    a request_id, and a re-keyed one keeps its prior key for the reverse map."""
    doc = json.loads((ROOT / "scripts" / "operator_corrections.json")
                     .read_text(encoding="utf-8"))
    for corr in doc["corrections"]:
        assert corr.get("request_id"), corr
        if "superseded_request_id" in corr:
            assert corr["superseded_request_id"] != corr["request_id"]
            assert corr.get("superseded_reason")


# ── QC-082, the standing alarm ────────────────────────────────────────────

def _corrections_file(tmp_path, corrections):
    p = tmp_path / "operator_corrections.json"
    p.write_text(json.dumps({"corrections": corrections}), encoding="utf-8")
    return p


def test_qc082_catches_the_orphaned_set_correction(tmp_path):
    path = _corrections_file(tmp_path, [
        {"request_id": "req_live", "set": {"status": "WIN"}},
        {"request_id": "req_gone", "set": {"status": "LOSS"}, "note": "Linda audit"},
    ])
    stale = qc.qc082_stale_operator_corrections([{"request_id": "req_live"}], path)
    assert [rid for rid, _ in stale] == ["req_gone"]


def test_qc082_exempts_create_and_exclude(tmp_path):
    """Absence is the PURPOSE of a `create` (it self-skips once the real
    booking email arrives) and the expected steady state of an `exclude` (a
    fresh ingest already drops the row). Flagging those would make the check
    permanently red and therefore permanently ignored."""
    path = _corrections_file(tmp_path, [
        {"request_id": "ol_260500", "create": True, "set": {"status": "WIN"}},
        {"request_id": "stand_260821", "exclude": True},
    ])
    assert qc.qc082_stale_operator_corrections([], path) == []


def test_qc082_does_not_page_on_a_set_whose_row_a_sibling_exclude_removed(tmp_path):
    """THE REAL ROW, from the first dry run against blob (2026-08-28).

    `stand_260905` carries BOTH of Michael's verdicts: a `set` fixing the lane
    (2026-07-14, "Oakland -> Tokyo ... so it resolves permanently every fire")
    and a later `exclude` (2026-08-13, "260905 260192 260963 were bookings
    hilmar cancelled"). The exclude drops the row, so the `set` matches
    nothing — by design.

    The old test asked only whether THIS correction carried the flag, so the
    `set` looked like an orphaned human verdict and QC-082 raised an ERROR on
    a healthy row, every fire, forever. That is the QC-081 failure mode
    exactly. QC-082 shipped 2026-08-27 and the next daily fire had not yet
    run, so it never reached production.
    """
    path = _corrections_file(tmp_path, [
        {"request_id": "stand_260905",
         "set": {"origin": "Oakland", "destination": "Tokyo"},
         "note": "operator-authoritative lane"},
        {"request_id": "stand_260905", "exclude": True,
         "note": "cancelled booking — not a win"},
    ])
    assert qc.qc082_stale_operator_corrections([], path) == [], (
        "QC-082 paged on a `set` whose row is deliberately excluded by a "
        "sibling correction — an ERROR on healthy data")


def test_qc082_still_catches_an_orphan_when_a_DIFFERENT_id_is_excluded(tmp_path):
    # The exemption must be keyed on the id, not applied to the whole file.
    # Widening it to "any exclude anywhere" would silence the check entirely.
    path = _corrections_file(tmp_path, [
        {"request_id": "stand_111", "exclude": True},
        {"request_id": "req_gone", "set": {"status": "LOSS"}, "note": "orphan"},
    ])
    assert [rid for rid, _ in qc.qc082_stale_operator_corrections([], path)] == ["req_gone"]


def test_the_migration_agrees_with_qc082_about_the_excluded_row(tmp_path):
    """QC-082's remediation message tells a human to run the migration. If the
    two disagree about the same row, the person following that instruction is
    sent to a tool that contradicts the alarm that sent them."""
    corrections = [
        {"request_id": "stand_260905", "set": {"destination": "Tokyo"}},
        {"request_id": "stand_260905", "exclude": True},
    ]
    assert mig.plan([], corrections)["already_stale"] == []
    # and it still reports a genuine orphan
    orphan = [{"request_id": "req_gone", "set": {"status": "LOSS"}}]
    assert [s["request_id"] for s in mig.plan([], orphan)["already_stale"]] == ["req_gone"]


def test_qc082_zero_state_is_reachable(tmp_path):
    """A check whose green state is unreachable is noise. This one's is real."""
    path = _corrections_file(tmp_path, [{"request_id": "req_live", "set": {}}])
    assert qc.qc082_stale_operator_corrections([{"request_id": "req_live"}], path) == []


def test_qc082_reports_an_unreadable_file_rather_than_passing(tmp_path):
    """The applier's own failure mode is to swallow an unreadable corrections
    file with a WARN and apply nothing — which is exactly the silent total loss
    this check exists to catch."""
    stale = qc.qc082_stale_operator_corrections([], tmp_path / "does-not-exist.json")
    assert stale and stale[0][0] == "<unreadable>"


def test_qc082_would_have_caught_a_locode_rekey(tmp_path):
    """End to end, on the arithmetic that motivates the whole change: a
    correction keyed to the pre-merge spelling stops matching the row the fire
    rebuilds after the merge — and QC-082 turns that into an ERROR instead of a
    line in a runner log."""
    old_rid = core.request_id(IMID, TS, "Jpyok")
    path = _corrections_file(tmp_path, [{"request_id": old_rid,
                                         "set": {"status": "LOSS"},
                                         "note": "Linda audit"}])
    rebuilt = [_row("Yokohama")]                     # what the next fire produces
    assert rebuilt[0]["request_id"] != old_rid
    assert [rid for rid, _ in qc.qc082_stale_operator_corrections(rebuilt, path)] == [old_rid]
    # ...and the migration is what clears it.
    doc = {"corrections": json.loads(path.read_text())["corrections"]}
    mig.apply_rekey(doc, mig.plan([_row("Jpyok")], doc["corrections"])["affected"])
    path.write_text(json.dumps(doc), encoding="utf-8")
    assert qc.qc082_stale_operator_corrections(rebuilt, path) == []


# ── the transition fire: the day the prior file and the new build disagree ──

import ingest as ingest  # noqa: E402


def test_carry_forward_matches_across_the_rename():
    """THE TRANSITION-DAY RISK, pinned.

    On the first fire after the merge the prior tracking file says "Jpyok" and
    the fresh build says "Yokohama". `_prior_win_captured` falls back to a
    lane+date match for any prior WIN with NO mdolx_ref, and both sides used a
    bare `.lower()` — so the prior WIN would look uncaptured and be APPENDED as
    a duplicate carrying the pre-merge spelling AND the pre-merge request_id:
    the defect re-imported one day after it was fixed. The reconcile-by-id
    branch cannot save it either, because the id moved with the destination.
    Both sides now go through core.canonical_port_key."""
    key = core.canonical_port_key
    new_lane_dates = {(key("Yokohama"), "2026-08-20")}
    assert ingest._prior_win_captured(
        None, [], set(), key("Jpyok"), "2026-08-20", new_lane_dates) is True


def test_carry_forward_still_refuses_a_different_port():
    key = core.canonical_port_key
    assert ingest._prior_win_captured(
        None, [], set(), key("Tokyo"), "2026-08-20",
        {(key("Yokohama"), "2026-08-20")}) is False


def test_mdolx_still_outranks_the_lane_fallback():
    """The lane match is a FALLBACK. A prior WIN carrying an MDOLX ref is
    decided by that ref alone — the alias-aware key must not have changed the
    precedence."""
    assert ingest._prior_win_captured(
        "260500", [], {"260500"}, core.canonical_port_key("Jpyok"),
        "2026-08-20", set()) is True

# ── WHERE THE COMPANION GUARD WENT ─────────────────────────────────
#
# test_two_lane_less_rows_are_not_evidence_of_each_other lived here, was held
# back from the UN/LOCODE branch on purpose, and now lives in
# tests/test_send_signal_thread_anchor.py.
#
# It guards ingest._prior_win_captured against canonical_port_key's "unknown"
# sentinel: route BOTH sides of that comparison through the alias-aware key
# and two rows that each failed to resolve a lane start matching each other
# as ("unknown", date), which DROPS a prior WIN — the opposite of what the
# function is for. The bare .lower() it replaces got this right by accident,
# because an empty string is falsy.
#
# The routing change belongs to the DUPLICATE-ROW work (one shipment stored as
# two), not to the LOCODE merge: this file's branch normalises at parse time,
# so a JPYOK row is stored as "Yokohama" and both sides already agree without
# it. Pinning it here would have pinned behaviour whose cause was not in that
# diff. It shipped with the routing change, which needs it.
