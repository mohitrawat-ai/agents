"""One-off probe: can Span/trace data give transaction -> dependency edges?

Discovery queries against the partner app's Span events: volume by category, the
attribute keyset (to find the transaction-scope + target fields), and span names.
Read-only NRQL. `uv run python tools/newrelic/nr_trace_probe.py`

Copied from ingren-rca/tools/nr_trace_probe.py on 2026-07-18; changes: imports
from sibling nr_run_nrql (was nr_fetch, which stays with the seasonal
pipeline) and unwraps its raw-response return shape; .env resolved via DOTENV.
"""

import json
import sys

from nr_run_nrql import DOTENV, load_env, run_nrql

APP = 1450765319

QUERIES = {
    "volume_by_category_1d": f"SELECT count(*) FROM Span WHERE appId = {APP} "
                             f"SINCE 1 day ago FACET category",
    "keyset": f"SELECT keyset() FROM Span WHERE appId = {APP} SINCE 1 day ago",
    "span_names_3h": f"SELECT count(*) FROM Span WHERE appId = {APP} "
                     f"SINCE 3 hours ago FACET name LIMIT 40",
}


def main() -> int:
    env = load_env(DOTENV)
    for label, q in QUERIES.items():
        try:
            status, data, elapsed = run_nrql(q, env)
            block = data["data"]["actor"]["account"]["nrql"]
            print(f"\n=== {label}  ({elapsed:.1f}s) ===")
            results = block.get("results")
            text = json.dumps(results, indent=2)
            print(text[:2500])
        except Exception as e:  # noqa: BLE001
            print(f"\n=== {label}  FAILED: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
