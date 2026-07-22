# Session handoff — 2026-07-23

Continue building the hosted RCA agent in `/Users/mohitrawat/projects/ingren/prod/agents`.
Python, canonical tree, deployed in place. Mohit fully understands every line —
deliberately slower is correct.

## Read first, in this order

1. `docs/design.md` — §8a–§8f amend/reverse everything. **§8f is new**
   (Q&A async off `rca-qa.fifo`, plus its 2026-07-23 review amendments)
2. GitHub issue #12 — closed, but its closing comment holds the
   three-Opus review record and the two minors that stayed open
3. `docs/live-tests.md` — Batch C (Q&A) passed 2026-07-23
4. `infra/` — provision.sh is SPLIT: `provision-foundation.sh` (free,
   static), `provision-definitions.sh` (free, hot), `services.sh`
   (up|down|status — the cost axis), `lib.sh` (shared). Plus `RUNBOOK.md`

## State (as of this session's commits)

- **Closed:** #1–#2, #4–#12, #16. Open: #13, #15 (tracker).
- **Q&A is live, end to end** (#12, ruled §8f). The flow:
  known-thread tag → router acks "Looking at the record…" + enqueues on
  `rca-qa.fifo` (dedup id = Slack event_id, group id = incident id) →
  `qa/worker.py` (third container in `rca-service`, `essential:false`)
  consumes one at a time → `qa/agent.py` answers (sonnet-5, 20 turns,
  `rca.md` inlined from `documents`, evidence via `read_record.py` only,
  Bash-allowlisted to that one script) → `qa_answered` event row
  (cost_usd, migration 005) → answer posted with qid citations.
- **Live check C1 passed:** 32s, 7 turns, $0.2166, correct answer, real
  qid. Bonus: the live agent tried command substitution, `>`/`>&`, and
  `find`; the PreToolUse boundary denied all four in the worker log.
  §8a-C's tool boundary is production-demonstrated.
- **Env-merge gotcha fixed and pinned:** the SDK merges `options.env`
  ONTO `os.environ` — omission removes nothing. SLACK_* vars are
  overridden to `""` on the Q&A subprocess (`subprocess_env`,
  `tests/test_qa.py`).
- 82 tests green (`uv run --env-file .env pytest -q` from `rca/`).
- Task def `rca-service` is at revision 3 (3 containers); the image
  includes `COPY qa/ qa/` (its absence broke the first boot — the
  Dockerfile COPY list is an allowlist, add a line per new task).

## Next build: #13 (liveness), then #15 closes

- **#13 liveness:** ping after the thread lookup, Synthetics canary,
  alarm → SNS (never Slack). The DLQ alarm story lands here — and it got
  BETTER: `rca-qa-dlq.fifo` exists now (§8f), so alert-path and
  question-path failures alarm per queue, no message attribute needed.
- **Deliberate ruling due at #13:** `rca-service` desired count.
  services.sh runs 1 (single tester); design §3 and services.sh comments
  say restore 2 at go-live. §8f amendment 1 already accepts the
  consequence: the Q&A storm bound is per-incident + task count.
- **#15** stays open as the provisioning tracker; its remaining boxes
  (idempotent re-run of the provision scripts, account audit) close at #13.
- **Small cleanups riding along:** delete `daemon.py` (doubly stale — it
  calls the CLI surface the #12 rewrite removed); consider
  `max_budget_usd` on Q&A (P6 §5b, currently bounded by max_turns 20 +
  the 300s worker timeout).

## Gotchas learned this session

- **SDK env is merge, not replace** (`subprocess_cli.py`:
  `{**inherited_env, **options.env}`). To strip a secret from the agent
  subprocess, override it to `""`; omitting the key silently inherits it.
- **FIFO group lock is queue-side only.** No consumer identity exists;
  any worker may take a group's next message once the previous deletes.
  One group = strict serial; group id choice = blast-radius choice.
- **`sqs create-queue` is idempotent only for identical attributes.**
  Changing an attribute (e.g. the load-bearing VisibilityTimeout 360 >
  300s answer timeout) on re-run FAILS — delete and recreate instead.
- **Roll command differs by what changed:** task-def edit needs
  `update-service --task-definition rca-service`; image-only push needs
  just `--force-new-deployment` (service already pins the revision).
- **zsh eats `===` as a glob** in chained echo separators; use `---`.

## Working rules (unchanged, CLAUDE.md)

- One file or coherent unit at a time, walked through in chat. Pause for
  questions. No batch code drops.
- DB writes, migrations, AWS mutations: **Mohit runs them**, or grants
  per-command permission in-session (this session: stepwise approval,
  Claude executed and reported each step).
- Never read `.env`.
- Small verify first; flag runs >2 min, background them.
- Code review via Opus subagents for non-trivial diffs; record accepted
  findings in the issue. #12's pattern: three lenses (correctness,
  security, conformance), findings verified against code before
  reporting, rulings recorded as §8f amendments.
- On a decent design question, ask "what's your guess?" before framing
  options. (#12's queue shape came from Mohit's own framing — it worked.)
- **Live-test batching:** paid/slow acceptance checks land in
  `docs/live-tests.md` and batch across issues; free checks run with
  their issue.

## Register

**ASD-STE100 for technical chat** — enforced by a `UserPromptSubmit` hook.
Short sentences, one fact each, active voice, lists over paragraphs.

---

Start by reading the docs above, confirm state (`git log --oneline -5`,
82 tests from `rca/`), then begin #13. Read P10 in `decision.md` first —
the alarm-never-Slack rule is its ruling — and §8f for the per-queue DLQ
story.
