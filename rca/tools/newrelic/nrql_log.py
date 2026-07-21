"""Run one NRQL and tee it into an incident's queries.jsonl.

Copied from ingren-rca/tools/nrql_log.py on 2026-07-18; changes: imports its
sibling nr_run_nrql directly (no repo-root sys.path hack), usage path updated,
and qid minting moved to after the query under a file lock — the original
counted lines before querying, so parallel invocations minted duplicate ids
(observed in the 2026-07-07 hb-prod-lb-503 log: three queries sharing q02).

The logging wrapper around nr_run_nrql: same query, same printed output,
plus an append-only evidence log. NR events expire in ~8 days, so the log is
the only durable copy of what an investigation saw — failed and dead-end
queries are logged too. Read-only against New Relic.

Usage:
    uv run python tools/newrelic/nrql_log.py --log-dir <incident-variant-dir> \
        --purpose "why this query is being run" '<NRQL>'
"""

import argparse
import fcntl
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lf_mirror import mirror_query  # noqa: E402
from nr_run_nrql import DOTENV, load_env, run_nrql  # noqa: E402


def append_locked(log_path: Path, entry: dict) -> str:
    """Mint the next qid and append entry under an exclusive lock, so
    parallel invocations can't count the same prior lines and collide."""
    with log_path.open("a+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.seek(0)
        n_prior = sum(1 for _ in f)
        qid = f"q{n_prior + 1:02d}"
        entry = {"id": qid, **entry}
        f.write(json.dumps(entry, default=str) + "\n")
    return qid


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Run one NRQL, log it to queries.jsonl.")
    ap.add_argument("--log-dir", required=True, help="incident variant dir (holds queries.jsonl)")
    ap.add_argument("--purpose", required=True, help="why this query is being run")
    ap.add_argument("nrql", help="the NRQL string")
    args = ap.parse_args(argv)

    log_path = Path(args.log_dir) / "queries.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    nrql = args.nrql.strip()
    status, data, elapsed = run_nrql(nrql, load_env(DOTENV))

    errors = data.get("errors")
    results, metadata = None, {}
    if not errors:
        block = data["data"]["actor"]["account"]["nrql"]
        results = block["results"] or []
        metadata = block.get("metadata") or {}

    entry = {
        "ts": datetime.now(UTC).isoformat(timespec="seconds"),
        "purpose": args.purpose,
        "nrql": nrql,
        "elapsed_s": round(elapsed, 2),
        "rows": len(results) if results is not None else None,
        "results": results,
        "errors": errors,
    }
    qid = append_locked(log_path, entry)
    mirror_query(Path(args.log_dir), {"id": qid, **entry})

    print(f"[{qid}] logged -> {log_path}")
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
