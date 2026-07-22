# Session handoff — 2026-07-22 (night)

Continue building the hosted RCA agent in `/Users/mohitrawat/projects/ingren/prod/agents`.
Python, canonical tree, deployed in place. Mohit fully understands every line —
deliberately slower is correct.

## Read first, in this order

1. `docs/design.md` — §8a–§8e amend/reverse everything. §7's Node gotcha was
   **corrected 2026-07-22**: the SDK wheel bundles a native `claude` binary,
   the image has no Node
2. `docs/issues.md` — read #10 in full (it is next), then #11, #15
3. `infra/provision.sh` + `infra/RUNBOOK.md` — everything that exists in AWS
4. `rca/Dockerfile` + `rca/entrypoint.sh` — the image; one image for all tasks

## State (as of commit `90eda40`)

- **Closed:** #1, #2, #4, #5, **#7** (parity ruled equal), #8, **#9** (all
  boxes verified live), #16.
- **#9 verified live 2026-07-22, all five tests:** hosted run $2.02/641s/67
  turns, same verdict as the #4-era record from 23 queries vs 35; duplicate
  `StartExecution` no-ops; killed task restarted once and wrote `attempt = 2`
  (proves the `States.MathAdd` ATTEMPT wiring); second kill → FAILED, exactly
  2 `TaskScheduled`; $0.10 budget run → attempt 1 exit 4, attempt 2 refused
  pre-spend ("refusing to re-run" in `/ecs/rca` logs, zero rows). §8e holds.
- **Live in AWS (ap-south-1, account `537124933640`, profile `ingren`):**
  ECR `rca` (arm64 image, `:latest`), log group `/ecs/rca` (30d), roles
  `rca-task-execution` / `rca-investigator` / `rca-sfn`, cluster `rca`,
  SG `rca-task` (no ingress), task def `rca-investigator` (rev 3 = normal
  command; rev 2 was the $0.10 test), state machine `rca-investigation`.
- **Image facts:** no Node — `claude_agent_sdk` wheels bundle the CLI binary
  (2.1.214 rides the SDK pin in `uv.lock`). No `.env`, proven. Non-root user
  `rca`. `entrypoint.sh` writes the `hb-role` profile from `PARTNER_ROLE_ARN`
  / `PARTNER_EXTERNAL_ID` (SSM-injected); tasks without the vars get none.
- **#6:** one box left — `SLACK_APP_TOKEN`→`SLACK_SIGNING_SECRET`, lands
  with #11's Slack app switch.
- **#15:** script covers everything through #9. SQS/ALB/canary land with
  #11/#13. Image push commands are in the RUNBOOK.
- Neon holds three verify incidents from today (slug `2026-07-18T02-47Z`,
  seeded via #5) plus the two older ones. Rows stay forever by design.
- 47 tests green (`uv run --env-file .env pytest -q` from `rca/`).

## Next: #10, the poller

Single-task Service. Read `docs/issues.md` #10 before building. The shape:

- Narrate milestones from `events` by row-id cursor; `tool_call` and
  `instrument_note` are not posted.
- Post the *"investigating…"* ack off `run_started` — idempotent across
  restarts by the same cursor.
- Terminal message from `DescribeExecution`, distinguishing #7's exit codes;
  a hard death writes no `run_failed` row, so the table alone is not enough.
- `ExecutionDoesNotExist` → posted failure (the never-started case, §8a-A).
- Errors logged, never swallowed (P8 §6).
- Needs: a task definition (poller DSN + bot token, no partner vars), a
  Service on cluster `rca`, and `states:DescribeExecution` on a poller task
  role. provision.sh grows.

## Working rules (unchanged, CLAUDE.md)

- One file or coherent unit at a time, walked through in chat. Pause for
  questions. No batch code drops.
- DB writes, migrations, AWS mutations: **Mohit runs them.** Read-only AWS
  calls are fine to run directly (this session's verify pattern worked well:
  Claude watches `DescribeExecution`, logs, and `SELECT`s while Mohit runs
  the mutating commands).
- Never read `.env`.
- Small verify first; flag any run >2 min before starting it, background it.
- Code review via Opus subagents for non-trivial diffs; record accepted
  findings in the issue.
- On a decent design question, ask "what's your guess?" before framing
  options.

## Register

**ASD-STE100 for technical chat** — enforced by a `UserPromptSubmit` hook
(global settings). Short sentences, one fact each, active voice, lists over
paragraphs.

---

Start by reading the docs above, confirm state (`git log --oneline -5`,
47 tests from `rca/`), then begin #10 with its task-definition unit.
