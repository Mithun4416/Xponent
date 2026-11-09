-- Xponent — indexes for platform-wide (all-tenant) time-range reads.
-- The rollup primary keys lead with tenant_id, which suits per-tenant reads but
-- forces a full scan when the organization view filters only on the bucket.
-- Covering indexes let those reads be index-only. The API also ensures these at
-- startup (app/core/db.py) so existing volumes get them without a rebuild.

CREATE INDEX IF NOT EXISTS idx_agg_orders_daily_bucket
    ON agg_orders_daily (day_bucket) INCLUDE (tenant_id, order_count, gross_sales_cents, net_sales_cents);
CREATE INDEX IF NOT EXISTS idx_agg_orders_hourly_bucket
    ON agg_orders_hourly (hour_bucket) INCLUDE (tenant_id, order_count, gross_sales_cents, net_sales_cents);
CREATE INDEX IF NOT EXISTS idx_agg_orders_minutely_bucket
    ON agg_orders_minutely (minute_bucket) INCLUDE (tenant_id, order_count, gross_sales_cents, net_sales_cents);
