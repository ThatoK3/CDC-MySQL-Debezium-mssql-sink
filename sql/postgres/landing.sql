-- Landing tables in the platform's PostgreSQL: a copy of what the app reads from SQL Server, filled by the JDBC sinks (pipeline/spec.py).
-- Names are SCHEMA__OBJECT in upper case, exactly as the Django loader asks for them when LEGACY_FLAT_NAMES=true and its `legacy` database
-- points at this schema (see docs/design.md). Run as the landing owner:  psql "$APP_DB_URL" -f sql/postgres/landing.sql   (safe to repeat)
CREATE SCHEMA IF NOT EXISTS landing;

CREATE TABLE IF NOT EXISTS landing."ML_LEAD__VW_ENTITY_PROGRAMME_ELIGIBILITY_HISTORY" (
    "ID" bigint PRIMARY KEY, "ENTITY_NO" bigint, "PROGRAMME_CODE" integer, "SUB_PROGRAMME_CODE" integer, "SUB_PROGRAMME_NAME" varchar(300),
    "FINAL_SCORE" double precision, "SCORE_CATEGORY" varchar(100), "PRIORITY_RANK" bigint, "STATUS" varchar(100),
    "SYS_EFF_FROM" timestamp, "SYS_EFF_TO" timestamp, "LOAD_DATE" timestamp, "UPDATE_DATE" timestamp, "BATCHSEQUENCE" bigint, "PROCESS_ID" bigint, "AUTHORISATION_ID" bigint,
    "NUMVAL1" double precision, "NUMVAL2" double precision, "NUMVAL3" double precision, "NUMVAL4" double precision, "NUMVAL5" double precision,
    "CHARVAL1" varchar(1000), "CHARVAL2" varchar(1000), "CHARVAL3" varchar(1000), "CHARVAL4" varchar(1000), "CHARVAL5" varchar(1000),
    "DATEVAL1" timestamp, "DATEVAL2" timestamp, "DATEVAL3" timestamp, "DATEVAL4" timestamp, "DATEVAL5" timestamp,
    "IS_CURRENT" integer DEFAULT 1,
    "_reconciled_at" timestamptz
);
-- the loader's incremental read is "LOAD_DATE >= x OR UPDATE_DATE >= x" ordered by ID, and its snapshot slices by LOAD_DATE
CREATE INDEX IF NOT EXISTS landing_elig_load_idx ON landing."ML_LEAD__VW_ENTITY_PROGRAMME_ELIGIBILITY_HISTORY" ("LOAD_DATE", "ID");
CREATE INDEX IF NOT EXISTS landing_elig_update_idx ON landing."ML_LEAD__VW_ENTITY_PROGRAMME_ELIGIBILITY_HISTORY" ("UPDATE_DATE", "ID") WHERE "UPDATE_DATE" IS NOT NULL;
CREATE INDEX IF NOT EXISTS landing_elig_current_idx ON landing."ML_LEAD__VW_ENTITY_PROGRAMME_ELIGIBILITY_HISTORY" ("ID") WHERE "IS_CURRENT" = 1;

-- No ID number, date of birth or legal reference: they are not read from SQL Server at all.
CREATE TABLE IF NOT EXISTS landing."ML_LEAD__ENTITY_DETAILS" (
    "ENTITY_NO" varchar(1000) PRIMARY KEY, "TITLE" varchar(30), "FIRST_NAME" varchar(120), "SURNAME" varchar(120), "INITIALS" varchar(16), "FULL_NAME" varchar(480),
    "PREFERRED_NAME" varchar(120), "GENDER" varchar(40), "MEDICAL_AID" varchar(40), "PLAN" varchar(60), "CELLPHONE" varchar(22), "WHATSAPP_NO" varchar(22),
    "EMAIL" varchar(120), "LOAD_DATE" timestamp, "BATCHSEQUENCE" bigint
);

CREATE TABLE IF NOT EXISTS landing."ML_CONFIG__PROGRAMME_NOTES" (
    "ID" bigint PRIMARY KEY, "PROGRAMME_ELIGIBILITY_ID" bigint, "PROGRAMME_CODE" integer, "USERNAME" varchar(100), "NOTES" text, "LOAD_DATE" timestamp
);
CREATE TABLE IF NOT EXISTS landing."ML_CONFIG__PROGRAMME_ASSIGNMENT" (
    "ID" bigint PRIMARY KEY, "PROGRAMME_ELIGIBILITY_ID" bigint, "PROGRAMME_CODE" integer, "ASSIGNEE_ENTITY_NO" bigint, "ASSIGNED_BY_ENTITY_NO" bigint,
    "LOAD_DATE" timestamp, "ASSIGNMENT_BATCH_NO" bigint
);
CREATE TABLE IF NOT EXISTS landing."ML_LEAD__DISCOVERY_PERSONNEL" ("ENTITY_NO" varchar(1000) PRIMARY KEY, "NAME" varchar(300), "EMAIL" varchar(200), "TEAM" varchar(200));
CREATE TABLE IF NOT EXISTS landing."ML_CONFIG__PROGRAMME_ROLE" ("ID" bigint PRIMARY KEY, "PROGRAMME_CODE" integer, "ROLE" varchar(60), "ENTITY_NO" bigint, "LOAD_DATE" timestamp);

