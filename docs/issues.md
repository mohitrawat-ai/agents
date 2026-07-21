# Issue backlog — drafted 2026-07-21

> **Revised 2026-07-21 for `design.md` §8a-F** — Python, one tree, in place.
> **#3 and #14 are struck**; #1, #7, and #12 lose their port framing. Nothing
> else moved, which is the evidence that the language was never the
> architecture.

Fourteen live issues covering `design.md` §6 (Slice 0–6) plus the gaps that §6
assumed but never gave a home. **Not yet published.** Numbers below are local;
they become real identifiers on publish, and "Blocked by" rewrites to match.
Struck issues keep their numbers so the blocking graph still reads.

Build order is horizontal on purpose (ruled 2026-07-21). This is a rework of a
working system in place, glue-first, so one half is always known-good.
**What "known-good" means changed under §8a-F**: not a second tree to diff
against, but #1's commit of the working system. Vertical re-slicing was
considered and rejected — it would trade that property for "demoable alone".

Every issue's verification is against a **real alert**. There are no tests
anywhere yet (P12), and the standing rule is that no slice lands without
saying how it was verified.

---

## 1 — Commit the working system, then lint and format

### What to build

**The commit comes first and it is the point of this issue.** This tree has no
commits. Under §8a-F there is no second tree holding a known-good copy, so
until the working system is committed, every issue below is an unrevertable
edit to the only version that has ever run a real incident.

Then the toolchain around it. `rca/pyproject.toml` already exists, so this is
linting, formatting, and the `uv` layout — not scaffolding from zero.

### Acceptance criteria

- [x] The working system is committed, unmodified, as the first commit (`0162a43`)
- [x] `ruff` (lint + format) configured and passing (`e1833d0`)
- [x] `.gitignore` covers `__pycache__`, `.venv`, and `.env`
- [x] No `.env` is tracked, and none ever becomes tracked
- [x] `uv run` starts the investigator from a clean checkout — verified 2026-07-21
      via a scratchpad clone running `--mock` against a real alert file; needed a
      missing-`.env` guard in `load_env_into_os` (crashed on any machine without
      one)

**Closed 2026-07-21.**

### Blocked by

None — can start immediately, and everything else is blocked on it.

---

## 2 — Schema: four tables, three roles, migrations

### What to build

The Postgres schema. **P2 is upstream of everything** — the router's lookup,
the poller's cursor, the `event_id` constraint, and every evidence row key off
it.

Four tables (`incidents`, `queries`, `events`, `documents`) and three roles
(`rca_agent`, `rca_service`, `rca_poller`) per `design.md` §5.

Two deltas from `decision.md`, both ruled in §8a:

- `queries` and `events` carry an **`attempt`** column (§8a-B). A restarted run
  re-runs every tool call under the same `incident_id` and the agent's role
  cannot clean the first set, so without this the self-check pass reads two
  interleaved investigations.
- **`dedup_key` and `condition_guess` do not ship** (§8a-D). Both are pure
  functions of `raw` and backfill when extraction works.

