"""diagnose_today.py — single-paste diagnostic dump for incident response.

Run on the Cloud PC (or any box that has the production tracking-data-v2.json
+ reports/ + secrets/sentry-auth-token.txt) to produce ONE block of text I can
read from chat. Replaces the friction of "paste qc-result.json, also paste
run-log tail, also paste Sentry hot issues" — that's three round-trips
between an iPhone and Claude. With this you run one command and paste once.

Usage (PowerShell or bash):
    python scripts/diagnose_today.py                    # print to stdout
    python scripts/diagnose_today.py > diag.txt         # save to file
    python scripts/diagnose_today.py --sections qc,log  # subset

The output is hand-readable and bounded (<60 KB even on a bad day). It NEVER
includes full email bodies, secrets, or anything from secrets/* — the Sentry
token is read locally to query the API and is not printed.

Sections:
  qc      — reports/qc-result.json error_details + warning_details
  log     — last ~400 lines of reports/run-log.txt (today only)
  drift   — reports/drift-result.json status + fail_reasons
  sentry  — top 10 unresolved + hot issues last 24h (requires Sentry token)
  data    — tracking-data-v2.json summary (counts, last_updated, rates)
  files   — mtimes of reports/email-subject.txt + email-body.html + dashboard

By default: all sections. Pass --sections to limit.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPORTS = ROOT / "reports"


def _hr(title: str) -> str:
    bar = "=" * 70
    return f"\n{bar}\n{title}\n{bar}"


def _read_text(p: Path, tail_lines: int | None = None) -> str:
    if not p.exists():
        return f"<missing: {p}>"
    try:
        txt = p.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return f"<unreadable: {p} — {type(e).__name__}: {e}>"
    if tail_lines is None:
        return txt
    lines = txt.splitlines()
    return "\n".join(lines[-tail_lines:])


def _read_json(p: Path):
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        return {"__diagnose_error__": f"{type(e).__name__}: {e}"}


def _fmt_mtime(p: Path) -> str:
    if not p.exists():
        return "<missing>"
    mt = datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc)
    age_h = (datetime.now(timezone.utc) - mt).total_seconds() / 3600.0
    return f"{mt.isoformat()} ({age_h:.1f}h ago, {p.stat().st_size:,}B)"


def section_qc() -> str:
    qc = _read_json(REPORTS / "qc-result.json")
    if qc is None:
        return _hr("QC RESULT") + "\n<reports/qc-result.json missing>"
    if "__diagnose_error__" in qc:
        return _hr("QC RESULT") + f"\n{qc['__diagnose_error__']}"
    lines = [
        f"status={qc.get('status')}  fixes={qc.get('fixes')}  "
        f"warnings={qc.get('warnings')}  errors={qc.get('errors')}",
        f"counts={qc.get('counts')}",
        f"data_freshness={qc.get('data_freshness')}",
    ]
    errs = qc.get("error_details") or []
    warns = qc.get("warning_details") or []
    if errs:
        lines.append("\n-- ERRORS --")
        lines.extend(f"  {e}" for e in errs)
    if warns:
        lines.append("\n-- WARNINGS --")
        lines.extend(f"  {w}" for w in warns)
    return _hr("QC RESULT") + "\n" + "\n".join(lines)


def section_log(tail_lines: int = 400) -> str:
    log = _read_text(REPORTS / "run-log.txt", tail_lines=tail_lines)
    # Trim to today's marker if present (US date format MM/DD/YYYY OR ISO)
    today_iso = datetime.now().strftime("%Y-%m-%d")
    today_us = datetime.now().strftime("%m/%d/%Y")
    for marker in (today_iso, today_us):
        idx = log.rfind(marker)
        if idx > 0:
            log = log[idx:]
            break
    return _hr(f"RUN-LOG (last {tail_lines} lines, trimmed to today's marker)") + "\n" + log


def section_drift() -> str:
    d = _read_json(REPORTS / "drift-result.json")
    if d is None:
        return _hr("DRIFT") + "\n<reports/drift-result.json missing>"
    if "__diagnose_error__" in d:
        return _hr("DRIFT") + f"\n{d['__diagnose_error__']}"
    return _hr("DRIFT") + "\n" + json.dumps(
        {"status": d.get("status"),
         "fail_reasons": d.get("fail_reasons"),
         "phases": d.get("phases")},
        indent=2, default=str,
    )


def section_sentry() -> str:
    """Top 10 unresolved + hot (≥3 events) Sentry issues from the last 24h.
    Silent no-op if the auth token isn't on this machine — that's a
    Cloud-PC-only secret and not present in CI / Codespaces."""
    try:
        sys.path.insert(0, str(ROOT / "scripts"))
        from sentry_api import SentryAPI  # type: ignore
    except Exception as e:
        return _hr("SENTRY") + f"\n<sentry_api import failed: {e}>"
    try:
        api = SentryAPI()
    except Exception as e:
        return _hr("SENTRY") + f"\n<SentryAPI init failed: {e}>"
    if not getattr(api, "enabled", False):
        return _hr("SENTRY") + "\n<auth token not configured on this machine>"
    try:
        issues = api.list_issues(stats_period="24h", query="is:unresolved", limit=20) or []
    except Exception as e:
        return _hr("SENTRY") + f"\n<list_issues failed: {type(e).__name__}: {e}>"
    rows = []
    for i in issues[:10]:
        rows.append(
            f"  {i.get('shortId','?'):<24} count={i.get('count','?'):<5} "
            f"lastSeen={(i.get('lastSeen') or '')[:19]}  {(i.get('title') or '')[:80]}"
        )
    hot = [i for i in issues if int(i.get("count") or 0) >= 5]
    out = [f"unresolved_total={len(issues)}  hot(>=5x/24h)={len(hot)}"]
    if hot:
        out.append("\n-- HOT (>=5 events / 24h) --")
        for i in hot:
            out.append(
                f"  {i.get('shortId','?')}  count={i.get('count')}  "
                f"{(i.get('title') or '')[:100]}  ::  {i.get('permalink','')}"
            )
    out.append("\n-- TOP 10 UNRESOLVED --")
    out.extend(rows)
    return _hr("SENTRY (last 24h)") + "\n" + "\n".join(out)


def section_data() -> str:
    data_path = ROOT / "tracking-data-v2.json"
    if not data_path.exists():
        return _hr("DATA") + f"\n<{data_path} missing>"
    try:
        d = json.loads(data_path.read_text(encoding="utf-8"))
    except Exception as e:
        return _hr("DATA") + f"\n<unreadable: {type(e).__name__}: {e}>"
    s = d.get("summary") or {}
    return _hr("DATA") + "\n" + json.dumps({
        "last_updated": d.get("last_updated"),
        "row_count": len(d.get("requests") or []),
        "wins": s.get("wins"),
        "quoted_lost": s.get("quoted_lost"),
        "not_quoted": s.get("not_quoted"),
        "pending_hilmar": s.get("pending_hilmar"),
        "win_rate": s.get("win_rate"),
        "quote_rate": s.get("quote_rate"),
    }, indent=2)


def section_files() -> str:
    targets = [
        REPORTS / "email-subject.txt",
        REPORTS / "email-body.html",
        REPORTS / "hilmar-dashboard.html",
        REPORTS / "hilmar-report.pdf",
        REPORTS / "improvements-report.html",
        REPORTS / "drift-result.json",
        REPORTS / "qc-result.json",
        REPORTS / "run-log.txt",
        ROOT / "tracking-data-v2.json",
    ]
    rows = [f"  {p.name:<32}  {_fmt_mtime(p)}" for p in targets]
    return _hr("ARTIFACT FRESHNESS") + "\n" + "\n".join(rows)


SECTIONS = {
    "qc":     section_qc,
    "log":    section_log,
    "drift":  section_drift,
    "sentry": section_sentry,
    "data":   section_data,
    "files":  section_files,
}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--sections", default="files,data,qc,drift,sentry,log",
        help="Comma-separated sections to include (default: all). "
             f"Available: {','.join(SECTIONS)}",
    )
    ap.add_argument(
        "--log-tail", type=int, default=400,
        help="Number of lines from run-log.txt to include (default: 400)",
    )
    args = ap.parse_args()

    now_et_str = ""
    try:
        sys.path.insert(0, str(ROOT / "scripts"))
        import core  # type: ignore
        now_et_str = datetime.now(core.ET).isoformat()
    except Exception:
        now_et_str = "<core.ET unavailable>"

    print(f"# Hilmar diagnose-today — generated {now_et_str}")
    print(f"# host={os.environ.get('COMPUTERNAME') or os.uname().nodename}")
    print(f"# repo_root={ROOT}")

    requested = [s.strip() for s in args.sections.split(",") if s.strip()]
    for name in requested:
        fn = SECTIONS.get(name)
        if fn is None:
            print(_hr(f"UNKNOWN SECTION: {name}"))
            continue
        if name == "log":
            print(section_log(tail_lines=args.log_tail))
        else:
            print(fn())

    return 0


if __name__ == "__main__":
    sys.exit(main())
