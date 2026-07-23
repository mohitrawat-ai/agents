"""Router: consume `inbound`, route by channel, upsert + StartExecution.

New code, 2026-07-22 (issue #11); reshaped 2026-07-23 to channel-per-
incident (ruled in chat 2026-07-23 — design.md amendment to follow). An
alert tagged in a central channel gets its own public channel, named
`rca-<incident id>`. The alert text is re-posted there as the channel's
first message; the poller narrates in that message's thread; questions
are separate tagged messages in the incident channel, answered in their
own threads. The central thread gets a single link back.

Routing is by channel, never by thread or message content:

- mention in an allowlisted (central) channel  -> alert path
- mention in a channel this system created     -> question
- anything else                                -> dropped, loudly logged

The alert path now posts to Slack, which amends §8a-A condition 3 ("the
happy alert path makes NO Slack post"). What that condition protected —
crash-then-redelivery convergence — is kept by different means:

1. The upsert stays first and stays `DO UPDATE ... RETURNING`, keyed on
   the Slack event_id — always one row back (§8a-A condition 1). It now
   also returns `channel`, which says how far setup got.
2. The channel name is derived from the incident id, so a retry can
   never create a second channel: `conversations.create` fails with
   `name_taken` and the id is recovered by name from `conversations.list`.
3. `MOVE` — the UPDATE that flips the row's channel/thread_ts from the
   origin thread to the incident channel — is the setup commit point.
   A crash before it re-runs setup on redelivery; worst case is a
   duplicate alert copy or link-back, invariant 6's loud cheap error.
   A crash after it skips setup and lands on the idempotent
   StartExecution (§8a-A condition 2: name = incident id, input =
   `{incident_id}` and nothing else).

The re-tag case (a second person tags the bot on an already-claimed
alert thread) is caught by ORIGIN_LOOKUP against `raw`, and answers with
a pointer to the incident channel instead of a duplicate run. The rate
check is skipped when the event_id already has a row: a redelivery must
converge even inside a full window. The invite of the tagging user is
best-effort — a failed invite must not cost the run.

Known small hole, accepted: a question asked in the incident channel in
the seconds before MOVE commits finds no row and is dropped; the asker
re-asks. The router still never answers a question itself (§8f): it
acks, enqueues on `rca-qa`, and returns. qa/worker.py answers.

A failed message is never deleted: it redelivers, and after
maxReceiveCount lands in the DLQ (queue config, provision.sh). Errors
log in full, never swallowed.

incidents.raw is the investigator's alert.json verbatim (run.py
materializes it), so it keeps the laptop-proven shape — source, channel,
thread_ts, condition_guess, received_utc, raw text — plus `envelope`,
the untouched Slack event callback (§8a-D). `raw`'s channel/thread_ts
stay the ORIGIN values forever; the row's columns move to the incident
channel at MOVE. ORIGIN_LOOKUP depends on that split.

Env (task definition, #11): RCA_DATABASE_URL (role rca_service),
SLACK_BOT_TOKEN, RCA_INBOUND_QUEUE_URL, RCA_QA_QUEUE_URL,
RCA_STATE_MACHINE_ARN, RCA_CHANNEL_ALLOWLIST (comma-separated central
channel ids). New Slack scope required: channels:manage.

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
from slack_sdk.errors import SlackApiError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from slackbot.parse_alert import parse_alert

RATE_LIMIT = 5  # investigations per window, global — a backstop, not policy
RATE_WINDOW = "10 minutes"

# question routing: is this a channel the system created? After MOVE the
# row's channel IS the incident channel, so equality is the whole test.
CHANNEL_LOOKUP = (
    "SELECT id, event_id FROM incidents WHERE channel = %s LIMIT 1"
)

# re-tag detection: the row's columns move at MOVE, but raw keeps the
# origin thread forever — so the origin lookup goes through raw.
ORIGIN_LOOKUP = (
    "SELECT id, event_id, channel FROM incidents"
    " WHERE raw->>'channel' = %s AND raw->>'thread_ts' = %s LIMIT 1"
)

# §8a-A condition 1: DO UPDATE, not DO NOTHING — always returns the row,
# including on redelivery. RETURNING channel tells the caller whether
# MOVE already committed (channel != the origin channel means yes).
UPSERT = """\
INSERT INTO incidents (event_id, channel, thread_ts, slug, raw, received_utc)
VALUES (%s, %s, %s, %s, %s, %s)
ON CONFLICT (event_id) DO UPDATE SET event_id = EXCLUDED.event_id
RETURNING id, channel
"""

# the setup commit point: everything before it may re-run on redelivery,
# everything after it is skipped.
MOVE = "UPDATE incidents SET channel = %s, thread_ts = %s WHERE id = %s"

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


def ensure_channel(slack: WebClient, incident_id: str) -> str:
    """Create `rca-<incident id>`, or recover its id if a prior attempt
    already did. The deterministic name is the idempotency key: Slack
    channel names are unique, so the second create can only fail with
    name_taken — and then the channel is findable by that same name."""
    name = f"rca-{incident_id}"
    try:
        return slack.conversations_create(name=name)["channel"]["id"]
    except SlackApiError as e:
        if e.response["error"] != "name_taken":
            raise
    cursor = None
    while True:
        page = slack.conversations_list(
            types="public_channel", exclude_archived=True, limit=200, cursor=cursor
        )
        for ch in page["channels"]:
            if ch["name"] == name:
                return ch["id"]
        cursor = page.get("response_metadata", {}).get("next_cursor") or None
        if not cursor:
            # taken but unlistable (archived by hand?) — raise, redeliver,
            # DLQ: loud, never a silent second channel.
            raise RuntimeError(f"channel {name} exists but was not found by list")


def invite(slack: WebClient, channel_id: str, user: str | None) -> None:
    """Best-effort: the tagging user should land in the incident channel,
    but a failed invite (already in, restricted account) must never cost
    the run — so nothing here raises."""
    if not user:
        return
    try:
        slack.conversations_invite(channel=channel_id, users=user)
    except SlackApiError as e:
        print(f"[router] invite of {user} failed: {e.response['error']}",
              file=sys.stderr)


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
    question gets guidance instead — nothing to enqueue. The anchor is
    the question's own ts for a top-level message, so the answer lands
    in the question's thread (channel-per-incident ruling, 2026-07-23)."""
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
        known = conn.execute(CHANNEL_LOOKUP, (channel,)).fetchone()
        if known:
            answer(sqs, cfg, slack, str(known["id"]), event, body.get("event_id"))
            return
        print(
            f"[router] dropped: channel {channel} is neither allowlisted nor "
            f"an incident channel (event {body.get('event_id')})",
            file=sys.stderr,
        )
        return

    # alert path — the mention is in a central channel
    origin_ts = event.get("thread_ts") or event["ts"]

    prior = conn.execute(ORIGIN_LOOKUP, (channel, origin_ts)).fetchone()
    if prior and prior["event_id"] != body.get("event_id"):
        # a human re-tag on a claimed thread, not a redelivery: point at
        # the incident channel instead of starting a duplicate run.
        _post(
            slack,
            channel,
            origin_ts,
            f"Already on this one — investigation is in <#{prior['channel']}>.",
        )
        return

    text = alert_text_for(event, slack)
    if not text:
        _post(
            slack,
            channel,
            origin_ts,
            "Tag me on an alert message (in its thread), or paste the alert "
            "text with the tag.",
        )
        return

    # prior (same event_id) means redelivery: skip the rate check — the
    # row already exists and refusing here would strand a paid-for run.
    if prior is None and conn.execute(RECENT).fetchone()["n"] >= RATE_LIMIT:
        _post(
            slack,
            channel,
            origin_ts,
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
        "thread_ts": origin_ts,
        "condition_guess": parsed["condition_guess"],
        "received_utc": parsed["received_utc"],
        "raw": parsed["raw"],
        "envelope": body,
    }
    row = conn.execute(
        UPSERT,
        (
            body["event_id"],
            channel,
            origin_ts,
            parsed["slug"],
            Jsonb(alert),
            received,
        ),
    ).fetchone()
    incident_id = str(row["id"])

    if row["channel"] == channel:
        # MOVE has not committed: fresh alert, or redelivery after a
        # crash mid-setup. Run (or re-run) the setup; duplicates of the
        # posts are invariant 6's accepted loud cheap error.
        incident_channel = ensure_channel(slack, incident_id)
        invite(slack, incident_channel, event.get("user"))
        copy_ts = slack.chat_postMessage(
            channel=incident_channel, text=text
        )["ts"]
        _post(
            slack,
            channel,
            origin_ts,
            f"On it — investigating in <#{incident_channel}>. "
            f"I'll post progress there.",
        )
        conn.execute(MOVE, (incident_channel, copy_ts, incident_id))

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
