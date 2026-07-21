# prod/agents — build design

**Status (2026-07-21): design closed, implementation not started.** Nothing is
unruled.

> **Language and tree reversed 2026-07-21 — see §8a-F.** This system stays
> **Python**, and it is built in **`prod/agents`**, in place. P11 ruled
> TypeScript in a separate `prod/ingren-agents` tree; that ruling is reversed
> and that tree is a tombstone. Everywhere below that still reads as a port —
> Slice 7, the three-line `procedure.md` edit, the copy-the-tools step — is
> gone. This document has been updated throughout; §8a-F holds the reasoning.

This document says **what to build and in what order**. It does not re-argue the
decisions — the reasoning, the rejected alternatives, and the prior art all live
in the decision register:

> **`docs/decision.md`** — P1–P11, all ruled 2026-07-20, **moved into this tree
> 2026-07-21**. Read it before changing anything here. Where this document says
> "per P4 §2", that is the section to read.
>
> Predecessor: `ingren-rca/docs/plans/rca-harness/design-v2.md` (D1–D14) — what
> the agent *is*. Still authoritative on the investigation itself.

**§8a is the exception to "does not re-argue."** A doc review on 2026-07-20/21
found rulings whose ground a *later* ruling had moved, and ruled them here
rather than leaving them stale:

| | Amends | Now |
|---|---|---|
| **A** | P3 | one queue; the router calls `StartExecution` directly |
| **B** | P7 | auto-**restart**, not resume; S3 session mirror deferred |
| **C** | P2 §3, P5 §5 | no Q&A export; a read CLI, and Q&A loses `Bash` |
| **D** | P4 §3–4 | dedup is not built; capture a Slack-rendered alert instead |
| **E** | P5 §1, D11 | `procedure.md:52` deleted; the topology probes were never tools |
| **F** | P11 | **reversed** — Python, built in `prod/agents` in place. One tree |

`decision.md` still holds the *why* for everything else, and each amended
ruling there carries a **⚠** marker pointing back here. E and F both touch §4
invariant 1, and both make it stronger — E returns it to three lines, F takes
it to zero.

---

## 1. What this system does

A New Relic or CloudWatch alert lands in a Slack channel. An on-call engineer
tags the bot on it. The bot runs a headless Claude Code agent that investigates
the incident against real telemetry, narrates what it finds into the thread, and
produces `rca.md` — a root-cause document where every quantitative claim cites a
logged query. Afterwards the engineer can ask questions in the same thread and
get answers grounded in that incident's record.

It is moving from a laptop daemon to a hosted service the design partner's
on-call depends on.

---

## 2. Where things live

| Tree | Role |
|---|---|
| **`prod/agents`** | **This tree. Python. Canonical, and the deployment target.** The only version that has run a real incident. Built on in place. |
| `prod/ingren-agents` | **Tombstone (§8a-F).** TypeScript target that never got code. Its four doc commits are the provenance of `docs/`; nothing else is there. Do not build in it. |
| `prod/ingren-rca` | Retiring. Do not import from or reference into it. |
| `prod/data/newrelic/incidents/` | Nine real incident folders, including the one scored run (`2026-07-18T02-47Z`, 432s / 52 turns / $2.03). Test fixtures and ground truth. |

**There is no reference copy to diff against any more, and that is the cost
§8a-F accepts.** The known-good system and the thing being changed are now the
same files. What replaces the diff is git: this tree has **no commits yet**, so
the first task is to commit the working system *before* Slice 1 touches it. That
commit is the reference implementation from then on.

---

## 3. The system

```
                    ┌──────────────────────────────────────────────┐
 Slack  ──ALB──▶    │  Service (Fargate, 2 tasks, always warm)      │
                    │                                              │
                    │  ingress:  verify signature                  │
                    │            → SQS(inbound) → HTTP 200          │
                    │                                              │
                    │  router:   poll inbound                       │
                    │            → look up (channel, thread_ts)     │
                    │              known   → answer Q&A in-process  │
                    │              unknown → upsert incident        │
                    │                        → StartExecution       │
                    └──────────────────────────────────────────────┘
                                        │
                    ┌───────────────────▼──────────────────────────┐
                    │  Step Functions                              │
                    │    execution name  = incident id             │
                    │    execution input = {incident_id} and       │
                    │                      nothing else            │
                    │    → Fargate Task (one per investigation)    │
                    │    → auto-restart, capped at 2 attempts,     │
                    │      infrastructure failures only            │
                    └──────────────────────────────────────────────┘

                    ┌──────────────────────────────────────────────┐
 Slack  ◀───────    │  Poller (Fargate, 1 task)                     │
                    │    events table      → narration             │
                    │    DescribeExecution → terminal message,      │
                    │                        and "never started"   │
                    └──────────────────────────────────────────────┘

  Postgres  →  evidence + operational state   (forever; evidence backed up)
  S3        →  session mirror                 (deferred — §8a-B)
  Synthetics canary → CloudWatch alarm → SNS  (never Slack)
```

### Components

