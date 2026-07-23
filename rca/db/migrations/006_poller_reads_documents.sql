-- 006: the poller reads documents (§8g amendment, ruled 2026-07-23).
-- Apply: psql "$DATABASE_URL" -f rca/db/migrations/006_poller_reads_documents.sql
--
-- The poller renders rca.md as the incident channel's canvas on doc_ready,
-- so it needs the document content. SELECT only — the poller writes nothing
-- to documents; P9 §5's shape (roles get exactly what their own acts need)
-- is unchanged.

BEGIN;

GRANT SELECT ON documents TO rca_poller;

COMMIT;
