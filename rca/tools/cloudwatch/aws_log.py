"""Run one read-only aws CLI command and tee it into an incident's queries.jsonl.

The AWS counterpart of tools/newrelic/nrql_log.py, written 2026-07-18 after
the 2026-07-07 hb-prod-lb-503 review showed AWS-side evidence going unlogged.
Same receipts contract: every look at the data — including failures and dead
ends — lands in the incident's queries.jsonl. Entries carry `cmd` where NRQL
entries carry `nrql`.

Read-only rail: the action verb must match an allowlist (describe-/get-/
list-/filter-/lookup- prefixes, plus Logs Insights query-job verbs). Anything
else is refused before execution. `--profile hb-role --output json` are
injected unless the command already sets them.

Usage:
    uv run python tools/cloudwatch/aws_log.py --log-dir <incident-variant-dir> \
        --purpose "why" cloudwatch describe-alarm-history --alarm-name <name>

append_locked is duplicated from tools/newrelic/nrql_log.py on purpose —
shared-module plumbing across source dirs isn't worth it until a third
telemetry source exists.
"""

import argparse
import fcntl
import json
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lf_mirror import mirror_query

ALLOWED_PREFIXES = ("describe-", "get-", "list-", "filter-", "lookup-")
ALLOWED_EXACT = {"start-query", "stop-query", "tail"}


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
        description="Run one read-only aws CLI command, log it to queries.jsonl."
    )
    ap.add_argument(
        "--log-dir", required=True, help="incident variant dir (holds queries.jsonl)"
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

    log_path = Path(args.log_dir) / "queries.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)

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

    entry = {
        "ts": datetime.now(UTC).isoformat(timespec="seconds"),
        "purpose": args.purpose,
        "cmd": " ".join(cmd),
        "elapsed_s": round(elapsed, 2),
        "exit_code": proc.returncode,
        "output": output,
        "errors": proc.stderr.strip() or None if proc.returncode != 0 else None,
    }
    qid = append_locked(log_path, entry)
    mirror_query(Path(args.log_dir), {"id": qid, **entry})

    print(f"[{qid}] logged -> {log_path}")
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
