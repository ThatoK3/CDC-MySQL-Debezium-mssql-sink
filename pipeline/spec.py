"""What flows where, written once, and the Kafka Connect configurations built from it.

READ  (SQL Server -> PostgreSQL landing tables). SQL Server has no CDC, so a JDBC *source* polls it the careful way: only rows whose
      COALESCE(UPDATE_DATE, LOAD_DATE) moved past the connector's offset, in small batches, inside the retention window. The connectors are
      paused except at the scheduled load times (scheduler.py). A JDBC *sink* upserts the rows into `landing.*` in the platform's PostgreSQL;
      the Django app reads those tables with the loader it already has, so SQL Server is touched by this environment only.

WRITE (PostgreSQL -> SQL Server). Debezium's PostgreSQL connector streams the inserts the app stages in `cdc_out_*` (publication
      `mlp_writeback`); JDBC sinks upsert them into the SQL Server inbox `CDC_IN.*` keyed by outbox_id (a replay is a no-op) and DBA-owned
      triggers turn each new inbox row into the real INSERT / procedure call (sql/sqlserver/cdc_inbox.sql).

Names, columns and the programme/retention filters come from the environment, never from a request. Nothing here is executed by importing it.
"""
import json
import os
from dataclasses import dataclass, field

CONVERTERS = {
    "key.converter": "org.apache.kafka.connect.json.JsonConverter", "key.converter.schemas.enable": "true",
    "value.converter": "org.apache.kafka.connect.json.JsonConverter", "value.converter.schemas.enable": "true",
}

ELIGIBILITY_COLUMNS = (["ID", "ENTITY_NO", "PROGRAMME_CODE", "SUB_PROGRAMME_CODE", "SUB_PROGRAMME_NAME", "FINAL_SCORE", "SCORE_CATEGORY", "PRIORITY_RANK", "STATUS",
                        "SYS_EFF_FROM", "SYS_EFF_TO", "LOAD_DATE", "UPDATE_DATE", "BATCHSEQUENCE", "PROCESS_ID", "AUTHORISATION_ID"]
                       + [f"NUMVAL{i}" for i in range(1, 6)] + [f"CHARVAL{i}" for i in range(1, 6)] + [f"DATEVAL{i}" for i in range(1, 6)] + ["IS_CURRENT"])
# Contact details are copied because coaches phone members; the ID number, date of birth and legal reference never leave SQL Server.
MEMBER_COLUMNS = ["ENTITY_NO", "TITLE", "FIRST_NAME", "SURNAME", "INITIALS", "FULL_NAME", "PREFERRED_NAME", "GENDER", "MEDICAL_AID", "PLAN",
                  "CELLPHONE", "WHATSAPP_NO", "EMAIL", "LOAD_DATE", "BATCHSEQUENCE"]


@dataclass
class Read:
    """One SQL Server object copied into one landing table."""
    name: str                                  # connector / topic suffix
    schema: str                                # SQL Server schema and object
    table: str
    columns: list
    pk: list                                   # landing primary key (what the upsert matches on)
    mode: str                                  # timestamp+incrementing | timestamp | incrementing | bulk
    incrementing: str = ""
    timestamps: list = field(default_factory=list)   # COALESCEd in this order: the first non-null date says when the row last changed
    where: str = ""                            # extra WHERE, written here by us, using ${VARS}
    group: str = "load"                        # load = every scheduled load; daily = first load of the day only
    note: str = ""

    @property
    def landing(self):
        return f"landing.{self.schema}__{self.table}"

    @property
    def topic(self):
        return f"ml.read.{self.name}"

    def query(self):
        cols = ", ".join(self.columns)
        where = f" WHERE {self.where}" if self.where else ""
        return f"SELECT * FROM (SELECT {cols} FROM {self.schema}.{self.table}{where}) AS t"


WINDOW = "LOAD_DATE >= DATEADD(day, -${RETENTION_DAYS}, CAST(GETDATE() AS date))"
PROGRAMMES = "${PROGRAMME_FILTER}"                      # rendered to "AND PROGRAMME_CODE IN (…)" or nothing

