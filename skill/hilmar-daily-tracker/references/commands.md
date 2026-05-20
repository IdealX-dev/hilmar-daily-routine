# Hilmar Daily Tracker — commands

Every command assumes you have located the project (see SKILL.md Step 0) and
your shell working directory is the **production root** —
`PROJECT HILMAR/` (the folder that contains `scripts/`, `config.json`,
`tracking-data-v2.json`, `data-backups/`, `reports/`, and the
`hilmar-daily-routine/` git subfolder).

On Windows the Python interpreter is `python` (Python 3.14 on the Cloud PC).
Set `PYTHONIOENCODING=utf-8` on every invocation — the OL/Lonny email bodies
contain en-dashes, smart quotes, and accented names that crash the default
cp1252 codec.

## Run the pipeline

Full daily fire — all 16 steps, ending with the email send:
```
PYTHONIOENCODING=utf-8 python scripts/run_pipeline.py
```

Dry run — print each step's command without executing (safe anywhere):
```
PYTHONIOENCODING=utf-8 python scripts/run_pipeline.py --dry
```

Skip the ingest step (re-run reporting on existing `tracking-data-v2.json`):
```
PYTHONIOENCODING=utf-8 python scripts/run_pipeline.py --skip-ingest
```

The pipeline runs `backup.py` as Step 1, so a snapshot is always taken before
anything mutates the data. See `pipeline.md` for the full step list.

## QC self-heal

Run the QC + self-heal suite. `HILMAR_QC_PHASE` controls Sentry behavior:
- `pre-patch` — runs before carrier enrichment; suppresses Sentry events for
  findings the patch step will fix moments later
- `post-patch` — the real shipped state; this is the run that fires Sentry

```
PYTHONIOENCODING=utf-8 HILMAR_QC_PHASE=post-patch python scripts/qc_selfheal.py
```

The output ends with a status block: `Status: HAS_ERRORS | CLEAN`, plus
`Fixes / Warnings / Errors` counts and the entries summary
(`NNN entries: NW | N Q&L | N NQ | N P`).

To see only specific checks, grep the output:
```
PYTHONIOENCODING=utf-8 HILMAR_QC_PHASE=post-patch python scripts/qc_selfheal.py 2>&1 | grep -E "QC-039|QC-044|QC-048|QC-050"
```

## Parser accuracy

Compute per-field + overall parser accuracy against `tracking-data-v2.json`:
```
PYTHONIOENCODING=utf-8 python -c "import json,sys; sys.path.insert(0,'hilmar-daily-routine/src'); from hilmar.parser_accuracy import compute_accuracy; r=compute_accuracy(json.load(open('tracking-data-v2.json',encoding='utf-8'))['requests']); print(f'overall {r[\"overall_rate\"]*100:.2f}%  weighted {r[\"weighted_rate\"]*100:.2f}%  pass={r[\"pass\"]}  failing={r[\"failing_fields\"]}')"
```

The gate is 95% (`ACCURACY_THRESHOLD`). A `pass=False` or non-empty
`failing_fields` means QC-039 will block the pipeline.

## Status

Last pipeline run + backup freshness:
```
PYTHONIOENCODING=utf-8 python -c "import json; d=json.load(open('tracking-data-v2.json',encoding='utf-8')); print('last_updated:', d.get('last_updated')); s=d.get('summary',{}); print('rows:', len(d.get('requests',[])), '| wins:', s.get('wins'), '| Q&L:', s.get('quoted_lost'), '| NQ:', s.get('not_quoted'), '| pending:', s.get('pending_hilmar'))"
```

Backup snapshots (newest last):
```
ls -t data-backups/tracking-data-v2*.json | head -5
```

Whether today's email already fired (idempotency flag):
```
ls reports/sent-$(date +%Y-%m-%d).flag reports/improvements-sent-$(date +%Y-%m-%d).flag 2>/dev/null
```

## Send email

The pipeline sends automatically as its last step. To send manually:

**Daily tracker email** to the configured distribution (`--to-from-config`
reads `config.json` → `distribution.full_list`):
```
PYTHONIOENCODING=utf-8 python scripts/outlook_send.py daily \
  --to-from-config \
  --subject-from-file reports/email-subject.txt \
  --body-from-file reports/email-body.html \
  --attach reports/hilmar-dashboard.html reports/hilmar-report.pdf \
  --force
```

**Sample / test** — explicit recipients only (NEVER use `--to-from-config`
for a test; that hits all 10 recipients):
```
PYTHONIOENCODING=utf-8 python scripts/outlook_send.py daily \
  --to michael.deitchman@idealx.us \
  --subject-from-file reports/email-subject.txt \
  --body-from-file reports/email-body.html \
  --attach reports/hilmar-dashboard.html reports/hilmar-report.pdf \
  --force
```

`--force` overrides the per-day idempotency flag. `outlook_send.py`
authenticates as `michael.deitchman@ol-usa.com` via cached MSAL token
(`secrets/token-cache.json`). Cross-tenant sends (ol-usa.com → idealx.us)
can land in Junk — tell Michael to check there if a send doesn't appear.

## Regenerate one report artifact

Sometimes you only need to rebuild one artifact (e.g. after a formatting fix):
```
PYTHONIOENCODING=utf-8 python scripts/gen_dashboard.py    # → reports/hilmar-dashboard.html
PYTHONIOENCODING=utf-8 python scripts/gen_pdf.py          # → reports/hilmar-report.pdf
PYTHONIOENCODING=utf-8 python scripts/gen_email.py        # → reports/email-body.html + email-subject.txt
PYTHONIOENCODING=utf-8 python scripts/gen_improvements_report.py  # → reports/improvements-report.html
```

## Sentry-driven QC actions

Scan unresolved Sentry issues + dispatch remediation (Seer autofix, Claude
diagnosis, resolve, or operator flag). `--apply` commits comments/resolves;
omit it for a dry run:
```
PYTHONIOENCODING=utf-8 python scripts/qc_actions_from_sentry.py          # dry run
PYTHONIOENCODING=utf-8 python scripts/qc_actions_from_sentry.py --apply  # live
```

## Re-ingest after a parser change

When `body_parser.py` / `ingest.py` change, existing fetched bodies need
re-parsing before the new fields populate:
```
PYTHONIOENCODING=utf-8 python scripts/reprocess_bodies.py   # re-parse staged bodies
PYTHONIOENCODING=utf-8 python scripts/ingest.py             # rebuild tracking-data-v2.json
PYTHONIOENCODING=utf-8 python scripts/patch_carriers.py     # carrier + field enrichment
```

## Tests

```
cd hilmar-daily-routine && PYTHONIOENCODING=utf-8 python -m pytest tests/ --override-ini="addopts=" -q
```
If `pytest` isn't installed, the test files can be exec'd directly — see the
repo's `scripts/run_tests.py`.

## Commit + push (only when Michael asks)

Edits land in the repo (`hilmar-daily-routine/`). After editing, mirror to
production (`cp hilmar-daily-routine/scripts/X.py scripts/X.py`), then from
the repo dir: stage the specific files, commit with a descriptive message +
the `Co-Authored-By` trailer, and `git push origin main`. Never commit
`secrets/`, `tracking-data-v2.json` churn, or `reports/` artifacts unless
explicitly asked.
