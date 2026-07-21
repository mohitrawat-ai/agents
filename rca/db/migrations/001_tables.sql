-- 001: the four tables. Schema per design.md §5; roles and grants are 002.
-- Apply: psql "$DATABASE_URL" -f rca/db/migrations/001_tables.sql

BEGIN;

CREATE TABLE incidents (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    event_id         text NOT NULL UNIQUE,       -- Slack delivery id; the idempotency guard (P4 §6, no expiry)
    channel          text NOT NULL,
    thread_ts        text NOT NULL,
    slug             text NOT NULL,              -- display label only, allowed to collide (P4 §2)
    raw              jsonb NOT NULL,             -- the envelope; dedup columns backfill from this if ever built (§8a-D)
    received_utc     timestamptz,
    narrated_through bigint NOT NULL DEFAULT 0,  -- poller's cursor into events (P8 §4)
    created_at       timestamptz NOT NULL DEFAULT now()
);

-- the router's lookup (P3 §3); not UNIQUE — thread-to-incident cardinality is #11's concern
CREATE INDEX incidents_channel_thread_ts ON incidents (channel, thread_ts);

CREATE TABLE queries (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    incident_id uuid NOT NULL REFERENCES incidents (id),
    attempt     integer NOT NULL,                -- restart discriminator (§8a-B); from the task environment
    qid         text NOT NULL,                   -- q01, q02, … minted by the wrapper's INSERT … RETURNING (#4)
    source      text NOT NULL CHECK (source IN ('nrql', 'aws')),
    purpose     text NOT NULL,
    query       text NOT NULL,
    elapsed_s   double precision,
    rows        integer,
    result      jsonb,
    error       text,                            -- failed and dead-end queries are rows too (P2 §1)
    created_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (incident_id, attempt, qid)           -- duplicate-q02 impossible, not carefully avoided
);

CREATE INDEX queries_incident_attempt ON queries (incident_id, attempt);

CREATE TABLE events (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,  -- monotonic; the cursor keys off it
    incident_id uuid NOT NULL REFERENCES incidents (id),
    attempt     integer NOT NULL,
    ts          timestamptz NOT NULL DEFAULT now(),
    event       text NOT NULL,
    payload     jsonb
);

CREATE INDEX events_incident_id ON events (incident_id, id);

CREATE TABLE documents (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    incident_id uuid NOT NULL REFERENCES incidents (id),
    attempt     integer NOT NULL,
    name        text NOT NULL,                   -- 'rca.md' or 'feedback.md'
    content     text NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX documents_incident_id ON documents (incident_id);

COMMIT;
