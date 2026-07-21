"""Headless RCA investigator: incident row in -> record out.

New code, 2026-07-18. Reworked 2026-07-21 (issue #7; §8a-A/B, P6 §5b, P7):
input is {incident_id} — INCIDENT_ID and ATTEMPT come from the task
environment, `raw` is read from Postgres, and alert.json is materialized in
a private workdir. D3's seam holds at the agent boundary: the agent still
sees ../alert.json in, record out. Lifecycle events land in the events
table; events.jsonl is gone. This is the one SDK-coupled box — swapping
harnesses later means rewriting this file and hooks.py, nothing else.

Exit codes (the retry contract Step Functions keys on, #9):
    0  run finished
    1  agent loop ended in error       (poison class — never retried)
    2  startup: bad environment, unreachable DB, or missing incident row
    3  wall-clock cap hit              (infrastructure tier)
    4  budget cap hit                  (policy stop — never retried)
    5  unexpected exception, incl. error_during_execution (infra tier)

A retry restarts from zero under attempt+1 and pays the full run again;
the attempt column keeps the two row sets apart (§8a-B). Resume-from-
where-it-stopped is ruled deferred with named triggers — see §8a-B before
building it.

Usage (env supplies scope, exactly like the tools):
    INCIDENT_ID=<uuid> ATTEMPT=1 uv run --env-file .env \
        python investigator/run.py [--max-turns 150] [--max-minutes 60] \
        [--max-budget-usd 5.0] [--model claude-sonnet-5] [--mock]
"""

import argparse
import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path

from claude_agent_sdk import ClaudeAgentOptions, HookMatcher, query
from hooks import make_post_tool_use_hook, make_pre_tool_use_hook

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import db

TOOLS_DIR = Path(__file__).resolve().parents[1] / "tools"
PROCEDURE = Path(__file__).resolve().parent / "procedure.md"

ALLOWED_TOOLS = ["Bash", "Read", "Write", "Glob", "Grep"]

EXIT_OK = 0
EXIT_AGENT_ERROR = 1
EXIT_STARTUP = 2
EXIT_WALL_CLOCK = 3
EXIT_BUDGET = 4
EXIT_EXCEPTION = 5


def emit_best_effort(event: str, **fields) -> None:
    """Terminal events must not mask the exit code: print-and-continue on a
    DB failure. Step Functions' view of the terminal state is the exit code
    (P8 §5); this row is for the poller's narration."""
    try:
        with db.connect() as conn:
            db.insert_event(conn, event, fields)
    except Exception as exc:  # noqa: BLE001 — the exit code is the authority
        print(f"[run] {event} insert failed: {exc}", file=sys.stderr)


