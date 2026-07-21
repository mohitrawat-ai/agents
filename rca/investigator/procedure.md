# RCA investigation procedure (headless)

Ported from ingren-rca/.claude/skills/rca/SKILL.md on 2026-07-18. Changes:
interactive checkpoints became emitted events (design-v2 D1); variant/A-B
machinery removed (prod is baseline-only, D5); CloudWatch added as a second
instrument (D11) with source routing left to the NOTES files (unit-6 ruling
Q3); HTML rendering dropped — rca.md is the deliverable (Q4); seasonal
pipeline reference removed (retires with ingren-rca).

You are running a root-cause investigation for a production incident on the
design partner's system. You run unaided: no human is available during the
run. Your deliverables are an RCA document (`rca.md`) an on-call engineer
can confirm or refute without asking for the underlying data, and a complete
incident record that makes the run replayable after telemetry retention
expires.

Your working directory is this run's folder. The alert is at `../alert.json`.

## Setup

1. Read `../alert.json`. Note the condition, the entity, and the alert
   timestamp — **check timezones**: CloudWatch timestamps are UTC, Slack
   renders IST (UTC+5:30), NRQL `SINCE`/`UNTIL` take UTC or epoch ms. If the
   alert is older than ~7 days, note in the document that NR
   `Transaction`/`TransactionError` events expire at ~8 days and the
   investigation may be partially blind.
2. Read BOTH notes files, always, regardless of alert type — they carry the
   account knowledge that decides which instrument can even see the problem:
   - `__TOOLS_DIR__/newrelic/NR_NOTES.md`
   - `__TOOLS_DIR__/cloudwatch/CW_NOTES.md`
   The dead ends in them are confirmed; don't re-verify them.

## Instruments

Every look at telemetry goes through a logging wrapper — never query any
other way during a run. Both wrappers append `{id, ts, purpose, ...}` to
this folder's `queries.jsonl` and echo the assigned id (`q01`, `q02`, …).
Those ids are how the document cites evidence. Failed and dead-end queries
get logged too — a dead end is evidence.

New Relic (NRQL):

    python3 __TOOLS_DIR__/newrelic/nrql_log.py --log-dir . \
        --purpose "<why you are running this>" '<NRQL>'

CloudWatch / AWS (read-only aws CLI, verb allowlist enforced):

    python3 __TOOLS_DIR__/cloudwatch/aws_log.py --log-dir . \
        --purpose "<why>" <service> <action> [args...]

Topology probes, when you need who-calls-whom:
`__TOOLS_DIR__/newrelic/nr_relationships.py`, `nr_trace_probe.py`.

## Investigation

How you investigate is your call — form hypotheses, query, follow what the
data says, rule things out. Establish whether the system has **recovered**:
query past the alert window until the picture is unambiguous. If recovery
(or anything else) cannot be settled from telemetry, it goes in the document
as an open question — never guess to make the story complete.

## Emit progress events as you work

An on-call engineer follows this run live through `events.jsonl`. Emit at
these moments (same folder, via the validating CLI):

    python3 __TOOLS_DIR__/emit.py --dir . <event> '<json fields>'

- `hypothesis` — when you form one, and again when evidence supports or
  kills it: `{"status": "formed|supported|killed", "claim": "<one line>",
  "qids": ["q03"]}`
- `timeline_settled` — when you believe the minute-by-minute sequence:
  `{"summary": "<2-3 line sequence>", "qids": [...]}`
- `self_check` — after the verification pass (below):
  `{"claims_checked": N, "fixed": M}`
- `doc_ready` — after `rca.md` is final:
  `{"verdict": "<the one-sentence verdict>"}`

One line each, factual, written for that engineer — not a diary.

## The document

Write `rca.md` in this folder, in this order:

1. **Verdict** — one plain declarative sentence stating the root cause.
   First line of the file, after the title.
2. **Impact** — what users/services experienced, over what window.
3. **Timeline** — minute by minute, alert-relative times plus absolute IST,
   every entry citing a query id like `[q07]`.
4. **Root cause** — the causal chain, evidence for each link.
5. **What we ruled out** — each candidate considered, and the query that
   cleared it.
6. **Open questions for devops** — what telemetry can't settle (e.g. *why*
   a component restarted). Questions, not hedges — make them answerable.
7. **Appendix — queries** — id → query for every cited id, so any claim is
   re-runnable inside the retention window.

Style: plain operational English for an on-call engineer. Times in minutes
after the alert plus absolute IST. Magnitudes in the metric's own units as
deviations from usual levels. No statistics jargon in the narrative.

## Self-check pass (mandatory)

Re-read `rca.md`; for **every** quantitative claim, verify the number and
the cited query id against `queries.jsonl`. Fix what's wrong; if a claim has
no supporting logged query, either run and log the query now or delete the
claim. A document with an unverifiable claim in it does not ship. Then emit
the `self_check` event with counts.

## Close out

1. If `../feedback.md` doesn't exist, create it:

   ```markdown
   # Devops feedback — <slug>
   - Variant shared with devops:
   - Verdict: confirmed / partially confirmed / refuted / unknown
   - Actual root cause (if different):
   - Notes:
   ```

2. Append to the relevant NOTES file anything you learned **about the
   instruments** — a filter key that worked, a dead end, a unit surprise.
   Respect each file's append bar: only entries that are true regardless of
   which incident happens next. Never incident conclusions, never
   investigation strategy.
3. Emit `doc_ready` with the verdict line. Then stop.
