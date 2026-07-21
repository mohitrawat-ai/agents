"""Append one semantic event to an incident run's events.jsonl.

New code, 2026-07-18 (unit-6 ruling Q2): the agent's milestone events —
hypothesis formed/killed, timeline_settled, self_check, doc_ready — go
through this validating CLI instead of shell-echoed JSON, because the Slack
poster tails events.jsonl and one malformed line would break it. Mechanical
tool_call events land in the same file via the harness hook; harness
lifecycle events (run_started/finished/failed) via investigator/run.py.

Usage:
    python3 tools/emit.py --dir <run-dir> <event> ['<json object of fields>']

Example:
    python3 tools/emit.py --dir . hypothesis \
        '{"status": "formed", "claim": "5XXs originate at the ALB, not the app", "qids": ["q03"]}'
"""

import argparse
import fcntl
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from lf_mirror import mirror_event


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Append one event line to events.jsonl.")
    ap.add_argument("--dir", default=".", help="run dir (holds events.jsonl)")
    ap.add_argument("event", help="event name, e.g. hypothesis / timeline_settled / doc_ready")
    ap.add_argument("fields", nargs="?", default="{}",
                    help="JSON object of extra fields")
    args = ap.parse_args(argv)

    try:
        fields = json.loads(args.fields)
        if not isinstance(fields, dict):
            raise ValueError
    except (json.JSONDecodeError, ValueError):
        print(f"fields must be one JSON object, got: {args.fields[:200]}", file=sys.stderr)
        return 2

    path = Path(args.dir) / "events.jsonl"
    entry = {
        "ts": datetime.now(UTC).isoformat(timespec="seconds"),
        "event": args.event,
        **fields,
    }
    with path.open("a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.write(json.dumps(entry, default=str) + "\n")
    mirror_event(Path(args.dir), args.event, fields)
    print(f"emitted {args.event} -> {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