READS = [
    Read("eligibility", "ML_LEAD", "VW_ENTITY_PROGRAMME_ELIGIBILITY_HISTORY", ELIGIBILITY_COLUMNS, ["ID"], "timestamp+incrementing", incrementing="ID",
         timestamps=["UPDATE_DATE", "LOAD_DATE"], where=f"{WINDOW} {PROGRAMMES}",
         note="The lead rows. New or changed since the offset (UPDATE_DATE else LOAD_DATE), IS_CURRENT included so superseded rows arrive too."),
    Read("members", "ML_LEAD", "ENTITY_DETAILS", MEMBER_COLUMNS, ["ENTITY_NO"], "timestamp", timestamps=["LOAD_DATE"],
         where="LOAD_DATE >= DATEADD(day, -${RETENTION_DAYS}, CAST(GETDATE() AS date)) AND ENTITY_NO IN (SELECT CAST(ENTITY_NO AS varchar(1000)) FROM ML_LEAD.VW_ENTITY_PROGRAMME_ELIGIBILITY_HISTORY "
               "WHERE IS_CURRENT = 1 AND " + WINDOW + " " + PROGRAMMES + ")",
         note="Members of current leads. Have the DBA check the plan: it is a semi-join onto the history view."),
    Read("notes", "ML_CONFIG", "PROGRAMME_NOTES", ["ID", "PROGRAMME_ELIGIBILITY_ID", "PROGRAMME_CODE", "USERNAME", "NOTES", "LOAD_DATE"], ["ID"], "incrementing", incrementing="ID",
         where="LOAD_DATE >= DATEADD(day, -${RETENTION_DAYS}, CAST(GETDATE() AS date)) AND ISNULL(PROGRAMME_ELIGIBILITY_ID, 0) > 0", note="Append-only; ID only grows."),
    Read("assignments", "ML_CONFIG", "PROGRAMME_ASSIGNMENT", ["ID", "PROGRAMME_ELIGIBILITY_ID", "PROGRAMME_CODE", "ASSIGNEE_ENTITY_NO", "ASSIGNED_BY_ENTITY_NO", "LOAD_DATE", "ASSIGNMENT_BATCH_NO"],
         ["ID"], "incrementing", incrementing="ID", where="LOAD_DATE >= DATEADD(day, -${RETENTION_DAYS}, CAST(GETDATE() AS date))", note="Append-only; ID only grows."),
    Read("staff", "ML_LEAD", "DISCOVERY_PERSONNEL", ["ENTITY_NO", "NAME", "EMAIL", "TEAM"], ["ENTITY_NO"], "bulk", note="Small; read whole at each load."),
    Read("roles", "ML_CONFIG", "PROGRAMME_ROLE", ["ID", "PROGRAMME_CODE", "ROLE", "ENTITY_NO", "LOAD_DATE"], ["ID"], "bulk", note="Small; read whole at each load."),
    Read("reconcile", "ML_LEAD", "VW_ENTITY_PROGRAMME_ELIGIBILITY_HISTORY", ["ID", "STATUS", "PRIORITY_RANK", "IS_CURRENT"], ["ID"], "bulk", group="daily",
         where=f"IS_CURRENT = 1 AND {WINDOW} {PROGRAMMES}",
         note="Once a day: status and rank of every current lead, because the old app can change a status without touching UPDATE_DATE. Only these columns are upserted; "
              "afterwards landing.finish_reconcile() marks leads that are no longer current."),
]


def name(prefix, kind, r):
    return f"{prefix}-{kind}-{r.name}"


