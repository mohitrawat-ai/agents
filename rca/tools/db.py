"""Postgres sink for the evidence tools.

New for issue #4 (P2 §1): nrql_log, aws_log, and emit keep their CLI surface
and swap jsonl for these inserts. This is the only module that knows the
connection or the table shapes.

Scope (incident id, attempt) comes from the task environment, never from
agent input (P9 §5). Every variable is required; a missing one stops the
tool loudly rather than guessing (design.md §4, invariant 6).
"""

import os

import psycopg
from psycopg.errors import UniqueViolation
from psycopg.types.json import Jsonb

_INSERT_QUERY = """\
INSERT INTO queries (incident_id, attempt, qid, source, purpose, query,
                     elapsed_s, rows, result, error)
VALUES (%(incident_id)s, %(attempt)s,
        (SELECT 'q' || lpad((count(*) + 1)::text, 2, '0')
           FROM queries
          WHERE incident_id = %(incident_id)s AND attempt = %(attempt)s),
        %(source)s, %(purpose)s, %(query)s,
        %(elapsed_s)s, %(rows)s, %(result)s, %(error)s)
RETURNING qid
"""

_INSERT_EVENT = """\
INSERT INTO events (incident_id, attempt, event, payload)
VALUES (%s, %s, %s, %s)
"""

_INSERT_DOCUMENT = """\
INSERT INTO documents (incident_id, attempt, name, content)
VALUES (%s, %s, %s, %s)
"""

_GET_INCIDENT = "SELECT slug, raw FROM incidents WHERE id = %s"


def _env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"{name} is not set; the task environment must provide it")
    return value


def connect() -> psycopg.Connection:
    # RCA_DATABASE_URL is role-neutral on purpose: each task's environment
    # carries the DSN for its own role (investigator: rca_agent; Q&A
    # subprocess: rca_service). The code never knows which (P9 §4).
    # connect_timeout: libpq's default is wait-forever, which would pin the
    # investigator's event loop on a Neon stall (issue #7 review).
    return psycopg.connect(
        _env("RCA_DATABASE_URL"), autocommit=True, connect_timeout=10
    )


def scope() -> tuple[str, int]:
    return _env("INCIDENT_ID"), int(_env("ATTEMPT"))


def insert_query(
    conn: psycopg.Connection,
    *,
    source: str,
    purpose: str,
    query: str,
    elapsed_s: float | None,
    rows: int | None,
    result: object,
    error: str | None,
) -> str:
    """Insert one evidence row and return its minted qid (q01, q02, ...).

    The qid subquery counts this incident+attempt's rows; a concurrent mint
    hits UNIQUE (incident_id, attempt, qid) and retries. Duplicate qids are
    impossible, not avoided (P2 §1).
    """
    incident_id, attempt = scope()
    params = {
        "incident_id": incident_id,
        "attempt": attempt,
        "source": source,
        "purpose": purpose,
        "query": query,
        "elapsed_s": elapsed_s,
        "rows": rows,
        "result": Jsonb(result) if result is not None else None,
        "error": error,
    }
    for _ in range(3):
        try:
            return conn.execute(_INSERT_QUERY, params).fetchone()[0]
        except UniqueViolation:
            continue
    raise SystemExit("qid mint lost the race 3 times; giving up")


def get_incident(conn: psycopg.Connection) -> tuple[str, dict]:
    """Return (slug, raw) for the environment's incident. Loud if absent —
    a task started for a row that does not exist should fail, not guess."""
    incident_id, _ = scope()
    row = conn.execute(_GET_INCIDENT, (incident_id,)).fetchone()
    if row is None:
        raise SystemExit(f"no incidents row for {incident_id}")
    return row[0], row[1]


def insert_event(conn: psycopg.Connection, event: str, payload: dict) -> None:
    incident_id, attempt = scope()
    conn.execute(_INSERT_EVENT, (incident_id, attempt, event, Jsonb(payload)))


def insert_document(conn: psycopg.Connection, name: str, content: str) -> None:
    incident_id, attempt = scope()
    conn.execute(_INSERT_DOCUMENT, (incident_id, attempt, name, content))
