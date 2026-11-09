-- Xponent — least-privilege role for the query API's order writes.
-- The dashboard's Orders tab (create / edit / cancel / delete) and the demo
-- endpoints write to the source DB. replication_reader is SELECT-only, so the
-- API uses this role instead of a superuser: plain DML on public tables, no DDL.
-- Dev-only password; override via secrets in any non-local environment.

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_writer') THEN
        CREATE ROLE app_writer WITH LOGIN PASSWORD 'app_writer_pass';
    END IF;
END$$;

GRANT USAGE ON SCHEMA public TO app_writer;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO app_writer;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO app_writer;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO app_writer;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO app_writer;
