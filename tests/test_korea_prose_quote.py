"""OL prose-format rate quote — 2026-06-24 Busan/Korea miss.

Michael forwarded an OL quote chain ("RE: Updated Cheese Rates Busan Korea
from Dalhart") and said the report showed "lane unresolved" / not-quoted.
Two root causes, both pinned here across BOTH parser trees:

  1. The Lonny RFQ subject is "...<DEST> <region> from <ORIGIN>"
     ("Busan Korea from Dalhart") rather than "<ORIGIN> to <DEST>".
     parse_subject_lane returned (None, None), so ingest.build_requests
     DROPPED the request row entirely (no parseable destination).

  2. OL quoted in free PROSE, not the pipe/column grid:

         Please see able Hapag option from Houston port to Busan.
         Houston to Busan _ 40' Reefer _ Chilled Cheese
         Hapag: $2,275/40' reefer
         4 equipment free days at Origin
         3 equipment free days at destination
         Direct service

     The production scripts/ parse_rate_table had NO prose path at all (pure
     drift from src/hilmar) and returned {} — the quote vanished. The bare
     carrier token "Hapag" also wasn't recognised.

Both trees must now resolve the lane from the subject AND fully extract the
prose quote (carrier normalized, rate, POL/POD, container, free time, direct).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import body_parser as SBP  # noqa: E402  (production tree)

from hilmar import body_parser as HBP  # noqa: E402

_TREES = (SBP, HBP)
_IDS = ("scripts", "hilmar")

# The OL prose quote body, exactly as it decodes from the .eml (the '
# is U+2019, OL's smart apostrophe in "40' reefer").
_KOREA_BODY = (
    "Hello Lonny,\n\n"
    "Please see able Hapag option from Houston port to Busan. We are pending "
    "additional carrier options and will advise as they come in.\n\n"
    "Houston to Busan _ 40’ Reefer _ Chilled Cheese\n"
    "Hapag: $2,275/40’ reefer\n"
    "Subject to destination charges\n"
    "4 equipment free days at Origin\n"
    "3 equipment free days at destination\n"
    "Direct service"
)

_SUBJECT = "RE: Updated Cheese Rates Busan Korea from Dalhart"


# ── 1. Subject lane: "...DEST <region> from ORIGIN" ────────────────────────
@pytest.mark.parametrize("BP", _TREES, ids=_IDS)
def test_subject_lane_dest_region_from_origin(BP):
    origin, dest = BP.parse_subject_lane(_SUBJECT)
    assert origin == "Dalhart", (origin, dest)
    assert dest == "Busan", (origin, dest)


@pytest.mark.parametrize("BP", _TREES, ids=_IDS)
def test_subject_from_fallback_does_not_shadow_normal_lane(BP):
    # The normal "<ORIGIN> to <DEST>" form must be untouched by the fallback.
    assert BP.parse_subject_lane("Oakland to Yokohama 2x40HC") == ("Oakland", "Yokohama")


@pytest.mark.parametrize("BP", _TREES, ids=_IDS)
def test_subject_from_fallback_bails_on_no_real_dest(BP):
    # "from <X>" with only generic words before it must NOT invent a lane.
    assert BP.parse_subject_lane("Quote from Oakland") == (None, None)
    assert BP.parse_subject_lane("Need rates from Lonny") == (None, None)


# ── 2. Bare "Hapag" carrier token ──────────────────────────────────────────
@pytest.mark.parametrize("BP", _TREES, ids=_IDS)
def test_bare_hapag_token(BP):
    assert BP.detect_carrier_token("Hapag: $2,275") == "Hapag-Lloyd"
    assert BP.detect_carrier_token("able Hapag option from Houston") == "Hapag-Lloyd"


# ── 3. Full prose-quote extraction (both trees) ────────────────────────────
@pytest.mark.parametrize("BP", _TREES, ids=_IDS)
def test_prose_rate_extraction(BP):
    rt = BP.parse_prose_rate(_KOREA_BODY)
    assert rt.get("carrier_quoted") == "Hapag-Lloyd", rt
    assert rt.get("ol_rate") == 2275.0, rt
    assert rt.get("pol") == "Houston", rt
    assert rt.get("pod") == "Busan", rt
    assert "40" in (rt.get("container_size") or "") and \
        "Reefer" in (rt.get("container_size") or ""), rt
    assert rt.get("origin_free_time") == "4 days", rt
    assert rt.get("dest_free_time") == "3 days", rt
    assert rt.get("transshipment") == "Direct", rt


@pytest.mark.parametrize("BP", _TREES, ids=_IDS)
def test_parse_rate_table_routes_prose(BP):
    # parse_rate_table is the entry point ingest/fetch_bodies call — it must
    # surface the prose quote when there's no pipe/column table.
    rt = BP.parse_rate_table(_KOREA_BODY)
    assert rt.get("carrier_quoted") == "Hapag-Lloyd", rt
    assert rt.get("ol_rate") == 2275.0, rt
    assert rt.get("pol") == "Houston", rt
    assert rt.get("pod") == "Busan", rt


# ── 4. False-positive guard: prose with NO rate is not a quote ─────────────
@pytest.mark.parametrize("BP", _TREES, ids=_IDS)
def test_prose_without_rate_returns_empty(BP):
    txt = "Please advise on a Hapag option from Houston to Busan when ready."
    assert BP.parse_prose_rate(txt) == {}
