"""Seed one incidents row from a real alert file.

New for issue #5. Under §8a-A the investigation task takes {incident_id} and
reads raw from Postgres, so nothing can run until a row exists — this script
is that row. Deliberately dumb: no dedup, no alert parsing (that is the
router's business, #11). received_utc stays NULL.

Connects as rca_service (RCA_SERVICE_DATABASE_URL): the role that owns
incident inserts in production, so seeding also exercises the real grant.

Usage:
    uv run --env-file .env python db/seed_incident.py <alert.json> \
        --channel C0XXXXX --thread-ts 1721500000.000100
"""

import argparse
import json
import os
import sys
import uuid
from pathlib import Path

import psycopg
from psycopg.types.json import Jsonb

INSERT = """\
INSERT INTO incidents (event_id, channel, thread_ts, slug, raw)
VALUES (%s, %s, %s, %s, %s)
RETURNING id
"""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Insert one incidents row from an alert file, print the id."
    )
    ap.add_argument("alert", type=Path, help="path to alert.json")
    ap.add_argument("--channel", required=True, help="Slack channel id for the poller")
    ap.add_argument("--thread-ts", required=True, help="Slack thread ts for the poller")
    ap.add_argument("--slug", help="default: the alert file's folder name")
    args = ap.parse_args(argv)

    raw = json.loads(args.alert.read_text())
    slug = args.slug or args.alert.resolve().parent.name
    event_id = f"seed-{uuid.uuid4()}"

    dsn = os.environ.get("RCA_SERVICE_DATABASE_URL")
    if not dsn:
        print("RCA_SERVICE_DATABASE_URL is not set", file=sys.stderr)
        return 2

    with psycopg.connect(dsn, autocommit=True) as conn:
        incident_id = conn.execute(
            INSERT, (event_id, args.channel, args.thread_ts, slug, Jsonb(raw))
        ).fetchone()[0]

    print(f"INCIDENT_ID={incident_id}")
    print(f"slug={slug}  event_id={event_id}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
