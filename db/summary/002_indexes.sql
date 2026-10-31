-- Xponent — Summary store indexes
-- Clustered PKs already lead with tenant_id (tenant co-location + range scans).
-- One extra covering index accelerates Top-N ranking; one partial index finds
-- corrupted digest buckets instantly for the verifier.

BEGIN;

CREATE INDEX IF NOT EXISTS idx_agg_prod_tenant_day_rev
    ON agg_products_daily (tenant_id, day_bucket, net_revenue_cents DESC)
    INCLUDE (product_id, product_name, qty_sold);

CREATE INDEX IF NOT EXISTS idx_digests_corrupted
    ON bucket_digests (is_corrupted) WHERE is_corrupted = TRUE;

COMMIT;
