"""An OL signer is whoever signed, not whoever is on a list.

Michael, 2026-08-20, on 8 of 12 quoted rows showing "—" for Who Responded:
"why are signors missing nothing should be missing" and then, on being told
the name was Maria Machado: "a signor is a signor if new staff comes, new
staff comes. if they change they change.. maria machado is staff then."

THE BUG. parse_signer matched the signature correctly and then THREW THE NAME
AWAY unless it appeared in a 14-entry hardcoded roster. Measured on the real
body (diag-blob run 32382040208):

    Best regards,
    Maria Machado
    Ocean Export Specialist
    Phone: 201.355.2531 ext 832

parsed cleanly and returned None. Linda Echevarria survived only because she
is on the roster AND her block ends "email: Linda.Echevarria@ol-usa.com",
which a separate email-based rule catches. Maria's block has phone, direct,
address and web — no email line — so both paths failed her.

A closed roster means every new OL hire is invisible by construction, and
silently: nothing checks this field (QC-027 covers ETD/ETA/Vessel/Rate/
Carrier/POL/POD, not the signer).

WHY OPENING IT IS SAFE. parse_signer only ever runs on bodies bucketed
mbd_inbound or mbd_rate_response (fetch_bodies.py:231), and refresh_stage
assigns those buckets ONLY when the sender is @ol-usa.com. Michael: "lonny
doesn't sign from an ol email address" — precisely, and the bucket already
enforces it. _BLOCKLIST remains as the second line of defence, because an OL
reply quotes the ask beneath it and Lonny's own sign-off sits in that text.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import core  # noqa: E402

# The real block, from the Aug-19 Oakland → Yokohama reply.
MARIA = """Please see our best rate below.

| OAKLAND | YOKOHAMA | 4x40'RF | PRESIDENT LB JOHNSON | 0DBP2W1MA | 26-Aug-26 |

Best regards,
Maria Machado
Ocean Export Specialist
Phone: 201.355.2531 ext 832
Direct: 212.457.4036
Address: 1 Meadowlands Plaza, Suite 200
East Rutherford, NJ 07073
Web: www.ol-usa.com
"""

LINDA = """Rate below.

Linda Echevarria
Ocean Export Manager
265 Post Avenue, Ste 333
Westbury, NY 11590
Tel: 847-338-2544
email: Linda.Echevarria@ol-usa.com
"""


def test_a_signer_who_is_not_on_the_roster_is_still_the_signer():
    """THE REGRESSION. Reverting the change returns None here."""
    assert core.parse_signer(None, MARIA) == "Maria Machado"


def test_a_roster_signer_still_resolves():
    """Opening the gate must not break the names that already worked, and
    the roster still supplies canonical spelling."""
    assert core.parse_signer(None, LINDA) == "Linda Echevarria"


def test_lonny_is_never_the_ol_signer():
    """He is the CLIENT. His sign-off sits in the quoted chain of every OL
    reply, so if _strip_chain ever fails to cut it, the blocklist must."""
    body = "See below.\n\nThanks,\nLonny Upfold\nLogistics Coordinator\n"
    assert core.parse_signer(None, body) is None


def test_a_team_name_is_not_a_person():
    """The regex matches the line after a sign-off, which is usually the
    person and sometimes a desk."""
    assert core.parse_signer(None, "x\n\nBest regards,\nOcean Export\n") is None
    assert core.parse_signer(None, "x\n\nRegards,\nPricing Team\n") is None


def test_a_new_hire_with_any_sign_off_form_resolves():
    """The point of the change: nobody has to edit a list when OL hires."""
    for greet in ("Best regards,", "Regards,", "Thanks,", "Kind regards,",
                  "Sincerely,"):
        body = f"Rate below.\n\n{greet}\nDana Whitfield\nOcean Export\n"
        assert core.parse_signer(None, body) == "Dana Whitfield", (
            f"a new hire signing off with {greet!r} is still invisible")


def test_the_roster_is_no_longer_required_but_is_still_consulted():
    """A first-name-only sign-off from a known person still expands to the
    full name — that is what the roster is FOR, now that it is not a gate."""
    got = core.parse_signer(None, "x\n\nThanks,\nCaren\n")
    assert got and got.lower().startswith("caren")
