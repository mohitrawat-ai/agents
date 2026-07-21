# NR account telemetry — what works, what doesn't

Copied from ingren-rca/tools/NR_NOTES.md on 2026-07-18 (this copy is canonical;
ingren-rca's freezes with that repo). Findings originally from 2026-05-28
discovery against the partner's `REST API - Python` app, appended since.
Captured so the next person doesn't redo the trial-and-error. Two sections
did not make the copy: AWS-side telemetry moved to
`tools/cloudwatch/CW_NOTES.md` (per-source notes), and seasonal-pipeline
method tuning stayed with ingren-rca (about the detector, not the instrument).

## Append bar — read before adding anything

This file helps the agent **operate the instrument**, never biases the search.

- **In**: instrument facts — schema quirks, filter keys per event type, unit
  conversions, queries that return empty on this account and why,
  identifier-discovery recipes. Test: *the entry is true regardless of which
  incident happens next.*
- **Out**: world priors — "X usually breaks", incident conclusions, component
  suspicion rankings. Those make the agent stop looking where the notes say
  nothing lives.

## Account identifiers

- **`appName`**: `REST API - Python`
- **`appId`**: `1450765319`
- **`entity.guid`**: `MzMwODc2M3xBUE18QVBQTElDQVRJT058MTQ1MDc2NTMxOQ`
  (= base64 of `<account_id>|APM|APPLICATION|<appId>`, but easier to copy from
  the NR UI than derive)

## Filter key per event type

| Event | Filter key that works | Notes |
|---|---|---|
| `Transaction` | `appId = 1450765319` | classic APM attribute |
| `Metric` | `entity.guid = '...'` | **`appId` returns empty on `Metric`** — must use entity GUID |
| `Span` | `appId = 1450765319` | works, but data is sparse — see "what doesn't work" |

## Working queries (production sources)

| Need | Event | Metric / field | Facet |
|---|---|---|---|
| Per-transaction latency, p95, throughput, error rate | `Transaction` | `duration`, `error` | `name` |
| Per-external-host latency + call count | `Metric` | `apm.service.external.host.duration` | `external.host` |
| Per-DB-operation latency + call count | `Metric` | `apm.service.datastore.operation.duration` | `db.system`, `db.sql.table`, `db.operation` |

Wrap metric durations with `convert(<expr>, unit, 'ms')` — native unit is seconds.

## What doesn't work on this account (don't redo)

- **`Span` with `category = 'http'`** for externals → 0 rows. The Python agent
  doesn't tag outbound HTTP as `http` spans. The `generic` category only
  contains internal Django framework spans (HealthView, CORS, WSGI).
- **`Span` with `category = 'datastore'`** → only 2 high-volume Redis ops
  survive sampling. Span data is **heavily sampled**; ~18 spans/min total.
  Don't rely on it for DB granularity.
- **`Metric` with `metricTimesliceName LIKE 'External/%'`** (legacy timeslice)
  → 0 rows. This account doesn't synthesize legacy timeslice metrics; use the
  modern `apm.service.*` dimensional metrics above.
- **`Transaction.host`** is the *application server* hostname (`rest-api-3-9-…`),
  not the external service host. Useless for per-external attribution.
- **`Transaction.externalDuration` / `databaseDuration`** are populated but
  only as totals per transaction — no per-host breakdown. Fallback if the
  Metric path fails, otherwise prefer the dimensional metrics above.

## Finding the entity GUID for a new app

The fastest path is the browser DevTools trick:

1. Open the app in NR UI.
2. DevTools → Network → filter `graphql`.
3. Reload the page.
4. Look at any GraphQL request body — the `nrql` field will contain
   `entity.guid = '<...>'`. Copy that value.

The UI's chart "View NRQL / View query" button works on some NR builds but
not all; DevTools is the reliable path.

## Tools that depend on these findings

- `tools/newrelic/nr_run_nrql.py` — generic ad-hoc NRQL runner (one query at a time).
- `tools/newrelic/nrql_log.py` — same runner, teeing into the incident record.

(ingren-rca's `nr_export_incident.py` and the incident-replay notebooks also
depend on them; those stay with that repo.)
