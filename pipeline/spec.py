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
    group: str = "load"                        # load = every scheduled load; daily = first load of the day only; enrolment = always on, polled every few minutes
    note: str = ""
    inner: str = ""                            # a complete inner SELECT (a join) instead of SELECT columns FROM schema.table
    landing_name: str = ""                     # landing table when it is not SCHEMA__TABLE (a flattened join)

    @property
    def landing(self):
        return f"landing.{self.landing_name or (self.schema + '__' + self.table)}"

    @property
    def topic(self):
        return f"ml.read.{self.name}"

    def output_columns(self):
        """The column names the connector emits (an expression's alias, brackets removed)."""
        out = []
        for c in self.columns:
            c = c.rsplit(" AS ", 1)[-1] if " AS " in c else c
            out.append(c.strip().strip("[]"))
        return out

    def query(self):
        if self.inner:
            return f"SELECT * FROM ({self.inner}) AS t"
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

# ---- the app's own configuration tables (ML_CONFIG; design export appendix A) — small, read whole at each load; the two big ones for a recent window only
CONFIG = [
    ("cfg_programme", "PROGRAMME", ["ID", "PROGRAMME", "SOURCE_NAME", "PROGRAMME_GROUP", "SHORT_NAME", "HEALTH_ID_NAME", "PROGRAMME_CODE", "CONDITIONS_TARGETED", "PURPOSE_OF_PROGRAMME",
                                    "CURRENT_SCHEMES_OPTED_INTO_PROGRAMME", "TEAM", "MODEL_ID", "PROGRAMME_PRIORITY", "EXPIRY", "ACTIVE", "BATCH_ID", "NEXT_PROGRAMME_WAIT_DAYS"], ["ID"]),
    ("cfg_sub_programme", "SUB_PROGRAMME", ["ID", "SUB_PROGRAMME_CODE", "SUB_PROGRAMME_NAME", "PROGRAMME_CODE", "DISPLAY_NAME"], ["ID"]),
    ("cfg_condition", "PROGRAMME_CONDITION", ["ID", "PROGRAMME_CODE", "CONDITION_CODE", "TYPE"], ["ID"]),
    ("cfg_scheme", "PROGRAMME_SCHEME", ["ID", "PROGRAMME_CODE", "MEDICAL_AID_NO"], ["ID"]),
    ("cfg_demographic", "PROGRAMME_DEMOGRAPHIC", ["ID", "PROGRAMME_CODE", "MIN_AGE", "MAX_AGE", "GENDER"], ["ID"]),
    ("cfg_field", "PROGRAMME_FIELD", ["ID", "PROGRAMME_CODE", "FIELD_NAME", "FIELD_TYPE", "FIELD_DATA_TYPE", "FIELD_OPTIONS", "DEFAULT_VALUE", "MANDATORY"], ["ID"]),
    ("cfg_notification_config", "PROGRAMME_NOTIFICATION_CONGIF", ["PROGRAMME_NOTIFICATION_CONGIF_ID", "PROGRAMME_CODE", "TYPE", "ADDRESS", "PROGRAMME_STATUS", "INSERT_DATE"],
     ["PROGRAMME_NOTIFICATION_CONGIF_ID"]),
    ("cfg_batch", "BATCH", ["BATCH_ID", "BATCH_NAME", "BATCH_DESCR", "BATCH_ENABLED", "BATCH_SCHEDULE_ID", "MAX_RUN_MINS", "BATCH_GROUP_NAME", "OWNER"], ["BATCH_ID"]),
    ("cfg_batch_status", "BATCH_STATUS", ["BATCH_STATUS_ID", "BATCH_STATUS_NAME", "BATCH_STATUS_DESCR"], ["BATCH_STATUS_ID"]),
]
for _name, _table, _cols, _pk in CONFIG:
    READS.append(Read(_name, "ML_CONFIG", _table, _cols, _pk, "bulk", note="Small configuration table, read whole."))
