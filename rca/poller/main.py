"""Poller: narrate the record into Slack, and post the terminal message.

New code, 2026-07-22 (issue #10). Replaces slackbot/poster.py's byte-offset
tail and daemon.py's _watch_run for the hosted system; the milestone
message formats are carried over from poster.py (2026-07-18). Runs as one
always-on Fargate task — exactly one (P8 §4): two pollers would post every
line twice, so the Service pins deploys to stop-then-start.

Two authorities (P8 §5). The events table says what the investigation
found — narrated by row-id cursor, one appended message per milestone.
A tool_call with a --purpose string posts as a "Querying:" line (§8g
amendment 2026-07-23 — the incident channel absorbs the volume D6 kept
out of the shared thread); purposeless tool_calls and instrument_note
do not post. DescribeExecution says
whether the run is over, and how — the terminal message. A hard task death
writes no run_failed row, so the table alone would wait forever.

Also owned here, per §8a-A: the "investigating…" ack, off the run_started
row, idempotent across restarts by the same cursor as every other line;
and the never-started case — an incident whose execution still does not
exist after a grace window gets a posted failure instead of silence.

Exceptions are logged in full and never swallowed (P8 §6): a poller that
cannot talk must be loudly broken in its logs, and repeated failures are
a liveness signal (#13). Narration is at-least-once — the cursor advances
after a post, so a crash between the two re-posts one line rather than
dropping it (invariant 6, loud cheap error).

Done means done: an incident leaves the active set only when
terminal_posted_at is set (migration 004), which happens in the same
breath as the terminal post.

Env (from the task definition, #10): RCA_DATABASE_URL (role rca_poller),
SLACK_BOT_TOKEN, RCA_STATE_MACHINE_ARN. The AWS region is parsed from the
ARN rather than carried as a fourth variable.
"""

import os
import re
import sys
import time
import traceback
from datetime import UTC, datetime

import boto3
import psycopg
from botocore.exceptions import ClientError
from psycopg.rows import dict_row
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

POLL_S = 30.0  # ruled 2026-07-22: milestones land ~48s apart (P8 §1), 5s bought nothing

# StartExecution can lag the incident row by several inbound redeliveries
# when it is failing (§8a-A: bad IAM, moved ARN). Inside this window a
# missing execution is a race with the router; after it, a failure to post.
# A never-started close is irreversible (review 2026-07-22), so the window
# must clear any sane redelivery span; #11 revisits it when the queue's
# visibility timeout and maxReceiveCount actually exist.
NEVER_STARTED_GRACE_S = 1800

# Slack rejects chat.postMessage text over 40k chars; an oversize milestone
# must not wedge the cursor (review 2026-07-22).
MAX_POST_CHARS = 39_000

# A telemetry tool_call carries the agent's own --purpose string; that line
# narrates (§8g amendment, 2026-07-23 — D6 relaxed for the incident channel).
# Mechanics (Read, Glob, --help probes, Write) have no purpose and stay silent.
PURPOSE = re.compile(r"--purpose\s+(?:\"([^\"]*)\"|'([^']*)')")

# run.py's exit-code contract (#7), rendered for the terminal message.
EXIT_LABELS = {
    1: "agent error — not retried",
    2: "startup failure",
    3: "wall-clock cap hit",
    4: "budget cap hit — not re-investigated",
    5: "unexpected error",
}

ACTIVE_INCIDENTS = """\
SELECT id, channel, thread_ts, slug, narrated_through, created_at
  FROM incidents
 WHERE terminal_posted_at IS NULL
 ORDER BY created_at
"""

NEW_EVENTS = """\
SELECT id, attempt, event, payload
  FROM events
 WHERE incident_id = %s AND id > %s
 ORDER BY id
"""

# The document for the channel canvas: newest attempt's rca.md.
# Needs migration 006 (GRANT SELECT ON documents TO rca_poller).
LAST_DOC = """\
SELECT content
  FROM documents
 WHERE incident_id = %s AND name = 'rca.md'
 ORDER BY id DESC
 LIMIT 1
"""

