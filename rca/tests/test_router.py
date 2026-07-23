"""Router tests (issue #11; channel-per-incident reshape 2026-07-23).

`handle` is driven directly with a fake connection, Slack, and Step
Functions, so the assertions are about ordering and calls: what got
upserted, which channel got created, what got posted where, and how each
crash window converges on redelivery. The live checks are Batch B in
docs/live-tests.md (to be re-run after this reshape).
"""

import json
import sys
from pathlib import Path

import pytest
from botocore.exceptions import ClientError
from slack_sdk.errors import SlackApiError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from service.router import RATE_LIMIT, enqueue_question, ensure_channel, handle

CFG = {"sm_arn": "arn:aws:states:ap-south-1:1:stateMachine:rca-investigation",
       "qa_queue_url": "https://sqs.ap-south-1.amazonaws.com/1/rca-qa.fifo",
       "allowlist": {"C-OK"}}
INCIDENT_ID = "11111111-2222-3333-4444-555555555555"
INC_CHANNEL = "C-INC"


class FakeConn:
    """Scripted psycopg stand-in: routes on SQL prefix."""

    def __init__(self, by_channel=None, by_origin=None, recent=0,
                 stored_channel=None):
        self.by_channel = by_channel    # row for CHANNEL_LOOKUP, or None
        self.by_origin = by_origin      # row for ORIGIN_LOOKUP, or None
        self.recent = recent            # count inside the rate window
        self.stored_channel = stored_channel  # UPSERT's RETURNING channel;
        #                                       None -> echo the insert param
        self.upserted: list[tuple] = []
        self.moved: list[tuple] = []

    def execute(self, sql, params=None):
        conn = self

        class Result:
            def fetchone(self):
                if sql.startswith("SELECT id, event_id FROM incidents"):
                    return conn.by_channel
                if sql.startswith("SELECT id, event_id, channel"):
                    return conn.by_origin
                if sql.startswith("SELECT count"):
                    return {"n": conn.recent}
                if sql.startswith("INSERT INTO incidents"):
                    conn.upserted.append(params)
                    return {"id": INCIDENT_ID,
                            "channel": conn.stored_channel or params[1]}
                raise AssertionError(f"unexpected SQL: {sql}")

        if sql.startswith("UPDATE incidents SET channel"):
            self.moved.append(params)
        return Result()


class FakeSlack:
    def __init__(self, parent_text="PARENT ALERT TEXT", name_taken=False,
                 listable=()):
        self.posts: list[dict] = []
        self.parent_text = parent_text
        self.parent_fetches = 0
        self.created: list[str] = []
        self.invited: list[tuple] = []
        self.name_taken = name_taken
        self.listable = list(listable)  # channels conversations_list returns

    def chat_postMessage(self, **kw):
        self.posts.append(kw)
        return {"ts": f"{200 + len(self.posts)}.1"}

    def conversations_replies(self, **_kw):
        self.parent_fetches += 1
        return {"messages": [{"text": self.parent_text}]}

    def conversations_create(self, name):
        if self.name_taken:
            raise SlackApiError("taken", {"error": "name_taken"})
        self.created.append(name)
        return {"channel": {"id": INC_CHANNEL}}

    def conversations_list(self, **_kw):
        return {"channels": self.listable,
                "response_metadata": {"next_cursor": ""}}

    def conversations_invite(self, channel, users):
        self.invited.append((channel, users))


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


def mention(channel="C-OK", ts="100.1", thread_ts=None,
            text="<@U1> alarm \"api-5xx\"", user="U-ASKER"):
    event = {"type": "app_mention", "channel": channel, "ts": ts,
             "text": text, "user": user}
    if thread_ts:
        event["thread_ts"] = thread_ts
    return {"type": "event_callback", "event_id": "Ev1", "event": event}


