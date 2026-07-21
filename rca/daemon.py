"""Slack daemon: a tag on an alert becomes an investigator run.

New code, 2026-07-18 (unit 7). Deliberately dumb dispatcher — no LLM here:
listen (Socket Mode via bolt), find the alert text, parse shallowly, dedup,
spawn investigator/run.py as a subprocess, acknowledge in the thread. The
handler core is transport-blind: moving to a hosted Events-API endpoint
later swaps the one SocketModeHandler line, nothing else (unit-7 ruling 2).

State (active run, recent dedup keys) is in-memory only — a restart forgets
it; accepted for now, on the design doc's review-before-production list.

Usage:
    uv run python daemon.py
Requires in rca/.env: SLACK_BOT_TOKEN (xoxb-), SLACK_APP_TOKEN (xapp-).
"""

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

# package is 'slackbot': 'slack' would collide with slack_sdk's deprecated shim
from slackbot.parse_alert import parse_alert
from slackbot.poster import tail_events

DRY_RUN = "--dry-run" in sys.argv  # spawn the investigator with --mock (no LLM)
RCA_ROOT = Path(__file__).resolve().parent
INCIDENTS_ROOT = RCA_ROOT.parents[1] / "data" / "newrelic" / "incidents"
RUN_PY = RCA_ROOT / "investigator" / "run.py"
QA_PY = RCA_ROOT / "qa" / "agent.py"
DEDUP_WINDOW_S = 30 * 60


def _incident_for_thread(thread_ts: str) -> Path | None:
    """Resolve a Slack thread to its incident folder via alert.json — the
    folder is the durable state, so this survives daemon restarts."""
    for alert_path in INCIDENTS_ROOT.glob("*/alert.json"):
        try:
            if json.loads(alert_path.read_text()).get("thread_ts") == thread_ts:
                return alert_path.parent
        except (json.JSONDecodeError, OSError):
            continue
    return None


def load_env_into_os(path: Path) -> None:
    """Same parser as investigator/run.py; values never printed."""
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_env_into_os(RCA_ROOT / ".env")
app = App(token=os.environ["SLACK_BOT_TOKEN"])

# in-memory daemon state (see review-before-production list)
_active_run: dict | None = None  # {"slug": ..., "thread": ...}
_recent: dict[str, dict] = {}  # dedup_key -> {"slug", "ts", "thread"}
_lock = threading.Lock()


def _say(client, channel: str, thread_ts: str, text: str) -> None:
    client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=text)


def _alert_text_for(event: dict, client) -> tuple[str | None, str]:
    """Return (alert_text, anchor_ts). If the mention is a threaded reply,
    the thread's parent message is the alert; otherwise the mention's own
    text (minus the bot tag) must carry it."""
    channel = event["channel"]
    thread_ts = event.get("thread_ts")
    if thread_ts and thread_ts != event["ts"]:
        parent = client.conversations_replies(channel=channel, ts=thread_ts, limit=1)[
            "messages"
        ][0]
        return parent.get("text", ""), thread_ts
    own = event.get("text", "")
    own = " ".join(w for w in own.split() if not w.startswith("<@")).strip()
    return (own or None), event["ts"]


def _watch_run(
    proc: subprocess.Popen,
    slug: str,
    channel: str,
    thread_ts: str,
    client,
    stop_tailer: threading.Event,
) -> None:
    """Post the exit status when the investigator finishes, then free the slot."""
    global _active_run
    code = proc.wait()
    stop_tailer.set()  # tailer drains remaining events once, then exits
    with _lock:
        _active_run = None
    if code == 0:
        _say(
            client,
            channel,
            thread_ts,
            f"Investigation finished for `{slug}` — document is ready.",
        )
    else:
        _say(
            client,
            channel,
            thread_ts,
            f"Investigation FAILED for `{slug}` (exit {code}) — "
            f"see {INCIDENTS_ROOT / slug} for what was captured.",
        )


def _handle_question(
    incident_dir: Path, question: str, channel: str, anchor: str, client
) -> None:
    """Run the Q&A agent as a subprocess (side thread — never takes the
    investigation slot) and post its answer + optional chart."""
    import tempfile

    chart_dir = Path(tempfile.mkdtemp(prefix="rca-qa-"))
    proc = subprocess.run(
        [
            sys.executable,
            str(QA_PY),
            "--incident-dir",
            str(incident_dir),
            "--question",
            question,
            "--chart-dir",
            str(chart_dir),
        ],
        capture_output=True,
        text=True,
        timeout=300,
        cwd=str(RCA_ROOT),
    )
    answer = proc.stdout.strip() or f"Q&A failed: {proc.stderr.strip()[:300]}"
    _say(client, channel, anchor, answer)
    chart = chart_dir / "chart.png"
    if chart.exists():
        client.files_upload_v2(
            channel=channel, thread_ts=anchor, file=str(chart), title="chart.png"
        )


