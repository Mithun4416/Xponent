-- Xponent — Primary OLTP indexes
-- Covering indexes let bounded verification / correctness-oracle reads run as
-- index-only scans without touching the heap. Edge reads for the API are served
-- from the summary store's minute buckets (decision D1), not the primary.

BEGIN;

CREATE UNIQUE INDEX IF NOT EXISTS idx_products_tenant_sku
    ON products (tenant_id, sku);

CREATE INDEX IF NOT EXISTS idx_products_tenant_id
    ON products (tenant_id, product_id) INCLUDE (product_name);

CREATE INDEX IF NOT EXISTS idx_customers_tenant
    ON customers (tenant_id, customer_id);

-- (tenant_id, created_at) covering index for correctness-oracle / digest reads.
CREATE INDEX IF NOT EXISTS idx_orders_tenant_created
    ON orders (tenant_id, created_at)
    INCLUDE (status, total_amount_cents, order_id);

CREATE INDEX IF NOT EXISTS idx_order_items_order
    ON order_items (order_id);

CREATE INDEX IF NOT EXISTS idx_order_items_tenant_created
    ON order_items (tenant_id, order_created_at)
    INCLUDE (product_id, quantity, line_amount_cents);

COMMIT;
