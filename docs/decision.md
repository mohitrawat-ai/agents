# Production decisions — hosted RCA agent

> **Moved to `prod/ingren-agents` on 2026-07-21, then back to `prod/agents` the
> same day** when §8a-F reversed P11 and collapsed the two trees into this one.
> Content is unchanged apart from this banner and the **⚠** markers below.
>
> **Six rulings here are amended by `design.md` §8a, and one is reversed
> outright.** Each is marked inline at the ruling itself. Read `design.md`
> first — reading these in their original form will teach you a shape that is
> no longer being built.
>
> | | Ruling | Amended to |
> |---|---|---|
> | A | P3 (two queues) | one queue; the router calls `StartExecution` directly |
> | B | P7 (resume) | auto-**restart**; the S3 session mirror is deferred |
> | C | P2 §3, P5 §5 | no Q&A export; a read CLI, and Q&A loses `Bash` |
> | D | P4 §3–4 (dedup) | not built; capture a Slack-rendered alert instead |
> | E | P5 §1, D11 | `procedure.md:52` deleted; the topology probes were never tools |
> | **F** | **P11 (TypeScript)** | **reversed. Python, in `prod/agents`, built in place. One tree** |
>
> **§8a-F makes this document's target tree and language wrong wherever it
> states them** — including the header block immediately below, which is left
> as written rather than quietly corrected. Everything architectural still
> stands; no §4 invariant weakened, and F strengthens invariant 1.

Successor to the "Review before production" checklist in
`ingren-rca/docs/plans/rca-harness/design-v2.md`. That doc's D1–D14 decided
what the agent *is*. This one decides what it takes to run as a hosted
service the design partner's on-call depends on.

**Status (2026-07-20): P1–P11 all ruled. Implementation is unblocked.**

**Target tree: `prod/ingren-agents` (TypeScript).** `prod/agents` is now the
reference implementation, not the deployment target. Each item is
framed with the constraint that forces it, the axes that actually differ, and
what stays blocked until it's settled. Rulings get filled in as we grill each
point. Implementation starts after, not before.

**Shape agreed so far:**

```
ALB → Fargate Service  ── always-warm, 2 tasks
        │  ingress:  verify signature → SQS(inbound) → ack
        │  router:   poll inbound → question? → answer Q&A in-process
        │                        → alert?    → SQS(investigations)
        └→ SQS(investigations) → Step Functions → Fargate Task  ── per-run

Postgres  →  evidence + operational state   (kept forever; evidence backed up)
S3        →  session mirror                 (lifecycle-expired; never backed up)
```

The investigation stays a black box per design-v2 D3, and `procedure.md` does
not change. We build the deployment layer ourselves rather than adopting
Managed Agents (P1b).

Numbering is `P1…` deliberately, so it never collides with design-v2's
`D1…`. Where a P item overturns a D ruling, it says so.

---

## Ordering rationale

Ordered by **dependency, not by pain**. P1 and P2 are the keystone pair:
almost every other item on the list resolves differently depending on how
they land, so ruling them first stops us re-deciding the rest twice.

The current architecture has one assumption threaded through every file —
**a single machine with a shared local disk**. `INCIDENTS_ROOT`, the
poster's byte-offset tail of `events.jsonl`, `fcntl.flock` for qid minting,
the Q&A agent reading the folder next to it. Going hosted breaks that
assumption, and P1/P2 are where we decide what replaces it.

P11 (language) is last on purpose. It's a consequence of P1, not a premise.

---

## P1. Where a run executes

**Question:** what compute runs an investigation, end to end?

**Forced by:** the daemon is a foreground process on a laptop that must stay
awake (design-v2 review list, item 2). A hosted on-call dependency cannot
have that property.

**Hard constraint that rules things out:** an investigation is 5–10 minutes
today and `--max-minutes` defaults to 60 (`investigator/run.py:93`). AWS
Lambda's ceiling is 15 minutes, so **Lambda cannot host the run**. It can
host the Slack ingress (see P3), which is a separate question.

**Axes that differ:**
- *Per-run isolation* — one container per investigation, versus a
  long-lived worker process handling runs in sequence.
- *Cold start vs. always-on cost* — an idle service billed continuously,
  versus per-run compute billed only during incidents.
- *How much AWS plumbing we own* — task definitions, roles, and networking,
  versus a managed runner or a plain always-on box.
- *Whether the sandbox comes free* — a per-run container with a scoped IAM
  role gives P5 an actual enforcement boundary instead of a prompt.

**Also settles P4's concurrency question.** If each run is its own
container, `_active_run` (`daemon.py:172`, the one-at-a-time refusal) simply
deletes and concurrency becomes free. Don't budget separate work for it.

**Blocks:** P2, P5, P7, P10, P11.

**Ruling (2026-07-20): one Fargate task per investigation, invoked by Step
Functions (`RunTask.sync`), entered via SQS, with the record in a database
(P2).**

The investigation stays **one opaque task** — D3's black-box seam survives
intact, and Step Functions does not orchestrate the investigation's
internals. If D5's `phased` variant later earns promotion on the bench,
subagents are spawned in-process by the SDK, not modelled as state-machine
states.

Reasoning:

- **Lambda is out.** Its 15-minute ceiling is 4x under D8's ruled 60-minute
  budget, and the one measured run (433s / 52 turns / $2.03) extrapolates to
  ~21 min at the configured `max_turns` of 150. The cap would truncate
  exactly the hard, multi-hypothesis incidents the agent exists for, and
  never touch the easy ones.
- **Cost and cold start were checked and don't differentiate:** ~$0.002
  Fargate vs ~$0.007 Lambda per run, against $2.03 of LLM spend.
- With Step Functions as orchestrator, **Lambda-vs-Fargate is a resource ARN
  in one state** — the only cheaply reversible decision in P1. Fargate is
  chosen because it removes the sole hard constraint at near-zero
  architectural cost, and can be revisited once ~20 real runs give an actual
  duration distribution. There is currently one.
- **Step Functions earns its place on P7's account** (per-failure-class
  retriers and a resume state), not on subagents'. Had P7 ruled "manual
  re-run only," SQS → `RunTask` would have been the smaller correct answer.

Accepted cost: Fargate task startup is 30-60s, so the first Slack post lands
about a minute later than it would on Lambda.

**Runtime constraint discovered.** `claude_agent_sdk` is a thin client that
spawns the `claude` Node CLI as a subprocess (`subprocess_cli.py:150`). The
image needs Node + `@anthropic-ai/claude-code` + Python + the AWS CLI, and
the real process tree per run is:

```
python  →  claude CLI (node)  →  bash  →  aws / python3 tools
```

**Ruling — the Q&A and ingress workload (2026-07-20).** An **always-warm
Fargate Service** (2 tasks, 1 GB each to start), running both the Slack HTTP
ingress and Q&A. Investigations stay per-run Fargate **Tasks**.

```
ALB → Fargate Service (Slack ingress + Q&A)  ── always-warm, 2 tasks
              │
              └→ SQS → Step Functions → Fargate Task (investigation)  ── per-run
```

*(P3 refines the wiring inside this shape: two queues, and the Service both
produces to and consumes from the first one. The substrate ruling is unchanged
— and P3 rejected a router Lambda on exactly the reasoning below.)*

One substrate, one image, one deploy, two ECS constructs. Chosen over Lambda
for Q&A because it keeps the deployment model singular, which is the stated
priority. ~$36/month for both tasks — noise against $2 per investigation.

Consequence: with 2 tasks, the ingress can hold nothing in memory. That is
already P4's direction, but it means `_active_run`, `_recent`, and the
`threading.Lock` don't survive in any form, even temporarily.

Ingress and Q&A share one service to start. Split them when Q&A load starts
affecting Slack ack latency — a metric, not a guess. Splitting later is a
task-definition change, not a rearchitecture.

### P1b. Harness vs. deployment — build, not managed

There are four ways to build on Claude, splitting on two independent
questions: who supplies the **harness** (agent loop + context management) and
who supplies the **deployment**.

| | Approach | Harness | Deployment |
|---|---|---|---|
| 1 | Manual loop over the Messages API | you | you |
| 2 | Tool Runner | SDK (loop only) | you |
| 3 | Managed Agents | Anthropic | **Anthropic** |
| 4 | **Claude Agent SDK — current** | SDK (full Claude Code harness) | **you** |

Options 1, 2, and 4 are harness-only. **Only option 3 supplies deployment** —
which is precisely what P1, P2, P5, P7, and P10 are all about. Managed Agents
would have absorbed the per-run sandbox, session persistence, the event
stream, liveness webhooks, and secret handling.

**Ruling (2026-07-20): stay on the Claude Agent SDK and build the deployment
layer.** Two reasons, in Mohit's words: avoid deepening vendor dependence, and
this tree is explicitly a harness learning phase (project `CLAUDE.md`) in a
field with no open standard yet, where his own judgement is worth building.

The sharper form of the first reason: the coupling is not symmetric. **D3's
portability rule already keeps the Agent SDK behind one file** — `run.py` plus
`hooks.py` are the only SDK-aware code, and everything downstream reads the
record. Managed Agents would spread coupling across deployment, storage,
sessions, and credentials simultaneously, with no equivalent seam to hide it
behind. One swappable box versus four entangled ones.

**Worth stealing anyway — the egress-substitution credential model.** Managed
Agents' vault `environment_variable` credentials substitute the real secret
into an outbound request *at egress*; the sandbox only ever sees an opaque
placeholder. That is strictly stronger than a scoped task role, because a role
the container holds is a role the agent can use. Carry the goal into P5: the
agent's shell should never hold a usable New Relic or AWS credential.

**Also carried forward, from the SDK's own process model:** `query()` (what
both `run.py` and `qa/agent.py` use) is documented for "fire-and-forget" and
"stateless" work — correct for the investigator, wrong for Q&A.
`ClaudeSDKClient` holds one subprocess across many turns and is the documented
fit for conversational use. Not adopted now: it is stateful in process memory
and cannot cross async runtime contexts, so on a 2-task service it would
require sticky sessions and fight P4. Recorded as a **P8 optimization**.

**Still mechanical in P1:** `aws_log.py:89` injects `--profile hb-role`, which
does not exist in Fargate — that becomes a task role, and is the seam into P5.

---

## P2. Where the incident record lives

**Question:** what replaces the shared local filesystem as the record store?

**Forced by:** P1. The moment runs move off one machine, there is no shared
disk. Four things depend on there being one:

| Depends on shared disk | Where |
|---|---|
| Incident folders under one root | `daemon.py:33` |
| Poster tails `events.jsonl` by byte offset | `poster.py:48-58` |
| `fcntl.flock` minting qids across processes | `nrql_log.py:32`, `aws_log.py:40` |
| Q&A agent reads the folder as its cwd | `qa/agent.py:82` |

**Axes that differ:**
- *Does the folder contract survive verbatim?* design-v2 D3 makes
  "`alert.json` in → incident folder out" the portability guarantee, and D4
  builds the whole event vocabulary on jsonl files. Keeping a filesystem
  shape (object store, network filesystem) preserves that seam; moving to
  rows in a database rewrites it.
