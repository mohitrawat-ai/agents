"""Tail a run's events.jsonl and narrate milestones into the Slack thread.

New code, 2026-07-18 (unit 8). Runs as a thread inside the daemon (unit-8
ruling: the daemon already holds the Slack client and thread anchor; a
separate process would re-plumb both for no gain at one-run-at-a-time
scale). Pure reader: never writes to the run folder.

Reads in binary with a byte offset so a line written concurrently is only
processed once its newline lands (the writers append whole locked lines, but
the tailer can catch one mid-write). Only milestone events post — tool_call
volume stays in the folder (design-v2 D6). run_started is covered by the
daemon's ack; run_finished by the watcher's completion message.
"""

import json
import threading
import time
from pathlib import Path


def _format(e: dict) -> str | None:
    ev = e.get("event")
    if ev == "hypothesis":
        verb = {"formed": "Hypothesis", "supported": "Supported",
                "killed": "Ruled out"}.get(e.get("status"), "Hypothesis")
        qids = f" ({', '.join(e['qids'])})" if e.get("qids") else ""
        return f"{verb}: {e.get('claim', '')}{qids}"
    if ev == "timeline_settled":
        return f"Timeline settled:\n{e.get('summary', '')}"
    if ev == "self_check":
        return (f"Self-check: {e.get('claims_checked', '?')} claims verified, "
                f"{e.get('fixed', 0)} fixed.")
    if ev == "doc_ready":
        return f"*Verdict:* {e.get('verdict', '(missing)')}"
    if ev == "run_failed":
        return f"Run failed: {e.get('reason', 'unknown')}"
    return None  # tool_call, run_started, run_finished: not posted


def tail_events(run_dir: Path, post, upload_doc,
                stop: threading.Event, poll_s: float = 2.0) -> None:
    """Post new milestone lines until `stop` is set, then drain once and exit.

    post(text) sends a thread message; upload_doc(path) attaches a file.
    Each callback is exception-guarded: a Slack hiccup skips that post but
    never kills the tailer or the daemon.
    """
    path = run_dir / "events.jsonl"
    pos = 0
    while True:
        final_pass = stop.is_set()
        if path.exists():
            with path.open("rb") as f:
                f.seek(pos)
                for raw in f:
                    if not raw.endswith(b"\n"):
                        break  # partial write; re-read from `pos` next poll
                    pos += len(raw)
                    try:
                        event = json.loads(raw.decode())
                    except json.JSONDecodeError:
                        continue  # emit.py validates, but never die on a bad line
                    text = _format(event)
                    if text is None:
                        continue
                    try:
                        post(text)
                        if event.get("event") == "doc_ready" and \
                                (run_dir / "rca.md").exists():
                            upload_doc(run_dir / "rca.md")
                    except Exception:  # noqa: BLE001 — narration must not kill the run
                        pass
        if final_pass:
            return
        time.sleep(poll_s)
