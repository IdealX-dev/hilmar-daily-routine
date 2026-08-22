"""OL answers one RFQ with SEVERAL options. Read them all, pick deliberately.

Michael, 2026-08-20, on the OL-USA RESPONSES table: "also the numbers are
wrong for $" — then, naming the cause himself: "could be different rates for
different steamship lines".

He was right, and the diagnostic (run 32413391384, real bodies pulled from the
blob store) showed all three shapes at once:

  different steamship lines   Oakland->Xingang   $810 ONE via Pusan
                                                 $675 CMA direct
  different sailings, one line  Oakland->Yokohama  PRESIDENT LB JOHNSON
                                                   PRESIDENT REAGAN, same $3,010
  a DIFFERENT LANE pasted in   Oakland->Haiphong  $555 ONE  -> Haiphong
                                                  $740 CMA  -> SHANGHAI

``_find_table_rows`` returned ``[header, first_data_row]`` and stopped, so the
parser saw option 1 and nothing else. In two of the four multi-option bodies
that week the option it discarded was the CHEAPER one — and in the Haiphong
body the row it did not read was a different destination's price, held off the
quote by nothing but the order Maria happened to type.

The grids below are the real rows from that diag run. The trailing free-time
column is truncated exactly where the capture truncated it; every value these
tests assert on (rate, carrier, vessel, ETA, POD) is verbatim.
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

import body_parser as BP  # noqa: E402
import core  # noqa: E402

from hilmar import body_parser as HBP  # noqa: E402

HEADER = ("POL | POD | Container Size | Vessel | Voyage | ERD | Doc Cut | "
          "Port Cut | ETD | ETA | RATE | CARRIER | TRANSSHIPMENT | "
          "DESTINATION FREE TIME")

# Oakland -> Xingang, 2026-08-19. Two steamship lines; the cheaper one is also
# the direct one. Production stored $810.
XINGANG = f"""Hello Lonny,

Please see below:

{HEADER}
OAKLAND | XINGANG | 1x40'HC | HMM TURQUOISE | 011W | 28-Aug-26 | 2-Sep-26 | 4-Sep-26 | 9-Sep-26 | 2-Oct-26 | $810 | ONE | VIA PUSAN | 14 DETENTION
OAKLAND | XINGANG | 1x40'HC | EVER LEGACY | 0TBOGW1MA | 2-Sep-26 | 4-Sep-26 | 5-Sep-26 | 7-Sep-26 | 22-Sep-26 | $675 | CMA | DIRECT | 4 DETENTION

Best regards,
Maria Machado
Ocean Export Specialist
"""

# Oakland -> Algeciras, 2026-08-19. Production stored $4,938; OL had also
# offered $4,201, arriving six days earlier.
ALGECIRAS = f"""{HEADER}
OAKLAND | ALGECIRAS | 40' HC | NYK RIGEL | 0CLNEE1MA | 8-Sep-26 | 10-Sep-26 | 11-Sep-26 | 15-Sep-26 | 24-Oct-26 | $4,938.00 | CMA | LE HARVE | 4 DETENTION
OAKLAND | ALGECIRAS | 40' HC | MAERSK KANSAS | 639E | 4-Sep-26 | 15-Sep-26 | 7-Sep-26 | 21-Sep-26 | 18-Oct-26 | $4,201.00 | HAPAG | HOUSTON, TX & ANTWERP | 4 DETENTION
"""

# Oakland -> Haiphong, 2026-08-19. Row 2 is a SHANGHAI quote.
HAIPHONG = f"""Please see option below:

{HEADER}
OAKLAND | Haiphong | 2x20' | WAN HAI A05 | W017 | 28-Aug-26 | 3-Sep-26 | 4-Sep-26 | 8-Sep-26 | 29-Sep-26 | $555 | ONE | DIRECT | 14 DETENTION
OAKLAND | SHANGHAI | 2x20' | APL DANUBE | 0M70JW1MA | 01-Sep-26 | 3-Sep-26 | 4-Sep-26 | 11-Sep-26 | 5-Oct-26 | $740 | CMA | DIRECT | 4 DETENTION

