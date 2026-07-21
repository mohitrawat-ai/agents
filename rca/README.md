# rca-agent — operations

Headless RCA agent: a Slack tag on an alert becomes an investigation run
that writes an evidence-backed `rca.md`. Design record:
`ingren-rca/docs/plans/rca-harness/design-v2.md` (moves here when ingren-rca
retires).

## Prerequisites

`rca/.env` (never committed) must contain:

```
NEW_RELIC_REGION / NEW_RELIC_ACCOUNT_ID / NEW_RELIC_API_KEY
ANTHROPIC_API_KEY
SLACK_BOT_TOKEN=xoxb-...     # bot token (OAuth & Permissions page)
SLACK_APP_TOKEN=xapp-...     # app-level token (Socket Mode)
```

AWS access uses CLI profile `hb-role` (see `tools/cloudwatch/CW_NOTES.md`).

## The daemon (Slack listener)

```bash
cd prod/agents/rca

uv run python daemon.py              # live: tags spawn real (LLM) investigations
uv run python daemon.py --dry-run    # mock: same pipeline, no LLM, no cost

# background it:
nohup uv run python daemon.py > daemon.log 2>&1 &

# is it running?
pgrep -fl daemon.py

# stop it:
pkill -f daemon.py
```

Notes:
- Runs in the foreground; Ctrl-C stops it. It must stay running (and the
  Mac awake) to receive tags — Socket Mode reconnects by itself after
  network blips, but a dead process misses everything (in-memory state:
  restart forgets active runs and dedup history).
- Usage from Slack: reply `@ingren_alerts` in an alert message's thread
  (parent message = the alert), or tag with pasted alert text. Dedup: the
  same alert within 30 min gets pointed at the existing run. One run at a
  time; extra tags are refused with a message.

## Manual investigator runs (no Slack)

```bash
# incident dir must contain alert.json
uv run python investigator/run.py --incident-dir <dir>            # real run
uv run python investigator/run.py --incident-dir <dir> --mock     # plumbing test
# knobs: --variant principled  --model claude-opus-4-8  --max-turns 150  --max-minutes 60
```

Model default is `claude-sonnet-5` during the implementation phase
(design-v2 "review before production": switch to `claude-opus-4-8` at
go-live).

## Where things land

```
prod/data/newrelic/incidents/<slug>/
  alert.json            # verbatim alert (daemon- or hand-written)
  feedback.md           # devops verdict slot (created by real runs)
  baseline/             # one folder per variant run
    queries.jsonl       # every telemetry look, receipts with qids
    events.jsonl        # tool calls + milestones + run lifecycle
    rca.md              # the document
```

A run costs roughly $2 / 5-10 min on Sonnet (first real case: 52 turns,
$2.03). `--mock` costs nothing.

## Layout

```
daemon.py               # Slack Socket Mode listener -> spawns runs
investigator/           # run.py (SDK entry), hooks.py, procedure.md
slackbot/               # parse_alert.py, poster.py (thread narration)
tools/newrelic/         # NRQL CLIs + NR_NOTES.md
tools/cloudwatch/       # aws_log.py + CW_NOTES.md
tools/emit.py           # semantic-event CLI (agent milestones)
```
