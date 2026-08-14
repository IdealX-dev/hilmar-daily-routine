"""Read the shared mailbox, not Michael's personal one.

Michael 2026-08-14: "all quotes from this week are out and you should see the
emails in the graphed box not from my ol usa email so stop checking my ol
emails and use your direct connection to the shared box."

WHY IT IS RIGHT. The shared mailbox is where OL sends, and it is already the
authoritative copy in the dedupe. Reading /me on top of it was mostly
duplication: the 2026-08-14 60-day sweep reported 3,636 already-staged against
4 genuinely new, while paging 23,724 unclassified items out of a personal
mailbox carrying every non-Hilmar thread Michael is on.

THE TRADE, stated rather than buried: mail reaching ONLY
michael.deitchman@ol-usa.com and never the shared box stops being seen.
Nothing already staged is lost (stage_emails.txt is durable), but future mail
of that shape would be. One env var puts /me back.

THE LINE THIS MUST NOT CROSS: when there is no Mail.Read.Shared token, /me is
still read. Dropping it there would leave the fire reading NOTHING, and
"Lonny sent nothing" already read identical to "we cannot see the mailbox"
for a week once.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import refresh_stage as RS  # noqa: E402


def _reload(monkeypatch, value):
    monkeypatch.setenv("HILMAR_READ_SHARED_ONLY", value)
    return importlib.reload(RS)


def _targets(mod, monkeypatch, *, shared_ok: bool):
    monkeypatch.setattr(mod, "_mailbox_base", f"{mod.GRAPH}/me")
    monkeypatch.setattr(mod, "shared_token_silent",
                        lambda: "shared-token" if shared_ok else None)
    return mod.read_targets("me-token")


def test_only_the_shared_mailbox_is_read(monkeypatch):
    mod = _reload(monkeypatch, "true")
    labels = [t[0] for t in _targets(mod, monkeypatch, shared_ok=True)]
    assert labels == [mod.SHARED_MAILBOX], labels
    assert "me" not in labels


def test_me_is_still_read_when_there_is_no_shared_token(monkeypatch):
    """The safeguard. No shared access must degrade to /me, never to nothing."""
    mod = _reload(monkeypatch, "true")
    labels = [t[0] for t in _targets(mod, monkeypatch, shared_ok=False)]
    assert labels == ["me"], labels


def test_the_switch_restores_me_without_a_code_change(monkeypatch):
    mod = _reload(monkeypatch, "false")
    labels = [t[0] for t in _targets(mod, monkeypatch, shared_ok=True)]
    assert labels == [mod.SHARED_MAILBOX, "me"], labels


def test_the_shared_copy_still_leads_when_both_are_read(monkeypatch):
    """Dedupe is first-writer-wins, so ordering is what makes the shared copy
    authoritative."""
    mod = _reload(monkeypatch, "false")
    labels = [t[0] for t in _targets(mod, monkeypatch, shared_ok=True)]
    assert labels[0] == mod.SHARED_MAILBOX


def test_app_only_auth_is_untouched(monkeypatch):
    """App-only addresses READ_MAILBOX directly and has no /me at all."""
    mod = _reload(monkeypatch, "true")
    monkeypatch.setattr(mod, "_mailbox_base",
                        f"{mod.GRAPH}/users/{mod.READ_MAILBOX}")
    labels = [t[0] for t in mod.read_targets("app-token")]
    assert labels == [mod.READ_MAILBOX]


def test_default_is_false_because_shared_only_cannot_work(monkeypatch):
    """THE LANDMINE THIS DEFUSES. Michael 2026-08-14: "ol won't grant more
    access." Full Access was the only route to reading the shared mailbox —
    Graph answers 404 "Default folder Root not found" for every folder on
    it — so shared-only is now permanently the one setting that CANNOT
    return mail.

    It was the DEFAULT, held off production by a single env var in
    daily.yml. Drop that var, or import this module anywhere it is unset,
    and the fire reads a 404-for-everything mailbox, stages nothing, and
    exits GREEN. That is not hypothetical: the 10:41 fire on 2026-08-14 did
    exactly that and reported success.

    The default must be the only configuration that can return mail."""
    monkeypatch.delenv("HILMAR_READ_SHARED_ONLY", raising=False)
    mod = importlib.reload(RS)
    assert mod.READ_SHARED_ONLY is False, (
        "HILMAR_READ_SHARED_ONLY defaults to shared-only, which cannot "
        "return mail — OL will not grant the Full Access it requires. An "
        "unset env var must not send the fire blind."
    )


def test_unset_env_still_reads_me_alongside_shared(monkeypatch):
    """The default is not just a flag value — with no env var set, /me must
    actually be in the target list, because it is the only mailbox that
    answers. Asserting the constant alone would pass while read_targets
    still dropped /me."""
    monkeypatch.delenv("HILMAR_READ_SHARED_ONLY", raising=False)
    mod = importlib.reload(RS)
    labels = [t[0] for t in _targets(mod, monkeypatch, shared_ok=True)]
    assert "me" in labels, (
        f"/me missing from targets under the default config: {labels}. "
        "The shared mailbox 404s on every folder, so this reads nothing."
    )


def teardown_module(_m):
    """Leave the module in its real, unpatched state for other test files."""
    import os
    os.environ.pop("HILMAR_READ_SHARED_ONLY", None)
    importlib.reload(RS)
