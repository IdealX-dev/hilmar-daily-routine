"""POL/POD completeness — 2026-06-17 recurring QC-027 ERROR.

QC-027 fired ERROR daily (POL=87% / POD=87% < 90%) and never self-healed:
ingest parsed pol/pod from the OL rate table but never wrote them onto the
row, and the check only MEASURED — nothing populated them. Fixed at intake
(ingest._derive_ports: POD = destination always; POL = origin when it's a
seaport) with OL's stated ports overriding, plus a QC-027 self-heal for rows
already in tracking-data, plus parse_rate_table column aliases (both trees).
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import body_parser as SBP  # noqa: E402  (production tree)
import ingest as SI  # noqa: E402
import qc_selfheal as q  # noqa: E402

from hilmar import body_parser as HBP  # noqa: E402


# ── intake derivation ──────────────────────────────────────────────────────
def test_derive_ports_seaport_origin():
    assert SI._derive_ports("Oakland", "Yokohama") == ("Oakland", "Yokohama")
    assert SI._derive_ports("Oakland, CA", "Osaka") == ("Oakland, CA", "Osaka")


def test_derive_ports_inland_origin_leaves_pol_blank():
    # Dalhart is inland — POL is a gateway seaport we can't guess, so leave it.
    pol, pod = SI._derive_ports("Dalhart", "Hamburg")
    assert pol is None
    assert pod == "Hamburg"


def test_derive_ports_pod_always_destination():
    assert SI._derive_ports(None, "Busan") == (None, "Busan")
    assert SI._derive_ports("", "") == (None, None)


# ── parse_rate_table POL/POD column aliases (production pipe-table parser) ──
def test_relabeled_port_columns_scripts():
    text = (
        "Port of Loading | Port of Discharge | RATE | ETD | ETA | CARRIER\n"
        "Oakland | Yokohama | $3000 | 7/8 | 7/20 | CMA"
    )
    rt = SBP.parse_rate_table(text)
    assert rt.get("pol") == "Oakland"
    assert rt.get("pod") == "Yokohama"


def test_relabeled_port_labels_src_positional():
    # src/hilmar uses a positional label→value parser; its _TABLE_LABELS now
    # maps the relabeled port headers to pol/pod.
    assert HBP._TABLE_LABELS.get("PORT OF LOADING") == "pol"
    assert HBP._TABLE_LABELS.get("PORT OF DISCHARGE") == "pod"


# ── QC-027 self-heal fills missing POL/POD from the lane ────────────────────
def _base(requests):
    return {"version": "2", "requests": requests,
            "summary": {"wins": 0, "quoted_lost": 0, "not_quoted": 0,
                        "pending_hilmar": 0}}


def test_qc027_selfheals_missing_pol_pod():
    # An active, reachable WIN row with no pol/pod → should be healed in place.
    reqs = [{"request_id": "r1", "status": "WIN", "quoted": True,
             "origin": "Oakland", "destination": "Yokohama",
             "response_timestamp": "2026-06-16T10:00:00Z", "ol_rate": 3000}]
    data = _base(reqs)
    q.phase_6_rules(q.Log(), data)
    assert data["requests"][0]["pol"] == "Oakland"
    assert data["requests"][0]["pod"] == "Yokohama"


def test_qc027_selfheal_inland_pod_only():
    reqs = [{"request_id": "r1", "status": "LOSS", "quoted": True,
             "origin": "Dalhart", "destination": "Hamburg",
             "response_timestamp": "2026-06-16T10:00:00Z", "ol_rate": 2800}]
    data = _base(reqs)
    q.phase_6_rules(q.Log(), data)
    assert data["requests"][0]["pod"] == "Hamburg"     # always derivable
    assert not data["requests"][0].get("pol")          # inland → left for OL