- *Append-and-tail semantics.* Live narration needs someone to notice new
  events within seconds. Some stores support that natively, some need
  polling, some need a separate notification path.
- *Durability and retention.* NR events expire at ~8 days, so the record is
  the only lasting copy of what an investigation saw. Whatever holds it is
  now production data with a backup story.
- *Query-ability.* Aggregating `feedback.md` verdicts for P13 evals is
  trivial over rows and awkward over files.

**Open sub-question:** the same store may or may not hold the operational
state from P4. One store or two is itself a decision.

**Blocks:** P4, P8, P13.

**Ruling (2026-07-20): Postgres for the record, S3 for the session mirror.**

```
Postgres  →  evidence + operational state   (kept forever; evidence backed up)
S3        →  session mirror                 (lifecycle-expired; never backed up)
```

**1. The tools write to Postgres directly.** `nrql_log.py`, `aws_log.py`, and
`emit.py` are already the only structured writers — `procedure.md` forbids any
other path to telemetry, which is exactly the choke point D11 built. Their sink
changes; **`procedure.md` does not change by a single character.** The agent
still runs `nrql_log.py --log-dir .`; that invocation means something different
underneath.

Two things this buys for free:

- `fcntl.flock` qid minting (`nrql_log.py:32`, `aws_log.py:40`) becomes
  `INSERT … RETURNING`. The duplicate-`q02` bug class stops being *possible*
  rather than being carefully avoided.
- P8's narration becomes a query instead of a byte-offset tail.

Rejected: syncing files written by the agent. Sync-at-end kills live narration
entirely; a continuously-syncing sidecar reintroduces the exact tailing problem
this decision exists to remove.

**2. The database role is INSERT-only** on the two tables — no `UPDATE`, no
`DELETE`, no read across incidents. This is the mitigation for putting a DB
credential inside the investigation container: the agent can reach the record,
so the record must be append-only *at the database level* rather than by
convention. That is stronger than the filesystem version ever was.

**Amended by P9 §5 (2026-07-20): "INSERT-only" was too strong.** The
self-check pass reads back `queries.jsonl` (`procedure.md:104-105`), so a
literal no-`SELECT` role breaks the run. The protection that matters is no
`UPDATE` and no `DELETE`, plus no read *across* incidents. See P9.

`rca.md` is the exception to the tool path — the agent authors it with `Write`,
and forcing it through a CLI would fight the harness. Uploaded on `doc_ready`.

**3. The folder export is a convenience, not a guarantee.** Rows are the
contract. D3's *purpose* was harness portability — "swapping harnesses later
means rewriting this file and `hooks.py`, nothing else" — and that purpose is
served by any stable record schema, not specifically by a filesystem. Making
the folder a guarantee would tax every future schema change for a consumer that
doesn't exist yet.

Q&A is sequenced: **now**, export one incident to a tmpdir per question (a
`SELECT` plus a few file writes, sub-100ms, and `qa/agent.py` doesn't change at
all); **later**, a read CLI symmetric with the write CLIs. **Named upgrade
trigger:** when D7's product chatbot lands, since a chatbot spanning many
incidents can't materialize a folder per question.

> **⚠ Amended by `design.md` §8a-C.** The export is not built. P11 ruled
> `qa/agent.py` gets rewritten in TypeScript, which deleted the justification
> above, and the trigger moved from D7 to now. `rca.md` goes in the prompt,
> evidence comes from a read CLI scoped by the environment, and Q&A loses
> `Bash` — see also P5 §5.

**4. Retention — back up what can't be re-derived, and nothing else.**

| | Lifetime | Backed up? |
|---|---|---|
| Evidence (queries, events, `rca.md`, `feedback.md`) | **Forever** | **Yes** |
| Operational state (dedup, idempotency) | **Forever** | No |
| Session mirror (P7) | S3 lifecycle expiry | No |

Evidence is different *in kind*: NR events expire at ~8 days, so the record is
the only durable copy of what an investigation saw. Past that window a lost row
is evidence that cannot be re-derived at any price.

Operational state is kept because **the 30-minute dedup window is a query
predicate, not a retention policy** — `WHERE created_at > now() - interval '30
minutes'`. The rows cost a few hundred a year and give us data we don't have
today on how often dedup actually fires, which is what would justify tuning the
window.

**5. The session mirror goes to S3 as objects, not a mounted filesystem.**
S3 has no append: every write to an existing object is a full read-modify-write,
and Mountpoint for Amazon S3 refuses object modification outright. With
`append()` firing at ~100ms cadence that would be quadratic bytes written, worst
on exactly the long runs most worth resuming.

Instead, implement the `SessionStore` protocol directly (it's duck-typed — no
subclassing needed). `append()` buffers **15 seconds** and writes one *new*
object per flush (`s3://…/<run-id>/00001.jsonl`); `load()` lists the prefix and
concatenates. S3 becomes append-only *by adding keys*, which is the shape it
actually supports.

At 15s on a 433s run: ~29 objects, negligible PUT cost, `load()` is 1 LIST plus
~29 concurrent GETs. Worst case on a hard kill is ~15s of lost transcript —
about two re-done turns, ~$0.08.

Buffer size is a **record-cleanliness dial, not a correctness one.** A re-done
turn re-runs its tool calls, so a lost window containing an `nrql_log.py` call
produces a second evidence row with a new qid, uncleanable under the INSERT-only
role. That is acceptable and arguably correct — the record honestly shows the
query ran twice, and a citation to either qid still resolves against the
self-check pass.

An S3 lifecycle rule handles expiry, so there is no delete job to write or
forget. Rejected: EFS — it supports append and would leave the local-file path
untouched, but it costs ~$0.30/GB-month, needs VPC mount targets, and puts back
the shared network filesystem this decision exists to remove.

**6. The nine existing incident folders are not imported now.** Deferred,
including the only real scored run (`2026-07-18`, 433s / 52 turns / $2.03).
Tracked under P13, which is the only consumer that will want them.

---

## P3. Slack ingress and dispatch

**Question:** how does a Slack mention reach a run, once Socket Mode is gone?

**Forced by:** Socket Mode holds a persistent outbound websocket from the
process — it exists precisely so you don't need a public endpoint, which is
the wrong shape for hosted. design-v2 unit-7 ruling 2 already anticipated
this and called it a one-line adapter swap.

**That ruling is half right.** Bolt's listener code genuinely is identical
under both transports. But HTTP adds a constraint Socket Mode doesn't have:
**Slack requires a response within 3 seconds** or it retries the delivery.
Spawning a 10-minute subprocess inside the handler stops being viable, so
ingress and execution must split, and something has to carry work between
them.

**Note:** unit-7 ruling 1 rejected SNS→SQS as the *trigger surface* — Slack
stays the trigger. A queue between our own ingress and our own runner is a
different thing and is not covered by that rejection.

**Axes that differ:**
- *What carries the work* — a queue, a direct API call to the runner, a
  state-machine service, or a row in the store from P2 that a worker polls.
- *Delivery guarantees.* Slack retries on a slow or failed ack. Whatever we
  choose must make a duplicate delivery harmless, which is P4's problem but
  originates here.
- *How much of `daemon.py` survives.* The handler core (parse → dedup →
  spawn → ack) was written transport-blind on purpose; worth checking that
  claim honestly rather than assuming it.

**Blocks:** P4, P6.

**Partly settled by P1:** the carrier is SQS into a Step Functions execution.
What remains open here is the ingress itself — what terminates the Slack HTTP
request and acks within 3 seconds, how Slack's retry-on-slow-ack is made
harmless (P4), and how much of `daemon.py`'s handler core survives.

**Ruling (2026-07-20): the Fargate Service terminates the request. The ingress
does nothing but verify, enqueue, and ack — routing happens behind the queue,
on the same Service.**

```
ALB → Service (ingress)   verify signature → SendMessage(inbound) → 200
                                                      │
                          ┌───────────────────────────┘
                          ▼
      Service (router)    poll inbound → look up thread_ts
                                ├── known thread    → answer Q&A in-process
                                └── new / unknown   → SendMessage(investigations)
                                                          │
                                                          ▼
                                        Step Functions → Fargate Task
```

Two queues. `inbound` carries raw Slack envelopes; `investigations` carries
parsed alerts.

> **⚠ Amended by `design.md` §8a-A.** One queue. `investigations` is not built —
> the router calls `StartExecution` directly. §6 below ruled the idempotency
> layers that make the second queue redundant, and this section was written
> before it. Three conditions have to hold and are written down in §8a-A; the
> load-bearing one is that the upsert must use `DO UPDATE`, not `DO NOTHING`,
> so it always returns the id.

**1. The governing rule: return 200 the instant the request is durably
recorded, and not before.** Both ways of being wrong follow from breaking it in
one direction or the other.

*Ack late* — do the work, then return. Any tail-latency hiccup crosses Slack's
3-second deadline, Slack retries, and we have bought a duplicate $2 run. The
current handler is squarely here: it makes up to two Slack API calls
(`daemon.py:81` `conversations_replies`, plus one of five `chat_postMessage`
paths at `:146, :168, :173, :217`) and a whole-filesystem glob (`:42`) before
returning. Under Socket Mode there was no deadline and none of it mattered.

*Ack early and forget* — return 200, then work in memory. If the process dies
in that gap the alert is gone, **and Slack will never re-deliver it, because we
already said we succeeded.** A 3am alert vanishes with no trace anywhere.

Corollary: the durable write must carry the **raw event envelope**, not a
parsed interpretation. And an enqueue failure must return non-2xx so Slack
retries — that is correct behaviour, not a bug.

**Also relevant: the 200 is invisible to the human.** Nobody in Slack sees it.
All human-visible feedback comes from a message we post into the thread, on its
own clock. So acking fast costs the user nothing, and there is no UX argument
for doing work first.

**2. Nothing fallible sits in front of the ack.** The ingress verifies the
signature and writes one message. It does not route, does not read the record,
and makes no Slack API call. Measured against the 3-second budget that is ~16ms,
but the margin is not the point — **the point is that the only I/O before the
signature is the durability write itself.** There is no dependency whose outage
can turn a 3am alert into a dropped one.

**3. Routing is by thread, not by message content, and it happens behind the
queue.** First tag in a thread is an alert; anything after it in the same thread
is a question.

Rejected: **routing on whether text follows the tag** (bare tag = alert, tag +
text = question). It is a pure function of the payload — zero I/O, no fallible
dependency in front of the durability write — and it is what the thread rule
approximates without state. It was rejected on one disagreement case:

> *"@rca is this the LB thing again?"* — tagged on the alert, first contact,
> with commentary.

The text rule calls that a question, finds no incident for the thread, and
replies with a shrug. **A real alert goes uninvestigated because the human was
chatty.** The reverse disagreement (a bare tag as a follow-up) is harmless:
dedup catches one, an empty-question prompt catches the other.

The first tag and every follow-up carry the *same* `thread_ts`, so nothing in
the payload distinguishes them and the rule needs a record lookup. Behind the
queue that lookup costs nothing structural: it gets a full SQS retry budget and
a DLQ instead of a 3-second budget.

