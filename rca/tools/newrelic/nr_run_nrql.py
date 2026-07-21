"""Run one NRQL string (from stdin or arg) against NerdGraph and print response.

Copied from ingren-rca/tools/nr_run_nrql.py on 2026-07-18; only change: .env
is resolved from this project's root (prod/agents/rca/.env), not ingren-rca.

Read-only. No files written. Usage:
    uv run python tools/newrelic/nr_run_nrql.py "SELECT count(*) FROM Span ..."
"""

import json
import sys
import time
import urllib.request
from pathlib import Path

DOTENV = Path(__file__).resolve().parents[2] / ".env"


def load_env(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def run_nrql(nrql: str, env: dict[str, str]) -> tuple[int, dict, float]:
    """POST one NRQL to NerdGraph. Returns (http status, response json,
    elapsed seconds). Read-only."""
    region = env["NEW_RELIC_REGION"].upper()
    endpoint = (
        "https://api.eu.newrelic.com/graphql" if region == "EU"
        else "https://api.newrelic.com/graphql"
    )
    body = {
        "query": (
            "{ actor { account(id: " + env["NEW_RELIC_ACCOUNT_ID"] + ") "
            "{ nrql(query: " + json.dumps(nrql) + ") { results "
            "metadata { timeWindow { begin end } facets } } } } }"
        )
    }
    payload = json.dumps(body).encode()
    req = urllib.request.Request(
        endpoint,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "API-Key": env["NEW_RELIC_API_KEY"],
        },
        method="POST",
    )
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=60) as resp:
        elapsed = time.time() - t0
        data = json.loads(resp.read())
        status = resp.status
    return status, data, elapsed


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: nr_run_nrql.py '<NRQL>'", file=sys.stderr)
        return 2
    nrql = sys.argv[1].strip()
    print("NRQL:")
    print(nrql)
    print()
    status, data, elapsed = run_nrql(nrql, load_env(DOTENV))
    print(f"HTTP {status} in {elapsed:.2f}s")

    if "errors" in data:
        print("GraphQL errors:")
        print(json.dumps(data["errors"], indent=2))
        return 1

    nrql_block = data["data"]["actor"]["account"]["nrql"]
    results = nrql_block["results"] or []
    metadata = nrql_block.get("metadata", {})

    print(f"Result rows: {len(results)}")
    print(f"Facets    : {metadata.get('facets')}")
    print()
    print("Full results:")
    print(json.dumps(results, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