| Component | ECS shape | Responsibility | Rulings |
|---|---|---|---|
| **Service — ingress** | part of the 2-task Service | Verify Slack signature, write raw envelope to `inbound`, return 200. **No other I/O.** | P3 §1, §2 |
| **Service — router** | same tasks, consumes `inbound` | Look up `(channel, thread_ts)`. Known thread → Q&A in-process. Unknown → parse, upsert the incident, `StartExecution`. **Those two writes and nothing else.** | P3 §3, §8a-A |
| **Service — Q&A** | same tasks, in-process | `rca.md` in the prompt, evidence via a read CLI scoped by environment, no `Bash`, no tmpdir. Post the answer. | §8a-C, P8 §7 |
| **Investigation task** | one Fargate Task per run | The Claude Agent SDK box. Takes `{incident_id}`, reads `raw` from Postgres, materializes `alert.json` in its own workdir → record out. **D3's seam is at the agent boundary, not the task boundary** — the agent still sees `alert.json` in → record out. | P1, D3, §8a-A |
| **Poller** | separate 1-task Service | Narrate milestones from the events table; post the terminal message from Step Functions; post the never-started case (§8a-A). Also posts the *"investigating…"* ack, off `run_started`. | P8, §8a-A |
| **Canary** | CloudWatch Synthetics | `@rca ping` end-to-end every 24h (15m at go-live). | P10 |

---

## 4. Invariants — do not break these

These are the load-bearing constraints. Breaking one silently is how this
system produces confident wrong answers.

1. **`procedure.md` does not change by a single character.** This is P2's
   original wording, and §8a-F restores it. The three-line `python3 <tool>.py`
   → `node <tool>.js` edit that P11 §5 carved out has no reason to happen: the
   tools stay Python and stay where they are. It is the highest-value proven
   artifact in the system.

   The one deletion that still applies is §8a-E's — the topology-probe line at
   `:52`. That is a removal ruled on its own merits, not a port artifact, and it
   happens once.

2. **The tools are the only path to telemetry.** `nrql_log`, `aws_log`, and
   `emit` are the sole structured writers; `procedure.md` forbids any other
   route. That choke point is what makes the record complete. (D11, P2 §1)

   This was **not** true in `prod/agents` — `procedure.md:52` offered two
   topology probes that wrote no evidence row. §8a-E deletes that line, which
   is what makes the invariant hold rather than merely be asserted.

3. **The record is append-only at the database level, not by convention.** The
   agent's role has `INSERT` and a scoped `SELECT`. No `UPDATE`, no `DELETE`, no
   cross-incident read. (P2 §2, amended by P9 §5)

4. **Both agents get a `PreToolUse` executable allowlist. Neither gets a
   general shell.** The investigator may run only the executables
   `procedure.md` names; Q&A may run only the read CLI (§8a-C). Everything
   else is denied. `check_read_only` in `aws_log` is load-bearing, not
   decorative — it is the only thing between the agent and the partner's AWS
   account. (P5 §1, §2, amended by §8a-C)

   Q&A is the one that looks safe and isn't: it runs in the Service process,
   which holds `SLACK_BOT_TOKEN`, `SLACK_SIGNING_SECRET`, and a Postgres role
   with `UPDATE`.

5. **Return 200 the instant the request is durable, not before.** Early loses
   alerts silently; late duplicates paid runs. (P3 §1)

6. **Under uncertainty, take the loud cheap error.** A duplicate $2 run is
   visible. A suppressed alert looks like the system working. This principle
   decides P3 §3, P4 §4, and P8 §4 the same way — and §8a-A, §8a-C, and §8a-D
   after them.

7. **The investigation stays one opaque task.** Step Functions does not
   orchestrate its internals. Swapping harnesses later means rewriting the SDK
   box and its hooks, nothing else. (D3, P1)

---

## 5. Data model

> **Derived from the rulings, not itself ruled.** The *invariants* below are
> ruled and must hold. The column layout is a starting sketch — change it where
> it fights, but not in a way that breaks §4.

### `incidents` — one row per investigation

| Column | Notes |
|---|---|
| `id` | uuid PK. **The identity.** Also the Step Functions execution name (P4 §6). |
| `event_id` | **UNIQUE, no expiry.** Slack's delivery id. The idempotency guard. (P4 §6) |
| `channel`, `thread_ts` | **Indexed together** — the router's lookup. (P3 §3) |
| `slug` | `%Y-%m-%dT%H-%MZ`. **Display label only, allowed to collide.** (P4 §2) |
| `raw`, `received_utc` | from `parse_alert`. `dedup_key` and `condition_guess` **do not ship** — §8a-D. Both are pure functions of `raw` and backfill when extraction works |
| `narrated_through` | the poller's cursor into `events` (P8 §4) |
| `created_at` | kept; the dedup window that would have used it is deferred (§8a-D) |

`<slug>-<short-id>` — e.g. `2026-07-20T10-15Z-a3f1` — is the readable-and-unique
name. It survives §8a-B and §8a-C, which removed both of P4 §2's original
consumers: it now names the investigation task's workdir, and the S3 prefix if
the mirror is ever built.

### `queries` — evidence, one row per telemetry call

Monotonic `id`; `incident_id`; `attempt`; `qid` minted by `INSERT … RETURNING`
(this deletes the `flock` duplicate-`q02` bug class); `source` (`nrql` / `aws`);
the query text; `elapsed_s`; `rows`; results as `jsonb`; error. **Failed and
dead-end queries are logged too** — NR events expire at ~8 days, so this is the
only durable copy of what the investigation saw.

**`attempt` is load-bearing under §8a-B.** A restarted run re-runs every tool
call under the same `incident_id`, and the role cannot clean the first set. The
self-check pass reads back only the current attempt; without the column it
reads two interleaved investigations and the citations stop meaning one thing.
It comes from the task environment, not from agent input.

### `events` — one row per milestone or tool call

