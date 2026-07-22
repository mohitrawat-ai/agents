"""Tests for the tools' Postgres sink (issue #4): db.py and read_record.py.

Needs DATABASE_URL (owner: fixture setup/teardown) and RCA_AGENT_DATABASE_URL
(what the tools run as). The tools read RCA_DATABASE_URL, INCIDENT_ID, and
ATTEMPT; each test sets those via monkeypatch — the same environment contract
the task will provide in production.

Run:  uv run --env-file .env pytest tests/test_tools_db.py
"""

import json
import os
import sys
import uuid
from pathlib import Path

import psycopg
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import read_record

import db

INSERT_INCIDENT = (
    "INSERT INTO incidents (event_id, channel, thread_ts, slug, raw)"
    " VALUES (%s, 'C_TEST', '1721500000.000200', 'tools-test', '{}')"
    " RETURNING id"
)


def connect_env(env_var: str) -> psycopg.Connection:
    dsn = os.environ.get(env_var)
    if not dsn:
        pytest.fail(f"{env_var} not set — add it to rca/.env")
    return psycopg.connect(dsn, autocommit=True)


@pytest.fixture(scope="module")
def owner():
    with connect_env("DATABASE_URL") as conn:
        yield conn


@pytest.fixture
def incident(owner, monkeypatch):
    """A committed incident row, with the tools' environment pointed at it."""
    incident_id = owner.execute(INSERT_INCIDENT, (f"ev-{uuid.uuid4()}",)).fetchone()[0]
    monkeypatch.setenv("RCA_DATABASE_URL", os.environ["RCA_AGENT_DATABASE_URL"])
    monkeypatch.setenv("INCIDENT_ID", str(incident_id))
    monkeypatch.setenv("ATTEMPT", "1")
    yield incident_id
    for table in ("queries", "events", "documents"):
        owner.execute(f"DELETE FROM {table} WHERE incident_id = %s", (incident_id,))
    owner.execute("DELETE FROM incidents WHERE id = %s", (incident_id,))


def insert_one(purpose: str = "test", query: str = "SELECT 1") -> str:
    with db.connect() as conn:
        return db.insert_query(
            conn,
            source="nrql",
            purpose=purpose,
            query=query,
            elapsed_s=0.5,
            rows=3,
            result=[{"count": 3}],
            error=None,
        )


# --- db.py ------------------------------------------------------------------


@pytest.mark.usefixtures("incident")
def test_qids_are_sequential():
    assert insert_one() == "q01"
    assert insert_one() == "q02"
    assert insert_one() == "q03"


@pytest.mark.usefixtures("incident")
def test_qids_restart_per_attempt(monkeypatch):
    assert insert_one() == "q01"
    monkeypatch.setenv("ATTEMPT", "2")
    assert insert_one() == "q01"


def test_failed_query_writes_a_row(incident, owner):
    with db.connect() as conn:
        qid = db.insert_query(
            conn,
            source="nrql",
            purpose="dead end",
            query="SELECT broken",
            elapsed_s=0.1,
            rows=None,
            result=None,
            error='[{"message": "syntax"}]',
        )
    row = owner.execute(
        "SELECT error FROM queries WHERE incident_id = %s AND qid = %s",
        (incident, qid),
    ).fetchone()
    assert row[0] == '[{"message": "syntax"}]'


def test_event_and_document_insert(incident, owner):
    with db.connect() as conn:
        db.insert_event(conn, "hypothesis", {"status": "formed", "qids": ["q01"]})
        db.insert_document(conn, "rca.md", "# verdict")
    event = owner.execute(
        "SELECT event, payload FROM events WHERE incident_id = %s", (incident,)
    ).fetchone()
    assert event == ("hypothesis", {"status": "formed", "qids": ["q01"]})
    doc = owner.execute(
        "SELECT name, content FROM documents WHERE incident_id = %s", (incident,)
    ).fetchone()
    assert doc == ("rca.md", "# verdict")


@pytest.mark.usefixtures("incident")
def test_missing_env_stops_loudly(monkeypatch):
    monkeypatch.delenv("INCIDENT_ID")
    with pytest.raises(SystemExit, match="INCIDENT_ID"):
        db.scope()


# --- read_record.py ---------------------------------------------------------


@pytest.mark.usefixtures("incident")
def test_list_shows_only_current_attempt(monkeypatch, capsys):
    insert_one(purpose="first attempt")
    monkeypatch.setenv("ATTEMPT", "2")
    insert_one(purpose="second attempt")
    assert read_record.main(["--list"]) == 0
    out = capsys.readouterr().out
    assert "second attempt" in out
    assert "first attempt" not in out
    assert "1 queries (attempt 2)" in out


@pytest.mark.usefixtures("incident")
def test_qid_returns_full_row(capsys):
    insert_one(purpose="the one", query="SELECT 2")
    assert read_record.main(["--qid", "q01"]) == 0
    row = json.loads(capsys.readouterr().out)
    assert row["purpose"] == "the one"
    assert row["query"] == "SELECT 2"
    assert row["result"] == [{"count": 3}]


@pytest.mark.usefixtures("incident")
def test_unknown_qid_is_exit_1(capsys):
    assert read_record.main(["--qid", "q99"]) == 1
    assert "no query q99" in capsys.readouterr().err


@pytest.mark.usefixtures("incident")
def test_incident_flag_is_ignored(owner, capsys):
    """A flag aiming at another incident must not move the scope (§8a-C)."""
    other = owner.execute(INSERT_INCIDENT, (f"ev-{uuid.uuid4()}",)).fetchone()[0]
    try:
        insert_one(purpose="mine")
        assert read_record.main(["--list", "--incident", str(other)]) == 0
        captured = capsys.readouterr()
        assert "mine" in captured.out
        assert "ignored" in captured.err
    finally:
        owner.execute("DELETE FROM incidents WHERE id = %s", (other,))


# --- last_failed_exit_code: the §8e retry guard's read -----------------------


@pytest.mark.usefixtures("incident")
def test_guard_reads_prior_policy_stop(monkeypatch):
    """Attempt 1 recorded a budget stop; the guard read (as rca_agent, on
    attempt 2) must see exit code 4."""
    with db.connect() as conn:
        db.insert_event(conn, "run_failed", {"reason": "budget", "exit_code": 4})
    monkeypatch.setenv("ATTEMPT", "2")
    with db.connect() as conn:
        assert db.last_failed_exit_code(conn, 1) == 4


@pytest.mark.usefixtures("incident")
def test_guard_sees_nothing_after_hard_death(monkeypatch):
    """A hard death writes no run_failed row: the guard returns None and the
    restart proceeds — §8e's infra tier."""
    monkeypatch.setenv("ATTEMPT", "2")
    with db.connect() as conn:
        assert db.last_failed_exit_code(conn, 1) is None


@pytest.mark.usefixtures("incident")
def test_guard_ignores_other_events_and_attempts(monkeypatch):
    """Only run_failed rows of the asked attempt count."""
    with db.connect() as conn:
        db.insert_event(conn, "run_started", {"model": "x"})
    monkeypatch.setenv("ATTEMPT", "2")
    with db.connect() as conn:
        db.insert_event(conn, "run_failed", {"reason": "wall", "exit_code": 3})
    with db.connect() as conn:
        assert db.last_failed_exit_code(conn, 1) is None
        assert db.last_failed_exit_code(conn, 2) == 3