def read_source(r, prefix="mlp"):
    """JDBC source: SQL Server -> Kafka. Gentle by construction: bounded batches, a settle delay for rows still being committed, no polling
    outside the scheduled windows (the scheduler resumes it, waits until it is idle, then pauses it again)."""
    cfg = {
        "connector.class": "io.confluent.connect.jdbc.JdbcSourceConnector", "tasks.max": "1",
        "connection.url": "jdbc:sqlserver://${MSSQL_HOST}:${MSSQL_PORT};databaseName=${MSSQL_DB};encrypt=${MSSQL_ENCRYPT};trustServerCertificate=${MSSQL_TRUST_CERT};applicationIntent=ReadOnly",
        "connection.user": "${MSSQL_READ_USER}", "connection.password": "${MSSQL_READ_PASSWORD}", "connection.attempts": "3", "connection.backoff.ms": "10000",
        "mode": r.mode, "query": r.query(), "topic.prefix": r.topic, "batch.max.rows": "${BATCH_MAX_ROWS}", "poll.interval.ms": "${POLL_INTERVAL_MS}",
        "db.timezone": "${DB_TIMEZONE}", "validate.non.null": "false", "numeric.mapping": "best_fit", **CONVERTERS,
    }
    if r.mode == "bulk":
        cfg["poll.interval.ms"] = "86400000"                        # one pass per resume; the scheduler pauses it when it is idle
    else:
        cfg["timestamp.delay.interval.ms"] = "${SETTLE_MS}"
    if "timestamp" in r.mode:
        cfg["timestamp.column.name"] = ",".join(r.timestamps)
        cfg["timestamp.initial"] = "${TIMESTAMP_INITIAL_MS}"        # the first start reads the retention window, not all of history
    if "incrementing" in r.mode:
        cfg["incrementing.column.name"] = r.incrementing
    return name(prefix, "src", r), cfg


def landing_sink(r, prefix="mlp"):
    """JDBC sink: Kafka -> landing table. Upsert by primary key, so a replay or an overlapping read changes nothing."""
    cfg = {
        "connector.class": "io.confluent.connect.jdbc.JdbcSinkConnector", "tasks.max": "1", "topics": r.topic,
        "connection.url": "jdbc:postgresql://${LANDING_HOST}:${LANDING_PORT}/${LANDING_DB}", "connection.user": "${LANDING_USER}", "connection.password": "${LANDING_PASSWORD}",
        "table.name.format": r.landing, "insert.mode": "upsert", "pk.mode": "record_value", "pk.fields": ",".join(r.pk),
        "auto.create": "false", "auto.evolve": "false", "quote.sql.identifiers": "always", "batch.size": "3000", "db.timezone": "${DB_TIMEZONE}",
        "errors.tolerance": "none", "errors.log.enable": "true", **CONVERTERS,
    }
    if r.name == "reconcile":
        # stamp each upserted row so landing.finish_reconcile() can tell which current leads were NOT seen today
        cfg.update({"transforms": "seen", "transforms.seen.type": "org.apache.kafka.connect.transforms.InsertField$Value", "transforms.seen.timestamp.field": "_reconciled_at"})
    return name(prefix, "sink", r), cfg


# ----- write-back: PostgreSQL staged rows -> SQL Server inbox -------------------------------------------------------
@dataclass
class Write:
    name: str
    staged: str              # PostgreSQL table the app writes
    inbox: str               # SQL Server inbox table
    pk: str = "outbox_id"

    @property
    def topic(self):
        return f"mlp.public.{self.staged}"


WRITES = [Write("note", "cdc_out_note", "CDC_IN.NOTE"), Write("assignment", "cdc_out_assignment", "CDC_IN.ASSIGNMENT"),
          Write("field", "cdc_out_field", "CDC_IN.FIELD"), Write("status", "cdc_out_status", "CDC_IN.STATUS")]


