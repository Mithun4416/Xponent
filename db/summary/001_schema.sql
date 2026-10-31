-- Xponent — Summary / rollup database schema
-- Pre-aggregated, bucket-bounded rollups keyed by tenant_id. All currency in
-- integer cents. gross_* includes all statuses; net_* excludes cancelled
-- (decision D2: demo default is gross; net carried alongside).
--
-- Grains for orders (hybrid range decomposition, decision D1):
--   daily    -> interior full days
--   hourly   -> boundary full hours
--   minutely -> sub-hour residual edges  (so arbitrary from/to stay exact
--               WITHOUT ever querying the primary DB)

BEGIN;

CREATE TABLE IF NOT EXISTS agg_orders_daily (
    tenant_id         INTEGER     NOT NULL,
    day_bucket        DATE        NOT NULL,
    status            VARCHAR(32) NOT NULL,
    order_count       BIGINT      NOT NULL DEFAULT 0,
    gross_sales_cents BIGINT      NOT NULL DEFAULT 0,
    net_sales_cents   BIGINT      NOT NULL DEFAULT 0,
    PRIMARY KEY (tenant_id, day_bucket, status)
);

CREATE TABLE IF NOT EXISTS agg_orders_hourly (
    tenant_id         INTEGER     NOT NULL,
    hour_bucket       TIMESTAMPTZ NOT NULL,
    status            VARCHAR(32) NOT NULL,
    order_count       BIGINT      NOT NULL DEFAULT 0,
    gross_sales_cents BIGINT      NOT NULL DEFAULT 0,
    net_sales_cents   BIGINT      NOT NULL DEFAULT 0,
    PRIMARY KEY (tenant_id, hour_bucket, status)
);

-- Minute grain: used only for the two sub-hour residual edges of a non-aligned
-- range. Keeps all edge evaluation inside the isolated summary store (D1 / fix C1).
CREATE TABLE IF NOT EXISTS agg_orders_minutely (
    tenant_id         INTEGER     NOT NULL,
    minute_bucket     TIMESTAMPTZ NOT NULL,
    status            VARCHAR(32) NOT NULL,
    order_count       BIGINT      NOT NULL DEFAULT 0,
    gross_sales_cents BIGINT      NOT NULL DEFAULT 0,
    net_sales_cents   BIGINT      NOT NULL DEFAULT 0,
    PRIMARY KEY (tenant_id, minute_bucket, status)
);

-- Per-product daily sums for exact query-time Top-N ranking (FR-3).
-- NOTE (product grain, documented trade-off): Top-N is exact for day-aligned
-- windows (the PRD "this week" case). Sub-hour-exact Top-N would require a
-- product hourly/minute grain; deferred (P1/P2) to bound cardinality. Tracked
-- in TECHNICAL_GAP_ANALYSIS.md.
CREATE TABLE IF NOT EXISTS agg_products_daily (
    tenant_id           INTEGER      NOT NULL,
    day_bucket          DATE         NOT NULL,
    product_id          BIGINT       NOT NULL,
    product_name        VARCHAR(255) NOT NULL DEFAULT '',
    order_count         BIGINT       NOT NULL DEFAULT 0,
    qty_sold            BIGINT       NOT NULL DEFAULT 0,
    gross_revenue_cents BIGINT       NOT NULL DEFAULT 0,
    net_revenue_cents   BIGINT       NOT NULL DEFAULT 0,
    PRIMARY KEY (tenant_id, day_bucket, product_id)
);

-- Optional local before-image cache keyed by order_id (used only if DB
-- pre-images are unavailable; REPLICA IDENTITY FULL is the primary path).
CREATE TABLE IF NOT EXISTS shadow_orders (
    order_id           BIGINT PRIMARY KEY,
    tenant_id          INTEGER     NOT NULL,
    created_at         TIMESTAMPTZ NOT NULL,
    status             VARCHAR(32) NOT NULL,
    total_amount_cents BIGINT      NOT NULL,
    last_lsn           BIGINT      NOT NULL
);

-- Per-(tenant, day) verification digest (differentiator B-2).
-- hash_sum_mod64 is an order-independent modular sum of row hashes so updates
-- and retractions maintain it in O(1): +hash(new) - hash(old) mod 2^64.
CREATE TABLE IF NOT EXISTS bucket_digests (
    tenant_id        INTEGER       NOT NULL,
    day_bucket       DATE          NOT NULL,
    row_count        BIGINT        NOT NULL DEFAULT 0,
    sum_amount_cents BIGINT        NOT NULL DEFAULT 0,
    hash_sum_mod64   NUMERIC(20,0) NOT NULL DEFAULT 0,
    updated_lsn      BIGINT        NOT NULL DEFAULT 0,
    verified_at      TIMESTAMPTZ   NULL,
    is_corrupted     BOOLEAN       NOT NULL DEFAULT FALSE,
    PRIMARY KEY (tenant_id, day_bucket)
);

-- Single-row-per-slot replication checkpoint + freshness watermark (idempotency
-- + Bonus B-1). last_applied_lsn is advanced atomically with the rollup deltas.
CREATE TABLE IF NOT EXISTS cdc_checkpoint (
    slot_name            VARCHAR(64) PRIMARY KEY,
    last_applied_lsn     BIGINT      NOT NULL DEFAULT 0,
    last_applied_lsn_str VARCHAR(32) NOT NULL DEFAULT '0/0',
    as_of_timestamp      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Live activity feed: one row per applied CDC event (for the dashboard's event stream).
CREATE TABLE IF NOT EXISTS cdc_activity (
    id         BIGSERIAL PRIMARY KEY,
    tenant_id  INTEGER     NOT NULL,
    op         VARCHAR(8)  NOT NULL,    -- I / U / D
    tbl        VARCHAR(32) NOT NULL,    -- orders / order_items
    order_id   BIGINT,
    lsn        BIGINT      NOT NULL,
    commit_ts  TIMESTAMPTZ,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_cdc_activity_at ON cdc_activity (applied_at DESC);

COMMIT;
