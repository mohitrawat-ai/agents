# Live-test ledger — deferred paid verification

Ruled 2026-07-22 (in-session): live acceptance checks that cost a paid run
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

> **Status 2026-07-22 (first batch run):** one real tag ran end to end —
> incident `35c6e40f`, slug `2026-07-22T10-28Z`, SUCCEEDED, 658s / 86
> turns / $2.37 / 27 queries, same verdict class as the laptop-era run.
> **Passed: A1, A2, A5, B1, B2.** Open: A3, A4, B3, B4, B5 — next
> session; A3/B3 need one more paid run, the rest are free.

## Batch A — poller (#10), runnable now; #11 will extend it

Shared fixture: one seeded incident (`rca/db/seed_incident.py`, per #5)
whose `channel`/`thread_ts` point at a real thread the bot can post to.
Runs 1–3 below share **one or two** paid investigations. Run 4 is free
but takes ~35 minutes of wall-clock.

### A1 — happy path: narration + terminal message — **PASSED 2026-07-22**

Evidence: ack + milestones + verdict in thread (Mohit-read), no tool_call
lines; terminal "finished … 658s, 86 turns, $2.37 — document is ready.";
`narrated_through`=386, `terminal_posted_at` set.

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

### A2 — poller restart mid-run: nothing double-posts — **PASSED 2026-07-22**

Evidence: forced redeploy mid-run; stop-then-start (never 2 tasks); new
task resumed from cursor 308; no duplicate ack or milestone in thread.

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

### A5 — errors logged, not swallowed — **PASSED 2026-07-22** (healthy side)

Evidence: poller streams silent between posts, no tracebacks. The
loudly-broken side rides A3/B4.

During any run, confirm `/ecs/rca` `poller/*` stream: healthy ticks are
silent; any failure prints a full traceback with the incident id.
Ticks #10: "errors logged, not swallowed".

---

## Batch B — ingress and router (#11)

Prereqs: image pushed with `service/`, provision run, 443 listener live,
Slack app switched to the Request URL. B1 is the gate: nothing else runs
until a real tag flows end to end. B1/B2 can share Batch A's paid run —
a real tag both exercises the router and produces A1's investigation.

### B1 — end to end: tag → investigation — **PASSED 2026-07-22**

Evidence: two POST /slack/events 200s; "[router] investigation started";
envelope in `raw.envelope` (the §8a-D sample); execution SUCCEEDED.
Gotcha found live: Socket Mode toggle had swallowed the first event —
the toggle must be OFF, deleting the app token is not enough (RUNBOOK).

1. Tag the bot on an alert message in the allowlisted channel.
2. Watch `service-ingress` and `service-router` log streams.

Expect: ingress 200 in Slack's dashboard (no retries); router logs
"investigation started"; incident row exists with `raw.envelope` set —
**the §8a-D sample, save it**; execution running; then Batch A1's
narration expectations in the thread.
Ticks #11: signature verified, durable-write-then-200, upsert returns id,
input `{incident_id}` only, no router post, raw envelope logged.

### B2 — duplicate delivery: one run only — **PASSED 2026-07-22**

Evidence: Slack delivered the mention twice (two 200s in ingress logs);
exactly one execution on the machine, one started line in router logs.

1. During B1, check Slack's event delivery dashboard for retries; if none
   occurred, redrive one message: copy the B1 envelope from the incident
   row and `aws sqs send-message` it onto `rca-inbound` again.

Expect: router routes it as a question now (thread is known) — or, if
replayed before the upsert commits, the `event_id` conflict returns the
same id and `StartExecution` no-ops. Either way: exactly one execution
on the machine.
Ticks #11: two deliveries of the same `event_id` start exactly one run.

### B3 — kill the router around StartExecution (paid, one run)

1. Seed nothing. Tag the bot on a fresh alert.
2. Within the router's processing window, `aws ecs stop-task` both
   Service tasks (kills router mid-message; SQS redelivers at +60s).
3. ECS restarts the tasks; the message redelivers.

Expect: exactly one incident row, exactly one execution, alert not lost.
This is the §8a-A crash-convergence table, live.
Ticks #11: the kill-router acceptance box, both halves.

### B4 — enqueue failure returns non-2xx (free)

1. Temporarily deny `sqs:SendMessage` on role `rca-service` (or scale the
   queue's policy); tag the bot.
2. Restore afterwards.

Expect: Slack dashboard shows non-2xx + retries; no lost alert once
restored; ingress logs the failure loudly.
Ticks #11: an enqueue failure returns non-2xx.

### B5 — allowlist and rate limit (free)

1. Invite the bot to a non-allowlisted channel; tag it. Expect: silence
   in-thread, loud "not allowlisted" drop in router logs, no incident.
2. Seed 5 incident rows dated now (owner SQL), then tag a real alert.
   Expect: in-thread refusal, no 6th execution. Delete the seed rows.
Ticks #11: channel allowlist; rate limit refuses at the 6th.