def writeback_source(prefix="mlp"):
    """Debezium PostgreSQL source on the write-back publication (insert only). The app creates the publication; the connector must not."""
    cfg = {
        "connector.class": "io.debezium.connector.postgresql.PostgresConnector", "tasks.max": "1", "plugin.name": "pgoutput",
        "database.hostname": "${APP_PG_HOST}", "database.port": "${APP_PG_PORT}", "database.user": "${APP_PG_REPLICATION_USER}", "database.password": "${APP_PG_REPLICATION_PASSWORD}",
        "database.dbname": "${APP_PG_DB}", "topic.prefix": "mlp", "slot.name": "${WRITEBACK_SLOT}", "slot.drop.on.stop": "false",
        "publication.name": "${WRITEBACK_PUBLICATION}", "publication.autocreate.mode": "disabled",
        "table.include.list": ",".join(f"public.{w.staged}" for w in WRITES),
        "snapshot.mode": "no_data",                                  # only changes from now on: start this connector BEFORE switching the app to WRITE_BACK_MODE=cdc
        "heartbeat.interval.ms": "10000", "tombstones.on.delete": "false", "decimal.handling.mode": "string",
        "transforms": "unwrap", "transforms.unwrap.type": "io.debezium.transforms.ExtractNewRecordState", "transforms.unwrap.drop.tombstones": "true",
        **CONVERTERS,
    }
    return f"{prefix}-cdc-writeback", cfg


def inbox_sink(w, prefix="mlp"):
    """JDBC sink: Kafka -> SQL Server inbox. Upsert on outbox_id, so at-least-once delivery cannot create a second note."""
    cfg = {
        "connector.class": "io.confluent.connect.jdbc.JdbcSinkConnector", "tasks.max": "1", "topics": w.topic,
        "connection.url": "jdbc:sqlserver://${MSSQL_HOST}:${MSSQL_PORT};databaseName=${MSSQL_DB};encrypt=${MSSQL_ENCRYPT};trustServerCertificate=${MSSQL_TRUST_CERT}",
        "connection.user": "${MSSQL_INBOX_USER}", "connection.password": "${MSSQL_INBOX_PASSWORD}",
        "table.name.format": w.inbox, "insert.mode": "upsert", "pk.mode": "record_value", "pk.fields": w.pk, "auto.create": "false", "auto.evolve": "false",
        "quote.sql.identifiers": "never", "batch.size": "500", "max.retries": "10", "retry.backoff.ms": "10000",
        "errors.tolerance": "none", "errors.log.enable": "true", "errors.log.include.messages": "false", **CONVERTERS,    # never log note text
    }
    return f"{prefix}-sink-{w.name}", cfg


def all_connectors(prefix="mlp"):
    """{connector name: (group, config)}. Groups: load / daily (read side, scheduled), writeback (always on)."""
    out = {}
    for r in READS:
        n, c = read_source(r, prefix)
        out[n] = (r.group, c)
        n, c = landing_sink(r, prefix)
        out[n] = ("sink", c)
    n, c = writeback_source(prefix)
    out[n] = ("writeback", c)
    for w in WRITES:
        n, c = inbox_sink(w, prefix)
        out[n] = ("writeback", c)
    return out


def env_values(env=None):
    """Template values: defaults for everything that is not a secret or an address."""
    env = dict(os.environ if env is None else env)
    days = int(env.get("RETENTION_DAYS", "365"))
    import time
    codes = ",".join(c.strip() for c in env.get("PROGRAMME_CODES", "").split(",") if c.strip())
    if codes and not all(c.isdigit() for c in codes.split(",")):
        raise ValueError("PROGRAMME_CODES must be a comma-separated list of numbers")
    d = {
        "RETENTION_DAYS": str(days), "PROGRAMME_FILTER": f"AND PROGRAMME_CODE IN ({codes})" if codes else "",
        "TIMESTAMP_INITIAL_MS": str(int((time.time() - days * 86400) * 1000)), "BATCH_MAX_ROWS": "5000", "POLL_INTERVAL_MS": "2000", "SETTLE_MS": "60000",
        "DB_TIMEZONE": "Africa/Johannesburg", "MSSQL_PORT": "1433", "MSSQL_ENCRYPT": "true", "MSSQL_TRUST_CERT": "false",
        "LANDING_PORT": "5432", "APP_PG_PORT": "5432", "WRITEBACK_SLOT": "mlp_writeback", "WRITEBACK_PUBLICATION": "mlp_writeback",
    }
    d.update({k: v for k, v in env.items() if v != ""})
    return d


if __name__ == "__main__":
    print(json.dumps({n: {"group": g, "config": c} for n, (g, c) in all_connectors().items()}, indent=2))
