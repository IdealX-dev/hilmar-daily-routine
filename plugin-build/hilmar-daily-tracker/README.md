# Hilmar Daily Tracker — Cowork plugin

Makes the **Hilmar Daily Shipment Tracker** project usable from any device
Michael signs into Claude with — laptops and the iPhone Claude app — the same
way the `ol-usa-quote-tracker` skill works for the rate checker.

## What it does

Bundles one skill, `hilmar-daily-tracker`, which gives Claude:

- **Where the project lives** — OneDrive primary path, GitHub fallback
  (`IdealX-dev/hilmar-daily-routine`), with device-detection guidance so the
  right path is used on whatever machine you're on.
- **How to run the pipeline** — the 16-step daily fire, QC self-heal, parser
  accuracy, status checks, sending the daily email, regenerating individual
  report artifacts.
- **The architecture** — data model, parser accuracy gate (95%), the ~46 QC
  checks (QC-001..QC-050), and the Sentry + Seer + Claude self-fix loop.
- **The hard rules** — email-send safety, ET timestamps, the parser gate,
  per-commit QC, mirror-edit discipline, never-greenfield.

## Once installed

The skill triggers automatically when you mention Hilmar, the daily shipment
tracker, Lonny Upfold, the OL-USA booking pipeline, MDOLX bookings, the
10 AM ET fire, parser accuracy, the Hilmar QC checks, or ask to run / check /
debug / report on the pipeline — on any device.

**Note:** the skill lets Claude *understand, run, and report on* the project.
Actually executing the Python pipeline still needs a real machine with the
code + Python (the Cloud PC does the scheduled 10 AM ET fire). From the
iPhone you can review the audit, check status, explain results, and make
decisions — heavy runs happen on a laptop or the Cloud PC.

## Components

| Component | Detail |
|---|---|
| Skill | `hilmar-daily-tracker` — reference + workflow for the pipeline |
| Agents | none |
| Hooks | none |
| MCP servers | none — the skill is self-contained |

## Install

Accept the `.plugin` file in Claude. It installs to your Claude account and
syncs to every device you sign into.