Best regards,
Maria Machado
"""

# Oakland -> Xingang 8x20', 2026-08-19. ONE option, and it must stay exactly
# the shape it has always been.
SINGLE = f"""{HEADER}
OAKLAND | XINGANG | 8x20' | HYUNDAI PLUTO | 047W | 26-Aug-26 | 27-Aug-26 | 31-Aug-26 | 3-Sep-26 | 20-Sep-26 | $745 | ONE | DIRECT | 14 DETENTION
"""


def test_the_second_option_is_read_at_all():
    """The whole defect in one assertion."""
    out = BP.parse_rate_table(XINGANG)
    assert "rate_options" in out, (
        "Only one option came back from a two-option OL reply — the parser is "
        "still stopping at the first data row.")
    assert [o["ol_rate"] for o in out["rate_options"]] == [810.0, 675.0]
    assert [o["carrier_quoted"] for o in out["rate_options"]] == ["ONE", "CMA CGM"]


def test_headline_is_the_best_rate_ol_offered_not_the_first_typed():
    assert BP.parse_rate_table(XINGANG)["ol_rate"] == 675.0
    assert BP.parse_rate_table(ALGECIRAS)["ol_rate"] == 4201.0


def test_every_field_comes_from_the_row_that_won():
    """A quote may never pair one sailing's price with another's schedule."""
    out = BP.parse_rate_table(XINGANG)
    assert out["ol_rate"] == 675.0
    assert out["carrier_quoted"] == "CMA CGM"
    assert out["vessel"] == "EVER LEGACY"
    assert out["voyage"] == "0TBOGW1MA"
    assert out["eta_offered"] == "22-Sep-26"
    assert out["transshipment"] == "DIRECT"

    alg = BP.parse_rate_table(ALGECIRAS)
    assert alg["vessel"] == "MAERSK KANSAS"
    assert alg["eta_offered"] == "18-Oct-26"
    assert alg["carrier_quoted"] == "Hapag-Lloyd"


def test_a_row_for_another_destination_never_prices_this_lane():
    out = BP.parse_rate_table(HAIPHONG)
    assert out["pod"] == "Haiphong"
    assert out["ol_rate"] == 555.0, (
        "The Shanghai row's $740 reached a Haiphong quote. Row order was the "
        "only thing keeping it out.")
    assert out.get("other_lane_pods") == ["SHANGHAI"]
    # One surviving option means no choice to show — the old shape stands.
    assert "rate_options" not in out


def test_a_single_option_grid_is_untouched():
    out = BP.parse_rate_table(SINGLE)
    assert out["ol_rate"] == 745.0
    assert out["carrier_quoted"] == "ONE"
    assert "rate_options" not in out
    assert "other_lane_pods" not in out


def test_the_nra_footer_is_not_an_option():
    """OL's legal footer is pipe-shaped. It must not become a $0 option."""
    body = SINGLE + (
        "\nTHE SHIPPER'S BOOKING OF CARGO AFTER RECEIVING THE TERMS OF THIS "
        "NRA CONSTITUTES\nACCEPTANCE OF THE RATES AND TERMS OF THIS NRA "
        "AMENDMENT. | | |\n")
    out = BP.parse_rate_table(body)
    assert "rate_options" not in out
    assert out["ol_rate"] == 745.0


def test_a_rule_row_is_not_an_option():
    body = HEADER + "\n|---|---|---|---|---|---|---|---|---|---|---|---|---|---|\n" + \
        SINGLE.split("\n", 1)[1]
    out = BP.parse_rate_table(body)
    assert out["ol_rate"] == 745.0
    assert "rate_options" not in out


def test_prose_and_empty_bodies_still_return_the_old_way():
    assert BP.parse_rate_table("") == {}
    assert BP.parse_rate_table("no table here at all, thanks") == {}


@pytest.mark.parametrize("body,expected", [
    (XINGANG, 675.0), (ALGECIRAS, 4201.0), (HAIPHONG, 555.0), (SINGLE, 745.0),
])
def test_both_trees_choose_the_same_option(body, expected):
    """src/hilmar keeps its own date contract but must not pick a different
    option — that divergence is exactly what the parity guard exists for."""
    assert BP.parse_rate_table(body)["ol_rate"] == expected
    assert HBP.parse_rate_table(body)["ol_rate"] == expected