# Scoped to the newest attempt (review 2026-07-22): attempt 1's run_failed
# must not label a terminal where attempt 2 died hard and wrote nothing —
# no row on the last attempt is exactly the hard-death signal (P8 §5).
LAST_PAYLOAD = """\
SELECT payload
  FROM events
 WHERE incident_id = %(id)s AND event = %(event)s
   AND attempt = (SELECT max(attempt) FROM events WHERE incident_id = %(id)s)
 ORDER BY id DESC
 LIMIT 1
"""


def _env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"{name} is not set; the task environment must provide it")
    return value


def format_event(event: str, attempt: int, payload: dict) -> str | None:
    """One milestone -> one Slack line, or None for the unnarrated kinds.

    Formats carried over from poster.py. run_started is new here: the ack
    moved to the poller (§8a-A) so it rides the cursor's idempotency.
    run_finished stays silent — the terminal message covers it.

    Total by design (review 2026-07-22): emit.py validates that a payload
    is an object, not its field shapes, and a raise here would wedge the
    cursor on the same row every tick, forever. Render what is there.
    """
    if event == "run_started":
        if attempt > 1:
            return (
                f"🔁 Restarting after an infrastructure failure "
                f"(attempt {attempt})."
            )
        return "🔍 *Investigating* — I'll post here as I go."
    if event == "hypothesis":
        verb = {
            "formed": "💡 Hypothesis",
            "supported": "✅ Supported",
            "killed": "❌ Ruled out",
        }.get(payload.get("status"), "💡 Hypothesis")
        raw_qids = payload.get("qids")
        qids = ""
        if isinstance(raw_qids, list) and raw_qids:
            qids = f" ({', '.join(str(q) for q in raw_qids)})"
        return f"{verb}: {payload.get('claim', '')}{qids}"
    if event == "timeline_settled":
        return f"📋 *Timeline settled*\n{payload.get('summary', '')}"
    if event == "self_check":
        return (
            f"🧪 Self-check: {payload.get('claims_checked', '?')} claims "
            f"verified, {payload.get('fixed', 0)} fixed."
        )
    if event == "doc_ready":
        return f"📄 *Verdict:* {payload.get('verdict', '(missing)')}"
    if event == "run_failed":
        return f"⚠️ Run failed: {payload.get('reason', 'unknown')}"
    if event == "tool_call":
        m = PURPOSE.search(payload.get("command") or "")
        if m:
            return f"🔎 Querying: {m.group(1) or m.group(2)}"
        return None  # a mechanics call: Read, Glob, --help, Write
    return None  # instrument_note, run_finished, qa_answered


def terminal_text(
    status: str,
    slug: str,
    finished: dict | None,
    failed: dict | None,
) -> str:
    """The closing line, from the execution status plus the record's last
    lifecycle row. FAILED with no run_failed row is the hard-death case:
    only Step Functions knew (P8 §5)."""
    if status == "SUCCEEDED":
        stats = ""
        if finished:
            parts = []
            if finished.get("elapsed_s") is not None:
                parts.append(f"{finished['elapsed_s']}s")
            if finished.get("turns") is not None:
                parts.append(f"{finished['turns']} turns")
            # cost_usd stays in the record (P6 §6 accounting), not the
            # channel (ruled 2026-07-23)
            if parts:
                stats = f" in {', '.join(parts)}"
        return f"✅ *Investigation finished* for `{slug}`{stats} — document is ready."
    if status == "TIMED_OUT":
        return (
            f"❌ Investigation FAILED for `{slug}` — the state machine's "
            f"2-hour timeout hit."
        )
    if status == "ABORTED":
        return f"❌ Investigation FAILED for `{slug}` — the execution was aborted."
    # FAILED
    if failed:
        code = failed.get("exit_code")
        label = EXIT_LABELS.get(code, f"exit {code}")
        reason = failed.get("reason", "unknown")
        return f"❌ Investigation FAILED for `{slug}` — {label}: {reason}"
    # No run_failed row on the last attempt covers two causes run.py cannot
    # tell apart from here: a hard infrastructure death, and a startup
    # failure (exit 2) — most startup paths cannot write a row because the
    # DB or the incident row is the thing that is broken (review 2026-07-22).
    return (
        f"❌ Investigation FAILED for `{slug}` — the task wrote no failure row: "
        f"a hard infrastructure death, or a startup failure (exit 2). "
        f"Check the /ecs/rca logs."
    )


