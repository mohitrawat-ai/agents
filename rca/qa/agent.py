"""Q&A over one incident's record: (incident, question) -> (answer, cost).

Rewritten 2026-07-22 (issue #12, §8a-C). Replaces the 2026-07-18 folder
version: no export, no incident tmpdir, no chart files (ASCII charts in
answer text are fine — ruled 2026-07-23). `rca.md` is inlined in
the system prompt, evidence comes back through tools/read_record.py, and
the agent's tool surface is `Bash` allowlisted to that one executable —
no Read, no Write (§8a-C, P5's injection threat model).

Scope (INCIDENT_ID, ATTEMPT) rides the environment of the CLI subprocess
the SDK spawns — never agent input (P9 §5). Every SLACK_* variable is
overridden to empty in that env: the Service process can speak as the
bot, its Q&A subprocess must not. Override, not omission — the SDK merges
options.env ONTO the inherited os.environ, so omitting a key removes
nothing (#12 review, subprocess_cli.py process_env).

Slack-free on purpose — service/router.py wraps this for threads. Manual
run (scope from env, like the tools):
    INCIDENT_ID=<uuid> uv run --env-file .env python qa/agent.py \
        --question "..."
"""

import argparse
import asyncio
import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

import psycopg
from claude_agent_sdk import ClaudeAgentOptions, HookMatcher, query

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from investigator.hooks import check_bash_command

TOOLS_DIR = Path(__file__).resolve().parents[1] / "tools"
READ_CLI = TOOLS_DIR / "read_record.py"

MODEL = "claude-sonnet-5"  # carried over from the folder version, ruled 2026-07-22
MAX_TURNS = 20

SYSTEM_PROMPT = """You answer an on-call engineer's question about ONE production
incident, using only this incident's investigation record.

The investigation document (rca.md) is inlined below. The evidence — every
telemetry query the investigation ran, with full results — is in the record;
read it with:

    python3 read_record.py --list      (one line per query)
    python3 read_record.py --qid q07   (full row for one qid)

Rules:
- Answer from the record only. If the record doesn't contain the answer, say
  exactly that — plainly, naming what WAS captured that comes closest. Never
  guess beyond the data, never run new telemetry queries.
- Cite query ids like [q07] for every number you quote.
- Keep answers short and operational — Slack-thread sized, not documents.
- If the question asks for a chart, trend, or distribution, draw a compact
  ASCII chart inside a ``` code block, built only from numbers in the record:
  labeled bars or a sparkline, at most ~50 characters wide, units and qids
  stated. Never interpolate points the record does not contain.
- If the question asks for a timeline or sequence of events, draw the
  milestones on an ASCII time axis in a ``` code block — one tick per event,
  timestamp and a short label each, in time order. Only events the record
  states; cite qids where the event came from evidence.

--- rca.md ---
"""

NO_DOC = (
    "(rca.md is not in the record yet — the investigation is still running "
    "or did not finish. Answer from the evidence queries only, and say the "
    "document isn't ready.)"
)

_RCA_MD = """\
SELECT attempt, content FROM documents
 WHERE incident_id = %s AND name = 'rca.md'
 ORDER BY attempt DESC LIMIT 1
"""

_NEWEST_ATTEMPT = "SELECT COALESCE(MAX(attempt), 1) FROM queries WHERE incident_id = %s"

_config_dir: str | None = None


def _config() -> str:
    """One private CLAUDE_CONFIG_DIR per Service process (transcripts land
    there; the agent has no Read, so per-question isolation buys nothing)."""
    global _config_dir
    if _config_dir is None:
        _config_dir = tempfile.mkdtemp(prefix="rca-qa-claude-config-")
    return _config_dir


@dataclass
class Answer:
    text: str
    attempt: int
    cost_usd: float | None
    turns: int | None


def make_qa_pre_tool_use_hook():
    """Bash may invoke only read_record.py; every other tool is denied.
    Reuses the investigator's executable-allowlist parser (issue #8) —
    same shell-trick denials, a one-script allowlist."""
    allowed = {READ_CLI.resolve()}
    cwd = TOOLS_DIR.resolve()

    async def pre_tool_use(input_data: dict, _tool_use_id, _context) -> dict:
        tool = input_data.get("tool_name", "?")
        try:
            if tool == "Bash":
                command = (input_data.get("tool_input") or {}).get("command", "")
                reason = check_bash_command(command, allowed, cwd)
            else:
                reason = f"tool '{tool}' is not allowed"
        except Exception as exc:  # noqa: BLE001 — a boundary that raises fails closed
            reason = f"boundary check raised, denying: {exc!r}"
        if reason is not None:
            print(f"[qa boundary] denied {tool}: {reason}", file=sys.stderr)
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                }
            }
        return {}

    return pre_tool_use


def fetch_record(conn: psycopg.Connection, incident_id: str) -> tuple[str, int]:
    """Return (rca.md content or the NO_DOC note, attempt to scope reads to).
    The document's attempt wins; with no document yet, the newest attempt
    that produced evidence."""
    row = conn.execute(_RCA_MD, (incident_id,)).fetchone()
    if row is not None:
        attempt, content = row[0], row[1]
        return content, attempt
    attempt = conn.execute(_NEWEST_ATTEMPT, (incident_id,)).fetchone()[0]
    return NO_DOC, attempt


def subprocess_env(incident_id: str, attempt: int) -> dict[str, str]:
    """The env overrides for the SDK's CLI subprocess. SLACK_* must be
    overridden to "" — the SDK merges these ONTO the inherited environment
    (subprocess_cli.py: {**inherited_env, **options.env}), so a key left
    out of this dict is inherited, not removed (#12 review)."""
    env = {k: "" for k in os.environ if k.startswith("SLACK_")}
    env.update(
        {
            "INCIDENT_ID": incident_id,
            "ATTEMPT": str(attempt),
            "CLAUDE_CONFIG_DIR": _config(),
        }
    )
    return env


async def answer(conn: psycopg.Connection, incident_id: str, question: str) -> Answer:
    rca_md, attempt = fetch_record(conn, incident_id)
    env = subprocess_env(incident_id, attempt)
    options = ClaudeAgentOptions(
        system_prompt=SYSTEM_PROMPT + rca_md,
        cwd=str(TOOLS_DIR),
        allowed_tools=["Bash"],
        max_turns=MAX_TURNS,
        model=MODEL,
        env=env,
        setting_sources=[],  # nothing leaks in from ~/.claude or project settings
        hooks={
            "PreToolUse": [
                HookMatcher(matcher="*", hooks=[make_qa_pre_tool_use_hook()])
            ],
        },
    )

    result = None
    async for message in query(prompt=question, options=options):
        if type(message).__name__ == "ResultMessage":
            result = message
    return Answer(
        text=getattr(result, "result", None) or "(no answer produced)",
        attempt=attempt,
        cost_usd=getattr(result, "total_cost_usd", None),
        turns=getattr(result, "num_turns", None),
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="Answer one question about one incident.")
    ap.add_argument("--question", required=True)
    args = ap.parse_args()

    incident_id = os.environ.get("INCIDENT_ID")
    dsn = os.environ.get("RCA_DATABASE_URL")
    if not incident_id or not dsn:
        print("INCIDENT_ID and RCA_DATABASE_URL must be set", file=sys.stderr)
        return 2

    with psycopg.connect(dsn, autocommit=True, connect_timeout=10) as conn:
        out = asyncio.run(answer(conn, incident_id, args.question))
    print(out.text)
    print(
        f"[qa] attempt {out.attempt}, {out.turns} turns, cost ${out.cost_usd}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
