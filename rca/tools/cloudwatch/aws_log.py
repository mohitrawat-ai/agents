"""Run one read-only aws CLI command and tee it into the incident's Postgres record.

The AWS counterpart of tools/newrelic/nrql_log.py, written 2026-07-18 after
the 2026-07-07 hb-prod-lb-503 review showed AWS-side evidence going unlogged.
Same receipts contract: every look at the data — including failures and dead
ends — lands in the record. Entries carry the aws command where NRQL entries
carry the query string.

Sink moved from queries.jsonl to Postgres 2026-07-21 (issue #4, P2 §1). Same
CLI surface; --log-dir is kept for the CLI contract and the Langfuse mirror.
qid minting is the insert itself (tools/db.py), so append_locked and its
file lock are gone — and with them the duplicated copy this file carried.

Read-only rail: the action verb must match an allowlist (describe-/get-/
list-/filter-/lookup- prefixes, plus Logs Insights query-job verbs). Anything
else is refused before execution. `--profile hb-role --output json` are
injected unless the command already sets them.

Usage:
    python3 tools/cloudwatch/aws_log.py --log-dir . \
        --purpose "why" cloudwatch describe-alarm-history --alarm-name <name>
"""

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lf_mirror import mirror_query

import db

ALLOWED_PREFIXES = ("describe-", "get-", "list-", "filter-", "lookup-")
ALLOWED_EXACT = {"start-query", "stop-query", "tail"}


def check_read_only(aws_args: list[str]) -> str | None:
    """Return an error message unless the command's action verb is read-only."""
    positional = [a for a in aws_args if not a.startswith("-")]
    if len(positional) < 2:
        return f"can't find <service> <action> in: {aws_args}"
    action = positional[1]
    if action.startswith(ALLOWED_PREFIXES) or action in ALLOWED_EXACT:
        return None
    return (
        f"refused: '{action}' is not on the read-only allowlist "
        f"(prefixes {', '.join(ALLOWED_PREFIXES)}; exact {sorted(ALLOWED_EXACT)})"
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Run one read-only aws CLI command, log it to the record."
    )
    ap.add_argument(
        "--log-dir", required=True, help="run dir (kept for the CLI contract)"
    )
    ap.add_argument("--purpose", required=True, help="why this command is being run")
    ap.add_argument(
        "aws_args",
        nargs=argparse.REMAINDER,
        help="the aws command, without the leading 'aws'",
    )
    args = ap.parse_args(argv)

    aws_args = [a for a in args.aws_args if a != "--"]
    if not aws_args:
        print(
            "usage: aws_log.py --log-dir D --purpose '...' <service> <action> [...]",
            file=sys.stderr,
        )
        return 2

    refusal = check_read_only(aws_args)
    if refusal:
        print(refusal, file=sys.stderr)
        return 2

    if "--profile" not in aws_args:
        aws_args += ["--profile", "hb-role"]
    if "--output" not in aws_args:
        aws_args += ["--output", "json"]

    cmd = ["aws", *aws_args]
    t0 = time.time()
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    elapsed = time.time() - t0

    output: object = None
    if proc.stdout:
        try:
            output = json.loads(proc.stdout)
        except json.JSONDecodeError:
            output = proc.stdout

    error = proc.stderr.strip() or None if proc.returncode != 0 else None
    with db.connect() as conn:
        qid = db.insert_query(
            conn,
            source="aws",
            purpose=args.purpose,
            query=" ".join(cmd),
            elapsed_s=round(elapsed, 2),
            rows=None,
            result=output,
            error=error,
        )
    mirror_query(
        Path(args.log_dir),
        {
            "id": qid,
            "purpose": args.purpose,
            "cmd": " ".join(cmd),
            "elapsed_s": round(elapsed, 2),
            "exit_code": proc.returncode,
            "output": output,
            "errors": error,
        },
    )

    print(f"[{qid}] logged -> postgres")
    print("CMD:", " ".join(cmd))
    print(f"exit {proc.returncode} in {elapsed:.2f}s")
    if proc.returncode != 0:
        print(proc.stderr.strip(), file=sys.stderr)
        return 1
    print()
    print(json.dumps(output, indent=2, default=str)[:4000])
    return 0


if __name__ == "__main__":
    sys.exit(main())