`rca_agent`'s read is scoped to its own incident **and its current attempt**.
Scope comes from the task environment, never from agent input — **wrapper-scoped
(re-ruled 2026-07-21, flipping P9 §5's original preference); RLS only if a
wrapper is ever found leaking scope**. The scoping itself is therefore proven in
#4, where the wrappers are built, not here.

Postgres itself is **Neon or Supabase** (§8c), created in #15 before this
issue. Migrations are **Mohit's to run.** Hand over the exact command.

### Acceptance criteria

- [x] Migrations create four tables and three roles (`rca/db/migrations/001`, `002`)
- [x] `event_id` is UNIQUE on `incidents`; `(channel, thread_ts)` indexed together
- [x] `queries` and `events` carry `attempt`
- [x] No `dedup_key`, no `condition_guess`
- [x] `rca_agent` has no `UPDATE` and no `DELETE` — **proven by a failing statement, not by reading the grant** (`tests/test_schema.py`, incl. TRUNCATE and `documents`)
- [x] `rca_poller` can `SELECT` `incidents` (it needs `channel`/`thread_ts`) and `UPDATE` only `narrated_through`
- [x] Migration command handed over, not executed — Mohit applied both against Neon; 22 tests green 2026-07-21

**Closed 2026-07-21.** Reviewed by three Opus subagents; two test-coverage gaps
fixed (documents/TRUNCATE denials). Accepted, not fixed: `\prompt` sends
cleartext `CREATE ROLE` statements the server could log (rotation via
`\password` declined); 002 is not re-runnable; no explicit `REVOKE CREATE ON
SCHEMA public` (Neon is PG15+).

### Blocked by

- #1
- #15 (a reachable Postgres)

---

## 3 — ~~Copy the Python tools into this tree~~ — STRUCK (§8a-F)

The whole issue was a consequence of the two-tree split: `prod/agents` was
frozen read-only, so the Postgres sink could not be written there. One tree
means the tools are edited where they are, and #4 does that directly.

**One piece survives and moves to #4:** deleting `nr_relationships.py` and
`nr_trace_probe.py` (§8a-E). That was ruled on its own merits, not as part of
the move.

### Blocked by

Nothing. Do not do it.

---

## 4 — Tools write to Postgres instead of jsonl

### What to build

Change the sink of `nrql_log`, `aws_log`, and `emit` from jsonl files to
Postgres, **in `rca/tools/` in place** (§8a-F). **Same CLI surface, same
stdout, new sink** — `procedure.md` does not change by a character at this
step, or at any later one.

**Inherited from the struck #3:** delete `nr_relationships.py` and
`nr_trace_probe.py`, and the `procedure.md:52` line that names them (§8a-E).
That line is the only edit `procedure.md` ever gets.

Two things this buys, both from P2 §1:

- `fcntl.flock` qid minting becomes `INSERT … RETURNING`, so the duplicate-`q02`
  bug class stops being *possible* rather than being carefully avoided.
- Narration becomes a query instead of a byte-offset tail, which is what #10
  depends on.

`emit` also owns the `documents` insert on `doc_ready` — the agent authors
`rca.md` with `Write`, but `emit` writes the row. That keeps invariant 2 true
for documents as well as telemetry.

Failed and dead-end queries are logged too. NR events expire at ~8 days, so
this is the only durable copy of what the investigation saw.

**`read_record.py` is built here too (§8c).** After this issue there is no
`queries.jsonl` for the self-check pass to verify against, so it reads evidence
back through this CLI — and #12's Q&A reuses the same one. This adds the second
ruled `procedure.md` edit: `:104-105` repoints from `queries.jsonl` to the read
CLI.

### Acceptance criteria

- [ ] All three tools write rows; no jsonl is produced
- [ ] `procedure.md` differs from #1's commit by exactly the two ruled edits (§8c): the deleted probe line and the self-check repoint
- [ ] `read_record.py` serves `--list` and `--qid`
- [ ] Both topology probe scripts are deleted
- [ ] qids come from `INSERT … RETURNING`; no `flock` remains
- [ ] Failed and errored queries produce rows
- [ ] Reads are scoped to the incident and attempt from the task environment; a flag aiming at another incident is ignored (moved from #2 — scoping is wrapper-level per the 2026-07-21 re-ruling of P9 §5)
- [ ] **Verify:** run the investigator against a real alert with the tools pointed at Postgres. The record is identical in content to a jsonl run — diff it against #1's commit.

### Blocked by

- #2

---

## 5 — Seed script: incident row from an alert file

### What to build

A one-off script that inserts an `incidents` row from one of the nine real
alert files in `prod/data/newrelic/incidents/`, and prints the incident id.

Needed because §8a-A changed the investigation task's input: it takes
`{incident_id}` and reads `raw` from Postgres, so there is nothing to run
against until a row exists. Three later issues assume this fixture and none of
them created it.

Kept deliberately dumb — no dedup, no parsing cleverness. It exists so #7, #9,
and #10 can be exercised before the router exists.

### Acceptance criteria

- [ ] Takes an alert file path, inserts one `incidents` row, prints the id
- [ ] Sets `channel` and `thread_ts` from arguments so the poller can post to a real thread
- [ ] Runs against all nine alert files without special-casing
- [ ] Insert is handed over as a command, not executed

### Blocked by

- #2

---

## 6 — Secrets: Parameter Store, delete the four `.env` loaders

### What to build

SSM Parameter Store SecureString → task-definition `secrets` → ordinary
environment variables. **Delete all four loaders rather than collapsing them**
(P9 §3) — with the platform injecting the environment, nothing parses anything.

`nr_run_nrql.py:16` is the one that matters: it reads `.env` from disk rather
than `os.environ`, so today the file must exist in the container and the `Read`
tool can open it. **#8's boundary does not hold until this ships.**

Local development keeps a `.env`, loaded outside the code. The application
never learns which environment it is in.

Secrets are scoped per task (P9 §4) — `SLACK_BOT_TOKEN` is **absent** from the
investigation task, so a compromised investigation cannot speak as the bot.

### Acceptance criteria

- [ ] All four loaders deleted; no code parses `.env`
- [ ] No `.env` in the image, proven from a running container
- [ ] Per-task scoping matches P9 §4, including `SLACK_BOT_TOKEN` absent from the investigation task
- [ ] `SLACK_APP_TOKEN` deleted; `SLACK_SIGNING_SECRET` added
- [ ] Local dev still runs via an external env file

### Blocked by

- #1

---

## 7 — Rework `run.py` + `hooks.py` for the task boundary

### What to build

The SDK-coupled box. This is the smallest interesting unit and the only code
that knows the Agent SDK exists — D3's portability seam. **Reworked in place
(§8a-F); the port is gone**, so what lands here is the P5/P6/P7 delta and
nothing else.

Takes `{incident_id}`, reads `raw` from Postgres, materializes `alert.json` in
its own workdir. **D3's seam is at the agent boundary, not the task boundary** —
the agent still sees `alert.json` in → record out.

Includes:

- **Distinguishable exit codes.** Today wall-clock breach, any exception, and
  `is_error` all return 1, so nothing downstream can tell a spot reclaim from a
  poison alert. ~20 lines, and it gates every retry policy plus the poller's
  terminal message.
- **`max_budget_usd`** (P6 §5b) — a per-run dollar stop, free from the SDK, and
  `error_max_budget_usd` is a distinguishable terminal result.
- **No `SessionStore`** — deferred by §8a-B.

`procedure.md` stays where it is, next to the code that loads it. Its one edit
happened in #4.

### Acceptance criteria

- [ ] One real alert end-to-end produces a record equal in content to a #4 run
- [ ] Wall-clock breach, exception, `is_error`, and budget breach return four distinguishable codes
- [ ] `max_budget_usd` set; a deliberately low value stops the run and reports it
- [ ] `procedure.md` is untouched by this issue
- [ ] `CLAUDE_CONFIG_DIR` points at a temp dir

### Blocked by

- #4, #6

---

## 8 — `PreToolUse` boundary: allowlist and confinement

### What to build

Invariant 4, and the item `decision.md` calls the one most likely to matter to
the design partner — these are the **partner's own credentials**, so the agent
is one bad command away from mutating their production monitoring.

Allowlist the **executable**, not the command string. Command-string filtering
is bypassable by `eval`, base64, `python3 -c`, `curl`, and variable indirection;
allowlisting the executable denies precisely those escape hatches. Four
invocations are permitted — the three writers plus `read_record.py`
(§8a-E, §8c) — and everything else is denied.

`Read`/`Glob`/`Grep` confined to the run directory and the tools directory —
including `/proc`, since environment variables are readable via
`Read /proc/self/environ` and blocking `env` in Bash while leaving `/proc` open
locks one door and leaves the one beside it open.

Use a `PreToolUse` hook, not `can_use_tool` — the latter is shadowed by
whole-tool `allowed_tools` entries and would be dead code (P5 §1).

`check_read_only` in `aws_log` is **promoted, not demoted**: once `aws` is
unreachable except through the wrapper, it is the only thing between the agent
and the partner's AWS account. Test it as such.

### Acceptance criteria

- [ ] The four permitted invocations succeed
- [ ] `curl`, `env`, `python3 -c`, `bash -c`, and a base64-decoded command are each denied
- [ ] `Read /proc/self/environ` is denied
- [ ] `Read` outside the run and tools directories is denied, including the transcript directory
- [ ] `check_read_only` has direct tests for the mutating verbs it must refuse
- [ ] A real alert still completes with the hook in place

### Blocked by

- #7

---

## 9 — Step Functions: state machine and restart Retry

### What to build

Execution name = incident id, which makes `StartExecution` naturally idempotent
for 90 days (P4 §6). **Execution input is `{incident_id}` and nothing else** —
name-based idempotency holds only on identical input, and a differing payload
raises `ExecutionAlreadyExists` instead of succeeding quietly (§8a-A).

Per-failure-class retriers, feeding off #7's exit codes:

- **Infrastructure tier** — auto-**restart**, capped at 2 attempts (§8a-B). A
  `Retry` block. **No resume state, no `SessionStore`, no S3.**
- **Everything else** stops and posts. Budget breach is a policy stop and is
  never retried. Poison `alert.json` is never retried.

A restart increments `attempt`, so its second set of evidence rows stays
distinguishable from the first.

### Acceptance criteria

- [ ] Execution name is the incident id; a duplicate `StartExecution` with identical input is a no-op
- [ ] Execution input carries only `{incident_id}`
- [ ] A killed task restarts once and writes `attempt = 2`
- [ ] A restart caps at 2 attempts
- [ ] Budget breach and poison alert both stop without retrying
- [ ] Verified by `StartExecution` by hand against a seeded incident

### Blocked by

- #7

---

## 10 — Poller: narration, terminal message, never-started

### What to build

A single-task poller. Narration is a **pull**, not a push — a runner that posts
to Slack couples the investigation container to Slack and breaks the D3 seam
that P2 preserved, which is also why #6 removes `SLACK_BOT_TOKEN` from the
investigation task.

One message per milestone, appended, not edited. Slack sends no notification on
edit, and the entire point is that on-call learns at 3am that a hypothesis was
ruled out. Measured volume is nine posts per run against a limit of roughly one
per second — 48x of headroom, so there is no rate argument for an edited
checklist.

Single task rather than a lease: two Service tasks would both post everything
twice, and a claim-with-lease is a distributed-systems mechanism introduced to
solve a problem created by running two tasks. Narration dying never loses the
record.

Two authorities — the events table says what was found, `DescribeExecution`
says whether the run is over. **A hard task death writes no `run_failed` row**,
so a table-only poller waits forever.

Also owns two things moved here by §8a-A:

- the *"investigating…"* ack, off the `run_started` row, so it is idempotent by
  the same cursor as every other line
- the **never-started** case: an incident whose execution answers
  `ExecutionDoesNotExist` gets a posted failure. That is the one router crash
  path that does not self-heal.

**Stop swallowing exceptions** (P8 §6). Today a bad token makes the thread go
quiet, indistinguishable from a slow investigation. Something whose only job is
talking should be loudly broken when it cannot talk.

### Acceptance criteria

- [ ] Narrates milestones from `events` by row-id cursor; `tool_call` is not posted
- [ ] Posts the ack off `run_started`, exactly once across a restart
- [ ] Terminal message comes from `DescribeExecution` and distinguishes #7's exit codes
- [ ] `ExecutionDoesNotExist` posts a failure rather than waiting forever
- [ ] A killed task mid-run still produces a terminal message
- [ ] Errors are logged, not swallowed; repeated failures are a liveness signal

### Blocked by

- #9, #5

---

## 11 — Ingress and router: one queue, upsert, StartExecution

### What to build

`slack_bolt` over HTTP. **One queue** (§8a-A) — `inbound` carries raw Slack
envelopes, and the router calls `StartExecution` directly.

**Return 200 the instant the request is durable, and not before.** Acking late
crosses Slack's 3-second deadline and buys a duplicate $2 run; acking early and
working in memory loses a 3am alert with no trace, because we already said we
succeeded. Nothing fallible sits in front of the ack: verify the signature,
write one message, return. No Slack API call, no record read. An enqueue
failure must return non-2xx so Slack retries — that is correct behaviour.

Routing is **by thread, not by message content**, and happens behind the queue.
First tag in a thread is an alert; anything after is a question. Content
routing was rejected on one case: *"@rca is this the LB thing again?"* — tagged
on the alert, first contact, with commentary. Content routing calls that a
question and a real alert goes uninvestigated because the human was chatty.

The three §8a-A conditions all land here, and the redundancy argument for
dropping the second queue is only valid if all three hold:

1. **The upsert uses `DO UPDATE`, not `DO NOTHING`**, so it always returns the
   id. `DO NOTHING` returns nothing on conflict and deliberately does not wait
   on the concurrent insert, so a follow-up `SELECT` can also come back empty —
   and a router that reads that as "someone else has it, drop it" never starts
   the run after a crash. An alert silently goes uninvestigated.
2. **Execution input is `{incident_id}` and nothing else.**
3. **Nothing non-idempotent between the two calls** — the ack belongs to #10.

Also: channel allowlist (an invite to `#general` should not be able to spend
money), rate limit of **5 investigations per 10 minutes, global**, refusing in
the thread. The limit is a backstop against a bug, not a policy against users —
manual tagging peaks at 2–4 in a cascade, a runaway loop is many per second, so
any threshold between them works and the number never needs tuning.

**No dedup check** (§8a-D).

**Log the raw Slack envelope of the first real alert.** There are zero samples
of an alert as a Slack message, and that is the only form `parse_alert` will
ever see. Costs nothing beyond the write, and it is what unblocks dedup later.

### Acceptance criteria

- [ ] Signature verified; 200 returned after the durable write and before any other I/O
- [ ] An enqueue failure returns non-2xx
- [ ] Upsert uses `DO UPDATE` and always returns an id
- [ ] Execution input carries only `{incident_id}`
- [ ] The router makes no Slack post
- [ ] **Kill the router between the upsert and `StartExecution`, and again after it — exactly one investigation in both cases, and none lost**
- [ ] Two concurrent deliveries of the same `event_id` start exactly one run
- [ ] Channel allowlist enforced; rate limit refuses in-thread at the 6th in 10 minutes
- [ ] Raw envelope logged
- [ ] Slack app switched to an HTTP Request URL

### Blocked by

- #9, #6

---

## 12 — Q&A: read CLI, prompt-inlined `rca.md`, no `Bash`

### What to build

Q&A answers follow-up questions in an incident's thread, grounded in that
incident's record. §8a-C changed its shape substantially from `decision.md`.

**No folder export.** `rca.md` goes into the prompt — it is a few KB and it is
what most questions are about. Evidence comes from a read CLI
(`read_record.py --qid q03` / `--list`), symmetric with the write CLIs —
built in #4 (§8c), reused here.

**Q&A loses the general shell**, and this is the point of the issue. Q&A runs in-process
on the Service, which holds `SLACK_BOT_TOKEN`, `SLACK_SIGNING_SECRET`,
`ANTHROPIC_API_KEY`, and a Postgres role with `UPDATE`. Its input is incident
data — the log content that is P5's named injection vector. It had a **worse**
position than the investigation task, which is the one P5 spent a whole ruling
hardening. It gets the same `PreToolUse` allowlist, permitting one executable.

**The CLI takes its incident id from the environment and ignores any flag that
would set it.** The agent's shell inherits that environment and could otherwise
aim it at another incident.

Charts are deferred (§8a-C) — no matplotlib, no tmpdir.

`_handle_question`'s timeout must post (P8 §7). Today it raises in a daemon
thread with no handler, leaving the user on *"Looking at the record…"* forever.
A dead end the user can act on beats silence.

Record Q&A cost (P6 §6) — `run.py` already writes `cost_usd` and Q&A drops it,
so the daily spend alarm currently measures half the spend.

### Acceptance criteria

- [ ] `rca.md` inlined in the prompt; no tmpdir is created
- [ ] `read_record.py` serves `--list` and `--qid`
- [ ] A `--incident` flag pointing at another incident is ignored, and the scope stays the environment's
- [ ] `Bash` stays as the vehicle; the `PreToolUse` hook denies every invocation except `read_record.py`. `Read` and `Write` are denied entirely
- [ ] Timeout posts a message rather than hanging
- [ ] `cost_usd` recorded per question
- [ ] A real follow-up question in a real thread returns an answer citing a real qid

### Blocked by

- #11, #2

---

## 13 — Liveness: ping path, Synthetics canary, alarm, SNS

### What to build

The monitoring story today is `pgrep -fl daemon.py` typed by hand. The system
whose job is noticing production problems can be silently dead, and that failure
is indistinguishable from a quiet week.

Platform liveness is already free — ALB health checks, ECS desired-count,
Step Functions status. **None of it catches the failure that matters:** Slack
credentials revoked, Slack disabling event delivery, the bot removed from the
channel, a consumer alive but no longer polling. Every one presents identically
as "no alerts arrived", and so does a quiet week. A heartbeat from our own
process proves the process runs and proves nothing about the Slack delivery
path, which is the fragile link.

A CloudWatch Synthetics API canary posts `@rca ping` into a canary channel and
asserts a reply, traversing Slack → ALB → verify → `inbound` → router → post.

**The ping check sits AFTER the thread lookup, not before.** A ping that
short-circuits at the top of the router would pass clean through a broken
routing lookup — the thing P3 spent five questions on.

**The alarm never lands in Slack.** Alerting on the alert-responder cannot
depend on the alert-responder, and Slack is the dependency most likely to be
what broke. CloudWatch alarm → SNS. This is the one deliberate exception to the
one-substrate rule: a monitor that shares fate with the thing it monitors is
not a monitor.

Daily now, 15 minutes at go-live.

### Acceptance criteria

- [ ] `ping` handled after the thread lookup, proven by breaking the lookup and watching the canary fail
- [ ] Canary asserts a reply, not just a 200
- [ ] Alarm routes to SNS and never to Slack
- [ ] A revoked Slack token fails the canary
- [ ] Interval documented as daily now, 15 minutes at go-live

### Blocked by

- #11

---

## 14 — ~~Tools to TypeScript; `procedure.md`'s three-line edit~~ — STRUCK (§8a-F)

The tools stay Python, in place. `procedure.md` keeps its `python3`
invocations, so the three-line edit that P11 §5 carved out of invariant 1 never
happens and the invariant returns to P2's original wording: **not one
character.**

The one thing this issue carried that mattered — moving the tools' sink to
Postgres — is #4, and it does not need the language change to happen.

Two smaller things go with it, both named in §8a-F:

- **Python does not leave the image**, and Node does not either. The SDK spawns
  the `claude` Node CLI regardless. Two runtimes, one language in our code.
- **#8's allowlist is written once**, against `python3 <tool>.py`, and never
  rewritten against `node <tool>.js`. P5 §6 is indifferent to which, but the
  re-verification pass against the same bypass attempts is work that now
  disappears.

### Blocked by

Nothing. Do not do it. **The build ends at #13** (#15 is provisioning
underneath the slices, not a new slice).

---

## 15 — Provisioning: AWS CLI script, and the managed Postgres

### What to build

Ruled in `design.md` §8c. A checked-in script of `aws` CLI commands that
creates every AWS resource the slices assume: SQS queue + DLQ, the state
machine, ECS cluster + Service + poller + task definitions, ECR, ALB, IAM
roles, SSM parameters, the canary, alarm, and SNS. Resources land as their
issues need them — the script grows with the build, it does not have to be
complete before #2.

Postgres is **Neon or Supabase** (§8c), created before #2; the connection
string goes into SSM like any other secret. Account and database creation are
**Mohit's to run** — the script covers AWS only.

No Terraform. The named failure that would change this: drift that bites, or a
rebuild the script cannot do.

**Progress 2026-07-21:** Postgres is **Neon** (ruled in-session: pooling fits
the ~45 short-lived tool connections per run; cold start accepted). Project
created by Mohit; role strings use the pooled endpoint, owner string direct,
all local in `.env`. SSM lands with the AWS script.

### Acceptance criteria

- [ ] Script checked in; every AWS resource the slices use is created by it
- [ ] Re-running against an existing stack is safe
- [ ] One-time manual steps (account signup, Slack app config) are written down next to it
- [x] Postgres reachable (Neon, 2026-07-21); connection string in SSM pending
- [ ] No resource exists that the script or its runbook lines don't record

### Blocked by

- #1. Blocks #2 (the database), and #9/#11/#13 as their AWS resources come up.

---

## 16 — NOTES appends vanish in a container: rule where instrument memory goes

### What to decide, then build

**Found 2026-07-21 tracing the close-out path.** `procedure.md:122` orders the
agent to append instrument learnings to `NR_NOTES.md` / `CW_NOTES.md`. On the
laptop that works because the tools dir is the repo working tree — a
persistent disk, shared across runs by git. Hosted, the tools dir is baked
into the image and the container is destroyed at task exit, so **the append
silently vanishes** and every future run reads stale NOTES. The agent believes
it is remembering; nothing is retained.

This is the last unswept shared-disk assumption. P2 hunted the others (the
record, the events tail, qid minting, the Q&A folder) but this write targets
the *tools* dir, not the *incident* dir, and escaped the sweep. The NOTES are
the system's only cross-run memory — the whole reason Setup step 2 reads them
— so losing the write side means instrument knowledge stops compounding the
day the system goes hosted.

**This needs a ruling before it needs code.** Candidate shapes, with their
costs:

| Option | Mechanism | Cost |
|---|---|---|
| Emit-for-review | agent emits a `notes_proposed` event; the row surfaces for review; accepted entries are committed to the repo and ride the next image | a new event type, and a human in the loop |
| NOTES to Postgres | a table read at boot, agent gains a write path to it | agent-writable shared state — see the constraint below |
| Drop the instruction | delete `procedure.md:122`'s append step until a mechanism exists | memory stops compounding, openly instead of silently; a third `procedure.md` edit |

**Constraint from P5's threat model, whichever way it goes:** the NOTES are
read as trusted guidance by every future run. An unreviewed agent write path
into them is a *persistence* channel for log-content prompt injection — a
poisoned line written today is instructions to every investigation after it.
Any ruling that lets agent text reach the NOTES without review has to answer
this.

**Invariant 1 is touched.** Options 1 and 3 edit `procedure.md` (the close-out
wording), which currently allows exactly two ruled edits, both in Slice 1. A
third needs its own ruling recorded there, same as §8c's.

### Acceptance criteria

- [ ] A ruling in `design.md` (§8a or §8c style) naming the chosen shape and
      what it rejected
- [ ] If `procedure.md` changes, invariant 1's edit list is updated in the
      same commit
- [ ] After one hosted run, a NOTES learning demonstrably survives to the next
      run's Setup read — or the append instruction demonstrably no longer
      exists
- [ ] `feedback.md` explicitly out of scope (per-incident by design; its
      fill-in loop is P13)

### Blocked by

- The ruling. Implementation must land before the first containerized run
  (#9) — that is when appends start vanishing. If the ruling edits
  `procedure.md`, the edit lands with #4's.
