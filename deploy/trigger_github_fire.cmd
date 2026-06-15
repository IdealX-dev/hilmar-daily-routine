@echo off
REM ==================================================================
REM trigger_github_fire.cmd  (added 2026-06-15)
REM
REM Punctual trigger for the daily Hilmar fire. Windows Task Scheduler
REM runs this ON TIME (it never had GitHub's multi-hour cron lateness);
REM it DISPATCHES the GitHub production-fire, which runs the full
REM pipeline against the live Azure-blob state and sends the client
REM email. The Cloud PC contributes only punctual timing -- all compute
REM and state stay on GitHub/blob (the post-2026-06-11 canonical home).
REM
REM TRIGGER ONLY -- this does NOT run the pipeline locally. Do NOT also
REM enable run_daily_laptop.cmd (the old local-fire wrapper): running
REM both double-computes and drifts local state from the blob. GitHub's
REM own backstop cron ticks + outlook_send's mailbox guard make an
REM overlap between this trigger and a GitHub tick safe (no double-send).
REM
REM Requires: gh CLI authenticated on the Cloud PC (already true -- the
REM old wrapper used gh for heartbeats). ASCII-only on purpose (Windows
REM cmd/PS codepage safety).
REM ==================================================================
gh workflow run daily.yml -R IdealX-dev/hilmar-daily-routine -f mode=production-fire -f send_to=full
echo trigger_github_fire: dispatched GitHub production-fire (rc=%ERRORLEVEL%)