def test_find_table_rows_still_shows_only_the_first_row():
    """The narrow view is unchanged for the callers and tests that pin it."""
    rows = BP._find_table_rows(XINGANG)
    assert rows is not None and len(rows) == 2
    assert "HMM TURQUOISE" in rows[1]
    block = BP._find_table_block(XINGANG)
    assert len(block) == 3


# ── The choice has to reach Michael's screen, not just the JSON ──────────────

def _quoted_row(**over):
    """A row shaped the way ingest writes an OL response, stamped on the day
    the report actually covers.

    ANCHORED TO _report_date, NOT TO "now". The first version of this helper
    used `now - 3h`, which lands on the report day only when the suite happens
    to run during ET business hours — it passed when written on a Friday
    afternoon and failed the same evening once the clock rolled past midnight
    ET into the next report window. A test whose result depends on the wall
    clock is not a test; it is a coin flip that reports as green most of the
    time, which is worse than red.
    """
    import gen_email as _GE
    _rd = _GE._report_date(datetime.now(timezone.utc).astimezone(core.ET))
    sent = datetime(_rd.year, _rd.month, _rd.day, 14, 0,
                    tzinfo=timezone.utc)   # midday ET on the reported day
    row = {
        "request_id": "req_multi", "lane": "Oakland → Xingang",
        "origin": "Oakland", "destination": "Xingang",
        "containers": "1-40' HC", "teu_requested": 2,
        "status": "LOSS", "quoted": True,
        "carrier_quoted": "CMA CGM", "ol_rate": 675.0,
        "ol_responder_signer": "Maria Machado",
        "request_timestamp": (sent - timedelta(hours=5)).isoformat(),
        "response_timestamp": sent.isoformat(),
        "etd_offered": "7-Sep-26", "eta_offered": "22-Sep-26",
        "rate_options": [
            {"ol_rate": 810.0, "carrier_quoted": "ONE", "vessel": "HMM TURQUOISE"},
            {"ol_rate": 675.0, "carrier_quoted": "CMA CGM", "vessel": "EVER LEGACY"},
        ],
    }
    row.update(over)
    return row


def _daily_html(rows):
    import gen_email as GE
    return GE.build_body({"requests": rows}, {})


def test_the_staff_report_shows_the_option_that_was_beaten():
    html = _daily_html([_quoted_row()])
    assert "$675" in html
    assert "also $810 ONE" in html, (
        "The rate cell shows one number and says nothing about the $810 ONE "
        "option in the same email — which is exactly how the first defect "
        "stayed invisible.")


def test_a_single_option_row_says_nothing_extra():
    html = _daily_html([_quoted_row(rate_options=None, ol_rate=745.0,
                                    carrier_quoted="ONE")])
    assert "$745" in html
    assert "also $" not in html


def _qc_messages(rows, capsys):
    """Everything phase 6 SAID, not just what it stored.

    Log.ok appends nothing — it only prints (the QC-077 lesson, 2026-08-19).
    So the audit line a passing check emits is only visible on stdout, and a
    test that reads log.errors/log.warnings alone cannot tell "the check
    passed" from "the check never ran".
    """
    import qc_selfheal as QC
    QC.phase_6_rules(QC.Log(), {"requests": rows})
    return capsys.readouterr().out


def test_qc079_names_a_row_that_kept_the_beaten_rate(capsys):
    """The invariant on the real dataset: five writers, one headline rule."""
    text = _qc_messages([_quoted_row(ol_rate=810.0)], capsys)
    assert "QC-079" in text, "QC-079 did not run"
    assert "best offered $675" in text, (
        f"QC-079 did not name the row that stored the beaten rate:\n{text}")


def test_qc079_is_quiet_when_the_best_rate_is_the_stored_one(capsys):
    text = _qc_messages([_quoted_row()], capsys)
    assert "QC-079" in text
    assert "best offered" not in text


def test_qc079_names_the_reply_that_priced_another_destination(capsys):
    text = _qc_messages([_quoted_row(rate_options=None,
                                     other_lane_pods=["SHANGHAI"])], capsys)
    assert "ALSO priced a different destination" in text
    assert "SHANGHAI" in text


