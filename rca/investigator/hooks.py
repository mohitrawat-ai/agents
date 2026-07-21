"""PostToolUse tap: append one line per tool call to the incident's events.jsonl.

New code, 2026-07-18. This is the mechanical event layer from design-v2 D4 —
the harness observing the loop without the procedure knowing. Semantic events
(hypothesis formed/killed, timeline settled) are emitted by the agent itself
per its procedure, into the same file. Later this same tap grows Langfuse
OTLP spans (D9) with zero procedure changes.

Inputs are compacted per tool: a Bash call logs its command, file tools log
the path — never full file contents, which would bloat the log (rca.md would
appear twice) without helping the oncall reader.
"""

import json
from datetime import UTC, datetime
from pathlib import Path


def _compact(tool_name: str, tool_input: dict) -> dict:
    if tool_name == "Bash":
        return {"command": tool_input.get("command")}
    if tool_name in ("Read", "Write", "Edit", "Glob", "Grep"):
        keep = ("file_path", "pattern", "path")
        return {k: tool_input[k] for k in keep if k in tool_input}
    return {"input": json.dumps(tool_input, default=str)[:500]}


def make_post_tool_use_hook(events_path: Path):
    """Return a PostToolUse hook bound to one incident's events.jsonl."""

    async def post_tool_use(input_data: dict, tool_use_id, context) -> dict:
        tool = input_data.get("tool_name", "?")
        entry = {
            "ts": datetime.now(UTC).isoformat(timespec="seconds"),
            "event": "tool_call",
            "tool": tool,
            **_compact(tool, input_data.get("tool_input") or {}),
        }
        with events_path.open("a") as f:
            f.write(json.dumps(entry, default=str) + "\n")
        return {}

    return post_tool_use
