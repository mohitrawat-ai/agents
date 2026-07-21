# Session handoff — 2026-07-22

Continue building the hosted RCA agent in `/Users/mohitrawat/projects/ingren/prod/agents`.
Python, canonical tree, deployed in place. Mohit fully understands every line —
deliberately slower is correct. Implement with Opus 4.8; run code review via Opus 4.8
subagents.

## Read first, in this order

1. `docs/design.md` — §8a and §8c amend/reverse everything; read them before the rest
2. `docs/decision.md` — only sections a slice cites (esp. P4 §6, P7, P8, §8a-A/B for #9)
3. `docs/issues.md` — the backlog; read #9, #15, #16 in full
4. `rca/investigator/procedure.md` — do not edit beyond ruled edits
5. `rca/investigator/run.py`, `hooks.py`, `tools/db.py` — the current SDK box and sink

## State (as of commit `17cbf14`)

- **Closed:** #1 (commit+ruff), #2 (schema on Neon), #4 (tools→Postgres, read_record),
  #5 (seed script), #7 (run.py takes `{incident_id}`, six exit codes, budget cap),
  #8 (PreToolUse boundary).
- **#6:** code half done (four `.env` loaders deleted, reads `os.environ`). Container
  half (no `.env` in image, per-task secret scoping, SLACK token swap) tracks #9/#11/#15.
- Postgres is **Neon**. Four connection strings in `rca/.env`: `DATABASE_URL` (owner,
  direct), `RCA_AGENT/SERVICE/POLLER_DATABASE_URL` (roles, pooled), and
  `RCA_DATABASE_URL` (role-neutral, = the agent string for the investigator).
- The record has a real verify run (slug `2026-07-18T02-47Z`) plus smoke attempts 1–6.

## Two decisions are Mohit's, both due now

Raise them, do not decide silently.

1. **#16 ruling** — NOTES appends vanish in a hosted container. Its implementation must
   land before #9's first containerized run. Three shapes drafted (emit-for-review /
   NOTES-to-Postgres / drop the instruction). Constraint: P5's threat model — any
   unreviewed agent write path into the NOTES is a prompt-injection persistence channel.
   Frame the shapes and their costs; let Mohit rule.
2. **Sequencing** — #9 (Step Functions state machine + restart Retry) needs AWS
   infrastructure, which needs #15 (checked-in AWS CLI provisioning script) — never
   started. #15 needs an AWS account + credentials configured, which is Mohit's to run.
   Ask whether to start #15 (provisioning) or discuss #9's shape first.

## Working rules (CLAUDE.md, non-negotiable)

- One file or coherent unit at a time, walked through in chat before the next. No batch
  code drops. Pause for questions.
- Database writes, migrations, AWS resource creation, account signups are **Mohit's to
  run** — hand him the exact command, never execute.
- Never read `.env`.
- Every issue ends by running its acceptance criteria and reporting results; verify
  against a real alert where the issue says so. Small verify first, flag any run >2 min.
- Code review: Opus 4.8 subagents (design + bug + security lenses; single reviewer for
  trivial/deletion diffs). Fix real findings, record accepted ones in the issue.
- On a decent design/trade-off question, ask "what's your guess?" before framing options.
- Match process weight to task weight — no heavy ceremony for mechanical edits.

## Register for technical chat

Ruled this session, in CLAUDE.md "Writing voice": **ASD-STE100 style** for
walkthroughs/runbooks/status — short sentences, one fact each, active voice, lists over
paragraphs. Lead with the action or the result.

## Skills

`/tdd` (used for #2), `/i-have-adhd` (project skill added this session).

---

Start by reading the docs above, confirm the state, then raise the two decisions.
