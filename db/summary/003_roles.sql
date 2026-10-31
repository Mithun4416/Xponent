-- Xponent — Summary store roles (fixes gap C4)
--
-- Two connection identities with different privileges:
--   * agg_user (the init superuser, POSTGRES_USER) OWNS the tables and is used by
--     the CDC worker to write ALL tenants. A superuser bypasses RLS, so the
--     worker is never blocked by tenant policies.
--   * agg_reader is a NON-superuser, SELECT-only login used by the API. RLS +
--     FORCE RLS apply to it, so it can only ever read rows for the tenant bound
--     into the session via SET LOCAL app.current_tenant_id.
--
-- The API MUST connect as agg_reader (see .env.example SUMMARY_DB_URL for the API),
-- never as agg_user. Dev-only password; override via secrets outside local dev.

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'agg_reader') THEN
        CREATE ROLE agg_reader WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD 'agg_reader_pass';
    END IF;
END$$;

GRANT USAGE ON SCHEMA public TO agg_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO agg_reader;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO agg_reader;