@app.event("app_mention")
def on_mention(event, client, logger):
    global _active_run
    channel = event["channel"]

    # a mention inside a known incident's thread is a question, not an alert
    thread_ts = event.get("thread_ts")
    if thread_ts and thread_ts != event["ts"]:
        incident_dir = _incident_for_thread(thread_ts)
        if incident_dir is not None:
            question = " ".join(
                w for w in event.get("text", "").split() if not w.startswith("<@")
            ).strip()
            if not question:
                _say(
                    client,
                    channel,
                    thread_ts,
                    "Ask me something about this incident — e.g. "
                    '"what did q04 show?" or "chart the 503s over time".',
                )
                return
            _say(client, channel, thread_ts, "Looking at the record…")
            threading.Thread(
                target=_handle_question,
                args=(incident_dir, question, channel, thread_ts, client),
                daemon=True,
            ).start()
            return

    alert_text, anchor = _alert_text_for(event, client)
    if not alert_text:
        _say(
            client,
            channel,
            anchor,
            "Tag me on an alert message (in its thread), or paste the alert "
            "text with the tag.",
        )
        return

    parsed = parse_alert(alert_text)
    now = time.time()

    with _lock:
        # dedup: same alert within the window -> point at the existing run
        seen = _recent.get(parsed["dedup_key"])
        if seen and now - seen["ts"] < DEDUP_WINDOW_S:
            _say(
                client,
                channel,
                anchor,
                f"Already on it — this looks like `{seen['slug']}` "
                f"(started {int((now - seen['ts']) / 60)} min ago).",
            )
            return
        if _active_run is not None:
            _say(
                client,
                channel,
                anchor,
                f"A run is in progress (`{_active_run['slug']}`) and I take "
                f"one at a time — tag me again when it finishes.",
            )
            return
        _recent[parsed["dedup_key"]] = {
            "slug": parsed["slug"],
            "ts": now,
            "thread": anchor,
        }
        _active_run = {"slug": parsed["slug"], "thread": anchor}

    incident_dir = INCIDENTS_ROOT / parsed["slug"]
    incident_dir.mkdir(parents=True, exist_ok=True)
    alert = {
        "source": "slack-tag",
        "channel": channel,
        "thread_ts": anchor,
        "condition_guess": parsed["condition_guess"],
        "received_utc": parsed["received_utc"],
        "raw": parsed["raw"],
    }
    (incident_dir / "alert.json").write_text(json.dumps(alert, indent=2))

    cmd = [sys.executable, str(RUN_PY), "--incident-dir", str(incident_dir)]
    if DRY_RUN:
        cmd.append("--mock")
    proc = subprocess.Popen(cmd, cwd=str(RCA_ROOT))

    stop_tailer = threading.Event()
    run_dir = incident_dir / "baseline"
    threading.Thread(
        target=tail_events,
        args=(
            run_dir,
            lambda text: _say(client, channel, anchor, text),
            lambda p: client.files_upload_v2(
                channel=channel, thread_ts=anchor, file=str(p), title=p.name
            ),
            stop_tailer,
        ),
        daemon=True,
    ).start()
    threading.Thread(
        target=_watch_run,
        args=(proc, parsed["slug"], channel, anchor, client, stop_tailer),
        daemon=True,
    ).start()

    cond = f" (`{parsed['condition_guess']}`)" if parsed["condition_guess"] else ""
    mode = " [DRY RUN — mock investigator]" if DRY_RUN else ""
    _say(
        client,
        channel,
        anchor,
        f"Investigating{cond}{mode} — record at `incidents/{parsed['slug']}/`. "
        f"I'll post here as I go.",
    )
    logger.info("spawned run %s", parsed["slug"])


if __name__ == "__main__":
    mode = "DRY RUN (mock investigator)" if DRY_RUN else "live"
    print(f"daemon up [{mode}] — incidents root: {INCIDENTS_ROOT}", flush=True)
    SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"]).start()
