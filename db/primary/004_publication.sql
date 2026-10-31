-- Xponent — Logical replication publication consumed by the CDC worker.
-- Covers the tables whose changes drive the rollups: orders (counts/sums/status),
-- order_items (product Top-N), products (catalog names). Created after the tables exist.

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_publication WHERE pubname = 'agg_layer_pub') THEN
        CREATE PUBLICATION agg_layer_pub FOR TABLE orders, order_items, products;
    END IF;
END$$;
