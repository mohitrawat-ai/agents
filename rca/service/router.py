"""Router: consume `inbound`, route by thread, upsert + StartExecution.

New code, 2026-07-22 (issue #11). Replaces daemon.py's on_mention handler;
the thread-anchor and alert-text logic carries over from daemon.py
(2026-07-18). Runs alongside ingress in the Service task.

Routing is by thread, never by message content (#11): the first tag in a
thread is an alert; a tag in a thread we already know is a question. The
§8a-A conditions all live here and each is load-bearing:

1. The upsert is `DO UPDATE ... RETURNING id` — always one row back, so
   a crash-then-redelivery converges instead of dropping an alert.
2. The execution input is `{incident_id}` and nothing else, and the
   execution name is the incident id — `StartExecution` is idempotent
   only on identical input.
3. Nothing non-idempotent sits between the upsert and `StartExecution`.
   The happy alert path makes NO Slack post — the ack is the poller's.

The router does post in four cases, each creating no incident: a tag
with no findable alert text, the rate-limit refusal (ruled in-thread,
#11), the empty-question guidance, and the "Looking at the record…" ack
before a question is enqueued (#12, §8f). The router never answers a
question itself: a Q&A call runs 30-300s, which would block the alert
path and outlive inbound's 60s visibility timeout — it acks, enqueues on
`rca-qa`, and returns. qa/worker.py answers.

A failed message is never deleted: it redelivers, and after
maxReceiveCount lands in the DLQ (queue config, provision.sh). Errors
log in full, never swallowed.

incidents.raw is the investigator's alert.json verbatim (run.py
materializes it), so it keeps the laptop-proven shape — source, channel,
thread_ts, condition_guess, received_utc, raw text — plus `envelope`,
the untouched Slack event callback (§8a-D: the sample nobody has, and
the backfill source if dedup is ever built).

Env (task definition, #11): RCA_DATABASE_URL (role rca_service),
SLACK_BOT_TOKEN, RCA_INBOUND_QUEUE_URL, RCA_QA_QUEUE_URL,
RCA_STATE_MACHINE_ARN, RCA_CHANNEL_ALLOWLIST (comma-separated
channel ids).

Run: python -m service.router
"""

import json
import os
import sys
import time
import traceback
from datetime import UTC, datetime
from pathlib import Path

import boto3
import psycopg
from botocore.exceptions import ClientError
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from slack_sdk import WebClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from slackbot.parse_alert import parse_alert

RATE_LIMIT = 5  # investigations per window, global — a backstop, not policy
RATE_WINDOW = "10 minutes"

LOOKUP = (
    "SELECT id, event_id FROM incidents"
    " WHERE channel = %s AND thread_ts = %s LIMIT 1"
)

# §8a-A condition 1: DO UPDATE, not DO NOTHING — always returns the id,
# including when a concurrent insert or a redelivery already holds event_id.
UPSERT = """\
INSERT INTO incidents (event_id, channel, thread_ts, slug, raw, received_utc)
VALUES (%s, %s, %s, %s, %s, %s)
ON CONFLICT (event_id) DO UPDATE SET event_id = EXCLUDED.event_id
RETURNING id
"""

RECENT = (
    "SELECT count(*) AS n FROM incidents"
    " WHERE created_at > now() - interval '" + RATE_WINDOW + "'"
)


def _env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"{name} is not set; the task environment must provide it")
    return value


def _post(slack: WebClient, channel: str, thread_ts: str, text: str) -> None:
    slack.chat_postMessage(channel=channel, thread_ts=thread_ts, text=text)


def alert_text_for(event: dict, slack: WebClient) -> str:
    """A threaded mention's alert is the thread's parent message; a bare
    mention must carry the alert in its own text (minus the tag). Carried
    over from daemon.py."""
    thread_ts = event.get("thread_ts")
    if thread_ts and thread_ts != event["ts"]:
        parent = slack.conversations_replies(
            channel=event["channel"], ts=thread_ts, limit=1
        )["messages"][0]
        return parent.get("text", "")
    own = event.get("text", "")
    return " ".join(w for w in own.split() if not w.startswith("<@")).strip()


def start_investigation(sfn, cfg: dict, incident_id: str) -> None:
    """The idempotent start, shared by the fresh-alert path and the
    redelivery path. Name = incident id, input = {incident_id}: a repeat
    with identical input is a no-op, never an error (§8a-A condition 2)."""
    try:
        sfn.start_execution(
            stateMachineArn=cfg["sm_arn"],
            name=incident_id,
            input=json.dumps({"incident_id": incident_id}),
        )
    except ClientError as e:
        if e.response["Error"]["Code"] != "ExecutionAlreadyExists":
            raise
    print(f"[router] investigation started: {incident_id}")


