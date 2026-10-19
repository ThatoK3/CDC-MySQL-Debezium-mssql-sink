# ML Leads — CDC environment

The data-movement environment for the [ml-leads-platform](https://github.com/ThatoK3/ml-leads-platform) (Django/DRF on PostgreSQL). It runs on its own,
so the app never has to talk to SQL Server directly and SQL Server is only touched at scheduled times.

```
                READ   (SQL Server has no CDC — it is polled, carefully, 06:00 / 12:00 / 17:00)
SQL Server ──JDBC source (UPDATE_DATE else LOAD_DATE past the offset, batches, 1-year window)──► Kafka ──JDBC sink (upsert)──► PostgreSQL  landing.*
                                                                                                                                      │ the app's loader reads it
                WRITE  (PostgreSQL has CDC — Debezium)                                                                                ▼
SQL Server CDC_IN.* ◄──JDBC sink (upsert by outbox_id)── Kafka ◄── Debezium PostgreSQL source ◄── app tables cdc_out_* (notes, assignments, fields, status)
      │ triggers (DBA-owned)
      ▼
ML_CONFIG.PROGRAMME_NOTES / _ASSIGNMENT / _FIELD_CONTROL  and  EXEC ML_LEAD.PRC_UPDATE_LEAD_STATUS
```

* **Read**: Kafka Connect's JDBC source in `timestamp+incrementing` mode with `UPDATE_DATE,LOAD_DATE` (COALESCE) and `ID`, over a query limited to the
  retention window and the programmes you list. The sources sit **paused**; `pipeline/scheduler.py` resumes them at the load times, waits until nothing new
  arrives and the sinks have caught up, then pauses them. The first window of the day also runs a *reconcile* pass (status/rank of every current lead,
  because the old app can change a status without touching `UPDATE_DATE`) and then marks leads SQL Server no longer reports as current.
* **Write**: the app stages each change in `cdc_out_*` in the same transaction that saves it (`WRITE_BACK_MODE=cdc`). Debezium streams those inserts; the sinks
  upsert them into the SQL Server inbox keyed by `outbox_id` (so redelivery cannot duplicate a note) and DBA-owned triggers perform the real INSERT / procedure
  call. The connector's login can write nothing but the inbox.

Details, decisions and what has **not** been run yet: [docs/design.md](docs/design.md).

## Start

```sh
cp .env.example .env                      # fill in SQL Server, the platform's PostgreSQL, the programme list
# one-time, by the right people (read each file first):
#   sql/sqlserver/read_login.sql  sql/sqlserver/cdc_inbox.sql      (DBA, SQL Server)
#   sql/postgres/landing.sql      sql/postgres/replication_role.sql (app database; wal_level=logical needs a restart)
#   in the app:  python manage.py migrate && python manage.py cdc_setup
docker compose up -d                      # Kafka, Kafka Connect, scheduler (registers the connectors, then waits for the next window)
python -m pipeline.connectctl status      # every connector and task
docker compose run --rm scheduler python -m pipeline.scheduler --now --first    # run a window now (first one of the day: includes the reconcile)
```

`python -m pipeline.connectctl render` shows the final connector configurations (secrets masked) and `check` lists values still missing from `.env`.
`docker compose --profile dev up -d` adds a throw-away PostgreSQL with logical WAL and the landing tables, for trying it without the real database.

## Tests

```sh
python3 -m unittest discover -s tests -t .     # no Kafka, SQL Server or Docker needed
```

They cover the generated configurations (no leftover placeholders, no personal columns, read and write logins kept apart, idempotent sinks, landing and
inbox DDL matching the connectors and the app's staged tables), `apply` against a fake Connect REST API, and the scheduler windows (idle detection,
timeout, failure, sink drain, daily reconcile).

`legacy-demo/` holds the original MySQL → Kafka → SQL Server demo this repository started from.
