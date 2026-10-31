-- Xponent — Row-Level Security for tenant isolation (fixes gap C4)
--
-- Defense-in-depth second barrier behind the application-layer tenant predicate.
-- The API wraps every read in a transaction that first runs:
--     SET LOCAL app.current_tenant_id = '<tenant_id>';
-- so the policy below resolves to that tenant. If current_setting is unset
-- (e.g. a developer forgot the SET LOCAL), the policy returns NO rows rather
-- than leaking — fail closed, not open.
--
-- FORCE is applied so the policy binds even for a table owner connection; the
-- CDC worker writes as a superuser (agg_user), which bypasses RLS entirely and
-- is therefore unaffected. The API's agg_reader role is a non-superuser and is
-- fully constrained.

BEGIN;

ALTER TABLE agg_orders_daily     ENABLE ROW LEVEL SECURITY;
ALTER TABLE agg_orders_hourly    ENABLE ROW LEVEL SECURITY;
ALTER TABLE agg_orders_minutely  ENABLE ROW LEVEL SECURITY;
ALTER TABLE agg_products_daily   ENABLE ROW LEVEL SECURITY;

ALTER TABLE agg_orders_daily     FORCE ROW LEVEL SECURITY;
ALTER TABLE agg_orders_hourly    FORCE ROW LEVEL SECURITY;
ALTER TABLE agg_orders_minutely  FORCE ROW LEVEL SECURITY;
ALTER TABLE agg_products_daily   FORCE ROW LEVEL SECURITY;

CREATE POLICY tenant_isolation_daily ON agg_orders_daily
    USING (tenant_id = current_setting('app.current_tenant_id', true)::INTEGER);
CREATE POLICY tenant_isolation_hourly ON agg_orders_hourly
    USING (tenant_id = current_setting('app.current_tenant_id', true)::INTEGER);
CREATE POLICY tenant_isolation_minutely ON agg_orders_minutely
    USING (tenant_id = current_setting('app.current_tenant_id', true)::INTEGER);
CREATE POLICY tenant_isolation_products ON agg_products_daily
    USING (tenant_id = current_setting('app.current_tenant_id', true)::INTEGER);

COMMIT;
