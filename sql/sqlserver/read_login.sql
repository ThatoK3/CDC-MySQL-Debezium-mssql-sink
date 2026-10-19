-- The login the JDBC SOURCE connectors use: read only, and only what the pipeline reads (pipeline/spec.py). Run as a DBA.
CREATE LOGIN mlp_cdc_read WITH PASSWORD = 'change-me', CHECK_POLICY = ON;
USE [ML_Model];
CREATE USER mlp_cdc_read FOR LOGIN mlp_cdc_read;
GRANT SELECT ON ML_LEAD.VW_ENTITY_PROGRAMME_ELIGIBILITY_HISTORY TO mlp_cdc_read;
GRANT SELECT ON ML_LEAD.ENTITY_DETAILS TO mlp_cdc_read;      -- consider a column-level grant that leaves out ID_NUMBER and DATE_OF_BIRTH: GRANT SELECT (ENTITY_NO, TITLE, ...)
GRANT SELECT ON ML_LEAD.DISCOVERY_PERSONNEL TO mlp_cdc_read;
GRANT SELECT ON ML_CONFIG.PROGRAMME_NOTES TO mlp_cdc_read;
GRANT SELECT ON ML_CONFIG.PROGRAMME_ASSIGNMENT TO mlp_cdc_read;
GRANT SELECT ON ML_CONFIG.PROGRAMME_ROLE TO mlp_cdc_read;
-- Optional, recommended by the platform docs: ALTER DATABASE [ML_Model] SET ALLOW_SNAPSHOT_ISOLATION ON; then readers can SET TRANSACTION ISOLATION LEVEL SNAPSHOT.
