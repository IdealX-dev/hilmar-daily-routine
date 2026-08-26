"""The back-entered bookings carry the date OL's own confirmation names.

Michael, 2026-08-24, on the 18 corrections with no booked date: "then read
them again". They were read on 2026-08-26 (diag-blob run 33019186551) and
10 of the 18 turned out to be evidenced by OL's own booking confirmation.

THE TRAP THIS FILE EXISTS TO HOLD SHUT
--------------------------------------
The cached body's own ``sent_ts`` is NOT the booked date. Those bodies are
FORWARDS, and every one of the ten carries a stamp between 19:59 and 20:23
on 2026-08-13 — one batch forward. Using it would have credited ten
bookings to a single day:

    ref      cached sent_ts (the FORWARD)   quoted Sent: (the BOOKING)
    261025   2026-08-13T20:23Z              Tuesday, August 4, 2026 9:23 AM
    261026   2026-08-13T20:12Z              Monday, August 3, 2026 5:43 PM
    261027   2026-08-13T20:05Z              Monday, August 3, 2026 5:51 PM
    261028   2026-08-13T20:14:12Z           Monday, August 3, 2026 5:57 PM
    261029   2026-08-13T20:11:15Z           Monday, August 3, 2026 6:03 PM
    261030   2026-08-13T20:09:24Z           Monday, August 3, 2026 6:09 PM
    261032   2026-08-13T19:59:04Z           Monday, August 3, 2026 6:19 PM
    261033   2026-08-13T20:16:16Z           Monday, August 3, 2026 6:24 PM
    261046   2026-08-13T20:18:20Z           Wednesday, August 5, 2026 6:12 PM
    261047   2026-08-13T20:01:53Z           Wednesday, August 5, 2026 6:11 PM

The stored value is the QUOTED header, read as ET wall-clock and converted
to UTC (August in America/New_York is EDT, UTC-4).

THE EIGHT LEFT INFERRED, DELIBERATELY
-------------------------------------
    261031  only a CMA CGM carrier notification is cached, not an OL booking
    260469  'DRAFT RATED FOR HILMAR' — a rating email, not a booking
    261072  appears ONLY inside our own daily-tracker emails (circular)
    260433  same — our own tracker / weekly rollup
    260358 260370 260896 261068   in no cached body at all

Stamping those would be fabrication. This file fails if a later session
quietly fills one in without new evidence.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import core  # noqa: E402

CORRECTIONS = ROOT / "scripts" / "operator_corrections.json"

#: ref -> (stored UTC stamp, the ET calendar date the win belongs to)
SOURCED = {
    "261025": ("2026-08-04T13:23:00Z", "2026-08-04"),
    "261026": ("2026-08-03T21:43:00Z", "2026-08-03"),
    "261027": ("2026-08-03T21:51:00Z", "2026-08-03"),
    "261028": ("2026-08-03T21:57:00Z", "2026-08-03"),
    "261029": ("2026-08-03T22:03:00Z", "2026-08-03"),
    "261030": ("2026-08-03T22:09:00Z", "2026-08-03"),
    "261032": ("2026-08-03T22:19:00Z", "2026-08-03"),
    "261033": ("2026-08-03T22:24:00Z", "2026-08-03"),
    "261046": ("2026-08-05T22:12:00Z", "2026-08-05"),
    "261047": ("2026-08-05T22:11:00Z", "2026-08-05"),
}

#: Refs with NO booking-confirmation evidence. Must stay unstamped.
UNSOURCED = {"261031", "260469", "261072", "260433",
             "260358", "260370", "260896", "261068"}

#: The batch-forward stamp. Must never be a stored booking_timestamp.
FORWARD_DAY = "2026-08-13"


def _by_ref():
    doc = json.loads(CORRECTIONS.read_text(encoding="utf-8"))
    out = {}
    for entry in doc["corrections"]:
        changes = entry.get("set") or {}
        ref = str(changes.get("mdolx_ref") or "")
        if ref:
            out[ref] = entry
    return out


def test_corrections_file_is_valid_json_and_intact():
    doc = json.loads(CORRECTIONS.read_text(encoding="utf-8"))
    assert isinstance(doc.get("corrections"), list)
    assert len(doc["corrections"]) >= 84, "corrections went missing"


def test_every_sourced_booking_carries_its_own_date():
    by_ref = _by_ref()
    for ref, (stamp, _day) in SOURCED.items():
        entry = by_ref.get(ref)
        assert entry, f"correction for MDOLX{ref} vanished"
        got = entry["set"].get("booking_timestamp")
        assert got == stamp, f"MDOLX{ref}: {got!r} != {stamp!r}"


def test_no_booking_is_dated_to_the_batch_forward():
    # The whole point. If a session ever re-derives these from the cached
    # body's own sent_ts, every one lands on 2026-08-13 and this fails.
    for ref, entry in _by_ref().items():
        stamp = (entry.get("set") or {}).get("booking_timestamp") or ""
        assert not stamp.startswith(FORWARD_DAY), (
            f"MDOLX{ref} is dated {stamp} — that is the day the confirmation "
            f"was FORWARDED, not the day it was booked. Read the quoted "
            f"Outlook header instead (see this file's docstring).")


def test_win_event_date_credits_each_booking_to_its_own_day():
    # Through the real code path, with a status_history deliberately stamped
    # on the forward day: booking_timestamp must win.
    for ref, (stamp, day) in SOURCED.items():
        row = {
            "status": "WIN",
            "mdolx_ref": ref,
            "booking_timestamp": stamp,
            "status_history": [{"to": "WIN", "at": "2026-08-13T20:14:12Z"}],
        }
        assert core.win_event_date(row) == day, (
            f"MDOLX{ref} credited to {core.win_event_date(row)}, want {day}")


def test_the_ten_do_not_all_land_on_one_day():
    # A sanity check on the shape, not just the values: had sent_ts been
    # used, this set would have size 1.
    days = {day for _stamp, day in SOURCED.values()}
    assert days == {"2026-08-03", "2026-08-04", "2026-08-05"}
    assert len(days) == 3


def test_unsourced_refs_are_left_alone():
    by_ref = _by_ref()
    for ref in UNSOURCED:
        entry = by_ref.get(ref)
        if entry is None:
            continue
        assert not (entry.get("set") or {}).get("booking_timestamp"), (
            f"MDOLX{ref} has no booking-confirmation evidence in the cached "
            f"bodies — a date here would be fabricated. If new evidence has "
            f"arrived, move it into SOURCED with the source recorded.")


def test_the_file_records_where_these_dates_came_from():
    # Each entry's own `note` names the source of the WIN (Linda's recap).
    # The DATE has a different source, and conflating them is how provenance
    # rots — so the file carries its own statement of it.
    doc = json.loads(CORRECTIONS.read_text(encoding="utf-8"))
    why = doc.get("_booked_dates_source") or ""
    assert why, (
        "booking_timestamp values are stored with no recorded provenance — "
        "add _booked_dates_source naming the evidence")
    for needle in ("quoted", "sent_ts", "33019186551", "2026-08-13"):
        assert needle in why.lower(), (
            f"_booked_dates_source does not mention {needle!r}; it must say "
            f"which field was read and which was rejected, or the next "
            f"session repeats the forward-date mistake")
