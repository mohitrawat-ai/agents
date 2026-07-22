"""Schema tests — issue #2's acceptance criteria.

Red against an empty database, green after db/migrations/001 + 002 are applied.
Every denial is proven by a failing statement (InsufficientPrivilege), never by
reading grants.

Needs four connection strings in the environment (built after 002 — see its
prompts): DATABASE_URL (owner, direct endpoint), RCA_AGENT_DATABASE_URL,
RCA_SERVICE_DATABASE_URL, RCA_POLLER_DATABASE_URL (roles, pooled endpoint).

Run:  uv run --env-file .env pytest tests/test_schema.py
"""

import os
import uuid

import psycopg
import pytest
from psycopg.errors import InsufficientPrivilege, UniqueViolation

TABLES = {"incidents", "queries", "events", "documents"}
ROLES = {"rca_agent", "rca_service", "rca_poller"}

INSERT_INCIDENT = (
    "INSERT INTO incidents (event_id, channel, thread_ts, slug, raw)"
    " VALUES (%s, 'C_TEST', '1721500000.000100', 'test-slug', '{}')"
    " RETURNING id"
)
INSERT_QUERY = (
    "INSERT INTO queries (incident_id, attempt, qid, source, purpose, query)"
    " VALUES (%s, %s, %s, 'nrql', 'schema test', 'SELECT 1')"
)
INSERT_EVENT = (
    "INSERT INTO events (incident_id, attempt, event) VALUES (%s, 1, 'hypothesis')"
)
INSERT_DOCUMENT = (
    "INSERT INTO documents (incident_id, attempt, name, content)"
    " VALUES (%s, 1, 'rca.md', '# test')"
)


def connect(env_var: str) -> psycopg.Connection:
    dsn = os.environ.get(env_var)
    if not dsn:
        pytest.fail(f"{env_var} not set — add it to rca/.env")
    return psycopg.connect(dsn, autocommit=True)


def denied(conn: psycopg.Connection, sql: str, params: tuple = ()) -> None:
    with pytest.raises(InsufficientPrivilege):
        conn.execute(sql, params)


@pytest.fixture(scope="module")
def owner():
    with connect("DATABASE_URL") as conn:
        yield conn


@pytest.fixture(scope="module")
def agent():
    with connect("RCA_AGENT_DATABASE_URL") as conn:
        yield conn


@pytest.fixture(scope="module")
def service():
    with connect("RCA_SERVICE_DATABASE_URL") as conn:
        yield conn


@pytest.fixture(scope="module")
def poller():
    with connect("RCA_POLLER_DATABASE_URL") as conn:
        yield conn


@pytest.fixture
def incident(owner):
    """A committed incident row; owner cleans up children then the row."""
    incident_id = owner.execute(INSERT_INCIDENT, (f"ev-{uuid.uuid4()}",)).fetchone()[0]
    yield incident_id
    for table in ("queries", "events", "documents"):
        owner.execute(f"DELETE FROM {table} WHERE incident_id = %s", (incident_id,))
    owner.execute("DELETE FROM incidents WHERE id = %s", (incident_id,))


# --- shape: four tables, three roles, ruled columns -------------------------


def test_four_tables_exist(owner):
    rows = owner.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
    ).fetchall()
    assert {r[0] for r in rows} >= TABLES


def test_three_roles_exist(owner):
    rows = owner.execute("SELECT rolname FROM pg_roles").fetchall()
    assert {r[0] for r in rows} >= ROLES


def test_dedup_columns_do_not_ship(owner):
    cols = {
        r[0]
        for r in owner.execute(
            "SELECT column_name FROM information_schema.columns"
            " WHERE table_name = 'incidents'"
        ).fetchall()
    }
    assert "dedup_key" not in cols
    assert "condition_guess" not in cols


def test_queries_and_events_carry_attempt(owner):
    for table in ("queries", "events"):
        cols = {
            r[0]
            for r in owner.execute(
                "SELECT column_name FROM information_schema.columns"
                " WHERE table_name = %s",
                (table,),
            ).fetchall()
        }
        assert "attempt" in cols, table


def test_event_id_unique(owner):
    event_id = f"ev-{uuid.uuid4()}"
    incident_id = owner.execute(INSERT_INCIDENT, (event_id,)).fetchone()[0]
    try:
        with pytest.raises(UniqueViolation):
            owner.execute(INSERT_INCIDENT, (event_id,))
    finally:
        owner.execute("DELETE FROM incidents WHERE id = %s", (incident_id,))


def test_channel_thread_ts_indexed_together(owner):
    defs = [
        r[0]
        for r in owner.execute(
            "SELECT indexdef FROM pg_indexes WHERE tablename = 'incidents'"
        ).fetchall()
    ]
    assert any("(channel, thread_ts)" in d for d in defs)


