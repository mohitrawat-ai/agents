"""Ingress: verify the Slack signature, enqueue the raw envelope, ack.

New code, 2026-07-23 (issue #11). Replaces daemon.py's Socket Mode
listener with `slack_bolt` over HTTP (Slice 5); the Service's other half,
the router, consumes what this enqueues.

Invariant 5 is the whole design: return 200 the instant the request is
durable in SQS, and not before. `process_before_response=True` is what
holds that ordering in bolt — the listener (the enqueue) completes before
the HTTP response is written, and a listener exception surfaces as a 500
so Slack redelivers. Nothing else happens on this path: no Slack API
call, no DB read, no parsing beyond bolt's own envelope dispatch
(#11: "nothing fallible sits in front of the ack").

The envelope goes onto the queue raw — the router owns interpretation.
The `type` message attribute exists for per-type DLQ alarming (§8a-A's
accepted-cost mitigation), not for routing logic.

Env (task definition, #11): SLACK_SIGNING_SECRET, RCA_INBOUND_QUEUE_URL.
No bot token: ingress authenticates requests, never makes them — a
static `authorize` replaces bolt's per-dispatch auth.test call, which
would otherwise put a Slack API round-trip in front of the ack.

Serve: uvicorn --factory service.ingress:asgi
"""

import json
import os

import boto3
from slack_bolt import App
from slack_bolt.adapter.asgi import SlackRequestHandler
from slack_bolt.authorization import AuthorizeResult


def _env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"{name} is not set; the task environment must provide it")
    return value


def build_app(sqs=None) -> App:
    """The bolt app, with the SQS client injectable for tests."""
    queue_url = _env("RCA_INBOUND_QUEUE_URL")
    if sqs is None:
        # https://sqs.<region>.amazonaws.com/<acct>/rca-inbound
        sqs = boto3.client("sqs", region_name=queue_url.split(".")[1])

    def authorize(enterprise_id, team_id, **_) -> AuthorizeResult:
        # A static authorize instead of a token: bolt's default calls
        # auth.test per dispatch, a Slack API call on the ack path —
        # exactly what invariant 5 forbids in front of the 200. Ingress
        # needs no auth context; it never calls Slack.
        return AuthorizeResult(enterprise_id=enterprise_id, team_id=team_id)

    app = App(
        authorize=authorize,
        signing_secret=_env("SLACK_SIGNING_SECRET"),
        process_before_response=True,  # invariant 5: durable write, then 200
    )

    @app.event("app_mention")
    def enqueue(body) -> None:
        # No per-type message attribute (review 2026-07-23): §8a-A's
        # per-type DLQ mitigation cannot live here — alert-vs-question is
        # decided by the router, after this write. A constant attribute
        # reads as discrimination and delivers none. #13 owns the DLQ
        # alarm story.
        sqs.send_message(QueueUrl=queue_url, MessageBody=json.dumps(body))

    return app


def asgi():
    """uvicorn factory target: /healthz for the ALB target group, signed
    Slack traffic for everything else. The health check must not depend
    on a signable request, and #13's canary — not this route — is what
    proves the Slack path end to end."""
    handler = SlackRequestHandler(build_app())

    async def app(scope, receive, send) -> None:
        if scope["type"] == "http" and scope["path"] == "/healthz":
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [(b"content-type", b"text/plain")],
                }
            )
            await send({"type": "http.response.body", "body": b"ok"})
            return
        await handler(scope, receive, send)

    return app