Monotonic `id` (the poller's cursor keys off it); `incident_id`; `attempt`;
`ts`; `event` name; payload as `jsonb`. Milestones (`hypothesis`, `timeline_settled`,
`self_check`, `doc_ready`, `run_failed`) are narrated; `tool_call` is not.

### `documents`

`rca.md` and `feedback.md` per incident, plus `attempt`. `rca.md` is the
exception to the tool path — the agent authors it with `Write`, and `emit`
uploads it on `doc_ready`. **`emit` does the insert, not the agent**, which is
what keeps the tool boundary (invariant 2) true for documents as well.

This table is Q&A's primary input under §8a-C: `rca.md` goes into the prompt.

### Roles (P9 §5)

| Role | Grants |
|---|---|
| `rca_agent` | `INSERT` on evidence; `SELECT` **scoped to its own incident and its current `attempt`** (the self-check pass reads back `queries` — see `procedure.md:104-105`; the attempt scope is §8a-B). No `UPDATE`, no `DELETE`. |
| `rca_service` | `SELECT`/`INSERT`/`UPDATE` on operational state; `SELECT` on evidence, including `documents` — Q&A reads `rca.md` through this role (§8a-C) |
| `rca_poller` | `SELECT` on `events`; `SELECT` on `incidents` (it needs `channel`/`thread_ts` to post, and the active list to loop over — P9 §5 omits this); `UPDATE` on `narrated_through` only |

### S3 — session mirror — **deferred, §8a-B**

Not built. Nothing in the build order below depends on it, and no S3 bucket
ships until a §8a-B trigger fires. The design below is P2 §5's, kept intact for
when it does.

`s3://…/<run-id>/00001.jsonl`, one new object per **15-second** flush. S3 has no
append, so the adapter adds keys rather than rewriting. `load()` lists the
prefix and concatenates. Lifecycle rule handles expiry — no delete job. (P2 §5)

**The reference adapter is TypeScript-only, and §8a-F gives it up.** The
TypeScript SDK repo ships `examples/session-stores/s3` implementing exactly
this shape plus a conformance suite; P11 §4 counted that as a reason to go
TypeScript. In Python it is a hand-written adapter again — but §8a-B had
already deferred the whole session store, so this costs nothing until a §8a-B
trigger fires. If one does, the TypeScript example is still worth reading as a
spec before writing the Python version.

---

## 6. Build order

Each slice is independently verifiable. Do not start a slice whose predecessor
isn't running.

### Slice 0 — tree setup, and the first commit
**Commit the working system as-is, before anything below touches it** (§2).
This tree has no commits; until it has one there is no revert point and no
reference implementation.

Then the toolchain: `pyproject.toml` already exists at `rca/`, so this is
linting, formatting, and the `uv` layout — not scaffolding from zero. `docs/`
arrived here 2026-07-21 from the tombstoned tree.

### Slice 1 — schema and the tools' sink
**Start here. P2 is upstream of everything.** The router's lookup, the poller's
cursor, the `event_id` constraint, and all evidence rows key off it.

Migrations for the four tables and three roles. Change `nrql_log`, `aws_log`,
and `emit` to write Postgres instead of jsonl — same CLI surface, same output,
new sink. `procedure.md` does not change.

**The tools are edited in place, in `rca/tools/`** (§8a-F). The copy-into-the-
other-tree step is gone; it existed only to keep a frozen reference in a
different language. Slice 0's commit is the reference now.

**`nr_relationships.py` and `nr_trace_probe.py` are deleted** (§8a-E), along
with `procedure.md:52`.

*Verify:* run the investigator against a real alert. The record should be
identical in content to a jsonl run — diff it against Slice 0's commit.

### Slice 2 — secrets
Parameter Store SecureString → task-definition `secrets` → environment
variables. **Delete all four `.env` loaders rather than collapsing them.** No
`.env` ships in the image. Local dev loads it outside the code.

**Sequenced before the investigation task, because Slice 3's boundary does not
hold without it** — today `nr_run_nrql.py:16` reads `.env` from disk, which the
`Read` tool can open. (This was Slice 3 until 2026-07-20; it was listed after
the slice that depends on it.)

### Slice 3 — the investigation task
Rework `run.py` + `hooks.py` in place. This is the SDK-coupled box and the
smallest interesting unit. It is no longer a port — §8a-F removed the rewrite,
so what lands here is the delta P7/P5/P6 asked for and nothing else.

`procedure.md` stays where it is, next to `run.py` which loads it. Its only
edit is §8a-E's deleted line, applied in Slice 1.

Includes three preconditions at once, because they all live here:
- **Distinguishable exit codes** (~20 lines). Today wall-clock breach, any
  exception, and `is_error` all return 1. **This gates every retry policy** and
  it is what lets the poller's terminal message say more than `exit 1`.
- **`PreToolUse` hook** denying anything but the tool invocations
  `procedure.md` names, plus `Read`/`Glob`/`Grep` confinement including
  `/proc`. (P5 §1, §4) Three executables, per §8a-E.
- **`max_budget_usd`** (P6 §5b). **No `SessionStore`** — deferred by §8a-B.

*Verify:* one real alert end-to-end, producing a record equal in content to a
Slice 1 run. **Seeding is manual at this point** — under §8a-A the task takes
`{incident_id}` and reads `raw` from Postgres, so there is nothing to run
against until a row exists. A one-off seed script that inserts an incident from
one of the nine `prod/data` alert files is the fixture, and it stays useful for
every later slice.

### Slice 4 — Step Functions and the poller
**Reordered ahead of the router by §8a-A.** The router now calls
`StartExecution` directly instead of enqueueing, so the state machine has to
exist before the router does. Under P3's two-queue shape the router could ship
against a queue nobody consumed yet; that slack is gone.

The state machine (execution name = incident id) and the per-failure-class
retriers — **a `Retry` block that restarts, capped at 2 attempts, infrastructure
tier only. No resume state** (§8a-B). The poller narrating from `events`, taking
terminal status from `DescribeExecution`, posting the *"investigating…"* ack
off `run_started`, and posting the **never-started** case when
`DescribeExecution` answers `ExecutionDoesNotExist` (§8a-A).

*Verify:* `StartExecution` by hand against a seeded incident. Narration lands in
a real thread, the terminal message is right, and a killed task restarts once
and writes `attempt = 2`.

### Slice 5 — ingress and router
`slack_bolt` over HTTP. Verify → `inbound` → 200. **One queue** (§8a-A).
`daemon.py` runs Socket Mode today; this replaces that path rather than porting
it.
Router polls, looks up the thread, and either answers in-process or upserts the
incident and calls `StartExecution`. Channel allowlist. Rate limit: **5
investigations per 10 minutes, global**, refusing in the thread. **No dedup
check** (§8a-D).

**Log the raw Slack envelope of the first real alert.** It is the sample nobody
has (§8b), it costs nothing beyond the write, and it is what unblocks dedup and
the `parse_alert` hardening later.

All three §8a-A conditions land here: the `DO UPDATE` upsert, an execution
input of `{incident_id}` and nothing else, and **no Slack post from the
router** — the *"investigating…"* ack belongs to the poller from Slice 4.

**Q&A ships in this slice too**, since the router answers in-process: `rca.md`
inlined in the prompt, `read_record.py` for evidence with the incident id taken
from the subprocess environment and any `--incident` flag ignored, and a
`PreToolUse` allowlist permitting that one executable. **No `Bash`, no `Read`,
no tmpdir** (§8a-C).

*Verify:* kill the router between the upsert and `StartExecution`, and again
after it. Exactly one investigation in both cases, and none lost.

**Slack app changes:** delete `SLACK_APP_TOKEN` (Socket Mode is gone), add
`SLACK_SIGNING_SECRET`, switch the app to an HTTP Request URL.

### Slice 6 — liveness
The `ping` path in the router, **placed after the thread lookup so a broken
lookup fails the canary**. Synthetics canary, CloudWatch alarm, SNS.

### Slice 7 — deleted by §8a-F
There is no tools port. The tools stay Python, in place, and `procedure.md`
keeps its `python3` invocations. **The build ends at Slice 6.**

Slice 1 already does the only thing Slice 7 was carrying that mattered — moving
the tools' sink to Postgres. The rest of it was the language change and the
three-line `procedure.md` edit, both of which §8a-F removes.

---

## 7. Known gotchas

- **`claude_agent_sdk` spawns the `claude` Node CLI as a subprocess.** The
  process tree is `python → claude CLI (node) → bash → python3 tools`. **The
  image needs Node and `@anthropic-ai/claude-code` regardless of what language
  we write** — plus the AWS CLI. §8a-F does not change this and does not claim
  to; the runtime carries both either way. It buys one language in *our* code,
  not one runtime in the image.
- **The subprocess always writes transcripts to local disk**, so the transcript
  is a file the agent could `Read`. Point `CLAUDE_CONFIG_DIR` at a temp dir via
  `options.env` and make sure the `Read` confinement covers it. This holds with
  no session store at all — it is a P5 item, not a P7 one. *(When §8a-B's
  trigger fires: the store is a mirror, not a replacement, so it does not move
  the local write; `sessionStore` with `persistSession: false` throws.)*
- **The two topology probes were never agent tools** — see §8a-E. The
  `procedure.md` line and both scripts are deleted in Slice 1.
- **`can_use_tool` is shadowed by whole-tool `allowed_tools` entries** and would
  be dead code. Use a `PreToolUse` hook. (P5 §1)
- **Slack blocks native slash commands inside threads.** If a manual re-run
  command is ever built, it cannot be a slash command where you'd want it.
- **`condition_guess` is `None` in 8 of 8 real alert files.** The regexes have
  never fired, so dedup could only ever be decorative — which is why §8a-D does
  not build it. `parse_alert` still ships for the slug and the raw envelope.
- **A hard task death writes no `run_failed` row.** Only Step Functions knows.
  (P8 §5)
- **Deploys interrupt investigations.** A task killed mid-run is exactly the
  infrastructure-tier failure P7's tier 1 covers — now by restart (§8a-B), which
  means the run pays ~$2 again and writes a second `attempt` into the record.

---

## 8. Open questions

### 8a. Simplification levers — raised and ruled 2026-07-20/21

Layers whose cost had come to exceed what they carry. Each was a ruling already
made that a *later* ruling moved the ground under — none is a re-litigation on
its own merits. All are now ruled; each keeps its reasoning below so the
rejected shape stays on the record.

**F is the largest and it is a reversal, not a deferral.** It is also, in part,
a consequence of A–E: three of P11's five supporting arguments stopped holding
once those landed.

| | Layer | Outcome |
|---|---|---|
| **A** | The `investigations` queue (P3) | **Ruled — dropped. See below.** |
| **B** | S3 session mirror + resume (P7) | **Ruled — restart now, resume deferred. See below.** |
| **C** | Q&A folder export (P2 §3) | **Ruled — dropped, and Q&A loses `Bash`. See below.** |
| **D** | Dedup (P4 §3–4; `dedup_key`, `condition_guess`, the 30-min predicate) | **Ruled — dropped. Capture a Slack-rendered alert instead. See below.** |
| **E** | The two topology probes at `procedure.md:52` | **Ruled — deleted. They were never agent tools. See below.** |
| **F** | TypeScript, and the second tree (P11) | **Ruled — reversed. Python, in `prod/agents`, in place. See below.** |

**Not on this list, checked:** Step Functions. P1 noted `SQS → RunTask` would
have been smaller had P7 ruled otherwise. That is no longer true — it is now
load-bearing in three rulings (P4 §6 execution-name idempotency, P7 resume,
P8 §5 terminal authority).

#### A — ruled 2026-07-20: one queue. The router calls `StartExecution` directly.

**This amends P3, which drew two queues.** P3 inherited `SQS → Step Functions`
from P1 and was written before P4 §6 ruled the idempotency layers. Once
`event_id` is unique on the incident row and the execution name is the incident
id, every router crash point converges under `inbound` redelivery alone:

| Crash at | State left behind | Redelivery does | Converges |
|---|---|---|---|
| before the Slack parent fetch | nothing | re-fetches, proceeds | ✓ |
| after fetch, before the upsert | nothing | re-fetches, proceeds | ✓ |
| after upsert, before `StartExecution` | incident row, no run | upsert returns the same id, starts the run | ✓ (condition 1) |
| after `StartExecution` | row + running execution | duplicate name is a no-op | ✓ (condition 2) |
| two routers racing | one wins the insert | loser gets the same id, `StartExecution` no-ops | ✓ (conditions 1, 2) |

A queue between the router and Step Functions insures against nothing on that
list. It also is not buying buffering: P6 §4 rejected queueing excess work
outright — *"investigations are perishable"* — and a queue that must never
absorb a backlog is not earning its keep as a queue.

**The redundancy is only real if all three of these are built.** They were
unwritten, which is the substance of this ruling.

**1. The upsert always returns the id.** `ON CONFLICT DO NOTHING RETURNING id`
returns *nothing* when someone else already holds the `event_id`, and Postgres
deliberately does not wait on the concurrent uncommitted insert — so a
follow-up `SELECT` can come back empty too. A router that reads an empty return
as *"someone else has it, drop the message"* never starts the run after a
crash-before-`StartExecution`. **An alert silently goes uninvestigated**, which
is exactly the class invariant 6 exists to prevent. Use the form that takes the
lock and waits:

```sql
INSERT INTO incidents (event_id, …) VALUES (…)
ON CONFLICT (event_id) DO UPDATE SET event_id = EXCLUDED.event_id
RETURNING id;
```

Always one row back, so the router has one branch instead of two and no
empty-return case to reason about. `rca_service` already holds `UPDATE` on
operational state (P9 §5), so this costs no grant.

**2. The execution input is `{incident_id}` and nothing else.** Name-based
idempotency holds for 90 days on the same name **with identical input**; the
same name with different input raises `ExecutionAlreadyExists` rather than
succeeding quietly. If the router built a payload out of the Slack fetch and
anything varied between attempts, the idempotent retry would become an error
path. Passing only the id removes the question, and the task reads the rest
from Postgres — which is what "rows are the contract" (P2 §3) already says.

**3. Nothing non-idempotent sits between the two calls.** `daemon.py:217` posts
*"Investigating… I'll post here as I go"* from the handler. In the router, a
redelivery posts it twice. **Move it to the poller**, off the `run_started`
row. The ack then becomes idempotent by the same cursor that makes every other
narration line idempotent, and the router is left doing exactly the two calls
above — which is what makes this argument true rather than nearly true.

**The one path that does not converge, and its fix.** A *persistent*
`StartExecution` failure — a bad IAM policy, a state machine ARN that moved —
exhausts `maxReceiveCount` and lands the message in the DLQ, leaving an
incident row with no run. Same silent shape as condition 1, different road.

The poller closes it for free: it already loops over active incidents calling
`DescribeExecution`, and `ExecutionDoesNotExist` is a **distinguishable
answer**, not a timeout. An incident row whose execution does not exist gets a
posted failure. No new component.

**Accepted cost.** Queue depth and a dedicated DLQ are observable; without
`investigations`, a failing `StartExecution` surfaces only as `inbound`
redelivery and eventually the shared DLQ, mixed in with question-path failures.
P10 cares about that. Mitigation: put the routed type in a message attribute
and alarm per-type on the one DLQ. A genuine, if small, downgrade.

A `StartExecution` failure also re-runs the whole router including the Slack
parent fetch. Against P6's ceiling of 5 investigations per 10 minutes, that is
negligible.

#### B — ruled 2026-07-20: auto-**restart** now, resume deferred to a trigger.

**This amends P7, which ruled "resume, don't restart."** P7's two tiers survive
unchanged — infrastructure-tier failures retry automatically capped at 2
attempts, everything else stops and posts. What changes is the mechanism inside
tier 1.

P7 argued resume over restart on **cost**: a restart pays the full ~$2 again.
Against a system that already spends ~$36/month sitting idle, that is noise by
this document's own accounting. Resume's entire marginal value over restart is
~$2 and ~7 minutes.

What restart does not need: an S3 bucket, a lifecycle rule, IAM for it, the
`SessionStore` adapter, `load()`, a resume state in the machine, and the
`CLAUDE_CONFIG_DIR` interaction with P5's `Read` confinement that §7 still
flags as unresolved. It is a Step Functions `Retry` block on the existing
`RunTask.sync` state and nothing else.

**Split three ways, because P7 bundled them:**

| Piece | Ships | Why |
|---|---|---|
| Distinguishable exit codes (~20 lines) | **Slice 3** | precondition for *any* retry policy, and it is what lets the poller's terminal message say more than `exit 1` |
| `max_budget_usd` (P6 §5b) | **Slice 3** | per-run dollar stop, free from the SDK |
| Auto-restart, 2 attempts, infra tier only | **Slice 4** | a `Retry` block, built with the state machine |
| `SessionStore` / S3 / `load()` / resume state | **deferred** | no observed failure yet |

**The cost this ruling accepts, and it is about the record, not the money.** A
restart re-runs every tool call under the same `incident_id`, so `queries` ends
up holding two complete sets of rows, and the agent's role cannot clean them.
The self-check pass reads `queries` back (`procedure.md:104-105`) and would see
both. Resume has a smaller version of the same problem — P2 §5 concedes ~15s of
re-done turns produce duplicate qids and calls it acceptable — but "acceptable"
at one duplicate query is a different claim at forty-five.

**Mitigation, and it is a Slice 1 schema decision:** `queries` and `events`
carry an `attempt` column, and the self-check reads the current attempt. See
§5. Smaller than the S3 apparatus, and the attempt number is worth having in
the record regardless.

**Named triggers to pick resume back up:**
- the first observed infrastructure-tier death where the restart's re-run cost
  or latency actually hurt, or
- the first run long enough that a full restart is materially worse than a
  resume — call it 15 minutes. No run has come close; the one scored run is
  432s.

P11 §4's finding still stands but **no longer makes the deferred work cheap**
— §8a-F reversed P11, and the reference `S3SessionStore` plus conformance suite
ships in the TypeScript SDK only. In Python the adapter is a build, not a copy.
Read the TypeScript example as a spec if the trigger fires. This is the largest
single cost §8a-F accepts, and it is unpaid until then.

#### C — ruled 2026-07-20: no export. A read CLI, `rca.md` in the prompt, and Q&A loses `Bash`.

**This amends P2 §3 and P5 §5 together, because they were one decision.**

P2 §3 exported an incident to a tmpdir per question, justified as *"`qa/agent.py`
doesn't change at all."* That justification is gone: P3 moves Q&A in-process on
the router and this ruling takes `Bash` off it, so `qa/agent.py` is being
rewritten either way and the export preserves nothing. P2 §3 already named a
read CLI as the successor and triggered it on D7's product chatbot; this moves
the trigger to now.

*(As written on 2026-07-20 this paragraph rested on P11 — "the glue is
rewritten in TypeScript, so nothing is being preserved." §8a-F reversed P11.
The argument stands without it, on P3 and on this ruling's own `Bash` removal,
and is restated above on that footing.)*

**What actually forces it is the security position, not the export.** Q&A runs
in-process on the Service (P3), and P9 §4 puts `SLACK_SIGNING_SECRET`,
`SLACK_BOT_TOKEN`, `ANTHROPIC_API_KEY`, and `rca_service` — which holds
`UPDATE` — in that process. P5 §5 gave Q&A raw `Bash` there. Its input is
incident data, which is the log content P5's threat model names as the
injection vector.

**So Q&A has a materially worse position than the investigation task**, which is
the one P5 spent an entire ruling hardening. P5 §5's justification — *"its write
target is a copy we delete"* — covers writes to the record and nothing else. It
does not cover `env` in the process that can speak as the bot.

| | P2 §3 / P5 §5 | Ruled here |
|---|---|---|
| `rca.md` | file in a tmpdir | inlined in the prompt — a few KB, and what most questions are about |
| evidence | `queries.jsonl` in a tmpdir | `python3 read_record.py --qid q03` / `--list` |
| incident scope | the tmpdir's contents | **the environment**, set by the Service on the subprocess it spawns |
| tool surface | `Bash`, `Read`, `Write` | `PreToolUse` allowlist, one executable. No `Bash`. |
| tmpdir | required | none |

**The CLI reads its incident id from the environment and ignores any flag that
would set it.** The agent's shell inherits that environment and could otherwise
pass `--incident <other>`. Same principle as P9 §5's scoped read for the
investigator: scope comes from the task, never from agent input.

This deletes a filesystem format Postgres already replaced, and stops every
future schema change from having to keep it working — which is the tax P2 §3
warned about when it refused to make the folder a guarantee.

**Charts are deferred rather than allowed to decide this.** A PNG needs
somewhere to write before upload, so matplotlib brings back a tmpdir and a
second allowlist entry. §8b already tracks it as an open product question. When
it lands it adds those two things — a smaller change than keeping the whole
folder export alive to hold its place.

**Accepted cost.** Inlining `rca.md` pays those tokens on every question whether
or not the question needs them, and a question ranging across many queries costs
a round trip per drill-down instead of a `Grep` over a file. Against a
30-second Q&A call, neither is the binding constraint.

#### D — ruled 2026-07-21: dedup is not built. Capture a Slack-rendered alert instead.

**This defers P4 §3–4 rather than amending them.** Both rulings stand for the
day dedup exists; the finding under them is that today it cannot fire.
`condition_guess` is `None` in 8 of 8 real alerts, and P4 §4 rules that no
condition means a unique key. Dedup is therefore a guaranteed no-op, and
shipping a no-op guard is worse than shipping none — it reads as protection in
the code and in this document.

**`raw` makes the columns free to defer.** `dedup_key` and `condition_guess` are
pure functions of the stored envelope, so they can be backfilled the day
extraction works. Neither ships in Slice 1. The 30-minute predicate does not
ship in Slice 5.

This also retires P2 §4's justification for keeping the rows forever — *"data we
don't have today on how often dedup actually fires"* — which collapses when the
answer is known in advance to be zero. The rows still stay forever, on the
`event_id` guard's account (P4 §6, no TTL).

**Fixing extraction now is blocked on data, not on effort.** P4's finding that
the identifier sits in `title` / `Subject` was made against webhook and SNS
payloads. `parse_alert` will only ever see the **Slack-rendered** form, and
there are zero samples of one (§8b). Writing a field read against a payload
shape nobody has looked at is how you get a second parser that has never fired.

**So the work item is not dedup — it is the sample.** The first real alert
through the HTTP ingress is it. Log the envelope in Slice 5; it costs nothing
beyond the write. If the integration posts Block Kit with the original JSON
attached, the eventual fix is a field read; if it posts plain text, that is worth
knowing before anyone writes a regex.

**Duplicate protection is unaffected, and the names must stay distinct** (P3 §5
already warns about the collision). `event_id` unique on the incident row, plus
execution-name idempotency, are what stop a repeated *delivery*. Both ship.
Dedup only ever addressed the same alarm arriving as a **fresh Slack message**
later.

**Accepted cost.** A human who re-tags the same alert twenty minutes later pays
$2 twice. Loud, visible, cheap, self-correcting — the direction invariant 6
prefers — and the system is tag-gated, so a human chose to do it.

**Trigger:** design-v2 v2.1 auto-trigger, which removes the human and is the
point dedup starts carrying real weight (P4 §3). By then the sample exists.

#### E — ruled 2026-07-21: `procedure.md:52` is deleted. The topology probes are not tools.

They read as a capability the agent has. They are not. Both docstrings say
*"One-off probe"* — they were hand-run discovery scripts (`uv run python …`)
answering questions whose answers are in `NR_NOTES.md`, and they got listed in
`procedure.md` alongside the real tools.

| | `nr_relationships.py` | `nr_trace_probe.py` |
|---|---|---|
| Target | `GUID = "MzMwODc2M3xBUE18…"` hardcoded | `APP = 1450765319` hardcoded |
| Arguments | none | none |
| Credentials | `load_env(DOTENV)` — reads `.env` from disk | same |
| Writes evidence | no | no |

So the agent is pointed at two scripts that only work against one hardcoded
entity, cannot be aimed anywhere else, write nothing to the record, and stop
working at Slice 2 when `.env` leaves the image.

**What is actually lost, and what isn't.** `nr_trace_probe` is three plain
`SELECT … FROM Span` queries — the agent can write those itself through
`nrql_log`, parameterized and logged as evidence. Strictly better; no loss.

`nr_relationships` is **NerdGraph, not NRQL** (`relatedEntities` on an entity
GUID), so `nrql_log` cannot express it — P5 §3's safety argument depends on the
GraphQL document being fixed with only the NRQL string interpolated. Entity-graph
topology therefore does genuinely disappear, and getting it back means a fourth
tool carrying its own fixed-document argument.

**Ruled not to build it: no investigation to date has needed it.** Span-derived
topology through `nrql_log` has been enough. **Trigger:** an investigation that
visibly stalls for want of who-calls-whom.

**Consequences.** Invariant 1 returns to three lines. Invariant 2 becomes true
rather than asserted. Neither probe is copied into `tools/` in Slice 1.

#### F — ruled 2026-07-21: Python, in `prod/agents`, in place. P11 is reversed.

**This reverses P11 outright.** It is the only ruling in this document that
does. A–E each amended a ruling whose ground had moved; F says P11's answer was
wrong on a criterion it never weighed, and that three of its five supports have
since stopped holding.

**1. What A–E did to P11's supports.** P11 was decided on 2026-07-20. A–E were
ruled the same day and the next, and they took two of its arguments with them:

| P11 support | Status after A–E |
|---|---|
| §1 SDK parity is total | Neutral. Cuts both ways by construction — both packages are thin clients over the same Node CLI |
| §2 ~600 of 646 glue lines are being rewritten anyway | **Holds.** This is the ruling's real argument, and F has to answer it |
| §3 matplotlib is not a glue dependency | Neutral, and **§8a-C deferred charts entirely** — there is no chart path to be Python-shaped |
| §4 the reference `S3SessionStore` "pays immediately" | **Dead. §8a-B deferred the session store**, so it pays nothing |
| §5 tools move too, sequenced second | **Contested — see 3 below** |
| §6 P5 is indifferent to the language | Neutral, and still true |

That leaves §2 standing alone.

**2. The criterion P11 never weighed: one engineer, one language.** P11 asked
what the code costs to write. It did not ask what the *system* costs to hold in
one head. There is one developer, and the working system, the tools, the
investigator, and every artifact that has run a real incident are Python today.
TypeScript made the answer "Python tools, TypeScript glue, two trees, a port
sequenced across seven slices" — three kinds of context switch bought with a
migration that had not started.

**This is the standing simplicity mantra applied to the language choice**, which
is the one place `CLAUDE.md` had not applied it. Cheapest layer that works. A
second language is a layer.

**Scope, because it decides the answer.** "One language" is scoped to *this
system*, not to the ingren platform — most of what surrounds it
(`ingren-api`, `ingren-app`, the turborepo) is TypeScript, and against that
boundary the ruling would flip. The boundary that matters is the one crossed
day to day, and that is `prod/agents`.

**3. Answering §2, which is the one support left.** §2 is true and it is not
enough. It establishes that the port is *cheap*, not that it is *worth
anything* — "we are choosing the language of new code, not paying for a
migration" argues the cost is near zero, and near-zero cost still loses to
zero when the benefit column emptied out with §3 and §4.

It is also not as true as it reads. §2 counts the 646 glue lines and finds ~600
being rewritten, then §5 moves the **595 tool lines** as well, on a sequencing
argument rather than a need. Those are not being rewritten for any other reason.
So the honest total is not "46 pointless lines ported" but 46 plus 595 — and
`procedure.md` names the tools verbatim, which is why P11 §5 had to carve out
the three-line edit to invariant 1 that P2 had explicitly forbidden.

P11 §5 rejected a permanent polyglot seam as *"a rule invented to justify a
split rather than one the problem demands."* That is right, and F agrees with
it — F does not build the seam. It removes it from the other side.

**4. Compile step.** Precompiled JS was P11 §5's own mitigation: 45 tool calls
per run, `tsx` per invocation costing 200–500ms each, 20+ seconds a run. The
fix works, and it is a build step standing between an edit and a run of the
one artifact that gets edited most while tuning an investigation. Python has no
such step. Small, real, and paid every iteration.

**5. What this ruling costs, stated plainly.**

- **The reference implementation is gone.** One tree means the known-good system
  and the thing being changed are the same files. Slice 0's commit replaces it,
  and this tree has no commits today — so the cost is real until that commit
  exists. §2 and Slice 0 both now say so.
- **The `S3SessionStore` adapter is hand-written** if §8a-B's trigger ever
  fires. Deferred, so unpaid today.
- **Two runtimes stay in the image.** Node and `@anthropic-ai/claude-code` are
  required by the SDK no matter what we write. F buys one language in our code,
  not one runtime in the box, and §7 now says that explicitly.
- **If the wider platform ever absorbs this service**, the scope argument in 2
  inverts and this ruling should be re-read. That is the trigger.

**6. Consequences, all applied above.**

| | Was | Now |
|---|---|---|
| Tree | `prod/ingren-agents`, TypeScript | **`prod/agents`, Python.** The other tree is a tombstone |
| Invariant 1 | `procedure.md` changes by exactly 3 lines | **changes by zero characters** — P2's original wording restored |
| Slice 0 | `package.json`, `tsconfig` | **commit the working system first**, then lint/format |
| Slice 1 | copy `rca/tools/` into the other tree, repoint `__TOOLS_DIR__` | **edit the tools in place** |
| Slice 3 | port `run.py` + `hooks.py` to TypeScript | **rework them in place**; only the P5/P6/P7 delta lands |
| Slice 7 | port the tools, edit `procedure.md` | **deleted. The build ends at Slice 6** |

Nothing in §3, §4 (bar invariant 1), or §5 changes. The architecture was never
the language — which is the other half of why this reversal is cheap today and
would not have been in a month.

### 8b. Tracked, not blocking

| | Question | Trigger |
|---|---|---|
| **Charts / matplotlib** | Deferred by §8a-C, not just tracked — Q&A ships with no chart path at all. Bringing it back means a tmpdir, a second allowlist entry, and either matplotlib or agent-authored SVG plus a rasterizer. **A Q&A product decision, not a language one.** | first time an answer genuinely needs a picture |
| **Alert format** | We have **zero samples** of an alert as a *Slack message* — all nine real payloads arrived by webhook or SNS directly, and that is the only form `parse_alert` will ever see. §8a-D turns this into the actual work item: log the first real envelope in Slice 5. | Slice 5 captures it; hardening waits for v2.1 |
| **Alerting path** | SNS → email is an alert nobody reads at 3am. P10 establishes that a signal exists, not that anyone answers it. | go-live |
| **Model** | Investigator is `claude-sonnet-5` (implementation phase). Switch to `claude-opus-4-8` at go-live or per eval verdict. | go-live |
| **Q&A voice** | Answers should speak about the incident, not about the record's plumbing. | tuning pass |
| **Evals (P13)** | `feedback.md` is written per incident and nothing reads it. No regression suite, so a `procedure.md` change can't be shown to help or hurt. D5 defines the promotion bar; it is unbuilt. | deferred, tracked |
| **Tests and CI (P12)** | There are none, anywhere, and nothing is committed. Each slice above should name what proves it works. | ongoing |

---

## 9. Reading order for a fresh session

1. This document. **§8a before `decision.md`** — it amends six of that
   document's rulings and **reverses one of them (P11, §8a-F)**. Reading them in
   their original form will teach you a shape that is no longer being built.
2. `docs/decision.md` — P1–P11, the *why*. Long, but every ruling names what it
   rejected and why, which is what stops a slice from being re-litigated
   mid-build. The amended rulings carry a **⚠** marker inline, so you cannot
   read a stale one without seeing what replaced it. **P11 carries a stronger
   marker: it is reversed in full, not amended.**
3. `rca/investigator/procedure.md` — what the agent actually does. 127 lines.
   The most valuable artifact in the system.
4. `rca/tools/newrelic/NR_NOTES.md` and `rca/tools/cloudwatch/CW_NOTES.md` —
   the boundary facts that do the routing between telemetry sources.
5. The code in `rca/`, one file at a time, as each slice needs it. Under §8a-F
   this is not a reference to port from — it is the system, and the slices
   change it in place.
