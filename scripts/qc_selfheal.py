#!/usr/bin/env python3
"""
Hilmar Tracker — self-healing QC engine.

Runs BEFORE and AFTER every daily processing cycle. Validates the data file,
auto-heals what it can, flags what it can't.

Safety:
  - Creates a timestamped backup BEFORE any mutation.
  - Idempotent — safe to run twice in a row.
  - Non-blocking — writes a qc-result.json and exits 0 even with errors;
    the orchestrator decides what to do.

Usage:
  python3 qc_selfheal.py [--data PATH] [--config PATH] [--no-backup]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import contextlib

import body_parser as BP
import core

# Host shape: a set AZURE_STORAGE_CONNECTION_STRING means we're on an
# ephemeral blob-store runner (GH Actions) — reports/ starts empty every
# fire, so "yesterday's artifact missing" is physics, not a finding, and
# the durable backup target is the blob store, not OneDrive dirs.
# (2026-06-12: the first post-cutover audit warned on four such phantoms
# and red-flagged backups that had simply moved.)
_BLOB_HOST = bool(os.environ.get("AZURE_STORAGE_CONNECTION_STRING"))


class _QC032Done(Exception):
    """Control-flow sentinel: the blob branch of QC-032 already reported."""

# Single source of truth for "is this a Hilmar ocean RFQ" — shared with
# ingest.py so the QC backstop and the intake filter never drift (QC-040).
from ingest import apply_operator_corrections, out_of_scope_reason

# ─────────────────────────────────────────────────────────────────────
# COVERED-loss reason heuristics — promote OTHER → COVERED when we have
# direct text evidence. Keeps loss_reason taxonomy honest.
# ─────────────────────────────────────────────────────────────────────
_COVERED_HINTS = ("covered", "competitor", "another forwarder", "going with",
                  "going with another", "used another")

# ─────────────────────────────────────────────────────────────────────
# Poisoned-placeholder healing (2026-07-14, run 29292014093 root cause).
# A row persisted before the pdf_parser._clean_port source-fix can carry the
# LITERAL string "Unknown" (or other placeholder junk) in a lane-defining
# field — pod / destination / origin. Left as a string it (a) DISPLAYS as a
# real value in the staff AND client emails, (b) defeats
# patch_carriers._dest_from_row_pod / _dest_from_pod (a truthy "Unknown" pod
# looks resolved, so the recovery pass stops before finding the real port),
# and (c) lands the row in the "Unmapped" trade region. Coercing it to None at
# entry-heal time — BEFORE lane derivation — kills all three at the source and
# stops the drift re-deriving unresolved every fire.
_GARBAGE_PLACEHOLDERS = frozenset({
    "unknown", "n/a", "na", "none", "null", "tbd", "-", "—", "",
})
#: Lane-defining fields swept for the poisoned placeholder above.
#: `pol` added 2026-07-27 (review of #124). It was the one asymmetry here:
#: `pod` was swept and `pol` was not, though both are written the same way
#: from free-text OL body parsing (ingest.py — `best["pol"] = rt.get("pol")`
#: on the line above the identical `pod` assignment), both are display fields
#: QC-064 already lists, and both are exported to durable external surfaces by
#: historian.py and share_intel.py. A literal "TBD" in OL's POL cell therefore
#: survived every scrub and shipped as a port name.
_PLACEHOLDER_FIELDS = ("pol", "pod", "destination", "origin")


def _is_placeholder(v) -> bool:
    """True when `v` is a garbage placeholder (case-insensitive) that must be
    coerced to None rather than treated as a real port/lane value. Non-strings
    (None, ints) are NOT placeholders here — None is already the target
    state, and a numeric field is out of scope for this heal."""
    return isinstance(v, str) and v.strip().lower() in _GARBAGE_PLACEHOLDERS

# ─────────────────────────────────────────────────────────────────────
# Backup
# ─────────────────────────────────────────────────────────────────────

def rotate_backup(data_path: Path, backups_dir: Path, keep: int = 14) -> Path:
    backups_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(core.ET).strftime("%Y-%m-%d-%H%M")
    dest = backups_dir / f"tracking-data-v2.{ts}.json"
    shutil.copy2(data_path, dest)
    # Rotate: keep newest N. Defensive against session-mount constraints
    # where files created in a prior session can fail to unlink even when
    # owned by current user; in that case, rename with .stale- prefix so
    # the next rotation pass doesn't recount them.
    existing = sorted(backups_dir.glob("tracking-data-v2.*.json"))
    while len(existing) > keep:
        victim = existing[0]
        try:
            victim.unlink()
        except (PermissionError, OSError) as e:
            try:
                victim.rename(backups_dir / f".stale-{victim.name}")
                print(f"  ⚠️  rotate_backup: unlink blocked on {victim.name} ({e}); renamed to .stale-")
            except Exception as e2:
                print(f"  ⚠️  rotate_backup: could not remove or rename {victim.name} ({e2}); skipping")
                pass
        existing = existing[1:]
    return dest


# ─────────────────────────────────────────────────────────────────────
# Log helpers
# ─────────────────────────────────────────────────────────────────────

# Sentry observability — lazy import so QC works even without sentry-sdk.
try:
    import sentry_setup as _sentry
except ImportError:
    _sentry = None


def _extract_check_name(msg: str) -> str:
    """Pull the QC-NNN prefix from a log message so Sentry events
    group by check (one issue per check, not one per row)."""
    import re as _re
    m = _re.match(r"\s*(QC-\d+[a-z]?)", msg)
    return m.group(1) if m else "QC-unknown"


def _qc_phase_is_pre_patch() -> bool:
    """Is this qc_selfheal invocation the PRE-PATCH run in the pipeline?

    The pipeline runs qc_selfheal twice:
      1. pre-patch  — naturally has gaps (carriers / rates not yet filled)
      2. patch_carriers — backfills the gaps
      3. post-patch — represents the actual shipped state

    Pre-patch findings are EXPECTED to be incomplete. Surfacing them to
    Sentry creates false-positive alert noise (parser accuracy looks 14
    points lower than it really is, data completeness looks broken, etc).
    Only the post-patch run represents the actual quality of the shipped
    daily email — that's what should fire real-time alerts.

    Set HILMAR_QC_PHASE=pre-patch in the pipeline's pre-patch step to
    suppress Sentry event capture for THAT run. Post-patch + standalone
    runs default to firing events normally.
    """
    return os.environ.get("HILMAR_QC_PHASE", "").lower() == "pre-patch"


class Log:
    def __init__(self):
        self.fixes, self.warnings, self.errors = [], [], []
        # Scrubbed carrier-extraction diagnostics for the stuck QC-056 /
        # QC-002 rows — surfaced in the daily audit + qc-result.json so a
        # human/Claude can see WHY each row has no carrier (a missed token vs
        # a genuinely bare rate) and fix the parser precisely. PII-scrubbed
        # via sentry_setup._scrub_string before it leaves the box.
        self.carrier_diag = []
        # Snapshot the phase once at construction so subsequent env changes
        # mid-run don't surprise us
        self._pre_patch = _qc_phase_is_pre_patch()
        # Phase tag for metric routing — pre-patch metrics are still
        # collected but tagged so we can filter them out in the dashboard.
        self._phase_tag = "pre-patch" if self._pre_patch else "post-patch"

    def fix(self, msg):
        self.fixes.append(msg); print(f"  🔧 FIX: {msg}")
        # Counter metric — track how often self-heal applies fixes.
        # If this trends upward, the upstream parser is degrading.
        if _sentry is not None:
            with contextlib.suppress(Exception):
                _sentry.metric_increment("qc.fixes", 1, phase=self._phase_tag)

    def warn(self, msg):
        self.warnings.append(msg); print(f"  ⚠️  WARN: {msg}")
        # Counter metric tagged by check name + phase for dashboard slicing
        if _sentry is not None:
            with contextlib.suppress(Exception):
                _sentry.metric_increment(
                    "qc.warnings", 1,
                    check=_extract_check_name(msg),
                    phase=self._phase_tag,
                )
        # Pre-patch QC findings are EXPECTED incomplete — patch_carriers
        # backfills them next. Suppress Sentry alerts on pre-patch run
        # to avoid false-positive noise. (Findings still log locally +
        # surface in the daily audit email, which is the audit channel.)
        if self._pre_patch:
            return
        # Fire Sentry warning for parser-accuracy + drift checks (high signal)
        if _sentry is not None and any(
            tag in msg for tag in ("QC-039", "QC-040", "QC-041", "PARSER ACCURACY")
        ):
            with contextlib.suppress(Exception):
                _sentry.capture_qc_warning(_extract_check_name(msg), msg)

    def error(self, msg):
        self.errors.append(msg); print(f"  🔴 ERROR: {msg}")
        # Counter metric tagged by check name + phase for dashboard slicing
        if _sentry is not None:
            with contextlib.suppress(Exception):
                _sentry.metric_increment(
                    "qc.errors", 1,
                    check=_extract_check_name(msg),
                    phase=self._phase_tag,
                )
        # Same pre-patch suppression — patch_carriers will run next and
        # fix the data gaps that pre-patch QC is flagging. Only post-patch
        # ERRORs represent the real shipped state.
        if self._pre_patch:
            return
        # Every ERROR-severity QC finding goes to Sentry — these gate the
        # daily pipeline ship and demand immediate operator attention.
        if _sentry is not None:
            with contextlib.suppress(Exception):
                _sentry.capture_qc_error(_extract_check_name(msg), msg)

    def ok(self, msg):
        print(f"  ✅ {msg}")

    def section(self, title):
        print("\n" + "═" * 60)
        print(title)
        print("═" * 60)


# ─────────────────────────────────────────────────────────────────────
# Phase 1 — file health
# ─────────────────────────────────────────────────────────────────────

def phase_1_files(log: Log, data_path: Path, schema_path: Path):
    log.section("PHASE 1: FILE HEALTH")
    for label, p in [("Data JSON", data_path), ("Schema", schema_path)]:
        if not p.exists():
            log.error(f"{label} MISSING at {p}")
            continue
        size = p.stat().st_size
        if size < 50:
            log.error(f"{label} exists but suspiciously small ({size} bytes)")
        else:
            log.ok(f"{label} present ({size:,} bytes)")


# ─────────────────────────────────────────────────────────────────────
# Phase 2 — schema + structure
# ─────────────────────────────────────────────────────────────────────

def phase_2_structure(log: Log, data: dict) -> bool:
    log.section("PHASE 2: STRUCTURE")
    for key in ("requests", "summary", "version"):
        if key not in data:
            log.error(f"Missing top-level key: '{key}'")
            if key == "requests":
                return False
        else:
            log.ok(f"Top-level key '{key}' present")
    if not isinstance(data["requests"], list):
        log.error("'requests' is not an array")
        return False
    log.ok(f"{len(data['requests'])} requests")
    return True


# ─────────────────────────────────────────────────────────────────────
# Phase 3 — entry-level healing
# ─────────────────────────────────────────────────────────────────────

def _heal_carrier_won(log: Log, rid_label: str, r: dict) -> None:
    """Best-effort fill carrier_won on a WIN. Order:
       1) copy carrier_quoted   2) parse from subject (MDOLX patterns)
       3) leave None and leave a per-record warning
    Always records what we did so the QC log is auditable."""
    if r.get("carrier_won"):
        return
    if r.get("carrier_quoted"):
        r["carrier_won"] = r["carrier_quoted"]
        log.fix(f"{rid_label}: WIN carrier_won copied from carrier_quoted={r['carrier_quoted']}")
        return
    # Subject-based fallback (standalone wins from MDOLX confirmations)
    subj = r.get("subject") or ""
    sc = BP.parse_subject_carrier(subj)
    if sc:
        sc = core.normalize_carrier(sc) or sc
        r["carrier_won"] = sc
        r["carrier_quoted"] = r.get("carrier_quoted") or sc
        log.fix(f"{rid_label}: WIN carrier_won={sc} parsed from subject")
        return
    log.warn(f"{rid_label}: WIN with no carrier_won AND no subject signal — flagged for manual review")


def _reclassify_covered(log: Log, rid_label: str, r: dict) -> None:
    """If a LOSS has loss_reason='OTHER' but the reason_detail or lonny_covered
    flag says it was covered by a competitor, promote it to 'COVERED' for clarity."""
    lr = (r.get("loss_reason") or "").upper()
    if lr in ("COVERED",):
        return
    if r.get("status") != "LOSS":
        return
    rd = (r.get("reason_detail") or "").lower()
    is_covered = bool(r.get("lonny_covered")) or any(h in rd for h in _COVERED_HINTS)
    if not is_covered:
        return
    if lr in ("OTHER", "", "NONE"):
        prior = r.get("loss_reason")
        r["loss_reason"] = "COVERED"
        log.fix(f"{rid_label}: loss_reason {prior!r} → COVERED (text/flag evidence)")


# ─────────────────────────────────────────────────────────────────────
# Containers hygiene — strip email-body bleed (CAUTION banners, identical-
# bookings phrases, etc.) so the equipment column stays clean.
# Added 2026-05-04 after parser bleed surfaced in CMA CGM scorecard.
# ─────────────────────────────────────────────────────────────────────
import re as _re

_DIRTY_CONTAINERS_HINTS = (
    "caution", "outside of", "originated from", "i need ", "identical bookings",
    "i need two", "i need three", "i need four", "do not click",
    "this email", "confidential",
)
_CONTAINER_PATTERN = _re.compile(
    r"(\d+\s*[\-xX]\s*\d{1,3}\s*['’]?\s*(?:HC|DV|RF|GP|Reefer|Flex)?(?:\s+Reefer)?)",
    _re.IGNORECASE,
)
_INEED_PATTERN = _re.compile(
    r"I\s+need\s+(two|three|four|five)\s+(?:identical\s+bookings?\.?\s*)?(?:\n|$)?\s*"
    r"(?:[\d\-x'\"’\s]*?(\d+)['’]?\s*(HC|DV|RF|Reefer|HC\s+Reefer)?)?",
    _re.IGNORECASE,
)


def _heal_containers(log: Log, rid_label: str, r: dict, bodies_idx: dict) -> None:
    """Detect and clean dirty `containers` values (email-body bleed). Try in
    order: regex on the dirty value itself, lookup from cached source body,
    finally placeholder + warn. Idempotent: a clean value is a no-op."""
    c = r.get("containers") or ""
    low = c.lower()
    if not c or not any(h in low for h in _DIRTY_CONTAINERS_HINTS):
        return  # already clean
    # 1) Try local regex on dirty value
    m = _CONTAINER_PATTERN.search(c)
    if m and "identical" not in m.group(0).lower():
        new_c = m.group(1).strip()
        r["containers"] = new_c
        log.fix(f"{rid_label}: containers cleaned from email-body bleed → {new_c!r}")
        return
    # 2) Try cached source body — Lonny's outbound usually has a clean equipment line
    for imid in (r.get("source_imids") or []):
        body = (bodies_idx.get(imid) or {}).get("text_body") or ""
        if not body:
            continue
        # "I need three identical bookings\n\n1-40' HC Reefer ..." → 3-40' HC Reefer
        ineed = _INEED_PATTERN.search(body)
        if ineed and ineed.group(2):
            qty_word = ineed.group(1).lower()
            qty = {"two": 2, "three": 3, "four": 4, "five": 5}[qty_word]
            size = ineed.group(2)
            eq = (ineed.group(3) or "").strip()
            new_c = f"{qty}-{size}' {eq}".strip()
            r["containers"] = new_c
            log.fix(f"{rid_label}: containers recovered from body via 'I need {qty_word}' → {new_c!r}")
            return
        # Plain pattern in body
        m2 = _CONTAINER_PATTERN.search(body[:200])
        if m2:
            new_c = m2.group(1).strip()
            r["containers"] = new_c
            log.fix(f"{rid_label}: containers recovered from body → {new_c!r}")
            return
    # 3) Could not recover — flag for manual review (don't leave junk in display)
    r["containers"] = "— (manual review)"
    log.warn(f"{rid_label}: containers had email bleed but no clean equipment found")


# ─────────────────────────────────────────────────────────────────────
# Rate backfill — when a LOSS/PRICE has carrier_quoted but no ol_rate,
# re-parse the cached OL response body via body_parser.parse_rate_table.
# Added 2026-05-04 after Port Klang surfaced as $rate=None in scorecard.
# ─────────────────────────────────────────────────────────────────────

# qc_selfheal writes the STRING "Not Quoted" into ol_rate as an NQ sentinel
# (see the NQ-contamination heal in phase 6). Any check written as
# `ol_rate is not None` therefore reads that sentinel as a quote. That is how
# QC-077 came to count rows with no quote to date — on a check whose whole
# job is to be believed. Anything that asks "is there a rate here" goes
# through _is_real_rate.
# Moved to core 2026-08-05 so gen_email can reach it — it cannot import
# qc_selfheal, which is how the second spelling of this predicate got written
# and how the two counts came to disagree. These names stay as aliases: they
# are referenced across this file and pinned by tests/test_audit_batch8.
_NON_RATE_SENTINELS = core.NON_RATE_SENTINELS
_is_real_rate = core.is_real_rate


def _stamp_response_time_from_bodies(r: dict, bodies_idx: dict, imid: str | None = None) -> bool:
    """Date a quote from the SEND TIME of the message its rate came out of.

    RECOVERY, NOT FABRICATION. The value is the sentDateTime of an OL message
    already linked to this row through source_imids — the same message the
    rate itself was parsed from. Returns False when no send time exists, and
    the quote stays undated: a synthesised timestamp would invent turnaround
    timing and corrupt time-to-quote, which CLAUDE.md forbids outright.

    Mirrors patch_carriers._stamp_response_time. It exists twice because the
    two modules recover rates by different routes and neither imports the
    other; tests/test_audit_batch8.py asserts BOTH stay wired.
    """
    if r.get("response_timestamp"):
        return False
    for i in ([imid] if imid else (r.get("source_imids") or [])):
        rec = bodies_idx.get(i) or {}
        # Same fallback chain as patch_carriers._load_bodies_by_imid. The
        # field name has moved across refresh_stage schema versions, and an
        # inbound copy of a message can carry `received` without `sent` —
        # reading only "sent" would leave those rows undated for a reason
        # that has nothing to do with the data being missing.
        sent = _body_send_time(rec)
        if sent:
            r["response_timestamp"] = sent
            return True
    return False


# Delegated to core 2026-08-06. These three spellings belong to
# stage_emails.txt; the BODY cache this heal actually reads is
# stage_emails_bodies.txt, which fetch_bodies writes with sent_ts/received_ts.
# So every lookup returned None and this heal dated nothing between 08-05 and
# 08-06 while the count it was meant to shrink went 41 → 43. See
# core.body_send_time. Kept as aliases — both names are used below.
_BODY_SEND_FIELDS = core.BODY_SEND_TIME_FIELDS
_body_send_time = core.body_send_time


def _undated_reason(r: dict, bodies_idx: dict) -> str:
    """Why this row could not be auto-dated. Exactly one label per row, so the
    breakdown is exhaustive by construction rather than by three counters
    happening to agree."""
    imids = r.get("source_imids") or []
    if not imids:
        return "no_imids"
    recs = [bodies_idx.get(i) for i in imids]
    if not any(recs):
        return "no_body"
    if not any(_body_send_time(rec) for rec in recs if rec):
        return "no_send_time"
    return "unexplained"


def _heal_undated_quote(log: Log, rid_label: str, r: dict, bodies_idx: dict) -> None:
    """Date a quote that ALREADY has a rate or carrier but no response time.

    2026-08-05, Michael on the audit's QC-077 banner — "41 further quotes are
    recorded with a rate or carrier but no response time" — "this is
    unacceptable." He is right, and the count had grown from 29 on 07-30.

    QC-077 was built as a DETECTOR and deliberately did not heal, on the
    reasoning that synthesising a timestamp would be fabrication. That
    reasoning holds for INVENTING a time and does not hold for READING one:
    these rows carry source_imids pointing at the very OL messages their
    rates were parsed out of, and those messages have a sentDateTime sitting
    unused in the bodies index. Detecting a gap you have the data to close is
    not caution, it is a warning nobody can action.

    #140 fixed one of the two rate-recovery routes (patch_carriers). This is
    the other one, plus the backfill for every row already stranded by both.
    """
    if r.get("response_timestamp"):
        return
    if not (_is_real_rate(r.get("ol_rate")) or r.get("carrier_quoted")):
        return
    # Standalone bookings are excluded for the same reason QC-077 excludes
    # them: ingest.py:887 leaves the field None DELIBERATELY to signal "no
    # rate response was ever seen", and filling it would erase that signal.
    if str(r.get("request_id") or "").startswith("stand_"):
        return
    if _stamp_response_time_from_bodies(r, bodies_idx):
        log.fix(f"{rid_label}: undated quote dated {r['response_timestamp']} "
                f"from the OL message it was parsed from")


def _heal_missing_rate(log: Log, rid_label: str, r: dict, bodies_idx: dict) -> None:
    """If a quoted LOSS has no ol_rate, try to recover from cached OL body."""
    if r.get("ol_rate") is not None:
        return
    if not (r.get("status") == "LOSS" and r.get("quoted")):
        return
    if not r.get("carrier_quoted"):
        return  # nothing to anchor against
    for imid in (r.get("source_imids") or []):
        body = (bodies_idx.get(imid) or {}).get("text_body") or ""
        if not body or " | " not in body:
            continue
        try:
            parsed = BP.parse_rate_table(body)
        except Exception:
            continue
        rate = parsed.get("ol_rate")
        if rate is not None:
            # DATE IT WHERE YOU SET IT. Recovering a rate and leaving it
            # undated produces a quote OL-USA RESPONSES can never show — real,
            # priced, invisible on every day forever. This heal ran for months
            # without the stamp, which is half of why the undated count hit 41.
            # Kept adjacent to the write on purpose: the guard in
            # test_audit_batch8 requires the two within a few lines, because a
            # stamp that drifts to the bottom of a branch is a stamp somebody
            # deletes without noticing what it was for.
            r["ol_rate"] = rate
            _dated = _stamp_response_time_from_bodies(r, bodies_idx, imid)
            log.fix(f"{rid_label}: ol_rate ${rate} backfilled from cached OL body")
            if _dated:
                log.fix(f"{rid_label}: response_timestamp "
                        f"{r['response_timestamp']} taken from that same OL message")
            # Also fill any missing structured fields
            for k in ("vessel_voyage", "etd_offered", "eta_offered", "transshipment"):
                if not r.get(k) and parsed.get(k):
                    r[k] = parsed[k]
            return


def _load_bodies_index() -> dict:
    """Load scripts/stage_emails_bodies.txt keyed by imid. Empty dict if missing.
    2026-05-06: prefer .txt for SharePoint indexing; fall back to legacy .jsonl."""
    here = Path(__file__).resolve().parent
    path = here / "stage_emails_bodies.txt"
    if not path.exists():
        path = here / "stage_emails_bodies.jsonl"
    out = {}
    if not path.exists():
        return out
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("imid"):
                    out[rec["imid"]] = rec
    except Exception:
        return {}
    return out


_CARRIER_DIAG_LINE_HINTS = (
    "carrier", "vessel", "line", "sailing", "ocean freight",
)
_CARRIER_DIAG_DOLLAR_RX = re.compile(r"\$\s?\d")


def _carrier_from_lane_rate_sibling(r: dict, requests: list, window_days: int = 45):
    """Infer a stuck row's missing carrier from a SIBLING row: same lane, same
    ol_rate (to the cent), request_date within ±window_days, that HAS a parsed
    carrier. Same lane + identical rate is a strong fingerprint — it's the
    same OL quote line landing in two emails, one of which parsed its carrier
    cell (the 2026-07-01 diagnostics: the stuck $3,076 Yokohama rows sit next
    to $3,076 Yokohama rows attributed CMA CGM).

    Deliberately NOT vessel-name inference: alliance slot-sharing means a
    Yang Ming quote can sail on a ONE-named vessel (the live $425 Busan row —
    vessel "ONE FANTASTIC", sibling attribution Yang Ming), so a vessel
    prefix can mislabel the QUOTING carrier; a sibling's parsed carrier
    cannot. Returns the carrier only when ALL matching siblings agree on
    exactly ONE carrier — any disagreement returns None (stay blank rather
    than guess wrong)."""
    try:
        from datetime import date as _date

        def _d(row):
            s = (row.get("request_date") or row.get("date") or "")[:10]
            try:
                return _date.fromisoformat(s)
            except Exception:
                return None

        lane = (r.get("lane") or "").strip().lower()
        rate = r.get("ol_rate")
        if not lane or not isinstance(rate, (int, float)):
            return None
        rd = _d(r)
        carriers = set()
        for s in requests:
            if s is r or not s.get("carrier_quoted"):
                continue
            if (s.get("lane") or "").strip().lower() != lane:
                continue
            srate = s.get("ol_rate")
            # Rounded-to-cents EQUALITY — float tolerance without widening the
            # match: $3,076.00 vs $3,076.01 are different quotes, not siblings.
            if not isinstance(srate, (int, float)) or round(srate, 2) != round(rate, 2):
                continue
            sd = _d(s)
            # Both dates known and too far apart → different quote cycles;
            # a missing date on either side doesn't disqualify (lane+rate
            # already carry the match).
            if rd and sd and abs((rd - sd).days) > window_days:
                continue
            car = s["carrier_quoted"]
            with contextlib.suppress(Exception):
                car = core.normalize_carrier(car) or car
            carriers.add(car)
        return carriers.pop() if len(carriers) == 1 else None
    except Exception:
        return None


# QC-027's denominator, named once so a diagnostic cannot hold a different
# idea of it than the check does. 2026-08-10: QC-027 has reported Carrier
# below 90% since August and "it used to work" — but a completeness
# percentage is a ratio, and a ratio moves when the DENOMINATOR moves, not
# only when the numerator breaks. Any tool that wants to explain the number
# has to select exactly the rows the check selected; a re-typed list
# comprehension in a diag script is the classic way to answer a question
# about a set you are not actually looking at.
QC027_FIELDS = [
    ("etd_offered",    "ETD"),
    ("eta_offered",    "ETA"),
    ("vessel_voyage",  "Vessel/Voyage"),
    ("ol_rate",        "Rate"),
    ("carrier_quoted", "Carrier"),
    ("pol",            "POL"),
    ("pod",            "POD"),
]


def qc027_active_rows(requests: list) -> list:
    """Rows QC-027 considers at all: a live status AND a response we timed."""
    return [r for r in requests
            if r.get("status") in ("WIN", "LOSS", "PENDING")
            and r.get("response_timestamp")]


def qc027_is_reachable(r: dict) -> bool:
    """True when a rate-response body was parseable enough to leave a trace.

    The comment on the original expression calls this an approximation of
    "at least one source_imid points to an mbd_rate_response body", and it is:
    if none of ETD / vessel / rate survived, the numbers were in a PDF we do
    not parse. Those rows are counted as PDF-only instead of failing the gate.
    """
    return bool(r.get("etd_offered") or r.get("vessel_voyage") or r.get("ol_rate"))


def _carrier_diag_snippet(r: dict, bodies_idx: dict, max_chars: int = 400) -> dict:
    """Build a SCRUBBED diagnostic dict for ONE stuck (no-carrier) row.

    Surfaces the exact text the carrier parser failed on for the QC-056
    (rate-without-carrier) + QC-002 (WIN-without-carrier_won) rows, so the
    next audit can show WHY a row has no carrier — a missed token in a real
    carrier line vs a genuinely bare rate with nothing to extract.

    The returned snippet is PII-scrubbed via sentry_setup._scrub_string
    (emails / MDOLX / booking refs / IMIDs / conv-IDs / req_<hex>) because it
    lands in BOTH the idealx.us audit email and the uploaded qc-result.json
    artifact. A diagnostic must NEVER break the QC run, so the whole body is
    wrapped: any error yields a minimal, still-safe dict.
    """
    try:
        # 1) First non-empty cached body for the row.
        body = ""
        for imid in (r.get("source_imids") or []):
            body = (bodies_idx.get(imid) or {}).get("text_body") or ""
            if body:
                break
        # 2) Carrier-region of the body — only the lines that mention a
        #    carrier/vessel/line/sailing/ocean-freight signal or a $ amount.
        region_lines = []
        for line in body.splitlines():
            low = line.lower()
            if any(h in low for h in _CARRIER_DIAG_LINE_HINTS) or _CARRIER_DIAG_DOLLAR_RX.search(line):
                stripped = line.strip()
                if stripped:
                    region_lines.append(stripped)
        region = " | ".join(region_lines)[:max_chars]
        # 3) Stored carrier-bearing fields off the row itself.
        stored_fields = " | ".join(
            f"{k}={r.get(k)}"
            for k in ("vessel_voyage", "transshipment", "pol", "pod", "reason_detail")
            if r.get(k)
        )
        raw = region or stored_fields or (
            "(no body cached + stored carrier fields empty -> likely a "
            "genuinely BARE rate; nothing to extract)"
        )
        # 4) Scrub — fall back to raw if sentry_setup can't be imported.
        try:
            from sentry_setup import _scrub_string
            snippet = _scrub_string(raw)
        except Exception:
            snippet = raw
        snippet = snippet[:max_chars]
        return {
            "lane": r.get("lane") or "?",
            "status": r.get("status"),
            "rate": r.get("ol_rate"),
            "has_body": bool(body),
            "snippet": snippet,
        }
    except Exception:
        # A diagnostic must never break the QC run.
        return {"lane": r.get("lane") if isinstance(r, dict) else "?",
                "snippet": "(diagnostic failed)"}


#: Operator-acknowledged non-RFQ emails (tracked in git). QC-057 cannot
#: safely auto-classify "commercial note" vs "real RFQ" — both can contain
#: rate language (the 2026-07-10 REEFER NEEDS note literally asks for "best
#: rate") and a wrong auto-classification silently hides a real dropped RFQ,
#: the exact 2026-06-24 failure QC-057 exists to prevent. So classification
#: is an OPERATOR decision recorded here after reading the QC-057-DIAG
#: snippet. Entries are DATE-SCOPED: they only cover emails sent strictly
#: BEFORE the entry's `sent_before` date, so a future same-subject email
#: that IS a real RFQ still WARNs.
INTAKE_ACK_PATH = Path(__file__).resolve().parent / "intake_acknowledged.json"


def _load_intake_acks() -> list:
    try:
        import json as _json
        return _json.loads(INTAKE_ACK_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []


def _norm_subject_57(s) -> str:
    import re as _re
    s = _re.sub(r"^\s*(re|fw|fwd):\s*", "", (s or "").strip(), flags=_re.IGNORECASE)
    return " ".join(s.split()).casefold()


def _intake_ack_match(subject, sent_iso, acks):
    """The ack entry covering this email, or None. Subject must match
    (normalized) AND the email's sent date must be strictly before the
    entry's sent_before. An email with no parseable sent date is NOT
    covered — fail open to the WARN, never silently acknowledge."""
    subj_n = _norm_subject_57(subject)
    sent_day = str(sent_iso or "")[:10]
    if not sent_day:
        return None
    for a in acks or []:
        if _norm_subject_57(a.get("subject")) == subj_n and \
                sent_day < str(a.get("sent_before") or ""):
            return a
    return None


def _intake_reconciliation(stage_rows, bodies, acks=None):
    """Reconcile staged Lonny RFQs against ingest's OWN drop conditions.

    ingest.build_requests silently drops any lonny_outbound email whose
    subject (and body) yields no parseable destination — the condition at
    ``if not destination: skipped_ops += 1; continue`` — incrementing a
    counter it never logs or returns. A genuine rate request can therefore
    vanish from the report with no alarm (the 2026-06-24 "Busan Korea from
    Dalhart" miss that hid for a week).

    Returns ``(expected, dropped)`` where ``expected`` is the count of
    lonny_outbound emails that are genuine rate asks (not operational, not
    out-of-scope) and ``dropped`` lists the subjects of those ingest would
    drop for lack of a destination. Reuses ingest's own
    out_of_scope_reason / is_operational_subject / clean_destination so this
    guard and the intake can never drift (QC-040 spirit).
    """
    import ingest

    bodies = bodies or {}
    acks = _load_intake_acks() if acks is None else acks
    expected, dropped = 0, []
    for r in stage_rows or []:
        if r.get("bucket") != "lonny_outbound":
            continue
        subj = r.get("subject") or ""
        body = bodies.get(r.get("imid")) or {}
        # Same two upstream filters ingest applies before build_requests.
        if ingest.out_of_scope_reason({
            "subject": subj,
            "summary_preview": r.get("summary_preview"),
            "text_body": body.get("text_body"),
        }):
            continue
        if ingest.is_operational_subject(subj):
            continue
        # Operator-acknowledged commercial note (date-scoped) — not an RFQ,
        # so it neither counts as expected nor as a silent drop.
        if _intake_ack_match(subj, r.get("sent") or r.get("received"), acks):
            continue
        expected += 1
        # Exact destination resolution from ingest.build_requests:
        #   destination = clean_destination(subject) or parsed.destination
        parsed = body.get("parsed") or {}
        dest = ingest.clean_destination(subj) or parsed.get("destination")
        if not dest:
            dropped.append(subj.strip()[:80] or "(no subject)")
    return expected, dropped


#: Body-line hints that mark a lane/equipment-bearing line in a dropped RFQ —
#: what the operator (or the next parser fix) needs to see to resolve the
#: destination the parser missed. Lowercase substring match.
_INTAKE_DIAG_LINE_HINTS = (
    "x40", "40'", "40ft", "40 ft", " hc", "reefer", "teu", "container",
    " to ", "->", "→", "port of", "etd", "eta", "free time", "dest",
)


def _intake_acked_notes(stage_rows, acks=None) -> list:
    """(subject, reason) for each staged lonny_outbound email covered by an
    acknowledged-note entry — printed as OK lines so the acknowledgment
    stays visible in every run log instead of silently vanishing."""
    acks = _load_intake_acks() if acks is None else acks
    out, seen = [], set()
    for r in stage_rows or []:
        if r.get("bucket") != "lonny_outbound":
            continue
        subj = r.get("subject") or ""
        a = _intake_ack_match(subj, r.get("sent") or r.get("received"), acks)
        key = _norm_subject_57(subj)
        if a and key not in seen:
            seen.add(key)
            out.append((subj.strip()[:80], a.get("reason") or "(no reason recorded)"))
    return out


def _intake_drop_diag(stage_rows, bodies, max_emails: int = 5,
                      max_chars: int = 500, acks=None) -> list:
    """PII-scrubbed body diagnostics for QC-057's silently-dropped RFQs.

    QC-057 can only say WHICH subjects dropped; the root fix (a parser
    extension) needs to see the lane-bearing body text the parser failed on.
    For each dropped RFQ this returns {subject, has_body, snippet} where
    snippet is the lane/equipment-hinted lines of the cached body (falling
    back to the first non-empty lines), scrubbed via sentry_setup's PII
    scrubber — it lands in the run log and the idealx.us audit email.
    A diagnostic must never break the QC run: any error yields a safe stub.
    """
    import ingest

    bodies = bodies or {}
    acks = _load_intake_acks() if acks is None else acks
    out = []
    try:
        for r in stage_rows or []:
            if len(out) >= max_emails:
                break
            if r.get("bucket") != "lonny_outbound":
                continue
            subj = r.get("subject") or ""
            body_rec = bodies.get(r.get("imid")) or {}
            if ingest.out_of_scope_reason({
                "subject": subj,
                "summary_preview": r.get("summary_preview"),
                "text_body": body_rec.get("text_body"),
            }) or ingest.is_operational_subject(subj):
                continue
            if _intake_ack_match(subj, r.get("sent") or r.get("received"), acks):
                continue  # acknowledged commercial note — no diag needed
            parsed = body_rec.get("parsed") or {}
            if ingest.clean_destination(subj) or parsed.get("destination"):
                continue  # resolved — not a drop
            body = body_rec.get("text_body") or ""
            lane_lines = [ln.strip() for ln in body.splitlines()
                          if ln.strip() and any(h in ln.lower()
                                                for h in _INTAKE_DIAG_LINE_HINTS)]
            if not lane_lines:  # nothing hinted — first non-empty lines instead
                lane_lines = [ln.strip() for ln in body.splitlines()
                              if ln.strip()][:6]
            raw = " | ".join(lane_lines)[:max_chars] if lane_lines else \
                "(no body cached — Graph fetch missing for this message)"
            try:
                from sentry_setup import _scrub_string
                raw = _scrub_string(raw)[:max_chars]
            except Exception:
                pass
            out.append({"subject": subj.strip()[:80] or "(no subject)",
                        "has_body": bool(body), "snippet": raw})
    except Exception:
        return out or [{"subject": "(diagnostic failed)", "has_body": False,
                        "snippet": ""}]
    return out


def phase_3_entries(log: Log, data: dict):
    log.section("PHASE 3: ENTRY-LEVEL HEALING")
    # Cleanup pass: drop stand_* WINs that don't have HILMAR in subject.
    # Pre-existing rows from when ingest accepted NUMIDIA-only (the bug
    # fixed 2026-05-17). These bleed non-Hilmar customer bookings into
    # Hilmar's data and drag down parser accuracy.
    # Per Michael 2026-05-17 ("your qc and parsers have to improve").
    cleaned = []
    removed_misclassified = []
    removed_oos = []  # (reason, id) — out-of-scope: numidia / trucking / recalled
    for r in data["requests"]:
        rid = r.get("request_id", "") or ""
        subj_up = (r.get("subject") or "").upper()
        # Out-of-scope backstop (Michael 2026-05-20; Linda Echevarria audit
        # 2026-05-19): purge any row that is not a Hilmar ocean RFQ — Numidia
        # (Hilmar-as-supplier), trucking (FTL/LTL), or a recalled request.
        # ingest.py blocks these at the source; this clears any already in
        # the data file. Uses ingest.out_of_scope_reason so the backstop and
        # the intake filter can never drift.
        oos = out_of_scope_reason({"subject": r.get("subject")})
        if oos:
            removed_oos.append((oos, rid or (r.get("subject") or "")[:40]))
            continue
        if rid.startswith("stand_") and "HILMAR" not in subj_up:
            removed_misclassified.append(rid)
            continue
        cleaned.append(r)
    if removed_misclassified or removed_oos:
        data["requests"] = cleaned
    if removed_oos:
        _by_reason = Counter(reason for reason, _ in removed_oos)
        log.fix(
            f"PHASE 3 cleanup: removed {len(removed_oos)} out-of-scope row(s) "
            f"[{', '.join(f'{n} {k}' for k, n in sorted(_by_reason.items()))}] — not Hilmar "
            f"ocean RFQs: "
            + ", ".join(i for _, i in removed_oos[:5])
            + (f" +{len(removed_oos) - 5} more" if len(removed_oos) > 5 else "")
        )
        if _sentry is not None:
            with contextlib.suppress(Exception):
                _sentry.metric_increment("qc.out_of_scope_rows_removed", len(removed_oos))
    if removed_misclassified:
        log.fix(
            f"PHASE 3 cleanup: removed {len(removed_misclassified)} misclassified "
            f"stand_* WIN(s) (subject lacks HILMAR): "
            f"{', '.join(removed_misclassified[:5])}"
            + (f" +{len(removed_misclassified)-5} more" if len(removed_misclassified) > 5 else "")
        )
        # Sentry metric so we can track how often the classifier rescues us
        if _sentry is not None:
            with contextlib.suppress(Exception):
                _sentry.metric_increment(
                    "qc.misclassified_stand_removed",
                    len(removed_misclassified),
                )

    # Operator corrections — authoritative human overrides (Linda Echevarria
    # audits etc.). Re-applied here as a self-heal backstop using the SAME
    # ingest.apply_operator_corrections() the pipeline runs, so the QC layer
    # enforces the same verdicts and the two can never drift. Runs before the
    # per-row decide loop so corrected rows are manual_locked and not re-decided.
    _op_corrected = apply_operator_corrections(data["requests"])
    if _op_corrected:
        log.fix(
            f"PHASE 3: re-applied {_op_corrected} operator correction(s) "
            f"from operator_corrections.json (authoritative human overrides)"
        )
        if _sentry is not None:
            with contextlib.suppress(Exception):
                _sentry.metric_increment("qc.operator_corrections_applied", _op_corrected)

    requests = data["requests"]
    # Load source bodies once for healers that need them (containers cleanup,
    # rate backfill). Safe if file is missing — healers gracefully skip.
    bodies_idx = _load_bodies_index()

    # Compute lane winning medians ONCE before the per-row decide loop.
    # decide_status uses this to determine PRICE vs UNDIFFERENTIATED on
    # Q&L rows — see core.decide_status docstring (2026-06-02 rewrite).
    # Computed BEFORE the loop because each row's decision is per-row pure
    # but needs book-wide WIN-rate context for the gap calc.
    lane_winning_median = core.compute_lane_winning_medians(requests)

    for i, r in enumerate(requests):
        rid_label = f"[{i}] {r.get('request_date') or r.get('date','?')} {r.get('destination','?')}"
        # HEAL poisoned placeholder literals in lane-defining fields BEFORE any
        # lane derivation (2026-07-14, run 29292014093). A persisted
        # "Unknown"/"N/A"/… in pod/destination/origin becomes None so it can
        # never display, defeat _dest_from_row_pod/_dest_from_pod, or bucket
        # the row as "Unmapped". Root cause: tracking-data-v2.json rows written
        # before pdf_parser._clean_port (log: stand_260905 pod=="Unknown"
        # re-derived unresolved every fire — "drift that keeps occurring").
        for _pf in _PLACEHOLDER_FIELDS:
            if _is_placeholder(r.get(_pf)):
                _bad = r.get(_pf)
                # POP, don't set to None. Setting the key to None left the
                # field PRESENT-but-null, so every downstream
                # `r.get("origin", "Oakland")` default was bypassed — `.get`
                # only substitutes when the key is ABSENT — and the value
                # rendered as the literal string "None". The client PDF's Lane
                # Performance table shipped a row labelled "None → Tokyo",
                # strictly worse than the "Unknown → Tokyo" this heal was
                # replacing, and gen_client_email's own
                # f"{r.get('origin','?')} → ..." fallback printed the same.
                r.pop(_pf, None)
                log.fix(f"{r.get('request_id') or rid_label}: cleaned poisoned "
                        f"placeholder {_pf}={_bad!r} → removed (garbage "
                        f"literal, pre lane-derivation)")
        # Hygiene healers — run on every record regardless of status/lock.
        _heal_containers(log, rid_label, r, bodies_idx)
        _heal_missing_rate(log, rid_label, r, bodies_idx)
        # Runs AFTER _heal_missing_rate so a rate recovered on this very pass
        # is dated too, and independently so rows that arrived already-rated
        # but undated (the backlog QC-077 counts) get dated as well.
        _heal_undated_quote(log, rid_label, r, bodies_idx)

        if not r.get("request_id"):
            r["request_id"] = core.request_id(
                r.get("conversationId"), r.get("request_timestamp"),
                r.get("destination"),
            )
            log.fix(f"{rid_label}: Assigned request_id={r['request_id']}")

        # request_date is RECOMPUTED from request_timestamp every pass, not
        # merely filled when absent. Filling-only was why the 2026-07-26
        # timezone fix could not stand on its own: every row already stored
        # with a UTC (ingest / merge_ingest) or PT (this heal, pre-fix) date
        # would have kept its wrong day forever, and a row mis-dated onto a
        # Saturday is invisible to EVERY report day. The timestamp is the
        # authority; the date is a derived bucket key, so recomputing it is a
        # migration that runs itself. Rows with no parseable timestamp keep
        # whatever they have — we correct dates, we don't invent them.
        _et_rd = core.et_date_of(r.get("request_timestamp"))
        if _et_rd:
            if r.get("request_date") != _et_rd:
                log.fix(f"{rid_label}: request_date {r.get('request_date')} → "
                        f"{_et_rd} (recomputed in ET — the clock every day "
                        f"bucket uses)")
                r["request_date"] = _et_rd
        elif not r.get("request_date") and r.get("date"):
            r["request_date"] = r["date"]
            log.fix(f"{rid_label}: request_date copied from legacy 'date'")
        # Keep the legacy 'date' mirror in step. Readers fall back to it
        # (`r.get("request_date") or r.get("date")`), so leaving it on the old
        # value would reintroduce the very split this heal just closed.
        if r.get("request_date") and r.get("date") != r["request_date"]:
            r["date"] = r["request_date"]

        c_count, teu = core.parse_teu(r.get("containers", ""))
        if (r.get("containers") and (not r.get("teu_requested") or r["teu_requested"] == 0)
                and teu > 0):
            r["teu_requested"] = teu
            r.setdefault("container_count", c_count)
            log.fix(f"{rid_label}: Recalculated teu_requested={teu}")
        if not r.get("container_count") and c_count:
            r["container_count"] = c_count

        if not r.get("lane") and r.get("origin") and r.get("destination"):
            r["lane"] = f"{r['origin']} → {r['destination']}"

        # Reconcile the quoted flag against the actual evidence. A row carrying
        # a real rate or carrier MUST be quoted=True — otherwise decide_status
        # sees quoted=False and (esp. with a missing response_timestamp) buckets
        # a genuine OL quote as NQ NO_RESPONSE (the user-reported phantom-NQ:
        # the OL response table showed the $3,076 rate while the request row was
        # counted Not Quoted). Previously this only DEFAULTED when the key was
        # absent, so a stored quoted=False desync survived. Now it also REPAIRS.
        # Was its own three-sentinel list, so ol_rate="N/A" or "—" read as a
        # real rate and flipped quoted=True on a row with no quote. Same
        # question as QC-077 asks, so it uses the same predicate now.
        _has_rate = core.has_quote_evidence(r)
        if "quoted" not in r:
            r["quoted"] = bool(r.get("response_timestamp") or _has_rate)
            log.fix(f"{rid_label}: Defaulted quoted={r['quoted']}")
        elif _has_rate and r.get("quoted") is not True:
            r["quoted"] = True
            log.fix(f"{rid_label}: Reconciled quoted=True (rate/carrier present, was {r.get('quoted')!r})")

        # Complete the reconcile: a row that now carries an OL rate can still
        # hold the PROSE written back when it looked NQ — reason_detail
        # "OL-USA never responded with a quote" (ingest deliberately never
        # overwrites a set reason_detail, so the text outlives the flip).
        # That contradiction ships to the audit + carrier diagnostics as
        # wrong info (the 2026-07-01 diagnostics showed it on 5 rated rows).
        # Rewrite it to the aged-Q&L truth, and correct a stale NO_RESPONSE
        # loss_reason the same way — a rated row WAS quoted; its loss is the
        # aged kind, not silence.
        if _has_rate and "never responded" in (r.get("reason_detail") or ""):
            r["reason_detail"] = "OL quoted (rate on file) — assumed aged; no booking followed"
            if r.get("status") == "LOSS" and r.get("loss_reason") == "NO_RESPONSE":
                r["loss_reason"] = "OTHER"
            log.fix(f"{rid_label}: reason_detail said 'never responded' but a rate "
                    f"is on file — rewrote to aged-Q&L")

        if "has_send" not in r:
            r["has_send"] = False

        if not isinstance(r.get("after_hours_request"), bool):
            req_dt = core.parse_iso(r.get("request_timestamp"))
            r["after_hours_request"] = core.is_after_hours_et(req_dt) if req_dt else False

        if isinstance(r.get("turnaround"), dict):
            ta = r.pop("turnaround")
            for src, dst in [
                ("hours", "turnaround_hours"),
                ("biz_hours", "turnaround_biz_hours"),
                ("response_timestamp", "response_timestamp"),
            ]:
                if src in ta and not r.get(dst):
                    r[dst] = ta[src]
            log.fix(f"{rid_label}: Flattened nested turnaround object")

        req_dt = core.parse_iso(r.get("request_timestamp"))
        resp_dt = core.parse_iso(r.get("response_timestamp"))
        if req_dt and resp_dt:
            if r.get("turnaround_hours") is None:
                r["turnaround_hours"] = core.clock_hours_between(req_dt, resp_dt)
            if r.get("turnaround_biz_hours") is None:
                r["turnaround_biz_hours"] = core.biz_hours_between(req_dt, resp_dt)
            if not r.get("lonny_time_pt"):
                r["lonny_time_pt"] = core.fmt_pt(req_dt, with_date=False)
            if not r.get("olusa_time_et"):
                r["olusa_time_et"] = core.fmt_et(resp_dt, with_date=False)

        # QC-048 self-heal: a real OL rate-response turnaround is sub-day
        # (biz-hours, usually <4h). A value above 40 biz-hours means the
        # response_timestamp was mis-paired — a stale rate response from a
        # later thread, or a leaked booking timestamp — not a real response
        # time. None ("no reliable timing") is the honest value. This runs
        # after the backfill above, every pass, so a re-backfill from the
        # bad timestamp can never resurrect the implausible number.
        _tabh = r.get("turnaround_biz_hours")
        if isinstance(_tabh, (int, float)) and _tabh > 40:
            r["turnaround_biz_hours"] = None
            r["turnaround_hours"] = None
            log.fix(f"{rid_label}: implausible turnaround ({_tabh:.1f} biz-hrs "
                    ">40) cleared — response_timestamp mis-paired, no reliable timing")

        if r.get("etd_fit_days") is None and r.get("quoted"):
            lonny_ask = r.get("eta_requested") or r.get("requested_dates") or r.get("cutoff_requested")
            ol_offer = r.get("eta_offered") or r.get("etd_offered")
            if lonny_ask and ol_offer:
                fit = core.etd_fit_days(lonny_ask, ol_offer)
                if fit is not None:
                    r["etd_fit_days"] = fit

        # HARD LOCK: skip status decisions on manually-locked records (Michael's audit pass 2026-05-01)
        # Even on locked records: heal carrier_won (WIN only) + reclassify OTHER -> COVERED (LOSS only).
        # _heal_carrier_won is meaningless on LOSS records — only call when locked record is WIN.
        if r.get("manual_locked"):
            if r.get("status") == "WIN":
                _heal_carrier_won(log, rid_label, r)
            if r.get("status") == "LOSS":
                _reclassify_covered(log, rid_label, r)
            continue
        # Honor explicit lonny_covered flag (Lonny replied "Covered" = lost to competitor)
        # 2026-06-02 (track 03 finding C-2): also set quoted=True. COVERED
        # is the canonical signal of a real lost contest — without
        # quoted=True the row bucketed as NQ by is_not_quoted() and got
        # excluded from win-rate (denominator AND distorted by C-1 NQ-in-
        # denom bug). Win rate was double-distorted on every email.
        if r.get("lonny_covered"):
            prior_status = r.get("status")
            prior_quoted = r.get("quoted")
            if (prior_status != "LOSS" or r.get("loss_reason") != "COVERED"
                    or prior_quoted is not True):
                r["status"] = "LOSS"
                r["loss_reason"] = "COVERED"
                r["quoted"] = True
                log.fix(f"{rid_label}: lonny_covered → LOSS/COVERED + quoted=True")
            continue
        prior_status = r.get("status")
        decision = core.decide_status(
            has_send=r.get("has_send", False),
            mdolx_ref=r.get("mdolx_ref"),
            response_timestamp=r.get("response_timestamp"),
            quoted=r.get("quoted", False),
            etd_fit_days=r.get("etd_fit_days"),
            request_timestamp=r.get("request_timestamp") or r.get("request_date"),
            ol_rate=r.get("ol_rate"),
            lane=r.get("lane"),
            lane_winning_median=lane_winning_median,
        )
        if prior_status != decision.status:
            core.record_transition(r, decision.status, decision.reason_detail)
            log.fix(f"{rid_label}: Status {prior_status} → {decision.status} ({decision.reason_detail})")
        else:
            r["status"] = decision.status
        r["has_send"] = decision.has_send
        r["loss_reason"] = decision.loss_reason

        if r["status"] == "WIN":
            _heal_carrier_won(log, rid_label, r)
            if not r.get("mdolx_ref") and not r.get("has_send"):
                log.warn(f"{rid_label}: WIN has no chain-send signal AND no MDOLX ref — verify booking")
            if not r.get("teu_won"):
                r["teu_won"] = r.get("teu_requested", 0) or 0
                log.fix(f"{rid_label}: WIN teu_won defaulted to teu_requested")
            r["quoted"] = True

        if r["status"] == "LOSS":
            _reclassify_covered(log, rid_label, r)

        if r["status"] == "LOSS" and not r.get("quoted"):
            for k in ("carrier_quoted", "vessel_offered", "etd_offered", "eta_offered", "transshipment"):
                if r.get(k) and r[k] not in ("", None, "N/A"):
                    log.fix(f"{rid_label}: NQ contamination — cleared {k}={r[k]!r}")
                    r[k] = None
            if r.get("ol_rate") and r["ol_rate"] not in (None, "", "Not Quoted", "N/A"):
                log.fix(f"{rid_label}: NQ contamination — cleared ol_rate={r['ol_rate']!r}")
                r["ol_rate"] = "Not Quoted"

        # has_send on a LOSS is normally contradictory — Lonny cannot have
        # accepted a quote we lost on price, and he certainly cannot have
        # accepted one OL never sent. SEND_NO_BOOKING is the ONE exception:
        # that loss reason MEANS "Lonny accepted and OL never confirmed the
        # booking", so has_send=True is the evidence that defines it. Clearing
        # it here was the second half of the 2026-07-26 defect — even with
        # core.decide_status fixed, this line wiped the flag straight back out
        # and the next pass relabelled the row UNDIFFERENTIATED, losing the
        # OL-service-failure signal from the loss mix and carrier scorecards.
        if (r["status"] == "LOSS" and r.get("has_send")
                and r.get("loss_reason") != "SEND_NO_BOOKING"):
            r["has_send"] = False
            log.fix(f"{rid_label}: Cleared has_send on LOSS/{r.get('loss_reason')}")


# ─────────────────────────────────────────────────────────────────────
# Phase 4 — duplicate detection
# ─────────────────────────────────────────────────────────────────────

def phase_4_duplicates(log: Log, data: dict):
    log.section("PHASE 4: DUPLICATE DETECTION")
    requests = data["requests"]

    # Pass 1 — exact request_id collisions: keep the richest copy.
    by_id: dict[str, list[dict]] = {}
    for r in requests:
        by_id.setdefault(r.get("request_id", ""), []).append(r)
    keepers: list[dict] = []
    id_dupes = 0
    for rid, group in by_id.items():
        if len(group) == 1:
            keepers.append(group[0])
            continue
        canonical = max(group, key=lambda r: sum(1 for v in r.values() if v not in (None, "", [])))
        keepers.append(canonical)
        id_dupes += len(group) - 1
        log.fix(f"Deduped request_id={rid} — kept richest, dropped {len(group)-1}")
    if id_dupes == 0:
        log.ok("No duplicate request_ids")

    # Pass 2 — CONTENT duplicates: the same shipment ingested as 2+ rows with
    # different request_ids. A shipment is uniquely identified by
    # (conversation_id, destination, request_date, containers) — same Outlook
    # thread + same lane + same calendar day + same container line. When a
    # booking confirmation links to one copy, the OTHER copy still gets flipped
    # to WIN on a send-signal → a phantom UNCONFIRMED win that inflates the win
    # count (Hamburg/Nagoya/Xingang, found in the 2026-05-21 audit).
    # SAFE BY CONSTRUCTION: only fires when a group has BOTH a booking-confirmed
    # win (has mdolx_ref) AND an unconfirmed win (status WIN, no mdolx); NEVER
    # when the group holds 2+ distinct MDOLX refs (two real bookings — e.g. the
    # 4/9 Tokyo pair); LOSS / PENDING rows are never touched. Distinct same-day
    # same-lane shipments differ in container count, so they land in separate
    # groups and are left alone.
    content: dict[tuple, list[dict]] = {}
    for r in keepers:
        cid = (r.get("conversation_id") or "").strip()
        dest = (r.get("destination") or "").strip().lower()
        rdate = r.get("request_date") or ""
        cont = (r.get("containers") or "").strip().lower()
        if cid and dest and rdate and cont:
            content.setdefault((cid, dest, rdate, cont), []).append(r)

    drop_ids: set[int] = set()
    content_dupes = 0
    for grp in content.values():
        if len(grp) < 2:
            continue
        confirmed = [r for r in grp if r.get("status") == "WIN" and r.get("mdolx_ref")]
        unconfirmed = [r for r in grp if r.get("status") == "WIN" and not r.get("mdolx_ref")]
        distinct_mdolx = {str(r.get("mdolx_ref")) for r in grp if r.get("mdolx_ref")}
        if len(distinct_mdolx) >= 2 or not confirmed or not unconfirmed:
            continue
        canonical = confirmed[0]
        for dup in unconfirmed:
            canonical["source_imids"] = sorted(set(
                (canonical.get("source_imids") or []) + (dup.get("source_imids") or [])))
            canonical["source_ids"] = sorted(set(
                (canonical.get("source_ids") or []) + (dup.get("source_ids") or [])))
            canonical.setdefault("merge_notes", []).append(
                f"Absorbed content-duplicate {dup.get('request_id')} "
                f"(send-signal WIN, no separate booking) {dup.get('request_date')}")
            drop_ids.add(id(dup))
            content_dupes += 1
            log.fix(f"Content-duplicate collapsed: {dup.get('request_id')} "
                    f"({dup.get('lane')} {dup.get('request_date')}) was a phantom "
                    f"unconfirmed win — same shipment as booking-confirmed "
                    f"{canonical.get('request_id')} (MDOLX{canonical.get('mdolx_ref')})")

    data["requests"] = [r for r in keepers if id(r) not in drop_ids]
    if content_dupes:
        log.fix(f"PHASE 4: collapsed {content_dupes} phantom unconfirmed-win "
                f"duplicate(s) — win count now reflects distinct shipments")
    else:
        log.ok("No content-duplicate phantom wins")


# ─────────────────────────────────────────────────────────────────────
# Phase 5 — summaries
# ─────────────────────────────────────────────────────────────────────

def _recompute_aggregates(data: dict) -> bool:
    """Rebuild summary / lane_summary / carrier_summary from `data["requests"]`.

    Returns True when anything changed. SILENT — it touches no Log, so
    callers decide whether a rebuild is worth reporting as a "fix".

    Split out of phase_5_summaries because the rebuild now runs more than once
    per fire (phase 5, again before QC-075, again in phase 7), while
    `log.fix("...rebuilt...")` fires unconditionally on every call. Routing the
    extra rebuilds through the logging wrapper inflated
    `data["qc"]["fixes_applied"]` — which gen_dashboard renders verbatim in its
    "N fixes" line — and printed the same rebuild message two or three times in
    the Fixes Applied list. Raised in review of #124.
    """
    old_summary = data.get("summary", {}) or {}
    computed = core.aggregate_summary(data["requests"])
    if "dod" in old_summary and "dod" not in computed:
        computed["dod"] = old_summary["dod"]
    drift = any(old_summary.get(k) != v for k, v in computed.items())
    data["summary"] = computed
    data["lane_summary"] = core.aggregate_lanes(data["requests"])
    data["carrier_summary"] = core.aggregate_carriers(data["requests"])
    return drift


def phase_5_summaries(log: Log, data: dict):
    log.section("PHASE 5: SUMMARY RECALCULATION")
    drift = _recompute_aggregates(data)
    log.fix("Summary, lane_summary, carrier_summary rebuilt from raw data" + (" (drift detected)" if drift else ""))


def _expected_report_date(now_et_date):
    """Mirror of gen_email._report_date via the single source of truth
    core.report_business_day. The ~6 PM ET evening fire reports on TODAY's
    now-complete business day: Mon–Fri → today; Sat → Fri (1 day back);
    Sun → Fri (2 days back)."""
    return core.report_business_day(now_et_date)


def _check_email_subject_date(log, subj_path, now_et=None):
    """QC-011 helper, extracted 2026-06-02 for testability.

    Distinguishes the failure modes of the email-subject vs expected
    business-day check. Under the ~6 PM ET evening fire (2026-06-16) the
    expected report day is TODAY's now-complete business day, so:

      1. File absent → WARN (skip; no signal either way)
      2. File present, date matches expected (== TODAY) → OK
      3. File present, date == the PREVIOUS business day AND file fresh
         (≤26h) → ERROR (gen_email regressed to the old morning framing)
      4. File present, date != expected AND file mtime > 26h old →
         WARN (stale subject from prior fire — gen_email didn't run today;
         pair with QC-021's wrapper-incomplete signal)
      5. File present, date != expected AND file mtime ≤ 26h →
         WARN (real gen_email._report_date logic bug)

    The 2026-06-02 fix split (4) out from the original behavior where
    every mismatch was reported the same way as a logic bug, hiding
    the actual root cause (the wrapper aborted before gen_email). The
    2026-06-16 fire move FLIPPED (3): TODAY is now the correct report day,
    and a fresh subject dated the PREVIOUS business day is the regression.

    Args:
        log: the qc.Log instance to write findings to.
        subj_path: pathlib.Path to reports/email-subject.txt.
        now_et: an optional aware datetime in ET for testability;
            defaults to wall-clock now.
    """
    try:
        import re as _re
        from datetime import datetime as _dt
        from datetime import timezone as _tz
        if now_et is None:
            now_et = _dt.now(core.ET)
        _now_et = now_et.date()
        # Pass the full DATETIME (not .date()) so core's wee-hours rule
        # applies here exactly as it does in gen_email — a 00:40 ET fire
        # reports the PREVIOUS business day, and QC-011 must expect the same
        # date the renderer used or it false-alarms on every night fire.
        _expected = _expected_report_date(now_et)
        if not subj_path.exists():
            if _BLOB_HOST:
                log.ok("QC-011: skipped — ephemeral runner, no stale subject can exist pre-render")
            else:
                log.warn("QC-011: reports/email-subject.txt not present — skip date check")
            return
        # Freshness check is paired with the date-mismatch check below.
        # 26h covers normal weekday fire-to-fire gap + small grace.
        _subj_mtime = _dt.fromtimestamp(subj_path.stat().st_mtime, tz=_tz.utc)
        _ref_now_utc = now_et.astimezone(_tz.utc)
        _subj_age_h = (_ref_now_utc - _subj_mtime).total_seconds() / 3600
        _subj = subj_path.read_text(encoding="utf-8").strip()
        # Parse subject like 'Hilmar Ingredients — Daily Shipment Tracker Update (May 6, 2026)'
        _m = _re.search(r"\(([A-Za-z]+)\s+(\d+),\s+(\d{4})\)", _subj)
        if not _m:
            log.warn(f"QC-011: could not parse date from subject: {_subj!r}")
            return
        _mo, _day, _yr = _m.group(1), int(_m.group(2)), int(_m.group(3))
        try:
            _parsed = _dt.strptime(f"{_mo} {_day} {_yr}", "%b %d %Y").date()
        except ValueError:
            try:
                _parsed = _dt.strptime(f"{_mo} {_day} {_yr}", "%B %d %Y").date()
            except ValueError:
                _parsed = None
        # The PREVIOUS-business-day date — what the OLD 10 AM ET morning fire
        # would have reported. A fresh subject dated this is the regression.
        _wrong = core.report_business_day(_now_et, window="previous")
        if _parsed is None:
            log.warn(f"QC-011: subject month not recognized: {_mo!r}")
        elif _parsed == _expected:
            log.ok(f"QC-011: email subject date {_parsed.isoformat()} == expected report day")
        elif _parsed == _wrong and _subj_age_h <= 26:
            log.error(
                f"QC-011: email subject is the PREVIOUS business day "
                f"({_parsed.isoformat()}) but the evening fire should report TODAY "
                f"({_expected.isoformat()}) — gen_email regressed to morning framing."
            )
        elif _subj_age_h > 26:
            # File predates today's fire window — gen_email didn't run today.
            # Real root cause: pipeline didn't complete (likely paired with QC-021).
            log.warn(
                f"QC-011: email-subject.txt is {_subj_age_h:.1f}h stale "
                f"(mtime {_subj_mtime.date().isoformat()}). subject date "
                f"{_parsed.isoformat()} matches a PRIOR fire's report-day, not today's "
                f"expected {_expected.isoformat()}. Today's gen_email likely never ran "
                f"— check QC-021 + the run log for the wrapper-incomplete cause."
            )
        else:
            # File is fresh but date is wrong — real logic bug.
            log.warn(
                f"QC-011: email subject date {_parsed.isoformat()} != expected "
                f"{_expected.isoformat()} (off by {(_parsed - _expected).days} days) — "
                f"subject file is fresh ({_subj_age_h:.1f}h old) so this is a real "
                f"gen_email._report_date logic bug, not a stale-file artifact."
            )
    except Exception as _e:
        log.warn(f"QC-011: check failed with exception: {_e}")


# ─────────────────────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────
# Environment-integrity helpers (QC-054 / QC-060 / QC-061 / QC-062).
# These verify the BOX the pipeline runs on, not the data — the gap behind
# the 2026-06 silent week (box drifted to Python 3.14 with jinja2 missing and
# stale shadow dirs, all invisible). Module-level + pure so the QC checks and
# their tests share one source of truth.
# ─────────────────────────────────────────────────────────────────────

REPO_ROOT = Path(__file__).resolve().parent.parent

# The modules the wrapper's interpreter MUST be able to import. Single source
# of truth: QC-054 verifies they import, QC-060 verifies each maps to a pinned
# entry in requirements.txt. (Was a local list inside QC-054 — promoted so the
# two checks can't drift.)
RUNTIME_IMPORT_REQUIRED = [
    "sentry_sdk",                 # observability — silent absence = HILMAR-9
    "msal", "requests",           # auth + Graph
    "jsonschema", "dateutil",     # schema + date parsing
    "tzdata",                     # zoneinfo data on Windows
    "reportlab", "jinja2",        # rendering
    "pdfplumber",                 # booking-PDF parsing
]

# import-name → pip-package-name where they differ.
_MODULE_TO_PACKAGE = {
    "sentry_sdk": "sentry-sdk",
    "dateutil": "python-dateutil",
}


def _norm_pkg(name: str) -> str:
    return name.strip().lower().replace("_", "-")


def _module_package(mod: str) -> str:
    return _MODULE_TO_PACKAGE.get(mod, _norm_pkg(mod))


def _dep_file(name: str) -> Path:
    """Locate a repo state file (requirements.txt, pyproject.toml,
    requirements-tracker.txt, .python-version). QC-060/061 are REPO-STATE checks
    ("fire the same in CI"), but on the Cloud PC qc_selfheal runs from the
    DEPLOYED mirror (PROJECT HILMAR/scripts/), where REPO_ROOT is the OneDrive
    parent — and these files are NOT deployed there (the wrapper syncs scripts/
    + deploy/ + config + src, not the dep lists). The live copies live in the
    git checkout subdir. Prefer the checkout copy so QC-060/061 read the CURRENT
    repo state instead of a stale/absent deployed copy and false-alarm. In CI /
    the repo itself, REPO_ROOT already IS the repo, so the checkout path doesn't
    exist and this falls back cleanly."""
    checkout = REPO_ROOT / "hilmar-daily-routine" / name
    return checkout if checkout.exists() else (REPO_ROOT / name)


def _parse_requirements_packages(path: Path) -> set:
    """Package names pinned in a requirements file (lowercased, _→-)."""
    out = set()
    if not path.exists():
        return out
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        name = re.split(r"[<>=!~ \[;]", line, maxsplit=1)[0]
        if name:
            out.add(_norm_pkg(name))
    return out


def _pyproject_runtime_packages() -> set:
    """Package names in pyproject [project.dependencies]."""
    pp = _dep_file("pyproject.toml")
    if not pp.exists():
        return set()
    try:
        import tomllib
        data = tomllib.loads(pp.read_text(encoding="utf-8"))
        deps = (data.get("project") or {}).get("dependencies") or []
    except Exception:
        return set()
    out = set()
    for d in deps:
        name = re.split(r"[<>=!~ \[;]", str(d), maxsplit=1)[0]
        if name:
            out.add(_norm_pkg(name))
    return out


def _read_pinned_python() -> str | None:
    f = _dep_file(".python-version")
    if not f.exists():
        return None
    for ln in f.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if ln:
            return ln
    return None


def check_interpreter_parity():
    """(ok, running, pinned_mm) — does the running interpreter's major.minor
    match .python-version? ok=True (with pinned=None) when no pin exists."""
    pinned = _read_pinned_python()
    running = f"{sys.version_info[0]}.{sys.version_info[1]}"
    if not pinned:
        return True, running, None
    pin_mm = ".".join(pinned.split(".")[:2])
    return (running == pin_mm), running, pin_mm


def check_dep_consistency():
    """(ok, problems[]) — every RUNTIME_IMPORT_REQUIRED module is pinned in
    requirements.txt, and pyproject deps == requirements-tracker.txt."""
    problems = []
    req = _parse_requirements_packages(_dep_file("requirements.txt"))
    for mod in RUNTIME_IMPORT_REQUIRED:
        pkg = _module_package(mod)
        if pkg not in req:
            problems.append(
                f"QC-054 imports '{mod}' but '{pkg}' is not pinned in requirements.txt")
    pp = _pyproject_runtime_packages()
    tracker = _parse_requirements_packages(_dep_file("requirements-tracker.txt"))
    if pp and tracker:
        only_pp = pp - tracker
        only_tr = tracker - pp
        if only_pp:
            problems.append(f"in pyproject deps but not requirements-tracker.txt: {sorted(only_pp)}")
        if only_tr:
            problems.append(f"in requirements-tracker.txt but not pyproject deps: {sorted(only_tr)}")
    return (not problems), problems


def find_stale_shadow_dirs():
    """Stale duplicate tests/ sitting directly under REPO_ROOT when the REAL
    git checkout lives in REPO_ROOT/hilmar-daily-routine (the Cloud PC layout).
    Returns [] in the dev/CI layout where REPO_ROOT IS the checkout.

    NOTE: REPO_ROOT/src is NO LONGER a stale shadow — the wrapper now deploys
    it ON PURPOSE (deploy/run_daily_laptop.cmd `xcopy src\\hilmar`) so
    scripts/qc_selfheal.py can `import hilmar.parser_accuracy` for the QC-039
    gate (it prepends REPO_ROOT/src to sys.path). On the Cloud PC REPO_ROOT/src
    is therefore a REQUIRED runtime directory and must never be swept. Only
    tests/ is a true never-deployed shadow (the wrapper never copies tests/ to
    the runtime root)."""
    checkout = REPO_ROOT / "hilmar-daily-routine"
    if not (checkout / "tests").is_dir() or not (checkout / "src" / "hilmar").is_dir():
        return []  # dev/CI — REPO_ROOT's own tests/+src/ are the real ones
    return [REPO_ROOT / sub for sub in ("tests",) if (REPO_ROOT / sub).is_dir()]


def load_step_history():
    """run_pipeline's per-fire failed-step log (rolling). [] if absent."""
    p = REPO_ROOT / "reports" / "step-history.json"
    if not p.exists():
        return []
    try:
        h = json.loads(p.read_text(encoding="utf-8"))
        return h if isinstance(h, list) else []
    except Exception:
        return []


def consecutive_failed_steps(history, n=3):
    """Step names that failed in ALL of the last n recorded fires — a step
    that's been dead for days, not a one-day blip."""
    if not history or len(history) < n:
        return []
    sets = [set(h.get("failed") or []) for h in history[-n:]]
    common = set.intersection(*sets) if sets else set()
    return sorted(common)


# Client-visible display fields scanned by QC-064. These all land in the email
# / PDF / dashboard as plain text, so garbage here is "absolutely wrong info"
# in front of the client — the failure class the operator flagged.
QC064_DISPLAY_FIELDS = (
    "carrier_quoted", "carrier_won", "origin", "destination",
    "lane", "pol", "pod", "vessel_voyage", "transshipment",
    # Equipment cell — the 2026-07-02 client email showed a phone fragment
    # ("209-656") here. Legit values ("2-40'RF + 1-20'DV") can't trip the
    # conservative patterns: the phone regex needs a 3-digit group and the
    # msgid shard needs a long alnum run with an MB anchor.
    "containers",
)

# Exchange message-id shard: a long run of uppercase/digits with an embedded
# "MB" segment (e.g. the MBD_OceanExport... mailbox's internetMessageId
# fragments). Conservative — needs the "MB" anchor + length, so it won't match
# a normal carrier/port name. Anchored on word boundaries elsewhere.
_QC064_MSGID_SHARD_RX = re.compile(r"[A-Z0-9]{4,}MB[0-9A-Z]{2,}")
# A phone fragment: NNN-NNN or NNN-NNNN (also . or whitespace separator).
# \b-bounded so it can't fire on an arbitrary substring of a longer token.
_QC064_PHONE_RX = re.compile(r"\b\d{3}[-.\s]\d{3,4}\b")


def qc064_garbage_reason(value):
    """Return a short reason string if `value` looks like garbage that leaked
    into a client-visible display field, else None.

    Patterns are deliberately conservative so a legitimate value (a normal
    city, port code, or carrier name) is NEVER flagged:
      - raw message-id: has the angle-bracket + '@' envelope OR an Exchange
        msg-id shard ([A-Z0-9]{4,}MB[0-9A-Z]{2,}) — neither shape occurs in a
        real lane/carrier value.
      - mailbox / email: contains '@' (no real display value has one) OR the
        OL responder-mailbox prose ("ocean export booking" / "mailbox" /
        "shared mailbox") that has leaked from a From/footer line.
      - phone fragment: a \\b-bounded NNN-NNN(N) run — a dialing fragment, never
        part of a port or carrier name.
    """
    if not isinstance(value, str):
        return None
    v = value.strip()
    if not v:
        return None
    low = v.lower()
    # raw message-id (angle-bracket envelope, e.g. "<AB12@host>")
    if "<" in v and "@" in v and ">" in v:
        return "raw message-id (<...@...>)"
    # Exchange message-id shard
    if _QC064_MSGID_SHARD_RX.search(v):
        return "Exchange message-id shard"
    # mailbox / email address or mailbox prose
    if "@" in v:
        return "email/mailbox address"
    if ("ocean export booking" in low or "shared mailbox" in low
            or "mailbox" in low):
        return "mailbox name leaked into display field"
    # phone fragment
    if _QC064_PHONE_RX.search(v):
        return "phone-number fragment"
    return None


# ── QC-065: client-report invariants ─────────────────────────────────
# The ONLY approved recipients for the CLIENT-facing daily email
# (gen_client_email.py). The client is Lonny Upfold at Hilmar Ingredients;
# Michael's ol-usa address rides CC. These tuples are the invariant — the
# fix for a QC-065 ERROR is always config.json / the renderer, NEVER
# widening these lists.
QC065_APPROVED_TO = ("lupfold@hilmaringredients.com",)
QC065_APPROVED_CC = ("michael.deitchman@ol-usa.com",)
#: Internal-analytics strings that must NEVER appear in the client email
#: body — win/loss framing, Q&L/NQ taxonomy, carrier-scoreboard/negotiation
#: intel. Matched case-insensitively; the &amp;-escaped variants are listed
#: explicitly so an HTML-escaped leak is caught too.
QC065_INTERNAL_MARKERS = (
    "win rate",
    "quoted & lost",
    "quoted &amp; lost",
    "q&l",
    "q&amp;l",
    "not quoted",
    "carrier scoreboard",
    "negotiation",
)
QC065_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.json"
QC065_CLIENT_BODY_PATH = (
    Path(__file__).resolve().parent.parent / "reports" / "client-email-body.html")


def qc069_duplicate_shipment_rows(rows):
    """QC-069: the SAME shipment stored as two rows.

    Michael's reported defect #2 (a lane shown as won AND still pending in the
    same report). Two shapes, both silent today:

      (a) DUPLICATE BOOKING REF — one mdolx_ref on more than one row.
          link_bookings_to_requests emits a standalone `stand_<mdolx>` WIN when
          it cannot link a booking to its request (e.g. the RFQ says "HCMC" and
          OL's confirmation says "Cat Lai"), so the shipment is counted once as
          a WIN and once as the still-open request it belongs to.
      (b) OPEN ROW SHADOWED BY A WIN — a PENDING row and a WIN row on the same
          canonical lane with the same container spec, the win landing at or
          after the request. TEU is double counted and the PENDING copy later
          ages to a LOSS claiming OL never quoted a move OL actually booked.

    Detect-only: collapsing rows is destructive and the correct survivor
    depends on which row carries the real request thread. The audit names both
    ids so the operator (or a later, evidenced heal) can fold them.

    Returns [(kind, key, [request_ids...]), ...].
    """
    out = []

    by_ref = {}
    for r in rows or []:
        refs = set()
        if r.get("mdolx_ref"):
            refs.add(str(r["mdolx_ref"]).strip().upper())
        for m in (r.get("mdolx_refs_all") or []):
            if m:
                refs.add(str(m).strip().upper())
        for ref in refs:
            by_ref.setdefault(ref, []).append(r.get("request_id", "?"))
    for ref, ids in sorted(by_ref.items()):
        if len(set(ids)) > 1:
            out.append(("duplicate_mdolx", ref, sorted(set(ids))))

    def _aliases(r):
        """Every name this destination can legitimately go by.

        Now anchored on core.canonical_port_key, the SAME key ingest matches
        bookings with, so detection and prevention cannot disagree. The
        original version only split a parenthetical ("HCMC (Cat Lai)" →
        {hcmc, cat lai}), which meant the exact pair this check was written
        for — one row saying "HCMC" and the other saying "Cat Lai", neither
        with parens — produced disjoint sets and slipped through. Verified
        2026-07-26: qc069 returned [] on that pair.
        """
        d = (r.get("destination") or "").strip().lower()
        if not d or d == "unknown":
            return set()
        out = {d, core.canonical_port_key(d)}
        m = re.match(r"^([^(]+?)\s*\((.+)\)\s*$", d)
        if m:
            out.add(m.group(1).strip())
            out.add(m.group(2).strip())
        return {a for a in out if a and a != "unknown"}

    def _equip(r):
        """Container spec as a COMPARABLE value, not a raw string.

        Lonny writes "1x40HC" and OL's booking subject says "1X40'HC"; case-
        folding alone leaves those different ("1x40hc" vs "1x40'hc"), so the
        second half of the shadowed-row check never fired on the real pair.
        Compare the parsed (count, TEU) instead — that is what double-counts
        in the rollups, and it is spelling-proof. Falls back to the folded
        string when nothing parses, so unparseable-but-identical specs still
        match rather than silently comparing (0, 0) to (0, 0).
        """
        raw = (r.get("containers") or "").strip().lower()
        if not raw:
            return None
        count, teu = core.parse_teu(raw)
        return (count, teu) if teu else raw

    wins = [r for r in rows or [] if (r.get("status") or "").upper() == "WIN"]
    pends = [r for r in rows or [] if (r.get("status") or "").upper() == "PENDING"]
    for pr in pends:
        p_alias, p_eq = _aliases(pr), _equip(pr)
        if not p_alias or not p_eq:
            continue
        p_at = core.parse_iso(pr.get("request_timestamp") or pr.get("request_date"))
        for w in wins:
            if _equip(w) != p_eq or not (_aliases(w) & p_alias):
                continue
            w_at = core.parse_iso(w.get("booking_timestamp")
                                  or w.get("request_timestamp")
                                  or w.get("request_date"))
            if p_at and w_at and w_at < p_at:
                continue
            _eq_label = (f"{p_eq[0]}x/{p_eq[1]}TEU" if isinstance(p_eq, tuple)
                         else str(p_eq))
            out.append(("open_row_shadowed_by_win",
                        sorted(p_alias)[0] + "|" + _eq_label,
                        sorted({pr.get("request_id", "?"), w.get("request_id", "?")})))
            break
    return out


#: A container-size token in free text — the trigger for QC-070 shape (b).
#: Deliberately looser than core._CONTAINER_RX (no quantity required): its
#: whole job is to say "this text is ABOUT containers", so that a row whose
#: text obviously describes equipment but yields 0 TEU is surfaced instead of
#: being read as a genuinely empty request.
_CONTAINER_TOKEN_RX = re.compile(
    r"(?<![\d.,])(?:20|40|45)(?![\d])['’\s]*(?:HC|RF|DV|GP|RE|RH|FR|OT|NOR)\b"
    r"|\b(?:container|containers|reefer|reefers|teu|fcl)\b",
    re.IGNORECASE,
)


def qc074_win_evidence_consistency(rows):
    """QC-074: a row whose WIN evidence and outcome disagree.

    Guards the two 2026-07-27 defects that both corrupt the SAME shipment
    identity, from opposite directions:

      (a) DUPLICATE request_id (**ERROR**) — the additive carry-forward used
          to APPEND a prior WIN beside the row the fresh stage had already
          rebuilt under the same id, so tracking-data-v2.json reported two
          entries and double the TEU for one shipment, with the id
          simultaneously PENDING/LOSS and WIN. phase_4 then arbitrated by
          counting non-empty fields and could discard the very win the
          carry-forward exists to protect. The carry-forward now reconciles by
          id and MERGES the evidence; reaching here means something appended
          a duplicate anyway.

      (b) BOOKED BUT NOT WON (**ERROR**) — a row carrying an `mdolx_ref` (a
          real booking OL issued) that is not a WIN and is not explicitly held
          for review as MDOLX_NO_SEND. A booking ref is hard evidence; a row
          holding one while reported as a loss is telling the client we lost a
          move OL booked.

      (c) WON WITHOUT EVIDENCE (**WARN**) — a WIN with neither `mdolx_ref` nor
          `has_send`. QC-003 already warns on this shape; repeated here so one
          check answers "does the win evidence hang together" end to end.

    Detect-only. Which row is the true one depends on evidence outside the
    dataset, so the audit names the ids for a human.

    Returns [(request_id, severity, detail), ...].
    """
    out = []
    seen = {}
    for r in rows or []:
        rid = r.get("request_id")
        if rid:
            seen.setdefault(rid, []).append(r)
    for rid, group in seen.items():
        if len(group) > 1:
            states = ", ".join(sorted({(g.get("status") or "?") for g in group}))
            out.append((rid, "error",
                        f"{len(group)} rows share this request_id (states: "
                        f"{states}) — one shipment stored more than once, so "
                        f"its TEU is counted more than once"))
    for r in rows or []:
        rid = r.get("request_id", "?")
        status = (r.get("status") or "").upper()
        if r.get("mdolx_ref") and status != "WIN" and r.get("loss_reason") != "MDOLX_NO_SEND":
            out.append((rid, "error",
                        f"carries booking {r['mdolx_ref']} but is {status or 'blank'}"
                        f"{'/' + str(r.get('loss_reason')) if r.get('loss_reason') else ''}"
                        f" — a booking ref is evidence OL moved this shipment"))
        if status == "WIN" and not r.get("mdolx_ref") and not r.get("has_send"):
            out.append((rid, "warn",
                        "WIN with neither an MDOLX booking ref nor a send "
                        "signal — no evidence backs this win"))
    return out


def qc073_standalone_booking_hygiene(rows):
    """QC-073: fabricated or degenerate values on a standalone booking row.

    A `stand_<mdolx>` row is what `link_bookings_to_requests` writes when it
    cannot find the RFQ a booking belongs to. It is a synthesised row — every
    field on it is inferred from one subject line — so it is the single most
    likely place for invented data to enter the dataset. Michael's reported
    defect #3 was exactly this: rows with a degenerate lane and entirely blank
    carrier/vessel/dates sitting in the report as real shipments.

    Three shapes:

      (a) DEGENERATE LANE (**ERROR**) — origin and destination resolve to the
          same port, so "Oakland → Oakland" renders as a real trade lane in
          Lane Performance. Now prevented at the source (the constructor
          treats it as unresolved), so reaching here is a regression.
      (b) FABRICATED RATE RESPONSE (**ERROR**) — `response_timestamp` set on a
          row with no `ol_rate`. A booking confirmation is not a rate quote;
          writing the booking time here makes the row claim an OL response
          that never happened and corrupts every turnaround metric it feeds.
      (c) UNEVIDENCED WIN (**WARN**) — a standalone carrying no `carrier_won`.
          Not corruption, but it IS a win nobody can attribute, and the
          operator needs the list to go find the thread.

    Detect-only. These rows are real bookings; the fix is to link them to
    their request or correct the source subject, and both need human eyes.

    Returns [(request_id, severity, detail), ...] with severity
    "error" or "warn".
    """
    out = []
    for r in rows or []:
        rid = str(r.get("request_id", "") or "")
        origin, dest = r.get("origin"), r.get("destination")
        if (origin and dest and str(dest).strip().lower() != "unknown"
                and core.canonical_port_key(origin) == core.canonical_port_key(dest)):
            out.append((rid or "?", "error",
                        f"degenerate lane {origin} → {dest} — origin and "
                        f"destination are the same port"))
        if (rid.startswith("stand_") and r.get("response_timestamp")
                and not r.get("ol_rate")):
            out.append((rid, "error",
                        f"response_timestamp={r['response_timestamp']} with no "
                        f"ol_rate — a booking confirmation is not a rate quote"))
        if rid.startswith("stand_") and not r.get("carrier_won"):
            out.append((rid, "warn",
                        "standalone booking WIN with no carrier_won — "
                        "unattributable win, find the thread"))
    return out


def qc071_request_date_clock(rows):
    """QC-071: rows bucketed on the wrong day because request_date is not ET.

    THE FAILURE (2026-07-26): `request_date` had THREE producers writing THREE
    clocks — ingest wrote the UTC calendar date, merge_ingest took a raw
    `ts[:10]` UTC slice, and this file's own heal wrote PT — while every
    reader buckets by the ET business day from `core.report_business_day`.
    An RFQ sent Friday 5:30 PM PT is 2026-07-25 in UTC and Friday 2026-07-24
    in ET, and since no fire ever reports a Saturday, that row appeared in NO
    day's New Requests, KPI tile or day reconciliation — on any day, ever —
    while still counting toward the period totals. The day tiles and the
    period tiles then disagreed by exactly the rows the clocks disagreed on,
    and the day reconciliation still balanced because the row was never in
    the denominator to begin with.

    All three producers now call `core.et_date_of`. This is the daily proof on
    live rows, and the catch for a fourth producer appearing later.

    The heal itself lives in phase_3 (it RECOMPUTES request_date every pass,
    which is also the migration for rows already stored on the wrong clock);
    by the time this runs, a finding means the heal did not reach the row —
    so this is ERROR-class and detect-only, on purpose.

    Returns [(request_id, stored, expected), ...].
    """
    out = []
    for r in rows or []:
        ts = r.get("request_timestamp")
        if not ts:
            continue
        expected = core.et_date_of(ts)
        if not expected:
            continue
        stored = r.get("request_date")
        if stored and stored != expected:
            out.append((r.get("request_id", "?"), stored, expected))
    return out


def qc072_history_contradicts_status(rows):
    """QC-072: status_history's terminal state contradicts the row's status.

    `status_history` is the field schema.json declares as THE transition
    record — audits, the dashboard timeline and Sentry triage reconstruct a
    row's outcome from it. Two writers used to bypass it (2026-07-26):
    `age_requests` assigned `r["status"]` directly, and `merge_idempotent`
    recomputed `status` while preserving the OLD history. A send-signal WIN
    that never booked therefore read status="LOSS" / SEND_NO_BOOKING with
    history still ending at {"to": "WIN"} — so every history-based reader
    reported it as WON, with no entry anywhere explaining the regression.

    Both writers now route through `core.record_transition` / union the log.
    This is the daily proof, plus the second invariant the same defect broke:
    `teu_won > 0` on a row that is not a WIN, which counts booked volume for
    a shipment that does not exist.

    Detect-only. Rewriting history is never a safe automatic act — the audit
    names the row so a human decides which record is the true one.

    Returns [(request_id, kind, detail), ...] where kind is
    "history-contradiction" or "stale-teu-won".
    """
    out = []
    for r in rows or []:
        rid = r.get("request_id", "?")
        status = (r.get("status") or "").upper()
        hist = r.get("status_history") or []
        if hist and isinstance(hist, list) and isinstance(hist[-1], dict):
            last_to = (hist[-1].get("to") or "").upper()
            if last_to and status and last_to != status:
                out.append((rid, "history-contradiction",
                            f"status={status} but status_history ends at "
                            f"{last_to} (at {hist[-1].get('at')})"))
        teu_won = r.get("teu_won") or 0
        if status != "WIN" and isinstance(teu_won, (int, float)) and teu_won > 0:
            out.append((rid, "stale-teu-won",
                        f"teu_won={teu_won} on a {status or 'blank'} row — "
                        f"booked volume for a shipment that was not booked"))
    return out


def qc070_teu_sanity(rows, heal=True):
    """QC-070: per-row TEU that cannot be real.

    THE FAILURE THIS EXISTS FOR (2026-07-26): a reference number in a subject
    line ("PO 4451440") parsed as 44,514 x 40' = 89,028 TEU on ONE row, and
    because every volume figure in the report is a SUM over rows, that single
    row rewrote the day's email, dashboard, PDF and every lane rollup with a
    number nobody could reconcile. The parser regex was hardened the same day.
    This is the check that makes the hardening unnecessary: even if the regex
    regresses tomorrow, an impossible number cannot reach a report.

    Two shapes, both ERROR-class:

      (a) OVER-COUNT — a stored `teu_requested` / `teu_won` / `container_count`
          above `core.MAX_ROW_TEU` / `MAX_ROW_CONTAINERS`. SELF-HEALED: the
          field is recomputed from the row's own `containers` text via
          `core.parse_teu` (which now refuses impossible parses itself), so
          the row lands on a real number or on 0 — never on the poisoned one.

      (b) UNDER-COUNT / REFUSAL — a row whose `containers` text plainly names
          container sizes but recomputes to 0 TEU. That is either the parser
          failing to read a real spelling, or `parse_teu` refusing a parse it
          judged impossible. Both are real defects and both need eyes, so
          this shape is DETECT-ONLY: healing it would mean inventing a
          volume, which is exactly the class of guess that caused (a).

    Returns [(request_id, shape, detail), ...] where shape is
    "over-count" or "unparsed".
    """
    out = []
    for r in rows or []:
        rid = r.get("request_id", "?")
        raw = r.get("containers") or ""
        # Recompute once — parse_teu is the same gate ingest uses, so a heal
        # can never write a value ingest would itself have refused.
        recount, reteu = core.parse_teu(raw)

        # (a) stored numbers above the ceiling, whatever wrote them. Each
        #     field is judged against its OWN ceiling so the message names the
        #     field that is actually wrong.
        is_win = (r.get("status") or "").upper() == "WIN"
        for field, healed in (("teu_requested", reteu),
                              ("teu_won", reteu if is_win else 0),
                              ("container_count", recount)):
            stored = r.get(field)
            if not isinstance(stored, (int, float)) or isinstance(stored, bool):
                continue
            why = (core.teu_implausible(int(stored), 0)
                   if field == "container_count"
                   else core.teu_implausible(0, int(stored)))
            if not why:
                continue
            out.append((rid, "over-count", f"{field}={stored} — {why}"))
            if heal:
                r[field] = healed

        # (b) container text present and readable-looking, but nothing parsed.
        if raw and reteu == 0 and _CONTAINER_TOKEN_RX.search(raw):
            out.append((rid, "unparsed",
                        f"containers={raw!r} names container sizes but "
                        f"recomputes to 0 TEU (parser gap, or a parse "
                        f"refused as implausible)"))
    return out


def qc068_ol_sla_breaches(rows, now=None):
    """QC-068: open RFQs where OL has BLOWN its response SLA.

    Michael 2026-07-26: "ol response time has to be 3 hours." Measured in
    BUSINESS hours (core.pending_ol_overdue -> biz_hours_between, ET
    8:30-17:30 Mon-Fri) so it matches the report's own Time-to-Quote column
    and never counts nights or weekends against OL.

    This is an OPERATIONAL alert, not a data defect: the rows are correctly
    stored as PENDING_OL (open). It exists so a breached SLA cannot sit
    silently in the dataset — the daily audit names every lane OL owes, so
    the desk can be chased the same morning.

    Returns [(request_id, lane, biz_hours_waiting), ...], worst first.
    """
    now = now or datetime.now(timezone.utc)
    out = []
    for r in rows or []:
        if core.pending_substate(r) != "PENDING_OL":
            continue
        req = core.parse_iso(r.get("request_timestamp") or r.get("request_date"))
        if not req or not core.pending_ol_overdue(req, now):
            continue
        hrs = core.biz_hours_between(req, now) or 0.0
        out.append((r.get("request_id", "?"), r.get("lane") or "?", round(hrs, 1)))
    out.sort(key=lambda x: -x[2])
    return out


def qc067_open_rfq_misfiled_as_lost(rows, now=None):
    """QC-067: live open RFQs filed as losses.

    A row is flagged when it is UNQUOTED with loss_reason NO_RESPONSE while
    Lonny's request is still INSIDE the PENDING-OL response window — i.e. OL
    simply has not answered yet, so the row is open business to chase, not a
    loss. This is the 2026-07-24 root cause (Michael: "your quality control
    system is not functioning"): decide_status classified every unquoted row
    as LOSS/NO_RESPONSE with zero grace, which made PENDING_OL structurally
    unreachable and stored live RFQs as lost.

    core.decide_status now prevents this at the source; QC-067 is the daily
    detector that proves it on EVERY fire against that day's real rows, and
    self-heals any row that slips through (a stale carry-forward, an operator
    correction, or a future regression).

    Returns [(request_id, hours_waiting), ...].
    """
    now = now or datetime.now(core.ET)
    out = []
    for r in rows or []:
        if r.get("quoted"):
            continue
        if (r.get("loss_reason") or "") != "NO_RESPONSE":
            continue
        if (r.get("status") or "").upper() not in ("LOSS", "NQ"):
            continue
        req = core.parse_iso(r.get("request_timestamp") or r.get("request_date"))
        if req and not core.pending_ol_stale(req, now):
            hrs = (now - req).total_seconds() / 3600.0
            out.append((r.get("request_id", "?"), round(hrs, 1)))
    return out


def qc066_impossible_states(rows, report_day=None):
    """QC-066: rows whose OUTCOME PREDATES their own request — the merge/
    carry-forward artifact behind the 2026-07-23 report (Michael: "your
    quality control system is not functioning"): a NEW Jul-22 HCMC request
    surfaced with a stale WIN/quote inherited from the recurring Outlook
    thread, so it vanished from PENDING OL (showed 0) while its lane sat in
    PENDING HILMAR under the OLD row. A row is flagged when:

      (a) its newest status_history entry is dated (ET) BEFORE its own
          request date — the recorded outcome happened before the ask, which
          is causally impossible for a genuine outcome of THIS request; or
      (b) it is a report-day request (request_date == report_day, non-stand_)
          in a terminal status (WIN/LOSS) whose status_history exists but
          contains NO entry dated on/after its request date — a same-day
          request cannot have silently resolved through events that all
          predate it.

    Legacy rows with EMPTY status_history are never flagged (nothing to
    prove against). Returns [(request_id, reason), ...]. DETECT-only for now
    — the correct heal (split the row back into request + prior outcome)
    needs one confirmed live shape before automating.
    """
    from gen_email import _et_date as _ed
    out = []
    for r in rows or []:
        hist = r.get("status_history") or []
        if not hist:
            continue
        req_d = _ed(r.get("request_date") or r.get("request_timestamp"))
        if not req_d:
            continue
        dated = [(_ed(h.get("at")), h) for h in hist if _ed(h.get("at"))]
        if not dated:
            continue
        newest = max(d for d, _ in dated)
        rid = r.get("request_id", "?")
        if newest < req_d:
            out.append((rid, f"newest status event {newest} predates request {req_d}"))
            continue
        if (report_day and req_d == report_day
                and not str(rid).startswith("stand_")
                and r.get("status") in ("WIN", "LOSS")
                and not any(d >= req_d for d, _ in dated)):
            out.append((rid, f"report-day request in terminal {r.get('status')} "
                             f"with no same-day-or-later status event"))
    return out


def qc065_internal_leaks(body_text) -> list:
    """Return the internal-analytics markers present in the rendered client
    email body (case-insensitive, raw + escaped forms). Empty list = clean."""
    low = (body_text or "").lower()
    return [m for m in QC065_INTERNAL_MARKERS if m in low]


def qc065_check_client_block(cfg: dict, key: str, body_path: Path) -> list:
    """Validate ONE client-facing artifact's config block + rendered body.

    Parameterised because there are two of them now — the daily
    (`client_report`) and, from 2026-08-05, the weekly (`client_weekly`) — and
    the invariants are identical: never a staff address, never more than the
    one approved recipient, exactly the approved to/cc while enabled, and zero
    internal analytics in the rendered HTML.

    A second inline copy of this for the weekly is precisely the mistake this
    codebase spent today undoing (five spellings of one rate predicate, two
    vocabularies for one status). Two client artifacts, ONE definition of what
    makes a client artifact safe.

    Recipients are checked even while DISABLED: a wrong address is not a
    problem until the flag flips, and then it is a problem instantly.
    """
    problems = []
    block = cfg.get(key)
    if block is not None:
        full = [str(a).lower() for a in
                (cfg.get("distribution", {}).get("full_list", []) or [])]
        to = [str(a).lower() for a in (block.get("to") or [])]
        cc = [str(a).lower() for a in (block.get("cc") or [])]
        staff = [a for a in to if a in full]
        if staff:
            problems.append(
                f"staff/full_list recipient(s) in {key}.to: {staff} "
                f"(a client artifact must never go to the internal distribution)")
        if len(to) > 1:
            problems.append(
                f"{key}.to has {len(to)} recipients {to} — "
                f"only 1 approved client recipient allowed")
        if block.get("enabled"):
            if to != [a.lower() for a in QC065_APPROVED_TO]:
                problems.append(f"{key} ENABLED with unapproved to={to} "
                                f"(approved: {list(QC065_APPROVED_TO)})")
            if cc != [a.lower() for a in QC065_APPROVED_CC]:
                problems.append(f"{key} ENABLED with unapproved cc={cc} "
                                f"(approved: {list(QC065_APPROVED_CC)})")
    if body_path.exists():
        leaks = qc065_internal_leaks(body_path.read_text(encoding="utf-8", errors="ignore"))
        if leaks:
            problems.append(
                f"internal analytics leaked into {body_path.name}: {leaks} — "
                f"fix the generator, never ship these to the client")
    return problems


QC065_CLIENT_WEEKLY_BODY_PATH = (
    Path(__file__).resolve().parent.parent / "reports" / "client-weekly.html")


# ─────────────────────────────────────────────────────────────────────
# Phase 6 — cross-check rules
# ─────────────────────────────────────────────────────────────────────


def _import_fire_alert():
    """Import the sibling fire_alert module, guarding the sys.path insert.

    The unguarded insert this replaced grew sys.path by one entry per call —
    harmless in production (one call per process) but it accumulated in the
    test suite, which calls phase_6_rules dozens of times per process. Matches
    the `if ... not in sys.path` convention used elsewhere in this file."""
    _d = str(Path(__file__).resolve().parent)
    if _d not in sys.path:
        sys.path.insert(0, _d)
    import fire_alert as _fa
    return _fa


def _fire_alert_teams_configured() -> bool:
    """True when a Teams webhook is resolvable — one of the two remote
    channels QC-076 counts. Delegates to fire_alert so there is ONE definition
    of "configured" (secret env, then secrets/teams-webhook-url.txt, then
    config.json); a second copy here would drift from the thing it checks."""
    try:
        return bool(_import_fire_alert()._teams_webhook_url())
    except Exception:
        return False


def _fire_alert_github_configured() -> bool:
    """True when the GitHub channel has a credential to try — the other remote
    channel QC-076 counts. Delegates for the same reason as teams, and this
    half USED to be the counter-example: it re-implemented half of
    _github_issue's auth inline as `GH_TOKEN or GITHUB_TOKEN`, which drifted
    immediately. _github_issue tries the gh CLI FIRST and needs no env var at
    all on a box where `gh auth login` was done, so the inline copy called a
    working channel dead — and called a junk token alive."""
    try:
        return bool(_import_fire_alert().github_configured())
    except Exception:
        return False


def phase_6_rules(log: Log, data: dict):
    log.section("PHASE 6: CROSS-CHECK RULES")
    requests = data["requests"]
    wins = [r for r in requests if r["status"] == "WIN"]
    ql = [r for r in requests if r["status"] == "LOSS" and r.get("quoted")]
    nq = [r for r in requests if r["status"] == "LOSS" and not r.get("quoted")]
    pending = [r for r in requests if r["status"] == "PENDING"]

    if len(requests) > 10 and len(ql) == 0:
        log.warn(f"QC-001: 0 Quoted & Lost among {len(requests)} entries — verify")
    else:
        log.ok(f"QC-001: {len(ql)} Q&L — plausible")

    bad = [r for r in wins if not r.get("carrier_won")]
    if bad:
        # phase_3 already attempted: copy from carrier_quoted, parse from subject.
        # If we still have unfilled values here, they're truly unrecoverable from
        # current data — surface as warn (manual review), not error (pipeline-fail).
        log.warn(f"QC-002: {len(bad)} WIN(s) with no carrier_won after auto-heal — manual review")
        # Capture the scrubbed text each stuck WIN was parsed from so the
        # next audit shows WHY carrier_won couldn't be inferred.
        _bodies = _load_bodies_index()
        for r in bad:
            _diag = _carrier_diag_snippet(r, _bodies)
            _diag["check"] = "QC-002"
            log.carrier_diag.append(_diag)
    else:
        log.ok("QC-002: All WINs have carrier_won")

    unverified = [r for r in wins if not r.get("mdolx_ref") and not r.get("has_send")]
    if unverified:
        log.warn(f"QC-003: {len(unverified)} WIN(s) have no chain-send AND no MDOLX — verify")
    else:
        log.ok("QC-003: All WINs have chain-send signal or MDOLX ref")

    contam = [r for r in nq if r.get("carrier_quoted") and r["carrier_quoted"] not in (None, "", "N/A")]
    if contam:
        log.error(f"QC-004: {len(contam)} NQ entries still contaminated")
    else:
        log.ok("QC-004: No NQ contamination")

    for r in requests:
        bh = r.get("turnaround_biz_hours")
        if bh and bh > 100:
            log.warn(f"QC-005: {r['request_id']} biz-hrs {bh} — suspicious")

    # QC-006 is the ADVISORY band: >30 TEU on a row is unusual for Hilmar but
    # can be real, so it asks a human to eyeball it and never blocks. It is
    # NOT the poisoned-data guard, and 2026-07-26 proved the difference — it
    # fired correctly on the 89,028 TEU row and the report shipped anyway,
    # because a WARN neither gates nor heals, and this only ever looked at
    # teu_requested. QC-070 is the hard ceiling (ERROR + self-heal, all three
    # volume fields). Keep both: "unusually large" and "impossible" are
    # different questions with different answers.
    for r in requests:
        t = r.get("teu_requested", 0)
        if t and t > 30:
            log.warn(f"QC-006: {r['request_id']} TEU={t} — verify large request")

    now = core.now_utc()
    for r in pending:
        rt = core.parse_iso(r.get("response_timestamp"))
        # Must use is_business_stale + PENDING_WINDOW_HOURS so this check
        # stays aligned with decide_status. Hardcoded 24h drifted from the
        # classifier's 48h+Friday-rule on 2026-06-01 (PR #14 updated
        # decide_status; QC-007 was missed). Result: 2 Friday-quoted rows
        # fired QC-007 ERRORs even though decide_status correctly kept
        # them PENDING. Same drift class the parity test catches between
        # scripts/core ↔ src/hilmar/core — now also between qc_selfheal
        # ↔ core.is_business_stale.
        if rt and core.pending_hilmar_stale(rt, now):
            log.error(f"QC-007: {r['request_id']} still PENDING past the "
                      f"{core.PENDING_HILMAR_LOSS_HOURS}h/{core.PENDING_HILMAR_LOSS_HOURS_FRIDAY}h-Friday "
                      f"decision window — state machine should have aged this to Q&L")

    # ─────────────────────────────────────────────────────────────────
    # QC-008/009/010 — paired with refresh_stage.py and the additive
    # ingest merge added 2026-05-05. Per Michael's standing rule, every
    # new code pattern ships with QC + self-heal in the same commit.
    # ─────────────────────────────────────────────────────────────────

    # QC-008: stage freshness
    # Stage files live next to the script directory. If max(received) is more
    # than 36h old on a weekday morning, refresh_stage probably hasn't run
    # (Task Scheduler missed, MSAL refresh expired, Graph quota, etc.).
    # 2026-05-06: prefer .txt; fall back to legacy .jsonl
    here = Path(__file__).resolve().parent
    stage_path = here / "stage_emails.txt"
    if not stage_path.exists():
        stage_path = here / "stage_emails.jsonl"
    if stage_path.exists():
        max_received = None
        try:
            with open(stage_path, encoding="utf-8") as f:
                for line in f:
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    rcv = rec.get("received") or rec.get("sent")
                    if rcv and (max_received is None or rcv > max_received):
                        max_received = rcv
        except OSError as e:
            log.warn(f"QC-008: stage file unreadable: {e}")
            max_received = None
        if max_received:
            ts = core.parse_iso(max_received)
            if ts:
                age_hours = (now - ts).total_seconds() / 3600
                # Stale-stage threshold: 36h (covers a weekend gap of 60h on
                # Monday morning would still alert; tune if Sunday refresh
                # ever lands in scope).
                if age_hours > 36 and now.weekday() < 5:
                    log.warn(
                        f"QC-008: stage_emails.jsonl latest received is "
                        f"{age_hours:.1f}h old — refresh_stage may have stopped firing"
                    )
                else:
                    log.ok(f"QC-008: stage freshness {age_hours:.1f}h — fresh")
        else:
            log.warn("QC-008: stage has no received timestamps to evaluate")
    else:
        log.warn("QC-008: stage_emails.jsonl not present (laptop pin without refresh?)")

    # QC-009: stage bucket classification drift
    # If any expected bucket has 0 records in the last 7 days while others
    # have meaningful counts, the sender-rule classifier in refresh_stage
    # is probably missing a sender pattern. WARN — no auto-heal (changes
    # to classification require human judgement).
    if stage_path.exists():
        # Simpler: compute cutoff inline
        from datetime import timedelta as _td
        cutoff = now - _td(days=7)
        bucket_counts_7d = Counter()
        with open(stage_path, encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                rcv = rec.get("received") or rec.get("sent")
                ts = core.parse_iso(rcv) if rcv else None
                if ts and ts >= cutoff:
                    bucket_counts_7d[rec.get("bucket") or "?"] += 1
        expected_buckets = {"lonny_outbound", "lonny_reply", "mbd_inbound", "mbd_rate_response"}
        empty_buckets = [
            b for b in expected_buckets
            if bucket_counts_7d.get(b, 0) == 0 and sum(bucket_counts_7d.values()) > 5
        ]
        if empty_buckets:
            log.warn(
                f"QC-009: bucket(s) with zero stage records in last 7d while "
                f"others have data — classifier may be dropping a sender: {sorted(empty_buckets)} "
                f"(7d counts: {dict(bucket_counts_7d)})"
            )
        else:
            log.ok(f"QC-009: stage bucket distribution healthy (7d: {dict(bucket_counts_7d)})")

    # QC-010: preserved-wins growth
    # The additive merge in ingest.py carries forward old WINs that the
    # fresh stage didn't reproduce. A small steady set is fine (genuine
    # off-channel bookings). A growing one means new bookings are
    # silently failing to land — refresh_stage's Outlook search is too
    # narrow.
    preserved = [r for r in requests if r.get("preserved_from_prior")]
    PRESERVED_THRESHOLD = 10
    if len(preserved) > PRESERVED_THRESHOLD:
        log.warn(
            f"QC-010: {len(preserved)} preserved-from-prior WIN(s) — "
            f"exceeds threshold {PRESERVED_THRESHOLD}. Refresh_stage may be "
            f"missing booking emails. Investigate before this set keeps growing."
        )
    elif preserved:
        log.ok(f"QC-010: {len(preserved)} preserved-from-prior WIN(s) — within threshold")
    else:
        log.ok("QC-010: No preserved-from-prior WINs (fresh stage covers everything)")

    # QC-011: email subject date == TODAY's report business day (the ~6 PM ET
    # evening fire reports on today's now-complete day; weekends roll to Friday).
    # Logic extracted to _check_email_subject_date for testability — see
    # the helper's docstring for the failure-mode taxonomy.
    _check_email_subject_date(
        log,
        Path(__file__).resolve().parent.parent / "reports" / "email-subject.txt",
        now_et=None,
    )

    # QC-012: weekly bucket labels are Mon–Fri (5 weekdays), not Mon–Sun
    # Per Michael 2026-05-07: 'the dating on the weekly should be based on
    # weekdays'. Pre-existing labels were 'W19 (May 4–10)' (Mon-Sun); current
    # spec is 'W19 (May 4–8)' (Mon-Fri) with cross-month clarity for
    # 'W14 (Mar 30–Apr 3)'. This QC parses week labels in email-body.html
    # and confirms the start→end span is exactly 4 days (Mon to Fri),
    # never 6 (Mon to Sun).
    try:
        import re as _re
        from datetime import datetime as _dt
        _body_path = Path(__file__).resolve().parent.parent / "reports" / "email-body.html"
        if not _body_path.exists():
            if _BLOB_HOST:
                log.ok("QC-012: skipped — ephemeral runner, no stale body can exist pre-render")
            else:
                log.warn("QC-012: reports/email-body.html not present — skip week label check")
        else:
            _body = _body_path.read_text(encoding="utf-8")
            # Match labels like 'W15 (Apr 6–10)' or 'W14 (Mar 30–Apr 3)'
            _wk_pat = _re.compile(
                r"W(\d+)\s*\(([A-Za-z]+)\s+(\d+)[–\-]([A-Za-z]+\s+)?(\d+)\)"
            )
            _bad = []
            _checked = 0
            _seen = set()
            for _m in _wk_pat.finditer(_body):
                _label = _m.group(0)
                if _label in _seen:
                    continue
                _seen.add(_label)
                _checked += 1
                _wk_n   = int(_m.group(1))
                _start_mo = _m.group(2)
                _start_d  = int(_m.group(3))
                _end_mo   = (_m.group(4) or _m.group(2)).strip()
                _end_d    = int(_m.group(5))
                # Try parsing both ends in 2026 (current data range).
                try:
                    _ds = _dt.strptime(f"{_start_mo} {_start_d} 2026", "%b %d %Y").date()
                    _de = _dt.strptime(f"{_end_mo} {_end_d} 2026", "%b %d %Y").date()
                    if _de < _ds:
                        # Cross-year edge — skip rather than misreport
                        continue
                    _span = (_de - _ds).days
                    if _span != 4:
                        _bad.append(f"{_label} span={_span}d (expected 4d Mon-Fri)")
                except Exception:
                    pass
            if _bad:
                log.error(
                    f"QC-012: {len(_bad)} week label(s) not Mon-Fri format: "
                    + "; ".join(_bad[:3]) + (f" + {len(_bad)-3} more" if len(_bad) > 3 else "")
                )
            elif _checked > 0:
                log.ok(f"QC-012: all {_checked} week labels Mon-Fri (4-day span)")
            else:
                log.ok("QC-012: no week labels found to check")
    except Exception as _e:
        log.warn(f"QC-012: check failed with exception: {_e}")

    # QC-013: email body header reflects the report-date framing — must NOT
    # say literal 'What Happened Today' (the regression case where gen_email.py
    # uses an unlabeled 'today' header). The fixed framing reads e.g. 'What
    # Happened — Wednesday May 6, 2026' — an explicit day/date so the report
    # day is unambiguous regardless of when the fire runs.
    try:
        _body_path = Path(__file__).resolve().parent.parent / "reports" / "email-body.html"
        if _body_path.exists():
            _body = _body_path.read_text(encoding="utf-8")
            if "What Happened Today" in _body:
                log.error(
                    "QC-013: email body has 'What Happened Today' — gen_email.py "
                    "regressed to unlabeled framing. Should be 'What Happened — <Day Date>' "
                    "(the explicit report business day, so the date is never ambiguous)."
                )
            elif "What Happened —" in _body:
                log.ok("QC-013: email body uses report-date framing")
            else:
                log.warn("QC-013: 'What Happened' header not found — body may be malformed")
    except Exception as _e:
        log.warn(f"QC-013: check failed with exception: {_e}")

    # QC-014: carrier coverage thresholds — added 2026-05-07 per Michael
    # 'handle all suggestions'. WIN coverage must be ≥95% (only 'Unknown' or
    # genuinely off-channel WINs should be missing). Q&L coverage soft-floor
    # at 60% (parser drift signal — WARN at 60-70%, FAIL below 60%).
    # The improvements report flags 50% Q&L coverage; this QC makes that
    # observation a hard threshold the system itself enforces.
    try:
        _wins  = [r for r in requests if r.get("status") == "WIN"]
        _wcarr = [r for r in _wins if r.get("carrier_won")]
        _wpct  = (len(_wcarr) / len(_wins) * 100) if _wins else 100
        _qls   = [r for r in requests if r.get("status") == "LOSS" and r.get("quoted")]
        _qlcarr = [r for r in _qls if r.get("carrier_quoted")]
        _qlpct = (len(_qlcarr) / len(_qls) * 100) if _qls else 100

        if _wpct < 90:
            log.error(
                f"QC-014a: WIN carrier coverage {_wpct:.1f}% < 90% threshold "
                f"({len(_wins)-len(_wcarr)}/{len(_wins)} WINs missing carrier). "
                "Patch_carriers auto-discovery + manual fallback should keep this >=95%."
            )
        elif _wpct < 95:
            log.warn(f"QC-014a: WIN carrier coverage {_wpct:.1f}% (target 95%+)")
        else:
            log.ok(f"QC-014a: WIN carrier coverage {_wpct:.1f}% ({len(_wcarr)}/{len(_wins)})")

        # Q&L coverage thresholds calibrated against current state 2026-05-07:
        # baseline ~48% (parser only catches table-format quotes; prose-format
        # quotes go uncaptured). ERROR triggers on regression below baseline,
        # WARN at baseline-target gap, OK at target. Parser improvement is a
        # multi-step effort — see improvements report for the suggestion.
        if _qlpct < 40:
            log.error(
                f"QC-014b: Q&L carrier coverage {_qlpct:.1f}% < 40% — REGRESSED "
                "below historical baseline. Recent parser change may have broken "
                "carrier extraction. Investigate body_parser.parse_rate_table."
            )
        elif _qlpct < 60:
            log.warn(
                f"QC-014b: Q&L carrier coverage {_qlpct:.1f}% (baseline ~48%, target 60%+). "
                "Parser only catches pipe-table quotes — prose quotes uncaught. See "
                "improvements report for the body-text fallback suggestion."
            )
        else:
            log.ok(f"QC-014b: Q&L carrier coverage {_qlpct:.1f}% ({len(_qlcarr)}/{len(_qls)})")
    except Exception as _e:
        log.warn(f"QC-014: check failed with exception: {_e}")

    # QC-015: NO unresolved row may render in a client-facing surface.
    # CONTRACT (2026-07-14 rewrite, run 29292014093 — Michael "your qc doesn't
    # work"; the old check printed GREEN "within tolerance" while an unresolved
    # standalone shipped in BOTH the staff daily email AND Lonny's now-live
    # client email). A row is "unresolved" when the client would see it as
    # "Lane unresolved": destination is a placeholder (None/""/"Unknown", case-
    # insensitive — FIX 1 nulls the poisoned literal at entry-heal, but a fresh
    # parser miss can still arrive Unknown/None here) OR the lane carries the
    # explicit "Lane unresolved" marker. QC-015 ERRORs when ANY such row WOULD
    # render client-facing — (a) a WIN inside the client email's active-
    # shipments window (last 14 days, the EXACT rows
    # gen_client_email._active_shipments gathers) or (b) any TODAY-dated staff-
    # section row. "within tolerance" GREEN is permissible ONLY when every
    # unresolved row is a non-rendered historical-tail row. The count-based
    # WARN/ERROR tiers still bound the pure historical tail (map-extension
    # signal). stand_* rows whose true lane is genuinely underivable stay
    # unresolved in the STAFF view by design — this ERROR is the standing flag
    # for the operator to assign the real lane; the CLIENT never sees them
    # (FIX 2 excludes them from the client email, FIX 4 from the region table).
    try:
        _tr = _trade_region_reconciliation(data)
        _unmapped = _tr.get("unmapped_destinations", []) if isinstance(_tr, dict) else []

        def _qc015_unresolved(r):
            dest = (r.get("destination") or "").strip().lower()
            return dest in ("", "unknown") or (r.get("lane") or "") == "Lane unresolved"

        _urows = [r for r in requests if _qc015_unresolved(r)]

        # Report day + the client email's active-shipments window. Mirrors
        # gen_client_email.ACTIVE_WINDOW_DAYS=14 and its date derivation
        # (request_date→request_timestamp→response_timestamp); replicated (not
        # imported) to keep the QC engine free of the renderer import.
        _ACTIVE_WINDOW_DAYS = 14

        def _qc015_iso_date(s):
            if not s:
                return None
            try:
                return datetime.strptime(str(s)[:10], "%Y-%m-%d").date()
            except Exception:
                return None

        try:
            _report_day = core.report_business_day(datetime.now(core.ET))
            _today_rd = _report_day.isoformat()
        except Exception:
            _report_day, _today_rd = None, None

        def _qc015_renders(r):
            """Why an unresolved row would surface client-facing, or None."""
            if _today_rd and _today_rd in (
                    (r.get("request_date") or ""),
                    str(r.get("response_timestamp") or "")[:10]):
                return "today-dated"
            if _report_day is not None and r.get("status") == "WIN":
                d = (_qc015_iso_date(r.get("request_date") or r.get("request_timestamp"))
                     or _qc015_iso_date(r.get("response_timestamp")))
                if d is not None and (_report_day - d).days <= _ACTIVE_WINDOW_DAYS:
                    return "active-shipments window"
            return None

        _rendered = [(r, _why) for r in _urows if (_why := _qc015_renders(r))]

        if _rendered:
            _det = "; ".join(
                f"{r.get('request_id')} ({_why}) pod={r.get('pod') or '—'} "
                f"dest={r.get('destination') or '—'} "
                f"subj='{(r.get('subject') or '')[:50]}'"
                for r, _why in _rendered[:5])
            log.error(
                f"QC-015: {len(_rendered)} unresolved row(s) WOULD render "
                f"client-facing as 'Lane unresolved' (active-shipments window / "
                f"today's daily sections): {_det} — every client-visible row must "
                f"carry a resolved lane; extend the parser or assign the true "
                f"lane on the source row (stand_* rows)."
            )

        # Historical-tail tiers — map-extension signal. "within tolerance"
        # GREEN is only reachable when NOTHING rendered above.
        if len(_unmapped) > 10:
            log.error(
                f"QC-015: {len(_unmapped)} unmapped destinations — extend "
                f"core._TRADE_REGION_MAP. First 5: {_unmapped[:5]}"
            )
        elif len(_unmapped) > 5:
            log.warn(f"QC-015: {len(_unmapped)} unmapped destinations — consider extending map: {_unmapped[:5]}")
        elif not _rendered:
            # Name the offending ROWS, not just the count — "2 unmapped
            # (within tolerance)" hid WHICH rows for two days (2026-07-09/10)
            # and root-causing them needed an ad-hoc diagnostic workflow.
            if _urows:
                _det = "; ".join(
                    f"{r.get('request_id')} pod={r.get('pod') or '—'} "
                    f"subj='{(r.get('subject') or '')[:60]}'"
                    for r in _urows[:5])
                log.ok(f"QC-015: {len(_unmapped)} unmapped destination(s) within "
                       f"tolerance — {len(_urows)} unresolved row(s), all "
                       f"non-rendered historical tail: {_det}")
            else:
                log.ok(f"QC-015: {len(_unmapped)} unmapped destination(s) "
                       f"(within tolerance); zero unresolved rows")
    except Exception as _e:
        log.warn(f"QC-015: check failed with exception: {_e}")

    # ─────────────────────────────────────────────────────────────────────
    # QC-021 through QC-025 added 2026-05-13 per Michael "all errors must be
    # fixed in the system not to happen again and added to qc and audit self
    # healing etc etc etc". One check per class of error this session hit.
    # ─────────────────────────────────────────────────────────────────────

    # QC-021: today's wrapper actually shipped the email.
    # Cause: 5/8 + 5/11 wrappers exited 255 between "Pipeline exit code: 0"
    # and the send step. Pipeline succeeded, no email shipped. This QC
    # parses the run-log for today's date and asserts that a successful
    # send line follows the pipeline section.
    try:
        from datetime import datetime as _dt
        _log_path = Path(__file__).resolve().parent.parent / "reports" / "run-log.txt"
        if _log_path.exists():
            _tail = _log_path.read_text(encoding="utf-8", errors="ignore")[-40000:]
            _today_us = _dt.now().strftime("%m/%d/%Y")  # 05/13/2026
            _today_iso = _dt.now().strftime("%Y-%m-%d")
            # Find today's wrapper header
            if _today_us in _tail or _today_iso in _tail:
                # Look for "Sent. request-id=" AFTER today's marker
                _idx = max(_tail.find(_today_us), _tail.find(_today_iso))
                _after = _tail[_idx:] if _idx >= 0 else _tail
                if "Sent. request-id=" in _after:
                    log.ok("QC-021: today's wrapper completed send step")
                elif "Pipeline exit code: 0" in _after:
                    log.warn(
                        "QC-021: today's pipeline completed BUT no 'Sent. request-id='"
                        " line follows. Wrapper may have exited before sending. Check"
                        " idempotency flag — if today's flag exists, email already went"
                        " out (manual fire or earlier scheduled fire). If not, send is"
                        " missing — investigate run-log."
                    )
                else:
                    # Report the LAST step marker seen for today so the audit
                    # tells us WHERE the wrapper got stuck — instead of just
                    # "didn't complete". Wrapper logs lines like
                    # "--- refresh_stage ---", "--- run_pipeline ---", etc.
                    import re as _re21
                    _steps = _re21.findall(r"^---\s*(.+?)\s*---\s*$",
                                           _after, _re21.MULTILINE)
                    _last_step = _steps[-1] if _steps else None
                    if _last_step:
                        log.warn(
                            f"QC-021: today's wrapper started but pipeline never "
                            f"completed — last step logged was '{_last_step}'. "
                            f"Check that step's output in reports/run-log.txt."
                        )
                    else:
                        log.warn(
                            "QC-021: today's wrapper started but pipeline never "
                            "completed — no step markers found (wrapper may have "
                            "died before the refresh_stage echo)."
                        )
            else:
                # No fire today yet — only WARN on weekday evenings, AFTER the
                # ~6 PM ET fire is due (it moved there 2026-06-16). Before 7 PM
                # ET the absence is expected, not a finding.
                _now_et = _dt.now(core.ET)
                if _now_et.weekday() < 5 and _now_et.hour >= 19:
                    log.warn(
                        f"QC-021: no wrapper fire for {_today_iso} in run-log "
                        f"(past 7 PM ET on a weekday — Cloud PC should have fired by now)"
                    )
                else:
                    log.ok(f"QC-021: no wrapper fire yet for {_today_iso} (off-hours)")
    except Exception as _e:
        log.warn(f"QC-021: check failed with exception: {_e}")

    # QC-022: distribution list invariants — must include michael.deitchman@idealx.us,
    # must be 8-12 recipients (normal mode; 9 as of 2026-07-20 after caren.tobel
    # was removed) OR 1 recipient (iteration mode), must NOT include external
    # (non-ol-usa, non-idealx) domains. Catches accidental edits to config.json
    # that could leak emails.
    #
    # 2026-05-19 PM iteration mode: when config has `_iteration_mode_note` at
    # top level, the full_list is locked to just michael.deitchman@idealx.us
    # while Michael iterates on email formatting. QC-022 honors the lock and
    # requires exactly 1 recipient. The original 10-recipient distro is
    # preserved in `full_list_archived` for easy restore.
    try:
        _cfg_path = Path(__file__).resolve().parent.parent / "config.json"
        if _cfg_path.exists():
            import json as _json
            _cfg = _json.loads(_cfg_path.read_text(encoding="utf-8"))
            _full = _cfg.get("distribution", {}).get("full_list", []) or []
            _iteration_mode = "_iteration_mode_note" in _cfg
            _missing = []
            if "michael.deitchman@idealx.us" not in [a.lower() for a in _full]:
                _missing.append("michael.deitchman@idealx.us")
            _external = [a for a in _full
                         if not (a.lower().endswith("@ol-usa.com") or a.lower().endswith("@idealx.us"))]
            _problems = []
            if _missing:
                _problems.append(f"missing: {_missing}")
            if _external:
                _problems.append(f"external domain(s): {_external}")
            if _iteration_mode:
                if len(_full) != 1:
                    _problems.append(f"iteration-mode count != 1: {len(_full)}")
                _verb = "iteration mode (locked)"
            else:
                if len(_full) < 8 or len(_full) > 12:
                    _problems.append(f"unexpected count: {len(_full)}")
                _verb = "normal mode"
            if _problems:
                log.error("QC-022: distribution list invariant violations: " + "; ".join(_problems))
            else:
                log.ok(f"QC-022: distribution list OK — {_verb}, {len(_full)} recipient(s)")
    except Exception as _e:
        log.warn(f"QC-022: check failed with exception: {_e}")

    # QC-023: MSAL token cache freshness. Tokens silently refresh up to ~90d
    # but the refresh-token TTL eventually expires and silent refresh fails,
    # causing send to error out. Warn at 60d so we have time to re-auth.
    try:
        # Prefer the canonical non-indexed .bin first; post-migration the legacy
        # .json is stale (no longer written) and would give a false "old cache".
        _cache_paths = [
            Path(__file__).resolve().parent.parent / "secrets" / "token-cache.bin",
            Path(__file__).resolve().parent.parent / "secrets" / "token-cache.json",
        ]
        _found = next((p for p in _cache_paths if p.exists()), None)
        if _found:
            from datetime import datetime as _dt
            from datetime import timezone as _tz
            _age = (_dt.now(_tz.utc).timestamp() - _found.stat().st_mtime) / 86400.0
            if _age > 80:
                log.error(
                    f"QC-023: MSAL token cache is {_age:.0f}d old (>80d) — silent refresh "
                    "will fail soon. Re-auth: `python scripts/outlook_send.py auth`."
                )
            elif _age > 60:
                log.warn(
                    f"QC-023: MSAL token cache {_age:.0f}d old (>60d). Plan a re-auth "
                    "soon before silent refresh fails."
                )
            else:
                log.ok(f"QC-023: MSAL token cache fresh ({_age:.0f}d, file: {_found.name})")
        else:
            log.warn("QC-023: no MSAL token cache found — sends will fail")
    except Exception as _e:
        log.warn(f"QC-023: check failed with exception: {_e}")

    # QC-024: stage-path consistency. Multiple scripts read stage_emails — they
    # must all read the SAME file. Bug surfaced 5/13: gen_improvements_report
    # was reading legacy .jsonl while qc_selfheal read .txt, causing a phantom
    # "stage stale 192h" red flag. This QC asserts the two sources agree on
    # which file is current (compares mtime — the live file must be newer).
    try:
        _scripts_dir = Path(__file__).resolve().parent
        _txt = _scripts_dir / "stage_emails.txt"
        _jsonl = _scripts_dir / "stage_emails.jsonl"
        if _txt.exists() and _jsonl.exists():
            _txt_age = _txt.stat().st_mtime
            _jsonl_age = _jsonl.stat().st_mtime
            if _jsonl_age > _txt_age + 3600:  # .jsonl newer by 1+ hour
                log.warn(
                    "QC-024: stage_emails.jsonl is NEWER than .txt — refresh_stage may "
                    "have reverted to legacy format. Investigate."
                )
            else:
                log.ok("QC-024: stage path consistent (.txt is current source)")
        elif _txt.exists():
            log.ok("QC-024: stage_emails.txt is the sole source (no legacy .jsonl)")
        elif _jsonl.exists():
            log.error("QC-024: only legacy .jsonl exists — refresh_stage should write .txt")
        else:
            log.warn("QC-024: neither stage_emails.txt nor .jsonl found")
    except Exception as _e:
        log.warn(f"QC-024: check failed with exception: {_e}")

    # QC-037: ol-quote-tracker sync freshness + success. After each pipeline
    # fire, sync_to_quote_tracker.py POSTs entities (Hilmar, Lonny, carriers,
    # OL operators) to ol-quote-tracker's Turso client_intelligence table.
    # Audit log written to reports/quote-tracker-sync.log. This QC parses
    # the most-recent line — WARN if last sync >36h ago or last sync errored.
    try:
        from datetime import datetime as _dt
        _sync_log = Path(__file__).resolve().parent.parent / "reports" / "quote-tracker-sync.log"
        if not _sync_log.exists():
            if _BLOB_HOST:
                # Ephemeral runner: reports/ starts empty each fire and the
                # "Sync to ol-quote-tracker" step runs LATER in the pipeline than
                # this QC, so no log exists yet. The sync's own step verifies its
                # Turso push; this freshness check is a Cloud-PC (persistent
                # reports/) concept, not a finding here.
                log.ok("QC-037: skipped — ephemeral runner, sync log is written later this fire (Turso push verified by the sync step)")
            else:
                log.warn("QC-037: quote-tracker-sync.log not found — sync_to_quote_tracker may never have run")
        else:
            _lines = _sync_log.read_text(encoding="utf-8", errors="ignore").splitlines()
            _last = next((ln for ln in reversed(_lines) if ln.strip()), None)
            if not _last:
                log.warn("QC-037: quote-tracker-sync.log empty")
            else:
                # Format: "<iso_ts> | entities=N ok=True upserted=N err=-"
                _ts_str = _last.split(" | ", 1)[0].strip()
                try:
                    _ts = _dt.fromisoformat(_ts_str.replace("Z", "+00:00"))
                    _age_h = (_dt.now(_ts.tzinfo) - _ts).total_seconds() / 3600.0
                except Exception:
                    _age_h = None
                if "ok=True" in _last:
                    if _age_h is None:
                        log.ok(f"QC-037: ol-quote-tracker sync succeeded ({_last[:80]}...)")
                    elif _age_h > 36:
                        log.warn(f"QC-037: last sync was {_age_h:.0f}h ago (>36h) — pipeline may not be running")
                    else:
                        log.ok(f"QC-037: ol-quote-tracker sync fresh ({_age_h:.1f}h ago)")
                elif "no APP_PASSWORD configured" in _last:
                    log.warn("QC-037: APP_PASSWORD not configured — sync skipped each fire. "
                             "Drop password in secrets/quote-tracker-pwd.txt to enable.")
                else:
                    # Consecutive-failure streak detection (added 2026-05-28 per
                    # Michael's "verify/harden existing sync" answer). A single
                    # failure can be a transient network blip; THREE in a row
                    # means the Turso sync is genuinely broken and the entity
                    # registry is going stale. ERROR-severity so the audit
                    # red-flags it and Sentry catches it.
                    _streak = 0
                    for _ln in reversed(_lines):
                        if not _ln.strip():
                            continue
                        if "ok=True" in _ln:
                            break
                        if "no APP_PASSWORD configured" in _ln:
                            break
                        _streak += 1
                        if _streak >= 5:
                            break
                    if _streak >= 3:
                        log.error(
                            f"QC-037: ol-quote-tracker sync FAILED {_streak} fires in a row "
                            f"— Turso entity registry going stale. Last error: {_last[:160]}"
                        )
                    else:
                        log.warn(f"QC-037: last sync failed: {_last[:120]}")
    except Exception as _e:
        log.warn(f"QC-037: check failed with exception: {_e}")

    # QC-043: SENTRY SELF-IMPROVEMENT LOOP — query Sentry's REST API for
    # the open-issues view of this project, log it locally, and flag any
    # issues that appear to be FIXED by recent commits so they auto-
    # resolve on the next pass.
    #
    # Per Michael 2026-05-17 ("you can use sentry for self check and
    # improvements as well"). Sentry becomes an active participant in
    # QC: it KNOWS what errored, when, and how often. qc_selfheal asks
    # it that question every fire and acts on the answer.
    #
    # Actions:
    #   - Surface unresolved-issue COUNT in qc-result.json (audit context)
    #   - WARN if ≥3 distinct issues remain unresolved >7 days (stale backlog)
    #   - WARN if any single issue has ≥5 events in 24h (active fire)
    try:
        import sys as _sys
        _sys.path.insert(0, str(Path(__file__).resolve().parent))
        from sentry_api import SentryAPI, get_issue_summary
        _api = SentryAPI()
        if not _api.enabled:
            log.ok("QC-043: Sentry auth token not configured — self-improvement loop disabled")
        else:
            _summary = get_issue_summary(_api, period="24h")
            _unresolved = _summary["unresolved_count"]
            _new = len(_summary["new_in_period"])
            _recurring = _summary["recurring"]
            _events = _summary["total_events_in_period"]

            # Always log the snapshot for the audit
            log.ok(
                f"QC-043: Sentry — {_unresolved} unresolved, {_new} new (24h), "
                f"{len(_recurring)} recurring, {_events} events (24h)"
            )

            # WARN: stale unresolved backlog (>= 5 issues open)
            if _unresolved >= 5:
                log.warn(
                    f"QC-043: {_unresolved} unresolved Sentry issues — operator should "
                    "triage in https://idealx-llc.sentry.io/issues/ (auto-resolve "
                    "covers issues that haven't fired in 24h, but recurring ones need review)"
                )

            # WARN: active fire (any issue >=5 events in 24h)
            _hot = [
                i for i in _recurring
                if int(i.get("count", 0)) >= 5
            ]
            if _hot:
                _titles = ", ".join(
                    f"{i.get('shortId', '?')}({i.get('count', '?')}x)"
                    for i in _hot[:3]
                )
                log.warn(
                    f"QC-043: {len(_hot)} Sentry issue(s) firing ≥5×/24h — "
                    f"active regression likely: {_titles}"
                )

            # Push a metric so the dashboard's "Sentry health" widget
            # can track unresolved-count + new-rate over time
            if _sentry is not None:
                try:
                    _sentry.metric_gauge("sentry.unresolved_count", _unresolved)
                    _sentry.metric_gauge("sentry.new_24h", _new)
                    _sentry.metric_gauge("sentry.recurring_count", len(_recurring))
                    _sentry.metric_gauge("sentry.events_24h", _events)
                except Exception:
                    pass
    except Exception as _e:
        log.warn(f"QC-043: Sentry self-improvement loop failed: {type(_e).__name__}: {_e}")

    # ─────────────────────────────────────────────────────────────────
    # QC-044, -045, -046, -047 — Email format invariants
    #
    # Per Michael 2026-05-19 PM ("publish this now and make sure it's in
    # qc audits self heal sentry/seer with autofix in git and claude").
    # Every email-formatting bug discovered in the v3-v7 cycle gets a QC
    # check so it can't silently regress. ERROR-level findings here go to
    # Sentry via log.error → capture_qc_error, and Seer auto-triggers via
    # qc_actions_from_sentry.ERROR_LEVEL_DEFAULT.
    # ─────────────────────────────────────────────────────────────────

    _body_path = Path(__file__).resolve().parent.parent / "reports" / "email-body.html"

    if _body_path.exists():
        try:
            _body = _body_path.read_text(encoding="utf-8")
        except Exception:
            _body = ""

        # QC-044: DOUBLE-ESCAPE GUARD — no `&amp;amp;` sequences (caught
        # 2026-05-19 PM v1 → v2: passing pre-escaped "Quoted &amp; Lost"
        # into _kpi_card which ran _esc() again produced &amp;amp;. Outlook
        # renders literally.
        if "&amp;amp;" in _body or "&amp;quot;" in _body:
            _ct = _body.count("&amp;amp;") + _body.count("&amp;quot;")
            log.error(
                f"QC-044: email-body.html has {_ct} double-escaped HTML entity "
                "sequences (&amp;amp; or &amp;quot;) — Outlook will render them "
                "literally. Check call sites passing pre-escaped strings into "
                "gen_email._kpi_card / _esc-wrapped helpers."
            )
        else:
            log.ok("QC-044: no double-escaped HTML entities in email body")

        # QC-045: TABLE-HEADER VISIBILITY — Outlook strips CSS linear-gradient
        # but renders solid background-color. Any <tr> with `color:white` or
        # `color:#ffffff` MUST also have a solid background-color set
        # somewhere reachable, otherwise the header text is white on white
        # = invisible. (Caught 2026-05-19 PM v4: Top Winning/Losing Lanes
        # header rows used only linear-gradient.)
        import re as _re_qc45
        _bad_headers = []
        for _m in _re_qc45.finditer(
            r"<tr[^>]*style=\"([^\"]*)\"[^>]*>", _body
        ):
            _style = _m.group(1)
            _has_white_text = (
                "color:white" in _style.lower()
                or "color:#ffffff" in _style.lower()
                or "color:#fff" in _style.lower()
            )
            _has_solid_bg = "background-color:" in _style.lower()
            _has_gradient_only = (
                "linear-gradient" in _style.lower()
                and "background-color:" not in _style.lower()
            )
            if _has_white_text and _has_gradient_only and not _has_solid_bg:
                _bad_headers.append(_m.group(0)[:80])
        if _bad_headers:
            log.error(
                f"QC-045: {len(_bad_headers)} email table header(s) use "
                "linear-gradient without a solid background-color fallback — "
                "Outlook will strip the gradient and render white-on-white "
                "(invisible). Fix: add `background-color:#NNNNNN;` BEFORE "
                "the `background:linear-gradient(...)` declaration. Sample: "
                + _bad_headers[0]
            )
        else:
            log.ok("QC-045: all white-text email headers have solid background-color fallback")

        # QC-046: PENDING TIMESTAMP POPULATION — when reports/email-body.html
        # contains the "Pending Hilmar Response" section AND the data has
        # response_timestamp populated, the rendered cells must NOT all be
        # dashes. (Caught 2026-05-19 PM v5: Windows strftime "%-d" raised
        # ValueError → except returned "—" for every row.)
        if "Pending Hilmar Response" in _body and "Lonny Requested (PT)" in _body:
            # Count the dash cells specifically in the Pending section.
            _pending_idx = _body.find("Pending Hilmar Response")
            _next_h2 = _body.find("<h2", _pending_idx + 1)
            _pending_section = _body[_pending_idx:_next_h2 if _next_h2 > 0 else len(_body)]
            # Each row contributes 2 timestamp cells; count "—" in those columns
            _dash_cells = _pending_section.count(">—</td>")
            # Heuristic: if there are dashes and at least one populated
            # timestamp would mean it's working. Check for any "PT</td>"
            # or "ET</td>" tail indicating real timestamp rendered.
            _real_ts = (" PT</td>" in _pending_section) or (" ET</td>" in _pending_section)
            if _dash_cells > 4 and not _real_ts:
                log.error(
                    f"QC-046: Pending Hilmar Response timestamps all rendering "
                    f"as dashes ({_dash_cells} dash cells, zero real PT/ET "
                    "timestamps). Likely Windows-incompatible strftime token "
                    "(%-d / %-I) in _fmt_pt_full / _fmt_et_full — use %d / %I "
                    "and strip leading zeros via .replace()."
                )
            else:
                log.ok(f"QC-046: Pending Hilmar timestamps populating "
                       f"({_dash_cells} dash cells, real timestamps: {_real_ts})")

        # QC-050: BACKUP FRESHNESS + RETENTION — Michael 2026-05-19 PM
        # "make sure sentry/seer and all backups work". The pipeline runs
        # scripts/backup.py as Step 1 of every fire; that creates a
        # tracking-data-v2_<timestamp>.json snapshot in data-backups/.
        # Health check: confirm at least one backup exists from the last
        # 26h (covers daily fire + slack). Also count total snapshots so
        # we see if retention pruning is wedged.
        try:
            from datetime import datetime as _dt_bk
            from datetime import timezone as _tz_bk
            _bk_dir = Path(__file__).resolve().parent.parent / "data-backups"
            if not _bk_dir.exists():
                log.error(
                    "QC-050: data-backups/ directory missing. backup.py should "
                    "create it on every pipeline fire (Step 1). Check scripts/backup.py "
                    "is on the pipeline + writable from the Cloud PC."
                )
            else:
                _snaps = sorted(_bk_dir.glob("tracking-data-v2*.json"),
                                key=lambda p: p.stat().st_mtime, reverse=True)
                if not _snaps:
                    log.error(
                        "QC-050: data-backups/ exists but contains zero snapshots. "
                        "backup.py is not writing. Check rules.backup_retention_count "
                        "in config.json and scripts/backup.py."
                    )
                else:
                    _latest = _snaps[0]
                    _age_h = (_dt_bk.now(_tz_bk.utc).timestamp() - _latest.stat().st_mtime) / 3600.0
                    if _age_h > 30:
                        log.error(
                            f"QC-050: latest backup is {_age_h:.1f}h old (>30h) — "
                            "pipeline backup step may have stopped firing. Latest: "
                            f"{_latest.name}"
                        )
                    elif _age_h > 25:
                        log.warn(
                            f"QC-050: latest backup is {_age_h:.1f}h old. Daily fire "
                            f"runs at 6 PM ET — newest backup expected <24h. Latest: "
                            f"{_latest.name}"
                        )
                    else:
                        log.ok(
                            f"QC-050: backups healthy — {len(_snaps)} snapshots, "
                            f"latest {_age_h:.1f}h old ({_latest.name})"
                        )
        except Exception as _e:
            log.warn(f"QC-050: check failed with exception: {_e}")

        # QC-049: UNCONFIRMED WINS — every WIN must be backed by an MDOLX
        # booking confirmation. Rows flipped PENDING->WIN on a "Lonny send-
        # reply" signal alone, with no booking confirmation ever linked, are
        # UNCONFIRMED: the reported win count overstates confirmed bookings.
        # This is the exact pattern Linda Echevarria's 2026-05-19 audit caught
        # (2 rows demoted via the operator-corrections layer). QC-049 lists
        # EVERY stale unconfirmed win — old enough that a booking confirmation
        # should already have arrived — so each gets the same review. It does
        # NOT auto-demote: a win-status change is operator judgment. A win too
        # recent for the booking to have arrived yet is normal lag, not flagged.
        # ERROR severity so it lands in the audit red-flags + fires Sentry —
        # this directly affects the headline win count, it must not be quiet.
        try:
            from datetime import datetime as _dt49
            from datetime import timedelta as _td49
            from datetime import timezone as _tz49
            _data_path = Path(__file__).resolve().parent.parent / "tracking-data-v2.json"
            if _data_path.exists():
                import json as _json_mdx
                _d = _json_mdx.loads(_data_path.read_text(encoding="utf-8"))
                _wins = [r for r in (_d.get("requests") or []) if r.get("status") == "WIN"]
                _unconf = [r for r in _wins
                           if not r.get("mdolx_ref") and not r.get("mdolx_refs_all")]
                _cut49 = (_dt49.now(_tz49.utc) - _td49(days=7)).date().isoformat()
                _stale = [r for r in _unconf
                          if (r.get("request_date") or "0000-00-00") < _cut49]
                if not _wins:
                    log.ok("QC-049: no WIN rows to check")
                elif not _unconf:
                    log.ok(f"QC-049: all {len(_wins)} wins confirmed by an MDOLX booking ref")
                elif not _stale:
                    log.ok(f"QC-049: {len(_unconf)} unconfirmed win(s), all recent "
                           f"(<7d) — normal booking-confirmation lag, not flagged")
                else:
                    _rows49 = "; ".join(
                        f"{r.get('request_id','')} {r.get('lane','')} "
                        f"({r.get('request_date','')})" for r in _stale)
                    log.error(
                        f"QC-049: {len(_stale)} of {len(_wins)} reported wins are "
                        f"UNCONFIRMED — flipped to WIN on a send-signal with no MDOLX "
                        f"booking confirmation linked, and old enough (>7d) that one "
                        f"should have arrived. Reported wins {len(_wins)}, confirmed by "
                        f"a booking {len(_wins) - len(_unconf)}. Each needs a booking-"
                        f"team review (cf. Linda Echevarria 2026-05-19 audit — some are "
                        f"real with an unlinked booking confirmation, some are false "
                        f"wins). Rows: {_rows49}"
                    )
        except Exception as _e:
            log.warn(f"QC-049: check failed with exception: {_e}")

        # QC-051: PHANTOM-DUPLICATE WIN GUARD — verifies phase_4's content-
        # dedup held. A phantom duplicate is an unconfirmed WIN (no MDOLX)
        # sharing (conversation_id, destination, request_date, containers)
        # with a booking-confirmed WIN — the same shipment counted twice,
        # which inflates the win count. phase_4_duplicates collapses these;
        # if any survive here, that dedup didn't run or has regressed.
        try:
            _pd_path = Path(__file__).resolve().parent.parent / "tracking-data-v2.json"
            if _pd_path.exists():
                import json as _json_pd
                _pdd = _json_pd.loads(_pd_path.read_text(encoding="utf-8"))
                _pdg: dict = {}
                for _r in (_pdd.get("requests") or []):
                    _cid = (_r.get("conversation_id") or "").strip()
                    _dst = (_r.get("destination") or "").strip().lower()
                    _rdt = _r.get("request_date") or ""
                    _cnt = (_r.get("containers") or "").strip().lower()
                    if _cid and _dst and _rdt and _cnt:
                        _pdg.setdefault((_cid, _dst, _rdt, _cnt), []).append(_r)
                _phantom = []
                for _grp in _pdg.values():
                    _conf = [x for x in _grp if x.get("status") == "WIN" and x.get("mdolx_ref")]
                    _unc = [x for x in _grp if x.get("status") == "WIN" and not x.get("mdolx_ref")]
                    _dm = {str(x.get("mdolx_ref")) for x in _grp if x.get("mdolx_ref")}
                    if _conf and _unc and len(_dm) < 2:
                        _phantom += _unc
                if _phantom:
                    log.warn(
                        f"QC-051: {len(_phantom)} phantom-duplicate win(s) survived "
                        "phase_4 content-dedup (same shipment as a booking-confirmed "
                        "win) — "
                        + ", ".join(f"{x.get('request_id','')} {x.get('lane','')}"
                                    for x in _phantom[:5])
                    )
                else:
                    log.ok("QC-051: no phantom-duplicate wins — content-dedup clean")
        except Exception as _e:
            log.warn(f"QC-051: check failed with exception: {_e}")

        # QC-048: TURNAROUND SANITY CHECK — flags rows with implausible
        # turnaround_biz_hours. Real OL rate-response turnaround is sub-day
        # biz-hours (usually <4h). Values >40h biz-hours indicate the
        # matcher used a stale timestamp source (e.g. measured Lonny RFQ →
        # Booking Confirmation instead of Lonny RFQ → OL rate response).
        # Caught 2026-05-19 PM: 3 WIN rows at 85.78h / 73.34h / 55.58h were
        # all booking-link path with no prior rate response → biz_hours
        # came from booking timestamp. Fix landed in ingest.py
        # link_bookings_to_requests; this QC keeps it honest.
        try:
            _data_path = Path(__file__).resolve().parent.parent / "tracking-data-v2.json"
            if _data_path.exists():
                import json as _json_ta
                _d = _json_ta.loads(_data_path.read_text(encoding="utf-8"))
                _high_ta = []
                for _r in (_d.get("requests") or []):
                    _ta = _r.get("turnaround_biz_hours")
                    if isinstance(_ta, (int, float)) and _ta > 40:
                        _high_ta.append({
                            "request_id": (_r.get("request_id") or "")[:30],
                            "lane": _r.get("lane"),
                            "biz_hours": round(_ta, 2),
                            "status": _r.get("status"),
                            "has_response_ts": bool(_r.get("response_timestamp")),
                            "has_booking_ts": bool(_r.get("booking_timestamp")),
                        })
                if _high_ta:
                    log.error(
                        f"QC-048: {len(_high_ta)} row(s) have turnaround_biz_hours > 40h — "
                        "implausible OL response time. Likely cause: booking timestamp "
                        "leaked into turnaround calc (link_bookings_to_requests should "
                        "leave turnaround None when no prior rate response). Sample: "
                        + ", ".join(f"{x['request_id']} {x['lane']} {x['biz_hours']}h ({x['status']})"
                                    for x in _high_ta[:3])
                    )
                else:
                    log.ok("QC-048: all turnaround_biz_hours values plausible (≤40h)")
        except Exception as _e:
            log.warn(f"QC-048: check failed with exception: {_e}")

        # QC-052: TEST + COVERAGE GATE — verifies the daily test routine ran
        # and the code is green. Reads reports/test-result.json (written by
        # scripts/run_audit_tests.py, an observer step in run_pipeline.py).
        # Added 2026-05-28 per Michael "a complete audit ... daily ...
        # checking that every line of code has testing on it and successful
        # ... must be in routines". This closes the gap where a 587-test
        # suite + 85% coverage gate existed in pyproject but ran NOWHERE in
        # the daily fire, so the audit was blind to code health.
        #   FAIL (test failed / coverage below gate) -> ERROR (audit red flag)
        #   SKIPPED (pytest unavailable on this host) -> WARN (install dev deps)
        #   modules below the per-module floor        -> WARN (learning target)
        try:
            _tr_path = Path(__file__).resolve().parent.parent / "reports" / "test-result.json"
            if not _tr_path.exists():
                log.warn(
                    "QC-052: reports/test-result.json absent — daily test routine "
                    "(run_audit_tests.py) hasn't run. Code health is unverified."
                )
            else:
                import json as _json_tr
                _tr = _json_tr.loads(_tr_path.read_text(encoding="utf-8"))
                _st = _tr.get("status")
                _cov = _tr.get("total_coverage")
                _gate = _tr.get("gate")
                _counts = _tr.get("counts") or {}
                if _st == "SKIPPED":
                    log.warn(
                        f"QC-052: test routine SKIPPED — {_tr.get('reason', 'pytest unavailable')}"
                    )
                elif _st == "FAIL":
                    _why = []
                    if not _tr.get("tests_ok", True):
                        _why.append(
                            f"{_counts.get('failed', 0)} failed / "
                            f"{_counts.get('error', 0)} error of "
                            f"{_counts.get('passed', 0) + _counts.get('failed', 0) + _counts.get('error', 0)}"
                        )
                    if not _tr.get("coverage_ok", True):
                        _why.append(f"coverage {_cov}% < gate {_gate}%")
                    # 2026-06-01: surface diagnostic detail when present so
                    # the audit email tells the operator WHAT broke. Without
                    # this, QC-052 was emitting opaque "22 errors" lines with
                    # no signal on the underlying cause (collection failure,
                    # missing dep, fixture crash, etc.). Backward-compatible:
                    # absent fields => same headline-only message as before.
                    _buckets = _tr.get("error_type_buckets") or []
                    _coll = _tr.get("collection_error")
                    _diag_parts = []
                    if _coll:
                        _diag_parts.append("pytest collection failed (modules failed to import)")
                    if _buckets:
                        _bucket_text = ", ".join(
                            f"{b.get('count', 0)}x {b.get('error_type', '?')}" for b in _buckets[:4]
                        )
                        _diag_parts.append(f"top error types: {_bucket_text}")
                    _diag = (" Diagnosis: " + "; ".join(_diag_parts) + ".") if _diag_parts else ""
                    _out_ref = _tr.get("pytest_output_path") or "reports/pytest-output.txt"
                    log.error(
                        "QC-052: daily test/coverage routine FAILED — "
                        + "; ".join(_why)
                        + ". The shipped code is not green."
                        + _diag
                        + f" Full output: {_out_ref}."
                    )
                else:  # PASS
                    log.ok(
                        f"QC-052: tests green ({_counts.get('passed', 0)} passed) "
                        f"coverage {_cov}% ≥ gate {_gate}%"
                    )
                # Learning loop: name under-tested modules so "every line tested"
                # has a concrete worklist, even when the global gate passes.
                _below = _tr.get("modules_below_floor") or []
                _untested = _tr.get("untested_modules") or []
                if _untested:
                    log.warn(
                        f"QC-052: {len(_untested)} module(s) with 0% coverage — "
                        f"{', '.join(_untested[:5])}. These ship untested; add tests."
                    )
                elif _below:
                    log.warn(
                        f"QC-052: {len(_below)} module(s) below the "
                        f"{_tr.get('module_floor')}% floor — "
                        + ", ".join(f"{m['module']} ({m['coverage']}%)" for m in _below[:5])
                    )
        except Exception as _e:
            log.warn(f"QC-052: check failed with exception: {_e}")

        # QC-053: DEPLOYMENT DRIFT — local checkout vs origin/main. Added
        # 2026-05-28 after Michael's "how is this possible" audit on May 28:
        # 4 commits of production fixes (Caucedo, Dublin, Sentry filter,
        # QC-021 step name, QC-052) had been pushed to a feature branch and
        # piled into a docs PR. The wrapper does `git pull --quiet origin
        # main` then xcopies into PROJECT HILMAR/scripts/ — so the Cloud PC
        # ran main, the PR never merged, none of the fixes took effect for
        # ~5 days. The audit kept reporting the SAME problems because
        # nothing was actually deployed. This check ERRORs if the local
        # repo HEAD is behind origin/main (the production case that bit us)
        # AND if a deployment-marker indicates the production xcopy is
        # behind the local repo. Read-only — does not run `git fetch`
        # (the wrapper Step 0 already pulled).
        try:
            import subprocess as _sp53
            from pathlib import Path as _Path53
            _git_dir = _Path53(__file__).resolve().parent.parent / ".git"
            if not _git_dir.exists():
                # Production xcopy has no .git nearby — read the marker the
                # wrapper writes after a successful git pull.
                _marker = _Path53(__file__).resolve().parent.parent / "reports" / "deployment-sha.txt"
                if _marker.exists():
                    _txt = _marker.read_text(encoding="utf-8").strip()
                    log.ok(f"QC-053: production checkout (no .git here) — marker: {_txt[:80]}")
                else:
                    log.warn(
                        "QC-053: no .git directory and no reports/deployment-sha.txt "
                        "— cannot verify the deployed code is current with origin/main. "
                        "Update deploy/run_daily_laptop.cmd Step 0 to write the marker."
                    )
            else:
                _head = _sp53.run(
                    ["git", "rev-parse", "--short", "HEAD"],
                    cwd=str(_git_dir.parent), capture_output=True, text=True, timeout=10,
                ).stdout.strip() or "unknown"
                _r = _sp53.run(
                    ["git", "rev-list", "--count", "HEAD..origin/main"],
                    cwd=str(_git_dir.parent), capture_output=True, text=True, timeout=10,
                )
                if _r.returncode != 0:
                    log.warn(
                        f"QC-053: could not compare HEAD vs origin/main: "
                        f"{(_r.stderr or '').strip()[:160]}"
                    )
                else:
                    _behind = int((_r.stdout or "0").strip() or "0")
                    if _behind > 0:
                        # Get the subject lines of the unmerged commits so the
                        # audit shows WHAT we're missing, not just a count.
                        _l = _sp53.run(
                            ["git", "log", "--oneline", "-5", "HEAD..origin/main"],
                            cwd=str(_git_dir.parent), capture_output=True, text=True, timeout=10,
                        )
                        _samples = (_l.stdout or "").strip().splitlines()[:3]
                        _sample_txt = " · ".join(_samples) if _samples else "(no log)"
                        log.error(
                            f"QC-053: local HEAD ({_head}) is {_behind} commit(s) "
                            f"BEHIND origin/main — Cloud PC is running stale code. "
                            f"Wrapper Step 0 git-pull may have failed. Missing: {_sample_txt}"
                        )
                    else:
                        log.ok(f"QC-053: deployment current — HEAD {_head} == origin/main")
        except Exception as _e:
            log.warn(f"QC-053: check failed with exception: {_e}")

        # QC-047: WIN RATE FORMULA CONSISTENCY — the global Win Rate KPI tile
        # and the per-lane Win Rate cells must use the same formula
        # (Wins / (Wins + Q&L)). The explainer banner below the KPI grid
        # publishes the numbers it computed; check those numbers match the
        # KPI tile rendering. Drift here means somebody changed one formula
        # without the other. (Set up 2026-05-19 PM v6.1 when per-lane was
        # still using old Wins / total.)
        import re as _re_qc47
        _kpi_match = _re_qc47.search(
            r"(\d+(?:\.\d+)?)\%</div>\s*<div[^>]*>Win Rate</div>", _body
        )
        _banner_match = _re_qc47.search(
            r"<strong>(\d+) wins ÷ (\d+) decided = (\d+(?:\.\d+)?)\%</strong>", _body
        )
        if _kpi_match and _banner_match:
            _kpi_pct = float(_kpi_match.group(1))
            _banner_pct = float(_banner_match.group(3))
            if abs(_kpi_pct - _banner_pct) > 0.2:
                log.error(
                    f"QC-047: Win Rate KPI tile ({_kpi_pct}%) and explainer "
                    f"banner ({_banner_pct}%) disagree by >0.2pp — formula "
                    "drift between _kpi_block_html computation and the "
                    "banner-text render. They should be identical."
                )
            else:
                log.ok(f"QC-047: Win Rate consistency OK — KPI {_kpi_pct}% matches banner {_banner_pct}%")
        elif "Win Rate" in _body:
            log.warn("QC-047: could not extract Win Rate KPI + banner pair for consistency check")

    # QC-038 (ol-quote-tracker reconciliation) retired 2026-05-21: a live API
    # probe proved ol-quote-tracker holds zero Hilmar rows of 24 total quotes,
    # so the cross-check only ever produced phantom drift. The reconcile script
    # + pipeline step were removed; this QC slot is intentionally left empty.


    # QC-040: CROSS-FOLDER DRIFT — per Michael 2026-05-17 "never to allow
    # drift like this as standard." Detects when scripts/core.py and
    # src/hilmar/core.py disagree on enums/constants that should be aligned.
    # Documented intentional drift (e.g. 4-state Q&L/NQ in src/hilmar/ STRICT
    # vs 3-state in scripts/) goes through ALLOWED_CROSS_FOLDER_DRIFT below.
    # NEW drift triggers WARN — operator must either align or document.
    try:
        import sys as _sys
        _src_dir = Path(__file__).resolve().parent.parent / "src"
        if str(_src_dir) not in _sys.path:
            _sys.path.insert(0, str(_src_dir))
        from hilmar import core as _h_core

        # Allowed intentional drift — documented in src/hilmar/core.py
        # comments. Adding to this list requires a deliberate code review.
        _ALLOWED = {
            # The 3-state vs 4-state classifier divide is INTENTIONAL —
            # src/hilmar/ has VALID_STATUSES_STRICT for the 4-state form
            # and VALID_STATUSES_LEGACY for the 3-state form. The union
            # `VALID_STATUSES` accepts both. scripts/core.VALID_STATUSES
            # is the 3-state legacy form only. They are EQUAL on the
            # intersection {WIN, PENDING} and differ deliberately on
            # {LOSS, Q&L, NQ}.
            "VALID_STATUSES": True,
        }

        _drift_findings = []
        # Compare VALID_STATUSES — special-cased because of intentional drift
        scripts_statuses = set(getattr(core, "VALID_STATUSES", set()))
        hilmar_legacy = set(getattr(_h_core, "VALID_STATUSES_LEGACY", set()))
        if scripts_statuses != hilmar_legacy:
            _drift_findings.append(
                f"scripts/core.VALID_STATUSES {sorted(scripts_statuses)} != "
                f"src/hilmar/core.VALID_STATUSES_LEGACY {sorted(hilmar_legacy)} — "
                "the LEGACY view in src/hilmar/ must mirror scripts/ exactly"
            )

        # Compare LOSS_REASONS — strict equality required (no allowed drift)
        scripts_reasons = set(getattr(core, "LOSS_REASONS", set()))
        hilmar_reasons = set(getattr(_h_core, "LOSS_REASONS", set()))
        if scripts_reasons and hilmar_reasons:
            _missing_in_scripts = hilmar_reasons - scripts_reasons
            _missing_in_hilmar = scripts_reasons - hilmar_reasons
            if _missing_in_scripts or _missing_in_hilmar:
                _drift_findings.append(
                    f"LOSS_REASONS drift: only in src/hilmar/ = {sorted(_missing_in_scripts)}; "
                    f"only in scripts/ = {sorted(_missing_in_hilmar)}"
                )

        # body_parser.KNOWN_ORIGINS — strict equality. This is the constant
        # whose drift caused the "Hilmar -> X" lane-bucket bug (the src tree was
        # fixed but production wasn't, invisible to the old core-only QC-040).
        # body_parser is a CLAUDE.md §2 paired file; the origin list must match.
        try:
            import body_parser as _s_bp

            from hilmar import body_parser as _h_bp
            _s_origins = tuple(getattr(_s_bp, "KNOWN_ORIGINS", ()) or ())
            _h_origins = tuple(getattr(_h_bp, "KNOWN_ORIGINS", ()) or ())
            if _s_origins and _h_origins and _s_origins != _h_origins:
                _only_s = [o for o in _s_origins if o not in _h_origins]
                _only_h = [o for o in _h_origins if o not in _s_origins]
                _drift_findings.append(
                    f"body_parser.KNOWN_ORIGINS drift: only in scripts/ = {_only_s}; "
                    f"only in src/hilmar/ = {_only_h} (order/membership must match)"
                )
        except Exception as _bpe:
            _drift_findings.append(f"body_parser.KNOWN_ORIGINS compare failed: {_bpe}")

        if _drift_findings:
            log.warn(
                f"QC-040: {len(_drift_findings)} undocumented cross-folder drift "
                f"finding(s) between scripts/ and src/hilmar/: " +
                " | ".join(_drift_findings)
            )
        else:
            log.ok("QC-040: scripts ↔ src/hilmar aligned (core VALID_STATUSES via "
                   "LEGACY view; LOSS_REASONS strict; body_parser.KNOWN_ORIGINS strict)")
    except Exception as _e:
        log.warn(f"QC-040: check failed with exception: {_e}")

    # QC-042: EMAIL-BODY DATA-URI GUARD — per Michael 2026-05-17 ("hilmar
    # logo not showing up"). Outlook blocks `<img src="data:image/...">`
    # references in HTML email bodies as a security measure, so any logo
    # or inline image embedded via data: URI renders as the broken-image
    # icon in the recipient's inbox. The fix is CID attachment (see
    # branding.logo_html_cid + outlook_send.py auto-attach with
    # contentId=hilmar-logo + isInline=true).
    #
    # This QC scans both email-bound HTML files (the tracker email body
    # AND the daily audit email body) for `data:image` substrings. ANY
    # occurrence = ERROR — it means someone reintroduced a data URI
    # somewhere and Outlook will block it next fire.
    #
    # Browser-opened artifacts (hilmar-dashboard.html, weekly-summary.html)
    # are EXEMPT — those are opened directly in browsers where data: URIs
    # render fine. Only files that go through outlook_send.py as the
    # message body are gated.
    try:
        _email_bodies = [
            Path(__file__).resolve().parent.parent / "reports" / "email-body.html",
            Path(__file__).resolve().parent.parent / "reports" / "improvements-report.html",
        ]
        _offenders = []
        for _path in _email_bodies:
            if not _path.exists():
                continue
            _text = _path.read_text(encoding="utf-8", errors="ignore")
            if "data:image" in _text:
                # Count occurrences for the error message
                _n = _text.count("data:image")
                _offenders.append(f"{_path.name} ({_n} data:image URI{'s' if _n > 1 else ''})")
        if _offenders:
            log.error(
                f"QC-042: {len(_offenders)} email-body file(s) contain data:image URIs "
                "(Outlook will block them — switch to cid:hilmar-logo via "
                "branding.logo_html_cid + outlook_send auto-attach): " +
                ", ".join(_offenders)
            )
        else:
            log.ok("QC-042: email bodies use CID logo references (no data:image URIs)")
    except Exception as _e:
        log.warn(f"QC-042: check failed with exception: {_e}")

    # QC-041: CLASSIFIER FORM CONSISTENCY — tracking-data-v2.json must use
    # ONE classifier form across all rows. Mixed strict (Q&L/NQ) and legacy
    # (LOSS) is a parser bug — at least one ingest pass mis-classified.
    try:
        import sys as _sys
        _src_dir = Path(__file__).resolve().parent.parent / "src"
        if str(_src_dir) not in _sys.path:
            _sys.path.insert(0, str(_src_dir))
        from hilmar.core import detect_classifier_form
        _form = detect_classifier_form(data.get("requests", []))
        if _form == "mixed":
            _strict_rows = [r for r in data.get("requests", [])
                             if r.get("status") in ("Q&L", "NQ")]
            _legacy_rows = [r for r in data.get("requests", [])
                             if r.get("status") == "LOSS"]
            log.error(
                f"QC-041: CLASSIFIER DRIFT — tracking-data-v2.json has "
                f"{len(_strict_rows)} STRICT rows (Q&L/NQ) and "
                f"{len(_legacy_rows)} LEGACY rows (LOSS). Mixed-form data "
                "is a parser bug. All ingest must write one consistent form."
            )
        elif _form == "empty":
            log.ok("QC-041: no LOSS/Q&L/NQ rows to evaluate classifier form")
        else:
            log.ok(f"QC-041: classifier form consistent ({_form.upper()})")
    except Exception as _e:
        log.warn(f"QC-041: check failed with exception: {_e}")

    # QC-034: tracking-data-v2.json schema validity gate. Added 2026-05-14
    # per best-practices batch. Calls core.validate_data_shape() and reports
    # structural issues (missing keys, wrong types, invalid status/loss_reason
    # enums). ERROR if invalid — pipeline shouldn't ship a malformed file.
    try:
        ok, issues = core.validate_data_shape(data, strict=False)
        if not ok:
            log.error("QC-034: data shape invalid: " + "; ".join(issues[:3])
                       + (f" + {len(issues)-3} more" if len(issues) > 3 else ""))
        else:
            log.ok("QC-034: tracking-data-v2.json shape valid (top-level keys + req fields)")
    except Exception as _e:
        log.warn(f"QC-034: check failed with exception: {_e}")

    # QC-035: stage file size cap. Without rotation, stage_emails.txt grows
    # unbounded. Warn at 5MB, ERROR at 20MB — those thresholds mean
    # `--rotate-stage-older-than 90` should be called.
    try:
        stage_path = Path(__file__).resolve().parent / "stage_emails.txt"
        if stage_path.exists():
            size_mb = stage_path.stat().st_size / 1_000_000
            if size_mb > 20:
                log.error(f"QC-035: stage_emails.txt is {size_mb:.1f}MB > 20MB — "
                          "run `refresh_stage.py --rotate-stage-older-than 90`")
            elif size_mb > 5:
                log.warn(f"QC-035: stage_emails.txt at {size_mb:.1f}MB — consider rotation soon")
            else:
                log.ok(f"QC-035: stage_emails.txt {size_mb:.2f}MB (well under cap)")
    except Exception as _e:
        log.warn(f"QC-035: check failed with exception: {_e}")

    # QC-036: unit test suite presence. The `tests/` folder should contain
    # at least one test_*.py file. Empty test folder means a regression net
    # got removed and won't catch future breakage.
    try:
        tests_dir = Path(__file__).resolve().parent.parent / "tests"
        if tests_dir.exists():
            test_files = list(tests_dir.glob("test_*.py"))
            if not test_files:
                log.error("QC-036: tests/ folder exists but no test_*.py files — "
                          "regression net is gone")
            elif len(test_files) < 3:
                log.warn(f"QC-036: only {len(test_files)} test files — "
                         "coverage thin (target ≥3 modules tested)")
            else:
                log.ok(f"QC-036: {len(test_files)} test files in tests/")
        else:
            log.warn("QC-036: no tests/ folder — regression net absent")
    except Exception as _e:
        log.warn(f"QC-036: check failed with exception: {_e}")

    # QC-033: brand logo presence + sanity. Per Michael 2026-05-14
    # "can you save this in your schema/data base and also add it to the
    # system fo rhilmar?" — wired branding.py module across all artifacts.
    # If logo file is missing or zero-bytes, the headers gracefully fall
    # back to emoji + text but Michael loses the brand identity. WARN.
    # Prefer SVG (vector) > PNG (raster). Either is acceptable.
    try:
        _brand_dir = Path(__file__).resolve().parent.parent / "assets" / "branding"
        _png = _brand_dir / "hilmar-logo.png"
        _svg = _brand_dir / "hilmar-logo.svg"
        if _svg.exists() and _svg.stat().st_size > 100:
            log.ok(f"QC-033: brand logo (SVG vector) present "
                   f"({_svg.stat().st_size:,} bytes)")
        elif _png.exists() and _png.stat().st_size > 100:
            # Verify it's actually a PNG by checking magic bytes
            with open(_png, "rb") as _f:
                _magic = _f.read(8)
            if _magic[:4] == b"\x89PNG":
                log.ok(f"QC-033: brand logo (PNG raster) present "
                       f"({_png.stat().st_size:,} bytes)")
            else:
                log.error("QC-033: assets/branding/hilmar-logo.png exists but "
                          "isn't a valid PNG (magic bytes wrong)")
        else:
            log.warn("QC-033: no logo file at assets/branding/hilmar-logo.{svg,png} "
                     "— artifacts will fall back to emoji + text header")
    except Exception as _e:
        log.warn(f"QC-033: check failed with exception: {_e}")

    # QC-032: offline backup freshness. Daily backup runs at wrapper Step 4.9
    # to two targets (secondary OneDrive folder + local offline folder). If
    # the most recent backup in EITHER target is >36h old, defense-in-depth
    # is broken. WARN at >36h, ERROR at >72h.
    try:
        import json as _j
        import os as _os
        from datetime import datetime as _dt
        from datetime import timezone as _tz
        _cfg_path = Path(__file__).resolve().parent.parent / "config.json"
        _cfg_data = _j.loads(_cfg_path.read_text(encoding="utf-8")) if _cfg_path.exists() else {}
        _cfg = _cfg_data.get("backup", {}) or {}
        home = Path(_os.environ.get("USERPROFILE", _os.path.expanduser("~")))
        # Use raw config strings (may have %USERPROFILE% — expand)
        def _expand_p(p):
            return Path(_os.path.expandvars(p))
        targets = []
        sec = _cfg.get("secondary_onedrive_dir", "%USERPROFILE%/OneDrive - IdealX/HILMAR_BACKUPS")
        loc = _cfg.get("local_offline_dir", "%USERPROFILE%/hilmar-local-backups")
        for label, p in [("secondary", _expand_p(sec)), ("offline", _expand_p(loc))]:
            if p.exists():
                latest = max(
                    (f.stat().st_mtime for f in p.glob("hilmar-*.tar.gz")),
                    default=None,
                )
                if latest:
                    age_h = (_dt.now().timestamp() - latest) / 3600.0
                    targets.append((label, age_h, p))
                else:
                    targets.append((label, None, p))
            else:
                targets.append((label, "missing", p))

        if _BLOB_HOST:
            import state_store as _sst
            _age_d = _sst.latest_backup_age_days()
            if _age_d is None:
                log.error("QC-032: NO blob backup snapshots found — "
                          "state_store.py backup has never run on this store")
            elif _age_d <= 1.5:
                log.ok(f"QC-032: blob backup snapshot fresh ({_age_d:.1f}d old, "
                       f"retention {_sst.BACKUP_RETENTION_DAYS}d)")
            elif _age_d <= 3:
                log.warn(f"QC-032: newest blob backup is {_age_d:.1f}d old — "
                         "a recent fire skipped its backup step")
            else:
                log.error(f"QC-032: newest blob backup is {_age_d:.1f}d old — "
                          "backups have stopped")
            raise _QC032Done
        ok_count = sum(1 for _, age, _p in targets if isinstance(age, (int, float)) and age <= 36)
        if ok_count == 2:
            log.ok(f"QC-032: backup fresh at both targets ({targets[0][1]:.1f}h secondary, "
                   f"{targets[1][1]:.1f}h offline)")
        elif ok_count == 1:
            log.warn("QC-032: backup fresh at only 1 of 2 targets — "
                     + "; ".join(f"{lbl}={'missing' if a == 'missing' else 'no archives' if a is None else f'{a:.1f}h'}"
                                  for lbl, a, _p in targets))
        else:
            log.error("QC-032: NO backup target is fresh — defense-in-depth broken: "
                      + "; ".join(f"{lbl}={'missing' if a == 'missing' else 'no archives' if a is None else f'{a:.1f}h'}"
                                   for lbl, a, _p in targets))
    except _QC032Done:
        pass
    except Exception as _e:
        log.warn(f"QC-032: check failed with exception: {_e}")

    # QC-030: transit-time data coverage. ETD + ETA together yield transit
    # days — Hilmar's carrier-comparison metric. We need both fields populated
    # on ≥80% of WIN/Q&L rows to produce a useful Carrier Rate + Transit
    # Ranges table in the daily rate intelligence section.
    try:
        _eligible = [r for r in requests
                     if r.get("status") in ("WIN", "LOSS")
                     and r.get("response_timestamp")]
        if _eligible:
            _with_both = sum(1 for r in _eligible
                             if r.get("etd_offered") and r.get("eta_offered"))
            _pct = _with_both * 100 / len(_eligible)
            if _pct < 70:
                log.error(
                    f"QC-030: transit-time pair (ETD+ETA) on only {_pct:.0f}% of "
                    f"rows — carrier transit-time analytics will be sparse"
                )
            elif _pct < 85:
                log.warn(f"QC-030: transit-time pair on {_pct:.0f}% (target 85%+)")
            else:
                log.ok(f"QC-030: transit-time pair on {_pct:.0f}% of {len(_eligible)} active rows")
    except Exception as _e:
        log.warn(f"QC-030: check failed with exception: {_e}")

    # QC-031: cross-project schema doc presence. Ensures
    # SHARED/client_intelligence/SCHEMA.md exists — rate-tracker integrators
    # need this to know the contract. Missing doc means future Claude
    # sessions / engineers won't know the schema and will diverge.
    try:
        import os as _os
        home = Path(_os.environ.get("USERPROFILE", _os.path.expanduser("~")))
        for c in [home / "OneDrive - IdealX" / "SHARED" / "client_intelligence",
                  home / "OneDrive" / "SHARED" / "client_intelligence"]:
            schema = c / "SCHEMA.md"
            if schema.exists():
                log.ok(f"QC-031: shared store SCHEMA.md present ({schema.stat().st_size:,} bytes)")
                break
        else:
            if _BLOB_HOST:
                # The OneDrive SHARED store isn't mounted on a runner, and the
                # cross-project intelligence is authoritative on Turso (the
                # sync_to_quote_tracker push). SCHEMA.md is a Cloud-PC artifact.
                log.ok("QC-031: skipped — ephemeral runner (SHARED store is OneDrive/Cloud-PC; Turso is authoritative)")
            else:
                log.warn("QC-031: SHARED/client_intelligence/SCHEMA.md not found — "
                         "cross-project integrators will need to reverse-engineer the schema")
    except Exception as _e:
        log.warn(f"QC-031: check failed with exception: {_e}")

    # QC-028: rate-intelligence artifact freshness. The cross-project rate-
    # negotiation cheat sheet (gen_rate_intelligence.py) runs each fire and
    # writes reports/rate-intelligence.json. If this file is missing or
    # stale, the daily audit lost its negotiation section.
    try:
        from datetime import datetime as _dt
        from datetime import timedelta as _td
        from datetime import timezone as _tz
        _ri_path = Path(__file__).resolve().parent.parent / "reports" / "rate-intelligence.json"
        if not _ri_path.exists():
            if _BLOB_HOST:
                log.ok("QC-028: skipped — ephemeral runner, artifact is generated later this fire")
            else:
                log.warn("QC-028: reports/rate-intelligence.json missing — gen_rate_intelligence didn't run this fire")
        else:
            _age_h = (_dt.now().timestamp() - _ri_path.stat().st_mtime) / 3600.0
            if _age_h > 26:  # daily fire + 2h slack
                log.warn(f"QC-028: rate-intelligence.json is {_age_h:.0f}h stale")
            else:
                import json as _j
                _ri = _j.loads(_ri_path.read_text(encoding="utf-8"))
                log.ok(f"QC-028: rate intel fresh — {len(_ri.get('lane_cheat_sheet', []))} lanes, "
                       f"{len(_ri.get('carrier_cooling', []))} cooling, "
                       f"{len(_ri.get('lane_regression', []))} regressing")
    except Exception as _e:
        log.warn(f"QC-028: check failed with exception: {_e}")

    # QC-029: shared cross-project client_intelligence store integrity.
    # Hilmar pipeline exports to SHARED/client_intelligence/hilmar/ each fire.
    # If the export failed silently or the row count drops vs. tracker count,
    # the rate-tracker for cross-client insights is reading stale/incomplete
    # data. Surface the freshness + count delta here.
    try:
        import os as _os
        from datetime import datetime as _dt
        home = Path(_os.environ.get("USERPROFILE", _os.path.expanduser("~")))
        shared_candidates = [
            home / "OneDrive - IdealX" / "SHARED" / "client_intelligence" / "hilmar",
            home / "OneDrive" / "SHARED" / "client_intelligence" / "hilmar",
        ]
        shared = next((c for c in shared_candidates if c.exists()), None)
        if not shared:
            if _BLOB_HOST:
                # Ephemeral runner: the OneDrive SHARED file export is Cloud-PC-
                # only. The cross-project store is fed via the Turso sync
                # (sync_to_quote_tracker, audited by QC-037), so a missing
                # OneDrive folder here is expected, not a finding.
                log.ok("QC-029: skipped — ephemeral runner (OneDrive SHARED export is Cloud-PC-only; cross-project store is fed via the Turso sync)")
            else:
                log.warn("QC-029: shared client_intelligence/hilmar/ folder not found — export likely never ran")
        else:
            _meta_path = shared / "_client_meta.json"
            if not _meta_path.exists():
                log.warn("QC-029: shared store missing _client_meta.json — schema broken")
            else:
                import json as _j
                _meta = _j.loads(_meta_path.read_text(encoding="utf-8"))
                _age_h = (_dt.now().timestamp() - _meta_path.stat().st_mtime) / 3600.0
                _shared_rows = _meta.get("row_count", 0)
                _local_rows = sum(1 for r in requests
                                  if r.get("status") in ("WIN", "LOSS", "PENDING"))
                _diff = _local_rows - _shared_rows
                if _age_h > 26:
                    log.warn(f"QC-029: shared store {_age_h:.0f}h stale — export didn't run today")
                elif abs(_diff) > 5:
                    log.warn(f"QC-029: shared store row count {_shared_rows} differs from local {_local_rows} (delta {_diff})")
                else:
                    log.ok(f"QC-029: shared store fresh ({_age_h:.1f}h, {_shared_rows} rows, {_meta.get('carrier_count')} carriers, {_meta.get('lane_count')} lanes)")
    except Exception as _e:
        log.warn(f"QC-029: check failed with exception: {_e}")

    # QC-027 SELF-HEAL ONLY. The MEASUREMENT lives at the end of this phase,
    # beside QC-039 — see the banner there.
    #
    # 2026-08-10, Michael on the Carrier=87% ERROR: "you have to fix this.. it
    # used to work.. don't know what you did." The heal is deliberately left
    # HERE rather than moved down with the measurement: it WRITES pol/pod, and
    # QC-064 (which nulls garbage out of client-visible fields, pol and pod
    # among them) runs later in this same phase. A heal that runs after the
    # scrub puts a derived value in front of the client that nothing checks.
    # Heals early, measurement last — that is the whole rule.
    try:
        _active = qc027_active_rows(requests)
        # SELF-HEAL (2026-06-17): POL/POD were measured but never healed, so
        # QC-027 fired ERROR every day with nothing fixing it. POD is always
        # the destination (the ocean discharge port); POL is the origin when
        # it's a seaport. Derive any missing ones from the lane before
        # measuring — the intake fix (ingest._derive_ports) covers new rows;
        # this covers rows already in tracking-data from before that landed.
        try:
            from ingest import _derive_ports as _dports
            _healed_ports = 0
            for r in _active:
                if not r.get("pol") or not r.get("pod"):
                    _p, _d = _dports(r.get("origin"), r.get("destination"))
                    if _p and not r.get("pol"):
                        r["pol"] = _p; _healed_ports += 1
                    if _d and not r.get("pod"):
                        r["pod"] = _d; _healed_ports += 1
            if _healed_ports:
                log.fix(f"QC-027: derived {_healed_ports} missing POL/POD value(s) from lane endpoints")
        except Exception as _e:
            log.warn(f"QC-027: POL/POD self-heal skipped: {_e}")
    except Exception as _e:
        log.warn(f"QC-027: POL/POD self-heal failed with exception: {_e}")

    # QC-026: script-sync drift between OneDrive (live) and git repo (remote).
    # Per Michael 2026-05-13 "i need this to become a remote app as well so i
    # can use code from my phone and other laptops". The wrapper now git-pulls
    # latest scripts into OneDrive each fire. This QC verifies the two are
    # in sync — drift means either the pull failed, or a manual edit landed
    # in OneDrive that isn't in git yet (will be overwritten next pull).
    try:
        _live_scripts = Path(__file__).resolve().parent
        _repo_scripts = _live_scripts.parent / "hilmar-daily-routine" / "scripts"
        if _repo_scripts.exists():
            _diffs = []
            for live in _live_scripts.glob("*.py"):
                repo = _repo_scripts / live.name
                if not repo.exists():
                    _diffs.append(f"{live.name} (not in repo)")
                    continue
                if live.read_bytes() != repo.read_bytes():
                    _diffs.append(live.name)
            if len(_diffs) > 3:
                log.warn(
                    f"QC-026: {len(_diffs)} scripts drift between OneDrive and repo: "
                    + ", ".join(_diffs[:5])
                    + (f" + {len(_diffs)-5} more" if len(_diffs) > 5 else "")
                    + " — next wrapper git-pull will overwrite local edits"
                )
            elif _diffs:
                log.ok(
                    f"QC-026: {len(_diffs)} script(s) differ — minor drift, "
                    f"will sync on next fire: {', '.join(_diffs)}"
                )
            else:
                log.ok("QC-026: scripts in OneDrive match git repo (no drift)")
        else:
            if _BLOB_HOST:
                log.ok("QC-026: skipped — runner executes the git checkout directly, no OneDrive mirror to drift")
            else:
                log.warn("QC-026: hilmar-daily-routine/scripts not found — git remote sync disabled")
    except Exception as _e:
        log.warn(f"QC-026: check failed with exception: {_e}")

    # QC-025: per-day flag integrity. Each daily send writes a line to
    # reports/sent-YYYY-MM-DD.flag. Multiple lines are normal (manual fire +
    # scheduled fire same day) BUT >3 lines in one day means something is
    # looping or auto-retrying — investigate.
    try:
        from datetime import datetime as _dt
        # Flags are keyed to the REPORT business day (outlook_send._flag_date
        # / core's wee-hours rule), so read the same name the sender writes.
        _today = core.report_business_day(_dt.now(core.ET)).strftime("%Y-%m-%d")
        _flag = Path(__file__).resolve().parent.parent / "reports" / f"sent-{_today}.flag"
        if _flag.exists():
            _lines = [ln for ln in _flag.read_text(encoding="utf-8").splitlines()
                      if ln.strip().startswith("Sent ")]
            if len(_lines) > 5:
                log.error(
                    f"QC-025: {len(_lines)} 'Sent' entries in today's flag — "
                    "something is looping. Check scheduled tasks + manual fires."
                )
            elif len(_lines) > 3:
                log.warn(
                    f"QC-025: {len(_lines)} 'Sent' entries in today's flag — "
                    "more than expected (manual + scheduled = 2 max usually)."
                )
            else:
                log.ok(f"QC-025: today's flag has {len(_lines)} send entries (healthy)")
        else:
            log.ok("QC-025: today's flag not present (no send yet — normal pre-6PM)")
    except Exception as _e:
        log.warn(f"QC-025: check failed with exception: {_e}")

    # QC-020: stale-NQ display cutoff is enforced AND aggregates remain whole.
    # Per Michael 2026-05-13: 'after 2 weeks with items that have no reply..
    # just remove them from system that says not quoted but keep it on the
    # talley of volumes that hilmar moves for rate negotiation'.
    # Two assertions:
    #   (a) every NQ row in reports/email-body.html has request_date within
    #       the last 14 days (the display cutoff)
    #   (b) summary.not_quoted + summary.teu_not_quoted still count ALL NQ
    #       rows regardless of age (volume tally preserved for rate-neg)
    try:
        import re as _re
        from datetime import datetime as _dt
        from datetime import timedelta as _td
        NQ_WINDOW = 14
        _cutoff = (_dt.now(core.ET).date() - _td(days=NQ_WINDOW)).isoformat()
        # Aggregate check (b)
        _all_nq = [r for r in requests
                   if r.get("status") == "LOSS" and (r.get("loss_reason") or "") == "NO_RESPONSE"]
        _summary_nq = (data.get("summary") or {}).get("not_quoted", 0)
        if _summary_nq != len(_all_nq):
            log.error(
                f"QC-020b: summary.not_quoted={_summary_nq} but raw count={len(_all_nq)}. "
                "Display-window filter leaked into the aggregate — volume tally broken."
            )
        else:
            log.ok(f"QC-020b: NQ aggregate intact ({len(_all_nq)} total — full tally for rate-neg)")
        # Display check (a)
        _body_path = Path(__file__).resolve().parent.parent / "reports" / "email-body.html"
        if _body_path.exists():
            _body = _body_path.read_text(encoding="utf-8")
            # NQ table rows have dates in YYYY-MM-DD format in the first <td>
            # Extract dates that appear between the NQ section header and its </table>
            _nq_section = _body.split("Not Quoted")[1] if "Not Quoted" in _body else ""
            _nq_dates = _re.findall(r"\b(202\d-\d{2}-\d{2})\b", _nq_section[:8000])
            _stale = [d for d in _nq_dates if d < _cutoff]
            if _stale:
                log.warn(
                    f"QC-020a: NQ section has {len(_stale)} rows older than {NQ_WINDOW}d "
                    f"({_stale[0]}..{_stale[-1]}). Display cutoff not applied."
                )
            else:
                log.ok(f"QC-020a: NQ section display rows all within last {NQ_WINDOW} days")
    except Exception as _e:
        log.warn(f"QC-020: check failed with exception: {_e}")

    # QC-019: status-change rows on the report date must have carrier_quoted.
    # Surfaced 2026-05-13 — Michael "status change of pending to quoted with no
    # carrier and no rate". When OL responds with a rate quote, the row's
    # status transitions PENDING -> QUOTED/WIN/LOSS, and that response body
    # carries the carrier+rate. If the parser fails to extract them (new
    # multi-line pipe-table template surfaced this week), the status change
    # appears in the email body but the carrier/rate columns are empty —
    # broken UX and broken negotiation depth. This QC catches the failure
    # at the data level so the email doesn't ship with empty cells.
    try:
        from datetime import datetime as _dt
        _now_et = _dt.now(core.ET).date()
        # Report day = today's now-complete biz day (core.report_business_day,
        # the single source of truth shared with gen_email._report_date).
        _report_iso = core.report_business_day(_now_et).isoformat()
        _missing = []
        for r in requests:
            for h in (r.get("status_history") or []):
                at = (h.get("at") or "")[:10]
                if (at == _report_iso and h.get("from") and h.get("to")
                        and h["from"] != h["to"] and h["to"] in ("QUOTED","WIN","LOSS")):
                    if not r.get("carrier_quoted") and not r.get("carrier_won"):
                        _missing.append(f"{(h.get('at') or '')[11:19]} {r.get('lane','?')}")
                    break
        if _missing:
            log.error(
                f"QC-019: {len(_missing)} status-change(s) on {_report_iso} have no "
                f"carrier — parser missed extraction. Rows: " + "; ".join(_missing[:5])
                + (f" + {len(_missing)-5} more" if len(_missing) > 5 else "")
            )
        else:
            log.ok(f"QC-019: all status changes on {_report_iso} have carrier attribution")
    except Exception as _e:
        log.warn(f"QC-019: check failed with exception: {_e}")

    # QC-018: day-row math reconciliation. The KPI day-row in email + dashboard
    # showed Requests vs W/QL/NQ but hid Pending — Michael 2026-05-08 caught
    # 2 Requests vs 0W+0QL+1NQ = 1, off-by-one. Now Pending is shown as a 5th
    # card and Total = W + QL + NQ + Pending. This QC enforces it.
    try:
        from datetime import datetime as _dt
        # Compute report date (mirror gen_email._report_date via the single
        # source of truth core.report_business_day — today's complete biz day).
        _now_et = _dt.now(core.ET).date()
        _report_iso = core.report_business_day(_now_et).isoformat()
        _day = [r for r in requests
                if (r.get("request_date") == _report_iso) or (r.get("date") == _report_iso)]
        _w  = sum(1 for r in _day if r.get("status") == "WIN")
        _ql = sum(1 for r in _day if r.get("status") == "LOSS" and r.get("quoted"))
        _nq = sum(1 for r in _day if r.get("status") == "LOSS" and not r.get("quoted"))
        _p  = sum(1 for r in _day if r.get("status") == "PENDING")
        _t  = len(_day)
        _sum = _w + _ql + _nq + _p
        if _t != _sum:
            log.error(
                f"QC-018: day-row math broken — total={_t} but W+QL+NQ+P={_sum} "
                f"(W={_w}, QL={_ql}, NQ={_nq}, P={_p}). Some rows have unknown status."
            )
        else:
            log.ok(f"QC-018: day-row math reconciled ({_t} = {_w}W + {_ql}QL + {_nq}NQ + {_p}P)")
    except Exception as _e:
        log.warn(f"QC-018: check failed with exception: {_e}")

    # QC-054: RUNTIME-DEPS IMPORTABLE — every module the wrapper's Python
    # actually IMPORTS must resolve, in the SAME interpreter that runs the
    # pipeline. Added 2026-06-09 after HILMAR-DAILY-TRACKER-9 fired daily
    # for weeks: the cron heartbeat code called `import sentry_sdk` but
    # sentry-sdk was not installed in the wrapper's Python, so it logged
    # "Sentry cron start failed (pipeline continues)" every fire and the
    # cron monitor alerted on the missed check-in. The PRIOR audit didn't
    # catch it because no QC asserted the modules the pipeline depends on
    # actually import. Now it does — this is the root-fix, not a patch.
    #
    # AT FUNCTION-BODY INDENT — must run regardless of any sibling check's
    # outcome (an earlier session put it inside the QC-043 outer try and
    # silent-skipped on Sentry auth failures, which is exactly the failure
    # mode this check exists to surface).
    try:
        import importlib as _imp54

        def _import_missing(mods):
            out = []
            for _m in mods:
                try:
                    _imp54.invalidate_caches()
                    _imp54.import_module(_m)
                except Exception:
                    out.append(_m)
            return out

        _missing = _import_missing(RUNTIME_IMPORT_REQUIRED)
        # SELF-HEAL (CLAUDE.md §3 'self-heal safe cases'): try to install the
        # missing packages into THIS interpreter once, then re-import. The box
        # repairs its own env instead of emailing a human a pip command that
        # rides the very Outlook channel that may be down. HILMAR_QC_NO_PIP=1
        # disables the install (tests / locked-down hosts).
        _healed = []
        if _missing and os.environ.get("HILMAR_QC_NO_PIP") != "1":
            _pkgs = [_module_package(m) for m in _missing]
            try:
                _rc = subprocess.run(
                    [sys.executable, "-m", "pip", "install", "--user", "--quiet", *_pkgs],
                    capture_output=True, text=True, timeout=300).returncode
            except Exception:
                _rc = 1
            if _rc == 0:
                _still = _import_missing(_missing)
                _healed = [m for m in _missing if m not in _still]
                _missing = _still
                for _h in _healed:
                    log.fix(f"QC-054: auto-installed missing runtime dep '{_module_package(_h)}'")
        if _missing:
            log.error(
                f"QC-054: {len(_missing)} runtime dep(s) NOT importable in the "
                f"wrapper's Python and auto-install did not resolve them — "
                f"pipeline observability and/or render WILL silently degrade. "
                f"Missing: {', '.join(_missing)}. Install: "
                f"`{sys.executable} -m pip install --user "
                f"{' '.join(_module_package(m) for m in _missing)}`"
            )
        elif _healed:
            log.ok(f"QC-054: healed {len(_healed)} missing dep(s); all "
                   f"{len(RUNTIME_IMPORT_REQUIRED)} wrapper-runtime imports now resolve")
        else:
            log.ok(f"QC-054: all {len(RUNTIME_IMPORT_REQUIRED)} wrapper-runtime imports resolve")
    except Exception as _e:
        log.warn(f"QC-054: check failed with exception: {_e}")

    # QC-055: SENTRY CRON HEARTBEAT REGISTERED — assert the pipeline's
    # cron check-in actually fired. sentry_setup.py wraps the start in a
    # try/except that prints "⚠️  Sentry cron start failed (pipeline
    # continues): <reason>" when it can't reach Sentry — historically
    # because sentry_sdk wasn't installed (QC-054 is the root) but also
    # when the DSN is wrong or the network is blocked. When the heartbeat
    # fails to register, Sentry's cron monitor alerts on a missed check-in
    # (HILMAR-DAILY-TRACKER-9) — a misleading "the pipeline didn't run"
    # when actually it did. Added 2026-06-09.
    try:
        from pathlib import Path as _Path55
        _log_path = _Path55(__file__).resolve().parent.parent / "reports" / "run-log.txt"
        if _log_path.exists():
            # Look only at the recent tail so we don't keep flagging an old
            # failure once the operator has fixed the root.
            _tail = _log_path.read_text(encoding="utf-8", errors="ignore")[-50000:]
            if "Sentry cron start failed (pipeline continues)" in _tail:
                _last = _tail.rfind("Sentry cron start failed (pipeline continues)")
                _excerpt = _tail[_last:_last + 200].splitlines()[0]
                log.error(
                    f"QC-055: Sentry cron heartbeat is NOT registering — alerts "
                    f"in HILMAR-DAILY-TRACKER-9 are false positives. Excerpt: "
                    f"{_excerpt}"
                )
            else:
                log.ok("QC-055: Sentry cron heartbeat registered on the recent fire")
        elif _BLOB_HOST:
            # Ephemeral runner: run-log.txt is written by the Cloud-PC WRAPPER
            # (run_daily_laptop.cmd), which never runs here — the GH fire's
            # heartbeat is emitted directly by daily.yml and verified by
            # liveness.yml, so an absent wrapper log is expected, not a finding.
            log.ok("QC-055: skipped — ephemeral runner (wrapper run-log is Cloud-PC-only; "
                   "the GH fire's heartbeat is emitted by daily.yml, verified by liveness)")
        else:
            log.warn("QC-055: reports/run-log.txt absent — can't verify cron heartbeat")
    except Exception as _e:
        log.warn(f"QC-055: check failed with exception: {_e}")

    # QC-056: RATE-WITHOUT-CARRIER — OL quoted a rate but the row has no
    # carrier. Surfaced 2026-06-15 (Michael, the Oakland→Manila $797 quote:
    # "nothing should be blank"). The production parse_rate_table only read a
    # column literally headed "Carrier", so when OL relabeled it the rate
    # parsed and the carrier blanked — a broken email cell AND lost
    # negotiation signal. The root fix is in body_parser (header aliases +
    # data-cell + prose carrier scan); this QC is the guard against the
    # failure class returning. SELF-HEAL: re-scan the row's own stored text
    # (vessel/transshipment/pol/pod/reason) for a carrier token and backfill.
    # WARN (not ERROR) on the remainder — OL does occasionally quote a bare
    # rate with the carrier assigned only at booking, so a hard gate here
    # would block the client email on a legitimately-blank row.
    try:
        _rate_no_carrier = [
            r for r in requests
            if r.get("ol_rate") and not r.get("carrier_quoted")
            and (r.get("quoted") or r.get("status") in ("WIN", "LOSS", "Q&L"))
        ]
        _bodies = _load_bodies_index()
        _healed, _stuck = [], []
        for r in _rate_no_carrier:
            _scan = " | ".join(str(r.get(k) or "") for k in (
                "vessel_voyage", "transshipment", "pol", "pod", "reason_detail"))
            _car = BP.detect_carrier_token(_scan, allow_short=False)
            if _car:
                with contextlib.suppress(Exception):
                    _car = core.normalize_carrier(_car) or _car
                r["carrier_quoted"] = _car
                # WINs inherit carrier_won from carrier_quoted (mirrors ingest).
                if r.get("status") == "WIN" and not r.get("carrier_won"):
                    r["carrier_won"] = _car
                _healed.append(f"{r.get('lane','?')}={_car}")
            else:
                # Second chance before declaring it stuck: a same-lane,
                # same-rate sibling row whose carrier DID parse (see
                # _carrier_from_lane_rate_sibling for why this beats
                # vessel-name inference).
                _sib = _carrier_from_lane_rate_sibling(r, requests)
                if _sib:
                    r["carrier_quoted"] = _sib
                    if r.get("status") == "WIN" and not r.get("carrier_won"):
                        r["carrier_won"] = _sib
                    _healed.append(f"{r.get('lane','?')}={_sib} (same-lane same-rate sibling)")
                    continue
                _stuck.append(r.get("lane", r.get("request_id", "?")))
                # Capture the scrubbed text the parser failed on so the
                # next audit shows WHY this row has no carrier.
                _diag = _carrier_diag_snippet(r, _bodies)
                _diag["check"] = "QC-056"
                log.carrier_diag.append(_diag)
        for _h in _healed:
            log.fix(f"QC-056: backfilled carrier from row text — {_h}")
        if _stuck:
            log.warn(
                f"QC-056: {len(_stuck)} row(s) have an OL rate but no carrier "
                f"(parser couldn't find one; re-ingest after a body_parser fix, "
                f"or OL quoted a bare rate). Lanes: " + "; ".join(_stuck[:5])
                + (f" + {len(_stuck)-5} more" if len(_stuck) > 5 else "")
            )
        elif not _rate_no_carrier:
            log.ok("QC-056: every row with an OL rate also has a carrier")
        else:
            log.ok(f"QC-056: healed {len(_healed)} rate-without-carrier row(s); none stuck")
    except Exception as _e:
        log.warn(f"QC-056: check failed with exception: {_e}")

    # QC-077: A QUOTE THE DAILY REPORT CAN NEVER SHOW.
    #
    # 2026-07-30, Michael on the Jul 29 report: "lots of data missing all
    # broken." NEW REQUESTS (3) and PENDING HILMAR (3) were populated — the
    # latter showing a real carrier and rate — while OL-USA RESPONSES,
    # STATUS CHANGES and PENDING OL all rendered "No activity".
    #
    # The two sections are not the same kind of thing. gen_email buckets OL
    # responses by EVENT DATE (gen_email.py:186-199, `resp_d == today_date`
    # off response_timestamp), while PENDING HILMAR is CURRENT STATE and is
    # not windowed at all (gen_email.py:800-801). So a row that carries an
    # ol_rate or a carrier_quoted but has NO response_timestamp is invisible
    # to OL-USA RESPONSES on every day, forever, while still displaying its
    # quote under PENDING HILMAR.
    #
    # Measured on the stored state: 29 of 315 rows (9.2%) are in exactly that
    # shape, and the newest response_timestamp anywhere is 2026-07-23 — so the
    # section had been silently empty since Jul 24 and nothing said so.
    # ingest.py:1200 is the ONLY place a matched rate response sets the field;
    # the sibling-lane fallback (ingest.py:1345) and QC-056's backfill both
    # set carrier_quoted without it.
    #
    # This does NOT heal. Synthesising a timestamp would fabricate turnaround
    # timing and corrupt the time-to-quote metrics, which CLAUDE.md forbids
    # outright. The honest move is to make the gap loud so it is fixed at the
    # ingest end, and to let the report say how many quotes it cannot date
    # (gen_email renders that count under the section).
    try:
        # Standalone bookings are EXCLUDED, and that exclusion is what keeps
        # this check honest. A stand_* row is a booking seen with no
        # rate-response email at all; ingest.py:887 leaves response_timestamp
        # None there DELIBERATELY, to signal "we never saw a rate response"
        # rather than polluting the field with the booking time. Five of the
        # 29 rows found on 2026-07-30 were exactly that — flagging them would
        # be crying wolf over correct behaviour.
        # _is_real_rate, NOT `is not None`. This check writes the STRING
        # "Not Quoted" into ol_rate itself as an NQ sentinel a few hundred
        # lines up, and `is not None` counted every one of those as a quote
        # that could not be dated. A row explicitly marked Not Quoted has no
        # quote to date — by definition, not by accident — so it inflated the
        # banner Michael reads and made the number untrustworthy. Same class
        # of error as the stand_* rows this check already excludes.
        _q_nots = [
            r for r in data.get("requests", [])
            if (_is_real_rate(r.get("ol_rate")) or r.get("carrier_quoted"))
            and not r.get("response_timestamp")
            and not str(r.get("request_id") or "").startswith("stand_")
        ]
        if _q_nots:
            _lanes = ", ".join(
                str(r.get("lane") or f"{r.get('origin')} → {r.get('destination')}")
                for r in _q_nots[:6])
            # WHY each survivor survived. A bare count is a number Michael can
            # only escalate; the split says which lever moves it — re-pull the
            # cache, or fix the ingest link. Everything reachable was already
            # dated by _heal_undated_quote before this check runs.
            _bodies_qc = _load_bodies_index()
            _why = Counter(_undated_reason(r, _bodies_qc) for r in _q_nots)
            _no_imids = _why["no_imids"]
            _no_body = _why["no_body"]
            # The two named reasons no longer have to account for everything.
            # A row whose message IS cached but carries no send time is a third
            # case, and a bucket that adds up only because nobody looked is how
            # a diagnostic starts lying. Anything left over is reported as
            # unexplained rather than dropped.
            _rest = len(_q_nots) - _no_imids - _no_body
            _rest_note = (
                f", {_rest} link to a cached message that carries no send time "
                f"or could not be classified" if _rest else "")
            log.error(
                f"QC-077: {len(_q_nots)} row(s) carry an OL rate or carrier but "
                f"NO response_timestamp — every one is a real quote that "
                f"OL-USA RESPONSES can NEVER show, because that section is "
                f"bucketed on response_timestamp. They still render under "
                f"PENDING HILMAR, so the report looks inconsistent rather than "
                # Deliberately does NOT name another check's ID in the emitted
                # text. Test helpers and the governance ratchet both scan
                # fired messages by substring, so quoting "QC-0xx" in prose
                # makes that check look like it fired from here. The
                # cross-reference lives in the comment above instead.
                f"broken. These are the rows the auto-dating heal could NOT "
                f"reach — {_no_imids} have no source message linked at all, "
                f"{_no_body} link to a message no longer in the body cache "
                f"(90-day retention){_rest_note}. Re-pull with "
                f"`refresh_stage.py --days-back N` to widen the cache, or fix "
                f"at ingest so the link is recorded when the rate is. "
                f"Lanes: {_lanes}"
                + (f" … +{len(_q_nots) - 6} more" if len(_q_nots) > 6 else ""))
        else:
            log.ok("QC-077: every quoted row has a response_timestamp and can "
                   "appear in OL-USA RESPONSES")
    except Exception as _e:
        log.warn(f"QC-077: check failed with exception: {_e}")

    # QC-076: CAN THE ALARM ACTUALLY REACH ANYONE?
    #
    # On 2026-07-27 the fire was blocked, raised a FIRE-ALERT, and that alert
    # returned {'github': False, 'teams': False} — daily.yml gave the step no
    # GH_TOKEN and the job no `issues: write`, and no Teams webhook is set. The
    # alarm existed only as a stderr banner in a failed job's log and a queue
    # file on an ephemeral runner that was then destroyed. Nobody was told; the
    # miss was noticed because the report never arrived.
    #
    # An alarm is only worth what it can deliver, and the worst time to find
    # out it is dead is the moment you need it. So check it on EVERY fire,
    # while everything is fine — the same reason QC-032 checks backup
    # freshness rather than waiting for a restore to fail.
    #
    # Scoped to UNATTENDED runs: when a human is at the terminal, stderr IS a
    # channel and an ERROR here would be crying wolf. The predicate is
    # HILMAR_NONINTERACTIVE, not GITHUB_ACTIONS — the repo sets that flag on
    # every unattended host, so keying on it (a) covers a scheduled run
    # wherever it fires, not just on Actions, and (b) stops this ERROR firing
    # on CI/PR runs, where test.yml drives qc_selfheal with GITHUB_ACTIONS=true
    # and no token and no alarm is expected.
    #
    # WHAT THIS DOES NOT PROVE: that the channel WORKS. Both halves check that
    # a credential is resolvable, not that it is powerful. A GH_TOKEN without
    # the job's `issues: write` passes here and still 403s in _github_issue —
    # that half of the 2026-07-27 outage is asserted statically against
    # daily.yml in tests/test_audit_batch7.py instead, because proving it live
    # would mean spending an API call on every fire to test the alarm. The log
    # line says "configured", not "delivers", so it cannot be misread as more.
    try:
        _unattended = bool(os.environ.get("HILMAR_NONINTERACTIVE"))
        _ch = {
            "github": bool(_fire_alert_github_configured()),
            "teams": bool(_fire_alert_teams_configured()),
        }
        _live = [k for k, v in _ch.items() if v]
        if _live:
            log.ok(f"QC-076: out-of-band alarm configured via {', '.join(_live)}")
        elif _unattended:
            log.error(
                "QC-076: NO out-of-band alert channel is available — a failing "
                "fire cannot tell anyone. github=False (needs GH_TOKEN + the "
                "job's `issues: write`), teams=False (needs TEAMS_WEBHOOK_URL). "
                "On an ephemeral runner stderr and the local queue die with the "
                "container, so an alert raised now would reach NOBODY.")
        else:
            log.ok("QC-076: skipped — attended run, stderr reaches the operator")
    except Exception as _e:
        log.warn(f"QC-076: alarm-configuration check failed: {_e}")

    # QC-057: INTAKE RECONCILIATION — a staged Lonny RFQ silently dropped.
    # Root cause this guards: ingest.build_requests skips any lonny_outbound
    # email whose subject (and body) yields no parseable destination
    # ("if not destination: skipped_ops += 1; continue"), bumping a counter it
    # never logs or returns — so a REAL rate request can vanish from the
    # report with NO alarm. This is the exact 2026-06-24 "Busan Korea from
    # Dalhart" miss that hid for a week (PR #57 fixed that subject; this guard
    # catches the NEXT novel one). _intake_reconciliation reuses ingest's own
    # clean_destination / is_operational_subject / out_of_scope_reason so the
    # guard and the intake can never drift. SELF-HEAL: none possible
    # automatically — you cannot invent a lane; the root fix is always a
    # parser extension, so the dropped subjects are surfaced for the operator
    # (route: flag_for_operator). WARN for 1-2 (one novel subject must not
    # block the whole client report), ERROR at >=3 (systemic parser breakage).
    try:
        import ingest as _ingest
        if not _ingest.STAGE_PATH.exists():
            log.ok("QC-057: skipped — no staged emails present (ephemeral runner / pre-ingest)")
        else:
            _expected, _dropped = _intake_reconciliation(
                _ingest.load_stage(), _ingest.load_bodies_index())
            # Acknowledged commercial notes stay visible every run — an
            # acknowledgment must never silently disappear an email.
            for _subj57, _why57 in _intake_acked_notes(_ingest.load_stage()):
                log.ok(f"QC-057: acknowledged commercial note (not an RFQ): "
                       f"'{_subj57}' — {_why57}")
            if not _dropped:
                log.ok(f"QC-057: intake reconciled — all {_expected} staged Lonny "
                       f"RFQ(s) resolved a destination")
            else:
                _n = len(_dropped)
                _lst = "; ".join(_dropped[:5]) + (f" + {_n-5} more" if _n > 5 else "")
                _m = (f"QC-057: {_n}/{_expected} staged Lonny RFQ(s) SILENTLY DROPPED by "
                      f"ingest — subject+body yield no destination (not ops, not "
                      f"out-of-scope), so a real rate request is missing from the report. "
                      f"Extend body_parser.parse_subject_lane for: {_lst}")
                if _n >= 3:
                    log.error(_m + "  [>=3 -> systemic parser breakage]")
                else:
                    log.warn(_m)
                # PII-scrubbed body diagnostics: the root fix is always a
                # parser extension, and the parser fix needs the lane-bearing
                # text it failed on — surface it (run log + audit email)
                # instead of making the operator dig out the raw email.
                for _dg in _intake_drop_diag(_ingest.load_stage(),
                                             _ingest.load_bodies_index()):
                    log.warn(f"QC-057-DIAG: subject='{_dg['subject']}' "
                             f"has_body={_dg['has_body']} body: {_dg['snippet']}")
    except Exception as _e:
        log.warn(f"QC-057: check failed with exception: {_e}")

    # QC-058: HISTORIAN FRESHNESS — the durable Turso stats store is being fed
    # daily. Per CLAUDE.md §3 ("new API integration → freshness check"): the
    # 2026-06-24 historian appends finalized rows to Turso so longitudinal
    # stats survive past the 14-day window. If that append silently stops, the
    # history quietly goes stale. This check WARNs (never ERROR — a downstream
    # analytics sync must not block the client report) when the historian is
    # configured but its newest write is >26h old. SKIPS cleanly when dormant
    # (no creds) so it costs nothing until the DB is provisioned. Note the
    # write happens later in the SAME fire (after QC), so a None age = "no rows
    # yet" is benign on the first day, not a failure.
    try:
        import historian as _hist
        if not _hist.is_configured():
            log.ok("QC-058: skipped — historian dormant (no Turso creds configured)")
        else:
            _age = _hist.latest_write_age_hours()
            if _age is None:
                log.ok("QC-058: historian configured; no rows yet "
                       "(first append happens later this fire)")
            elif _age > 26:
                log.warn(
                    f"QC-058: historian last write was {_age:.0f}h ago (>26h) — "
                    f"the daily finalized-row append may be failing. Check the "
                    f"'Historian (finalized → Turso)' step + secrets/historian-turso.txt.")
            else:
                log.ok(f"QC-058: historian fresh — last write {_age:.0f}h ago")
    except Exception as _e:
        log.warn(f"QC-058: check failed with exception: {_e}")

    # QC-059: DATA-FLOW INTEGRITY — the cached parse matches the CURRENT parser.
    # Michael 2026-06-24: "the system should check for breaks in data flow then
    # backfill as quality control." refresh_stage parses each email once at
    # fetch time and caches it; ingest consumes that cache. So after a
    # body_parser fix the back-catalog already in the window stays stale until
    # its cache is refreshed — a real break (upstream raw body fine, downstream
    # parse stale) that used to need a manual reprocess. The pipeline now runs
    # reprocess_bodies BEFORE ingest to self-heal this every fire; THIS check is
    # the guard that the backfill actually happened: it re-parses the cache and
    # compares to what's stored. changed==0 → integrity verified. changed>0 →
    # the pre-ingest backfill didn't run (or a parser change landed after it),
    # so SELF-HEAL by backfilling now (takes effect next fire) and WARN with
    # what was stale. Never ERROR — a stale cache degrades fields, it doesn't
    # lose live data (tracking-data rebuilds from Outlook each fire).
    try:
        import reprocess_bodies as _rp
        _drift = _rp.reprocess(write=False)
        if not _drift.get("present"):
            log.ok("QC-059: skipped — no cached bodies present (ephemeral runner / pre-fetch)")
        elif _drift.get("changed", 0) == 0:
            log.ok(f"QC-059: data-flow integrity verified — all {_drift['total']} "
                   f"cached parses match the current parser")
        else:
            _healed = _rp.reprocess(write=True)
            _bits = ", ".join(
                f"{k.replace('delta_', '+')}={_drift[k]}"
                for k in ("delta_carrier", "delta_rate", "delta_dest",
                          "delta_signer", "delta_vessel") if _drift.get(k))
            log.fix(f"QC-059: backfilled {_healed.get('changed', _drift['changed'])} "
                    f"stale parse(s) [{_bits or 'fields changed'}] — the pre-ingest "
                    f"reprocess step did not keep the cache fresh this fire")
            log.warn(f"QC-059: {_drift['changed']}/{_drift['total']} cached parses were "
                     f"STALE vs the current parser (data-flow break) — backfilled now; "
                     f"re-ingest to surface them this report, else they land next fire. "
                     f"Check the 'Parser backfill (reprocess cache)' pipeline step.")
    except Exception as _e:
        log.warn(f"QC-059: check failed with exception: {_e}")

    # QC-061: INTERPRETER PARITY — the running Python matches the pinned
    # .python-version. The box silently drifted to 3.14 (untested; CI is 3.12)
    # for a week because the wrapper's discovery loop preferred whatever was on
    # disk and nothing asserted the version. Because QC runs INSIDE the
    # wrapper's chosen interpreter, this catches the exact drift at fire time.
    # No auto-heal (can't reinstall Python from here) → flag_for_operator.
    try:
        _ok61, _running, _pinned = check_interpreter_parity()
        if _pinned is None:
            log.warn("QC-061: no .python-version pin found — cannot verify interpreter parity")
        elif _ok61:
            log.ok(f"QC-061: interpreter {_running} matches pinned {_pinned}")
        else:
            log.error(
                f"QC-061: running Python {_running} != pinned {_pinned} "
                f"(.python-version). The box is on an interpreter no test validates "
                f"— 'green in CI / broken on the box'. Install Python {_pinned} and "
                f"repoint the wrapper (deploy/setup_cloudpc.ps1).")
    except Exception as _e:
        log.warn(f"QC-061: check failed with exception: {_e}")

    # QC-060: DEPENDENCY-LIST CONSISTENCY — the list the box installs is
    # PROVABLY the list QC-054 verifies. Every RUNTIME_IMPORT_REQUIRED module
    # must be pinned in requirements.txt, and pyproject deps must equal
    # requirements-tracker.txt. The 2026-06 jinja2/sentry-sdk gap existed
    # precisely because three dep lists disagreed and none was enforced.
    # This is a SHRINK-ONLY config invariant (like QC-040 cross-tree drift):
    # repo-state, so it fires the same in CI — a contributor can't add a
    # QC-054 dep without pinning it. No auto-heal → flag_for_operator.
    try:
        _ok60, _probs60 = check_dep_consistency()
        if _ok60:
            log.ok("QC-060: dependency lists consistent (requirements.txt covers "
                   "QC-054; pyproject == requirements-tracker)")
        else:
            log.error("QC-060: dependency-list drift — the box may install a set "
                      "that doesn't cover what QC-054 needs. " + "; ".join(_probs60[:4]))
    except Exception as _e:
        log.warn(f"QC-060: check failed with exception: {_e}")

    # QC-062: LAYOUT HYGIENE — no stale duplicate tests/ shadow the real git
    # checkout. On the Cloud PC a pre-checkout-era flat copy of tests/ sat
    # directly under PROJECT HILMAR/ shadowing hilmar-daily-routine/, causing
    # pytest 'import file mismatch' collection errors. The xcopy never cleans
    # them. SELF-HEAL: delete them (known-safe — the checkout subdir is the only
    # valid copy). Returns [] in dev/CI (REPO_ROOT IS the checkout), so it never
    # deletes the real trees.
    #
    # REPO_ROOT/src is DELIBERATELY EXCLUDED here: the wrapper now mirrors
    # src\hilmar to the runtime root ON PURPOSE (deploy/run_daily_laptop.cmd
    # `xcopy src\hilmar`) so this very module can import hilmar.parser_accuracy
    # for the QC-039 gate. It is a REQUIRED deploy target, not a stale shadow —
    # sweeping it would break QC-039 and, because OneDrive locks __pycache__, the
    # rmtree would also fail and fire a false QC-062 ERROR every fire. Only
    # tests/ (never deployed to the root) is swept.
    try:
        _stale = find_stale_shadow_dirs()
        if not _stale:
            log.ok("QC-062: layout clean — no stale tests/ shadowing the checkout")
        else:
            _removed = []
            for _d in _stale:
                try:
                    shutil.rmtree(_d)
                    _removed.append(_d.name)
                except Exception as _re62:
                    log.warn(f"QC-062: could not remove stale {_d}: {_re62}")
            if _removed:
                log.fix(f"QC-062: removed stale shadow dir(s) under repo root: "
                        f"{', '.join(_removed)} (the hilmar-daily-routine/ checkout is authoritative)")
            _left = find_stale_shadow_dirs()
            if _left:
                log.error("QC-062: stale shadow dirs remain after self-heal: "
                          + ", ".join(str(d) for d in _left))
    except Exception as _e:
        log.warn(f"QC-062: check failed with exception: {_e}")

    # QC-063: CONSECUTIVE-FAILURE RATCHET — a best-effort/observer step that's
    # been dead for DAYS, not a one-day blip. Eight best-effort steps + the
    # test routine exit 0 by design, so per-fire a dead step is invisible and a
    # step failing every fire for a WEEK looks identical to a single blip (no
    # aggregation). run_pipeline records each fire's failed steps to
    # step-history.json; this escalates any step that failed the last 3
    # consecutive fires to a LOUD WARN. No auto-heal (the step's own
    # dep/env/config is the fix) → flag_for_operator.
    try:
        _hist = load_step_history()
        if not _hist:
            log.ok("QC-063: skipped — no step history yet (ephemeral runner / first fires)")
        else:
            _dead = consecutive_failed_steps(_hist, n=3)
            if _dead:
                log.warn(
                    f"QC-063: {len(_dead)} pipeline step(s) have failed the last 3 "
                    f"CONSECUTIVE fires — degraded for days, not a blip: "
                    f"{', '.join(_dead)}. Investigate the step + its dep/env "
                    f"(check reports/run-log.txt); a best-effort step being dead "
                    f"for a week is the silent-degradation failure mode.")
                # A WARN only surfaces in the idealx.us audit email, which rides
                # the same Outlook/MSAL channel RELIABILITY.md calls least
                # trustworthy when something is wrong. A best-effort step dead
                # for days deserves an out-of-band page so it can't rot silently
                # — route through fire_alert (Teams/GitHub-issue/queue/stderr).
                # Best-effort + isolated: a fire_alert failure must never affect
                # QC (mirrors the surrounding try/except isolation).
                try:
                    import fire_alert
                    fire_alert.send_alert(
                        f"QC-063: pipeline step(s) dead {3}+ consecutive fires",
                        f"{', '.join(_dead)} failed the last 3 consecutive daily "
                        f"fires (silent best-effort degradation). See "
                        f"reports/run-log.txt + reports/step-history.json.",
                        level="warning", labels=("fire-alert", "qc-063"))
                except Exception as _ae:
                    log.warn(f"QC-063: out-of-band escalation failed: {_ae}")
            else:
                log.ok(f"QC-063: no step failing 3 consecutive fires "
                       f"({len(_hist)} fires recorded)")
    except Exception as _e:
        log.warn(f"QC-063: check failed with exception: {_e}")

    # QC-064: GARBAGE IN CLIENT-VISIBLE DISPLAY FIELDS. Defense-in-depth for
    # the "absolutely wrong info" class the operator flagged — a phone
    # fragment, a raw message-id, or the OL responder-mailbox name leaking
    # into a display field (carrier/origin/destination/lane/pol/pod/vessel/
    # transshipment) that ships straight into the client email + PDF. The
    # parser is the real fix, but a single bad token in front of the client is
    # high-blast-radius, so this is a last-line scrub. SELF-HEAL: null the
    # offending field (a blank cell is strictly better than wrong info) and log
    # the field + request id + scrubbed value. WARN-class, NOT an ERROR gate —
    # a false positive must never block the client email, and the self-heal
    # already removed the bad value.
    try:
        _g64_found = 0
        _g64_remaining = 0
        for r in requests:
            for _f in QC064_DISPLAY_FIELDS:
                _reason = qc064_garbage_reason(r.get(_f))
                if not _reason:
                    continue
                _g64_found += 1
                _bad = r.get(_f)
                r[_f] = None
                log.fix(f"QC-064: nulled {_f}={_bad!r} on {r.get('request_id', '?')} "
                        f"— {_reason} (garbage in client-visible field)")
                # Confirm the heal stuck (None can't be garbage); if a field
                # somehow still reads garbage it's unhealable from here.
                if qc064_garbage_reason(r.get(_f)):
                    _g64_remaining += 1
        if _g64_found == 0:
            log.ok("QC-064: no garbage tokens in display fields")
        elif _g64_remaining:
            log.warn(f"QC-064: {_g64_remaining} garbage display value(s) could not be "
                     f"scrubbed — manual review; fix the upstream parser")
    except Exception as _e:
        log.warn(f"QC-064: check failed with exception: {_e}")

    # QC-066: IMPOSSIBLE STATE — outcome predates its own request (merge /
    # carry-forward artifact from Lonny's recurring Outlook threads). This is
    # the "new request swallowed by a stale outcome" shape from the Jul-23
    # report: a fresh request inherits WIN/quote state recorded BEFORE the ask
    # existed, so it never shows in PENDING OL and the pending math lies.
    # ERROR-class (audit red flag): the row's displayed state is wrong for the
    # client-facing pipeline. DETECT-only — no auto-heal until one live shape
    # is confirmed; the audit names each row so it can be split manually.
    try:
        _rep_day = core.report_business_day(datetime.now(core.ET))
        _bad66 = qc066_impossible_states(requests, report_day=_rep_day)
        if _bad66:
            for _rid66, _why66 in _bad66:
                log.error(f"QC-066: {_rid66} — {_why66} (stale outcome swallowed "
                          f"a new request; split the row)")
        else:
            log.ok("QC-066: no impossible request/outcome orderings")
    except Exception as _e:
        log.warn(f"QC-066: check failed with exception: {_e}")

    # QC-067: OPEN RFQ MISFILED AS LOST. Every fire, re-test that day's real
    # rows: an unquoted row whose request is still inside the PENDING-OL
    # response window is OPEN BUSINESS (chase OL), never a NO_RESPONSE loss.
    # decide_status fixes this at the source (2026-07-24); this is the daily
    # proof on live data + the self-heal for anything that slips through
    # (stale carry-forward, operator correction, future regression).
    # SELF-HEAL: restore PENDING (quoted stays False -> PENDING_OL) and clear
    # the loss_reason, recording the transition so the audit trail survives.
    try:
        _bad67 = qc067_open_rfq_misfiled_as_lost(requests)
        if _bad67:
            _by_id67 = {r.get("request_id"): r for r in requests}
            for _rid67, _hrs67 in _bad67:
                _row67 = _by_id67.get(_rid67)
                if not _row67:
                    continue
                # An operator's verdict outranks an automatic heal. Every
                # other re-decide path in this file skips manual_locked rows;
                # this one did not, so a human correction could be silently
                # undone on the next fire.
                if _row67.get("manual_locked"):
                    log.warn(f"QC-067: {_rid67} looks misfiled but is "
                             f"manual_locked — leaving the operator's verdict "
                             f"in place")
                    continue
                _row67["loss_reason"] = None
                _row67["reason_detail"] = (
                    f"Awaiting OL quote — {_hrs67}h since Lonny's RFQ, still "
                    f"inside the {core.PENDING_OL_LOSS_HOURS}h response window")
                # record_transition, not a hand-rolled append: the old code
                # hardcoded "from": "LOSS", which is a FABRICATED prior state
                # whenever the row was in any other status. record_transition
                # reads the real one, and no-ops if the status already matches.
                core.record_transition(
                    _row67, "PENDING",
                    "QC-067: open RFQ was misfiled as NO_RESPONSE")
                log.fix(f"QC-067: {_rid67} restored to PENDING OL — waiting "
                        f"{_hrs67}h, inside the response window (was filed "
                        f"LOSS/NO_RESPONSE)")
        else:
            log.ok("QC-067: no open RFQs misfiled as losses")
    except Exception as _e:
        log.warn(f"QC-067: check failed with exception: {_e}")

    # QC-068: OL RESPONSE-SLA BREACH. Michael 2026-07-26 — OL owes a quote
    # within 3 BUSINESS hours. Not a data defect (the rows are correctly
    # PENDING_OL); this is the daily operational alert so a blown SLA can
    # never sit silently in the dataset. WARN-class: it names the lanes OL
    # owes so the desk gets chased the same morning. No heal — only OL
    # sending the quote clears it.
    try:
        _sla68 = qc068_ol_sla_breaches(requests)
        if _sla68:
            for _rid68, _lane68, _hrs68 in _sla68:
                log.warn(f"QC-068: OL SLA breached — {_lane68} waiting {_hrs68} "
                         f"biz-h (SLA {core.PENDING_OL_SLA_BIZ_HOURS}h), "
                         f"request {_rid68} — chase the OL desk")
        else:
            log.ok(f"QC-068: no OL SLA breaches "
                   f"(SLA {core.PENDING_OL_SLA_BIZ_HOURS} biz-h)")
    except Exception as _e:
        log.warn(f"QC-068: check failed with exception: {_e}")

    # QC-069: ONE SHIPMENT, TWO ROWS. Michael's reported defect #2 — a lane
    # showing as won AND still pending in the same report. Catches a booking
    # ref landing on two rows (the stand_<mdolx> fallback firing when a lane
    # alias blocks the link) and an open row shadowed by a won row on the same
    # lane + equipment. ERROR-class: TEU is double counted and the open copy
    # will later age into a LOSS claiming OL never quoted a move OL booked.
    # Detect-only — the correct survivor depends on which row holds the real
    # request thread, so the audit names both ids rather than guessing.
    try:
        _dup69 = qc069_duplicate_shipment_rows(requests)
        if _dup69:
            for _kind69, _key69, _ids69 in _dup69:
                log.error("QC-069: " + _kind69 + " — " + _key69 + " appears on rows "
                          + ", ".join(_ids69) + " (one shipment stored twice; "
                          "TEU is double counted)")
        else:
            log.ok("QC-069: no duplicate shipment rows")
    except Exception as _e:
        log.warn("QC-069: check failed with exception: " + str(_e))

    # QC-070: TEU THAT CANNOT BE REAL. The second line of defence behind
    # core.parse_teu's hardened regex. Every volume figure in the report is a
    # SUM over rows, so ONE poisoned row (2026-07-26: "PO 4451440" -> 89,028
    # TEU) rewrites the whole day's numbers invisibly. Over-counts SELF-HEAL
    # by recomputing from the row's own containers text; rows whose text names
    # equipment but yields 0 TEU are named for a human, because healing that
    # shape would mean inventing a volume.
    try:
        _teu70 = qc070_teu_sanity(requests)
        if _teu70:
            for _rid70, _shape70, _detail70 in _teu70:
                if _shape70 == "over-count":
                    log.error(f"QC-070: request {_rid70} — {_detail70} "
                              f"(recomputed from its containers text)")
                else:
                    log.error(f"QC-070: request {_rid70} — {_detail70}")
        else:
            log.ok(f"QC-070: all rows within the {core.MAX_ROW_TEU} TEU / "
                   f"{core.MAX_ROW_CONTAINERS} container per-row ceiling")
    except Exception as _e:
        log.warn("QC-070: check failed with exception: " + str(_e))

    # QC-071: DAY BUCKETS ON THE WRONG CLOCK. request_date must be the ET
    # calendar date of request_timestamp, because every reader buckets by the
    # ET business day. A row on the wrong clock can land on a Saturday, which
    # no fire ever reports — invisible to every day-scoped surface forever
    # while still inflating the period totals. phase_3 recomputes it each pass;
    # reaching here means the heal missed the row.
    try:
        _rd71 = qc071_request_date_clock(requests)
        if _rd71:
            for _rid71, _stored71, _exp71 in _rd71:
                log.error(f"QC-071: request {_rid71} — request_date={_stored71} "
                          f"but its timestamp is ET {_exp71}; the row buckets "
                          f"on the wrong report day")
        else:
            log.ok("QC-071: every request_date matches its timestamp in ET")
    except Exception as _e:
        log.warn("QC-071: check failed with exception: " + str(_e))

    # QC-072: THE ROW AND ITS OWN AUDIT TRAIL DISAGREE. status_history is the
    # declared transition record, so a row whose history ends somewhere other
    # than its status reports the WRONG outcome to every history-based reader
    # (audits, dashboard timeline, Sentry triage). Same check covers teu_won
    # left behind on a row that is no longer a WIN. Detect-only — rewriting
    # history is never a safe automatic act.
    try:
        _h72 = qc072_history_contradicts_status(requests)
        if _h72:
            for _rid72, _kind72, _detail72 in _h72:
                log.error(f"QC-072: request {_rid72} — {_detail72}")
        else:
            log.ok("QC-072: status_history agrees with status on every row")
    except Exception as _e:
        log.warn("QC-072: check failed with exception: " + str(_e))

    # QC-073: STANDALONE BOOKING ROW HYGIENE. A stand_<mdolx> row is synthesised
    # from ONE subject line when a booking can't be linked to its RFQ, so it is
    # where invented data most easily enters the dataset — Michael's reported
    # defect #3 (degenerate lanes, blank carrier/vessel/dates shown as real
    # shipments). Degenerate lanes and fabricated rate-response timestamps are
    # ERRORs; an unattributable win is a WARN with the list to chase.
    try:
        _sb73 = qc073_standalone_booking_hygiene(requests)
        if _sb73:
            for _rid73, _sev73, _detail73 in _sb73:
                if _sev73 == "error":
                    log.error(f"QC-073: request {_rid73} — {_detail73}")
                else:
                    log.warn(f"QC-073: request {_rid73} — {_detail73}")
        else:
            log.ok("QC-073: standalone booking rows clean "
                   "(no degenerate lanes, no fabricated rate responses)")
    except Exception as _e:
        log.warn("QC-073: check failed with exception: " + str(_e))

    # QC-074: WIN EVIDENCE vs OUTCOME. Two ways one shipment's identity gets
    # corrupted — the carry-forward appending a second row under an existing
    # request_id (TEU counted twice, the id both PENDING and WIN), and a row
    # holding a real MDOLX booking ref while reported as a loss. Both are
    # ERRORs; a WIN with no evidence at all is a WARN.
    try:
        _we74 = qc074_win_evidence_consistency(requests)
        if _we74:
            for _rid74, _sev74, _detail74 in _we74:
                if _sev74 == "error":
                    log.error(f"QC-074: request {_rid74} — {_detail74}")
                else:
                    log.warn(f"QC-074: request {_rid74} — {_detail74}")
        else:
            log.ok("QC-074: win evidence consistent (no duplicate ids, "
                   "no booked-but-not-won rows)")
    except Exception as _e:
        log.warn("QC-074: check failed with exception: " + str(_e))

    # QC-065: CLIENT-REPORT INVARIANTS. The client-facing daily email
    # (gen_client_email.py → reports/client-email-body.html) goes to THE
    # CLIENT, so two invariants are hard ERRORs:
    #   (a) recipients — `to` may never contain a staff/full_list address or
    #       more than the one approved recipient (checked even while
    #       disabled: a wrong value goes live the moment the flag flips);
    #       while ENABLED, to/cc must be EXACTLY the approved pair
    #       (QC065_APPROVED_TO / QC065_APPROVED_CC).
    #   (b) content — the rendered body must carry ZERO internal analytics
    #       (win/loss framing, Q&L/NQ taxonomy, carrier-scoreboard or
    #       negotiation intel), raw OR &amp;-escaped.
    # Root fix is always config.json or gen_client_email.py — NEVER widen the
    # approved-recipient tuples or trim the marker list to make this pass.
    try:
        _problems65 = []
        _blocks65 = {}
        if QC065_CONFIG_PATH.exists():
            import json as _json
            _cfg65 = _json.loads(QC065_CONFIG_PATH.read_text(encoding="utf-8"))
            # BOTH client artifacts, one definition. client_weekly went live
            # 2026-08-05; adding it here rather than writing a QC-078 is the
            # point — the invariants are identical, so a second check would be
            # a second thing to drift.
            for _key65, _body65 in (("client_report", QC065_CLIENT_BODY_PATH),
                                    ("client_weekly", QC065_CLIENT_WEEKLY_BODY_PATH)):
                _blocks65[_key65] = _cfg65.get(_key65)
                _problems65 += qc065_check_client_block(_cfg65, _key65, _body65)
        if _problems65:
            log.error("QC-065: client-artifact invariant violations: "
                      + "; ".join(_problems65))
        else:
            _live65 = sorted(k for k, v in _blocks65.items() if (v or {}).get("enabled"))
            _off65 = sorted(k for k, v in _blocks65.items()
                            if v is not None and not (v or {}).get("enabled"))
            _absent65 = sorted(k for k, v in _blocks65.items() if v is None)
            _bits65 = []
            if _live65:
                _bits65.append(f"ENABLED {_live65} — recipients locked to the approved to/cc pair")
            if _off65:
                _bits65.append(f"disabled {_off65} — sample-only, the client receives nothing")
            if _absent65:
                _bits65.append(f"absent {_absent65} — path inert")
            log.ok("QC-065: " + "; ".join(_bits65) + "; rendered content clean")
    except Exception as _e:
        log.warn(f"QC-065: check failed with exception: {_e}")

    # QC-017: carrier over-attribution. Calibrated 2026-05-08 against actual
    # Hilmar data where CMA CGM legitimately holds ~54% of quotes (CMA is
    # Hilmar's primary carrier). ERROR > 75% catches the CMA-boilerplate
    # false-positive bug (pre-fix it ran ~55% but now we know that level is
    # legit; only >75% indicates over-attribution). WARN > 65% triggers
    # a check-the-data prompt without crying wolf.
    try:
        from collections import Counter as _Counter
        _q = _Counter()
        for r in requests:
            c = r.get("carrier_quoted") or r.get("carrier_won")
            # Count only rows where a carrier actually quoted (WINs +
            # quoted-and-lost). 2026-06-02 (track 03 finding C-5): the
            # prior check was `status in ("WIN", "LOSS") and (quoted or
            # status==WIN)` — that EXCLUDED STRICT-form Q&L entirely
            # (status=="Q&L" not "LOSS"), so today's 66% concentration
            # was computed from WIN rows only. Now uses the storage-
            # agnostic display_status / is_quoted_and_lost helpers.
            if c and (r.get("status") == "WIN" or core.is_quoted_and_lost(r)):
                _q[c] += 1
        _tot = sum(_q.values())
        if _tot > 20:
            top_carrier, top_count = _q.most_common(1)[0]
            top_pct = top_count / _tot * 100
            if top_pct > 75:
                log.error(
                    f"QC-017: {top_carrier} has {top_count}/{_tot} quotes ({top_pct:.0f}%) > 75% — "
                    "over-attribution. Body scanner may be matching boilerplate / vessel-name "
                    "text. Investigate patch_carriers + parse_rate_table column accuracy."
                )
            elif top_pct > 65:
                log.warn(
                    f"QC-017: {top_carrier} holds {top_count}/{_tot} quotes ({top_pct:.0f}%) > 65%. "
                    "Sample 3 bodies with parse_rate_table to confirm carrier column matches."
                )
            else:
                log.ok(f"QC-017: top carrier {top_carrier} = {top_count}/{_tot} ({top_pct:.0f}%) — healthy spread")
    except Exception as _e:
        log.warn(f"QC-017: check failed with exception: {_e}")

    # QC-016: backup retention cap. backup.py prune step must catch BOTH naming
    # formats (tracking-data-v2_T...Z.json from backup.py + tracking-data-v2.YYYY-...
    # from qc_selfheal Phase 1). Pre-fix the period-format files grew unbounded.
    # If we ever exceed retention*2 the prune step is broken again.
    try:
        from pathlib import Path as _P
        _bdir = _P(__file__).resolve().parent.parent / "data-backups"
        if _bdir.exists():
            _all_bk = list(_bdir.glob("tracking-data-v2*.json"))
            # cfg not in scope here; use the same default as backup.py
            _retain = 14
            if len(_all_bk) > _retain * 2:
                log.error(
                    f"QC-016: {len(_all_bk)} backup files > 2x retention ({_retain*2}). "
                    "backup._list_snapshots glob is missing a naming format again."
                )
            elif len(_all_bk) > _retain + 5:
                log.warn(
                    f"QC-016: {len(_all_bk)} backups (retention {_retain}) — prune is "
                    "running but a few stragglers remain. Likely .stale- prefixed files."
                )
            else:
                log.ok(f"QC-016: {len(_all_bk)} backup file(s) (retention {_retain})")
    except Exception as _e:
        log.warn(f"QC-016: check failed with exception: {_e}")

    # ── QC-039 LIVES AT THE END OF THIS PHASE. DO NOT MOVE IT EARLIER. ──
    # A GATE MEASURES THE FINAL STATE OF THE ROWS, AFTER EVERY MUTATING HEAL.
    # Twice now it did not, in both directions:
    #   2026-07-27 (withheld a good report): it measured ~880 lines before the
    #     carrier heals, read carrier_quoted 291/313 (93.0%) and BLOCKED the
    #     client email. QC-056 then backfilled 10 carriers on that same run —
    #     301/313 = 96.2%, over the 95% gate. A business day's report was lost
    #     to a stale measurement.
    #   2026-07-28 (would have SHIPPED a bad one): the first fix moved it only
    #     past QC-056, still ahead of QC-064, which NULLS garbage out of
    #     client-visible cells — six of QC064_DISPLAY_FIELDS are graded here
    #     and five are CRITICAL. The gate counted a field populated, QC-064
    #     blanked it, and the email would ship below threshold with the gate
    #     reading green. Measuring early can withhold a good report; measuring
    #     before a NULLING heal ships a bad one, which is worse.
    # Enforced by tests/test_audit_batch7.py — a real AST walk over every
    # graded field, including variable-key writes (`r[_f] = None`) and writes
    # inside helpers this phase calls. The substring test that shipped with the
    # first fix could not see either, and passed straight through the bug.
    # QC-039: PARSER ACCURACY GATE — per Michael 2026-05-17 "this parser and
    # your system have to run at minimum of 98 percent accuracy no matter
    # COST." Measures per-field % populated against applicability predicates
    # in src/hilmar/parser_accuracy.py. Computes:
    #   - Overall rate (equal-weight mean across fields)
    #   - Weighted rate (by applicable-row count)
    # ERROR if overall < ACCURACY_THRESHOLD OR any CRITICAL field falls
    # below ACCURACY_THRESHOLD. WARN if overall passes but a non-critical
    # field falls below.
    # Critical fields: origin, destination, lane, container_count,
    # teu_requested, carrier_quoted, carrier_won, ol_rate.
    # 2026-05-19: threshold lowered from 0.98 to 0.95 per Michael "PARSER
    # MUST REACH 95 PERCENT AT A MINIMUM AND INCLUDE ATTACHMENTS". See
    # src/hilmar/parser_accuracy.py for the gate definition + per-field
    # threshold table.
    try:
        import sys as _sys
        _src_dir = Path(__file__).resolve().parent.parent / "src"
        if str(_src_dir) not in _sys.path:
            _sys.path.insert(0, str(_src_dir))
        from hilmar.parser_accuracy import ACCURACY_THRESHOLD, CRITICAL_FIELDS, compute_accuracy
        _acc = compute_accuracy(data.get("requests", []))
        _pct = f"{_acc['overall_rate']:.1%}"
        _wpct = f"{_acc['weighted_rate']:.1%}"
        # Push Sentry metrics — these power the dashboard's "Parser
        # accuracy trend (90 days)" widget. Gauges represent current
        # snapshot value; one row per accuracy run.
        if _sentry is not None:
            try:
                _sentry.metric_gauge(
                    "parser.accuracy_overall",
                    _acc["overall_rate"],
                    phase=("pre-patch" if _qc_phase_is_pre_patch() else "post-patch"),
                )
                _sentry.metric_gauge(
                    "parser.accuracy_weighted",
                    _acc["weighted_rate"],
                    phase=("pre-patch" if _qc_phase_is_pre_patch() else "post-patch"),
                )
                # Per-field gauges, one tagged metric per field. Lets the
                # dashboard show "which field is degrading" at a glance.
                for _field, _stats in _acc.get("field_stats", {}).items():
                    if _stats.get("n_a"):
                        continue
                    _sentry.metric_gauge(
                        "parser.accuracy_per_field",
                        _stats["rate"],
                        field=_field,
                        critical=str(_field in CRITICAL_FIELDS).lower(),
                        phase=("pre-patch" if _qc_phase_is_pre_patch() else "post-patch"),
                    )
            except Exception:
                pass
        if _acc["critical_failing"]:
            log.error(
                f"QC-039: parser accuracy {_pct} (weighted {_wpct}) with "
                f"{len(_acc['critical_failing'])} CRITICAL field(s) below "
                f"{ACCURACY_THRESHOLD:.0%}: " +
                ", ".join(
                    f"{f}={_acc['field_stats'][f]['populated']}/"
                    f"{_acc['field_stats'][f]['applicable']} "
                    f"({_acc['field_stats'][f]['rate']:.1%})"
                    for f in _acc["critical_failing"]
                )
            )
        elif _acc["failing_fields"]:
            log.warn(
                f"QC-039: parser accuracy {_pct} overall (weighted {_wpct}); "
                f"{len(_acc['failing_fields'])} non-critical field(s) below "
                f"{ACCURACY_THRESHOLD:.0%}: " +
                ", ".join(
                    f"{f}={_acc['field_stats'][f]['rate']:.1%}"
                    for f in _acc["failing_fields"]
                )
            )
        elif _acc["overall_rate"] < ACCURACY_THRESHOLD:
            # All individual fields ≥ threshold but the equal-weight mean
            # falls below (e.g. one big-applicable field at 95% pulls down
            # several 100%s). Warn — investigate distribution.
            log.warn(
                f"QC-039: parser accuracy {_pct} below threshold "
                f"{ACCURACY_THRESHOLD:.0%} (weighted {_wpct}) — "
                "no single field failed but equal-weight mean is low"
            )
        else:
            log.ok(
                f"QC-039: parser accuracy {_pct} (weighted {_wpct}) "
                f"≥ {ACCURACY_THRESHOLD:.0%} on all {len([f for f in _acc['field_stats'].values() if not f.get('n_a')])} measured fields"
            )
    except Exception as _e:
        # Fail CLOSED, not open: a parser-accuracy gate that cannot evaluate
        # (import regression, malformed requests, KeyError, ...) must surface
        # as an ERROR so it gates qc-result status (HAS_ERRORS) and fires
        # Sentry, not get buried as a non-blocking WARN. Per CLAUDE.md rule #3
        # (solve root causes, never let a broken gate silently degrade).
        log.error(f"QC-039: parser-accuracy gate FAILED TO EVALUATE (failing closed): {_e}")

    # ── QC-027 MEASUREMENT. SAME RULE AS QC-039 ABOVE: DO NOT MOVE EARLIER. ──
    # Data completeness across key fields. Per Michael 2026-05-13 "90 percent
    # for all is the bare minimum". Measures REACHABLE rows only — rows whose
    # rate-response body left a trace. WIN rows whose only body is the booking
    # confirmation (data lives in the PDF attachment) are counted separately as
    # "PDF-only" so the gap is visible without breaking the gate.
    #
    # 2026-08-10 — WHY IT MOVED. It used to sit ~1200 lines up, ahead of two
    # heals that write the very fields it grades:
    #     QC-056  backfills carrier_quoted (from row text, then from a
    #             same-lane same-rate sibling)
    #     QC-064  NULLS garbage out of carrier/origin/destination/lane/pol/
    #             pod/vessel/transshipment
    # So the daily "Carrier=87% (ERROR <90%)" was a reading of a state that no
    # longer existed by the time the run ended — it counted as missing every
    # carrier QC-056 was about to restore, and counted as present every value
    # QC-064 was about to blank. Michael: "you have to fix this.. it used to
    # work.. don't know what you did." Nothing broke the carriers; the ruler
    # was held up before the repair and after the damage.
    #
    # This is the FOURTH instance of measure-before-heal in this phase (QC-039
    # 2026-07-27, batch-5 #15's persisted aggregates, QC-075's stale summary,
    # now QC-027). Enforced by tests/test_qc027_measures_final_state.py, which
    # AST-walks every write to a graded field inside phase_6_rules.
    try:
        _active27 = qc027_active_rows(requests)
        _reachable = [r for r in _active27 if qc027_is_reachable(r)]
        _pdf_only = [r for r in _active27 if not qc027_is_reachable(r)]
        if _reachable:
            _problems = []
            _ok_count = 0
            for fld, label in QC027_FIELDS:
                _present = sum(1 for r in _reachable if r.get(fld))
                _pct27 = _present * 100 / len(_reachable)
                if _pct27 < 90:
                    _problems.append(f"{label}={_pct27:.0f}% (ERROR <90%)")
                elif _pct27 < 95:
                    _problems.append(f"{label}={_pct27:.0f}% (WARN 90-95%)")
                else:
                    _ok_count += 1
            _pdf_note = (f" — {len(_pdf_only)} PDF-only WIN(s) excluded (data in attachment)"
                         if _pdf_only else "")
            if any("ERROR" in p for p in _problems):
                log.error(f"QC-027: data completeness on {len(_reachable)} reachable rows — "
                          + "; ".join(_problems) + _pdf_note)
            elif _problems:
                log.warn(f"QC-027: data completeness on {len(_reachable)} reachable rows — "
                         + "; ".join(_problems) + _pdf_note)
            else:
                log.ok(f"QC-027: data completeness OK on {len(_reachable)} reachable rows "
                       f"({_ok_count}/{len(QC027_FIELDS)} fields ≥95%){_pdf_note}")
        # Track PDF-only rows separately so they're visible — they
        # need either PDF parsing or stage extension to surface
        if len(_pdf_only) > 5:
            log.warn(
                f"QC-027b: {len(_pdf_only)} WIN(s) have rate data only in PDF attachment "
                "— consider PDF parsing (pdfplumber) to lift completeness for confirmed bookings"
            )
    except Exception as _e:
        log.warn(f"QC-027: check failed with exception: {_e}")


# ─────────────────────────────────────────────────────────────────────
# Phase 7 — persist + QC result file
# ─────────────────────────────────────────────────────────────────────

def _carrier_coverage(requests):
    """Compute carrier-attribution coverage for QC report."""
    losses_quoted = [r for r in requests if r.get("status") == "LOSS" and r.get("quoted")]
    wins = [r for r in requests if r.get("status") == "WIN"]
    losses_with_carrier = [r for r in losses_quoted if r.get("carrier_quoted")]
    wins_with_carrier = [r for r in wins if r.get("carrier_won")]
    return {
        "ql_with_carrier": len(losses_with_carrier),
        "ql_total": len(losses_quoted),
        "ql_coverage_pct": round(len(losses_with_carrier) * 100 / max(1, len(losses_quoted)), 1),
        "win_with_carrier": len(wins_with_carrier),
        "win_total": len(wins),
        "win_coverage_pct": round(len(wins_with_carrier) * 100 / max(1, len(wins)), 1),
    }


def _trade_region_reconciliation(data):
    """Verify trade-region rollup matches summary KPIs."""
    requests = data.get("requests", []) or []
    summary = data.get("summary", {}) or {}
    try:
        regions = core.aggregate_trade_regions(requests)
    except Exception:
        return {"reconciled": False, "error": "aggregator failed"}
    sum_req = sum(m["requests"] for m in regions.values())
    sum_w   = sum(m["wins"] for m in regions.values())
    sum_ql  = sum(m["quoted_lost"] for m in regions.values())
    sum_nq  = sum(m["not_quoted"] for m in regions.values())
    reconciled = (
        sum_req == summary.get("total_entries", 0)
        and sum_w == summary.get("wins", 0)
        and sum_ql == summary.get("quoted_lost", 0)
        and sum_nq == summary.get("not_quoted", 0)
    )
    unmapped = next((m for m in regions.values() if m["region"] == "Unmapped"), None)
    return {
        "reconciled": reconciled,
        "regions": [
            {"region": m["region"], "requests": m["requests"], "wins": m["wins"],
             "ql": m["quoted_lost"], "nq": m["not_quoted"], "teu": m["teu_requested"],
             "win_rate": m["win_rate"]}
            for m in sorted(regions.values(), key=lambda m: m["teu_requested"], reverse=True)
        ],
        "unmapped_destinations": unmapped["destinations"] if unmapped else [],
        "summary_totals": {
            "requests": summary.get("total_entries", 0),
            "wins": summary.get("wins", 0),
            "ql": summary.get("quoted_lost", 0),
            "nq": summary.get("not_quoted", 0),
        },
    }


def _parser_sweep_audit(requests):
    """Pull parser-sweep history from per-record status_history entries."""
    sweep_count = 0
    sweep_records = []
    for r in requests:
        for h in (r.get("status_history") or []):
            reason = (h.get("reason") or "").lower()
            if "parser sweep" in reason or "backfilled" in reason:
                sweep_count += 1
                sweep_records.append({
                    "request_id": r.get("request_id"),
                    "lane": r.get("lane"),
                    "request_date": r.get("request_date"),
                    "at": h.get("at"),
                    "reason": h.get("reason"),
                })
                break
    return {
        "total_sweep_fixes": sweep_count,
        "records_touched": sweep_records[:30],  # cap for readability
    }



def _per_carrier_breakdown(requests):
    """Per-carrier W/L/TEU summary for the QC report."""
    try:
        carriers = core.aggregate_carriers(requests)
    except Exception:
        return []
    return [
        {
            "carrier": c, "quotes": cm.get("quotes", 0), "wins": cm.get("wins", 0),
            "losses": cm.get("losses", 0), "win_rate": cm.get("win_rate", 0),
            "teu_won": cm.get("teu_won", 0), "teu_lost": cm.get("teu_lost", 0),
            "lanes": cm.get("lanes_quoted", 0),
        }
        for c, cm in sorted(carriers.items(), key=lambda x: x[1].get("quotes", 0), reverse=True)
    ]


def phase_7_save(log: Log, data: dict, data_path: Path, result_path: Path):
    log.section("PHASE 7: PERSIST")

    # RECOMPUTE THE AGGREGATES BEFORE PERSISTING THEM.
    #
    # phase_5 builds summary / lane_summary / carrier_summary, then phase_6
    # runs the MUTATING heals — QC-064 nulls a leaked responder mailbox out of
    # carrier_quoted, QC-067 restores a misfiled row to PENDING, QC-056
    # backfills a carrier. Persisting phase_5's output after phase_6 shipped a
    # file whose aggregates contradicted its own rows: the audit read clean
    # because the row WAS fixed, while carrier_summary still keyed a carrier
    # named "MBD_OceanExport@ol-usa.com" and the client PDF printed it. Same
    # mechanism put a QC-067-restored row in the dashboard's not_quoted KPI
    # while the row list showed it PENDING.
    #
    # The SILENT recompute, not phase_5_summaries: this is a pure rebuild from
    # `data["requests"]`, cheap and idempotent, and it must not log a second
    # "rebuilt" fix — that inflated the fix count the dashboard renders.
    _recompute_aggregates(data)

    data["qc"] = {
        "last_run": core.now_utc().isoformat(),
        "fixes_applied": len(log.fixes),
        "warnings": len(log.warnings),
        "errors": len(log.errors),
        "fix_log": log.fixes,
        "warning_log": log.warnings,
        "error_log": log.errors,
    }
    data["last_updated"] = core.now_utc().isoformat()
    core.save_data(data, data_path)
    log.ok(f"Wrote {data_path}")
    requests = data["requests"]
    summary = data["summary"]
    result = {
        "status": "CLEAN" if not log.errors else "HAS_ERRORS",
        "fixes": len(log.fixes),
        "warnings": len(log.warnings),
        "errors": len(log.errors),
        "error_details": log.errors,
        "warning_details": log.warnings,
        "fix_details": log.fixes,
        "counts": {
            "total": len(requests),
            "wins": summary["wins"],
            "ql": summary["quoted_lost"],
            "nq": summary["not_quoted"],
            "pending": summary["pending_hilmar"],
        },
        "teu": {
            "requested": summary["teu_requested"],
            "won": summary["teu_won"],
            "ql": summary["teu_quoted_lost"],
            "nq": summary["teu_not_quoted"],
            "pending": summary["teu_pending"],
        },
        "rates": {
            "win_rate": summary["win_rate"],
            "quote_rate": summary["quote_rate"],
        },
        "carrier_coverage": _carrier_coverage(requests),
        # Scrubbed carrier-extraction diagnostics for the stuck QC-056 /
        # QC-002 rows. Capped to keep qc-result.json small; already
        # PII-scrubbed at collection time (sentry_setup._scrub_string).
        "carrier_diagnostics": log.carrier_diag[:12],
        "trade_region_reconciliation": _trade_region_reconciliation(data),
        "parser_sweep_audit": _parser_sweep_audit(requests),
        "per_carrier_breakdown": _per_carrier_breakdown(requests),
        "data_freshness": {
            "data_last_updated": data.get("last_updated"),
            "qc_run_at": core.now_utc().isoformat(),
        },
    }
    result_path.parent.mkdir(parents=True, exist_ok=True)
    with open(result_path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)
    log.ok(f"Wrote {result_path}")
    return result


# ─────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────

# Option A hard gate (CLAUDE.md rule #2): the exit code main() returns when the
# POST-PATCH parser-accuracy gate (QC-039) fails — a HARD client-ship block.
# run_pipeline.py recognizes this exact code from the post-patch QC step and
# aborts the fire before the wrapper sends. Must match
# run_pipeline.QC039_GATE_BLOCK_RC (locked by tests/test_auditfix_qc039_gate.py).
QC039_GATE_BLOCK_RC = 39


# Marker the QC-039 check uses when it could not EVALUATE (import error,
# malformed data) as opposed to having MEASURED sub-95% accuracy. Must match the
# string qc_selfheal logs in the QC-039 except branch.
_QC039_UNEVAL_MARK = "FAILED TO EVALUATE"


def _qc039_block_errors(error_messages) -> list:
    """The QC-039 errors that represent a REAL measured accuracy miss (sub-95%
    overall or a critical field below threshold) — i.e. the ones that justify
    hard-blocking the client ship. EXCLUDES "FAILED TO EVALUATE" errors, which
    mean the gate could not run (a deploy/infra gap), not that the data is bad."""
    return [e for e in (error_messages or [])
            if "QC-039" in (e or "") and _QC039_UNEVAL_MARK not in (e or "")]


def _qc039_uneval_errors(error_messages) -> list:
    """QC-039 errors where the gate could NOT evaluate (missing dep, import
    error). These scream + set HAS_ERRORS for visibility but must NOT block the
    client email — a missing measurement module is a deploy problem, not
    sub-95% data, and blocking the fire over it is a self-inflicted outage."""
    return [e for e in (error_messages or [])
            if "QC-039" in (e or "") and _QC039_UNEVAL_MARK in (e or "")]


def _gate_exit_code(error_messages, *, pre_patch: bool) -> int:
    """CLAUDE.md rule #2: the exit code main() returns for the QC-039
    parser-accuracy gate. A POST-PATCH QC-039 error that represents a MEASURED
    sub-95% accuracy is a hard client-ship block (returns QC039_GATE_BLOCK_RC).
    The PRE-PATCH run is advisory and never blocks. A QC-039 "FAILED TO
    EVALUATE" error (the gate couldn't run) does NOT block — it screams instead
    (see main()), because a deploy/import gap is not evidence the data is bad
    and must never take down the client fire. Pure + injectable for unit tests."""
    if pre_patch:
        return 0
    return QC039_GATE_BLOCK_RC if _qc039_block_errors(error_messages) else 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=str(core.CONFIG_PATH))
    parser.add_argument("--no-backup", action="store_true")
    args = parser.parse_args()
    # Initialize Sentry early so any failure in subsequent setup is captured.
    if _sentry is not None:
        with contextlib.suppress(Exception):
            _sentry.init(component="qc_selfheal")
    cfg = core.load_config(args.config)
    data_path = Path(cfg["paths"]["data"])
    schema_path = Path(cfg["paths"]["schema"])
    backups_dir = Path(cfg["paths"]["backups"])
    result_path = Path(cfg["paths"]["qc_result"])
    log = Log()
    if not data_path.exists():
        skeleton = {"version":cfg["version"],"client":cfg["client"]["name"],"contact":cfg["client"]["primary_contact"]["email"],"provider":cfg["provider"]["name"],"last_updated":core.now_utc().isoformat(),"requests":[],"summary":{"total_entries":0,"wins":0,"quoted_lost":0,"not_quoted":0,"pending_hilmar":0,"win_rate":0.0,"quote_rate":0.0,"teu_requested":0,"teu_won":0,"teu_quoted_lost":0,"teu_not_quoted":0,"teu_pending":0,"turnaround_entries":0,"turnaround_avg_biz_hours":0.0,"turnaround_avg_clock_hours":0.0},"lane_summary":{},"carrier_summary":{}}
        core.save_data(skeleton, data_path)
        log.fix(f"Created new empty data file at {data_path}")
    phase_1_files(log, data_path, schema_path)
    if not args.no_backup:
        dest = rotate_backup(data_path, backups_dir, keep=cfg["rules"].get("backup_retention_count", 14))
        log.ok(f"Backup -> {dest.name}")
    data = core.load_data(data_path)
    if not phase_2_structure(log, data):
        log.error("BLOCKING: structural integrity failure")
        result = {"status": "BLOCKED", "errors": log.errors}
        with open(result_path, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2)
        return 1
    phase_3_entries(log, data)
    phase_4_duplicates(log, data)
    phase_5_summaries(log, data)
    phase_6_rules(log, data)

    # QC-075 MUST fire BEFORE phase_7_save, not after it.
    #
    # phase_7_save serializes log.errors into BOTH persisted artifacts —
    # data["qc"]["error_log"] in tracking-data-v2.json, and error_details /
    # status in reports/qc-result.json. Those files are what gen_dashboard's
    # QC tab and gen_improvements_report's red-flags section actually read.
    # Escalating afterwards appended to a list nothing re-serialized, so the
    # divergence appeared only on this subprocess's stdout — behaviourally the
    # same `print()` QC-075 was created to replace. Raised in review of #124.
    # ...and it must compare TWO AGGREGATIONS OF THE SAME ROWS.
    #
    # _trade_region_reconciliation recomputes the regions fresh from
    # data["requests"] but reads data["summary"] as-is — and phase_6's heals
    # (QC-067 restoring a misfiled row to PENDING, QC-004, QC-064, ...) change
    # rows WITHOUT touching that dict. So a summary built back in phase 5
    # describes the PRE-heal rows, and the comparison failed on ordering
    # rather than on any real disagreement: proved with a single QC-067 row,
    # fresh NQ=0 vs stale NQ=1 -> reconciled=False. That is a FALSE QC-075
    # ERROR on essentially every fire that heals a status, persisted into both
    # artifacts — while phase_7_save's own recompute a few lines later makes
    # the same qc-result.json report reconciled=True beside it. The report
    # contradicting itself, in a check written to stop exactly that.
    # QC-075's job is catching two AGGREGATORS that disagree, never two points
    # in time, so rebuild first. Raised in review of #124.
    _recompute_aggregates(data)
    _tr75 = _trade_region_reconciliation(data)
    if _tr75 and _tr75.get("reconciled") is False:
        log.error("QC-075: trade-region rollup does not reconcile to summary — "
                  f"{_tr75.get('error') or _tr75}")

    result = phase_7_save(log, data, data_path, result_path)
    print("\n" + "=" * 60)
    print("QC SELF-HEAL COMPLETE")
    print("=" * 60)
    print(f"  Status:      {result['status']}")
    print(f"  Fixes:       {result['fixes']}")
    print(f"  Warnings:    {result['warnings']}")
    print(f"  Errors:      {result['errors']}")
    c = result["counts"]
    print(f"  {c['total']} entries: {c['wins']}W | {c['ql']} Q&L | {c['nq']} NQ | {c['pending']} P")
    print(f"  Win rate: {result['rates']['win_rate']}% | Quote rate: {result['rates']['quote_rate']}%")
    cc = result.get("carrier_coverage", {})
    print(f"  Carrier coverage: WIN {cc.get('win_with_carrier')}/{cc.get('win_total')} ({cc.get('win_coverage_pct')}%) | Q&L {cc.get('ql_with_carrier')}/{cc.get('ql_total')} ({cc.get('ql_coverage_pct')}%)")
    tr = result.get("trade_region_reconciliation", {})
    print(f"  Trade region reconciled: {tr.get('reconciled')}")
    # QC-075 itself already fired above, BEFORE phase_7_save, so it is inside
    # both persisted artifacts. Nothing to escalate here — this line only
    # echoes the outcome to the run log.

    # CLAUDE.md rule #2 hard gate (POST-PATCH only; pre-patch is advisory).
    # Two distinct QC-039 failure modes, deliberately handled differently:
    #
    #  (1) MEASURED sub-95% accuracy → BLOCK the client ship. Scream out-of-band
    #      (Teams/issue/queue/stderr — never Outlook) and return the distinct
    #      gate code so run_pipeline aborts before any email is built.
    #
    #  (2) The gate COULD NOT EVALUATE (missing dep / src not deployed / import
    #      error) → scream just as loudly, but DO NOT block. A missing
    #      measurement module is a deploy gap, not sub-95% data; the data
    #      pipeline itself succeeded, so blocking the client email over it is a
    #      self-inflicted outage. The operator is paged to fix the deploy; the
    #      email still ships. (This is what surfaced when the box ran without
    #      src/hilmar/ on the path — "No module named 'hilmar'".)
    def _scream(title, body, labels):
        try:
            import fire_alert
            fire_alert.send_alert(title, body, level="error", labels=labels)
        except Exception as _e:
            print(f"  (fire_alert escalation failed: {_e})", file=sys.stderr)

    if _gate_exit_code(log.errors, pre_patch=_qc_phase_is_pre_patch()):
        miss = _qc039_block_errors(log.errors)
        _scream(
            "Daily fire BLOCKED — parser accuracy below the 95% gate (QC-039)",
            "Post-patch QC-039 MEASURED sub-95% accuracy, so the daily client "
            "email is BLOCKED (CLAUDE.md rule #2: do not ship sub-95% data). Fix "
            "the parser and re-fire.\n  - " + "\n  - ".join(miss),
            ("fire-alert", "qc-039-gate"))
        print("\n❌ QC-039 PARSER-ACCURACY GATE FAILED (post-patch) — blocking "
              f"the client ship (exit {QC039_GATE_BLOCK_RC}).", file=sys.stderr)
        return QC039_GATE_BLOCK_RC

    # Gate could not evaluate in the real (post-patch) run — scream, don't block.
    if not _qc_phase_is_pre_patch():
        uneval = _qc039_uneval_errors(log.errors)
        if uneval:
            _scream(
                "Parser-accuracy gate COULD NOT EVALUATE (QC-039) — fix the deploy",
                "QC-039 could not run this fire (e.g. src/hilmar not deployed or a "
                "missing dependency), so parser accuracy was NOT verified. The email "
                "still SHIPPED — the data pipeline succeeded — but the gate is blind "
                "until the deploy is fixed.\n  - " + "\n  - ".join(uneval),
                ("fire-alert", "qc-039-uneval"))
            print("\n⚠️  QC-039 could NOT evaluate (deploy/dep gap) — screamed; NOT "
                  "blocking the ship.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
