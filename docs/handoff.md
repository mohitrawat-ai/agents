# Session handoff — 2026-07-22 (night)

Continue building the hosted RCA agent in `/Users/mohitrawat/projects/ingren/prod/agents`.
Python, canonical tree, deployed in place. Mohit fully understands every line —
deliberately slower is correct.

## Read first, in this order

1. `docs/design.md` — §8a–§8e amend/reverse everything
2. `docs/issues.md` — #10 and #11 progress blocks (both built + reviewed),
   then #12 in full (it is next)
3. `docs/live-tests.md` — the deferred-verification ledger and what passed
4. `infra/provision.sh` + `infra/RUNBOOK.md` — everything live in AWS

## State (as of this session's commits)

- **Closed:** #1–#2, #4–#11, #16. **#10 and #11 closed 2026-07-22 —
  the live-test ledger is clear, all of A1–A5 and B1–B5 passed.**
  Open: #12, #13, #15 (tracker).
- **THE SYSTEM IS LIVE, END TO END.** `rca.ingren.ai` → ALB → ingress →
  SQS → router → Step Functions → investigator → Postgres → poller →
  thread. First fully-hosted tag ran 2026-07-22: incident `35c6e40f`,
  658s / 86 turns / $2.37 / 27 queries, correct verdict, terminal message
  posted. The laptop daemon is dead (`pkill`ed, Socket Mode off).
- **Live in AWS** (ap-south-1, `537124933640`, profile `ingren`): all of
  #9's resources, plus poller Service (1 task), `rca-inbound` + DLQ
  (60s × 5 ≈ 5 min to DLQ), `rca-service` Service (2 tasks: ingress on
  8000 + router, per-container secrets, router `essential:false`), ALB
  `rca` + 443 listener, ACM cert for `rca.ingren.ai` (Route 53 in-account,
  zone `Z0233500389POHCI4O3MM`).
- **Schema:** migration 004 added `incidents.terminal_posted_at` (poller's
  completion marker; P9 §5 amended — poller updates only columns recording
  its own acts). Existing incidents were backfilled closed before first
  poller boot.
- 76 tests green (`uv run --env-file .env pytest -q` from `rca/`).
- Both slices carry three-Opus review records in their issue blocks. The
  #11 critical find (redelivered alert routed as a question — routing key
  vs idempotency key) is fixed with an `event_id` gate + pinning test.

## Live checks — ALL PASSED 2026-07-22

`docs/live-tests.md` holds the evidence per check. A4/B4/B5 ran free;
A3/B3 shared one paid run (incident `80d7822d`, ~$3.50 — attempt 1
killed mid-run, attempt 2 SUCCEEDED at 76 turns / $2.35 / 812s).
Notables:

- B3's mid-processing kill is not hand-timeable; the redelivery half was
  forced by redriving the saved envelope (B2 pattern) — the `event_id`
  gate converged it. Ledger records the method.
- B4's tag went in the KNOWN incident thread so the post-restore
  redelivery routed as a free question, not a paid run.
- B5 seeds carried `terminal_posted_at` at insert — the poller never
  sees them, so no deletion deadline. Rows since deleted.
- `rca-service` runs desired-count **1**, not 2 as the previous handoff
  said. Worth a deliberate ruling when #13 lands.
- Poller's never-started close is silent in logs by design — evidence
  is the Slack post + `terminal_posted_at`, not a log line.

## Next build: #12 (Q&A), then #13

- **#12 Q&A:** the router's `answer` seam (`service/router.py`,
  `answer_stub`) is the insertion point. `rca.md` inlined in the prompt,
  `read_record.py` as the only executable, no `Read`/`Write`, incident id
  from the environment, timeout posts, `cost_usd` recorded. Read §8a-C
  first.
- **#13 liveness:** ping after the thread lookup, Synthetics canary,
  alarm → SNS (never Slack). The DLQ alarm story also lands here — the
  per-type message attribute was removed in review as undeliverable at
  ingress.
- **#15** stays open as the provisioning tracker.

## Gotchas learned this session

- **Slack Socket Mode toggle:** must be OFF or events go to the dead
  socket; deleting the app token is not enough. Cost us the first tag.
- **ECS + ALB ordering:** CreateService fails on a target group no
  listener references — provision.sh gates Service creation on the cert.
- **Bolt calls `auth.test` per dispatch** unless given a static
  `authorize`; that was a Slack API call in front of the ack.
- **Date bug:** mid-session docs were written "2026-07-23"; today is
  2026-07-22. Fixed by sed; watch for stragglers in review.

## Working rules (unchanged, CLAUDE.md)

- One file or coherent unit at a time, walked through in chat. Pause for
  questions. No batch code drops.
- DB writes, migrations, AWS mutations: **Mohit runs them**, or grants
  per-command permission in-session (this session's pattern: Claude ran
  approved commands stepwise and watched read-only the rest of the time).
- Never read `.env` (append-blind with `>>` was ruled fine).
- Small verify first; flag runs >2 min, background them.
- Code review via Opus subagents for non-trivial diffs; record accepted
  findings in the issue.
- On a decent design question, ask "what's your guess?" before framing
  options.
- **Live-test batching (ruled 2026-07-22):** paid/slow acceptance checks
  land in `docs/live-tests.md` and batch across issues; free checks run
  with their issue.

## Register

**ASD-STE100 for technical chat** — enforced by a `UserPromptSubmit` hook.
Short sentences, one fact each, active voice, lists over paragraphs.

---

Start by reading the docs above, confirm state (`git log --oneline -5`,
76 tests from `rca/`), then begin #12 at the `answer` seam — read
design.md §8a-C first. #13 follows; #15's remaining boxes (idempotent
re-run of the provision scripts, account audit) close at #13.