async def run(
    run_dir: Path,
    alert_text: str,
    args: argparse.Namespace,
    config_dir: str,
):
    system_prompt = PROCEDURE.read_text().replace("__TOOLS_DIR__", str(TOOLS_DIR))

    options = ClaudeAgentOptions(
        system_prompt=system_prompt,
        cwd=str(run_dir),
        allowed_tools=ALLOWED_TOOLS,
        max_turns=args.max_turns,
        max_budget_usd=args.max_budget_usd,
        model=args.model,
        # the CLI subprocess writes transcripts under CLAUDE_CONFIG_DIR; a
        # private dir outside the workdir keeps them out of the agent's
        # Read reach (design.md §7, enforced fully in #8)
        env={**os.environ, "CLAUDE_CONFIG_DIR": config_dir},
        setting_sources=[],  # nothing leaks in from ~/.claude or project settings
        hooks={
            "PreToolUse": [
                HookMatcher(
                    matcher="*",
                    hooks=[make_pre_tool_use_hook(run_dir, run_dir.parent, TOOLS_DIR)],
                )
            ],
            "PostToolUse": [
                HookMatcher(matcher="*", hooks=[make_post_tool_use_hook()])
            ],
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


def mock_run(run_dir: Path) -> int:
    """No LLM: write stub outputs through the same record contract, for
    plumbing tests. Exercises incident fetch, event inserts, and the
    document upload — a free end-to-end of everything but the agent."""
    with db.connect() as conn:
        db.insert_event(
            conn,
            "hypothesis",
            {
                "status": "formed",
                "claim": "MOCK: plumbing test, no investigation performed",
                "qids": [],
            },
        )
        (run_dir / "rca.md").write_text(
            "# MOCK RUN — plumbing test\n\n**Verdict:** No investigation "
            "was performed; this run verifies the task → record pipeline only.\n"
        )
        with conn.transaction():
            db.insert_event(
                conn, "doc_ready", {"verdict": "mock run, no investigation"}
            )
            db.insert_document(conn, "rca.md", (run_dir / "rca.md").read_text())
        db.insert_event(
            conn, "run_finished", {"elapsed_s": 0, "turns": 0, "cost_usd": 0.0}
        )
    print("run_finished: mock")
    return EXIT_OK


def main() -> int:
    ap = argparse.ArgumentParser(description="Run one headless RCA investigation.")
    ap.add_argument("--max-turns", type=int, default=150)
    ap.add_argument("--max-minutes", type=int, default=60)
    # 2.5x the one scored run ($2.03); a stop against runaway loops, not a
    # quality target (P6 §5b). Tune on data.
    ap.add_argument("--max-budget-usd", type=float, default=5.0)
    # implementation-phase default; switch to claude-opus-4-8 at go-live (§8b)
    ap.add_argument("--model", default="claude-sonnet-5")
    ap.add_argument(
        "--mock",
        action="store_true",
        help="no LLM: stub outputs through the same record contract",
    )
    args = ap.parse_args()

    try:
        incident_id, attempt = db.scope()
        with db.connect() as conn:
            slug, raw = db.get_incident(conn)
    except SystemExit as e:
        print(f"startup: {e}", file=sys.stderr)
        return EXIT_STARTUP
    except Exception as e:  # noqa: BLE001 — startup must be distinguishable (#9)
        print(f"startup: {e!r}", file=sys.stderr)
        return EXIT_STARTUP

    workdir = Path(tempfile.gettempdir()) / f"rca-{slug}-{incident_id[:8]}-a{attempt}"
    run_dir = workdir / "run"
    run_dir.mkdir(parents=True, exist_ok=True)
    alert_text = json.dumps(raw, indent=2, ensure_ascii=False)
    (workdir / "alert.json").write_text(alert_text)

    try:
        with db.connect() as conn:
            db.insert_event(
                conn,
                "run_started",
                {
                    "model": "mock" if args.mock else args.model,
                    "max_turns": args.max_turns,
                    "max_minutes": args.max_minutes,
                    "max_budget_usd": args.max_budget_usd,
                },
            )
    except Exception as e:  # noqa: BLE001 — no ack row means the run is invisible
        print(f"startup: run_started insert failed: {e!r}", file=sys.stderr)
        return EXIT_STARTUP

    try:
        if args.mock:
            return mock_run(run_dir)
        config_dir = tempfile.mkdtemp(prefix="rca-claude-config-")
    except Exception as e:  # noqa: BLE001 — a crash after run_started needs a row
        emit_best_effort(
            "run_failed", reason=repr(e)[:500], exit_code=EXIT_EXCEPTION, elapsed_s=0
        )
        print(f"run_failed: {e!r}", file=sys.stderr)
        return EXIT_EXCEPTION

    t0 = time.time()
    try:
        result = asyncio.run(
            asyncio.wait_for(
                run(run_dir, alert_text, args, config_dir),
                timeout=args.max_minutes * 60,
            )
        )
    except TimeoutError:
        emit_best_effort(
            "run_failed",
            reason=f"wall-clock cap {args.max_minutes}min hit",
            exit_code=EXIT_WALL_CLOCK,
            elapsed_s=round(time.time() - t0),
        )
        print(f"run_failed: wall-clock cap {args.max_minutes}min", file=sys.stderr)
        return EXIT_WALL_CLOCK
    except Exception as e:  # noqa: BLE001 — any crash must land in the record (D8)
        # The CLI emits its error result (budget, max turns, …) and then
        # exits non-zero on purpose; the SDK surfaces that as a plain
        # Exception carrying the result text, not as a ResultMessage. So
        # terminal classification happens here, on the preserved text.
        text = str(e)
        if "maximum budget" in text.lower():
            code, reason = EXIT_BUDGET, f"budget cap ${args.max_budget_usd:.2f} hit"
        elif "error_during_execution" in text:
            # the CLI's graceful mid-run failure bucket, incl. transient API
            # errors — infra tier, retried (ruled 2026-07-21, invariant 6:
            # one extra $2 attempt on true poison beats a dropped incident)
            code, reason = EXIT_EXCEPTION, f"execution error: {text[:300]}"
        elif "returned an error result" in text:
            code, reason = EXIT_AGENT_ERROR, f"agent loop ended in error: {text[:300]}"
        else:
            code, reason = EXIT_EXCEPTION, repr(e)[:500]
        emit_best_effort(
            "run_failed",
            reason=reason,
            exit_code=code,
            elapsed_s=round(time.time() - t0),
        )
        print(f"run_failed: {reason}", file=sys.stderr)
        return code

    elapsed = round(time.time() - t0)
    cost = getattr(result, "total_cost_usd", None)
    turns = getattr(result, "num_turns", None)
    subtype = getattr(result, "subtype", None)
    is_error = getattr(result, "is_error", result is None)

    if subtype == "error_max_budget_usd":
        emit_best_effort(
            "run_failed",
            reason=f"budget cap ${args.max_budget_usd:.2f} hit",
            exit_code=EXIT_BUDGET,
            elapsed_s=elapsed,
            turns=turns,
            cost_usd=cost,
        )
        print(f"run_failed: budget cap ${args.max_budget_usd:.2f}", file=sys.stderr)
        return EXIT_BUDGET

    if is_error:
        emit_best_effort(
            "run_failed",
            reason=f"agent loop ended in error (subtype={subtype})",
            exit_code=EXIT_AGENT_ERROR,
            elapsed_s=elapsed,
            turns=turns,
            cost_usd=cost,
        )
        print(f"run_failed after {elapsed}s, {turns} turns", file=sys.stderr)
        return EXIT_AGENT_ERROR

    emit_best_effort("run_finished", elapsed_s=elapsed, turns=turns, cost_usd=cost)
    print(
        f"run_finished: {elapsed}s, {turns} turns, ${cost:.4f}"
        if cost is not None
        else f"run_finished: {elapsed}s, {turns} turns"
    )
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
