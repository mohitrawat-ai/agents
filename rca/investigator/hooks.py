"""Hooks: the PreToolUse boundary, and the PostToolUse tool_call tap.

PostToolUse: new 2026-07-18; sink moved from events.jsonl to Postgres
2026-07-21 (issue #7). The mechanical event layer from design-v2 D4 — the
harness observing the loop without the procedure knowing. Inputs are
compacted per tool: a Bash call logs its command, file tools log the path —
never full file contents. A DB failure here is printed and swallowed:
observation must not kill the investigation mid-run (design-v2 D9).

PreToolUse: new 2026-07-21 (issue #8, P5 §1-§2, invariant 4). Allowlists
the EXECUTABLE, not the command string — command-string filtering is
bypassable by eval, base64, `python3 -c`, and variable indirection;
requiring token zero to be python3 and token one to resolve to one of the
four tool scripts denies those by construction. Shell operators, command
substitution, and env-prefixed invocations (`INCIDENT_ID=other python3 …`,
P9 §5's residual) all fail the same parse. Read/Glob/Grep are confined to
the workdir and tools dir after resolve() (symlinks can't escape); Write
and Edit are confined to the workdir plus the two NOTES files — writable
tool scripts would make the executable allowlist worthless.
"""

import asyncio
import json
import shlex
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import db

_BASH_OPERATORS = set("|&;<>()")


def _resolve(path_str: str, cwd: Path) -> Path:
    p = Path(path_str)
    return (p if p.is_absolute() else cwd / p).resolve()


def _under(path: Path, roots: list[Path]) -> bool:
    return any(path == r or r in path.parents for r in roots)


def check_bash_command(
    command: object, allowed_scripts: set[Path], cwd: Path
) -> str | None:
    """Return a denial reason, or None if the command is exactly one allowed
    `python3 <tool>.py …` invocation with no shell tricks.

    The allowlist secures the executable and token one; the args after it are
    trusted to reach one of the four tool scripts. That is only safe because
    none of them pass agent input to a shell — aws_log runs a list-form
    subprocess gated by check_read_only, the rest never shell out. If that
    ever changes, this boundary changes with it.
    """
    if not isinstance(command, str):
        return f"command is not a string: {type(command).__name__}"
    # shlex treats a newline as whitespace; bash treats it as a command
    # separator, so a second command on line two would pass the token[0]/[1]
    # gate and run unchecked (issue #8 review). NRQL/purpose must be single
    # line — no real cost, NRQL is whitespace-insensitive.
    if "\n" in command or "\r" in command:
        return "newlines are not allowed in a command"
    try:
        lex = shlex.shlex(command, posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        tokens = list(lex)
    except ValueError as e:
        return f"unparseable command: {e}"
    if not tokens:
        return "empty command"
    for t in tokens:
        if t and t[0] in _BASH_OPERATORS:
            return f"shell operator '{t}' is not allowed"
        if "$(" in t or "`" in t:  # redundant with '(' above; kept as a wall
            return "command substitution is not allowed"
    if tokens[0] != "python3" and not tokens[0].endswith("/python3"):
        return f"only the python3 tool invocations are allowed, got '{tokens[0]}'"
    if len(tokens) < 2:
        return "missing tool script path"
    script = _resolve(tokens[1], cwd)
    if script not in allowed_scripts:
        return f"'{tokens[1]}' is not an allowed tool"
    return None


def check_path(
    path_str: str | None, roots: list[Path], cwd: Path, extra_files: set[Path]
) -> str | None:
    """Return a denial reason unless path resolves under an allowed root
    (or is an explicitly allowed file). Missing path = the cwd: allowed."""
    if not path_str:
        return None
    p = _resolve(path_str, cwd)
    if p in extra_files or _under(p, roots):
        return None
    return f"'{path_str}' is outside the allowed directories"


def make_pre_tool_use_hook(run_dir: Path, workdir: Path, tools_dir: Path):
    """Return the PreToolUse boundary bound to one run's directories."""
    tools_dir = tools_dir.resolve()
    allowed_scripts = {
        tools_dir / "newrelic" / "nrql_log.py",
        tools_dir / "cloudwatch" / "aws_log.py",
        tools_dir / "emit.py",
        tools_dir / "read_record.py",
    }
    read_roots = [workdir.resolve(), tools_dir]
    write_roots = [workdir.resolve()]
    notes_files = {
        tools_dir / "newrelic" / "NR_NOTES.md",
        tools_dir / "cloudwatch" / "CW_NOTES.md",
    }
    cwd = run_dir.resolve()

    def _deny(reason: str) -> dict:
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }

    async def pre_tool_use(input_data: dict, _tool_use_id, _context) -> dict:
        tool = input_data.get("tool_name", "?")
        ti = input_data.get("tool_input") or {}
        try:
            if tool == "Bash":
                reason = check_bash_command(ti.get("command", ""), allowed_scripts, cwd)
            elif tool == "Read":
                reason = check_path(ti.get("file_path"), read_roots, cwd, set())
            elif tool in ("Glob", "Grep"):
                reason = check_path(ti.get("path"), read_roots, cwd, set())
                pattern = ti.get("pattern", "") if tool == "Glob" else ""
                if reason is None and (pattern.startswith("/") or ".." in pattern):
                    reason = "glob patterns may not leave the allowed directories"
            elif tool in ("Write", "Edit"):
                reason = check_path(ti.get("file_path"), write_roots, cwd, notes_files)
            else:
                reason = f"tool '{tool}' is not allowed"
        except Exception as exc:  # noqa: BLE001 — a boundary that raises fails closed
            reason = f"boundary check raised, denying: {exc!r}"
        if reason is not None:
            print(f"[boundary] denied {tool}: {reason}", file=sys.stderr)
            return _deny(reason)
        return {}

    return pre_tool_use


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
