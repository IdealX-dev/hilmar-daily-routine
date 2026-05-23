# IdealX / Michael Deitchman — Operating Standards for Claude

**Canonical source:** `github.com/IdealX-dev/idealx-claude-standards`
**Version:** 1.0 — 2026-05-23

This document defines the operating rules every Claude session, every
device (laptop, iOS, Cowork web), and every repo must follow.

Each active IdealX repo carries a copy at:
- `<repo>/STANDARDS.md` (root, for visibility) AND/OR
- `<repo>/.claude/STANDARDS.md` (Claude Code project-level)

The user-level `~/.claude/CLAUDE.md` references this file. The
weekly cruft-audit routine cross-checks every repo's copy against
the canonical version and flags drift.

---

## 0. Hard rules — read first

1. **Report all timestamps in Eastern Time (ET) in chat.** Code, DB,
   and APIs use UTC. The chat translation layer is ET (EDT in summer,
   EST in winter — "ET" covers both). Never expose raw UTC to Michael
   in conversation.

2. **Auto-commit and publish.** When a unit of work is complete and
   verified, commit and push immediately — no "should I commit?"
   ceremony. Each fix/feature is its own commit. Never `git add -A`,
   never `--no-verify`, never force-push to main/master. Verify
   BEFORE pushing always.

3. **Never wait for approval on standard work.** Michael's explicit
   in-chat OK or this standing rule IS the approval. The harness may
   ask permission on specific actions — that's a UI safeguard, not a
   decision gate.

4. **Scheduled remote agents are MY job, not Michael's.** Every
   session starts with `RemoteTrigger {action:"list"}`. Any routine
   fired since the last check, fetch its transcript via
   `{action:"get"}` and act on findings. Surface progress, not links.
   Never tell Michael to "view your routines at https://..." unless
   he explicitly asks.

---

## 1. Session-start audit (mandatory, every session, every repo)

Before ANY feature work in a production repo (rate-blaster,
ol-quote-tracker, hilmar-daily-routine, henco-*, any future
prod-impacting project), run this 5-minute audit and surface the
results in the session's FIRST message:

```
=== Session-start audit (<repo>) ===
  Coverage:        X% / floor Y%       ← PASS/FAIL
  QC orphans:      N untested _check_* methods   ← list ≤ 5
  cmd/route orphans: M untested entry points     ← list ≤ 5
  Parser accuracy: X% / floor Y%       ← PASS/FAIL (if parser-driven)
  py_compile:      N files fail to parse on Python X.Y
  Stale branches:  N branches > 14 days old, unmerged
```

Then ask whether to close the gaps in this session or stash for
later. **Never silently proceed to feature work while orphans
exist** — the latent debt compounds.

**Coverage floors per project** (only ratchets UP, never down):

| Project | Floor | Enforcement |
|---|---|---|
| rate-blaster | 60% | `pytest --cov-fail-under` |
| ol-quote-tracker | 70% | `coverage:ratchet check` |
| hilmar-daily-routine | 80% | repo-local check |

---

## 2. Every-line QC + self-heal ambition

The per-commit rule says "every new pattern ships with paired
QC + self-heal". The aspirational extension is broader:

- **Every public function has at least a smoke test** ("does it run
  without crashing on representative input?")
- **Every `_check_*` method has at least one test** that exercises it
- **Every operator-facing route has a smoke test** (200/302/401 with
  no traceback on a seeded test DB)
- **Every production code path that mutates customer-facing data has
  a paired QC check + self-heal action**
- **The coverage floor only goes UP**

The aim isn't "100% coverage today." It's "every gap is named,
ticketed, and visible, and the floor only ratchets up."

### Hard blocker rules (no PR merges without these)

- Don't ship a `_check_*` method without at least a smoke test
- Don't ship a `cmd_*` / route handler without a smoke test
- Don't ship a parser change without re-exercising the parser-accuracy
  framework
- Don't merge a PR that drops coverage > 0.5pp on any module without
  EITHER compensating tests OR an explicit `coverage:baseline:freeze`
  with operator-facing justification in the commit message

---

## 3. Paired QC + self-heal on every new code pattern

When shipping a new pattern, in the SAME commit:

| New pattern | Required QC check | Self-heal if safe |
|---|---|---|
| New mailbox/folder/storage layout | Walk the structure, flag drift | Auto-delete empty stale folders, re-trigger backfills |
| New scheduled job / cron / timer | Staleness (last fired? last success? log file age?) | Restart timer if that's the only fix |
| New external API integration | Token freshness, reachability, error-rate | Re-acquire token, skip with WARN on degraded external |
| New schema migration / DB constraint | Orphan rows, FK violations, constraint mismatches | Only if zero data-loss risk; otherwise WARN |
| New automated email path | Delivery success rate, bounce backlog, suppression health | None — alert only |

**When NOT to add a self-heal:**
- Action could destroy real data
- Root cause varies (journal errors, disk pressure)
- Requires human judgment (re-blast vs retract decisions)

Keep the QC check (so it surfaces in the daily report) and emit an
alert only.

### Pattern for QC additions (Python)

```python
def _check_<pattern_name>(self):
    """Description of what this checks."""
    # gather data
    # decide PASS / WARN / FAIL
    self.results.append(CheckResult(
        name="<pattern_name>", status=status, summary=summary,
        details=details_list_or_None,
    ))

# Register in run_all() / run_drift_audit():
self._check_<pattern_name>,

# In self_heal(), if safely auto-fixable:
def self_heal(self) -> dict:
    actions = {..., "<pattern>_fixed": 0}
    # safe auto-fix logic
```

---

## 4. Parser-driven projects: 98% accuracy is a hard production gate

For every parser pipeline (intake parser, email parser, document
parser, etc.):

1. **Per-field accuracy framework** like
   `src/<project>/parser_accuracy.py`. Defines:
   - `FIELD_REQUIREMENTS`: applicability predicates
   - `CRITICAL_FIELDS`: gate-failing fields
   - `PER_FIELD_THRESHOLDS`: documented historical-gap overrides
   - `compute_accuracy(rows)` returning per-field + overall + weighted

2. **A QC check that BLOCKS ship** (severity = ERROR) when:
   - Overall accuracy < `ACCURACY_THRESHOLD`
   - Any critical field < its threshold

3. **Daily-audit report includes accuracy** so regressions surface
   same-day.

4. **Cost is not a constraint.** Spend the API tokens. If LLM
   tier-2 is needed for fallback, ship it. The cost of one wrong
   booking confirmation > monthly LLM bill.

**Thresholds by project:**
- rate-blaster intake parser: 95%
- ol-quote-tracker emailParser: 95%
- hilmar parser: 98%

---

## 5. Email-shape constraints (Outlook + Exchange)

### Outlook 255-char subject truncation

Any feature constructing `mailto:` links with comma-separated
quote_refs MUST chunk batches so each URL-encoded subject stays
≤ 220 chars. 240+ chars hits the danger zone.

**Required QC for every new bulk-mailto feature:**
```python
def _check_<feature>_subject_length(self):
    # render the longest batch the feature would emit;
    # FAIL if URL-encoded ≥ 240 chars
    # WARN if ≥ 220
```

**Required runtime detection at intake** when inbound
CLOSE/BOOK/CANCEL messages arrive:
1. Subject ≥ 240 chars
2. Last token < 60% the median length
On detection: drop partial last ref, send operator [WARNING], log
`<verb>_TRUNCATED_DETECTED`.

### Outbound identity invariants

Every outbound operator-recognized email shape (RFQ, comparison,
digest) gets a `_check_<shape>_identity` QC that asserts:
- Subject prefix matches template
- From mailbox matches expected
- Reply-To includes the bot mailbox

Severity: **FAIL not WARN.** One wrong-identity batch to 100 agents
costs more than being loud about config drift.

---

## 6. Sentry observability mandatory

Every long-running production pipeline gets:

1. **Single `sentry_setup.py`** at `scripts/sentry_setup.py` or
   `src/<pkg>/sentry_setup.py`. Provides:
   - `init(component="<entry_point>")` — silent no-op without DSN
   - `capture_qc_error(check_name, summary)`
   - `capture_qc_warning(check_name, summary)`
   - `capture_step_failure(step_name, error)`

2. **Comprehensive PII scrubbing** in the `before_send` hook:
   - Email addresses, internal IDs, customer-specific identifiers
   - `send_default_pii=False`, `with_locals=False`

3. **ERROR-severity QC checks auto-fire Sentry events.** Wire via
   the project's logger so `.error()` also calls `capture_qc_error()`.

4. **Per-step transactions** in the orchestrator:
   `sentry_sdk.start_span(op="pipeline.step", name="...")`
   Top-level `start_transaction(op="pipeline.run")`.

5. **`docs/SENTRY.md`** — runbook documenting DSN location, what
   gets sent vs scrubbed, common alerts + diagnosis, cost ceiling.

6. **Tags every event needs:**
   - `component`, `pipeline_run_id`, `environment`, `release`

7. **Sentry Crons heartbeat for ALL scheduled pipelines.** Without
   a heartbeat, a scheduler/wrapper crash before any code runs is
   invisible. The DAILY EMAIL won't catch it either.

8. **Custom metrics** (paid tier required for trending):
   parser/data accuracy, pipeline duration, QC findings, send health,
   plus project-specific KPIs.

---

## 7. Architectural anti-patterns

### Never re-blast already-handled cargo

When a forwarded request matches an EXISTING job (same client +
same PO + same lane within 30 days), the system MUST treat it as a
duplicate intake of an already-handled cargo, not as a new RFQ.

**Match criteria (in order):**
1. Same `intake_message_id` — exact dedup
2. Same `client_name` + normalized subject — MODE-AGNOSTIC
3. Same `client_name` + shared PO/Order/Reference number

**Window:** 30 days.

**When dedup hits:** skip job creation, mark intake as processed
(`OK_DUP`), log `DEDUP_HIT` line for audits.

### Never fork-without-history (no greenfield duplicates)

When asked to deliver something for an existing project domain,
**NEVER create a new repo from scratch if a related one already
exists.** Refactor the existing one. Fork-with-history if necessary.

**Concretely — before creating ANY new repo:**

1. **Inventory check FIRST:** `gh repo list <org> --limit 200` and
   grep for any related name. If ANY hit exists, you must NOT
   greenfield.
2. **If a related repo exists**, read its README + recent commits.
   Determine if the new ask is a genuinely separate concern (rare)
   or a deployment/feature variation of the existing system
   (usually). For variations → refactor the existing repo.
3. **If refactor would be too disruptive** → create a branch on the
   EXISTING repo, ship a PR. Never a new repo.
4. **If you genuinely must create a new repo** — document WHY in
   the initial commit message + link to the related repo so future
   sessions know they're intentionally separate.
5. **Tell Michael BEFORE creating the new repo.** Surface the choice
   with the inventory results.

### Drift prevention (mandatory QC checks)

For projects with multiple source folders or status enums:

- **QC: Cross-folder enum drift detection.** When the same enum
  exists in two folders (e.g. `scripts/` + `src/<pkg>/`), they MUST
  match exactly OR the divergence is documented in an
  ALLOWED-DRIFT constant inside the QC itself.
- **QC: Classifier form consistency in data.** When two storage
  forms are accepted, the actual data file MUST use exactly one
  form across all rows. Mixed-form = ERROR.

**Classifier changes are protected operations.** Any change to a
status enum / loss-reason enum / schema status field must:
1. Back up data BEFORE the change
2. Update ALL constants + schema.json + fixtures + ALLOWED-DRIFT
   in the SAME commit
3. Test suite passes BEFORE push
4. Production pipeline runs end-to-end BEFORE push

---

## 8. HTML email rendering (Outlook gotchas)

### Linear-gradient strip — Outlook compatibility

ANY element with `color:white` or `color:#ffffff` MUST also have a
solid `background-color:` set. Outlook strips
`background:linear-gradient(...)` and leaves white text on a
white default — invisible.

List `background-color:` BEFORE `background:linear-gradient(...)`
so Outlook reads the solid color when it strips the gradient.

### Double HTML-escape of `&`

Every escape function runs EXACTLY ONCE per string. Author-facing
labels in source code use raw `&`, `<`, `>` — let the render helper
do the escape. Never pass pre-escaped strings to escape-running
helpers.

QC pattern:
```python
if "&amp;amp;" in body or "&amp;quot;" in body:
    log.error("Double-escape detected ...")
```

### Windows + Unix-only strftime tokens

`%-d`, `%-I`, `%-m`, `%-H`, `%-M`, `%-S` are Unix-only — on Windows
(Cloud PC pipelines), these raise `ValueError`. Use the portable
pattern: `%d` / `%I` (zero-padded) + `.replace(" 0", " ", 1)`.

### KPI tile heights

Outlook honors `height` attribute on inline-styled divs; modern
clients honor `min-height`. Set BOTH on every card:
```
min-height:88px; height:88px; box-sizing:border-box
```

### Iteration-mode distribution lock

When iterating on a daily-distribution email format, reduce the
recipient list to ONLY the operator's personal address for the
duration of iteration. Restore the full distribution once approved.

---

## 9. Project tooling lives in the repo

For tooling to be available on every device (laptop, claude.ai web,
iOS Claude app, Cowork), it MUST travel with the repo.

**Committed `<repo>/.claude/` IS the project's portable tooling.**
- `.claude/skills/<name>/SKILL.md`
- `.claude/agents/`
- `.claude/commands/`
- `<repo>/CLAUDE.md` (project-specific guidance)
- `<repo>/STANDARDS.md` (this file)

**Machine-local is invisible elsewhere:**
- `~/.claude/skills/`
- `~/.claude/plugins/`
- `~/.claude/scheduled-tasks/`
- A non-committed project `.claude/skills/`
- `~/.claude/CLAUDE.md` itself (until synced to a repo)

**Device-reachability prerequisite:** the iOS/web Claude app reaches
GitHub-connected repos. For repos on Azure DevOps only, confirm
they're connected in the Claude app first.

**New-device audit:** opening a project on a new device and finding
a skill or command missing means it was left machine-local. Fix by
migrating that tooling into the repo — never re-install locally.

---

## 10. Privacy + security

### What Claude will NEVER do (prohibited)

- Handle banking, sensitive credit card, ID, or passport data
- Download files from untrusted sources without explicit approval
- Delete permanently (emptying trash, deleting emails, files, messages)
  unless explicitly requested AND confirmed
- Modify security permissions / sharing / access controls — even
  with explicit permission. Operator must do these themselves.
- Provide investment / financial advice
- Execute financial trades or transactions on the operator's behalf
- Create new accounts on the operator's behalf
- Authorize password-based access — operator inputs passwords directly

### What requires explicit chat confirmation

- Operations that expand sensitive info beyond current audience
- File downloads (state filename + size + source)
- Purchases / financial transactions
- Account settings changes
- Sharing or forwarding confidential information
- Accepting terms / conditions / agreements
- OAuth / SSO authorizations (login flows only, not new accounts)
- Cookie / data-collection policies
- Publishing or modifying public content
- Sending email on operator's behalf (drafts OK; sends need approval)
- "Send" / "publish" / "post" / "purchase" / "submit" buttons

---

## 11. Cross-project memory + version control hygiene

### Where things live

- **Project-specific memory:**
  `~/.claude/projects/<project>/memory/MEMORY.md`
- **Cross-project rules + behavioral preferences:**
  `~/.claude/CLAUDE.md` (user-level) + this STANDARDS.md (portable)
- **Project-specific guidance:** `<repo>/CLAUDE.md`

Update both when a rule's scope changes.

### Active IdealX repo inventory

Always run `gh repo list IdealX-dev --limit 200` before any new-repo
decision. Active repos as of 2026-05-23:

| Repo | Status | Purpose |
|---|---|---|
| rate-blaster | active | Freight RFQ automation (Python + Postgres on Azure VM) |
| ol-quote-tracker | active | OL USA quote-tracker UI (TypeScript + libsql) |
| hilmar-daily-routine | active | Hilmar tracker (Python, Cloud PC daily pipeline) |
| hilmar-tracker | ARCHIVED 2026-05-17 | All work merged into hilmar-daily-routine |
| air-profits-automation | active | Air freight profit automation |
| idealx-claude-standards | active | THIS FILE — canonical source |

---

## 12. The weekly cruft-audit routine

A claude.ai routine runs every Sunday 06:00 ET on every active repo:

- Modules with 0 importers
- `cmd_*` / route entry points with no caller
- `_check_*` methods not registered in run_all() / runQcAudit()
- Unused requirements / package.json deps
- Stale remote branches > 14 days, unmerged
- Dead helper functions (≤ 2 call sites)

The routine ends with a `WEEKLY_AUDIT_RESULT:` summary line. The
Monday session reads the transcript via standing rule #4 and
surfaces findings + closes the easy ones.

---

## 13. Open question protocol

When information is ambiguous or missing:

1. **State the open question explicitly.** Don't guess.
2. **Surface it in chat** with proposed options (a/b/c).
3. **Default to the SAFER option** if Michael doesn't pick.
4. **Never silently choose a path** that could be wrong without
   flagging.

For destructive operations (DROP TABLE, force-push, sudo permission
changes, mass deletions), ALWAYS ask even if rules above would
otherwise allow auto-action.

---

_Last updated: 2026-05-23. Bump version when changes land. The
weekly cruft routine cross-checks every repo's copy against the
canonical at `IdealX-dev/idealx-claude-standards`._