# ── Linda's template, and the ETA that was Lonny's own ask ───────────────────
#
# Michael, 2026-08-20, on the same report: "important data still missing" —
# a Shanghai row with ETA Offered "—". Diag run 32493969967 printed Linda's
# header verbatim and three of its columns named nothing the parser knew:
#
#   Port of loading | Port of discharge | Container Size | Vessel | Voyage |
#   ERD | Doc Cutoff | Cutoff | Sail | Arrive | RATE | CARRIER | ...
#
# "Doc Cutoff", "Cutoff" and "Arrive" were all unmapped, so on every quote she
# sends, the doc cutoff, the port cutoff and the ETA were read out of columns
# the parser could not name and dropped on the floor.

LINDA_HEADER = ("Port of loading | Port of discharge | Container Size | "
                "Vessel | Voyage | ERD | Doc Cutoff | Cutoff | Sail | "
                "Arrive | RATE | CARRIER | TRANSSHIPMENT | ORIGIN FREE TIME "
                "| DESTINATION FREE TIME")

# The real 2026-08-19 Algeciras reply, with Lonny's RFQ quoted beneath it —
# the shape every OL reply has.
ALGECIRAS_FULL = f"""{LINDA_HEADER}
OAKLAND | ALGECIRAS | 40' HC | NYK RIGEL | 0CLNEE1MA | 8-Sep-26 | 10-Sep-26 | 11-Sep-26 | 15-Sep-26 | 24-Oct-26 | $4,938.00 | CMA | LE HARVE | 4 DETENTION + 5 DEMURRAGE FREE DAYS | 7 COMBINED FREE DAYS
OAKLAND | ALGECIRAS | 40' HC | MAERSK KANSAS | 639E | 4-Sep-26 | 15-Sep-26 | 7-Sep-26 | 21-Sep-26 | 18-Oct-26 | $4,201.00 | HAPAG | HOUSTON, TX & ANTWERP | 4 DENTION FREE DAYS | 7 COMBINED FREE DAYS

Linda Echevarria
Ocean Export Manager
email: Linda.Echevarria@ol-usa.com

From: Lonny Upfold
Sent: Tuesday, August 19, 2026
Subject: Oakland to Algeciras

1-40' HC
Oakland to Algeciras
ETA 10/21
Product Protein
Thanks,
Lonny Upfold
"""


def test_lindas_column_names_are_understood():
    """Every header cell of her template must name a field, or the value
    under it is silently discarded."""
    unmapped = [c.strip() for c in LINDA_HEADER.split("|")
                if BP._header_key(c.strip()) is None]
    assert not unmapped, (
        f"These columns of Linda's grid name nothing, so their values are "
        f"dropped on every quote she sends: {unmapped}")


def test_the_decoy_columns_are_still_refused():
    """Widening the alias list must not re-open the decoy hole: a header the
    parser only half-understands stays unmapped."""
    for decoy in ("Terminal Operator", "Service Line", "Rate Notes"):
        assert BP._header_key(decoy) is None, decoy


def test_the_eta_comes_from_ols_grid_not_lonnys_ask():
    out = BP.parse_rate_table(ALGECIRAS_FULL)
    assert out["ol_rate"] == 4201.0
    assert out["eta_offered"] == "18-Oct-26", (
        "OL's Arrive column was dropped, so the row falls back to prose — "
        "and the prose under an OL reply is Lonny's RFQ.")
    assert out["etd_offered"] == "21-Sep-26"
    assert out["doc_cutoff"] == "15-Sep-26"
    assert out["port_cutoff"] == "7-Sep-26"


def test_fetch_bodies_never_reads_the_offered_eta_out_of_the_quoted_ask():
    """The end-to-end shape: an OL reply whose grid has NO eta column at all.
    The fallback may read OL's own prose and nothing below the chain marker."""
    import fetch_bodies as FB
    body = (
        "Hello Lonny, please see below:\n"
        "POL | POD | Container Size | Vessel | RATE | CARRIER\n"
        "OAKLAND | ALGECIRAS | 40' HC | NYK RIGEL | $4,938.00 | CMA\n"
        "\nLinda Echevarria\n"
        "\nFrom: Lonny Upfold\nSent: Tuesday, August 19, 2026\n"
        "Oakland to Algeciras\nETA 10/21\nThanks,\nLonny Upfold\n")
    parsed = FB._parse_all(body, "RE: Oakland to Algeciras",
                           "mbd_rate_response")
    assert parsed.get("eta_offered") is None, (
        f"Lonny's requested ETA came back as OL's offered ETA: "
        f"{parsed.get('eta_offered')!r}")
