"""Insert one semantic event into the incident's Postgres record.

New code, 2026-07-18 (unit-6 ruling Q2): the agent's milestone events —
hypothesis formed/killed, timeline_settled, self_check, doc_ready — go
through this validating CLI instead of shell-echoed JSON, because the Slack
poller reads events and one malformed line would break it. Mechanical
tool_call events land via the harness hook; harness lifecycle events
(run_started/finished/failed) via investigator/run.py.

Sink moved from events.jsonl to Postgres 2026-07-21 (issue #4, P2 §1). Same
CLI surface. One addition, ruled in design.md §5: on doc_ready this CLI also
uploads the run dir's rca.md into the documents table — emit does the
insert, not the agent, which keeps the tool boundary (invariant 2) true for
documents as well.

Usage:
    python3 tools/emit.py --dir <run-dir> <event> ['<json object of fields>']

Example:
    python3 tools/emit.py --dir . hypothesis \
        '{"status": "formed", "claim": "5XXs are ALB-side", "qids": ["q03"]}'
"""

import argparse
import json
import sys
from pathlib import Path

from lf_mirror import mirror_event

import db


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Insert one event into the record.")
    ap.add_argument("--dir", default=".", help="run dir (holds rca.md on doc_ready)")
    ap.add_argument(
        "event", help="event name, e.g. hypothesis / timeline_settled / doc_ready"
    )
    ap.add_argument(
        "fields", nargs="?", default="{}", help="JSON object of extra fields"
    )
    args = ap.parse_args(argv)

    try:
        fields = json.loads(args.fields)
        if not isinstance(fields, dict):
            raise ValueError
    except (json.JSONDecodeError, ValueError):
        print(
            f"fields must be one JSON object, got: {args.fields[:200]}", file=sys.stderr
        )
        return 2

    doc = None
    if args.event == "doc_ready":
        doc_path = Path(args.dir) / "rca.md"
        if not doc_path.is_file():
            print(f"doc_ready but no rca.md at {doc_path}", file=sys.stderr)
            return 2
        doc = doc_path.read_text()

    with db.connect() as conn, conn.transaction():
        db.insert_event(conn, args.event, fields)
        if doc is not None:
            db.insert_document(conn, "rca.md", doc)

    mirror_event(Path(args.dir), args.event, fields)
    print(f"emitted {args.event} -> postgres")
    if doc is not None:
        print("uploaded rca.md -> documents")
    return 0


if __name__ == "__main__":
    sys.exit(main())
