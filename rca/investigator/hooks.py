"""PostToolUse tap: one tool_call row in the events table per tool call.

New code, 2026-07-18; sink moved from events.jsonl to Postgres 2026-07-21
(issue #7). This is the mechanical event layer from design-v2 D4 — the
harness observing the loop without the procedure knowing. Semantic events
(hypothesis, timeline_settled, …) are emitted by the agent via tools/emit.py
into the same table. The poller narrates milestones; tool_call rows are not
narrated (design.md §5).

Inputs are compacted per tool: a Bash call logs its command, file tools log
the path — never full file contents, which would bloat the record (rca.md
would appear twice) without helping the oncall reader.

A DB failure here is printed and swallowed: observation must not kill the
investigation mid-run (same principle as lf_mirror, design-v2 D9).
"""

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import db


def _compact(tool_name: str, tool_input: dict) -> dict:
    if tool_name == "Bash":
        return {"command": tool_input.get("command")}
    if tool_name in ("Read", "Write", "Edit", "Glob", "Grep"):
        keep = ("file_path", "pattern", "path")
        return {k: tool_input[k] for k in keep if k in tool_input}
    return {"input": json.dumps(tool_input, default=str)[:500]}


def make_post_tool_use_hook():
    """Return a PostToolUse hook writing tool_call rows for this task's
    incident and attempt (scope comes from the environment, like the tools)."""

    async def post_tool_use(input_data: dict, _tool_use_id, _context) -> dict:
        tool = input_data.get("tool_name", "?")
        payload = {"tool": tool, **_compact(tool, input_data.get("tool_input") or {})}

        def _insert() -> None:
            with db.connect() as conn:
                db.insert_event(conn, "tool_call", payload)

        try:
            # to_thread keeps blocking libpq I/O off the SDK's event loop —
            # a hung connection here must not stop the wall-clock timer.
            await asyncio.to_thread(_insert)
        except Exception as exc:  # noqa: BLE001 — observation must not kill the run
            print(f"[hooks] tool_call insert failed: {exc}", file=sys.stderr)
        return {}

    return post_tool_use
