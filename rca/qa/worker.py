"""qa-worker: consume `rca-qa`, answer from the record, post to the thread.

New code, 2026-07-22 (issue #12, §8f). Runs as the third container in the
rca-service task, `essential:false` like the router. One message at a
time, on purpose: this loop's synchrony IS P6's Q&A storm bound (§8f) —
a storm queues behind one paid call at a time instead of fanning out.

Per message: run qa/agent.py under P8 §7's 300-second timeout, record a
`qa_answered` event (cost_usd — the half of the daily spend P6 §6 found
missing), post the answer, delete. A timeout posts a dead end the user
can act on, then deletes — redelivering a timed-out question would pay
the 300 seconds again for the same result. Any other failure raises: the
message stays, redelivers after the queue's 360s visibility timeout, and
lands in rca-qa-dlq after maxReceiveCount (provision-foundation.sh). The
final receive posts a dead end to the thread before the DLQ move — the
asker must never be left on the ack forever (invariant 6, ruled
2026-07-23).

The cost row is best-effort, like run.py's terminal events: the answer
reaching the user outranks the accounting row, and the daily total is an
alarm, never a stop (P6 §2).

Env (task definition): RCA_DATABASE_URL (role rca_service),
SLACK_BOT_TOKEN, RCA_QA_QUEUE_URL.

Run: python -m qa.worker
"""

import asyncio
import json
import os
import sys
import time
import traceback

import boto3
import psycopg
from psycopg.types.json import Jsonb
from slack_sdk import WebClient

from qa import agent

QA_TIMEOUT_S = 300  # P8 §7

# must match the RedrivePolicy maxReceiveCount on rca-qa.fifo
# (provision-foundation.sh): the receive that hits this number is the last
# one before SQS moves the message to the DLQ.
MAX_RECEIVES = 3

TIMEOUT_TEXT = (
    "That took too long and I stopped. Ask something narrower — "
    "one query id, one number, one time window."
)

FAILED_TEXT = (
    "I couldn't answer this — it failed on every attempt and I've stopped "
    "trying. The failure is in the worker logs. Re-ask to try again."
)

_INSERT_EVENT = (
    "INSERT INTO events (incident_id, attempt, event, payload)"
    " VALUES (%s, %s, %s, %s)"
)


def _env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"{name} is not set; the task environment must provide it")
    return value


def record_cost(
    conn: psycopg.Connection, incident_id: str, out: agent.Answer, elapsed_s: int
) -> None:
    try:
        conn.execute(
            _INSERT_EVENT,
            (
                incident_id,
                out.attempt,
                "qa_answered",
                Jsonb(
                    {
                        "cost_usd": out.cost_usd,
                        "turns": out.turns,
                        "elapsed_s": elapsed_s,
                    }
                ),
            ),
        )
    except Exception as exc:  # noqa: BLE001 — the answer outranks the accounting
        print(f"[qa-worker] qa_answered insert failed: {exc}", file=sys.stderr)


def dead_end(slack: WebClient, raw_body: str) -> None:
    """The last receive before the DLQ posts a dead end the user can act
    on — silence is the one wrong answer (invariant 6; ruled 2026-07-23,
    #12 review). Best-effort: the message DLQs either way."""
    try:
        body = json.loads(raw_body)
        slack.chat_postMessage(
            channel=body["channel"], thread_ts=body["thread_ts"], text=FAILED_TEXT
        )
    except Exception as exc:  # noqa: BLE001 — the DLQ record is the fallback
        print(f"[qa-worker] dead-end post failed: {exc}", file=sys.stderr)


def handle(conn: psycopg.Connection, slack: WebClient, body: dict) -> None:
    """Answer one question. Returning normally lets the caller delete the
    message; raising leaves it for redelivery."""
    t0 = time.time()
    try:
        out = asyncio.run(
            asyncio.wait_for(
                agent.answer(conn, body["incident_id"], body["question"]),
                timeout=QA_TIMEOUT_S,
            )
        )
    except TimeoutError:
        # terminal by design: post the dead end, let the delete happen
        print(
            f"[qa-worker] timeout after {QA_TIMEOUT_S}s: {body['incident_id']}",
            file=sys.stderr,
        )
        slack.chat_postMessage(
            channel=body["channel"], thread_ts=body["thread_ts"], text=TIMEOUT_TEXT
        )
        return
    record_cost(conn, body["incident_id"], out, round(time.time() - t0))
    slack.chat_postMessage(
        channel=body["channel"], thread_ts=body["thread_ts"], text=out.text
    )
    print(
        f"[qa-worker] answered {body['incident_id']} in {round(time.time() - t0)}s"
        f" (attempt {out.attempt}, turns {out.turns}, ${out.cost_usd})"
    )


def main() -> int:
    dsn = _env("RCA_DATABASE_URL")
    token = _env("SLACK_BOT_TOKEN")
    queue_url = _env("RCA_QA_QUEUE_URL")
    region = queue_url.split(".")[1]
    slack = WebClient(token=token)
    sqs = boto3.client("sqs", region_name=region)

    print(f"qa-worker up — queue {queue_url}")
    while True:
        try:
            msgs = sqs.receive_message(
                QueueUrl=queue_url,
                MaxNumberOfMessages=1,
                WaitTimeSeconds=20,
                AttributeNames=["ApproximateReceiveCount"],
            ).get("Messages", [])
            if not msgs:
                continue
            msg = msgs[0]
            try:
                with psycopg.connect(
                    dsn, autocommit=True, connect_timeout=10
                ) as conn:
                    handle(conn, slack, json.loads(msg["Body"]))
                sqs.delete_message(
                    QueueUrl=queue_url, ReceiptHandle=msg["ReceiptHandle"]
                )
            except Exception:  # noqa: BLE001 — redelivers, then DLQ
                print("[qa-worker] message failed, will redeliver:", file=sys.stderr)
                traceback.print_exc()
                receives = int(
                    msg.get("Attributes", {}).get("ApproximateReceiveCount", "1")
                )
                if receives >= MAX_RECEIVES:
                    dead_end(slack, msg["Body"])
        except Exception:  # noqa: BLE001 — a bad poll is loud, the loop lives
            print("[qa-worker] receive failed:", file=sys.stderr)
            traceback.print_exc()
            time.sleep(5)


if __name__ == "__main__":
    sys.exit(main())
