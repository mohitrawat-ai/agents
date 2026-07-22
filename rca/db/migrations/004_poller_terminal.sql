-- 004: the poller's completion marker (issue #10).
-- Apply: psql "$DATABASE_URL" -f rca/db/migrations/004_poller_terminal.sql
--
-- The poller loops over incidents whose terminal message is not yet posted.
-- That set must be durable: without it, a restart re-posts terminal
-- messages, and the loop polls dead executions forever. terminal_posted_at
-- records the poller's own act — "I posted the terminal message" — not an
-- incident status; Step Functions stays the authority on whether a run is
-- over (P8 §5).
--
-- P9 §5 amendment (ruled 2026-07-22 in-session, issue #10): rca_poller may
-- update exactly the columns that record its own actions — the cursor, and
-- now this marker. Nothing else changes.

BEGIN;

ALTER TABLE incidents ADD COLUMN terminal_posted_at timestamptz;

GRANT UPDATE (terminal_posted_at) ON incidents TO rca_poller;

COMMIT;
