"""Router tests (issue #11): routing and the §8a-A conditions, with fakes.

`handle` is driven directly with a fake connection, Slack, and Step
Functions, so the assertions are about ordering and calls: what got
upserted, what got started, and — on the happy alert path — that no
Slack post happened at all. The live kill-tests are Batch B in
docs/live-tests.md.
"""

import json
import sys
from pathlib import Path

import pytest
from botocore.exceptions import ClientError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from service.router import RATE_LIMIT, enqueue_question, handle

CFG = {"sm_arn": "arn:aws:states:ap-south-1:1:stateMachine:rca-investigation",
       "qa_queue_url": "https://sqs.ap-south-1.amazonaws.com/1/rca-qa.fifo",
       "allowlist": {"C-OK"}}
INCIDENT_ID = "11111111-2222-3333-4444-555555555555"


class FakeConn:
    """Scripted psycopg stand-in: routes on SQL prefix."""

    def __init__(self, known=None, recent=0):
        self.known = known  # row for the thread lookup, or None
        self.recent = recent  # count inside the rate window
        self.upserted: list[tuple] = []

    def execute(self, sql, params=None):
        conn = self

        class Result:
            def fetchone(self):
                if sql.startswith("SELECT id, event_id FROM incidents"):
                    return conn.known
                if sql.startswith("SELECT count"):
                    return {"n": conn.recent}
                if sql.startswith("INSERT INTO incidents"):
                    conn.upserted.append(params)
                    return {"id": INCIDENT_ID}
                raise AssertionError(f"unexpected SQL: {sql}")

        return Result()


class FakeSlack:
    def __init__(self, parent_text="PARENT ALERT TEXT"):
        self.posts: list[dict] = []
        self.parent_text = parent_text
        self.parent_fetches = 0

    def chat_postMessage(self, **kw):
        self.posts.append(kw)

    def conversations_replies(self, **_kw):
        self.parent_fetches += 1
        return {"messages": [{"text": self.parent_text}]}


class FakeSQS:
    def __init__(self):
        self.sent: list[dict] = []

    def send_message(self, **kw):
        self.sent.append(kw)


class FakeSFN:
    def __init__(self, already_exists=False):
        self.already_exists = already_exists
        self.started: list[dict] = []

    def start_execution(self, **kw):
        if self.already_exists:
            raise ClientError(
                {"Error": {"Code": "ExecutionAlreadyExists"}}, "StartExecution"
            )
        self.started.append(kw)


def mention(channel="C-OK", ts="100.1", thread_ts=None, text="<@U1> alarm \"api-5xx\""):
    event = {"type": "app_mention", "channel": channel, "ts": ts, "text": text}
    if thread_ts:
        event["thread_ts"] = thread_ts
    return {"type": "event_callback", "event_id": "Ev1", "event": event}


def test_new_alert_upserts_starts_and_never_posts():
    conn, slack, sfn = FakeConn(), FakeSlack(), FakeSFN()
    handle(conn, slack, sfn, FakeSQS(), CFG, mention())
    assert len(conn.upserted) == 1
    assert len(sfn.started) == 1
    start = sfn.started[0]
    assert start["name"] == INCIDENT_ID
    assert json.loads(start["input"]) == {"incident_id": INCIDENT_ID}
    assert slack.posts == []  # the ack is the poller's, §8a-A condition 3


def test_upsert_carries_event_id_envelope_and_received_utc():
    conn, slack, sfn = FakeConn(), FakeSlack(), FakeSFN()
    body = mention()
    handle(conn, slack, sfn, FakeSQS(), CFG, body)
    event_id, channel, anchor, _slug, raw, received = conn.upserted[0]
    assert event_id == "Ev1"
    assert (channel, anchor) == ("C-OK", "100.1")
    assert raw.obj["envelope"] == body  # §8a-D: the sample nobody has
    assert raw.obj["raw"] == 'alarm "api-5xx"'
    assert received.year >= 1970


def test_threaded_mention_uses_parent_as_alert_text():
    conn, slack, sfn = FakeConn(), FakeSlack(parent_text="PARENT!"), FakeSFN()
    handle(conn, slack, sfn, FakeSQS(), CFG, mention(ts="100.2", thread_ts="100.1"))
    assert slack.parent_fetches == 1
    assert conn.upserted[0][4].obj["raw"] == "PARENT!"
    assert conn.upserted[0][2] == "100.1"  # anchored to the thread, not the reply


