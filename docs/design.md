# Design

## Why two different mechanisms

SQL Server has **no CDC** (it needs SQL Server Agent, and a primary key or unique index on each captured table; none of the `ML_LEAD` tables has one) and
the server is sensitive. PostgreSQL has CDC for free (logical replication). So:

| Direction | Mechanism | Why |
|---|---|---|
| SQL Server → PostgreSQL | scheduled, windowed JDBC reads by `UPDATE_DATE` / `LOAD_DATE` | the only change markers the source has; bounded batches; the connectors are paused outside the load times |
| PostgreSQL → SQL Server | Debezium (`pgoutput`, publication `mlp_writeback`, insert only) → Kafka → JDBC sink into an inbox | app changes go to PostgreSQL first, then reach SQL Server at-least-once and in order per table |

## Read side

`pipeline/spec.py` lists seven reads: eligibility history (`timestamp+incrementing`, `UPDATE_DATE,LOAD_DATE`, `ID`), members, notes and assignments
(`incrementing` on `ID`), staff and roles (`bulk`, small) and the daily `reconcile` (bulk: `ID, STATUS, PRIORITY_RANK, IS_CURRENT` of current leads). Each lands in
`landing."SCHEMA__OBJECT"` with the same upper-case names, so the app's existing loader reads them unchanged: in the app set

```
LEGACY_DATABASE_URL=postgres://<landing_reader>@<host>/<db>?options=-csearch_path%3Dlanding
LEGACY_FLAT_NAMES=true
```

(This path was checked against a real PostgreSQL with the platform's loader and sync check: snapshot, incremental and `verify_sync` all worked on landing tables.)
The app keeps its snapshot/incremental logic, `IS_CURRENT` supersession, programme mapping and year window; it just reads a cheap local copy. Its own scheduled
loads and `Load now` button then cost SQL Server nothing, because only this environment reads SQL Server.

Gentleness: `BATCH_MAX_ROWS` rows per query, `SETTLE_MS` ignores rows committed in the last minute, `applicationIntent=ReadOnly`, read-only login, paused
connectors, a `MAX_RUN_MINUTES` ceiling per window. The first start reads the retention window (`timestamp.initial`), not all history.

Not captured by dates, handled by the daily reconcile: a status or rank change made without touching `UPDATE_DATE` (open question N6/N-status in the platform), and a
lead that stops being current. `landing.finish_reconcile()` marks current leads not seen in the pass (and unchanged since the window opened) as `IS_CURRENT = 0`.

## The app's own tables and the enrolment events

The design export's appendix A tables are all in `spec.READS`: the configuration tables are small and read whole in every window; `BATCH_EXECUTION` (~1.5M rows) and the notification control table for a recent window
only; `PROGRAMME_FIELD_CONTROL` (~795k) by `ID` inside the retention window. **`SYS_EFF_FROM` / `SYS_EFF_TO` are application-managed, not temporal columns: no query filters on them** (the design export §5.3).

Enrolment sources (`group="enrolment"`) are the only connectors left running: PHP by `LOAD_DATE` + `ROW_ID`; Workflow has no key and only a day (`DATE_COMPLETED_SKEY`), so the last `ENROL_FEED_DAYS` (3) are re-read and every row's key
is a SHA-256 of its values (a repeat is a no-op); HealthID's cohort + pathway + status join is flattened into `WH_HS_RECOMMENDER__HEALTHID_STATUS`, versioned by `SYS_EFF_FROM`. The yearly Excel imports (`[COACHING 2026]`,
`[ENROLLMENTS 2026]`, names in `.env`; edit the landing DDL too when a new year starts) are read once a day. To backfill a year of enrolment history once, apply with `ENROL_FEED_DAYS=365`, let it finish, then set it back to 3.

## Write side

The app (WRITE_BACK_MODE=cdc) writes `cdc_out_note | _assignment | _field | _status` rows shaped like the SQL Server targets, in the same transaction as the change,
and marks the outbox event handed over. The publication `mlp_writeback` publishes **inserts only** of those four tables. The Debezium connector must be started
before the app is switched to `cdc` (`snapshot.mode=no_data` sends only new changes). The inbox tables `CDC_IN.*` take the events by upsert on `outbox_id`; their
`AFTER INSERT` triggers write `PROGRAMME_NOTES`, `PROGRAMME_ASSIGNMENT`, `PROGRAMME_FIELD_CONTROL` and call `PRC_UPDATE_LEAD_STATUS`. A status the app saved is not overwritten by
loaded data for `CDC_ROUNDTRIP_MINUTES` (default 120), the round trip through SQL Server and the next load.

Ordering: per table it is preserved; across tables it is not (a status can overtake a note) — harmless here.

## Personal data

Kafka carries note text and member contact details: topic retention is seven days; `errors.log.include.messages=false` on the write sinks; the ID number, date of birth
and legal reference are never selected; the Debezium connector reads only the four staged tables. Grant `mlp_cdc_read` column-level SELECT on `ENTITY_DETAILS` if you
want the database to enforce that.

## What has not been run

* Nothing in this repository has run against a live Kafka, SQL Server or Debezium: the unit tests use fakes, and the landing DDL and the app's read path were
  checked on PostgreSQL only. First runs should be on a copy with one programme (`PROGRAMME_CODES=`), watching `connectctl status` and the app's data-engineering page.
* **DBA to check**: the plan of the eligibility query (`IS_CURRENT`/`PRIORITY_RANK` are computed inside the view — does the date filter push down?), the members semi-join,
  that `PRC_UPDATE_LEAD_STATUS` can be called from a trigger and whether it sets `UPDATE_DATE`, the time zone of `LOAD_DATE` (D18) and of the trigger conversion, the JDBC driver
  version (`mssql-jdbc` 8.4.1 is bundled; use a current 12.x for modern TLS).
* JDBC sinks to SQL Server use `MERGE`; a failing trigger fails the batch and the task stops (`errors.tolerance=none`) — deliberate: fix and restart, nothing is skipped silently.
* `timestamp+incrementing` over `COALESCE(UPDATE_DATE, LOAD_DATE)`: a row whose date columns never move after its first load but whose other columns change is missed until the daily reconcile (status and rank only).