def test_new_alert_creates_channel_moves_row_and_starts():
    conn, slack, sfn = FakeConn(), FakeSlack(), FakeSFN()
    handle(conn, slack, sfn, FakeSQS(), CFG, mention())
    assert slack.created == [f"rca-{INCIDENT_ID}"]
    assert slack.invited == [(INC_CHANNEL, "U-ASKER")]
    # first post: the alert copy, top-level in the new channel
    copy = slack.posts[0]
    assert copy["channel"] == INC_CHANNEL
    assert "thread_ts" not in copy
    assert copy["text"] == 'alarm "api-5xx"'
    # second post: the link-back in the origin thread
    back = slack.posts[1]
    assert (back["channel"], back["thread_ts"]) == ("C-OK", "100.1")
    assert f"<#{INC_CHANNEL}>" in back["text"]
    # MOVE commits the incident channel and the copy's ts
    assert conn.moved == [(INC_CHANNEL, "201.1", INCIDENT_ID)]
    start = sfn.started[0]
    assert start["name"] == INCIDENT_ID
    assert json.loads(start["input"]) == {"incident_id": INCIDENT_ID}


def test_upsert_carries_event_id_envelope_and_received_utc():
    conn, slack, sfn = FakeConn(), FakeSlack(), FakeSFN()
    body = mention()
    handle(conn, slack, sfn, FakeSQS(), CFG, body)
    event_id, channel, anchor, _slug, raw, received = conn.upserted[0]
    assert event_id == "Ev1"
    assert (channel, anchor) == ("C-OK", "100.1")  # origin values, pre-MOVE
    assert raw.obj["envelope"] == body  # §8a-D: the sample nobody has
    assert raw.obj["raw"] == 'alarm "api-5xx"'
    assert raw.obj["channel"] == "C-OK"  # raw keeps the origin forever
    assert received.year >= 1970


def test_threaded_mention_uses_parent_as_alert_text():
    conn, slack, sfn = FakeConn(), FakeSlack(parent_text="PARENT!"), FakeSFN()
    handle(conn, slack, sfn, FakeSQS(), CFG, mention(ts="100.2", thread_ts="100.1"))
    assert slack.parent_fetches == 1
    assert conn.upserted[0][4].obj["raw"] == "PARENT!"
    assert conn.upserted[0][2] == "100.1"  # anchored to the thread, not the reply
    assert slack.posts[0]["text"] == "PARENT!"  # the copy carries the parent


def test_mention_in_incident_channel_routes_to_answer():
    conn = FakeConn(by_channel={"id": INCIDENT_ID, "event_id": "Ev-alert"})
    slack, sfn = FakeSlack(), FakeSFN()
    seen = []
    handle(conn, slack, sfn, FakeSQS(), CFG,
           mention(channel=INC_CHANNEL, ts="300.1", text="<@U1> what did q04 show?"),
           answer=lambda _q, _cf, _s, iid, _e, _eid: seen.append(iid))
    assert seen == [INCIDENT_ID]
    assert conn.upserted == []
    assert sfn.started == []


def test_retag_on_claimed_thread_points_at_incident_channel():
    """A second human tag on the same alert thread (different event_id)
    must not start a duplicate run — it gets a pointer instead."""
    conn = FakeConn(by_origin={"id": INCIDENT_ID, "event_id": "Ev-original",
                               "channel": INC_CHANNEL})
    slack, sfn = FakeSlack(), FakeSFN()
    handle(conn, slack, sfn, FakeSQS(), CFG, mention(thread_ts="100.1", ts="105.5"))
    assert conn.upserted == []
    assert sfn.started == []
    assert slack.created == []
    assert len(slack.posts) == 1
    assert f"<#{INC_CHANNEL}>" in slack.posts[0]["text"]


def test_redelivery_mid_setup_recovers_channel_and_converges():
    """Crash after conversations.create, before MOVE: the redelivery
    (same event_id) re-runs setup — name_taken recovers the channel id,
    the rate check is skipped even in a full window, and the run starts."""
    conn = FakeConn(
        by_origin={"id": INCIDENT_ID, "event_id": "Ev1", "channel": "C-OK"},
        recent=RATE_LIMIT,  # full window must not strand the redelivery
    )
    slack = FakeSlack(name_taken=True,
                      listable=[{"name": f"rca-{INCIDENT_ID}", "id": INC_CHANNEL}])
    sfn = FakeSFN()
    handle(conn, slack, sfn, FakeSQS(), CFG, mention())
    assert slack.created == []  # create failed name_taken; recovered by list
    assert conn.moved == [(INC_CHANNEL, "201.1", INCIDENT_ID)]
    assert len(sfn.started) == 1
    assert sfn.started[0]["name"] == INCIDENT_ID


