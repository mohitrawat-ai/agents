"""One-off probe: does New Relic return an entity relationship map (call graph)
for the partner app? Queries NerdGraph `relatedEntities` for the app GUID and
prints the edges (type → target entity + type). Read-only. Uses `load_env` so
credentials never reach stdout.

Copied from ingren-rca/tools/nr_relationships.py on 2026-07-18; changes:
imports load_env from sibling nr_run_nrql (was nr_fetch, which stays with the
seasonal pipeline), .env resolved via DOTENV (this project's root).

    uv run python tools/newrelic/nr_relationships.py
"""

import json
import sys
import urllib.request

from nr_run_nrql import DOTENV, load_env

GUID = "MzMwODc2M3xBUE18QVBQTElDQVRJT058MTQ1MDc2NTMxOQ"  # REST API - Python

QUERY = """
{
  actor {
    entity(guid: "%s") {
      name
      entityType
      relatedEntities {
        results {
          type
          source { entity { name entityType type guid } }
          target { entity { name entityType type guid } }
        }
      }
    }
  }
}
""" % GUID


def graphql(query: str, env: dict) -> dict:
    region = env.get("NEW_RELIC_REGION", "US").upper()
    endpoint = (
        "https://api.eu.newrelic.com/graphql" if region == "EU"
        else "https://api.newrelic.com/graphql"
    )
    req = urllib.request.Request(
        endpoint,
        data=json.dumps({"query": query}).encode(),
        headers={"Content-Type": "application/json", "API-Key": env["NEW_RELIC_API_KEY"]},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def main() -> int:
    env = load_env(DOTENV)
    data = graphql(QUERY, env)
    if "errors" in data:
        print("GraphQL errors:")
        print(json.dumps(data["errors"], indent=2))
        return 1
    entity = (data.get("data", {}).get("actor", {}) or {}).get("entity")
    if not entity:
        print("No entity returned for GUID. Raw:", json.dumps(data)[:800])
        return 1
    print(f"App: {entity.get('name')}  type={entity.get('entityType')}")
    rel = (entity.get("relatedEntities") or {}).get("results") or []
    print(f"{len(rel)} relationship edges:\n")
    for e in rel:
        t = e.get("type")
        src = ((e.get("source") or {}).get("entity") or {}) or {}
        tgt = ((e.get("target") or {}).get("entity") or {}) or {}
        print(
            f"  {src.get('name')!r} --{t}--> {tgt.get('name')!r}"
            f"  [{tgt.get('entityType') or tgt.get('type')}]"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
