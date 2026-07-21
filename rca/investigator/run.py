"""Headless RCA investigator: alert.json in -> incident folder out.

New code, 2026-07-18. The one SDK-coupled box from design-v2 D3: everything
downstream (Slack poster, Q&A agent) reads only the incident folder this run
produces — queries.jsonl (receipts), events.jsonl (what happened), and the
documents the procedure writes. Swapping harnesses later means rewriting
this file and hooks.py, nothing else.

Usage:
    uv run python investigator/run.py --incident-dir <dir-containing-alert.json> \
        [--max-turns 150] [--max-minutes 60] [--model claude-opus-4-8]
"""

import argparse
import asyncio
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from claude_agent_sdk import ClaudeAgentOptions, HookMatcher, query
from hooks import make_post_tool_use_hook

RCA_ROOT = Path(__file__).resolve().parents[1]  # prod/agents/rca
TOOLS_DIR = RCA_ROOT / "tools"
PROCEDURE = Path(__file__).resolve().parent / "procedure.md"

ALLOWED_TOOLS = ["Bash", "Read", "Write", "Glob", "Grep"]


def emit(events_path: Path, event: str, **fields) -> None:
    """Harness-level event line (run_started / run_finished / run_failed)."""
    entry = {
        "ts": datetime.now(UTC).isoformat(timespec="seconds"),
        "event": event,
        **fields,
    }
    with events_path.open("a") as f:
        f.write(json.dumps(entry, default=str) + "\n")


async def run(run_dir: Path, alert_text: str, max_turns: int, model: str):
    system_prompt = PROCEDURE.read_text().replace("__TOOLS_DIR__", str(TOOLS_DIR))
    events_path = run_dir / "events.jsonl"

    options = ClaudeAgentOptions(
        system_prompt=system_prompt,
        cwd=str(run_dir),
        allowed_tools=ALLOWED_TOOLS,
        max_turns=max_turns,
        model=model,
        setting_sources=[],  # nothing leaks in from ~/.claude or project settings
        hooks={
            "PostToolUse": [
                HookMatcher(matcher="*", hooks=[make_post_tool_use_hook(events_path)])
            ]
        },
    )

    prompt = (
        "Investigate the alert at ../alert.json (contents below), "
        "following your procedure exactly.\n\nAlert:\n" + alert_text
    )

    result = None
    async for message in query(prompt=prompt, options=options):
        if type(message).__name__ == "ResultMessage":
            result = message
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description="Run one headless RCA investigation.")
    ap.add_argument(
        "--incident-dir",
        required=True,
        help="incident root folder; must contain alert.json",
    )
    # D5: variant runs are siblings sharing alert.json
    ap.add_argument("--variant", default="baseline", help="run subfolder name")
    ap.add_argument("--max-turns", type=int, default=150)
    ap.add_argument("--max-minutes", type=int, default=60)
    # implementation-phase default; switch to claude-opus-4-8 when the daemon goes live
    ap.add_argument("--model", default="claude-sonnet-5")
    ap.add_argument(
        "--mock",
        action="store_true",
        help="no LLM: write stub outputs through the same folder "
        "contract, for daemon/plumbing tests",
    )
    args = ap.parse_args()

    incident_dir = Path(args.incident_dir).resolve()
    if not (incident_dir / "alert.json").exists():
        print(f"no alert.json in {incident_dir}", file=sys.stderr)
        return 2
    alert_text = (incident_dir / "alert.json").read_text()
    run_dir = incident_dir / args.variant
    run_dir.mkdir(parents=True, exist_ok=True)

    events_path = run_dir / "events.jsonl"
    emit(
        events_path,
        "run_started",
        model=("mock" if args.mock else args.model),
        max_turns=args.max_turns,
        max_minutes=args.max_minutes,
    )

    if args.mock:
        time.sleep(3)  # let the watcher thread be observably async
        emit(
            events_path,
            "hypothesis",
            status="formed",
            claim="MOCK: plumbing test, no investigation performed",
            qids=[],
        )
        (run_dir / "rca.md").write_text(
            "# MOCK RUN — plumbing test\n\n**Verdict:** No investigation "
            "was performed; this run verifies the daemon → investigator → "
            "folder pipeline only.\n"
        )
        emit(events_path, "doc_ready", verdict="mock run, no investigation")
        emit(events_path, "run_finished", elapsed_s=3, turns=0, cost_usd=0.0)
        print("run_finished: mock")
        return 0

    t0 = time.time()
    try:
        result = asyncio.run(
            asyncio.wait_for(
                run(run_dir, alert_text, args.max_turns, args.model),
                timeout=args.max_minutes * 60,
            )
        )
    except TimeoutError:
        emit(
            events_path,
            "run_failed",
            reason=f"wall-clock cap {args.max_minutes}min hit",
            elapsed_s=round(time.time() - t0),
        )
        print(f"run_failed: wall-clock cap {args.max_minutes}min", file=sys.stderr)
        return 1
    except Exception as e:  # noqa: BLE001 — any crash must land in events.jsonl (D8: loud, never silent)
        emit(
            events_path,
            "run_failed",
            reason=repr(e)[:500],
            elapsed_s=round(time.time() - t0),
        )
        print(f"run_failed: {e}", file=sys.stderr)
        return 1

    elapsed = round(time.time() - t0)
    cost = getattr(result, "total_cost_usd", None)
    turns = getattr(result, "num_turns", None)
    is_error = getattr(result, "is_error", result is None)

    if is_error:
        emit(
            events_path,
            "run_failed",
            reason="agent loop ended in error",
            elapsed_s=elapsed,
            turns=turns,
            cost_usd=cost,
        )
        print(f"run_failed after {elapsed}s, {turns} turns", file=sys.stderr)
        return 1

    emit(events_path, "run_finished", elapsed_s=elapsed, turns=turns, cost_usd=cost)
    print(
        f"run_finished: {elapsed}s, {turns} turns, ${cost:.4f}"
        if cost is not None
        else f"run_finished: {elapsed}s, {turns} turns"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