def post(slack: WebClient, inc: dict, text: str) -> None:
    """The one Slack write path: append to the incident's thread, truncated
    to Slack's hard limit so an oversize milestone cannot wedge the cursor.

    Each message carries a divider block so consecutive entries separate
    visually (ruled 2026-07-23, cosmetic). Section blocks cap at 3000
    chars against 40k for plain text; a long line (timeline summary)
    falls back to plain rather than truncating harder — `text` doubles
    as the notification fallback either way."""
    if len(text) > MAX_POST_CHARS:
        text = text[:MAX_POST_CHARS] + " …[truncated]"
    kwargs: dict = {
        "channel": inc["channel"], "thread_ts": inc["thread_ts"], "text": text,
    }
    if len(text) <= 3000:
        kwargs["blocks"] = [
            {"type": "section", "text": {"type": "mrkdwn", "text": text}},
            {"type": "divider"},
        ]
    slack.chat_postMessage(**kwargs)


def publish_canvas(conn: psycopg.Connection, slack: WebClient, inc: dict) -> None:
    """Render rca.md as the incident channel's canvas (ruled 2026-07-23).

    Best-effort on purpose: a raise here would wedge the cursor on the
    doc_ready row and repost the verdict every tick forever — the same
    wedge class as the oversize post. The document's home is the DB; the
    canvas is a rendering. One channel holds one canvas, so a duplicate
    create fails `channel_canvas_already_exists` — idempotency for free.
    """
    doc = conn.execute(LAST_DOC, (inc["id"],)).fetchone()
    if not doc:
        print(f"[poller] doc_ready but no rca.md row: {inc['id']}", file=sys.stderr)
        return
    content = doc["content"]
    if not content.lstrip().startswith("# "):
        # channel canvases have no API title field; the H1 is the title
        content = f"# RCA: {inc['slug']}\n\n{content}"
    try:
        slack.conversations_canvases_create(
            channel_id=inc["channel"],
            document_content={"type": "markdown", "markdown": content},
        )
        post(slack, inc, "📄 Full report is in this channel's canvas.")
    except SlackApiError as e:
        if e.response["error"] == "channel_canvas_already_exists":
            return  # a prior tick made it; the repost path lands here
        print(f"[poller] canvas failed: {e.response['error']}", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001 — rendering never wedges narration
        print(f"[poller] canvas failed: {exc}", file=sys.stderr)


def narrate(conn: psycopg.Connection, slack: WebClient, inc: dict) -> None:
    """Post every unnarrated milestone, oldest first, and advance the cursor.

    The cursor lands in `finally`: a Slack failure mid-batch keeps what was
    posted, propagates loudly, and the next tick resumes after the last
    success. Unnarrated kinds advance the cursor without a post.
    """
    rows = conn.execute(NEW_EVENTS, (inc["id"], inc["narrated_through"])).fetchall()
    last = inc["narrated_through"]
    try:
        for row in rows:
            text = format_event(row["event"], row["attempt"], row["payload"] or {})
            if text is not None:
                post(slack, inc, text)
            if row["event"] == "doc_ready":
                publish_canvas(conn, slack, inc)
            last = row["id"]
    finally:
        if last != inc["narrated_through"]:
            conn.execute(
                "UPDATE incidents SET narrated_through = %s WHERE id = %s",
                (last, inc["id"]),
            )
            inc["narrated_through"] = last


def close_incident(
    conn: psycopg.Connection, slack: WebClient, inc: dict, text: str
) -> None:
    """Post the terminal message, then mark the incident done. Post-first:
    a crash between the two re-posts once rather than closing silently."""
    post(slack, inc, text)
    conn.execute(
        "UPDATE incidents SET terminal_posted_at = now() WHERE id = %s",
        (inc["id"],),
    )


def check_execution(
    conn: psycopg.Connection,
    slack: WebClient,
    sfn,
    exec_arn_prefix: str,
    inc: dict,
) -> None:
    """Terminal authority: DescribeExecution. RUNNING means come back next
    tick. A terminal status drains narration once more (rows written after
    this tick's narrate pass, before the task stopped), then closes."""
    try:
        desc = sfn.describe_execution(executionArn=f"{exec_arn_prefix}{inc['id']}")
    except ClientError as e:
        if e.response["Error"]["Code"] != "ExecutionDoesNotExist":
            raise
        age = datetime.now(UTC) - inc["created_at"]
        if age.total_seconds() < NEVER_STARTED_GRACE_S:
            return  # router may still be between upsert and StartExecution
        close_incident(
            conn,
            slack,
            inc,
            f"❌ Investigation never started for `{inc['slug']}` — no execution "
            f"exists {int(age.total_seconds() // 60)} minutes after the "
            f"incident was recorded. StartExecution is likely failing; "
            f"check the router logs and the DLQ (§8a-A).",
        )
        return

    if desc["status"] == "RUNNING":
        return
    narrate(conn, slack, inc)
    finished = conn.execute(
        LAST_PAYLOAD, {"id": inc["id"], "event": "run_finished"}
    ).fetchone()
    failed = conn.execute(
        LAST_PAYLOAD, {"id": inc["id"], "event": "run_failed"}
    ).fetchone()
    text = terminal_text(
        desc["status"],
        inc["slug"],
        finished["payload"] if finished else None,
        failed["payload"] if failed else None,
    )
    close_incident(conn, slack, inc, text)


def tick(slack: WebClient, sfn, dsn: str, exec_arn_prefix: str) -> None:
    """One pass over the active set. Per-incident failures are logged in
    full and skip to the next incident — one broken thread must not starve
    the others — but are never swallowed silently (P8 §6)."""
    with psycopg.connect(
        dsn, autocommit=True, connect_timeout=10, row_factory=dict_row
    ) as conn:
        for inc in conn.execute(ACTIVE_INCIDENTS).fetchall():
            inc["id"] = str(inc["id"])
            try:
                narrate(conn, slack, inc)
                check_execution(conn, slack, sfn, exec_arn_prefix, inc)
            except Exception:  # noqa: BLE001 — logged loudly, next incident
                print(f"[poller] incident {inc['id']} failed:", file=sys.stderr)
                traceback.print_exc()


def main() -> int:
    dsn = _env("RCA_DATABASE_URL")
    token = _env("SLACK_BOT_TOKEN")
    sm_arn = _env("RCA_STATE_MACHINE_ARN")
    # arn:aws:states:<region>:<acct>:stateMachine:<name>
    region = sm_arn.split(":")[3]
    exec_arn_prefix = sm_arn.replace(":stateMachine:", ":execution:") + ":"

    slack = WebClient(token=token)
    sfn = boto3.client("stepfunctions", region_name=region)

    print(f"poller up — machine {sm_arn}, every {POLL_S}s", flush=True)
    while True:
        try:
            tick(slack, sfn, dsn, exec_arn_prefix)
        except Exception:  # noqa: BLE001 — a bad tick is loud, the loop lives
            print("[poller] tick failed:", file=sys.stderr)
            traceback.print_exc()
        time.sleep(POLL_S)


if __name__ == "__main__":
    sys.exit(main())
