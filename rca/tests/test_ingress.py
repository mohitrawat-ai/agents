"""Ingress tests (issue #11): the ack contract, without a server.

Drives the bolt App through App.dispatch with hand-signed BoltRequests,
so signature verification, dispatch, and the enqueue-then-200 ordering
are all real; only SQS is a stub. Invariant 5 in test form: 200 only
after the durable write, non-2xx when the write fails.
"""

import hashlib
import hmac
import json
import sys
import time
from pathlib import Path

import pytest
from slack_bolt.request import BoltRequest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from service.ingress import build_app

SECRET = "test-signing-secret"


class StubSQS:
    def __init__(self, fail: bool = False):
        self.fail = fail
        self.sent: list[dict] = []

    def send_message(self, **kwargs):
        if self.fail:
            raise RuntimeError("sqs unavailable")
        self.sent.append(kwargs)
        return {"MessageId": "m1"}


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("SLACK_SIGNING_SECRET", SECRET)
    monkeypatch.setenv(
        "RCA_INBOUND_QUEUE_URL",
        "https://sqs.ap-south-1.amazonaws.com/537124933640/rca-inbound",
    )
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test-not-a-token")


def signed_request(body: str, secret: str = SECRET) -> BoltRequest:
    ts = str(int(time.time()))
    sig = (
        "v0="
        + hmac.new(
            secret.encode(), f"v0:{ts}:{body}".encode(), hashlib.sha256
        ).hexdigest()
    )
    return BoltRequest(
        body=body,
        headers={
            "x-slack-signature": [sig],
            "x-slack-request-timestamp": [ts],
            "content-type": ["application/json"],
        },
    )


MENTION = json.dumps(
    {
        "type": "event_callback",
        "event_id": "Ev123",
        "event": {
            "type": "app_mention",
            "channel": "C1",
            "ts": "1.1",
            "text": "<@U1> alert",
        },
    }
)


def test_valid_mention_enqueues_raw_envelope_then_200():
    sqs = StubSQS()
    resp = build_app(sqs=sqs).dispatch(signed_request(MENTION))
    assert resp.status == 200
    assert len(sqs.sent) == 1
    assert json.loads(sqs.sent[0]["MessageBody"])["event_id"] == "Ev123"


def test_bad_signature_is_rejected_and_nothing_enqueued():
    sqs = StubSQS()
    resp = build_app(sqs=sqs).dispatch(signed_request(MENTION, secret="wrong"))
    assert resp.status == 401
    assert sqs.sent == []


def test_enqueue_failure_returns_non_2xx_so_slack_redelivers():
    resp = build_app(sqs=StubSQS(fail=True)).dispatch(signed_request(MENTION))
    assert resp.status >= 500


def test_healthz_returns_200_without_signature():
    import asyncio

    from service.ingress import asgi

    app = asgi()
    sent = []

    async def run():
        scope = {"type": "http", "path": "/healthz", "method": "GET"}

        async def receive():
            return {"type": "http.request"}

        async def send(msg):
            sent.append(msg)

        await app(scope, receive, send)

    asyncio.run(run())
    assert sent[0]["status"] == 200


def test_url_verification_challenge_echoes():
    body = json.dumps({"type": "url_verification", "challenge": "ch42"})
    resp = build_app(sqs=StubSQS()).dispatch(signed_request(body))
    assert resp.status == 200
    assert "ch42" in resp.body