READS += [
    Read("cfg_batch_execution", "ML_CONFIG", "BATCH_EXECUTION", ["BATCH_EXECUTION_ID", "BATCH_ID", "BATCH_START_EXECUTION_TIME", "BATCH_END_EXECUTION_TIME", "BATCH_STATUS_ID", "COMMENT"],
         ["BATCH_EXECUTION_ID"], "bulk", where="BATCH_START_EXECUTION_TIME >= DATEADD(day, -14, CAST(GETDATE() AS date))", note="~1.5M rows: the last two weeks; a run's status changes in place, so the window is re-read."),
    Read("cfg_notification_control", "ML_CONFIG", "PROGRAMME_NOTIFICATION_CONTROL", ["ID", "PROGRRAMME_CODE", "BATCH_EXECUTION_ID", "BATCH_STATUS_ID", "EMAIL_SENT_TO", "PROGRAMME_STATUS", "INSERT_DATE"],
         ["ID"], "bulk", where="INSERT_DATE >= DATEADD(day, -30, CAST(GETDATE() AS date))"),
    Read("field_control", "ML_CONFIG", "PROGRAMME_FIELD_CONTROL", ["ID", "PROGRAMME_ELIGIBILITY_ID", "PROGRAMME_CODE", "FIELD_NAME", "FIELD_VALUE", "LOAD_DATE", "FIELD_CONTROL_BATCH_NO"], ["ID"],
         "incrementing", incrementing="ID", where=WINDOW, note="Values coaches entered (~795k rows; the retention window). Append-only, so ID only grows."),
]

