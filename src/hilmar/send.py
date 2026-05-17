"""
hilmar.send — Outbound delivery wrappers around Microsoft Graph.

Two responsibilities:

1. :func:`send_daily_email` — sends the rendered email body to the OL-USA
   distribution list. Always CCs ``HILMAR_DAILY_CC`` (defaults to
   ``michael.deitchman@idealx.us``) so Michael sees the daily even if his
   OL inbox is bouncing or he's traveling.

2. :func:`upload_artifacts` — pushes ``tracking-data-v2.json`` and the
   rendered HTML/PDF artifacts into Michael's OneDrive folder
   (``HILMAR_ONEDRIVE_FOLDER_ID``) for archival + share.

Both are thin wrappers around :mod:`hilmar.graph_client` so all retry /
auth logic lives in one place. The orchestrator dry-run gate
(:envvar:`HILMAR_DRY_RUN=true`) is enforced at the orchestrator layer, not
here — these functions just do what they're told.
"""
from __future__ import annotations

import logging
import os
from collections.abc import Iterable
from pathlib import Path

from .graph_client import GraphClient

log = logging.getLogger(__name__)


def _resolve_cc(extra_cc: Iterable[str] | None) -> list[str]:
    """Always include ``HILMAR_DAILY_CC`` so Michael's idealx inbox sees the
    daily. Caller-supplied ``extra_cc`` is merged in (deduped, order
    preserved).
    """
    daily_cc_env = os.environ.get("HILMAR_DAILY_CC", "michael.deitchman@idealx.us")
    daily_cc_list = [c.strip() for c in daily_cc_env.split(",") if c.strip()]
    seen: set[str] = set()
    out: list[str] = []
    for addr in list(extra_cc or []) + daily_cc_list:
        addr_l = addr.lower()
        if addr_l in seen:
            continue
        seen.add(addr_l)
        out.append(addr)
    return out


def send_daily_email(
    client: GraphClient,
    *,
    to: list[str],
    subject: str,
    html_body: str,
    attachments: list[Path] | None = None,
    cc: list[str] | None = None,
) -> str:
    """Send the rendered daily email via Graph.

    The CC list ALWAYS includes ``HILMAR_DAILY_CC`` (per ``.env.example``).
    Returns the Graph message id.
    """
    full_cc = _resolve_cc(cc)
    log.info("send_daily_email to=%s cc=%s subject=%r attachments=%d",
             to, full_cc, subject, len(attachments or []))
    return client.send_mail(
        to=to,
        cc=full_cc,
        subject=subject,
        html_body=html_body,
        attachments=list(attachments or []),
    )


def send_internal_review(
    client: GraphClient,
    *,
    to: list[str],
    subject: str,
    html_body: str,
) -> str:
    """Send the internal-only review email — System / Design / Data /
    Business narrative + QC summary + parser stats. Per Michael's
    2026-04-28 split: staff (the daily TO list) sees only Business
    actions; this email carries the operational-internal view that
    only Michael should read. No attachments — body is self-contained.

    Recipient defaults via ``HILMAR_INTERNAL_TO`` env (typically
    Michael's idealx address). Returns the Graph message id, or empty
    string if no recipient configured (no-op, logged).
    """
    if not to:
        log.warning("send_internal_review: no recipients configured (HILMAR_INTERNAL_TO unset) — skipping")
        return ""
    log.info("send_internal_review to=%s subject=%r", to, subject)
    return client.send_mail(
        to=to,
        cc=[],  # internal review never CCs anyone
        subject=subject,
        html_body=html_body,
        attachments=[],
    )


def send_failure_email(
    client: GraphClient,
    *,
    to: str,
    host: str,
    run_at: str,
    traceback_text: str,
) -> str:
    """Page out via email when the daily run crashes. Body is the
    traceback tail (last 60 lines) wrapped in a <pre> block. Uses the
    same Graph auth as the daily send — if THAT is what failed, the
    webhook path in :func:`hilmar.orchestrator._post_failure_webhook`
    is the backup. Swallowing of own errors is the caller's job.

    Returns the Graph message id.
    """
    tb_tail = "\n".join(traceback_text.splitlines()[-60:])
    subject = f"[hilmar] daily run FAILED on {host} at {run_at}"
    html = (
        "<p style='font-family:system-ui;color:#991b1b;font-weight:600'>"
        f"hilmar-tracker daily run FAILED on <code>{host}</code> at {run_at}.</p>"
        "<p style='font-family:system-ui;font-size:13px;color:#334155'>"
        "Last 60 traceback lines below. Full log: "
        "<code>journalctl -u hilmar-tracker.service --since today</code> on the VM."
        "</p>"
        "<pre style='background:#0f172a;color:#e2e8f0;padding:12px;border-radius:6px;"
        f"font-size:12px;overflow:auto;'>{tb_tail}</pre>"
    )
    log.info("send_failure_email to=%s subject=%r", to, subject)
    return client.send_mail(
        to=[to], cc=[], subject=subject, html_body=html, attachments=[],
    )


def upload_artifacts(
    client: GraphClient,
    *,
    folder_id: str | None = None,
    folder_path: str | None = None,
    paths: Iterable[Path],
) -> dict[Path, str]:
    """Upload each path in ``paths`` to OneDrive.

    Pass ``folder_path`` (preferred — auto-creates and survives folder
    rename) or ``folder_id`` (legacy, goes stale on rename/move).
    ``folder_path`` wins when both are passed.

    Returns ``{local_path: webUrl}`` for each file successfully uploaded.
    Skips paths that don't exist with a warning (caller decides whether to
    treat that as fatal).
    """
    if not folder_path and not folder_id:
        raise ValueError("upload_artifacts: pass folder_path or folder_id")
    results: dict[Path, str] = {}
    for p in paths:
        path = Path(p)
        if not path.exists():
            log.warning("upload_artifacts: %s does not exist — skipping", path)
            continue
        if folder_path:
            web_url = client.upload_to_onedrive_by_path(
                folder_path=folder_path, local_path=path,
            )
        else:
            web_url = client.upload_to_onedrive(folder_id=folder_id, local_path=path)
        results[path] = web_url
        log.info("uploaded %s -> %s", path, web_url)
    return results


def main() -> int:
    """Console-script entrypoint registered as ``hilmar-send`` (future use).
    Currently a no-op placeholder — the orchestrator drives the live flow.
    """
    print("hilmar-send: this is normally invoked by the orchestrator. "
          "No standalone behavior yet.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
