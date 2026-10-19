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
