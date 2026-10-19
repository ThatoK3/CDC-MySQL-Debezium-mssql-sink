-- The SQL Server INBOX for changes made in the platform. The JDBC sinks upsert Debezium's change events into CDC_IN.* keyed by outbox_id (so a
-- replay is a no-op); the triggers below turn every NEW inbox row into the real write. Everything that touches ML_CONFIG / ML_LEAD is in these
-- triggers, owned and reviewable by the DBA — the connector's login can only write to CDC_IN.
-- Run as a DBA after reviewing. Set @tz to the time zone SQL Server's LOAD_DATE values are in (open question D18); staged times arrive as UTC text.
USE [ML_Model];
GO
IF SCHEMA_ID('CDC_IN') IS NULL EXEC('CREATE SCHEMA CDC_IN');
GO
CREATE TABLE CDC_IN.NOTE (
    outbox_id bigint NOT NULL PRIMARY KEY, eligibility_id bigint NOT NULL, programme_code int NULL, staged_at nvarchar(40) NULL,
    username nvarchar(100) NULL, notes nvarchar(max) NULL, load_date nvarchar(40) NULL, applied_at datetime2(0) NOT NULL DEFAULT SYSDATETIME());
CREATE TABLE CDC_IN.ASSIGNMENT (
    outbox_id bigint NOT NULL PRIMARY KEY, eligibility_id bigint NOT NULL, programme_code int NULL, staged_at nvarchar(40) NULL,
    assignee_entity_no bigint NOT NULL, assigned_by_entity_no bigint NULL, load_date nvarchar(40) NULL, assignment_batch_no bigint NOT NULL, applied_at datetime2(0) NOT NULL DEFAULT SYSDATETIME());
CREATE TABLE CDC_IN.FIELD (
    outbox_id bigint NOT NULL PRIMARY KEY, eligibility_id bigint NOT NULL, programme_code int NULL, staged_at nvarchar(40) NULL,
    field_name nvarchar(100) NOT NULL, field_value nvarchar(1000) NULL, load_date nvarchar(40) NULL, field_control_batch_no bigint NOT NULL, applied_at datetime2(0) NOT NULL DEFAULT SYSDATETIME());
CREATE TABLE CDC_IN.STATUS (
    outbox_id bigint NOT NULL PRIMARY KEY, eligibility_id bigint NOT NULL, programme_code int NULL, staged_at nvarchar(40) NULL,
    status nvarchar(50) NOT NULL, updated_by_entity_no bigint NULL, requested_at nvarchar(40) NULL, applied_at datetime2(0) NOT NULL DEFAULT SYSDATETIME());
GO
CREATE OR ALTER FUNCTION CDC_IN.LOCAL_TIME(@utc nvarchar(40)) RETURNS datetime2(0) AS
BEGIN
    RETURN CAST(TRY_CONVERT(datetimeoffset, @utc) AT TIME ZONE 'South Africa Standard Time' AS datetime2(0));
END;
GO
CREATE OR ALTER TRIGGER CDC_IN.TR_NOTE ON CDC_IN.NOTE AFTER INSERT AS
BEGIN
    SET NOCOUNT ON;
    INSERT INTO ML_CONFIG.PROGRAMME_NOTES (PROGRAMME_ELIGIBILITY_ID, PROGRAMME_CODE, USERNAME, NOTES, LOAD_DATE)
    SELECT eligibility_id, programme_code, username, notes, CDC_IN.LOCAL_TIME(load_date) FROM inserted ORDER BY outbox_id;
END;
GO
CREATE OR ALTER TRIGGER CDC_IN.TR_ASSIGNMENT ON CDC_IN.ASSIGNMENT AFTER INSERT AS
BEGIN
    SET NOCOUNT ON;     -- ASSIGNMENT_BATCH_NO >= 9,000,000,000 marks the row as written by the platform, so its own sync does not import it back
    INSERT INTO ML_CONFIG.PROGRAMME_ASSIGNMENT (PROGRAMME_ELIGIBILITY_ID, PROGRAMME_CODE, ASSIGNEE_ENTITY_NO, ASSIGNED_BY_ENTITY_NO, LOAD_DATE, ASSIGNMENT_BATCH_NO)
    SELECT eligibility_id, programme_code, assignee_entity_no, assigned_by_entity_no, CDC_IN.LOCAL_TIME(load_date), assignment_batch_no FROM inserted ORDER BY outbox_id;
END;
GO
CREATE OR ALTER TRIGGER CDC_IN.TR_FIELD ON CDC_IN.FIELD AFTER INSERT AS
BEGIN
    SET NOCOUNT ON;
    INSERT INTO ML_CONFIG.PROGRAMME_FIELD_CONTROL (PROGRAMME_ELIGIBILITY_ID, PROGRAMME_CODE, FIELD_NAME, FIELD_VALUE, LOAD_DATE, FIELD_CONTROL_BATCH_NO)
    SELECT eligibility_id, programme_code, field_name, field_value, CDC_IN.LOCAL_TIME(load_date), field_control_batch_no FROM inserted ORDER BY outbox_id;
END;
GO
CREATE OR ALTER TRIGGER CDC_IN.TR_STATUS ON CDC_IN.STATUS AFTER INSERT AS
BEGIN
    SET NOCOUNT ON;
    -- The procedure takes int parameters and a varchar(50) status; the platform already refuses anything that would be truncated.
    DECLARE @id int, @by int, @st varchar(50);
    DECLARE c CURSOR LOCAL FAST_FORWARD FOR SELECT CAST(eligibility_id AS int), CAST(updated_by_entity_no AS int), status FROM inserted ORDER BY outbox_id;
    OPEN c; FETCH NEXT FROM c INTO @id, @by, @st;
    WHILE @@FETCH_STATUS = 0
    BEGIN
        EXEC ML_LEAD.PRC_UPDATE_LEAD_STATUS @STATUS = @st, @UPDATED_BY_ENTITY_NO = @by, @ID = @id;
        FETCH NEXT FROM c INTO @id, @by, @st;
    END
    CLOSE c; DEALLOCATE c;
END;
GO
-- The connector's login: it can write the inbox and nothing else (the triggers run with the inbox owner's rights: give dbo ownership of the schema and enable cross-schema chaining, or EXECUTE AS OWNER).
CREATE LOGIN mlp_cdc_inbox WITH PASSWORD = 'change-me', CHECK_POLICY = ON;
CREATE USER mlp_cdc_inbox FOR LOGIN mlp_cdc_inbox;
GRANT SELECT, INSERT, UPDATE ON CDC_IN.NOTE TO mlp_cdc_inbox;
GRANT SELECT, INSERT, UPDATE ON CDC_IN.ASSIGNMENT TO mlp_cdc_inbox;
GRANT SELECT, INSERT, UPDATE ON CDC_IN.FIELD TO mlp_cdc_inbox;
GRANT SELECT, INSERT, UPDATE ON CDC_IN.STATUS TO mlp_cdc_inbox;
GO
-- Housekeeping: the inbox rows only exist for idempotency. Keep a month, then delete (a replay older than that cannot happen: Kafka keeps 7 days).
-- DELETE FROM CDC_IN.NOTE WHERE applied_at < DATEADD(day, -30, SYSDATETIME());   (same for the other three) — schedule with SQL Agent.