Four independent implementations route this way — Claude Tag, Hermes, Pinet,
and OpenHands (which does it as a literal `WHERE channel_id = ? AND parent_id =
?`). None of them route on message content. See Prior art.

**4. Rejected alternatives.**

*Routing before the ack, with a fail-toward-the-queue fallback* — **ruled first,
then reversed the same day.** A pre-ack lookup is affordable only if a failure
has somewhere to go, so it drags in a fallback branch: on timeout, treat it as
an alert and enqueue. That branch is correct and nearly untestable. It fires
maybe twice a year, cannot be exercised naturally, and would be broken the one
time it mattered. Enqueueing everything does not handle that case better — **it
makes the case not exist.**

The reversal also makes the *nobody is watching vs. someone is watching*
durability asymmetry moot. It was the justification for giving questions a
non-durable fast path; with one uniform path, everything is durable and the
asymmetry costs nothing to ignore.

*A Lambda router* — P1 chose Fargate for Q&A **over a cheaper Lambda**
specifically to keep the deployment model singular: one substrate, one image,
one deploy. A router Lambda puts back the second substrate that ruling paid
~$36/month to avoid, and the Service is the better consumer anyway — always
warm, already holds the Slack client, already has DB access and the Q&A code
in-process.

*A third queue for questions* — once the Service is the router, it already has
the message and the code to answer it, so a question queue is the Service
handing work to itself. **One thing would earn it:** P6 flags that N mentions
today means N concurrent paid LLM calls, and a queue with bounded consumer
concurrency is a free rate limiter. Left to P6 to justify rather than built on
speculation.

*An LLM call for routing* — routing is `SELECT … WHERE channel = ? AND
thread_ts = ?`. Exact, ~20ms, free, deterministic. An LLM makes it approximate,
slower, billable, and non-reproducible, and reproducibility is what lets a
misroute be debugged at 3am. **There is a real LLM job next door**, though:
`condition_guess` is `None` in 8 of 8 real alerts (P4), and a model reading a
templated alert to extract condition and entity would fix that outright. Behind
a queue it has the time budget. That is *parsing*, and it belongs in the
investigation consumer where a retry is free.

**Accepted cost.** Questions now pay a queue round trip before anyone starts
thinking: ~250ms with the Service long-polling, versus ~25ms for a pre-ack
lookup. Against a 30-second Q&A call that is 0.8% overhead, which was never
worth the fallback branch it bought.

**5. Idempotency: `event_id` on the alert path, enforced by the consumer.
Nothing on the question path.**

| | Alert path | Question path |
|---|---|---|
| Duplicate source | Slack retry **and** `inbound` redelivery **and** `investigations` redelivery | Slack retry **and** `inbound` redelivery |
| Cost of a duplicate | a second $2 investigation | one extra LLM call, a repeated answer |
| Guard | conditional write on `event_id` | none |

Both paths now sit behind a queue, so both inherit at-least-once redelivery.
That does not change the ruling — it changes only the reason. The guard is
justified by what a duplicate *costs*, not by how many ways one can arrive.

Queue redelivery is the source that matters, because it fires on our bugs
rather than on Slack's rare tail. The guard sits in the consumer, not the
ingress — putting it in the ingress means a second lookup before the ack. **The
queue is allowed to hold duplicates as long as exactly one of them starts a
run.**

This is a *delivery-level* guard. It catches "this exact message, twice." It
says nothing about the same alarm firing again from a fresh Slack message
twenty minutes later — different key, P4's problem. Two mechanisms, one word;
keep the names distinct in code.

**6. What survives of `daemon.py`.** Unit-7 ruling 2 claimed the handler core
was transport-blind and the move was a one-line adapter swap. Checked honestly,
that is half right — Bolt's listener signature is identical, but the handler
body is not portable, because it was written with no deadline.

| Component | Fate |
|---|---|
| `parse_alert.py` | **survives verbatim**, moves to the consumer |
| `_handle_question` (`:107`) | survives, runs in-process on the Service |
| `_incident_for_thread` (`:39`) | becomes the router's lookup — indexed read on `thread_ts`, not a glob, and behind the queue rather than in front of the ack |
| `_alert_text_for` (`:74`) | moves to the investigation consumer; the parent fetch is a Slack API call and cannot sit before the ack |
| `_watch_run` (`:90`) | **dies** — Step Functions owns run completion (P1) |
| `_active_run`, `_recent`, `_lock` (`:64-67`) | **die** — already ruled by P1's 2-task Service |
| `on_mention` (`:127`) | splits into a ~10-line router plus consumer-side handlers |
| `load_env_into_os` (`:51`) | P9 |
| `tail_events` thread spawn (`:200`) | P8 |

**7. Capability removed.** Today an investigation can be started by pasting
alert text at top level with the tag (`daemon.py:85-87`). Under the thread rule
a top-level tag has no parent to read, so that path goes away. Recorded as a
deliberate loss, not an oversight.

**Handed to P4:** a thread can host a second, unrelated alert an hour later,
and the thread rule will call it a question forever. "First tag in a thread" is
therefore a lookup with a time bound, not a boolean, and P4 owns what that
bound is.

**Handed to P6:** the question path has no idempotency guard and no cost gate.
A Slack retry storm on that path is billable. P6 already flags Q&A as ungated;
this ruling does not improve it.

---

## P4. Run state, dedup, and idempotency

**Question:** where does operational state live, and what makes a repeated
trigger safe?

**Forced by:** state is three in-memory Python objects today —
`_active_run`, `_recent`, and a `threading.Lock` (`daemon.py:64-67`). A
restart forgets all of it, so a re-tag duplicates a paid run (design-v2
review list, item 1). Hosted, with more than one process, they stop working
entirely.

**Three separate needs, currently conflated in one dict each:**
1. *Dedup* — the same alert twice inside 30 minutes is one incident.
2. *Idempotency* — the same Slack delivery twice (a retry from P3) must not
   start a second run. This is a different problem from dedup and isn't
   handled at all today.
3. *Concurrency control* — how many runs at once. May dissolve into P1.

**Axes that differ:**
- *Correctness under races.* Two ingress workers receiving near-simultaneous
  mentions need something better than a per-process lock — a conditional
  write, a unique constraint, or a lease.
- *Dedup key quality.* `parse_alert.py:33` hashes a condition guess or a
  120-character text prefix. Worth deciding whether that's good enough to
  hang money on, since a false match silently suppresses a real
  investigation.
- *Slug collision.* Slugs are minute-precision (`parse_alert.py:36`). Today
  the one-at-a-time rule hides this; if P1 makes runs concurrent, two alerts
  in the same minute collide on one folder.

**Blocks:** P7.

**Ruling (2026-07-20): one tag one investigation, identity is a surrogate key,
dedup stays deliberately dumb, idempotency is a unique constraint with no
expiry.**

**1. One tag, one investigation. Correlation is the agent's job, not the
dispatcher's.** A cascading failure fires several different alarms; if a human
tags two of them, that is two investigations at $2 each. We do not merge them.
Building alarm-correlation is the thing not to build — the agent can read the
record and observe that an incident looks related to another one. **The human
is the correlator.**

**2. The slug is a display label, not an identity.** `parse_alert.py:36` mints
`%Y-%m-%dT%H-%MZ` — minute precision — and that string became the folder name
(`daemon.py:181`), then `--incident-dir`, then `--log-dir`, then the
`queries.jsonl` path every qid citation resolves against.

That was safe under exactly one condition: **one run at a time.** `_active_run`
(`daemon.py:172`) made a second concurrent tag impossible. P1 deleted that
refusal, so the collision is now reachable, and its failure mode is not a crash
— it is two investigations appending into **one** `queries.jsonl`, interleaving
qids, each citing evidence the other ran. **The record silently becomes
fiction.**

Note this is a *naming* bug, not an incident-modelling one. It fires precisely
when dedup has correctly decided two alerts are separate, and then the naming
layer merges them anyway. `nrql_log.py:32` already hardened the sibling case
(three queries sharing `q02`, 2026-07-07) but `flock` protects one file from
parallel writers; it cannot help two incidents that should never have shared a
file.

Fix costs nothing under P2: the row has a primary key the moment we insert, so
there is no surrogate id to *add*. The folder export and S3 prefix are named
`<slug>-<short-id>` — `2026-07-20T10-15Z-a3f1`. Readable at a glance, unique by
construction.

> **⚠ §3 and §4 amended by `design.md` §8a-D.** Dedup is not built at all.
> `condition_guess` is `None` in 8 of 8 real alerts and §4 below rules that no
> condition means a unique key, so it provably never fires — and a no-op guard
> reads as protection. `dedup_key` and `condition_guess` do not ship; both are
> pure functions of `raw` and backfill later. The real work item is capturing a
> Slack-rendered alert (§8b). Idempotency — §6 below — is unaffected and ships.

**3. Dedup stays simple and does not get hardened now.** The system is
tag-gated: a human reads the alert and decides to tag it. That human already
knows whether they are looking at something they just tagged, and will catch a
duplicate long before the bot does. Dedup is a convenience against a re-tag, not
a correctness mechanism. **Harden it when auto-trigger (design-v2 v2.1) removes
the human, which is the point at which it starts carrying real weight.**

**4. Never suppress on a guess.** `parse_alert.py:32` falls back to hashing the
first 120 characters of the message when no condition name is found. Alert
messages are templated, so a boilerplate prefix makes unrelated alerts hash
identically — and the consequence is that a **real incident is silently
suppressed** with one skimmable line in the thread. If no condition can be
identified, the dedup key is unique and dedup does not fire. Same shape as P3's
fail-toward-the-queue: under uncertainty take the loud cheap error, not the
quiet expensive one.

| | Cause | Cost | Visibility |
|---|---|---|---|
| False match | boilerplate prefix collides | a real incident never investigated | one line in a thread |
| False miss | same alarm, text drifted | a duplicate $2 run | obvious — two investigations appear |

**5. Concurrency dissolved into P1.** One container per investigation means
`_active_run` deletes and concurrency is free. No separate work.

### Findings from the eight real alert files (`prod/data/newrelic/incidents/`)

**`condition_guess` is `None` in 8 of 8.** The pattern set at
`parse_alert.py:14-18` has never once succeeded on anything we have. Combined
with §4 above, that means dedup is currently **decorative** — inverting the
fallback disables it outright until extraction works.

**Alerts are not generic; the parser is looking in the wrong place.** Every real
payload carries a clean, stable identifier — New Relic gives `title` plus an
issue uuid, CloudWatch gives an alarm name double-quoted inside `Subject`, plus
`Namespace` / `MetricName` / `Dimensions`. `parse_alert` runs regexes over prose
while the identity sits in structured JSON.

**2026-07-18T16-41Z and 16-57Z are the same alert text 16 minutes apart, and
dedup did not fire.** Design-v2 review-list item 1 caught in the wild:
in-memory state lost across a daemon restart, second run paid for.

**We have zero samples of an alert as a Slack message.** All eight arrived by
webhook or SNS directly; the only two `slack-tag` records are a human typing
`aws load balancer 503 error` by hand. `parse_alert` will only ever see the
Slack-rendered form, and we have never looked at one. Prediction, untested:
CloudWatch's `ALARM: "name"` hits pattern 1 because the name is double-quoted;
New Relic's `… on 'Error Transactions (%)'` misses because the quotes are
single. Worth capturing one of each before hardening.

