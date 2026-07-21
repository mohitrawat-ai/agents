"""Q&A over one incident's record: (question, folder) -> answer [+ chart].

New code, 2026-07-18 (unit 9, design-v2 D7). Slack-free on purpose — the
daemon wraps this for threads today, the product chatbot wraps the same CLI
later. Read-only over the incident folder: it explains, summarizes, and
charts what the investigation captured; it never runs fresh telemetry
queries (the designed-for upgrade) and never writes into the record. A
chart, if one genuinely helps, lands in --chart-dir as chart.png.

Usage:
    uv run python qa/agent.py --incident-dir <dir> [--variant baseline] \
        --question "..." [--chart-dir <tmp dir>]
Prints the answer to stdout.
"""

import argparse
import asyncio
import sys
from pathlib import Path

from claude_agent_sdk import ClaudeAgentOptions, query

SYSTEM_PROMPT = """You answer an on-call engineer's question about ONE production
incident, using only this incident's investigation record — you are in the
run's folder:

- ../alert.json — the alert as received
- rca.md — the investigation's document (may not exist yet if the run is live)
- events.jsonl — what the investigator did and concluded, timestamped
- queries.jsonl — every telemetry query with its full results; ids (q01...)
  are the citation currency

Rules:
- Answer from the record only. If the record doesn't contain the answer, say
  exactly that — plainly, naming what WAS captured that comes closest. Never
  guess beyond the data, never run new telemetry queries.
- Cite query ids like [q07] for every number you quote.
- NEVER modify anything in this folder or its parent. Read-only.
- Keep answers short and operational — Slack-thread sized, not documents.
- A chart is optional: only if the question is really asking about a shape
  over time (a spike, a trend). Build it from queries.jsonl result rows with
  the matplotlib available at the python interpreter given in the prompt,
  save ONLY to the chart path given in the prompt, and mention in your answer
  that the chart shows X. No chart for questions a sentence answers."""


async def answer(run_dir: Path, question: str, chart_dir: Path | None) -> str:
    chart_note = (
        f"\nIf a chart helps: save it as {chart_dir / 'chart.png'} using "
        f"this interpreter (it has matplotlib): {sys.executable}"
        if chart_dir
        else "\nDo not produce a chart (no chart dir available)."
    )
    options = ClaudeAgentOptions(
        system_prompt=SYSTEM_PROMPT,
        cwd=str(run_dir),
        allowed_tools=["Read", "Grep", "Glob", "Bash"],
        max_turns=20,
        model="claude-sonnet-5",
        setting_sources=[],
    )
    result = None
    async for message in query(prompt=question + chart_note, options=options):
        if type(message).__name__ == "ResultMessage":
            result = message
    return getattr(result, "result", None) or "(no answer produced)"


def main() -> int:
    ap = argparse.ArgumentParser(description="Answer one question about one incident.")
    ap.add_argument("--incident-dir", required=True)
    ap.add_argument("--variant", default="baseline")
    ap.add_argument("--question", required=True)
    ap.add_argument(
        "--chart-dir",
        default=None,
        help="writable dir OUTSIDE the record for an optional chart.png",
    )
    args = ap.parse_args()

    run_dir = Path(args.incident_dir).resolve() / args.variant
    if not run_dir.exists():
        print(f"no {args.variant}/ run in {args.incident_dir}", file=sys.stderr)
        return 2
    chart_dir = Path(args.chart_dir).resolve() if args.chart_dir else None
    if chart_dir:
        chart_dir.mkdir(parents=True, exist_ok=True)

    print(asyncio.run(answer(run_dir, args.question, chart_dir)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