-- After the daily reconcile pass: a lead that SQL Server no longer reports as current (and that has not changed since the window opened) stops being current here.
CREATE OR REPLACE FUNCTION landing.finish_reconcile(since timestamptz) RETURNS bigint LANGUAGE plpgsql AS $$
DECLARE n bigint;
BEGIN
    UPDATE landing."ML_LEAD__VW_ENTITY_PROGRAMME_ELIGIBILITY_HISTORY" h SET "IS_CURRENT" = 0
     WHERE h."IS_CURRENT" = 1 AND (h."_reconciled_at" IS NULL OR h."_reconciled_at" < since)
       AND COALESCE(h."UPDATE_DATE", h."LOAD_DATE") < since::timestamp;
    GET DIAGNOSTICS n = ROW_COUNT;
    RETURN n;
END $$;

-- Roles: the sink writes landing; the app (its `legacy` alias) only reads it.
-- CREATE ROLE landing_writer LOGIN PASSWORD '...';  GRANT USAGE ON SCHEMA landing TO landing_writer;
-- GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA landing TO landing_writer;  GRANT EXECUTE ON FUNCTION landing.finish_reconcile(timestamptz) TO landing_writer;
-- CREATE ROLE landing_reader LOGIN PASSWORD '...';  GRANT USAGE ON SCHEMA landing TO landing_reader;  GRANT SELECT ON ALL TABLES IN SCHEMA landing TO landing_reader;

-- ================= the app's configuration tables (design export appendix A) — mirrors read by the platform's `reference` step =================
CREATE TABLE IF NOT EXISTS landing."ML_CONFIG__PROGRAMME" (
    "ID" integer PRIMARY KEY, "PROGRAMME" varchar(2000), "SOURCE_NAME" varchar(1000), "PROGRAMME_GROUP" varchar(200), "SHORT_NAME" varchar(10), "HEALTH_ID_NAME" varchar(2000),
    "PROGRAMME_CODE" integer, "CONDITIONS_TARGETED" varchar(2000), "PURPOSE_OF_PROGRAMME" varchar(2000), "CURRENT_SCHEMES_OPTED_INTO_PROGRAMME" varchar(2000), "TEAM" varchar(500),
    "MODEL_ID" varchar(2000), "PROGRAMME_PRIORITY" integer, "EXPIRY" integer, "ACTIVE" integer, "BATCH_ID" integer, "NEXT_PROGRAMME_WAIT_DAYS" integer);
CREATE TABLE IF NOT EXISTS landing."ML_CONFIG__SUB_PROGRAMME" ("ID" integer PRIMARY KEY, "SUB_PROGRAMME_CODE" integer, "SUB_PROGRAMME_NAME" varchar(2000), "PROGRAMME_CODE" integer, "DISPLAY_NAME" varchar(300));
CREATE TABLE IF NOT EXISTS landing."ML_CONFIG__PROGRAMME_CONDITION" ("ID" integer PRIMARY KEY, "PROGRAMME_CODE" integer, "CONDITION_CODE" varchar(100), "TYPE" varchar(100));
CREATE TABLE IF NOT EXISTS landing."ML_CONFIG__PROGRAMME_SCHEME" ("ID" integer PRIMARY KEY, "PROGRAMME_CODE" integer, "MEDICAL_AID_NO" integer);
CREATE TABLE IF NOT EXISTS landing."ML_CONFIG__PROGRAMME_DEMOGRAPHIC" ("ID" integer PRIMARY KEY, "PROGRAMME_CODE" integer, "MIN_AGE" integer, "MAX_AGE" integer, "GENDER" varchar(1));
CREATE TABLE IF NOT EXISTS landing."ML_CONFIG__PROGRAMME_FIELD" (
    "ID" integer PRIMARY KEY, "PROGRAMME_CODE" integer, "FIELD_NAME" varchar(100), "FIELD_TYPE" varchar(50), "FIELD_DATA_TYPE" varchar(50), "FIELD_OPTIONS" varchar(3000),
    "DEFAULT_VALUE" varchar(100), "MANDATORY" integer);
CREATE TABLE IF NOT EXISTS landing."ML_CONFIG__PROGRAMME_NOTIFICATION_CONGIF" (
    "PROGRAMME_NOTIFICATION_CONGIF_ID" bigint PRIMARY KEY, "PROGRAMME_CODE" integer, "TYPE" varchar(100), "ADDRESS" varchar(250), "PROGRAMME_STATUS" varchar(100), "INSERT_DATE" timestamp);
CREATE TABLE IF NOT EXISTS landing."ML_CONFIG__PROGRAMME_NOTIFICATION_CONTROL" (
    "ID" bigint PRIMARY KEY, "PROGRRAMME_CODE" integer, "BATCH_EXECUTION_ID" bigint, "BATCH_STATUS_ID" integer, "EMAIL_SENT_TO" varchar(2000), "PROGRAMME_STATUS" varchar(100), "INSERT_DATE" timestamp);
