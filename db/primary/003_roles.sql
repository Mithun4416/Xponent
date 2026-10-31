-- Xponent — Primary OLTP least-privilege role for the CDC worker
-- The worker streams logical replication and reads bounded ground-truth for the
-- verifier. It needs REPLICATION + SELECT only; it never writes the primary.
-- Dev-only password; override via secrets in any non-local environment.

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'replication_reader') THEN
        CREATE ROLE replication_reader WITH LOGIN REPLICATION PASSWORD 'rep_pass';
    END IF;
END$$;

GRANT SELECT ON ALL TABLES IN SCHEMA public TO replication_reader;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO replication_reader;