**Structural fork, deferred with the hardening:** if the Slack integration posts
Block Kit attachments rather than plain text, the original JSON is still in
`event.attachments` / `event.blocks`, and the condition can be read as a *field*
instead of regexed out of a rendering. That is categorically better than any
prose heuristic. Whether it's available is a fact about the channel, not a
design choice.

**6. Idempotency: a unique constraint on `event_id`, on the incident row
itself, with no expiry — plus a second free layer at run-start.**

The design content is *where the guard lives*, not how long it lasts. OpenHands
keeps a Redis marker (`slack_msg:{client_msg_id}`, 60s) while the incident lives
in Postgres — two separate objects, so a crash between them leaves a guard
claimed with no work done, and the user sees nothing at all (Prior art). The TTL
is only how long that inconsistency lasts.

Put `event_id` as a unique column **on the incidents table** and the failure
becomes unrepresentable: *"claimed"* and *"exists"* are the same fact, stored
once, so they cannot disagree.

```sql
INSERT INTO incidents (event_id, …) VALUES (…)
ON CONFLICT (event_id) DO NOTHING
RETURNING id;
```

An id back means we created it. Nothing back means someone else has it — drop
the message.

**No TTL.** Slack's retry backoff runs out to roughly an hour, so any window
shorter than that leaves a hole, while the same window silently swallows a human
re-tagging inside it. One knob, both errors, no setting that fixes both. P2
already ruled this exact question the other way for the 30-minute dedup window:
*"the window is a query predicate, not a retention policy."* Cost is a few
hundred rows a year.

**Second layer, free.** A crash after the `INSERT` but before run-start leaves
an incident with no investigation, and on redelivery the `ON CONFLICT` correctly
refuses to create a second incident but must still start the run. Rather than a
status column and a state machine, use the **incident id as the Step Functions
execution name** — AWS refuses duplicate execution names for 90 days, so
`StartExecution` is naturally idempotent.

| Layer | Guarantees | Enforced by |
|---|---|---|
| unique `event_id` on the incident row | one incident per Slack delivery | Postgres |
| execution name = incident id | one run per incident | Step Functions, 90 days |

No lock, no lease, no timeout, and the consumer stays stateless — it can crash
anywhere and re-run the whole sequence from the top.

**7. No time bound on P3's thread rule.** This reverses the handoff P3 made to
this item; on inspection the concern does not survive.

**A Slack thread is anchored to exactly one parent message, and that parent is
the alert.** A second alert is a new top-level post from the integration with
its own thread. For one thread to acquire two alerts, a human must deliberately
paste an alert into an existing incident's thread — and if they do, they get a
question, the agent replies that it isn't what this incident is about, and the
error is loud and self-correcting.

**A bound would create a worse bug than the one it prevents.** When the window
expires, a follow-up question in an old thread is reclassified as an alert and
spawns a fresh $2 investigation of the original parent. Asking about last week's
incident is common, real behaviour; two alerts in one thread is not. The bound
converts a rare, loud, self-correcting misroute into a frequent, silent, and
billable one — the direction §4 already ruled against.

---

## P5. What the agent is allowed to do

**Question:** what enforces the read-only guarantee?

**Forced by:** nothing enforces it now. `ALLOWED_TOOLS` includes unrestricted
`Bash` (`investigator/run.py:31`). The AWS verb allowlist in
`aws_log.py:53` is real code, but the agent can bypass it by calling `aws`
directly, and can equally run `curl`, write anywhere on the filesystem, or
read files outside the run directory. `procedure.md` saying "never query any
other way" is an instruction, not a boundary.

The Q&A agent has the same shape: told "NEVER modify anything in this
folder" in its system prompt (`qa/agent.py:43`) while holding `Bash` and
write access to the record it exists to only read.

**This is the item most likely to matter to the design partner**, since it's
credentials-in-production territory rather than an availability concern.

**Axes that differ:**
- *Where the boundary sits* — the credential (an IAM role that can only
  read), the process (a container with no write path), or the tool layer
  (no raw `Bash`, only allowlisted commands).
- *Cost to the procedure.* Removing raw `Bash` means revisiting design-v2
  D11, which chose CLI-over-Bash uniformly and named its own upgrade
  triggers. Worth checking whether this counts as one of them.
- *Blast radius we're willing to accept.* Read-only-everything is the
  strong version; "can't touch anything outside its own record" is weaker
  and cheaper.

**Ruling (2026-07-20): a `PreToolUse` executable allowlist is the boundary.
Credential scoping backs it up rather than carrying it. Nearly all of this
ships now; the one item that needed real design turned out not to be needed.**

**The stake, established during the grill: `NEW_RELIC_API_KEY` and the
`hb-role` profile are the design partner's own credentials**, not
Ingren-owned ones. The agent is not one bad command away from breaking our
staging account — it is one bad command away from mutating the partner's
production monitoring. That reframes P5 from hygiene to a precondition.

**Threat model.** Not "the model goes rogue." **This agent reads production
logs and error messages, which contain strings outside parties put there.**
Prompt injection through log content is a real vector for an RCA agent
specifically, in a way it is not for a coding agent working on your own repo.

### What was reachable before this ruling

| Surface | Code | Reality |
|---|---|---|
| Tool set | `run.py:31` — `["Bash", "Read", "Write", "Glob", "Grep"]` | unrestricted shell |
| AWS read-only rail | `aws_log.py:53` `check_read_only` | real code, bypassed by typing `aws` into Bash |
| AWS identity | `aws_log.py:88` injects `--profile hb-role` | the partner's role |
| Q&A read-only | `qa/agent.py:41` *"NEVER modify anything in this folder"* | a sentence in a prompt, alongside `Bash` at `:60` |
| Every credential | `load_env_into_os` → `os.environ` → SDK subprocess → bash | `env` prints NR, AWS, Anthropic **and Slack** keys |

**1. The boundary is a `PreToolUse` hook that allowlists the executable, not
the command string.** Only the three invocations `procedure.md` actually names
are permitted:

> **⚠ Extended by `design.md` §8a-E.** "The three invocations `procedure.md`
> names" was not accurate — `procedure.md:52` also named two topology probes,
> which this allowlist would have denied silently. They were one-off discovery
> scripts, not tools: single hardcoded entity, no arguments, no evidence row.
> That line is deleted, which makes the sentence below true as written and
> restores D11's choke point.

```
python3 <TOOLS_DIR>/newrelic/nrql_log.py  --log-dir . …
python3 <TOOLS_DIR>/cloudwatch/aws_log.py --log-dir . …
python3 <TOOLS_DIR>/emit.py               --dir .    …
```

Everything else is denied. It sits next to the `PostToolUse` tap already in
`hooks.py`, costs roughly 50 lines, and **`procedure.md` does not change by a
character** — because D11's "CLI-over-Bash uniformly" already funnelled the
entire investigation through three executables. `procedure.md` is 127 lines and
mentions Bash exactly three times (`:43`, `:48`, `:67`). Everything else the
agent does goes through `Read`, `Glob`, `Grep`, and `Write`.

**The distinction is the whole ruling:**

| Hook style | Category |
|---|---|
| Filter Bash *command strings* for dangerous verbs | **guardrail** — bypassable by `eval`, base64, `python3 -c`, `curl`, variable indirection |
| Allowlist the *executable* | **control** — the escape hatches are precisely what is denied |

Rejected: command-string filtering. It is the same class of defence as today's
`check_read_only`, one layer up, and fails to the same tricks.

**Confirmed in the SDK source, and it rules out the obvious alternative.**
`PreToolUse` hooks can return `permissionDecision: "allow" | "deny" | "ask" |
"defer"` (`claude_agent_sdk/types.py:417`), so a hook is a real enforcement
point, not advisory.

The alternative — the SDK's `can_use_tool` callback — **would have been silently
dead code.** `types.py:1632` defines `CanUseToolShadowedWarning`: *"can_use_tool
is set but some tool calls are auto-approved before it runs."* A whole-tool entry
in `allowed_tools` auto-approves and the callback never fires, and `Bash` is
exactly that entry (`run.py:31`). **Hooks are not shadowed.** The choice of a
hook over `can_use_tool` is therefore forced by the SDK, not a preference.

Worth knowing but not adopted: `allowed_tools` entries support per-invocation
specifiers — `_whole_tool_allowed` (`types.py:1643`) shows `"Bash"` allows the
whole tool while `"Bash(ls:*)"` allows only matching invocations. That is a
second, cheaper expression of the allowlist, but it is an *auto-approve* list
rather than a deny, so the hook stays primary.

**2. `check_read_only` is promoted, not demoted.** An earlier position in this
grill was to demote it to a guardrail and stop counting it. That was wrong under
this design. Once `aws` is unreachable except through `aws_log.py`, the verb
allowlist at `aws_log.py:53` becomes **the only thing between the agent and the
partner's AWS account.** It is load-bearing and must be tested as such.

**3. New Relic needs no proxy.** `nr_run_nrql.py:36-43` builds a **fixed
GraphQL document** and embeds the NRQL with `json.dumps`. The agent controls the
quoted argument, never the query structure, so there is no path to a NerdGraph
mutation — and NRQL is a read-only language by design. Reached only through
`nrql_log.py`, the partner's key can run SELECTs and nothing else.