def test_known_thread_routes_to_answer_not_alert():
    """A different event_id in a known thread is a genuine follow-up."""
    conn = FakeConn(known={"id": INCIDENT_ID, "event_id": "Ev-original"})
    slack, sfn = FakeSlack(), FakeSFN()
    seen = []
    handle(conn, slack, sfn, FakeSQS(), CFG, mention(thread_ts="90.1", ts="100.5"),
           answer=lambda _q, _cf, _s, iid, _e, _eid: seen.append(iid))
    assert seen == [INCIDENT_ID]
    assert conn.upserted == []
    assert sfn.started == []


def test_redelivered_alert_redrives_start_not_qa():
    """Review 2026-07-22, critical: the incident's own envelope redelivering
    (same event_id) must converge to StartExecution, never to Q&A — this is
    the crash-between-upsert-and-start window of §8a-A condition 1."""
    conn = FakeConn(known={"id": INCIDENT_ID, "event_id": "Ev1"})
    slack, sfn = FakeSlack(), FakeSFN()
    seen = []
    handle(conn, slack, sfn, FakeSQS(), CFG, mention(),
           answer=lambda _q, _cf, _s, iid, _e, _eid: seen.append(iid))
    assert seen == []  # not a question
    assert conn.upserted == []  # no second row
    assert len(sfn.started) == 1  # the run is re-driven
    assert sfn.started[0]["name"] == INCIDENT_ID
    assert slack.posts == []


def test_unallowlisted_channel_is_dropped_entirely():
    conn, slack, sfn = FakeConn(), FakeSlack(), FakeSFN()
    handle(conn, slack, sfn, FakeSQS(), CFG, mention(channel="C-GENERAL"))
    assert conn.upserted == []
    assert sfn.started == []
    assert slack.posts == []


def test_rate_limit_refuses_in_thread_without_starting():
    conn = FakeConn(recent=RATE_LIMIT)
    slack, sfn = FakeSlack(), FakeSFN()
    handle(conn, slack, sfn, FakeSQS(), CFG, mention())
    assert conn.upserted == []
    assert sfn.started == []
    assert len(slack.posts) == 1
    assert "Rate limit" in slack.posts[0]["text"]


def test_empty_alert_text_gets_guidance_not_an_incident():
    conn, slack, sfn = FakeConn(), FakeSlack(), FakeSFN()
    handle(conn, slack, sfn, FakeSQS(), CFG, mention(text="<@U1>"))
    assert conn.upserted == []
    assert sfn.started == []
    assert "Tag me on an alert" in slack.posts[0]["text"]


def test_execution_already_exists_is_a_noop_not_an_error():
    conn, slack, sfn = FakeConn(), FakeSlack(), FakeSFN(already_exists=True)
    handle(conn, slack, sfn, FakeSQS(), CFG, mention())  # must not raise
    assert len(conn.upserted) == 1


def test_question_acks_then_enqueues_fifo_deduped():
    slack, sqs = FakeSlack(), FakeSQS()
    event = {"channel": "C-OK", "ts": "100.5", "thread_ts": "90.1",
             "text": "<@U1> what did q04 show?"}
    enqueue_question(sqs, CFG, slack, INCIDENT_ID, event, "Ev-q1")
    assert slack.posts[0]["text"] == "Looking at the record…"
    sent = sqs.sent[0]
    assert json.loads(sent["MessageBody"]) == {
        "incident_id": INCIDENT_ID, "channel": "C-OK", "thread_ts": "90.1",
        "question": "what did q04 show?", "event_id": "Ev-q1"}
    assert sent["QueueUrl"] == CFG["qa_queue_url"]
    assert sent["MessageDeduplicationId"] == "Ev-q1"  # §8f: dedup at the queue
    assert sent["MessageGroupId"] == INCIDENT_ID


def test_empty_question_gets_guidance_not_an_enqueue():
    slack, sqs = FakeSlack(), FakeSQS()
    enqueue_question(sqs, CFG, slack, INCIDENT_ID,
                     {"channel": "C-OK", "ts": "1.1", "text": "<@U1>"}, "Ev-q2")
    assert sqs.sent == []
    assert "Ask me something" in slack.posts[0]["text"]


def test_other_sfn_errors_propagate_for_redelivery():
    class Boom(FakeSFN):
        def start_execution(self, **_kw):
            raise ClientError({"Error": {"Code": "AccessDenied"}}, "StartExecution")

    with pytest.raises(ClientError):
        handle(FakeConn(), FakeSlack(), Boom(), FakeSQS(), CFG, mention())