def test_redelivery_after_move_skips_setup_entirely():
    """Crash after MOVE, before StartExecution: the upsert returns the
    incident channel, so no create, no posts — just the idempotent start."""
    conn = FakeConn(
        by_origin={"id": INCIDENT_ID, "event_id": "Ev1", "channel": INC_CHANNEL},
        stored_channel=INC_CHANNEL,
    )
    slack, sfn = FakeSlack(), FakeSFN()
    handle(conn, slack, sfn, FakeSQS(), CFG, mention())
    assert slack.created == []
    assert slack.posts == []
    assert conn.moved == []
    assert len(sfn.started) == 1
    assert sfn.started[0]["name"] == INCIDENT_ID


def test_unknown_channel_is_dropped_entirely():
    conn, slack, sfn = FakeConn(), FakeSlack(), FakeSFN()
    handle(conn, slack, sfn, FakeSQS(), CFG, mention(channel="C-GENERAL"))
    assert conn.upserted == []
    assert sfn.started == []
    assert slack.posts == []


def test_rate_limit_refuses_fresh_alert_without_starting():
    conn = FakeConn(recent=RATE_LIMIT)
    slack, sfn = FakeSlack(), FakeSFN()
    handle(conn, slack, sfn, FakeSQS(), CFG, mention())
    assert conn.upserted == []
    assert sfn.started == []
    assert slack.created == []
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


def test_failed_invite_never_costs_the_run():
    class NoInvite(FakeSlack):
        def conversations_invite(self, channel, users):  # noqa: ARG002
            raise SlackApiError("nope", {"error": "cant_invite"})

    conn, slack, sfn = FakeConn(), NoInvite(), FakeSFN()
    handle(conn, slack, sfn, FakeSQS(), CFG, mention())  # must not raise
    assert len(sfn.started) == 1
    assert conn.moved != []


def test_ensure_channel_raises_when_taken_but_unlistable():
    slack = FakeSlack(name_taken=True, listable=[])
    with pytest.raises(RuntimeError):
        ensure_channel(slack, INCIDENT_ID)


def test_question_acks_then_enqueues_fifo_deduped():
    slack, sqs = FakeSlack(), FakeSQS()
    event = {"channel": INC_CHANNEL, "ts": "300.5",
             "text": "<@U1> what did q04 show?"}
    enqueue_question(sqs, CFG, slack, INCIDENT_ID, event, "Ev-q1")
    assert slack.posts[0]["text"] == "Looking at the record…"
    sent = sqs.sent[0]
    assert json.loads(sent["MessageBody"]) == {
        "incident_id": INCIDENT_ID, "channel": INC_CHANNEL,
        "thread_ts": "300.5",  # the question's own ts: answer in its thread
        "question": "what did q04 show?", "event_id": "Ev-q1"}
    assert sent["QueueUrl"] == CFG["qa_queue_url"]
    assert sent["MessageDeduplicationId"] == "Ev-q1"  # §8f: dedup at the queue
    assert sent["MessageGroupId"] == INCIDENT_ID


def test_empty_question_gets_guidance_not_an_enqueue():
    slack, sqs = FakeSlack(), FakeSQS()
    enqueue_question(sqs, CFG, slack, INCIDENT_ID,
                     {"channel": INC_CHANNEL, "ts": "1.1", "text": "<@U1>"}, "Ev-q2")
    assert sqs.sent == []
    assert "Ask me something" in slack.posts[0]["text"]


def test_other_sfn_errors_propagate_for_redelivery():
    class Boom(FakeSFN):
        def start_execution(self, **_kw):
            raise ClientError({"Error": {"Code": "AccessDenied"}}, "StartExecution")

    with pytest.raises(ClientError):
        handle(FakeConn(), FakeSlack(), Boom(), FakeSQS(), CFG, mention())