def test_qid_unique_per_incident_and_attempt(owner, incident):
    owner.execute(INSERT_QUERY, (incident, 1, "q01"))
    owner.execute(INSERT_QUERY, (incident, 2, "q01"))  # same qid, next attempt: fine
    with pytest.raises(UniqueViolation):
        owner.execute(INSERT_QUERY, (incident, 1, "q01"))


# --- rca_agent: append-only on evidence, reads its incident and queries -----


def test_agent_inserts_evidence(agent, incident):
    agent.execute(INSERT_QUERY, (incident, 1, "q01"))
    agent.execute(INSERT_EVENT, (incident,))
    agent.execute(INSERT_DOCUMENT, (incident,))


def test_agent_reads_incidents_and_queries(agent, incident):
    assert agent.execute(
        "SELECT raw FROM incidents WHERE id = %s", (incident,)
    ).fetchone()
    agent.execute(
        "SELECT qid FROM queries WHERE incident_id = %s", (incident,)
    ).fetchall()


def test_agent_cannot_update(agent, incident):
    denied(agent, "UPDATE incidents SET slug = 'x' WHERE id = %s", (incident,))
    denied(agent, "UPDATE queries SET rows = 0 WHERE incident_id = %s", (incident,))
    denied(agent, "UPDATE events SET event = 'x' WHERE incident_id = %s", (incident,))
    denied(
        agent, "UPDATE documents SET content = 'x' WHERE incident_id = %s", (incident,)
    )


def test_agent_cannot_delete(agent, incident):
    denied(agent, "DELETE FROM queries WHERE incident_id = %s", (incident,))
    denied(agent, "DELETE FROM events WHERE incident_id = %s", (incident,))
    denied(agent, "DELETE FROM documents WHERE incident_id = %s", (incident,))
    denied(agent, "DELETE FROM incidents WHERE id = %s", (incident,))


def test_agent_cannot_truncate(agent):
    # TRUNCATE is its own privilege bit; DELETE denials do not cover it.
    denied(agent, "TRUNCATE queries")
    denied(agent, "TRUNCATE events")
    denied(agent, "TRUNCATE documents")
    denied(agent, "TRUNCATE incidents")


def test_agent_cannot_insert_incidents(agent):
    denied(agent, INSERT_INCIDENT, (f"ev-{uuid.uuid4()}",))


def test_agent_reads_events_but_not_documents(agent):
    """Migration 003 (§8e): the retry guard reads run_failed rows, so
    events became SELECTable. Documents stay closed."""
    agent.execute("SELECT event FROM events LIMIT 1")
    denied(agent, "SELECT name FROM documents")


# --- rca_service: owns operational state, read-only on evidence -------------


def test_service_inserts_and_updates_incidents(service, owner, incident):
    service.execute(
        "UPDATE incidents SET narrated_through = 5 WHERE id = %s", (incident,)
    )
    new_id = service.execute(INSERT_INCIDENT, (f"ev-{uuid.uuid4()}",)).fetchone()[0]
    owner.execute("DELETE FROM incidents WHERE id = %s", (new_id,))


def test_service_reads_evidence(service, incident):
    service.execute(
        "SELECT qid FROM queries WHERE incident_id = %s", (incident,)
    ).fetchall()
    service.execute(
        "SELECT event FROM events WHERE incident_id = %s", (incident,)
    ).fetchall()
    service.execute(
        "SELECT content FROM documents WHERE incident_id = %s", (incident,)
    ).fetchall()


def test_service_cannot_write_evidence(service, incident):
    denied(service, INSERT_QUERY, (incident, 1, "q98"))
    denied(service, "UPDATE queries SET rows = 0 WHERE incident_id = %s", (incident,))


def test_service_cannot_delete(service, incident):
    denied(service, "DELETE FROM incidents WHERE id = %s", (incident,))
    denied(service, "DELETE FROM queries WHERE incident_id = %s", (incident,))


# --- rca_poller: reads, and writes exactly one column -----------------------


def test_poller_reads_events_and_incidents(poller, incident):
    poller.execute(
        "SELECT event FROM events WHERE incident_id = %s", (incident,)
    ).fetchall()
    assert poller.execute(
        "SELECT channel, thread_ts FROM incidents WHERE id = %s", (incident,)
    ).fetchone()


def test_poller_updates_only_its_cursor(poller, incident):
    poller.execute(
        "UPDATE incidents SET narrated_through = 3 WHERE id = %s", (incident,)
    )
    denied(poller, "UPDATE incidents SET channel = 'x' WHERE id = %s", (incident,))


def test_poller_cannot_write_events(poller, incident):
    denied(poller, INSERT_EVENT, (incident,))
    denied(poller, "DELETE FROM events WHERE incident_id = %s", (incident,))


def test_poller_cannot_read_queries(poller):
    denied(poller, "SELECT qid FROM queries")
