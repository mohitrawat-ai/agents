# Live-test ledger — deferred paid verification

Ruled 2026-07-23 (in-session): live acceptance checks that cost a paid run
or real waiting are **batched**, not run per-issue. Issues land with unit
tests and this ledger entry; the boxes in `issues.md` stay unticked until
the batch runs. Rationale: later slices share test attributes (a seeded
incident, a real thread, a paid investigation), so one ~$2 run can tick
boxes across several issues instead of one.

Rules:

- Every deferred check lands here **when its issue's code lands**, with
  exact commands and the observation that ticks it.
- Free-and-fast checks are NOT deferred — they run with their issue.
- When a batch runs, tick here AND in `issues.md`, with dates.
- Mohit runs every mutating command; Claude watches logs, executions, and
  `SELECT`s (the #9 verify pattern).

---

## Batch A — poller (#10), runnable now; #11 will extend it

Shared fixture: one seeded incident (`rca/db/seed_incident.py`, per #5)
whose `channel`/`thread_ts` point at a real thread the bot can post to.
Runs 1–3 below share **one or two** paid investigations. Run 4 is free
but takes ~35 minutes of wall-clock.

### A1 — happy path: narration + terminal message

1. Seed an incident; note its id.
2. `StartExecution` (name = incident id, input `{"incident_id": "<id>"}`).
3. Watch the thread.

Expect, in order: the *"Investigating…"* ack (off `run_started`), the
milestone lines (hypotheses, timeline, self-check, verdict), no
`tool_call`/`instrument_note` lines, then
`Investigation finished for <slug> in <n>s, <n> turns, $<n> — document is ready.`
In the DB: `narrated_through` > 0, `terminal_posted_at` set.
Ticks #10: cursor narration box, ack box, terminal-from-DescribeExecution
box (success side).

### A2 — poller restart mid-run: nothing double-posts

1. During A1's investigation, force a poller redeploy:
   `aws ecs update-service --profile ingren --region ap-south-1 --cluster rca --service rca-poller --force-new-deployment`
2. Watch the thread through the restart.

Expect: narration pauses ≤ ~1 min, resumes, **no repeated ack, no
repeated milestone** (at most one re-posted line if the restart landed
between a post and its cursor write — acceptable, invariant 6).
Ticks #10: "ack exactly once across a restart".

### A3 — investigator killed mid-run: restart narrated, terminal still lands

1. Start a second paid run (or reuse A1's if not yet finished).
2. Mid-run: `aws ecs stop-task` on the investigator task.
3. Watch the thread and the execution.

Expect: `Restarting after an infrastructure failure (attempt 2).` in the
thread; attempt-2 milestones follow; terminal message arrives whatever
the outcome. If the retry is killed too: execution FAILED and the
no-failure-row terminal message (hard death / startup failure wording).
Ticks #10: "killed task mid-run still produces a terminal message" and
the exit-code-distinguishing side of the terminal box.

### A4 — never-started: posted failure after grace (free, slow)

1. Seed an incident. Start **nothing**.
2. Wait out `NEVER_STARTED_GRACE_S` (30 min).

Expect: `Investigation never started for <slug> — …` posted to the
thread, `terminal_posted_at` set, incident leaves the active set (log
goes quiet for it).
Ticks #10: `ExecutionDoesNotExist` box.

### A5 — errors logged, not swallowed (piggybacks on any of the above)

During any run, confirm `/ecs/rca` `poller/*` stream: healthy ticks are
silent; any failure prints a full traceback with the incident id.
Ticks #10: "errors logged, not swallowed".

---

## Batch B — reserved for #11 (ingress/router)

Lands with #11's code. Known members, from its acceptance list: the
kill-between-upsert-and-StartExecution pair, duplicate `event_id`
deliveries, rate-limit refusal, raw-envelope capture. The router kill
tests reuse Batch A's seeded-thread fixture and can share Batch A's paid
runs if staged in the same session.