def enqueue_question(
    sqs, cfg: dict, slack: WebClient, incident_id: str, event: dict, event_id: str
) -> None:
    """The question path (#12, §8f): ack, enqueue on `rca-qa`, return.
    FIFO dedup on the Slack event_id makes the enqueue idempotent; the
    ack may double-post across a crash (§8f accepted cost). An empty
    question gets guidance instead — nothing to enqueue."""
    anchor = event.get("thread_ts") or event["ts"]
    question = " ".join(
        w for w in event.get("text", "").split() if not w.startswith("<@")
    ).strip()
    if not question:
        _post(
            slack,
            event["channel"],
            anchor,
            'Ask me something about this incident — e.g. "what did q04 '
            'show?" or "what was the verdict?".',
        )
        return
    _post(slack, event["channel"], anchor, "Looking at the record…")
    sqs.send_message(
        QueueUrl=cfg["qa_queue_url"],
        MessageBody=json.dumps(
            {
                "incident_id": incident_id,
                "channel": event["channel"],
                "thread_ts": anchor,
                "question": question,
                "event_id": event_id,
            }
        ),
        MessageGroupId=incident_id,
        MessageDeduplicationId=event_id,
    )
    print(f"[router] question enqueued: {incident_id}")


def handle(
    conn: psycopg.Connection,
    slack: WebClient,
    sfn,
    sqs,
    cfg: dict,
    body: dict,
    answer=enqueue_question,
) -> None:
    """Route one envelope. Raising leaves the message on the queue for
    redelivery; returning normally lets the caller delete it."""
    event = body["event"]
    channel = event["channel"]
    if channel not in cfg["allowlist"]:
        print(
            f"[router] dropped: channel {channel} is not allowlisted "
            f"(event {body.get('event_id')})",
            file=sys.stderr,
        )
        return
    anchor = event.get("thread_ts") or event["ts"]

    known = conn.execute(LOOKUP, (channel, anchor)).fetchone()
    if known and known["event_id"] != body.get("event_id"):
        answer(sqs, cfg, slack, str(known["id"]), event, body.get("event_id"))
        return
    if known:
        # Same event_id: this is the ALERT redelivering, not a question
        # (review 2026-07-22, critical). The routing key (channel,
        # thread_ts) and the idempotency key (event_id) are different
        # keys — without this gate, a crash or a throttled StartExecution
        # after the upsert commits meant every redelivery matched the
        # incident's own row, got a Q&A stub, and the run never started.
        # Converge instead: re-drive the idempotent start (§8a-A cond 1).
        start_investigation(sfn, cfg, str(known["id"]))
        return

    text = alert_text_for(event, slack)
    if not text:
        _post(
            slack,
            channel,
            anchor,
            "Tag me on an alert message (in its thread), or paste the alert "
            "text with the tag.",
        )
        return

    if conn.execute(RECENT).fetchone()["n"] >= RATE_LIMIT:
        _post(
            slack,
            channel,
            anchor,
            f"Rate limit: {RATE_LIMIT} investigations in {RATE_WINDOW} reached "
            f"— refusing this one. This is a runaway backstop; tag me again "
            f"in a few minutes.",
        )
        return

    received = datetime.fromtimestamp(float(event["ts"]), UTC)
    parsed = parse_alert(text, received_utc=received)
    alert = {
        "source": "slack-tag",
        "channel": channel,
        "thread_ts": anchor,
        "condition_guess": parsed["condition_guess"],
        "received_utc": parsed["received_utc"],
        "raw": parsed["raw"],
        "envelope": body,
    }
    incident_id = str(
        conn.execute(
            UPSERT,
            (
                body["event_id"],
                channel,
                anchor,
                parsed["slug"],
                Jsonb(alert),
                received,
            ),
        ).fetchone()["id"]
    )

    start_investigation(sfn, cfg, incident_id)


def main() -> int:
    dsn = _env("RCA_DATABASE_URL")
    token = _env("SLACK_BOT_TOKEN")
    queue_url = _env("RCA_INBOUND_QUEUE_URL")
    cfg = {
        "sm_arn": _env("RCA_STATE_MACHINE_ARN"),
        "qa_queue_url": _env("RCA_QA_QUEUE_URL"),
        "allowlist": {
            c.strip()
            for c in _env("RCA_CHANNEL_ALLOWLIST").split(",")
            if c.strip()
        },
    }
    region = cfg["sm_arn"].split(":")[3]
    slack = WebClient(token=token)
    sqs = boto3.client("sqs", region_name=region)
    sfn = boto3.client("stepfunctions", region_name=region)

    print(f"router up — queue {queue_url}, allowlist {sorted(cfg['allowlist'])}")
    while True:
        try:
            msgs = sqs.receive_message(
                QueueUrl=queue_url, MaxNumberOfMessages=10, WaitTimeSeconds=20
            ).get("Messages", [])
            if not msgs:
                continue
            with psycopg.connect(
                dsn, autocommit=True, connect_timeout=10, row_factory=dict_row
            ) as conn:
                for msg in msgs:
                    try:
                        handle(conn, slack, sfn, sqs, cfg, json.loads(msg["Body"]))
                        sqs.delete_message(
                            QueueUrl=queue_url, ReceiptHandle=msg["ReceiptHandle"]
                        )
                    except Exception:  # noqa: BLE001 — redelivers, then DLQ
                        print(
                            "[router] message failed, will redeliver:",
                            file=sys.stderr,
                        )
                        traceback.print_exc()
        except Exception:  # noqa: BLE001 — a bad poll is loud, the loop lives
            print("[router] receive failed:", file=sys.stderr)
            traceback.print_exc()
            time.sleep(5)


if __name__ == "__main__":
    sys.exit(main())
