# Session handoff — 2026-07-23 (second session: UX day + capture layer)

Continue building the hosted RCA agent in `/Users/mohitrawat/projects/ingren/prod/agents`.
Python, canonical tree, deployed in place. Mohit fully understands every line —
deliberately slower is correct.

## Read first, in this order

1. `docs/design.md` — **§8g is new** (channel-per-incident + its two
   amendment blocks), and **§8a-B carries a 2026-07-23 amendment**
   (capture built, resume still deferred). §5's S3 mirror section header
   changed to match.
2. `service/router.py` and `poller/main.py` docstrings — both reshaped
   this session; the docstrings are current.
3. `infra/RUNBOOK.md` — unchanged, still accurate on scripts and deploys.

## State (as of `09e647e`, pushed)

- **§8g channel-per-incident is live and end-to-end proven.** An alert
  tagged in the central channel gets its own public channel
  `rca-<incident uuid>`: alert copy as first message (poller narrates in
  its thread), tagger invited, one link-back in the origin thread.
  Routing is by channel: allowlist = alert, `incidents.channel` row =
  question, else drop. The upsert returns `(id, channel)`; the MOVE
  UPDATE is the setup commit point — crash before it re-runs setup
  (duplicate posts accepted, invariant 6), crash after it skips to the
  idempotent StartExecution. Slack scope `channels:manage` added.
  Live incident `37f6f724` exercised the whole flow.
- **Narration UX (all deployed):** purpose-carrying tool_calls post as
  "🔎 Querying: <purpose>" (D6 relaxed for the incident channel only);
  emoji/bold on every line; each message is a section+divider Block Kit
  pair (>3000 chars falls back to plain); terminal message drops the
  dollar amount (cost stays in the record).
- **rca.md renders as the channel canvas** on doc_ready: H1 title
  prepended unless the doc has one, `channel_canvas_already_exists` =
  free idempotency, best-effort so the cursor never wedges. Scope
  `canvases:write` added. Migration 006: poller SELECT on documents.
- **Q&A draws ASCII charts** (bars/sparklines) and ASCII timelines on
  request — prompt rules only, tool surface untouched (§8a-C's "no
  charts" meant chart FILES; recorded in §8g amendment). Q&A context:
  every question is an independent agent run; nothing but cost numbers
  is stored — the Slack thread is the only Q&A record.
- **§8a-B capture layer is built** (`09e647e`): the investigator mirrors
  its transcript to S3 when `RCA_SESSION_BUCKET` is set.
  `investigator/session_store.py` — append/load only, P2 §5 part-object
  shape, uuid dedup, direct-children listing; passes the SDK's own
  conformance suite. run.py stamps the session id into an
  `instrument_note` event (the resume handle) and records
  `mirror_error` drops. Bucket `rca-sessions-<acct>`: private, 90-day
  lifecycle is the ONLY deletion path. **Resume/steering deliberately
  NOT built** — `resume=` never passed, retries still restart from
  zero; §8a-B's named triggers still gate.
- **Capture infra is provisioned and verified** (2026-07-23, end of
  session): bucket exists, `mirror-sessions` policy on rca-investigator,
  task def rev 8 carries `RCA_SESSION_BUCKET`, image pushed. The capture
  live-check (`aws s3 ls` on the sessions prefix + the instrument_note
  SELECT) runs on the next real investigation.
- Tests: 59 green without DB env; `test_tools_db` + `test_schema` need
  `.env` DSNs (schema suite gained poller-reads-documents).
- Issues: #13 (liveness) and #15 (tracker) still open — untouched today.

## Next build: #13 (liveness), then #15 closes

Unchanged from last handoff: ping after the routing lookup, Synthetics
canary, alarm → SNS (never Slack — P10), per-queue DLQ alarms (§8f).
Plus riding along: delete `daemon.py` (doubly stale), consider
`max_budget_usd` on Q&A. New candidates from today: Batch B live-tests
re-run under §8g routing (the §8g section lists a new kill-test:
router killed between create and MOVE), and the §8g "already
investigating" pointer's origin-lookup only matches while `raw` keeps
origin values — fine today, worth a test if raw's shape ever moves.

## Gotchas learned this session

- **Deploy overlap consumes the queue twice.** During a rolling deploy
  the draining task still polls SQS for ~5 min; a question routed there
  hit old code and was silently dropped. One-time per deploy; wait for
  rollout COMPLETED before live-testing.
- **Channel-canvas API has no title field** — the markdown H1 IS the
  title. One canvas per channel; duplicate create fails
  `channel_canvas_already_exists` (usable as idempotency).
- **Slack section blocks cap at 3000 chars** vs 40k plain text — any
  Block Kit switch needs a plain-text fallback for long lines.
- **The Python SDK now ships SessionStore natively** (0.2.122:
  Protocol, `session_store` option, conformance suite in
  `claude_agent_sdk.testing`). §8a-B's "adapter is a build, not a copy"
  is amended — the build was ~100 lines.
- **The conformance suite caught a real bug**: a main transcript's S3
  prefix is a parent of subpath prefixes; list direct children only.
- **No pytest-asyncio in the venv** — async tests wrap bodies in
  `asyncio.run()` inside sync defs (see `test_session_store.py`).
- **Cursor replay is the free UI test:** reset `terminal_posted_at` +
  `narrated_through` on a closed incident and the poller re-narrates
  with current formatting (canvas hits the already-exists skip).

## Working rules (unchanged, CLAUDE.md)

- One file or coherent unit at a time, walked through in chat. Pause for
  questions. No batch code drops.
- DB writes, migrations, AWS mutations: **Mohit runs them**, or grants
  per-command permission in-session.
- Never read `.env`.
- Small verify first; flag runs >2 min, background them.
- On a decent design question, ask "what's your guess?" before framing
  options. (§8g's channel-name idempotency came from Mohit's own
  framing — it worked again.)
- Live-test batching: paid/slow checks land in `docs/live-tests.md`.

## Register

**ASD-STE100 for technical chat** — enforced by a `UserPromptSubmit` hook.
Short sentences, one fact each, active voice, lists over paragraphs.

---

Start by reading §8g and the §8a-B amendment, confirm state
(`git log --oneline -8`; 59 tests from `rca/` without DB env), confirm
whether the capture-layer provision steps ran, then begin #13.
