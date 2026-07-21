"""Read the incident's evidence back from Postgres.

New for issue #4 (design.md §8c): once the tools' sink moved to Postgres
there is no queries.jsonl for the self-check pass to verify against, so
evidence is read back through this CLI. It is the read half of the tool
boundary — the same choke point as the writers, in the other direction.
#12's Q&A reuses it unchanged.

Scope comes from the task environment (INCIDENT_ID, ATTEMPT), never from
agent input: an --incident flag is accepted and ignored (§8a-C, P9 §5).

Usage:
    python3 tools/read_record.py --list
    python3 tools/read_record.py --qid q03
"""

import argparse
import json
import sys

import db

_LIST = """\
SELECT qid, source, elapsed_s, rows, error IS NOT NULL AS failed, purpose
  FROM queries
 WHERE incident_id = %s AND attempt = %s
 ORDER BY id
"""

_ONE = """\
SELECT qid, source, purpose, query, elapsed_s, rows, result, error
  FROM queries
 WHERE incident_id = %s AND attempt = %s AND qid = %s
"""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Read evidence rows back.")
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--list", action="store_true", help="one line per query")
    group.add_argument("--qid", help="full row for one qid")
    ap.add_argument(
        "--incident", help="ignored; scope comes from the task environment"
    )
    args = ap.parse_args(argv)

    if args.incident:
        print(
            "--incident is ignored; scope comes from the task environment",
            file=sys.stderr,
        )

    incident_id, attempt = db.scope()
    with db.connect() as conn:
        if args.list:
            rows = conn.execute(_LIST, (incident_id, attempt)).fetchall()
            for qid, source, elapsed_s, n, failed, purpose in rows:
                status = "FAILED" if failed else f"{n} rows"
                line = f"[{qid}] {source:<4} {elapsed_s or 0:6.2f}s {status:>9}"
                print(f"{line}  {purpose}")
            print(f"{len(rows)} queries (attempt {attempt})")
            return 0

        row = conn.execute(_ONE, (incident_id, attempt, args.qid)).fetchone()
        if row is None:
            print(
                f"no query {args.qid} in this incident, attempt {attempt}",
                file=sys.stderr,
            )
            return 1
        qid, source, purpose, query, elapsed_s, n, result, error = row
        print(
            json.dumps(
                {
                    "qid": qid,
                    "source": source,
                    "purpose": purpose,
                    "query": query,
                    "elapsed_s": elapsed_s,
                    "rows": n,
                    "result": result,
                    "error": error,
                },
                indent=2,
                default=str,
            )
        )
        return 0


if __name__ == "__main__":
    sys.exit(main())