# ---- enrolment events (RuleEngine's sources): HealthID, PHP, Workflow, Excel. The app reads these every few minutes, so the always-on group polls every ENROL_POLL_MS.
HASH = "CONVERT(varchar(64), HASHBYTES('SHA2_256', CONCAT_WS('|', {cols})), 2) AS ROW_KEY"
FEED = "${ENROL_FEED_DAYS}"                  # days re-read each poll (3); set it to the retention window once, to backfill, then back
WF = ["ENTITY_NO", "Programme", "DATE_COMPLETED_SKEY", "[Programme status]", "[Enrollment status]", "[Attempt Outcome]", "[Call Attempt Outcome]", "[Interaction outcome]"]
READS += [
    Read("enrol_php", "ML_STAGE", "LEADS_PHP_ENROLLMENTS", ["ENTITY_NO", "PROGRAMME", "STATUS", "STATUS_FROM_DATE", "STATUS_TO_DATE", "ROW_ID", "LOAD_DATE"], ["ROW_ID"], "timestamp+incrementing",
         incrementing="ROW_ID", timestamps=["LOAD_DATE"], group="enrolment", where="LOAD_DATE >= DATEADD(day, -${RETENTION_DAYS}, CAST(GETDATE() AS date)) AND PROGRAMME LIKE 'DHCS_COACHING_%' AND STATUS <> 'ELIGIBLE'",
         note="PHP statuses by LOAD_DATE; ROW_ID orders rows loaded together."),
    Read("enrol_workflow", "ML_STAGE", "LEADS_WORKFLOW_ENROLLMENTS", WF + [HASH.format(cols=", ".join(WF))], ["ROW_KEY"], "bulk", group="enrolment",
         where=f"DATE_COMPLETED_SKEY >= CONVERT(int, CONVERT(char(8), DATEADD(day, -{FEED}, GETDATE()), 112))",
         note="Workflow has no key and only a day: the last few days are re-read; a hash of the row is its key, so a repeat changes nothing."),
    Read("enrol_healthid", "WH_HS_RECOMMENDER", "HEALTHID_STATUS", ["ENTITY_NO", "PATHWAY_ID", "COHORT_NAME", "STATUS_TYPE", "BUS_EFF_FROM", "SYS_EFF_FROM", "REASON_DESC", "ROW_KEY"], ["ROW_KEY"], "timestamp", timestamps=["SYS_EFF_FROM"], group="enrolment", landing_name="WH_HS_RECOMMENDER__HEALTHID_STATUS",
         inner=("SELECT p.ENTITY_NO, p.PATHWAY_ID, k.COHORT_NAME, s.STATUS_TYPE, s.BUS_EFF_FROM, s.SYS_EFF_FROM, s.REASON_DESC, "
                + HASH.format(cols="p.ENTITY_NO, p.PATHWAY_ID, s.STATUS_TYPE, s.BUS_EFF_FROM") +
                " FROM ${HEALTHID_DATABASE}.WH_HS_RECOMMENDER.COHORT k INNER JOIN ${HEALTHID_DATABASE}.WH_HS_RECOMMENDER.PATHWAY p ON k.ENTITY_NO = p.ENTITY_NO AND p.CONTEXT_TYPE = 'COACHING' "
                "INNER JOIN ${HEALTHID_DATABASE}.WH_HS_RECOMMENDER.PATHWAY_STATUS s ON p.PATHWAY_ID = s.PATHWAY_ID "
                "WHERE k.COHORT_NAME LIKE '%COACHING%' AND s.BUS_EFF_FROM >= DATEADD(day, -${RETENTION_DAYS}, CAST(GETDATE() AS date)) "
                # as RuleEngine's sql/healthID.sql: only the row versions in effect now (the warehouse keeps superseded corrections)
                "AND GETDATE() BETWEEN k.SYS_EFF_FROM AND k.SYS_EFF_TO AND GETDATE() BETWEEN p.SYS_EFF_FROM AND p.SYS_EFF_TO AND GETDATE() BETWEEN s.SYS_EFF_FROM AND s.SYS_EFF_TO"),
         note="The HealthID pathway status versions, flattened (cohort + pathway + status) so one table carries them; a new version appears with a new SYS_EFF_FROM."),
]
for _n, _tbl, _col in (("enrol_excel_coaching", "${EXCEL_COACHING_TABLE}", "[Programme Status]"), ("enrol_excel_enrollments", "${EXCEL_ENROLLMENTS_TABLE}", "[Attempt Outcome]")):
    _cols = ["[Entity Number]", "Programme", "Date", _col]
    READS.append(Read(_n, "ML_TEMP", _tbl, _cols + [HASH.format(cols=", ".join(_cols))], ["ROW_KEY"], "bulk", group="daily",
                      landing_name="ML_TEMP__COACHING 2026" if "COACHING" in _tbl else "ML_TEMP__ENROLLMENTS 2026",
                      where="Date >= DATEADD(day, -${RETENTION_DAYS}, CAST(GETDATE() AS date))", note="Yearly manual Excel imports: read whole once a day."))


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
    if r.group == "enrolment":
        # always on. HealthID's tables are eventful: every few minutes. The ML_STAGE tables are refilled by batch pipelines (a few times a day, more as programmes are added): a longer poll
        cfg["poll.interval.ms"] = "${ENROL_POLL_MS}" if r.name == "enrol_healthid" else "${ENROL_STAGE_POLL_MS}"
    elif r.mode == "bulk":
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
        "ENROL_POLL_MS": "300000", "ENROL_STAGE_POLL_MS": "600000", "ENROL_FEED_DAYS": "3", "HEALTHID_DATABASE": "PSTAGING", "EXCEL_COACHING_TABLE": "[COACHING 2026]", "EXCEL_ENROLLMENTS_TABLE": "[ENROLLMENTS 2026]",
        "DB_TIMEZONE": "Africa/Johannesburg", "MSSQL_PORT": "1433", "MSSQL_ENCRYPT": "true", "MSSQL_TRUST_CERT": "false",
        "LANDING_PORT": "5432", "APP_PG_PORT": "5432", "WRITEBACK_SLOT": "mlp_writeback", "WRITEBACK_PUBLICATION": "mlp_writeback",
    }
    d.update({k: v for k, v in env.items() if v != ""})
    return d


if __name__ == "__main__":
    print(json.dumps({n: {"group": g, "config": c} for n, (g, c) in all_connectors().items()}, indent=2))
