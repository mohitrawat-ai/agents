-- 003: rca_agent gains SELECT on events (design.md §8e, issue #9).
-- The retry guard: on attempt > 1, run.py reads the previous attempt's
-- run_failed row to refuse re-running policy stops (exit 1, 4). Scope is
-- wrapper-level like the queries read (P9 §5 re-ruling); the DB still
-- forbids UPDATE and DELETE — invariant 3 untouched.
-- Apply: psql "$DATABASE_URL" -f rca/db/migrations/003_agent_reads_events.sql

GRANT SELECT ON events TO rca_agent;
