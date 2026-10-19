-- The login the Debezium PostgreSQL connector uses on the APP database. It needs REPLICATION and nothing it can write with.
-- The app (`manage.py cdc_setup`) owns the publications and the staged tables; the connector must not create or alter them
-- (publication.autocreate.mode=disabled). wal_level=logical is required on the server (restart).
CREATE ROLE debezium WITH LOGIN REPLICATION PASSWORD 'change-me';
GRANT CONNECT ON DATABASE mlp TO debezium;
GRANT USAGE ON SCHEMA public TO debezium;
GRANT SELECT ON cdc_out_note, cdc_out_assignment, cdc_out_field, cdc_out_status TO debezium;      -- heartbeats / metadata reads; no snapshot is taken (snapshot.mode=no_data)
