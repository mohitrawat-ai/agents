-- 005: rca_service gains INSERT on events (design.md §8f, issue #12).
-- The qa-worker records a qa_answered row (cost_usd, turns, elapsed_s)
-- per question — its own act, under the incident and attempt it answered
-- from (P6 §6, P9 §5). Without it the daily spend query sees only the
-- investigation half of the spend. INSERT only: events stays append-only
-- for every role — invariant 3 untouched.
-- Apply: psql "$DATABASE_URL" -f rca/db/migrations/005_service_writes_qa_events.sql

GRANT INSERT ON events TO rca_service;
