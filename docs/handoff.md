# Session handoff — 2026-07-22 (evening)

Continue building the hosted RCA agent in `/Users/mohitrawat/projects/ingren/prod/agents`.
Python, canonical tree, deployed in place. Mohit fully understands every line —
deliberately slower is correct.

## Read first, in this order

1. `docs/design.md` — §8a–**§8e** amend/reverse everything; §8d and §8e are new
   this session. Read them before the rest
2. `docs/issues.md` — read #9 in full (it is mid-flight), then #6, #15
3. `rca/investigator/run.py` — the §8e retry guard is at the top of `main()`
4. `infra/provision.sh` + `infra/RUNBOOK.md` — what exists in AWS and how it ran

## State (as of commit `3392cb9`)

- **Closed:** #1, #2, #4, #5, #7*, #8, **#16** (NOTES are capture-only
  `instrument_note` events — §8d; ruled and landed 2026-07-22).
- **#15 in flight:** `infra/provision.sh` skeleton + SSM section live. 11
  SecureStrings under `/rca/` in **ap-south-1** (region ruled), account
  `537124933640`, profile `ingren`. Partner access is **AssumeRole**
  (`arn:aws:iam::356367897942:role/ingren-rca-readonly` + ExternalId), NOT
  static keys; profile `hb-role` locally. Script invocation:
  `uv run --project rca --env-file rca/.env bash infra/provision.sh`
  (bash `source` chokes on the `.env` format — do not revert that).
- **#9 in flight:** §8e ruled — machine retries `States.TaskFailed` blind
  (1 retry); `run.py` refuses re-runs of policy stops (exit 1/4) by reading
  the prior attempt's `run_failed` row. Migration 003 (agent SELECT on
  events) applied to Neon. 47 tests green.
- *#7 and #16 each keep one deferred box: they close on #9's first full run
  (record parity; an `instrument_note` at close-out). #9 lists both riders.

## Next: #9 units 3–5, in order

1. **Dockerfile** — Python 3.12 + uv, Node + `@anthropic-ai/claude-code`,
   AWS CLI, `~/.aws/config` with profile `hb-role` +
   `credential_source = EcsContainer` (then `aws_log.py` needs no change).
   No `.env` in the image — closes #6 boxes, proven from the container.
2. **provision.sh growth** — ECR, ECS cluster, log group, IAM roles (task,
   execution, SFN; task role gets `sts:AssumeRole` on the partner ARN with
   ExternalId), task definition (SSM `secrets` mapping, P9 §4 scoping:
   no bot token in the investigation task), state machine (name = incident
   id, input `{incident_id}` only, blind Retry ×1). ATTEMPT env var from
   `$$.State.RetryCount` — needs JSONata or States.MathAdd; check before
   writing ASL.
3. **Verify** — hand `StartExecution` against a seeded incident: duplicate
   start no-ops, killed task restarts once and writes `attempt = 2`, cap at
   2, budget/poison stop without re-investigation. Plus the two riders.
   Mohit runs all AWS mutations — hand over commands.

## Working rules (unchanged, CLAUDE.md)

- One file or coherent unit at a time, walked through in chat. Pause for
  questions. No batch code drops.
- DB writes, migrations, AWS resource creation: **Mohit runs them.**
- Never read `.env`.
- Small verify first; flag any run >2 min before starting it.
- Code review via Opus subagents for non-trivial diffs; record accepted
  findings in the issue.
- On a decent design question, ask "what's your guess?" before framing
  options. This worked well for §8e.

## Register

**ASD-STE100 for technical chat** — now enforced by a `UserPromptSubmit`
hook (global settings) that injects the rule every turn, added this session
after two lapses. Follow it. A memory file also reinforces it.

---

Start by reading the docs above, confirm state (`git log --oneline -5`,
47 tests via `uv run --env-file .env pytest -q` from `rca/`), then begin
the Dockerfile unit.
