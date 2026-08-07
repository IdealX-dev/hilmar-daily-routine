#!/bin/bash
# SessionStart hook — make a Claude Code on the web session able to run the
# linter and the test suite without hand-installing anything first.
#
# WHY THE DEP LIST IS SPELLED OUT RATHER THAN `pip install -e .`:
# pyproject.toml declares weasyprint, which needs GTK/pango system libraries
# that are not on the runner. Installing the project pulls it in and then
# every import fails. .github/workflows/test.yml has installed this exact
# explicit list since the repo went green, so mirroring it is the proven
# path — and tests/test_setup_hook_matches_ci.py fails if the two drift.
#
# Idempotent: pip skips satisfied requirements, so re-running on resume,
# clear or compact is a no-op costing a couple of seconds.
set -euo pipefail

# Web sessions only. A local checkout has its own environment and should not
# have packages installed into it behind the operator's back.
if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

cd "${CLAUDE_PROJECT_DIR:-$(dirname "$0")/../..}"

echo "[session-start] python: $(python3 --version 2>&1)"

# Runtime deps (scripts/ + src/hilmar/ imports) followed by test deps.
# Kept on one pip invocation so the resolver sees the whole set at once.
#
# No `pip install --upgrade pip` here, deliberately. CI can do that because
# actions/setup-python provides its own pip; this runner's pip comes from
# Debian with no RECORD file, so the upgrade fails outright — and under
# `set -e` that took the whole hook down before it installed anything. A
# newer pip buys nothing for a plain requirement list.
python3 -m pip install --quiet \
  reportlab msal requests tzdata pdfplumber \
  sentry-sdk jsonschema python-dateutil jinja2 anthropic \
  pytest pytest-cov responses ruff

# UTF-8 mode, for the session and for anything it shells out to.
#
# This is not housekeeping. core.load_data and ~70 other call sites read text
# files, and on a non-UTF-8 default locale a utf-8 file decoded as cp1252
# SUCCEEDS — silently — turning every "→" into "â†’" and every "×" into "Ã—".
# That shipped to production on 2026-08-05. Every workflow in .github/ sets
# these two variables for exactly this reason; a session that runs the same
# scripts by hand needs them too.
if [ -n "${CLAUDE_ENV_FILE:-}" ]; then
  {
    echo 'export PYTHONUTF8=1'
    echo 'export PYTHONIOENCODING=utf-8'
  } >> "$CLAUDE_ENV_FILE"
fi

echo "[session-start] ready — ruff check scripts/ src/ tests/ deploy/"
echo "[session-start]         pytest tests/ --no-cov     (the suite)"
echo "[session-start]         pytest tests/             (adds the 90% src/hilmar coverage gate)"
