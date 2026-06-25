"""
preflight_env.py — verify the box BEFORE the fire builds anything.

The env checks that existed (QC-052/053/054) all run INSIDE run_pipeline —
downstream of the interpreter/dep choice and of the fire that may never fire —
so they can't catch drift at the boundary where it can still shout cheaply and
abort before producing a degraded report on an unvalidated interpreter.

This runs as wrapper Step 0.5 (after git pull, before refresh_stage). It:
  - HARD-FAILS (exit 2) on interpreter drift — the running Python's major.minor
    must equal the pinned .python-version. The box silently ran 3.14 (untested;
    CI is 3.12) for a week. Can't self-heal a wrong interpreter → abort loud
    rather than build a report no test validates.
  - SOFT-FLAGS (alert, exit 0) on missing runtime deps (QC-054 self-heals them
    in-pipeline) and a behind-origin checkout.

Any problem raises the OUT-OF-BAND alarm (fire_alert — GitHub issue + Teams +
queue, never Outlook). Reuses qc_selfheal's pinned constants so it can't drift.
"""
from __future__ import annotations

import argparse
import importlib
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))


def _git_behind() -> int | None:
    """Commits the checkout is behind origin/main, or None if undeterminable."""
    try:
        out = subprocess.run(
            ["git", "-C", str(ROOT), "rev-list", "--count", "HEAD..origin/main"],
            capture_output=True, text=True, timeout=30)
        if out.returncode == 0 and out.stdout.strip().isdigit():
            return int(out.stdout.strip())
    except Exception:
        pass
    return None


def run_preflight() -> tuple[list[str], list[str]]:
    """Return (hard_problems, soft_problems). hard → abort the fire."""
    hard: list[str] = []
    soft: list[str] = []
    try:
        from qc_selfheal import RUNTIME_IMPORT_REQUIRED, check_interpreter_parity
    except Exception as e:  # the QC engine itself won't import — critical
        hard.append(f"qc_selfheal is unimportable ({type(e).__name__}: {e}) — "
                    f"the pipeline cannot run")
        return hard, soft

    ok, running, pinned = check_interpreter_parity()
    if pinned and not ok:
        hard.append(f"interpreter drift: running Python {running} != pinned "
                    f"{pinned} (.python-version) — no test validates this build")

    missing = []
    for mod in RUNTIME_IMPORT_REQUIRED:
        try:
            importlib.invalidate_caches()
            importlib.import_module(mod)
        except Exception:
            missing.append(mod)
    if missing:
        soft.append(f"runtime deps not importable: {', '.join(missing)} "
                    f"(QC-054 will attempt to self-heal in-pipeline)")

    behind = _git_behind()
    if behind and behind > 0:
        soft.append(f"checkout is {behind} commit(s) behind origin/main — the "
                    f"fire may run stale code")

    return hard, soft


def write_fingerprint(hard: list[str], soft: list[str], *, path: Path | None = None) -> str:
    """Stamp the box's environment health to reports/env-fingerprint.txt so the
    wrapper can forward it into the heartbeat. heartbeat.yml (on GitHub,
    independent of the box) then pages the operator when a fire shipped on a
    DRIFTED env — the proactive sentinel. One line, space-separated key=value:
        running=3.12 pinned=3.12 health=ok
        running=3.12 pinned=3.12 health=soft missing=jinja2,msal
        running=3.14 pinned=3.12 health=drift
    """
    running = f"{sys.version_info[0]}.{sys.version_info[1]}"
    pinned = "unknown"
    try:
        from qc_selfheal import _read_pinned_python
        pinned = (_read_pinned_python() or "unknown")
    except Exception:
        pass
    health = "drift" if hard else ("soft" if soft else "ok")
    line = f"running={running} pinned={pinned} health={health}"
    # Surface missing deps for the issue body (parse from the soft message).
    for s in soft:
        m = re.search(r"runtime deps not importable: ([^(]+)", s)
        if m:
            line += " missing=" + m.group(1).strip().rstrip(" ").replace(", ", ",")
            break
    path = path or (ROOT / "reports" / "env-fingerprint.txt")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # write_bytes with an explicit LF — text mode on Windows would emit
        # CRLF, and `set /p` in the wrapper would carry the stray CR into the
        # heartbeat input and break the sentinel's parsing.
        path.write_bytes((line + "\n").encode("utf-8"))
    except Exception:
        pass
    return line


def main() -> int:
    ap = argparse.ArgumentParser(description="Fire-start environment preflight")
    ap.add_argument("--no-alert", action="store_true", help="Don't raise out-of-band alert (tests)")
    args = ap.parse_args()

    hard, soft = run_preflight()
    write_fingerprint(hard, soft)   # always stamp the fingerprint for the sentinel
    if not hard and not soft:
        print("✅ Preflight OK — interpreter pinned, deps import, checkout current")
        return 0

    title = ("Preflight HARD-FAIL — fire aborted on environment drift"
             if hard else "Preflight warning — environment drift detected")
    lines = [*(f"[HARD] {p}" for p in hard), *(f"[soft] {p}" for p in soft)]
    body = "Fire-start preflight found:\n  - " + "\n  - ".join(lines)
    (print if not hard else (lambda *a, **k: print(*a, file=sys.stderr, **k)))("⚠️  " + title)
    for ln in lines:
        print("   - " + ln, file=sys.stderr)

    if not args.no_alert and (hard or soft):
        try:
            import fire_alert
            res = fire_alert.send_alert(title, body,
                                        level="critical" if hard else "warning")
            print(f"   out-of-band alert channels: {res}", file=sys.stderr)
        except Exception as e:
            print(f"   (fire_alert failed: {e})", file=sys.stderr)

    # exit 2 → wrapper aborts the fire; exit 0 (soft) → fire proceeds.
    return 2 if hard else 0


if __name__ == "__main__":
    raise SystemExit(main())