CREATE TABLE IF NOT EXISTS landing."ML_CONFIG__BATCH" (
    "BATCH_ID" integer PRIMARY KEY, "BATCH_NAME" varchar(50), "BATCH_DESCR" varchar(1000), "BATCH_ENABLED" integer, "BATCH_SCHEDULE_ID" integer, "MAX_RUN_MINS" integer,
    "BATCH_GROUP_NAME" varchar(100), "OWNER" varchar(100));
CREATE TABLE IF NOT EXISTS landing."ML_CONFIG__BATCH_STATUS" ("BATCH_STATUS_ID" integer PRIMARY KEY, "BATCH_STATUS_NAME" varchar(50), "BATCH_STATUS_DESCR" varchar(250));
CREATE TABLE IF NOT EXISTS landing."ML_CONFIG__BATCH_EXECUTION" (
    "BATCH_EXECUTION_ID" bigint PRIMARY KEY, "BATCH_ID" integer, "BATCH_START_EXECUTION_TIME" timestamp, "BATCH_END_EXECUTION_TIME" timestamp, "BATCH_STATUS_ID" integer, "COMMENT" varchar(4000));
CREATE TABLE IF NOT EXISTS landing."ML_CONFIG__PROGRAMME_FIELD_CONTROL" (
    "ID" bigint PRIMARY KEY, "PROGRAMME_ELIGIBILITY_ID" bigint, "PROGRAMME_CODE" integer, "FIELD_NAME" varchar(100), "FIELD_VALUE" varchar(1000), "LOAD_DATE" timestamp, "FIELD_CONTROL_BATCH_NO" bigint);
CREATE INDEX IF NOT EXISTS landing_fieldcontrol_load_idx ON landing."ML_CONFIG__PROGRAMME_FIELD_CONTROL" ("LOAD_DATE");

-- ================= enrolment events: PHP, Workflow, Excel, HealthID (the platform's 5-minute feed and live reads) =================
CREATE TABLE IF NOT EXISTS landing."ML_STAGE__LEADS_PHP_ENROLLMENTS" (
    "ROW_ID" bigint PRIMARY KEY, "ENTITY_NO" bigint, "PROGRAMME" varchar(200), "STATUS" varchar(100), "STATUS_FROM_DATE" timestamp, "STATUS_TO_DATE" timestamp, "LOAD_DATE" timestamp);
CREATE INDEX IF NOT EXISTS landing_php_load_idx ON landing."ML_STAGE__LEADS_PHP_ENROLLMENTS" ("LOAD_DATE");
CREATE INDEX IF NOT EXISTS landing_php_entity_idx ON landing."ML_STAGE__LEADS_PHP_ENROLLMENTS" ("ENTITY_NO");
CREATE TABLE IF NOT EXISTS landing."ML_STAGE__LEADS_WORKFLOW_ENROLLMENTS" (
    "ROW_KEY" varchar(64) PRIMARY KEY, "ENTITY_NO" varchar(1000), "Programme" varchar(500), "DATE_COMPLETED_SKEY" integer, "Programme status" varchar(200), "Enrollment status" varchar(200),
    "Attempt Outcome" varchar(200), "Call Attempt Outcome" varchar(200), "Interaction outcome" varchar(200));
CREATE INDEX IF NOT EXISTS landing_wf_date_idx ON landing."ML_STAGE__LEADS_WORKFLOW_ENROLLMENTS" ("DATE_COMPLETED_SKEY");
CREATE INDEX IF NOT EXISTS landing_wf_entity_idx ON landing."ML_STAGE__LEADS_WORKFLOW_ENROLLMENTS" ("ENTITY_NO");
CREATE TABLE IF NOT EXISTS landing."ML_TEMP__COACHING 2026" ("ROW_KEY" varchar(64) PRIMARY KEY, "Entity Number" bigint, "Programme" varchar(500), "Date" timestamp, "Programme Status" varchar(200));
CREATE TABLE IF NOT EXISTS landing."ML_TEMP__ENROLLMENTS 2026" ("ROW_KEY" varchar(64) PRIMARY KEY, "Entity Number" bigint, "Programme" varchar(500), "Date" timestamp, "Attempt Outcome" varchar(200));
-- HealthID's cohort + pathway + status, flattened by the connector's query (the platform reads it with ENROL_HEALTHID_FLAT=WH_HS_RECOMMENDER__HEALTHID_STATUS)
CREATE TABLE IF NOT EXISTS landing."WH_HS_RECOMMENDER__HEALTHID_STATUS" (
    "ROW_KEY" varchar(64) PRIMARY KEY, "ENTITY_NO" bigint, "PATHWAY_ID" bigint, "COHORT_NAME" varchar(500), "STATUS_TYPE" varchar(100), "BUS_EFF_FROM" timestamp, "SYS_EFF_FROM" timestamp, "REASON_DESC" varchar(1000));
CREATE INDEX IF NOT EXISTS landing_hid_entity_idx ON landing."WH_HS_RECOMMENDER__HEALTHID_STATUS" ("ENTITY_NO");
