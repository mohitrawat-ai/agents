"""Run one NRQL and tee it into the incident's Postgres record.

Copied from ingren-rca/tools/nrql_log.py on 2026-07-18; changes: imports its
sibling nr_run_nrql directly (no repo-root sys.path hack), usage path updated,
and qid minting moved to after the query under a file lock — the original
counted lines before querying, so parallel invocations minted duplicate ids
(observed in the 2026-07-07 hb-prod-lb-503 log: three queries sharing q02).

Sink moved from queries.jsonl to Postgres 2026-07-21 (issue #4, P2 §1). Same
CLI surface; --log-dir is kept for the CLI contract and the Langfuse mirror
but the record now lands in the queries table. qid minting is the insert
itself (tools/db.py), so the file lock is gone.

The logging wrapper around nr_run_nrql: same query, same printed output,
plus an append-only evidence record. NR events expire in ~8 days, so the
record is the only durable copy of what an investigation saw — failed and
dead-end queries are logged too. Read-only against New Relic.

Usage:
    python3 tools/newrelic/nrql_log.py --log-dir . \
        --purpose "why this query is being run" '<NRQL>'
"""

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lf_mirror import mirror_query
from nr_run_nrql import run_nrql

import db


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Run one NRQL, log it to the record.")
    ap.add_argument(
        "--log-dir", required=True, help="run dir (kept for the CLI contract)"
    )
    ap.add_argument("--purpose", required=True, help="why this query is being run")
    ap.add_argument("nrql", help="the NRQL string")
    args = ap.parse_args(argv)

    nrql = args.nrql.strip()
    status, data, elapsed = run_nrql(nrql, os.environ)

    errors = data.get("errors")
    results, metadata = None, {}
    if not errors:
        block = data["data"]["actor"]["account"]["nrql"]
        results = block["results"] or []
        metadata = block.get("metadata") or {}

    with db.connect() as conn:
        qid = db.insert_query(
            conn,
            source="nrql",
            purpose=args.purpose,
            query=nrql,
            elapsed_s=round(elapsed, 2),
            rows=len(results) if results is not None else None,
            result=results,
            error=json.dumps(errors) if errors else None,
        )
    mirror_query(
        Path(args.log_dir),
        {
            "id": qid,
            "purpose": args.purpose,
            "nrql": nrql,
            "elapsed_s": round(elapsed, 2),
            "rows": len(results) if results is not None else None,
            "results": results,
            "errors": errors,
        },
    )

    print(f"[{qid}] logged -> postgres")
    print("NRQL:")
    print(nrql)
    print()
    print(f"HTTP {status} in {elapsed:.2f}s")
    if errors:
        print("GraphQL errors:")
        print(json.dumps(errors, indent=2))
        return 1
    print(f"Result rows: {len(results)}")
    print(f"Facets    : {metadata.get('facets')}")
    print()
    print("Full results:")
    print(json.dumps(results, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
