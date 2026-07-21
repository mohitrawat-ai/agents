-- 002: the three roles, grants per decision.md P9 §5 (as amended: SELECT allowed,
-- scope is wrapper-level per the 2026-07-21 re-ruling; UPDATE/DELETE are what the
-- DB itself forbids the agent).
-- Apply: psql "$DATABASE_URL" -f rca/db/migrations/002_roles.sql
-- Prompts for the three passwords; nothing lands in the file or shell history.

\prompt 'password for rca_agent: ' pw_agent
\prompt 'password for rca_service: ' pw_service
\prompt 'password for rca_poller: ' pw_poller

BEGIN;

CREATE ROLE rca_agent LOGIN PASSWORD :'pw_agent';
CREATE ROLE rca_service LOGIN PASSWORD :'pw_service';
CREATE ROLE rca_poller LOGIN PASSWORD :'pw_poller';

-- rca_agent: the investigation task. Append-only on evidence; reads its own
-- incident (raw) and its own queries back for the self-check. No UPDATE, no
-- DELETE, anywhere — that is invariant 3, enforced here.
GRANT SELECT ON incidents TO rca_agent;
GRANT INSERT ON queries, events, documents TO rca_agent;
GRANT SELECT ON queries TO rca_agent;

-- rca_service: ingress/router/Q&A. Owns operational state; reads evidence
-- (Q&A reads rca.md through this role, §8a-C).
GRANT SELECT, INSERT, UPDATE ON incidents TO rca_service;
GRANT SELECT ON queries, events, documents TO rca_service;

-- rca_poller: narration. Reads events and the incident's channel/thread_ts;
-- writes exactly one column — its cursor.
GRANT SELECT ON events, incidents TO rca_poller;
GRANT UPDATE (narrated_through) ON incidents TO rca_poller;

COMMIT;