This removes the one P5 item flagged as needing genuine design. P1b's
egress-substitution goal ("the agent's shell should never hold a usable New
Relic or AWS credential") is met in the form that matters: the shell cannot
*reach* the credential, even though the process still holds it.

**4. Two supporting changes, without which the hook leaks.**

- **Credentials come from Parameter Store / task-definition secrets, never a
  file on disk.** `nr_run_nrql.py:16` reads `.env` from `parents[2]` directly
  rather than `os.environ`, so today the file must exist in the container — and
  `Read` can open it. This is P9's work, and **P5 depends on it.**
- **`Read` / `Glob` / `Grep` are confined to the run directory and the tools
  directory.** Even with `.env` gone, environment variables are readable via
  `Read /proc/self/environ`, since the SDK subprocess inherited them. Blocking
  `env` in Bash without blocking `/proc` in `Read` locks one door and leaves the
  one beside it open.

**5. Q&A keeps `Bash`, deliberately.** P2 gives it an exported tmpdir per
question, so its write target is a copy we delete. The `NEVER modify` prompt
line stops being load-bearing. Recorded so the reason is on the record rather
than the omission looking like an oversight.

> **⚠ Reversed by `design.md` §8a-C.** Q&A does not keep `Bash`. The reasoning
> above covers writes to the record and nothing else — it misses that Q&A runs
> **in the Service process**, which per §9 §4 holds `SLACK_BOT_TOKEN`,
> `SLACK_SIGNING_SECRET`, `ANTHROPIC_API_KEY`, and a Postgres role with
> `UPDATE`. Its input is incident data, which is this section's own named
> injection vector. Q&A gets the same `PreToolUse` allowlist as the
> investigator, permitting one read CLI.

**6. `SLACK_BOT_TOKEN` is removed from the investigation task.** Pure deletion —
the investigator never posts; the Service does. Largest blast-radius cut per
unit of work, and it means a compromised investigation cannot speak as the bot.

### Deferred, with triggers

| | Item | Trigger |
|---|---|---|
| AWS read-only task role | with the hook in place this is defence in depth, not the primary control | when the partner is willing to scope `hb-role`; it is their role, so this is a conversation, not a config change |
| NR egress proxy | superseded by §3 unless the wrapper's shape changes | if a future tool needs raw NerdGraph access |
| Removing raw `Bash` entirely | unnecessary — §1 achieves the same effect without touching D11 | only if the allowlist proves unworkable |
| **Prompt injection** | residual and unaddressed | first time the agent surfaces something from a log that reads like an instruction |

Blast radius after this ruling is bounded to **a misleading `rca.md`** — the
agent can be steered into writing something wrong, but not into doing something
destructive. That is a materially different failure than the one we started
with.

---

## P6. Who may trigger a run, and what it may cost

**Question:** authorization, rate limits, and spend ceilings.

**Forced by:** none of these exist. Any user in any channel the bot belongs
to can spawn a ~$2 investigation. Q&A has no gate at all — `_handle_question`
(`daemon.py:107`) starts a thread per mention with no slot, no dedup, and no
cap, so N mentions are N concurrent paid LLM calls.

**Axes that differ:**
- *Where the gate sits* — channel allowlist, Slack user or group membership,
  or an explicit approval step for expensive actions.
- *What a limit protects.* A per-user rate limit stops accidents. A global
  daily spend ceiling stops runaway loops. They're different failure modes
  and one doesn't imply the other.
- *Behaviour at the ceiling* — refuse loudly, queue, or degrade to a cheaper
  model.
- *Cost visibility.* Investigator runs record `cost_usd` in `events.jsonl`
  (`run.py:158`); Q&A records nothing. Nothing aggregates either.

**Escalated by P7.** Auto-resume raises the worst case to $6 per incident,
and P1 removes the one-at-a-time limit that was implicitly capping spend.
P7's ruling is explicitly blocked on this one — the spend ceiling ships
first, or auto-retry doesn't ship.

**Ruling (2026-07-20): a count-based rate limit as a hard stop, a daily total
as an alarm that never stops anything, and a channel allowlist. No dollar
ceiling, no per-user gating.**

**1. The ceiling is denominated in runs, not dollars.** A dollar ceiling
requires knowing dollars, and **cost is only knowable after the run** — you
cannot refuse the investigation that would breach the limit, only the one after
it. So a dollar ceiling is a count ceiling with extra steps and a fuzzier
boundary. A count is available *before* the spend, as a `COUNT(*)` over rows P2
already keeps forever.

Checked against the modes it exists to stop: a runaway loop and a Q&A storm are
both caught by a count, and a single run burning all 150 turns is caught by
`max_turns`, which already exists and was never the ceiling's job.

**2. Rate limit and daily total have different severities, not just different
windows.** This is the substance of the ruling.

| Mechanism | Window | Behaviour | Protects against |
|---|---|---|---|
| **Rate limit** | minutes | **hard stop**, self-healing | runaway loop, Q&A storm |
| **Daily total** | 24h | **loud alarm, never a stop** | slow bleed nobody noticed |

Rejected: a daily cap as a hard stop. **A ceiling that turns the agent off is a
self-inflicted outage of the thing that responds to outages.** A daily cap hit
at 14:00 means the 3am page goes unanswered, caused by our own spend policy.
Overspending is visible on an invoice; an unanswered alert is visible nowhere.
A rate limit can be a hard stop precisely *because* it is temporary — worst case
is a few minutes of refusal, not a dead night.

**3. The rate limit is a backstop against a bug, not a policy against users.**
Manual tagging peaks at 2–4 alerts over a few minutes during a real cascade; a
runaway loop is many per second. Three orders of magnitude apart, so any
threshold between them works and **the number never needs tuning.** If it ever
fires, that is a signal something is broken, not that someone was enthusiastic.

Value: **5 investigations per 10 minutes, global.** Q&A is bounded by the
Service's SQS consumer concurrency, which P3 already gave us for free — no new
component, just a number.

**4. At the limit: refuse, in the thread, naming the limit.** Rejected:
queueing the excess. **Investigations are perishable.** NR events last ~8 days
but usefulness is far shorter — a run delayed 40 minutes narrates history to
someone who has already handled the incident, and `procedure.md` reasons about
"now" relative to the alert, so the investigated window is no longer the
interesting one. Queueing perishable work turns a rate limit into a latency
bomb, and the pathological case is ugly: a loop fires 500 times, they all
queue, the loop is fixed, and we spend $1000 investigating 500 copies of a
resolved incident — because P4 ruled dedup stays dumb.

**5. Authorization is a channel allowlist. No per-user gating.** The bot acts
only in channels on an explicit list. P3's ingress now receives events for every
channel the bot is in, and being invited to a channel is easy and accidental —
an invite to `#general` should not be able to spend money. Per-user ACLs are
friction against a threat that does not exist: the users *are* the on-call team.

**5b. Addendum — the SDK ships a per-run dollar ceiling.** `max_budget_usd`
(`claude_agent_sdk/types.py:1806`): *"The query will stop if this budget is
exceeded, returning an `error_max_budget_usd` result."*

This does not change §1 — a *fleet-wide* ceiling still cannot refuse a run before
its cost is known, so the rate limit stands. But it gives a **per-run hard stop
denominated in dollars for free**, which `max_turns` only approximates, and it
covers the one failure mode §1 conceded to `max_turns` rather than to the
ceiling. Set it.

It also composes with **P7**: `error_max_budget_usd` is a distinguishable
terminal result, which feeds the exit-code precondition directly and lands
exactly in P7's "budget breach is a policy stop, never retried" carve-out.

**6. Record Q&A cost.** `qa/agent.py` drops the cost fields from
`ResultMessage`; `run.py:158` already writes `cost_usd`. Roughly five lines,
and without it the daily alarm measures half the spend.

Where the daily alarm lands is **P10's** problem — alerting on the
alert-responder cannot depend on the alert-responder.

### This unblocks P7

P7's precondition was *"2 attempts × $2 with no ceiling is how you find out
about a retry loop from the invoice."* The 2-attempt cap bounds a single
incident; the ingress rate limit bounds how many incidents can start. Between
them the worst case is bounded **without a dollar ceiling existing at all**, so
P7's auto-retry ships once this does.

---

## P7. Failure, retry, and resume

**Question:** what happens when a run dies at turn 140 of 150?

**Forced by:** design-v2 D8 ruled "no auto-retry — a manual re-run command
on the folder is the escape hatch," and that command was never built. Today
a failure loses the whole run and costs full price to repeat. On a laptop
you notice; hosted, at 3am, nobody does.

**This item revisits D8.**

**Axes that differ:**
- *Retry versus resume.* Restarting from `alert.json` is simple and pays
  twice. Resuming mid-investigation needs the SDK to support it and needs
  the partial record to be trustworthy.
- *What partial output is worth.* D8 already says partial capture matters
  because NR retention is ~8 days. Whether a half-finished `rca.md` helps
  or misleads on-call is a judgement call.
- *Who decides to retry* — automatic with a cap, or a human in the thread.

**Ruling (2026-07-20): two tiers.**

1. **Auto-resume, capped at 2 attempts**, and only for the infrastructure
   tier — the Fargate task died for reasons unrelated to the investigation
   (spot reclaim, AZ blip, OOM, deploy). Resume, don't restart.
2. **Everything else stops and posts to the thread** with what was captured
   and why it stopped. D8's "failure is loud, never silent" holds.

> **⚠ Tier 1's mechanism amended by `design.md` §8a-B.** Both tiers stand;
> tier 1 **restarts** rather than resumes. The argument below is a cost one,
> and ~$2 is noise against ~$36/month of idle Fargate — while restart needs no
> S3 bucket, no `SessionStore`, no `load()`, and no resume state. It is a Step
> Functions `Retry` block. `SessionStore` and the S3 mirror are deferred to a
> named trigger. The exit-code precondition below still ships, and `queries` /
> `events` carry an `attempt` column so a restart's second set of rows stays
> distinguishable.

**Budget breach never retries.** It is a policy stop, not a failure. D8's
wording is kept verbatim. Poison `alert.json` is never retried.
Tool-environment failures (expired credentials, NR down) wait for a human to
fix the cause.

**Mechanism — the SDK supports this natively.** `ClaudeAgentOptions` exposes
`resume`, `session_id`, `fork_session`, and a `SessionStore` protocol
(`types.py:1440-1490`) built for ephemeral compute:

- `append()` mirrors transcript entries at ~100ms cadence *after* the local
  write, and is fail-open — 3 retries, then a `MirrorErrorMessage`, and the
  run continues unaffected.
- `load(key)` materializes the session to a temp JSONL and the subprocess
  resumes from it.
- Entries carry a stable `uuid` to use as an idempotency key. Postgres JSONB is
  named as one supported adapter shape, but **P2 rules the mirror onto S3
  instead** — S3 has no append operation, so the adapter writes one new object
  per 15-second flush rather than rewriting a growing one. See P2 §5.

Resume is not free: the transcript replays as input context, largely
absorbed by prompt caching. Still far cheaper than a $2 restart.

**Blocking precondition — distinguishable exit codes.** Today wall-clock
breach (`run.py:136`), any exception (line 141), and `is_error` (line 152)
all return exit 1, and `_watch_run` posts only `exit {code}`. Nothing
downstream can tell a spot reclaim from a poison alert, so a retry policy on
top either retries nothing or retries everything. Roughly 20 lines of work,
and it gates this entire ruling.

**Dependency flagged:** 2 attempts x $2 is a $6 worst case per incident, and
P6 has no spend ceiling. Auto-retry must not ship before P6 is ruled.

**Resolved (2026-07-20).** P6 ruled a count-based rate limit — 5 investigations
per 10 minutes, global, refusing at the limit. The 2-attempt cap bounds one
incident; the rate limit bounds how many can start. The worst case is bounded
without a dollar ceiling existing, so this dependency is cleared. The exit-code
precondition still stands.

This ruling **revises design-v2 D8**, which said "no auto-retry."

---

## P8. Live narration after P2

**Question:** how does the Slack thread stay live once the record moves?

**Forced by:** `poster.py` opens a local file, seeks to a byte offset, and
polls every 2 seconds. It runs as a thread inside the daemon because the
daemon already holds the Slack client and the thread anchor (unit-8 ruling).
Both facts break when the run executes elsewhere.

**Axes that differ:**
- *Push or pull.* The runner posting its own milestones directly, versus
  something watching the record and narrating.
- *Where the Slack credential lives.* Push means the runner needs Slack
  access, which widens P5's blast radius.
- *Ordering and duplicates.* Byte offsets gave exactly-once narration for
  free. Most alternatives don't.

**Also on the list from design-v2:** the poster swallows every exception
silently (`poster.py:71`), so a bad token means the thread just goes quiet.
And `_handle_question`'s 300-second timeout raises inside a daemon thread
with no handler, so a slow Q&A leaves the user on "Looking at the record…"
permanently.

**Ruling (2026-07-20): a single-task poller pulls from Postgres and posts a
message per milestone, as today. Two authorities — the events table narrates,
Step Functions terminates.**

**1. Measured first: the volume is nine posts per run.** The one scored run
(`2026-07-18T02-47Z`, 432s, 56 events) breaks down as 45 `tool_call`, 6
`hypothesis`, and one each of `timeline_settled`, `self_check`, `doc_ready`,
`run_started`, `run_finished`. Only the middle four are posted, so **nine posts
over seven minutes — one every 48 seconds**, against Slack's limit of roughly
one per second per channel. **48x of headroom.**

*This corrects an earlier claim in the Prior art section*, which called
`poster.py`'s unthrottled per-milestone posting an unpriced problem and treated
it as an argument for an edited checklist. It was wrong by a factor of ~48.

**2. Narration model: append one message per milestone. Unchanged from today.**
With the cost argument gone, the choice among the four observed models is purely
about notification behaviour — and an edited message (Claude Tag) or a status
line (Hermes) notifies nobody. Slack sends no notification on edit. **The entire
point is that on-call learns at 3am that a hypothesis was ruled out**, so the
model that pings is the model that wins. Link-out (OpenHands) fails the same
test and adds a click.

**3. Push is out; narration is a pull.** P5 removed `SLACK_BOT_TOKEN` from the
investigation task, but the deeper reason is architectural: a runner that posts
to Slack **couples the investigation container to Slack**, breaking D3's
"alert.json in, record out" seam that P2 deliberately preserved.

**4. A single-task poller, not a lease.** The byte-offset tail
(`poster.py:48-58`) is replaced by a row-id cursor, exactly as P2 anticipated
(*"P8's narration becomes a query instead of a byte-offset tail"*):

```sql
SELECT * FROM events WHERE incident_id = ? AND id > ? ORDER BY id;
```

P1's two Service tasks would both poll and post everything twice, so something
must claim each batch. **Rejected: a claim-with-lease on the Service** — claim,
post, commit, with lease expiry causing a re-post so failures produce duplicates
rather than gaps. Correct, ~15 lines, and a distributed-systems mechanism
introduced to solve a problem created by running two tasks.

A single-task poller removes the race instead of managing it: post, then advance
the cursor, and no coordination exists to get wrong. Cost is a third ECS
construct and a single point of failure for narration — acceptable because
**narration dying never loses the record.** The evidence is in Postgres either
way, and `rca.md` still lands.

**5. Two authorities, split by what each actually knows.**

| Authority | Answers |
|---|---|
| The events table | what the investigation found — narration |
| **Step Functions execution status** | whether the run is over, and how |

`_watch_run` (`daemon.py:90`) dies with P1, and there is a real gap under it:
**a hard task death writes no row.** Spot reclaim, OOM, or a deploy kills the
container before `run.py` can emit `run_failed`, so the events table shows a run
that stops mid-sentence and a table-only poller waits forever.

The poller already loops over active incidents; for each it also calls
`DescribeExecution` — at most a handful of calls per cycle under P6's rate
limit, which is nothing.

**This composes with P7 rather than fighting it.** Auto-resume means an
execution is not terminal until its retries are exhausted, and
`DescribeExecution` reflects that. A poller reading only the events table would
post *"FAILED"* while a resume was already underway. P7's exit-code precondition
feeds directly in: distinguishable codes are what let the terminal message say
something better than `exit 1`.

**6. Stop swallowing exceptions.** `poster.py:71` catches everything, so a bad
token makes the thread go quiet — indistinguishable from a slow investigation.
The blanket catch was correct when narration ran inside the daemon and must not
kill the run; the poller is now its own task, so a crash costs only narration.
Log it, and let repeated failures be a **P10** liveness signal. Something whose
only job is talking should be loudly broken when it cannot talk.

**7. `_handle_question`'s timeout posts.** The 300-second timeout
(`daemon.py:117`) raises inside a daemon thread with no handler, leaving the
user on *"Looking at the record…"* forever. Under P3, Q&A runs on the Service
off `inbound`; catch the timeout and post — *"that took too long, ask something
narrower."* A dead end the user can act on beats silence.

---

## P9. Secrets and configuration

**Question:** how do credentials reach a hosted process?

**Forced by:** `.env` is hand-parsed in four places — `daemon.py:51`,
`run.py:34`, `lf_mirror.py:23`, `nr_run_nrql.py:19` — three near-identical
and one returning a dict instead of mutating the environment. All three
mutating versions use `os.environ.setdefault`, so a stale exported shell
variable silently wins over the file. The file itself holds production New
Relic, AWS, Anthropic, and Slack credentials on a laptop.

design-v2 D8 says "secrets from env only; never read into prompts." The
second half holds. The first needs a hosted answer.

**Axes that differ:** managed secret storage versus injected environment
variables; whether rotation is a requirement or a nice-to-have; whether the
four parsers collapse into one loader or disappear entirely.

**Mostly mechanical once P1 lands** — probably not worth a full grill.

**But P5 now depends on it, which promotes it from cleanup to a precondition.**
`nr_run_nrql.py:16` reads `.env` from disk rather than `os.environ`, so the
credentials file must exist in the container for New Relic to work — and the
`Read` tool can open it. P5's boundary does not hold until the loaders take
credentials from Parameter Store / task-definition secrets and no `.env` ships
in the image.

**Ruling (2026-07-20): SSM Parameter Store into task-definition `secrets`. All
four loaders are deleted, not collapsed. Secrets are scoped per task, and
Postgres gets three roles.**

**1. The secret set is changing under P3 and P2, before anything is decided.**

| Secret | Status |
|---|---|
| `SLACK_APP_TOKEN` | **deleted** — Socket Mode is gone (P3) |
| `SLACK_SIGNING_SECRET` | **new** — HTTP signature verification (P3) |
| Postgres credentials | **new** (P2) |
| AWS credentials | **deleted** — `--profile hb-role` becomes a task role (P1/P5) |
| `SLACK_BOT_TOKEN`, `ANTHROPIC_API_KEY`, `NEW_RELIC_*`, `LANGFUSE_*` | unchanged |

**2. Source: SSM Parameter Store SecureString, injected by the task
definition's `secrets` block.** Free at standard tier, arrives as ordinary
environment variables. Rejected: Secrets Manager — $0.40/secret/month for
rotation we do not need yet. Rotation is a named upgrade trigger, not a
requirement.

**3. Delete all four loaders. Do not collapse them into one.** With the
platform injecting environment variables, the code reads `os.environ` and
nothing parses anything.

| Where | Shape | What deletion fixes |
|---|---|---|
| `daemon.py:51` | mutates `os.environ` | `setdefault` — a stale shell var silently beat the file |
| `run.py:34` | same (`qa/agent.py` imports it) | same |
| `lf_mirror.py:23` | same | also **re-read `.env` from disk on every span**, i.e. once per tool call |
| `nr_run_nrql.py:19` | returns a dict, reads the file directly | **the one blocking P5** — it ignores `os.environ` entirely |

Local development keeps a `.env`, loaded *outside* the code by
`uv run --env-file .env`. The application never learns which environment it is
in, which is precisely what P5 needs since no `.env` ships in the image.

**4. Secrets are scoped per task. This is P5's enforcement, not housekeeping.**

| Secret | Service | Poller | Investigation Task |
|---|---|---|---|
| `SLACK_SIGNING_SECRET` | ✓ | — | — |
| `SLACK_BOT_TOKEN` | ✓ | ✓ | **✗ (P5)** |
| `ANTHROPIC_API_KEY` | ✓ | — | ✓ |
| `NEW_RELIC_*` | — | — | ✓ |
| `LANGFUSE_*` | — | — | ✓ |
| AWS | task role | task role | task role |
| Postgres | `rca_service` | `rca_poller` | `rca_agent` |

**5. Three Postgres roles, not one.**

| Role | Grants |
|---|---|
| `rca_agent` | `INSERT` on evidence tables, **`SELECT` scoped to its own incident**. No `UPDATE`, no `DELETE`, no cross-incident read |
| `rca_service` | `SELECT`/`INSERT`/`UPDATE` on operational state, `SELECT` on evidence |
| `rca_poller` | `SELECT` on events, `UPDATE` on the narration cursor only |

Roles are free — a `CREATE ROLE` and a `GRANT` — and collapsing them would
silently undo P2, because a shared role must be the union of all three, which
hands the investigation container `UPDATE` and `DELETE` back.

### Amends P2 §2 — "INSERT-only" was too strong

P2 ruled the agent's database role **`INSERT`-only: "no `UPDATE`, no `DELETE`,
no read across incidents."** Taken literally that breaks the investigation at
runtime, and it would fail deep into a paid run.

`procedure.md:104-105` requires the self-check pass to *"verify the number and
the cited query id against `queries.jsonl`"*, and `:95` requires an appendix
mapping id → query for every cited id. **The agent must read back its own
evidence.** That step is what makes citations trustworthy, so losing it is not
a graceful degradation.

The correct grant keeps P2's intent and drops its over-reach: **the protection
that matters is no `UPDATE` and no `DELETE`** — the record stays append-only —
plus no read *across* incidents. Reading its own incident was never the threat.

Scoping the read: preferred is Postgres row-level security keyed on an incident
id taken from the **task environment**, not from agent input, so the agent
cannot widen it. If RLS proves fiddly, the wrapper CLI scoping the query is
acceptable — P5's executable allowlist means the agent cannot reach Postgres
except through those wrappers, which is a materially stronger position than P2
was written under.

---

## P10. Health and liveness of the service itself

**Question:** how do we know the agent is alive?

**Forced by:** the monitoring story is `pgrep -fl daemon.py` typed by hand
(README:32). The system whose job is noticing production problems can be
silently dead, and the failure mode is indistinguishable from a quiet week.

**Axes that differ:** heartbeat versus synthetic end-to-end probe;
where the alert lands, given that alerting on the alert-responder can't
depend on the alert-responder; whether Langfuse (design-v2 D9) already
carries enough signal to serve as the liveness check.

**Ruling (2026-07-20): a CloudWatch Synthetics API canary exercising the real
ingress path, alerting via CloudWatch alarm → SNS, never Slack. Daily for now,
15-minute at go-live.**

**1. "The process is dead" is already solved by the platform.**

| Construct | Liveness, free |
|---|---|
| Service (2 tasks) | ALB health check → ECS replaces an unhealthy task |
| Poller (1 task) | ECS desired-count=1 → restarts on crash |
| Investigation Task | Step Functions execution status (P8 §5) |

**2. That is not the failure mode that matters.** None of the above catches
Slack credentials revoked, Slack disabling event delivery after repeated
failures, the bot removed from the channel, or a consumer alive but no longer
polling. **Every one presents identically as "no alerts arrived" — and so does
a quiet week.**

Rejected: a heartbeat from our own process. It proves the process runs and
proves nothing about the Slack delivery path, which is the fragile link. Only a
probe that traverses the real path can distinguish healthy-and-quiet from dead.

**3. CloudWatch Synthetics, API canary.** The probe posts `@rca ping` into a
canary channel and asserts a reply. That traverses Slack → ALB → signature
verify → `inbound` → router → Slack post: the whole ingress, at effectively zero
LLM cost.

Rejected: a hand-rolled Lambda plus EventBridge schedule plus `PutMetricData`
plus alarm wiring. Synthetics gives scheduling, `SuccessPercent` / `Duration` /
`Failed` metrics, alarm integration, and a console for the same two HTTPS calls.

Honest about what we're buying: artifact capture (HAR files) is useless for an
API probe, and AWS deprecates canary runtimes on their schedule, not ours. At
~$0.0012/run that is noise, and the project rule is the cheapest layer that
*works*, not the cheapest bill. Fewer things we maintain wins.

**4. The probe deliberately stops short of the investigation.** Those failures
are already loud — Step Functions surfaces them and P8's poller posts a terminal
message. The silent path is the ingress, and that is what the probe covers. A
real Q&A probe would cost more per month than the canary saves.

**5. The ping check sits AFTER the thread lookup, not before.** A ping that
short-circuits at the top of the router would pass clean through a broken
routing lookup — the thing P3 spent five questions on. Placed after, the router
performs its real `SELECT` on `thread_ts` and only then recognizes the ping, so
a dead database, a stalled consumer, or a broken lookup all fail the canary.

**6. This is the one deliberate exception to P1's one-substrate rule.** P1
rejected Lambda for Q&A and P3 rejected it for the router, both to keep the
deployment model singular. That reasoning inverts here: **a monitor that shares
fate with the thing it monitors is not a monitor.** A canary running on the
Service goes silent exactly when the Service dies, which is the failure being
detected. Separate infrastructure is the requirement, not incidental cost.

**7. Interval: once per day now; 15 minutes at go-live.** Pre-live the canary is
a smoke test that the wiring works, and daily is enough for that at ~$0.04/month.

**Named trigger — go-live.** Detection latency must be shorter than the gap
between real alerts, so a 24-hour probe is useless in production. Fifteen
minutes rather than five keeps the canary channel to 96 messages a day instead
of 288, and bounds the blind window to something shorter than a typical incident.

**8. The alarm never lands in Slack.** Alerting on the alert-responder cannot
depend on the alert-responder, and Slack is the dependency most likely to be the
thing that broke. CloudWatch alarm → SNS.

### The full layering

| Layer | Catches | Cost |
|---|---|---|
| ALB health check | Service dead or hung | free |
| ECS desired-count | Poller dead | free |
| **Synthetics canary** | **Slack delivery broken, subscription disabled, bot removed, consumer stalled** | ~$0.04/mo now, ~$3.5/mo at go-live |
| Step Functions failure events | investigation infrastructure failures | EventBridge rule |
| Daily spend total (P6) | slow bleed | one query |
| Poller error rate (P8 §6) | narration broken while everything else looks fine | log metric |

**Unresolved and worth naming: SNS → email is an alert nobody reads at 3am.**
This ruling establishes that a signal exists and where it originates. Routing it
to a path a human actually answers is a separate problem, and building one is
out of scope here. **Named trigger — the same go-live that raises the interval.**

---

## P11. Language and runtime

> **⚠ REVERSED IN FULL by `design.md` §8a-F, 2026-07-21.** The ruling below —
> TypeScript, in `prod/ingren-agents`, tools sequenced second — is **not what
> is being built.** The system stays **Python** and is built in **`prod/agents`
> in place**. Slice 7 and the three-line `procedure.md` edit are deleted.
>
> This is the only reversal in the register; A–E are amendments. Two of the
> supports below died within a day of being written — §4's `S3SessionStore`
> argument (§8a-B deferred the session store) and §3's matplotlib argument
> (§8a-C deferred charts) — and the criterion that decides it, one engineer
> holding one language, is not weighed anywhere below.
>
> **Read it anyway.** §1's parity table is still accurate and still useful, §7's
> transcript gotcha is language-independent and load-bearing for P5, and §8a-F's
> argument only makes sense against what this ruling actually said.

**Question:** Python or TypeScript?

**This item revisits design-v2 D3**, which chose the Claude Agent SDK in
Python because every proven asset was Claude-Code-native and the SDK was the
engine v1 rehearsed on.

**Deliberately last, because it's a consequence of P1.** A Lambda-heavy
answer weights differently from a long-running container answer, and there
is no way to weigh it honestly before P1 is ruled.

**Largely deflated by a finding under P1.** `claude_agent_sdk` is a thin
client over the `claude` Node CLI, which it spawns as a subprocess. Node and
`@anthropic-ai/claude-code` are in the runtime image either way, so the
language choice changes neither runtime weight nor cold start. It's a
question about our ~1,400 lines of glue, not about the agent.

P1 also landed on Fargate, which removes the cold-start argument entirely.

**Axes that still differ:**
- *What actually gets rewritten.* `procedure.md`, both NOTES files, and the
  record contract are language-agnostic and carry most of the proven value.
  The glue is the only thing in scope.
- *SDK parity* between the Python and TypeScript Agent SDKs for the features
  actually used: hooks, `setting_sources`, `max_turns`, cost reporting, and
  now `SessionStore` (P7 depends on it).
- *matplotlib.* The Q&A chart path (`qa/agent.py`) is the one genuinely
  Python-shaped dependency.

**Ruling (2026-07-20): TypeScript, in `prod/ingren-agents`. Tools move too, but
sequenced second.**

**1. SDK parity is total, and structurally so.** Checked against the installed
Python SDK (`claude_agent_sdk` 0.2.122, reading `types.py`) and the TypeScript
Agent SDK reference.

| Feature we use | Python | TypeScript |
|---|---|---|
| `PreToolUse` / `PostToolUse` hooks | `hooks: dict[HookEvent, list[HookMatcher]]` (`types.py:1913`) | `hooks: Partial<Record<HookEvent, HookCallbackMatcher[]>>` |
| **Hook can deny a tool call** | `permissionDecision: "allow" \| "deny" \| "ask" \| "defer"` (`:417`) | same protocol |
| `setting_sources` | `:1953` | `settingSources` |
| `max_turns` | `:1800` | `maxTurns` |
| Cost reporting | `total_cost_usd` (`:1211`) | `total_cost_usd` |
| `SessionStore` / `resume` / `fork_session` | `:1426`, `:1790`, `:2058` | `sessionStore`, `sessionStoreFlush`, `persistSession`, `resume`, `forkSession` |

Parity is not coincidence: **both packages are thin clients spawning the same
`claude` Node CLI**, so hooks, session storage, and resume are CLI-level
features the SDK only marshals. The Python source states the lockstep outright
(`types.py:1634`): *"The TypeScript SDK reports the same condition as a process
warning with code `CLAUDE_SDK_CAN_USE_TOOL_SHADOWED`."*

**2. The deciding argument is timing, not preference.**

| | Lines | Fate |
|---|---|---|
| Tools (`nrql_log`, `aws_log`, `emit`, `nr_run_nrql`, `lf_mirror`, …) | **595** | agent-facing — named verbatim in `procedure.md` |
| Glue (daemon, run, hooks, qa, parse_alert, poster) | **646** | portable |

**Roughly 600 of the 646 portable lines are already being rewritten** —
`daemon.py` splits under P3, `poster.py` is replaced under P8, `run.py` gains
exit codes, `SessionStore`, a `PreToolUse` hook and a Postgres sink under
P7/P5/P2. The only pointless port is `parse_alert.py`, 41 lines of regex and a
hash. **We are choosing the language of new code, not paying for a migration.**

**3. The two expected blockers aren't.**

*matplotlib is not a dependency of our code.* `qa/agent.py:53` hands the agent
an **interpreter path** and the agent shells out to it. The image needs python3
+ matplotlib either way; the glue passes a string.

*The runtime gets simpler.* The image already needs both:

```
Python glue:  python → claude CLI (node) → bash → python3 tools
TS glue:      node   → claude CLI (node) → bash → python3 tools
```

One fewer runtime in the parent process — parent and CLI share Node.

**4. A find that pays immediately.** The TypeScript SDK repo ships a reference
**`S3SessionStore`** (`examples/session-stores/s3`): *"One JSONL part file per
`append()`; `load()` lists, sorts, and concatenates."* That is **exactly** the
adapter P2 §5 designed by hand — new object per flush because S3 has no append,
LIST plus concurrent GETs on load. We copy a reference implementation and run
the shipped conformance suite instead of writing and testing our own. A Postgres
adapter ships too.

**5. Tools move to TypeScript as well — sequenced second, not simultaneously.**
Nothing in them is Python-shaped: HTTPS POST to NerdGraph, `subprocess` to the
`aws` CLI, and (post-P2) Postgres inserts. `json.dumps` → `JSON.stringify` has
the same escaping semantics, which is what P5 §3 relies on.

Rejected: a permanent polyglot seam ("anything the agent invokes is Python").
That is a rule invented to justify a split rather than one the problem demands.

**Sequenced glue-first** because doing both at once means rewriting the evidence
path with no working reference and **no tests** (P12) to catch a regression.
Glue first runs the new architecture against tools that already work; tools
second, verified on one real alert, against a known-good other half.

Three consequences to write down rather than discover:

- **`procedure.md` gets a deliberate 3-line edit** (`:43`, `:48`, `:67`) —
  `python3 …` becomes `node …`. Every prior ruling protected this file; P2 noted
  it *"does not change by a single character."* This is the one exception, and
  it is an edit, not a rewrite.
- **Tools ship precompiled to plain JS.** The measured run made **45 tool
  calls**; `tsx`/`ts-node` per invocation would add 200–500ms of compile each
  time — 20+ seconds a run for nothing. Precompiled Node startup (~40ms) matches
  Python's.
- **matplotlib is tracked as a separate open question.** It is the last thread
  keeping Python in the image. Removing it means agent-authored SVG or a JS
  charting library plus a rasterizer for Slack upload — **a Q&A product decision,
  not a language one.** Not smuggled into this ruling.

The honest counter to "the tools are proven code": there are **no tests**, so
proven means "ran on nine incidents," and a port is validated the same way — one
real alert. P2 rewrites their sink regardless.

**6. P5 is indifferent to the language.** Its boundary is an executable
allowlist; `node <TOOLS_DIR>/nrql_log.js` is exactly as enforceable as
`python3 <TOOLS_DIR>/nrql_log.py`.

**7. Gotcha, language-independent, found while checking parity.** The session
store is a **mirror, not a replacement** — the subprocess always writes
transcripts to local disk (`~/.claude/projects/`) first, then the SDK forwards
each batch to `append()`. So the transcript is a file the agent could `Read`.
Point `CLAUDE_CONFIG_DIR` at a temp dir via `options.env`, and make sure P5's
`Read` confinement covers it. Also: `sessionStore` with `persistSession: false`
throws, as does combining a store with file checkpointing.

This ruling **revises design-v2 D3**, which chose the Python SDK because every
proven asset was Claude-Code-native. That reasoning held; what changed is that
the proven assets — `procedure.md`, both NOTES files, the record contract — turn
out to be language-agnostic, and the SDK-coupled surface is thinner than D3
assumed.

**Target tree: `prod/ingren-agents`** (empty git repo, no commits as of
2026-07-20). `prod/agents` becomes the reference implementation, not the
deployment target.

---

## Prior art

Reference, not rulings. Recorded because several P items were decided from
first principles with no external check, and it's worth knowing where the
decisions agree with people who have shipped this and where they deliberately
don't.

### Claude Tag (Claude in Slack) — researched 2026-07-20

Anthropic's own Slack-resident agent. Same problem preface as ours:
tag-invoked, thread-scoped, minutes of work behind Slack's 3-second ack.

**Converges with what we ruled — three independent confirmations.**

- **Routing is by thread membership, not message content.** A tag in a channel
  starts a new session; a tag inside a thread continues that thread's session;
  once Claude is in a thread, every reply reaches it without a mention. This is
  P3 §2, arrived at independently by a team with far more Slack telemetry than
  we have. It also confirms the cost we accepted — thread membership is state
  you look up, and they pay that lookup too.
- **One sandbox per thread**, keyed on the same thing P1 keys a container on.
- **Immediate non-blocking ack.** An *"is thinking…"* line appears when Claude
  picks the message up. Confirms P3 §1 — ack first, work after, human-visible
  feedback via a posted message rather than the HTTP response.
- **Explicit human escape hatch over automatic logic.** There is no documented
  dedup or auto-retry; there is `!restart`, which archives the session and
  starts fresh. A conversational product at scale chose *"let the human say
  so"* over *"detect it."* That is P4 §3 (the human is the correlator) and is
  consistent with P7's "everything except infrastructure failure stops and
  posts to the thread."

**Diverges, and we should stay diverged — progress narration.**

They post a checklist as the first reply and **edit it in place**. We post a new
message per milestone (`poster.py:67`). Their docs name the cost outright:

> *"Slack does not send notifications when a message is edited, so the thread
> can look frozen while the list is still moving."*

Correct trade for a work assistant. **Wrong trade at 3am during an outage**,
where the entire point is that on-call gets pinged when a hypothesis is ruled
out. Append-per-milestone is right for us.

~~Their design also solves a rate-limit problem we hadn't priced: Slack allows
roughly 1 post/sec per channel and `poster.py` has no throttle.~~
**Struck 2026-07-20 — measured and wrong by ~48x.** The one scored run posts
**nine** milestones over seven minutes, one every 48 seconds. There is no rate
pressure, so the hybrid this note proposed has no cost argument behind it. See
P8 §1.

**Hands a concrete precedent to P6.**

| Layer | Their mechanism |
|---|---|
| Who | workspace-wide by default; restrictable to users with an account; Enterprise adds a role capability |
| Where | must be `/invite`d to a channel |
| Cost | **org-wide spend limit per billing period, plus optional per-channel limits** |
| At the ceiling | *"Work that would exceed a limit is declined rather than silently truncated."* A blocked user can request more from an admin in Slack. |

That last row answers P6's "behaviour at the ceiling" axis with a shipped
precedent: **refuse loudly, with an in-Slack escalation path.** Not queue, not
degrade to a cheaper model. The two-tier shape (global ceiling plus per-channel
ceiling) maps onto P6's observation that a per-user rate limit and a global
spend ceiling protect against different failures.

**Not answered by their docs:** transport (Socket Mode vs HTTP Events API),
rate limits, concurrency caps, and duplicate handling. The parts of P3 and P4
we spent the most effort on are exactly the parts they don't expose — no
shortcut available there.

Sources: [How Claude Tag works](https://claude.com/docs/claude-tag/concepts/how-it-works.md),
[Restrict where Claude operates](https://claude.com/docs/claude-tag/admins/restrict-access.md),
[Set a spend limit](https://claude.com/docs/claude-tag/admins/set-spend-limit.md),
[Commands](https://claude.com/docs/claude-tag/users/commands.md).

### Hermes Agent and Pinet — researched 2026-07-20

Both are **Socket Mode only**, so neither faces the 3-second deadline and
neither documents it. Hermes states the motivation outright: *"Slack uses
WebSockets instead of public HTTP endpoints, so your Hermes instance doesn't
need to be publicly accessible — it works behind firewalls, on your laptop, or
on a private server."* **That is the architecture we are leaving** (design-v2
review list, item 2). They are at our v1, not our v2. Read for the narrower
lessons only.

**Thread routing, confirmed again.** Hermes: *"Once the bot has an active
session in a thread, subsequent replies in that thread do not require
@mention."* Pinet: *"Thread ownership ensures continuity. Once an agent owns a
thread, it keeps receiving those messages."*

Both take a **stronger** form than P3 §2 — after the first tag, no mention is
needed at all. Better UX, with a concrete cost: Hermes' event subscriptions are
`message.channels`, `message.groups`, `message.im`, `message.mpim` *plus*
`app_mention`, meaning it receives **every message in every channel it is in**,
not just mentions. That widens ingress volume and privacy surface considerably.
A real trade for a later P3/P6 revision, not a free win. Not adopted now.

**A third narration option (P8 input).** Hermes uses Slack's **status line**
rather than messages — `is thinking...` by default, and `live_status: full`
shows the live action (*"is running pytest tests/…"*), which the docs say rides
the existing cadence with **no extra API calls**, sidestepping the rate limit
entirely. Same defect as Claude Tag's in-place edit for our case: ephemeral, no
notification, no history. But it means P8's choice is three-way — status line,
edited message, posted message — with different costs and different
notification behaviour.

**Pinet's inbox** is the only external gesture at durable-before-ack we found:
*"Messages are routed based on thread ownership, queued when agents are busy,
marked read when processed, preserved across restarts."* No implementation
detail, and under Socket Mode the motivation differs from ours.

**Incidental gotcha worth keeping:** Hermes ships a `!cmd` prefix specifically
because **Slack blocks native slash commands inside threads**. If P7's unbuilt
manual re-run ever becomes a command, it cannot be a slash command in the
thread where it would be used.

Sources: [Hermes Slack integration](https://hermes-agent.nousresearch.com/docs/user-guide/messaging/slack),
[@pinet/slack-bridge](https://pi.dev/packages/@pinet/slack-bridge).

### OpenHands — researched 2026-07-20, from source

The only fully-sourced reference architecture we found for HTTP ingress. Read
directly from `All-Hands-AI/OpenHands`
(`enterprise/server/routes/integration/slack.py` and siblings).

```
Slack POST → verify sig → challenge? → SETNX dedup(client_msg_id, 60s)
           → BackgroundTasks.add_task(...) → 200
                    ↓ (same process, in memory)
        authenticate → look up (channel_id, thread_ts) in Postgres
                     → new conversation (uuid4) + persist SlackConversation row
                     → persist EventCallback row
                     → post "I'm on it! [track progress here]" to thread
                    ↓ (minutes later, driven by the persisted callback)
                     → on execution_status == 'finished': post final answer
```

**Confirms three of our rulings, from code rather than docs.**

- **Session identity is `(channel_id, thread_ts)`, looked up in Postgres**
  (`slack_conversation_store.py:12-25`). A top-level mention with no `thread_ts`
  is *always* a new conversation (`slack_view.py:482-498`). That is P3 §2, and
  it confirms the pre-ack DB lookup we accepted as a cost is simply what this
  shape requires.
- **The conversation id is a fresh `uuid4`, not derived from the thread**
  (`slack_view.py:272`) — the DB row is the mapping. That is P4 §2: identity is
  a surrogate key, the human-facing string is a label.
- **Nothing is posted to Slack before the 200.** No placeholder, no reaction.
  First user-visible signal comes from the background task. That is P3 §1.

**Contradicts P3's durability rule, and the failure mode is instructive.** Their
ack is *not* durable. The Redis `SET NX EX` at `slack.py:316-321` stores a dedup
marker only — the payload lives in Python memory inside a FastAPI
`BackgroundTask`, same process, same event loop. If the process dies between the
200 and conversation creation, **the request is lost silently**, and because the
dedup key survives 60 seconds, a manual re-tag inside that window is swallowed
too. The user sees nothing at all.

They took that trade to avoid running a queue. We are not taking it, because our
trigger is an unattended 3am alert rather than a developer watching a thread —
which is exactly the durability asymmetry P3 §4 is built on. Worth recording
that the trade is *deliberate and shipped*, not an oversight, so we know what we
are paying the queue for.

**Directly relevant to P4's open idempotency item:** their key is Slack's
`client_msg_id`, not `event_id`, with a **60-second TTL**. Short enough that a
Slack retry arriving on the later backoff steps (minutes to an hour) would
re-execute. A second guard for button interactions uses a composite key
(`team_id:channel_id:message_ts:thread_ts`) at 300s, with the docstring:
*"Slack can deliver multiple button click payloads… Only the first interaction
for an original Slack message should start a conversation."* Both TTLs are
shorter than Slack's own retry window.

**Narration is a fourth model: link out.** Two messages, no edits, no reactions
— *"I'm on it! … [track my progress here]"* after the conversation is created,
then the final answer on `execution_status == 'finished'`
(`slack_v1_callback_processor.py:46-57`). Everything in between lives in their
web UI behind the link. Pre-start errors go out as **ephemeral** messages
visible only to the requester.

**Gating:** follow-ups in a thread are restricted to the original requester
(`slack_view.py:459-462`). There is otherwise **no rate limit, no concurrency
cap, and no backpressure** anywhere in the ingress — only a global
`SLACK_WEBHOOKS_ENABLED` kill switch. P6 input in both directions.

**Devin** (docs only, paraphrased — one page reachable): a mention creates a
session, each thread is a separate session, and it uses **emoji reactions** as a
cheap status channel for completed/failed. Inline keywords `sleep`, `mute`,
`EXIT`, `archive` control the session. Ack pattern, idempotency, and rate limits
are undocumented. **Cursor: nothing obtained.**

---

## Cross-cutting

### P12. Tests and CI

There are none. No `test_*.py` anywhere, no CI config, and nothing is
committed to git — `git log` is fatal on an empty `main`, so there is no
revert point. `pytest` and `ruff` are in the dev dependency group and unused.

Not a grill item so much as a standing requirement: each P decision above
should name what proves it works. Worth an early ruling on **what gets
committed and when**, independent of everything else.

### P13. Evals — deferred, tracked

`feedback.md` is created per incident (`procedure.md:112`) and nothing ever
reads it. There is no regression suite over past incidents, so a change to
`procedure.md` can't be shown to help or hurt. design-v2 D5 already defines
the promotion bar (a challenger beats baseline over ≥15 scored rows), which
means the mechanism is designed and unbuilt.

Explicitly deferred. Recorded here so it stays visible:
- [ ] Aggregate `feedback.md` verdicts into a scoreable set.
- [ ] Replay harness over past incidents.
- [ ] Wire the D5 promotion bar to real numbers.

---

## Carried forward from design-v2's review list

Still open, not superseded by anything above:

- [ ] Investigator model is `claude-sonnet-5` (implementation phase);
      switch to `claude-opus-4-8` at go-live or per eval verdict.
- [ ] Q&A answer voice: answers should speak about the incident, not about
      the record's plumbing.
- [ ] Q&A is a real LLM call even under `--dry-run`; only the investigator
      is mocked.
