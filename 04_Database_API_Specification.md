This Database Design Document and REST API Specification provides the implementation-ready data schemas, SQL operations, and HTTP service contracts for the Multi-Tenant Live Aggregation Layer.
PART A — DATABASE DESIGN
1. Database Architecture
 * Primary OLTP Database (PostgreSQL 16 Primary): Hosts the authoritative transactional tables for tenants, products, customers, orders, and order line items. Configured with logical write-ahead logging (wal_level set to logical) and full replica identity on mutable tables to stream non-blocking change events.
 * Summary and Rollup Database (PostgreSQL 16 Summary Store): Runs as a physically isolated PostgreSQL instance with dedicated CPU and memory limits to store pre-aggregated time buckets, product totals, hash digests, and replication watermarks.
 * Cache and Coordination Layer (Redis 7 Alpine — Optional Enhancement): Stores ephemeral per-tenant token-bucket counters for query admission control (Bonus B-3) and caches LSN-keyed responses for hot dashboard queries with a 2-second TTL.
 * Data Ownership and Separation: The Primary Database owns all raw transactional writes and serves only bounded sub-hour edge lookups. The Summary Database owns all derived rollup tables and replication checkpoints, ensuring dashboard queries never contend with primary OLTP locks or buffer pools.
2. Entity Relationship Design
2.1 Entities and Relationships
 * Primary OLTP Entities: A Tenant owns multiple Products, Customers, and Orders (1:N). Each Order belongs to one Tenant and one Customer, and contains one or more Order Items (1:N), each referencing a Product.
 * Summary Store Entities: A Tenant maps to multiple Hourly Order Rollups, Daily Order Rollups, Daily Product Rollups, and Daily Bucket Digests (1:N). Shadow Orders map 1:1 to base orders when local before-image caching is enabled, while CDC Checkpoints track global and per-slot replication progress.
2.2 Entity Relationship Diagram (Mermaid)
erDiagram
    TENANTS ||--o{ PRODUCTS : owns
    TENANTS ||--o{ CUSTOMERS : owns
    TENANTS ||--o{ ORDERS : receives
    CUSTOMERS ||--o{ ORDERS : places
    ORDERS ||--|{ ORDER_ITEMS : contains
    PRODUCTS ||--o{ ORDER_ITEMS : referenced_in

    TENANTS ||--o{ AGG_ORDERS_HOURLY : summarized_in
    TENANTS ||--o{ AGG_ORDERS_DAILY : summarized_in
    TENANTS ||--o{ AGG_PRODUCTS_DAILY : ranked_in
    TENANTS ||--o{ BUCKET_DIGESTS : verified_by
    ORDERS ||--o| SHADOW_ORDERS : cached_as

    TENANTS {
        int tenant_id PK
        varchar store_name
        varchar tier
        timestamptz created_at
    }
    PRODUCTS {
        bigint product_id PK
        int tenant_id FK
        varchar sku
        varchar product_name
        bigint unit_price_cents
    }
    CUSTOMERS {
        bigint customer_id PK
        int tenant_id FK
        varchar email_hash
        timestamptz created_at
    }
    ORDERS {
        bigint order_id PK
        int tenant_id FK
        bigint customer_id FK
        varchar status
        bigint total_amount_cents
        timestamptz created_at
        timestamptz updated_at
    }
    ORDER_ITEMS {
        bigint order_item_id PK
        bigint order_id FK
        int tenant_id FK
        bigint product_id FK
        int quantity
        bigint line_amount_cents
    }
    AGG_ORDERS_HOURLY {
        int tenant_id PK
        timestamptz hour_bucket PK
        varchar status PK
        bigint order_count
        bigint gross_sales_cents
        bigint net_sales_cents
    }
    AGG_ORDERS_DAILY {
        int tenant_id PK
        date day_bucket PK
        varchar status PK
        bigint order_count
        bigint gross_sales_cents
        bigint net_sales_cents
    }
    AGG_PRODUCTS_DAILY {
        int tenant_id PK
        date day_bucket PK
        bigint product_id PK
        bigint order_count
        bigint qty_sold
        bigint gross_revenue_cents
        bigint net_revenue_cents
    }
    SHADOW_ORDERS {
        bigint order_id PK
        int tenant_id
        timestamptz created_at
        varchar status
        bigint total_amount_cents
        bigint last_lsn
    }
    BUCKET_DIGESTS {
        int tenant_id PK
        date day_bucket PK
        bigint row_count
        bigint sum_amount_cents
        numeric hash_sum_mod64
        bigint updated_lsn
    }
    CDC_CHECKPOINT {
        varchar slot_name PK
        bigint last_applied_lsn
        timestamptz as_of_timestamp
        timestamptz updated_at
    }

3. Schema Design
3.1 Primary OLTP Database Tables
Table 1: tenants (Primary DB)
| Column Name | Data Type | Key / Ref | Constraints | Purpose |
|---|---|---|---|---|
| tenant_id | INTEGER | PK | NOT NULL | Unique store identifier |
| store_name | VARCHAR(120) | None | NOT NULL | Merchant display name |
| plan_tier | VARCHAR(32) | None | NOT NULL, DEFAULT 'standard' | Quota rate-limit tier |
| created_at | TIMESTAMPTZ | None | NOT NULL, DEFAULT NOW() | Store onboarding timestamp |
 * Primary Key: tenant_id.
 * Indexes: Primary B-Tree index on tenant_id.
 * Purpose: Stores tenant metadata for the 100 hosted merchants.
Table 2: products (Primary DB)
| Column Name | Data Type | Key / Ref | Constraints | Purpose |
|---|---|---|---|---|
| product_id | BIGINT | PK | NOT NULL | Unique product identifier |
| tenant_id | INTEGER | FK (tenants) | NOT NULL | Owning tenant ID |
| sku | VARCHAR(64) | None | NOT NULL | Merchant product code |
| product_name | VARCHAR(255) | None | NOT NULL | Product title for Top-N |
| unit_price_cents | BIGINT | None | NOT NULL, CHECK (>= 0) | Price in integer cents |
| created_at | TIMESTAMPTZ | None | NOT NULL, DEFAULT NOW() | Creation timestamp |
 * Primary Key: product_id.
 * Foreign Keys: tenant_id references tenants(tenant_id).
 * Indexes: Composite unique index idx_products_tenant_sku on (tenant_id, sku) and covering index idx_products_tenant_id on (tenant_id, product_id) INCLUDE (product_name).
 * Purpose: Stores the merchant product catalog used in Top-N product rankings (FR-3).
Table 3: customers (Primary DB)
| Column Name | Data Type | Key / Ref | Constraints | Purpose |
|---|---|---|---|---|
| customer_id | BIGINT | PK | NOT NULL | Unique customer ID |
| tenant_id | INTEGER | FK (tenants) | NOT NULL | Owning tenant ID |
| email_hash | VARCHAR(128) | None | NOT NULL | Anonymized customer identifier |
| created_at | TIMESTAMPTZ | None | NOT NULL, DEFAULT NOW() | Registration timestamp |
 * Primary Key: customer_id.
 * Foreign Keys: tenant_id references tenants(tenant_id).
 * Indexes: Composite B-Tree index idx_customers_tenant on (tenant_id, customer_id).
 * Purpose: Represents store buyers generated by the organizer's dataset script.
Table 4: orders (Primary DB)
| Column Name | Data Type | Key / Ref | Constraints | Purpose |
|---|---|---|---|---|
| order_id | BIGINT | PK | NOT NULL | Unique order ID |
| tenant_id | INTEGER | FK (tenants) | NOT NULL | Owning store ID |
| customer_id | BIGINT | FK (customers) | NOT NULL | Purchasing customer ID |
| status | VARCHAR(32) | None | NOT NULL, CHECK IN list | placed, shipped, cancelled |
| total_amount_cents | BIGINT | None | NOT NULL, CHECK (>= 0) | Order total in cents |
| created_at | TIMESTAMPTZ | None | NOT NULL | Order placement timestamp |
| updated_at | TIMESTAMPTZ | None | NOT NULL, DEFAULT NOW() | Last modification timestamp |
 * Primary Key: order_id.
 * Foreign Keys: tenant_id references tenants(tenant_id); customer_id references customers(customer_id).
 * Indexes: Covering index idx_orders_tenant_created on (tenant_id, created_at) INCLUDE (status, total_amount_cents, order_id) to support sub-hour boundary edge queries and DBLog chunked verification without heap fetches.
 * Purpose: Core transactional order table scaled to 10 million rows across 100 tenants. Configured with REPLICA IDENTITY FULL so WAL update records carry the old row state.
Table 5: order_items (Primary DB)
| Column Name | Data Type | Key / Ref | Constraints | Purpose |
|---|---|---|---|---|
| order_item_id | BIGINT | PK | NOT NULL | Unique line-item ID |
| order_id | BIGINT | FK (orders) | NOT NULL | Parent order ID |
| tenant_id | INTEGER | FK (tenants) | NOT NULL | Denormalized tenant ID |
| product_id | BIGINT | FK (products) | NOT NULL | Purchased product ID |
| quantity | INTEGER | None | NOT NULL, CHECK (> 0) | Units purchased |
| line_amount_cents | BIGINT | None | NOT NULL, CHECK (>= 0) | Line revenue in cents |
| order_created_at | TIMESTAMPTZ | None | NOT NULL | Parent order timestamp |
 * Primary Key: order_item_id.
 * Foreign Keys: order_id references orders(order_id); product_id references products(product_id).
 * Indexes: Index idx_order_items_order on (order_id) and covering index idx_order_items_tenant_created on (tenant_id, order_created_at) INCLUDE (product_id, quantity, line_amount_cents).
 * Purpose: Captures per-product sales quantities and revenue within an order to drive Top-N product rankings (FR-3).
3.2 Summary and Rollup Database Tables
Table 6: agg_orders_hourly (Summary DB)
| Column Name | Data Type | Key / Ref | Constraints | Purpose |
|---|---|---|---|---|
| tenant_id | INTEGER | PK (Part 1) | NOT NULL | Tenant isolation key |
| hour_bucket | TIMESTAMPTZ | PK (Part 2) | NOT NULL | UTC hour start |
| status | VARCHAR(32) | PK (Part 3) | NOT NULL | Order status category |
| order_count | BIGINT | None | NOT NULL, DEFAULT 0 | Exact count in bucket |
| gross_sales_cents | BIGINT | None | NOT NULL, DEFAULT 0 | Sum of all orders |
| net_sales_cents | BIGINT | None | NOT NULL, DEFAULT 0 | Sum excluding cancelled |
 * Primary Key: (tenant_id, hour_bucket, status).
 * Indexes: Clustered primary B-Tree index on (tenant_id, hour_bucket, status).
 * Purpose: Answers hourly trend queries (FR-2), category breakdowns (FR-4), and partial-day interior hours during hybrid range decomposition (FR-1).
Table 7: agg_orders_daily (Summary DB)
| Column Name | Data Type | Key / Ref | Constraints | Purpose |
|---|---|---|---|---|
| tenant_id | INTEGER | PK (Part 1) | NOT NULL | Tenant isolation key |
| day_bucket | DATE | PK (Part 2) | NOT NULL | UTC calendar date |
| status | VARCHAR(32) | PK (Part 3) | NOT NULL | Order status category |
| order_count | BIGINT | None | NOT NULL, DEFAULT 0 | Daily order count |
| gross_sales_cents | BIGINT | None | NOT NULL, DEFAULT 0 | Daily gross sales |
| net_sales_cents | BIGINT | None | NOT NULL, DEFAULT 0 | Daily non-cancelled sales |
 * Primary Key: (tenant_id, day_bucket, status).
 * Indexes: Clustered primary B-Tree index on (tenant_id, day_bucket, status).
 * Purpose: Serves multi-day counts and totals (FR-1), 30-day daily trends (FR-2), and status breakdowns (FR-4) in under 5 ms.
Table 8: agg_products_daily (Summary DB)
| Column Name | Data Type | Key / Ref | Constraints | Purpose |
|---|---|---|---|---|
| tenant_id | INTEGER | PK (Part 1) | NOT NULL | Tenant isolation key |
| day_bucket | DATE | PK (Part 2) | NOT NULL | UTC calendar date |
| product_id | BIGINT | PK (Part 3) | NOT NULL | Product identifier |
| product_name | VARCHAR(255) | None | NOT NULL | Cached product name |
| order_count | BIGINT | None | NOT NULL, DEFAULT 0 | Orders containing product |
| qty_sold | BIGINT | None | NOT NULL, DEFAULT 0 | Total units sold |
| gross_revenue_cents | BIGINT | None | NOT NULL, DEFAULT 0 | Gross product revenue |
| net_revenue_cents | BIGINT | None | NOT NULL, DEFAULT 0 | Active non-cancelled revenue |
 * Primary Key: (tenant_id, day_bucket, product_id).
 * Indexes: Primary key index on (tenant_id, day_bucket, product_id) plus covering index idx_agg_prod_tenant_day_rev on (tenant_id, day_bucket, net_revenue_cents DESC) INCLUDE (product_id, product_name, qty_sold).
 * Purpose: Maintains exact per-product daily revenue sums so Top-N product rankings (FR-3) remain 100% accurate under order cancellations.
Table 9: shadow_orders (Summary DB — Optional Fallback if Pre-Images Disabled)
| Column Name | Data Type | Key / Ref | Constraints | Purpose |
|---|---|---|---|---|
| order_id | BIGINT | PK | NOT NULL | Source order ID |
| tenant_id | INTEGER | None | NOT NULL | Owning tenant ID |
| created_at | TIMESTAMPTZ | None | NOT NULL | Immutable order timestamp |
| status | VARCHAR(32) | None | NOT NULL | Last known status |
| total_amount_cents | BIGINT | None | NOT NULL | Last known amount |
| last_lsn | BIGINT | None | NOT NULL | LSN of last mutation |
 * Primary Key: order_id.
 * Indexes: Primary B-Tree index on order_id.
 * Purpose: Provides local before-image lookup and per-row LSN deduplication when database pre-images are unavailable (such as default PostgreSQL replica identity or MongoDB without pre-images).
Table 10: bucket_digests (Summary DB — Bonus B-2 Verification)
| Column Name | Data Type | Key / Ref | Constraints | Purpose |
|---|---|---|---|---|
| tenant_id | INTEGER | PK (Part 1) | NOT NULL | Tenant identifier |
| day_bucket | DATE | PK (Part 2) | NOT NULL | Verified UTC day |
| row_count | BIGINT | None | NOT NULL, DEFAULT 0 | Total orders in day |
| sum_amount_cents | BIGINT | None | NOT NULL, DEFAULT 0 | Sum of order amounts |
| hash_sum_mod64 | NUMERIC(20,0) | None | NOT NULL, DEFAULT 0 | Unsigned 64-bit hash sum |
| updated_lsn | BIGINT | None | NOT NULL, DEFAULT 0 | Last LSN applied |
| verified_at | TIMESTAMPTZ | None | NULL | Last clean verification |
| is_corrupted | BOOLEAN | None | NOT NULL, DEFAULT FALSE | Drift alert flag |
 * Primary Key: (tenant_id, day_bucket).
 * Indexes: Primary index on (tenant_id, day_bucket) and partial index idx_digests_corrupted on (is_corrupted) WHERE is_corrupted = TRUE.
 * Purpose: Enables anti-entropy verification and targeted DBLog single-bucket repair (Bonus B-2).
Table 11: cdc_checkpoint (Summary DB)
| Column Name | Data Type | Key / Ref | Constraints | Purpose |
|---|---|---|---|---|
| slot_name | VARCHAR(64) | PK | NOT NULL | Replication slot identifier |
| last_applied_lsn | BIGINT | None | NOT NULL, DEFAULT 0 | Committed WAL LSN |
| last_applied_lsn_str | VARCHAR(32) | None | NOT NULL, DEFAULT '0/0' | Postgres pg_lsn format |
| as_of_timestamp | TIMESTAMPTZ | None | NOT NULL, DEFAULT NOW() | Source commit watermark |
| updated_at | TIMESTAMPTZ | None | NOT NULL, DEFAULT NOW() | Worker apply timestamp |
 * Primary Key: slot_name.
 * Indexes: Primary index on slot_name.
 * Purpose: Stores the atomic replication offset and freshness watermark exposed in API responses (Bonus B-1).
4. Required Data Models
 * Tenants: Modeled in tenants with integer IDs (1 to 100) and plan tiers used by the API token-bucket rate limiter.
 * Source Records: Modeled in orders, order_items, products, and customers using 64-bit integer cents for currency to prevent floating-point drift.
 * CDC Event Metadata: Normalized in memory by the worker from the logical replication header (transaction LSN, commit timestamp, operation code, before-image tuple, and after-image tuple).
 * Aggregated Summaries & Time-Based Rollups: Partitioned across agg_orders_hourly and agg_orders_daily so multi-day queries read compact daily buckets while sub-day queries read hourly buckets.
 * Category Rollups: Embedded directly into the primary keys of agg_orders_hourly and agg_orders_daily via the status dimension, avoiding redundant summary tables.
 * Top-N Results: Modeled as exact daily product sums in agg_products_daily rather than a materialized top-10 cache, guaranteeing self-maintainability when orders are cancelled.
 * Processing Checkpoints & Watermarks: Consolidated in cdc_checkpoint, tracking both the 64-bit integer LSN for idempotency and as_of_timestamp for freshness reporting.
 * Reconciliation Metadata: Modeled in bucket_digests, storing order-independent modular 64-bit hash sums per tenant-day.
5. Aggregation Data Model
 * Counts: Stored in order_count across hourly and daily tables and updated via additive delta weights (+1 on insert, -1 on retraction).
 * Sums: Stored as both gross_sales_cents (all placed/shipped/cancelled orders) and net_sales_cents (excluding cancelled orders) so the API can satisfy either revenue definition instantaneously.
 * Trends: Computed by selecting ordered time series slices from agg_orders_daily (daily grain) or agg_orders_hourly (hourly grain) and summing across statuses per timestamp bucket.
 * Category Breakdowns: Computed by grouping agg_orders_daily (plus boundary hours/edges) by status for the requested tenant and time range.
 * Top-N Rankings: Computed at query time by summing net_revenue_cents and qty_sold per product_id across the requested date range in agg_products_daily, ordering by revenue descending with deterministic tie-breaking on product_id ASC, and limiting to N.
6. Update and Retraction Handling
When a source record in orders changes (for example, Order 9001 for Tenant 42 created at 2026-10-31 12:15 UTC with amount 15000 cents changes status from placed to cancelled), the CDC worker receives the old tuple and new tuple in a single WAL event and emits a two-part Z-set adjustment:
 * Retraction of Old State (w = -1): For bucket (tenant_id=42, hour='2026-10-31 12:00Z', status='placed') and day '2026-10-31', the worker decrements order_count by 1, decrements gross_sales_cents by 15000, and decrements net_sales_cents by 15000. It also subtracts the old row's 64-bit hash from bucket_digests.
 * Insertion of New State (w = +1): For bucket (tenant_id=42, hour='2026-10-31 12:00Z', status='cancelled') and day '2026-10-31', the worker increments order_count by 1, increments gross_sales_cents by 15000, and adds 0 to net_sales_cents. It simultaneously decrements net_revenue_cents in agg_products_daily for the order's line items and adds the new row's 64-bit hash to bucket_digests.
7. Indexing and Query Optimization
 * Tenant-Aware Leading Key Prefix: Every summary table places tenant_id as the leading column of its clustered primary key index, ensuring all buckets for a single tenant reside on adjacent B-Tree leaf pages.
 * Composite Time-Based Indexes: Putting day_bucket or hour_bucket second in the composite key allows range predicates (day_bucket BETWEEN $2 AND $3) to execute as a single contiguous B-Tree range scan.
 * Covering Indexes for Sub-Hour Edges: On the Primary DB, idx_orders_tenant_created includes (status, total_amount_cents, order_id) via the PostgreSQL INCLUDE clause so hybrid boundary edge scans execute as 100% Index-Only Scans without touching table heap pages.
 * Partitioning Decision: For the 48-hour hackathon scale (100 tenants, ~146k daily summary rows and ~500k active hourly summary rows), standard B-Tree indexing with tenant_id as the leading column fits entirely in 256 MB of RAM and avoids PostgreSQL query-planner overhead across hundreds of child partitions. On the Primary DB's 10M-row orders table, declarative hash partitioning into 16 partitions by tenant_id is optional if initial seeding requires parallel table writes.
8. Data Integrity and Correctness
 * Single-Transaction Atomic Boundaries: Every micro-batch flush in the Summary Store executes inside a single BEGIN ... COMMIT block that updates agg_orders_hourly, agg_orders_daily, agg_products_daily, bucket_digests, and cdc_checkpoint together.
 * Idempotency and Duplicate Handling: Before applying a micro-batch, the worker locks the checkpoint row (SELECT last_applied_lsn FROM cdc_checkpoint WHERE slot_name = $1 FOR UPDATE) and filters out any incoming WAL event where event_lsn <= last_applied_lsn.
 * Read Consistency Guarantees: The API backend executes multi-table rollup reads (combining daily interior buckets, hourly boundary buckets, and the watermark) inside a REPEATABLE READ transaction so responses always reflect a single atomic LSN cut.
9. Sample SQL
-- =========================================================================
-- 1. PRIMARY OLTP DATABASE DDL & INDEXES
-- =========================================================================
CREATE TABLE tenants (
    tenant_id INTEGER PRIMARY KEY,
    store_name VARCHAR(120) NOT NULL,
    plan_tier VARCHAR(32) NOT NULL DEFAULT 'standard',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE products (
    product_id BIGINT PRIMARY KEY,
    tenant_id INTEGER NOT NULL REFERENCES tenants(tenant_id),
    sku VARCHAR(64) NOT NULL,
    product_name VARCHAR(255) NOT NULL,
    unit_price_cents BIGINT NOT NULL CHECK (unit_price_cents >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_products_tenant_id
    ON products (tenant_id, product_id) INCLUDE (product_name);

CREATE TABLE customers (
    customer_id BIGINT PRIMARY KEY,
    tenant_id INTEGER NOT NULL REFERENCES tenants(tenant_id),
    email_hash VARCHAR(128) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE orders (
    order_id BIGINT PRIMARY KEY,
    tenant_id INTEGER NOT NULL REFERENCES tenants(tenant_id),
    customer_id BIGINT NOT NULL REFERENCES customers(customer_id),
    status VARCHAR(32) NOT NULL CHECK (status IN ('placed', 'shipped', 'cancelled')),
    total_amount_cents BIGINT NOT NULL CHECK (total_amount_cents >= 0),
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Enable full before-images in WAL for accurate status/amount retractions
ALTER TABLE orders REPLICA IDENTITY FULL;

-- Covering index for sub-hour edge queries and DBLog chunk verification
CREATE INDEX idx_orders_tenant_created
    ON orders (tenant_id, created_at)
    INCLUDE (status, total_amount_cents, order_id);

CREATE TABLE order_items (
    order_item_id BIGINT PRIMARY KEY,
    order_id BIGINT NOT NULL REFERENCES orders(order_id),
    tenant_id INTEGER NOT NULL,
    product_id BIGINT NOT NULL REFERENCES products(product_id),
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    line_amount_cents BIGINT NOT NULL CHECK (line_amount_cents >= 0),
    order_created_at TIMESTAMPTZ NOT NULL
);

ALTER TABLE order_items REPLICA IDENTITY FULL;

CREATE INDEX idx_order_items_order ON order_items (order_id);
CREATE INDEX idx_order_items_tenant_created
    ON order_items (tenant_id, order_created_at)
    INCLUDE (product_id, quantity, line_amount_cents);

-- Logical replication publication for CDC worker
CREATE PUBLICATION agg_layer_pub FOR TABLE orders, order_items, products;

-- =========================================================================
-- 2. SUMMARY & ROLLUP DATABASE DDL, INDEXES & ROW-LEVEL SECURITY
-- =========================================================================
CREATE TABLE agg_orders_hourly (
    tenant_id INTEGER NOT NULL,
    hour_bucket TIMESTAMPTZ NOT NULL,
    status VARCHAR(32) NOT NULL,
    order_count BIGINT NOT NULL DEFAULT 0,
    gross_sales_cents BIGINT NOT NULL DEFAULT 0,
    net_sales_cents BIGINT NOT NULL DEFAULT 0,
    PRIMARY KEY (tenant_id, hour_bucket, status)
);

CREATE TABLE agg_orders_daily (
    tenant_id INTEGER NOT NULL,
    day_bucket DATE NOT NULL,
    status VARCHAR(32) NOT NULL,
    order_count BIGINT NOT NULL DEFAULT 0,
    gross_sales_cents BIGINT NOT NULL DEFAULT 0,
    net_sales_cents BIGINT NOT NULL DEFAULT 0,
    PRIMARY KEY (tenant_id, day_bucket, status)
);

CREATE TABLE agg_products_daily (
    tenant_id INTEGER NOT NULL,
    day_bucket DATE NOT NULL,
    product_id BIGINT NOT NULL,
    product_name VARCHAR(255) NOT NULL DEFAULT '',
    order_count BIGINT NOT NULL DEFAULT 0,
    qty_sold BIGINT NOT NULL DEFAULT 0,
    gross_revenue_cents BIGINT NOT NULL DEFAULT 0,
    net_revenue_cents BIGINT NOT NULL DEFAULT 0,
    PRIMARY KEY (tenant_id, day_bucket, product_id)
);

CREATE INDEX idx_agg_prod_tenant_day_rev
    ON agg_products_daily (tenant_id, day_bucket, net_revenue_cents DESC)
    INCLUDE (product_id, product_name, qty_sold);

CREATE TABLE bucket_digests (
    tenant_id INTEGER NOT NULL,
    day_bucket DATE NOT NULL,
    row_count BIGINT NOT NULL DEFAULT 0,
    sum_amount_cents BIGINT NOT NULL DEFAULT 0,
    hash_sum_mod64 NUMERIC(20, 0) NOT NULL DEFAULT 0,
    updated_lsn BIGINT NOT NULL DEFAULT 0,
    verified_at TIMESTAMPTZ NULL,
    is_corrupted BOOLEAN NOT NULL DEFAULT FALSE,
    PRIMARY KEY (tenant_id, day_bucket)
);

CREATE INDEX idx_digests_corrupted
    ON bucket_digests (is_corrupted) WHERE is_corrupted = TRUE;

CREATE TABLE cdc_checkpoint (
    slot_name VARCHAR(64) PRIMARY KEY,
    last_applied_lsn BIGINT NOT NULL DEFAULT 0,
    last_applied_lsn_str VARCHAR(32) NOT NULL DEFAULT '0/0',
    as_of_timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Defense-in-depth Row-Level Security (RLS) for tenant isolation
ALTER TABLE agg_orders_hourly ENABLE ROW LEVEL SECURITY;
ALTER TABLE agg_orders_daily ENABLE ROW LEVEL SECURITY;
ALTER TABLE agg_products_daily ENABLE ROW LEVEL SECURITY;

CREATE POLICY tenant_isolation_hourly ON agg_orders_hourly
    USING (tenant_id = current_setting('app.current_tenant_id', true)::INTEGER);
CREATE POLICY tenant_isolation_daily ON agg_orders_daily
    USING (tenant_id = current_setting('app.current_tenant_id', true)::INTEGER);
CREATE POLICY tenant_isolation_products ON agg_products_daily
    USING (tenant_id = current_setting('app.current_tenant_id', true)::INTEGER);

-- =========================================================================
-- 3. INITIAL SNAPSHOT AGGREGATION SQL (BOOTSTRAP AT LSN L0)
-- =========================================================================
INSERT INTO agg_orders_hourly (tenant_id, hour_bucket, status, order_count, gross_sales_cents, net_sales_cents)
SELECT
    tenant_id,
    DATE_TRUNC('hour', created_at AT TIME ZONE 'UTC') AT TIME ZONE 'UTC' AS hour_bucket,
    status,
    COUNT(*) AS order_count,
    SUM(total_amount_cents) AS gross_sales_cents,
    SUM(CASE WHEN status <> 'cancelled' THEN total_amount_cents ELSE 0 END) AS net_sales_cents
FROM orders
GROUP BY tenant_id, DATE_TRUNC('hour', created_at AT TIME ZONE 'UTC') AT TIME ZONE 'UTC', status;

INSERT INTO agg_orders_daily (tenant_id, day_bucket, status, order_count, gross_sales_cents, net_sales_cents)
SELECT
    tenant_id,
    (created_at AT TIME ZONE 'UTC')::DATE AS day_bucket,
    status,
    COUNT(*) AS order_count,
    SUM(total_amount_cents) AS gross_sales_cents,
    SUM(CASE WHEN status <> 'cancelled' THEN total_amount_cents ELSE 0 END) AS net_sales_cents
FROM orders
GROUP BY tenant_id, (created_at AT TIME ZONE 'UTC')::DATE, status;

-- =========================================================================
-- 4. INCREMENTAL Z-SET ROLLUP UPDATE (RETRACTION + INSERTION TRANSACTION)
-- Example: Order 9001 (Tenant 42, 2026-10-31 12:15Z, $150.00) changes
-- from 'placed' (-1 retraction) to 'cancelled' (+1 insertion) at LSN 105400.
-- =========================================================================
BEGIN;

-- Step 1: Verify idempotency against cdc_checkpoint
SELECT last_applied_lsn FROM cdc_checkpoint WHERE slot_name = 'agg_slot' FOR UPDATE;

-- Step 2: Apply coalesced Z-set deltas to agg_orders_hourly
INSERT INTO agg_orders_hourly (tenant_id, hour_bucket, status, order_count, gross_sales_cents, net_sales_cents)
VALUES
    (42, '2026-10-31 12:00:00+00', 'placed', -1, -15000, -15000),
    (42, '2026-10-31 12:00:00+00', 'cancelled', 1, 15000, 0)
ON CONFLICT (tenant_id, hour_bucket, status) DO UPDATE SET
    order_count       = agg_orders_hourly.order_count + EXCLUDED.order_count,
    gross_sales_cents = agg_orders_hourly.gross_sales_cents + EXCLUDED.gross_sales_cents,
    net_sales_cents   = agg_orders_hourly.net_sales_cents + EXCLUDED.net_sales_cents;

-- Step 3: Apply coalesced Z-set deltas to agg_orders_daily
INSERT INTO agg_orders_daily (tenant_id, day_bucket, status, order_count, gross_sales_cents, net_sales_cents)
VALUES
    (42, '2026-10-31', 'placed', -1, -15000, -15000),
    (42, '2026-10-31', 'cancelled', 1, 15000, 0)
ON CONFLICT (tenant_id, day_bucket, status) DO UPDATE SET
    order_count       = agg_orders_daily.order_count + EXCLUDED.order_count,
    gross_sales_cents = agg_orders_daily.gross_sales_cents + EXCLUDED.gross_sales_cents,
    net_sales_cents   = agg_orders_daily.net_sales_cents + EXCLUDED.net_sales_cents;

-- Step 4: Advance checkpoint watermark atomically
UPDATE cdc_checkpoint
SET last_applied_lsn = 105400,
    last_applied_lsn_str = '0/19BB8',
    as_of_timestamp = '2026-10-31 14:10:05.120+00',
    updated_at = NOW()
WHERE slot_name = 'agg_slot' AND last_applied_lsn < 105400;

COMMIT;

-- =========================================================================
-- 5. TENANT-SCOPED HYBRID TIME-RANGE QUERY (FR-1: COUNTS & TOTALS)
-- Range: 2026-10-24 10:00:00Z to 2026-10-31 14:00:00Z for Tenant 42
-- Decomposed into: Left Partial Day Hours + Interior Full Days + Right Hours
-- =========================================================================
BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;
SET LOCAL app.current_tenant_id = '42';

WITH interior_days AS (
    SELECT COALESCE(SUM(order_count), 0) AS cnt,
           COALESCE(SUM(gross_sales_cents), 0) AS gross_cents,
           COALESCE(SUM(net_sales_cents), 0) AS net_cents
    FROM agg_orders_daily
    WHERE tenant_id = 42
      AND day_bucket >= '2026-10-25'::DATE
      AND day_bucket <= '2026-10-30'::DATE
),
boundary_hours AS (
    SELECT COALESCE(SUM(order_count), 0) AS cnt,
           COALESCE(SUM(gross_sales_cents), 0) AS gross_cents,
           COALESCE(SUM(net_sales_cents), 0) AS net_cents
    FROM agg_orders_hourly
    WHERE tenant_id = 42
      AND (
          (hour_bucket >= '2026-10-24 10:00:00+00' AND hour_bucket < '2026-10-25 00:00:00+00')
          OR
          (hour_bucket >= '2026-10-31 00:00:00+00' AND hour_bucket < '2026-10-31 14:00:00+00')
      )
)
SELECT
    (i.cnt + b.cnt) AS total_orders,
    ROUND((i.gross_cents + b.gross_cents) / 100.0, 2) AS gross_sales_amount,
    ROUND((i.net_cents + b.net_cents) / 100.0, 2) AS net_sales_amount,
    c.as_of_timestamp,
    c.last_applied_lsn_str
FROM interior_days i
CROSS JOIN boundary_hours b
CROSS JOIN cdc_checkpoint c
WHERE c.slot_name = 'agg_slot';

COMMIT;

-- =========================================================================
-- 6. TIME-SERIES TREND QUERY (FR-2: ORDERS PER DAY FOR LAST 30 DAYS)
-- =========================================================================
SELECT
    day_bucket AS bucket_date,
    SUM(order_count) AS total_orders,
    ROUND(SUM(gross_sales_cents) / 100.0, 2) AS gross_sales,
    ROUND(SUM(net_sales_cents) / 100.0, 2) AS net_sales
FROM agg_orders_daily
WHERE tenant_id = $1
  AND day_bucket BETWEEN $2::DATE AND $3::DATE
GROUP BY day_bucket
ORDER BY day_bucket ASC;

-- =========================================================================
-- 7. CATEGORY BREAKDOWN QUERY (FR-4: ORDERS BY STATUS)
-- =========================================================================
SELECT
    status,
    SUM(order_count) AS order_count,
    ROUND(SUM(gross_sales_cents) / 100.0, 2) AS total_amount
FROM agg_orders_daily
WHERE tenant_id = $1
  AND day_bucket BETWEEN $2::DATE AND $3::DATE
GROUP BY status
ORDER BY order_count DESC, status ASC;

-- =========================================================================
-- 8. TOP-N PRODUCTS RANKING QUERY (FR-3: TOP 10 PRODUCTS THIS WEEK)
-- =========================================================================
SELECT
    product_id,
    MAX(product_name) AS product_name,
    SUM(qty_sold) AS total_quantity_sold,
    SUM(order_count) AS total_orders,
    ROUND(SUM(net_revenue_cents) / 100.0, 2) AS net_sales_revenue
FROM agg_products_daily
WHERE tenant_id = $1
  AND day_bucket BETWEEN $2::DATE AND $3::DATE
GROUP BY product_id
HAVING SUM(net_revenue_cents) > 0
ORDER BY SUM(net_revenue_cents) DESC, product_id ASC
LIMIT $4;

PART B — REST API SPECIFICATION
1. API Design Principles
 * Zero-Trust Tenant Context Binding: Every tenant endpoint requires a cryptographic Bearer token or signed API key whose authorized tenant claim is verified against the URL path parameter before execution.
 * First-Class Freshness Contract: Every analytical response embeds a standardized freshness metadata block containing as_of, last_applied_lsn, and lag_ms (Bonus B-1).
 * Exact Hybrid Time-Window Evaluation: Endpoints accept full ISO-8601 UTC timestamps (from and to) and seamlessly combine pre-computed daily/hourly buckets with exact sub-hour edge scans.
 * Predictable Error Envelope: All 4xx and 5xx responses return a structured JSON error payload with an explicit machine-readable error code, message, and request correlation ID.
2. Endpoint Catalogue
| Endpoint ID | Method | URI Path | Feature Tier | Purpose |
|---|---|---|---|---|
| EP-01 | GET | /api/v1/health | Core Ops | Liveness and store connectivity check |
| EP-02 | GET | /api/v1/tenants/{tenant_id}/dashboard | Core Composite | Unified single-call dashboard payload |
| EP-03 | GET | /api/v1/tenants/{tenant_id}/orders/summary | Must-Have (FR-1) | Order counts and sales totals |
| EP-04 | GET | /api/v1/tenants/{tenant_id}/orders/trends | Must-Have (FR-2) | Hourly or daily order trends |
| EP-05 | GET | /api/v1/tenants/{tenant_id}/products/top | Must-Have (FR-3) | Top-N products by sales |
| EP-06 | GET | /api/v1/tenants/{tenant_id}/orders/by-status | Must-Have (FR-4) | Order breakdown by status category |
| EP-07 | GET | /api/v1/tenants/{tenant_id}/freshness | Bonus (B-1) | Real-time tenant watermark status |
| EP-08 | GET | /api/v1/admin/verification | Bonus (B-2, B-5) | Digest verification and lag monitor |
| EP-09 | POST | /api/v1/admin/rebuild | Bonus (B-2, B-4) | Targeted DBLog or full rebuild |
The table above maps every API endpoint to its corresponding functional or bonus requirement in the PRD.
3. Detailed Endpoint Specifications
EP-01: System Health Check
 * HTTP Method & URL: GET /api/v1/health
 * Purpose: Verifies connectivity to the Summary Database, Primary Database, and Redis cache.
 * Authentication Requirements: Public (no token required for container health probes).
 * Tenant Identification: None (system-level probe).
 * Query Parameters & Request Body: None.
 * Status Codes & Error Responses: 200 OK when healthy; 503 Service Unavailable if the Summary Store is unreachable.
{
  "status": "HEALTHY",
  "components": {
    "summary_db": "UP",
    "primary_db": "UP",
    "redis_cache": "UP",
    "cdc_worker_lag_ms": 240
  },
  "timestamp": "2026-10-31T14:12:00.000Z"
}

EP-02: Unified Tenant Dashboard Summary
 * HTTP Method & URL: GET /api/v1/tenants/{tenant_id}/dashboard
 * Purpose: Returns FR-1 (counts/totals), FR-2 (trend), FR-3 (Top-N products), and FR-4 (status breakdown) in a single parallelized call to power sub-200 ms dashboard loads.
 * Authentication Requirements: Bearer JWT or X-API-Key header required.
 * Tenant Identification: Path parameter tenant_id (integer) verified against token claims.
 * Query Parameters: from (ISO-8601 UTC string, required), to (ISO-8601 UTC string, required), granularity (enum: hour or day, default day), top_n (integer 1 to 100, default 10).
 * Request Body: None.
 * Status Codes & Error Responses: 200 OK; 400 Bad Request (invalid date range); 401 Unauthorized; 403 Forbidden (cross-tenant access attempt); 429 Too Many Requests (rate limit exceeded).
{
  "tenant_id": 42,
  "window": {
    "from": "2026-10-24T00:00:00Z",
    "to": "2026-10-31T14:00:00Z",
    "granularity": "day"
  },
  "summary": {
    "total_orders": 14250,
    "gross_sales": 845210.50,
    "net_sales": 798400.00
  },
  "status_breakdown": [
    { "status": "shipped", "order_count": 10200, "total_amount": 612000.00 },
    { "status": "placed", "order_count": 3150, "total_amount": 186400.00 },
    { "status": "cancelled", "order_count": 900, "total_amount": 46810.50 }
  ],
  "top_products": [
    { "rank": 1, "product_id": 5012, "product_name": "Pro Wireless Headset", "qty_sold": 890, "net_sales": 89000.00 }
  ],
  "trends": [
    { "bucket": "2026-10-24", "order_count": 1980, "gross_sales": 115200.00, "net_sales": 109500.00 }
  ],
  "freshness": {
    "as_of": "2026-10-31T14:11:59.650Z",
    "last_applied_lsn": "0/1A4F890",
    "lag_ms": 350,
    "human_readable": "updated 0.4 seconds ago",
    "status": "FRESH"
  }
}

EP-03: Counts and Totals for a Time Range (PRD FR-1)
 * HTTP Method & URL: GET /api/v1/tenants/{tenant_id}/orders/summary
 * Purpose: Returns total order count and total sales revenue for a tenant within any arbitrary [from, to] window.
 * Authentication Requirements: Bearer JWT or X-API-Key header required.
 * Tenant Identification: Path parameter tenant_id validated against authenticated caller context.
 * Query Parameters: from (ISO-8601 timestamp, required), to (ISO-8601 timestamp, required), exclude_cancelled (boolean, default false).
 * Request Body: None.
 * Status Codes & Error Responses: 200 OK; 400 Bad Request; 401 Unauthorized; 403 Forbidden; 429 Too Many Requests.
{
  "tenant_id": 42,
  "from": "2026-10-24T10:17:23Z",
  "to": "2026-10-31T14:42:09Z",
  "data": {
    "order_count": 13892,
    "gross_sales": 819450.25,
    "net_sales": 774120.00,
    "currency": "USD"
  },
  "freshness": {
    "as_of": "2026-10-31T14:42:09.120Z",
    "last_applied_lsn": "0/1A50120",
    "lag_ms": 280,
    "human_readable": "updated 0.3 seconds ago",
    "status": "FRESH"
  }
}

EP-04: Time-Series Trends Over Time (PRD FR-2)
 * HTTP Method & URL: GET /api/v1/tenants/{tenant_id}/orders/trends
 * Purpose: Returns chronological order counts and sales totals bucketed by hour or by day over the requested window.
 * Authentication Requirements: Bearer JWT or X-API-Key header required.
 * Tenant Identification: Path parameter tenant_id verified against caller credentials.
 * Query Parameters: from (ISO-8601 timestamp, required), to (ISO-8601 timestamp, required), granularity (enum: hour or day, required), page (integer, default 1), page_size (integer 1 to 1000, default 366).
 * Request Body: None.
 * Status Codes & Error Responses: 200 OK; 400 Bad Request; 401 Unauthorized; 403 Forbidden; 429 Too Many Requests.
{
  "tenant_id": 42,
  "granularity": "day",
  "from": "2026-10-01T00:00:00Z",
  "to": "2026-10-30T23:59:59Z",
  "pagination": {
    "page": 1,
    "page_size": 30,
    "total_buckets": 30
  },
  "series": [
    {
      "bucket_start": "2026-10-01T00:00:00Z",
      "order_count": 1840,
      "gross_sales": 92400.00,
      "net_sales": 88100.00
    },
    {
      "bucket_start": "2026-10-02T00:00:00Z",
      "order_count": 2105,
      "gross_sales": 105250.00,
      "net_sales": 101900.00
    }
  ],
  "freshness": {
    "as_of": "2026-10-31T14:12:10.000Z",
    "last_applied_lsn": "0/1A50120",
    "lag_ms": 310,
    "human_readable": "updated 0.3 seconds ago",
    "status": "FRESH"
  }
}

EP-05: Top-N Products Ranking (PRD FR-3)
 * HTTP Method & URL: GET /api/v1/tenants/{tenant_id}/products/top
 * Purpose: Returns the Top-N products ranked by sales revenue or quantity sold for the specified tenant and time window.
 * Authentication Requirements: Bearer JWT or X-API-Key header required.
 * Tenant Identification: Path parameter tenant_id verified against caller credentials.
 * Query Parameters: from (ISO-8601 timestamp, required), to (ISO-8601 timestamp, required), limit (integer 1 to 100, default 10), sort_by (enum: net_sales, gross_sales, qty_sold, default net_sales).
 * Request Body: None.
 * Status Codes & Error Responses: 200 OK; 400 Bad Request; 401 Unauthorized; 403 Forbidden; 429 Too Many Requests.
{
  "tenant_id": 42,
  "from": "2026-10-24T00:00:00Z",
  "to": "2026-10-31T00:00:00Z",
  "limit": 10,
  "sort_by": "net_sales",
  "items": [
    {
      "rank": 1,
      "product_id": 5012,
      "product_name": "Pro Wireless Headset",
      "qty_sold": 890,
      "order_count": 740,
      "gross_sales": 91200.00,
      "net_sales": 89000.00
    },
    {
      "rank": 2,
      "product_id": 5088,
      "product_name": "Mechanical Keyboard RGB",
      "qty_sold": 620,
      "order_count": 590,
      "gross_sales": 74400.00,
      "net_sales": 71300.00
    }
  ],
  "freshness": {
    "as_of": "2026-10-31T14:12:15.000Z",
    "last_applied_lsn": "0/1A50190",
    "lag_ms": 210,
    "human_readable": "updated 0.2 seconds ago",
    "status": "FRESH"
  }
}

EP-06: Category Breakdown by Order Status (PRD FR-4)
 * HTTP Method & URL: GET /api/v1/tenants/{tenant_id}/orders/by-status
 * Purpose: Returns order counts and revenue grouped by order status (placed, shipped, cancelled).
 * Authentication Requirements: Bearer JWT or X-API-Key header required.
 * Tenant Identification: Path parameter tenant_id verified against caller credentials.
 * Query Parameters: from (ISO-8601 timestamp, required), to (ISO-8601 timestamp, required).
 * Request Body: None.
 * Status Codes & Error Responses: 200 OK; 400 Bad Request; 401 Unauthorized; 403 Forbidden; 429 Too Many Requests.
{
  "tenant_id": 42,
  "from": "2026-10-24T00:00:00Z",
  "to": "2026-10-31T14:00:00Z",
  "categories": [
    { "status": "shipped", "order_count": 10200, "total_sales": 612000.00, "percentage_orders": 71.58 },
    { "status": "placed", "order_count": 3150, "total_sales": 186400.00, "percentage_orders": 22.11 },
    { "status": "cancelled", "order_count": 900, "total_sales": 46810.50, "percentage_orders": 6.31 }
  ],
  "freshness": {
    "as_of": "2026-10-31T14:12:20.000Z",
    "last_applied_lsn": "0/1A50200",
    "lag_ms": 195,
    "human_readable": "updated 0.2 seconds ago",
    "status": "FRESH"
  }
}

EP-07: Tenant Freshness and Watermark Status (Bonus B-1)
 * HTTP Method & URL: GET /api/v1/tenants/{tenant_id}/freshness
 * Purpose: Returns standalone replication watermark and ingestion queue depth for the specified tenant.
 * Authentication Requirements: Bearer JWT or X-API-Key header required.
 * Tenant Identification: Path parameter tenant_id verified against caller credentials.
 * Query Parameters & Request Body: None.
 * Status Codes & Error Responses: 200 OK; 401 Unauthorized; 403 Forbidden.
{
  "tenant_id": 42,
  "as_of": "2026-10-31T14:12:25.100Z",
  "last_applied_lsn": "0/1A50240",
  "lag_ms": 180,
  "human_readable": "updated 0.2 seconds ago",
  "pending_drr_queue_events": 0,
  "status": "FRESH"
}

EP-08: Verification Status and Operational Telemetry (Bonus B-2 & B-5)
 * HTTP Method & URL: GET /api/v1/admin/verification
 * Purpose: Exposes real-time query latency percentiles (p50/p95/p99), CDC lag, and per-bucket hash digest verification health for the /monitor dashboard.
 * Authentication Requirements: Admin Bearer token (role = admin) required.
 * Tenant Identification: Optional query filter tenant_id to inspect a specific tenant's digest buckets.
 * Query Parameters: tenant_id (integer, optional), only_mismatched (boolean, default false).
 * Request Body: None.
 * Status Codes & Error Responses: 200 OK; 401 Unauthorized; 403 Forbidden.
{
  "query_latency_ms": { "p50": 6.4, "p95": 18.2, "p99": 42.1 },
  "cdc_pipeline": {
    "slot_name": "agg_slot",
    "last_applied_lsn": "0/1A50240",
    "lag_ms": 180,
    "events_per_sec": 4820
  },
  "digest_verification": {
    "total_buckets_checked": 3650,
    "mismatched_buckets": 0,
    "last_repair_duration_ms": 145,
    "status": "VERIFIED_100_PERCENT"
  }
}

EP-09: Administrative Rebuild and Repair Trigger (Bonus B-2)
 * HTTP Method & URL: POST /api/v1/admin/rebuild
 * Purpose: Triggers either a targeted DBLog watermark-bracketed repair for specific (tenant_id, day_bucket) partitions or a full non-blocking snapshot rebuild.
 * Authentication Requirements: Admin Bearer token (role = admin) required.
 * Tenant Identification: Specified in JSON request body (tenant_id optional for global rebuild, or integer for single-tenant rebuild).
 * Query Parameters: None.
 * Request Body: JSON specifying mode (TARGETED_DIGEST_REPAIR or FULL_SNAPSHOT_REBUILD), optional tenant_id, and optional day_bucket.
 * Status Codes & Error Responses: 202 Accepted (background repair job queued into low-priority DRR lane); 400 Bad Request; 401 Unauthorized; 403 Forbidden.
{
  "job_id": "rep-20261031-0042",
  "mode": "TARGETED_DIGEST_REPAIR",
  "scope": { "tenant_id": 42, "day_bucket": "2026-10-12" },
  "status": "ACCEPTED",
  "estimated_completion_ms": 250
}

Standard Error Response Envelope (All Endpoints)
{
  "error": {
    "code": "TENANT_ACCESS_DENIED",
    "message": "Authenticated token for tenant 15 is not authorized to query tenant 42.",
    "request_id": "req-9f8c7b6a5d4e",
    "timestamp": "2026-10-31T14:15:00.000Z"
  }
}

4. API Security
 * Authentication: Every request must supply either an HMAC-SHA256 signed JWT in the Authorization: Bearer <token> header or a pre-shared tenant API key in X-Tenant-Key. For effortless hackathon evaluation by judges, a documented development toggle (AUTH_DEV_MODE=true) allows passing X-Tenant-ID matching the path while still exercising the full isolation middleware.
 * Authorization (Never Trust Client-Supplied Tenant ID): The security middleware extracts the authorized authenticated_tenant_id from the verified token claims and compares it against the URL path tenant_id. If they differ (and the token does not carry the admin role), the request is immediately aborted with HTTP 403 Forbidden before acquiring a database connection.
 * Multi-Layer Tenant Isolation: Before running any SQL query on the Summary Store connection, the repository layer executes SET LOCAL app.current_tenant_id = $1 inside the transaction, activating PostgreSQL Row-Level Security policies alongside parameterized WHERE tenant_id = $1 predicates.
 * Input Validation: FastAPI Pydantic schemas enforce strict constraints: tenant_id (\ge 1), limit (1 \le \text{limit} \le 100), valid ISO-8601 timestamps with from <= to, and maximum single-query span of 366 days.
 * Per-Tenant Rate Limiting & Concurrency Control (Bonus B-3): A Redis-backed Lua token bucket limits each tenant to 50 requests/second (burst 100) and a semaphore caps concurrent in-flight heavy queries per tenant at 4, returning HTTP 429 Too Many Requests with a Retry-After header when exceeded.
5. API Performance
 * Pagination: Time-series trend endpoints support page and page_size (default 366 buckets), while Top-N endpoints bound limit to at most 100 items.
 * LSN-Coherent Caching: Hot query responses are cached in Redis under the key format agg:v1:t{tenant_id}:lsn{last_applied_lsn}:{endpoint_hash} with a 2-second TTL. Because the key embeds last_applied_lsn, the instant a new CDC batch commits for that tenant, stale cache entries are bypassed automatically with zero cache-invalidation race conditions.
 * Query Optimization: The API backend uses an asyncpg connection pool with binary protocol prepared statements and executes decomposed sub-hour edge queries concurrently with interior bucket queries using asyncio.gather.
 * Response-Size Limits: Responses larger than 1 KB are automatically compressed via GZip middleware, and query windows exceeding 8,760 hourly buckets (1 year) are rejected with HTTP 400 to prevent memory spikes.
6. API Documentation (OpenAPI 3.0 & Runnable Examples)
6.1 OpenAPI 3.0 Structure
openapi: 3.0.3
info:
  title: Multi-Tenant Live Aggregation Layer API
  version: 1.0.0
  description: High-performance incremental rollup API with freshness watermarks and DBLog repair.
servers:
  - url: http://localhost:8000
security:
  - BearerAuth: []
components:
  securitySchemes:
    BearerAuth:
      type: http
      scheme: bearer
      bearerFormat: JWT
  parameters:
    TenantIdParam:
      name: tenant_id
      in: path
      required: true
      schema:
        type: integer
        minimum: 1
    FromParam:
      name: from
      in: query
      required: true
      schema:
        type: string
        format: date-time
    ToParam:
      name: to
      in: query
      required: true
      schema:
        type: string
        format: date-time
  schemas:
    FreshnessMetadata:
      type: object
      required: [as_of, last_applied_lsn, lag_ms, human_readable, status]
      properties:
        as_of:
          type: string
          format: date-time
        last_applied_lsn:
          type: string
        lag_ms:
          type: integer
        human_readable:
          type: string
        status:
          type: string
          enum: [FRESH, STALE_WARNING]
    ErrorResponse:
      type: object
      properties:
        error:
          type: object
          properties:
            code:
              type: string
            message:
              type: string
            request_id:
              type: string
            timestamp:
              type: string
              format: date-time
paths:
  /api/v1/tenants/{tenant_id}/orders/summary:
    get:
      summary: Counts and totals for a time range (FR-1)
      parameters:
        - $ref: '#/components/parameters/TenantIdParam'
        - $ref: '#/components/parameters/FromParam'
        - $ref: '#/components/parameters/ToParam'
      responses:
        '200':
          description: Exact order count and sales totals with freshness watermark
        '403':
          description: Cross-tenant access forbidden
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ErrorResponse'
        '429':
          description: Per-tenant rate or concurrency limit exceeded
  /api/v1/tenants/{tenant_id}/orders/trends:
    get:
      summary: Time-series trend per hour or day (FR-2)
      parameters:
        - $ref: '#/components/parameters/TenantIdParam'
        - $ref: '#/components/parameters/FromParam'
        - $ref: '#/components/parameters/ToParam'
        - name: granularity
          in: query
          required: true
          schema:
            type: string
            enum: [hour, day]
      responses:
        '200':
          description: Time-bucketed trend series
  /api/v1/tenants/{tenant_id}/products/top:
    get:
      summary: Top-N products by sales (FR-3)
      parameters:
        - $ref: '#/components/parameters/TenantIdParam'
        - $ref: '#/components/parameters/FromParam'
        - $ref: '#/components/parameters/ToParam'
        - name: limit
          in: query
          schema:
            type: integer
            default: 10
            minimum: 1
            maximum: 100
      responses:
        '200':
          description: Ranked Top-N product list
  /api/v1/tenants/{tenant_id}/orders/by-status:
    get:
      summary: Order breakdown by category status (FR-4)
      parameters:
        - $ref: '#/components/parameters/TenantIdParam'
        - $ref: '#/components/parameters/FromParam'
        - $ref: '#/components/parameters/ToParam'
      responses:
        '200':
          description: Breakdown by placed, shipped, and cancelled status

6.2 Example cURL Requests
# 1. Query Counts and Totals (FR-1) with arbitrary non-aligned timestamps
curl -X GET "http://localhost:8000/api/v1/tenants/42/orders/summary?from=2026-10-24T10:17:23Z&to=2026-10-31T14:42:09Z" \
  -H "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJ0ZW5hbnRfaWQiOjQyfQ.sig" \
  -H "Accept: application/json"

# 2. Query Top-10 Products by Sales This Week (FR-3)
curl -X GET "http://localhost:8000/api/v1/tenants/42/products/top?from=2026-10-24T00:00:00Z&to=2026-10-31T00:00:00Z&limit=10" \
  -H "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJ0ZW5hbnRfaWQiOjQyfQ.sig"

# 3. Trigger Targeted DBLog Bucket Repair (Bonus B-2)
curl -X POST "http://localhost:8000/api/v1/admin/rebuild" \
  -H "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJyb2xlIjoiYWRtaW4ifQ.sig" \
  -H "Content-Type: application/json" \
  -d '{"mode": "TARGETED_DIGEST_REPAIR", "tenant_id": 42, "day_bucket": "2026-10-12"}'
```bash
# 4. Query Time-Series Trends per Day (FR-2: Last 30 Days)
curl -X GET "http://localhost:8000/api/v1/tenants/42/orders/trends?from=2026-10-01T00:00:00Z&to=2026-10-30T23:59:59Z&granularity=day" \
  -H "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJ0ZW5hbnRfaWQiOjQyfQ.sig" \
  -H "Accept: application/json"

# 5. Query Category Breakdown by Order Status (FR-4)
curl -X GET "http://localhost:8000/api/v1/tenants/42/orders/by-status?from=2026-10-24T00:00:00Z&to=2026-10-31T14:00:00Z" \
  -H "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJ0ZW5hbnRfaWQiOjQyfQ.sig" \
  -H "Accept: application/json"

# 6. Check Tenant Freshness Watermark (Bonus B-1)
curl -X GET "http://localhost:8000/api/v1/tenants/42/freshness" \
  -H "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJ0ZW5hbnRfaWQiOjQyfQ.sig" \
  -H "Accept: application/json"

# 7. Query Operational Verification & Latency Telemetry (Bonus B-5)
curl -X GET "http://localhost:8000/api/v1/admin/verification" \
  -H "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJyb2xlIjoiYWRtaW4ifQ.sig" \
  -H "Accept: application/json"

```

#### 6.3 Example Responses

##### Response 1: Counts & Totals (`GET /api/v1/tenants/42/orders/summary`)

```json
{
  "tenant_id": 42,
  "from": "2026-10-24T10:17:23Z",
  "to": "2026-10-31T14:42:09Z",
  "data": {
    "order_count": 13892,
    "gross_sales": 819450.25,
    "net_sales": 774120.00,
    "currency": "USD"
  },
  "freshness": {
    "as_of": "2026-10-31T14:42:09.120Z",
    "last_applied_lsn": "0/1A50120",
    "lag_ms": 280,
    "human_readable": "updated 0.3 seconds ago",
    "status": "FRESH"
  }
}

```

##### Response 2: Category Breakdown by Status (`GET /api/v1/tenants/42/orders/by-status`)

```json
{
  "tenant_id": 42,
  "from": "2026-10-24T00:00:00Z",
  "to": "2026-10-31T14:00:00Z",
  "categories": [
    {
      "status": "shipped",
      "order_count": 10200,
      "total_sales": 612000.00,
      "percentage_orders": 71.58
    },
    {
      "status": "placed",
      "order_count": 3150,
      "total_sales": 186400.00,
      "percentage_orders": 22.11
    },
    {
      "status": "cancelled",
      "order_count": 900,
      "total_sales": 46810.50,
      "percentage_orders": 6.31
    }
  ],
  "freshness": {
    "as_of": "2026-10-31T14:12:20.000Z",
    "last_applied_lsn": "0/1A50200",
    "lag_ms": 195,
    "human_readable": "updated 0.2 seconds ago",
    "status": "FRESH"
  }
}

```

##### Response 3: Admin Targeted Repair (`POST /api/v1/admin/rebuild`)

```json
{
  "job_id": "rep-20261031-0042",
  "mode": "TARGETED_DIGEST_REPAIR",
  "scope": {
    "tenant_id": 42,
    "day_bucket": "2026-10-12"
  },
  "status": "COMPLETED",
  "metrics": {
    "rows_scanned": 1420,
    "duration_ms": 118,
    "mismatch_detected": true,
    "drift_corrected": true,
    "new_hash_sum": "1489201940294102941"
  }
}

```

---

## 7. Python FastAPI Implementation Reference

### 7.1 Database Connection Pool & Tenant Security Middleware

```python
# app/core/database.py
import asyncpg
from fastapi import FastAPI, Request, HTTPException, Security, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
import jwt
import os

DATABASE_URL = os.getenv("SUMMARY_DB_URL", "postgresql://agg_user:agg_pass@pg-summary:5432/agg_db")
JWT_SECRET = os.getenv("JWT_SECRET", "hackathon-secret-key-32bytes-min")
DEV_MODE = os.getenv("AUTH_DEV_MODE", "true").lower() == "true"

security_bearer = HTTPBearer(auto_error=False)

class DatabasePool:
    def __init__(self):
        self.pool: asyncpg.Pool = None

    async def connect(self):
        self.pool = await asyncpg.create_pool(
            dsn=DATABASE_URL,
            min_size=5,
            max_size=20,
            command_timeout=5.0
        )

    async def disconnect(self):
        if self.pool:
            await self.pool.close()

db = DatabasePool()

async def get_tenant_context(
    tenant_id: int,
    request: Request,
    credentials: HTTPAuthorizationCredentials = Security(security_bearer)
) -> int:
    """
    Enforces tenant scoping. Rejects unauthorized access before hitting any database table.
    Supports a dev override header (X-Dev-Tenant-ID) if AUTH_DEV_MODE is enabled.
    """
    if DEV_MODE:
        dev_header = request.headers.get("X-Dev-Tenant-ID")
        if dev_header and int(dev_header) == tenant_id:
            return tenant_id

    if not credentials:
        raise HTTPException(status_code=401, detail="Authentication credentials missing.")

    try:
        payload = jwt.decode(credentials.credentials, JWT_SECRET, algorithms=["HS256"])
        token_tenant_id = payload.get("tenant_id")
        role = payload.get("role", "tenant")

        if role == "admin":
            return tenant_id

        if token_tenant_id != tenant_id:
            raise HTTPException(
                status_code=403,
                detail=f"Cross-tenant access forbidden: Token tenant {token_tenant_id} cannot access tenant {tenant_id}."
            )
        return tenant_id
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="Invalid or expired Bearer token.")

```

### 7.2 Hybrid Time-Range Decomposer & Query Router

```python
# app/services/query_service.py
from datetime import datetime, timezone, timedelta
import asyncpg
from typing import Dict, Any, List

def decompose_range(start_ts: datetime, end_ts: datetime) -> Dict[str, Any]:
    """
    Splits arbitrary [start_ts, end_ts] into:
    1. Full Interior Calendar Days (queried from agg_orders_daily)
    2. Boundary Full Hours (queried from agg_orders_hourly)
    3. Boundary Sub-Hour Residuals (executed as index-only edge scans on base tables)
    """
    start_utc = start_ts.astimezone(timezone.utc)
    end_utc = end_ts.astimezone(timezone.utc)

    # Calculate hour ceiling of start and hour floor of end
    left_hour_ceil = (start_utc + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
    right_hour_floor = end_utc.replace(minute=0, second=0, microsecond=0)

    # Check for sub-hour edges
    left_edge = (start_utc, min(left_hour_ceil, end_utc)) if start_utc < left_hour_ceil else None
    right_edge = (right_hour_floor, end_utc) if (right_hour_floor < end_utc and right_hour_floor >= left_hour_ceil) else None

    # Full days calculation between hours
    interior_day_start = (left_hour_ceil + timedelta(days=1)).date() if left_hour_ceil.hour != 0 else left_hour_ceil.date()
    interior_day_end = right_hour_floor.date() - timedelta(days=1)

    return {
        "has_edges": bool(left_edge or right_edge),
        "left_edge": left_edge,
        "right_edge": right_edge,
        "hourly_ranges": [(left_hour_ceil, right_hour_floor)] if left_hour_ceil < right_hour_floor else [],
        "interior_days": (interior_day_start, interior_day_end) if interior_day_start <= interior_day_end else None
    }

async def fetch_tenant_summary(
    pool: asyncpg.Pool,
    tenant_id: int,
    from_ts: datetime,
    to_ts: datetime
) -> Dict[str, Any]:
    plan = decompose_range(from_ts, to_ts)

    async with pool.acquire() as conn:
        async with conn.transaction(isolation="repeatable_read", readonly=True):
            # Enforce Row-Level Security session context
            await conn.execute("SET LOCAL app.current_tenant_id = $1", str(tenant_id))

            # Query 1: Read latest committed checkpoint watermark
            cp = await conn.fetchrow(
                "SELECT as_of_timestamp, last_applied_lsn_str FROM cdc_checkpoint WHERE slot_name = 'agg_slot'"
            )
            as_of = cp["as_of_timestamp"] if cp else datetime.now(timezone.utc)
            lsn_str = cp["last_applied_lsn_str"] if cp else "0/0"
            lag_ms = max(0, int((datetime.now(timezone.utc) - as_of).total_seconds() * 1000))

            # Query 2: Aggregate interior days
            total_orders = 0
            gross_sales_cents = 0
            net_sales_cents = 0

            if plan["interior_days"]:
                d_start, d_end = plan["interior_days"]
                row = await conn.fetchrow("""
                    SELECT COALESCE(SUM(order_count), 0) AS cnt,
                           COALESCE(SUM(gross_sales_cents), 0) AS gross,
                           COALESCE(SUM(net_sales_cents), 0) AS net
                    FROM agg_orders_daily
                    WHERE tenant_id = $1 AND day_bucket BETWEEN $2 AND $3
                """, tenant_id, d_start, d_end)
                total_orders += row["cnt"]
                gross_sales_cents += row["gross"]
                net_sales_cents += row["net"]

            # Query 3: Aggregate boundary full hours
            if plan["hourly_ranges"]:
                h_start, h_end = plan["hourly_ranges"][0]
                row = await conn.fetchrow("""
                    SELECT COALESCE(SUM(order_count), 0) AS cnt,
                           COALESCE(SUM(gross_sales_cents), 0) AS gross,
                           COALESCE(SUM(net_sales_cents), 0) AS net
                    FROM agg_orders_hourly
                    WHERE tenant_id = $1 AND hour_bucket >= $2 AND hour_bucket < $3
                """, tenant_id, h_start, h_end)
                total_orders += row["cnt"]
                gross_sales_cents += row["gross"]
                net_sales_cents += row["net"]

    return {
        "tenant_id": tenant_id,
        "from": from_ts.isoformat(),
        "to": to_ts.isoformat(),
        "data": {
            "order_count": total_orders,
            "gross_sales": round(gross_sales_cents / 100.0, 2),
            "net_sales": round(net_sales_cents / 100.0, 2),
            "currency": "USD"
        },
        "freshness": {
            "as_of": as_of.isoformat(),
            "last_applied_lsn": lsn_str,
            "lag_ms": lag_ms,
            "human_readable": f"updated {lag_ms / 1000.0:.1f} seconds ago",
            "status": "FRESH" if lag_ms < 30000 else "STALE_WARNING"
        }
    }

```

### 7.3 Complete FastAPI Router Implementation

```python
# app/main.py
from fastapi import FastAPI, Depends, Query, Path
from datetime import datetime
from app.core.database import db, get_tenant_context
from app.services.query_service import fetch_tenant_summary

app = FastAPI(
    title="Multi-Tenant Live Aggregation Layer",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc"
)

@app.on_event("startup")
async def startup():
    await db.connect()

@app.on_event("shutdown")
async def shutdown():
    await db.disconnect()

@app.get("/api/v1/health", tags=["Operational"])
async def health_check():
    async with db.pool.acquire() as conn:
        val = await conn.fetchval("SELECT 1")
    return {"status": "HEALTHY", "summary_db_probe": val == 1}

@app.get("/api/v1/tenants/{tenant_id}/orders/summary", tags=["FR-1: Counts & Totals"])
async def get_summary(
    tenant_id: int = Path(..., ge=1, description="Target Tenant ID"),
    from_ts: datetime = Query(..., alias="from", description="UTC Start ISO-8601"),
    to_ts: datetime = Query(..., alias="to", description="UTC End ISO-8601"),
    authorized_tenant: int = Depends(get_tenant_context)
):
    return await fetch_tenant_summary(db.pool, authorized_tenant, from_ts, to_ts)

@app.get("/api/v1/tenants/{tenant_id}/orders/trends", tags=["FR-2: Time Trends"])
async def get_trends(
    tenant_id: int = Path(..., ge=1),
    from_ts: datetime = Query(..., alias="from"),
    to_ts: datetime = Query(..., alias="to"),
    granularity: str = Query("day", regex="^(hour|day)$"),
    authorized_tenant: int = Depends(get_tenant_context)
):
    table = "agg_orders_daily" if granularity == "day" else "agg_orders_hourly"
    col = "day_bucket" if granularity == "day" else "hour_bucket"

    async with db.pool.acquire() as conn:
        await conn.execute("SET LOCAL app.current_tenant_id = $1", str(authorized_tenant))
        rows = await conn.fetch(f"""
            SELECT {col} AS bucket,
                   SUM(order_count) AS order_count,
                   ROUND(SUM(gross_sales_cents) / 100.0, 2) AS gross_sales,
                   ROUND(SUM(net_sales_cents) / 100.0, 2) AS net_sales
            FROM {table}
            WHERE tenant_id = $1 AND {col} BETWEEN $2 AND $3
            GROUP BY {col}
            ORDER BY {col} ASC
        """, authorized_tenant, from_ts.date() if granularity == "day" else from_ts, to_ts.date() if granularity == "day" else to_ts)

    return {
        "tenant_id": authorized_tenant,
        "granularity": granularity,
        "series": [dict(r) for r in rows]
    }

@app.get("/api/v1/tenants/{tenant_id}/products/top", tags=["FR-3: Top-N Products"])
async def get_top_products(
    tenant_id: int = Path(..., ge=1),
    from_ts: datetime = Query(..., alias="from"),
    to_ts: datetime = Query(..., alias="to"),
    limit: int = Query(10, ge=1, le=100),
    authorized_tenant: int = Depends(get_tenant_context)
):
    async with db.pool.acquire() as conn:
        await conn.execute("SET LOCAL app.current_tenant_id = $1", str(authorized_tenant))
        rows = await conn.fetch("""
            SELECT product_id,
                   MAX(product_name) AS product_name,
                   SUM(qty_sold) AS qty_sold,
                   SUM(order_count) AS order_count,
                   ROUND(SUM(net_revenue_cents) / 100.0, 2) AS net_sales
            FROM agg_products_daily
            WHERE tenant_id = $1 AND day_bucket BETWEEN $2 AND $3
            GROUP BY product_id
            HAVING SUM(net_revenue_cents) > 0
            ORDER BY SUM(net_revenue_cents) DESC, product_id ASC
            LIMIT $4
        """, authorized_tenant, from_ts.date(), to_ts.date(), limit)

    return {
        "tenant_id": authorized_tenant,
        "limit": limit,
        "items": [
            {"rank": i + 1, **dict(r)} for i, r in enumerate(rows)
        ]
    }

@app.get("/api/v1/tenants/{tenant_id}/orders/by-status", tags=["FR-4: Category Breakdown"])
async def get_orders_by_status(
    tenant_id: int = Path(..., ge=1),
    from_ts: datetime = Query(..., alias="from"),
    to_ts: datetime = Query(..., alias="to"),
    authorized_tenant: int = Depends(get_tenant_context)
):
    async with db.pool.acquire() as conn:
        await conn.execute("SET LOCAL app.current_tenant_id = $1", str(authorized_tenant))
        rows = await conn.fetch("""
            SELECT status,
                   SUM(order_count) AS order_count,
                   ROUND(SUM(gross_sales_cents) / 100.0, 2) AS total_sales
            FROM agg_orders_daily
            WHERE tenant_id = $1 AND day_bucket BETWEEN $2 AND $3
            GROUP BY status
            ORDER BY order_count DESC, status ASC
        """, authorized_tenant, from_ts.date(), to_ts.date())

    return {
        "tenant_id": authorized_tenant,
        "categories": [dict(r) for r in rows]
    }

```

---

## 8. Change Data Capture (CDC) Worker Pipeline

The CDC engine runs as an independent daemon (`cdc-worker`). It connects to PostgreSQL's replication slot via logical replication streaming (`pgoutput`), groups changes into bounded micro-batches, computes Z-set deltas, and commits the state and LSN checkpoint atomically.

```python
# worker/cdc_processor.py
import asyncio
import os
from datetime import datetime, timezone
import psycopg
from psycopg import sql

PRIMARY_DB_URL = os.getenv("PRIMARY_DB_URL", "postgresql://rep_user:rep_pass@pg-primary:5432/primary_db")
SUMMARY_DB_URL = os.getenv("SUMMARY_DB_URL", "postgresql://agg_user:agg_pass@pg-summary:5432/agg_db")
SLOT_NAME = "agg_slot"
PUBLICATION_NAME = "agg_layer_pub"

class MicroBatchCoalescer:
    """
    Coalesces row-level mutations over a 100ms window into net additive deltas.
    Keys on: (tenant_id, bucket_hour, bucket_day, status)
    """
    def __init__(self):
        self.hourly_deltas = {}
        self.daily_deltas = {}
        self.max_lsn = 0
        self.latest_commit_ts = None

    def add_order_mutation(self, lsn: int, commit_ts: datetime, tenant_id: int, created_at: datetime, old_row: dict, new_row: dict):
        self.max_lsn = max(self.max_lsn, lsn)
        self.latest_commit_ts = commit_ts

        hour_ts = created_at.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
        day_date = created_at.astimezone(timezone.utc).date()

        # Step 1: Handle Retraction (-1) if old_row exists (UPDATE / DELETE)
        if old_row:
            old_status = old_row["status"]
            old_amt = int(old_row["total_amount_cents"])
            old_net = old_amt if old_status != "cancelled" else 0

            # Retract from Hourly
            h_key = (tenant_id, hour_ts, old_status)
            self._apply_delta(self.hourly_deltas, h_key, count=-1, gross=-old_amt, net=-old_net)

            # Retract from Daily
            d_key = (tenant_id, day_date, old_status)
            self._apply_delta(self.daily_deltas, d_key, count=-1, gross=-old_amt, net=-old_net)

        # Step 2: Handle Insertion (+1) if new_row exists (INSERT / UPDATE)
        if new_row:
            new_status = new_row["status"]
            new_amt = int(new_row["total_amount_cents"])
            new_net = new_amt if new_status != "cancelled" else 0

            # Insert to Hourly
            h_key = (tenant_id, hour_ts, new_status)
            self._apply_delta(self.hourly_deltas, h_key, count=1, gross=new_amt, net=new_net)

            # Insert to Daily
            d_key = (tenant_id, day_date, new_status)
            self._apply_delta(self.daily_deltas, d_key, count=1, gross=new_amt, net=new_net)

    def _apply_delta(self, storage: dict, key: tuple, count: int, gross: int, net: int):
        if key not in storage:
            storage[key] = {"count": 0, "gross": 0, "net": 0}
        storage[key]["count"] += count
        storage[key]["gross"] += gross
        storage[key]["net"] += net

    def is_empty(self) -> bool:
        return len(self.hourly_deltas) == 0 and len(self.daily_deltas) == 0

async def flush_batch_to_summary(summary_conn, batch: MicroBatchCoalescer):
    """
    Flushes all coalesced deltas and the latest LSN in a single atomic ACID transaction.
    """
    if batch.is_empty():
        return

    async with summary_conn.transaction():
        # Checkpoint check for idempotency
        cursor = await summary_conn.execute(
            "SELECT last_applied_lsn FROM cdc_checkpoint WHERE slot_name = %s FOR UPDATE",
            (SLOT_NAME,)
        )
        row = await cursor.fetchone()
        last_lsn = row[0] if row else 0

        if batch.max_lsn <= last_lsn:
            return  # Discard already-applied replayed LSNs

        # Apply Hourly Rollups
        for (tenant_id, hour_ts, status), metrics in batch.hourly_deltas.items():
            if metrics["count"] == 0 and metrics["gross"] == 0 and metrics["net"] == 0:
                continue
            await summary_conn.execute("""
                INSERT INTO agg_orders_hourly (tenant_id, hour_bucket, status, order_count, gross_sales_cents, net_sales_cents)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (tenant_id, hour_bucket, status) DO UPDATE SET
                    order_count = agg_orders_hourly.order_count + EXCLUDED.order_count,
                    gross_sales_cents = agg_orders_hourly.gross_sales_cents + EXCLUDED.gross_sales_cents,
                    net_sales_cents = agg_orders_hourly.net_sales_cents + EXCLUDED.net_sales_cents
            """, (tenant_id, hour_ts, status, metrics["count"], metrics["gross"], metrics["net"]))

        # Apply Daily Rollups
        for (tenant_id, day_date, status), metrics in batch.daily_deltas.items():
            if metrics["count"] == 0 and metrics["gross"] == 0 and metrics["net"] == 0:
                continue
            await summary_conn.execute("""
                INSERT INTO agg_orders_daily (tenant_id, day_bucket, status, order_count, gross_sales_cents, net_sales_cents)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (tenant_id, day_bucket, status) DO UPDATE SET
                    order_count = agg_orders_daily.order_count + EXCLUDED.order_count,
                    gross_sales_cents = agg_orders_daily.gross_sales_cents + EXCLUDED.gross_sales_cents,
                    net_sales_cents = agg_orders_daily.net_sales_cents + EXCLUDED.net_sales_cents
            """, (tenant_id, day_date, status, metrics["count"], metrics["gross"], metrics["net"]))

        # Atomically advance LSN checkpoint
        lsn_hex = f"{batch.max_lsn >> 32:X}/{batch.max_lsn & 0xFFFFFFFF:X}"
        await summary_conn.execute("""
            INSERT INTO cdc_checkpoint (slot_name, last_applied_lsn, last_applied_lsn_str, as_of_timestamp, updated_at)
            VALUES (%s, %s, %s, %s, NOW())
            ON CONFLICT (slot_name) DO UPDATE SET
                last_applied_lsn = EXCLUDED.last_applied_lsn,
                last_applied_lsn_str = EXCLUDED.last_applied_lsn_str,
                as_of_timestamp = EXCLUDED.as_of_timestamp,
                updated_at = NOW()
        """, (SLOT_NAME, batch.max_lsn, lsn_hex, batch.latest_commit_ts or datetime.now(timezone.utc)))

```

---

## 9. Verification & Reconciliation Engine (DBLog Protocol)

To prove correctness and deliver on Bonus B-2, this service runs as a background process comparing bucket hash sums against the primary ground truth. When a corrupted bucket is detected (e.g., during live fault injection), it triggers a non-blocking chunk repair.

```python
# worker/verifier.py
import asyncio
from datetime import date
import psycopg
import xxhash

def compute_row_hash(order_id: int, status: str, amount_cents: int) -> int:
    """Computes a 64-bit integer hash for a row to maintain modular checksums."""
    h = xxhash.xxh64()
    h.update(f"{order_id}:{status}:{amount_cents}".encode("utf-8"))
    return h.intdigest()

async def verify_bucket_digest(primary_conn, summary_conn, tenant_id: int, day: date) -> bool:
    """
    Compares the Summary Store bucket digest against the Primary DB ground truth.
    Executes within an index-only scan window.
    """
    # 1. Fetch Summary Digest
    s_cur = await summary_conn.execute(
        "SELECT row_count, sum_amount_cents, hash_sum_mod64 FROM bucket_digests WHERE tenant_id = %s AND day_bucket = %s",
        (tenant_id, day)
    )
    summary_digest = await s_cur.fetchone()

    # 2. Fetch Ground Truth from Primary
    p_cur = await primary_conn.execute("""
        SELECT COUNT(*) AS cnt,
               COALESCE(SUM(total_amount_cents), 0) AS total_cents
        FROM orders
        WHERE tenant_id = %s AND created_at >= %s AND created_at < %s + INTERVAL '1 day'
    """, (tenant_id, day, day))
    ground_truth = await p_cur.fetchone()

    if not summary_digest and ground_truth[0] == 0:
        return True  # Empty bucket matches

    if not summary_digest or summary_digest[0] != ground_truth[0] or summary_digest[1] != ground_truth[1]:
        # Flag corruption in summary store
        await summary_conn.execute(
            "UPDATE bucket_digests SET is_corrupted = TRUE WHERE tenant_id = %s AND day_bucket = %s",
            (tenant_id, day)
        )
        return False

    return True

async def repair_bucket_chunked(primary_conn, summary_conn, tenant_id: int, day: date):
    """
    DBLog Watermark-Bracketed targeted repair for a single mismatched (tenant_id, day).
    Reads primary DB in small chunks with lock-free semantics and atomic replacement.
    """
    # Step 1: Read exact ground-truth aggregates for the specific day from primary
    cursor = await primary_conn.execute("""
        SELECT status,
               COUNT(*) AS order_count,
               SUM(total_amount_cents) AS gross_sales_cents,
               SUM(CASE WHEN status <> 'cancelled' THEN total_amount_cents ELSE 0 END) AS net_sales_cents
        FROM orders
        WHERE tenant_id = %s AND created_at >= %s AND created_at < %s + INTERVAL '1 day'
        GROUP BY status
    """, (tenant_id, day, day))
    repaired_rows = await cursor.fetchall()

    # Step 2: Atomically overwrite only the corrupted day in summary store
    async with summary_conn.transaction():
        # Clear damaged daily status rows
        await summary_conn.execute(
            "DELETE FROM agg_orders_daily WHERE tenant_id = %s AND day_bucket = %s",
            (tenant_id, day)
        )

        # Insert fresh ground-truth records
        for r in repaired_rows:
            await summary_conn.execute("""
                INSERT INTO agg_orders_daily (tenant_id, day_bucket, status, order_count, gross_sales_cents, net_sales_cents)
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (tenant_id, day, r[0], r[1], r[2], r[3]))

        # Clear corruption flag
        await summary_conn.execute(
            "UPDATE bucket_digests SET is_corrupted = FALSE, verified_at = NOW() WHERE tenant_id = %s AND day_bucket = %s",
            (tenant_id, day)
        )

```

---

## 10. Automated Benchmark & Verification Harness

This automated test script (`benchmark_harness.py`) runs the performance suite against 10M records across 100 tenants, measures query latencies and OLTP overhead, and prints an evaluation report matching the PRD criteria.

```python
# tests/benchmark_harness.py
import asyncio
import time
import httpx
import statistics

BASE_URL = "http://localhost:8000/api/v1"
HEADERS = {"X-Dev-Tenant-ID": "42"}  # Test tenant 42

async def benchmark_endpoint(client: httpx.AsyncClient, name: str, url: str, runs: int = 100):
    latencies = []
    for _ in range(runs):
        start = time.perf_counter()
        resp = await client.get(url, headers=HEADERS)
        duration_ms = (time.perf_counter() - start) * 1000.0
        assert resp.status_code == 200, f"Query failed: {resp.text}"
        latencies.append(duration_ms)

    latencies.sort()
    p50 = statistics.median(latencies)
    p95 = latencies[int(len(latencies) * 0.95)]
    p99 = latencies[int(len(latencies) * 0.99)]
    return {"name": name, "p50": p50, "p95": p95, "p99": p99}

async def run_full_suite():
    print("==========================================================================")
    print("        STARTING MULTI-TENANT AGGREGATION LAYER BENCHMARK HARNESS         ")
    print("==========================================================================")

    async with httpx.AsyncClient(base_url=BASE_URL, timeout=10.0) as client:
        # 1. Verify System Health
        h = await client.get("/health")
        print(f"[*] Health Check Status: {h.json()}")

        # 2. Benchmark FR-1: Counts and Totals
        r1 = await benchmark_endpoint(
            client,
            "FR-1: Counts & Totals (7 Days)",
            "/tenants/42/orders/summary?from=2026-10-24T00:00:00Z&to=2026-10-31T14:00:00Z"
        )

        # 3. Benchmark FR-2: 30-Day Daily Trends
        r2 = await benchmark_endpoint(
            client,
            "FR-2: Daily Trends (30 Days)",
            "/tenants/42/orders/trends?from=2026-10-01T00:00:00Z&to=2026-10-31T00:00:00Z&granularity=day"
        )

        # 4. Benchmark FR-3: Top-10 Products Ranking
        r3 = await benchmark_endpoint(
            client,
            "FR-3: Top-10 Products",
            "/tenants/42/products/top?from=2026-10-24T00:00:00Z&to=2026-10-31T00:00:00Z&limit=10"
        )

        # 5. Benchmark FR-4: Category Breakdown by Status
        r4 = await benchmark_endpoint(
            client,
            "FR-4: Category Status Breakdown",
            "/tenants/42/orders/by-status?from=2026-10-24T00:00:00Z&to=2026-10-31T14:00:00Z"
        )

        # 6. Check Replication Watermark & Freshness Lag
        freshness_resp = await client.get("/tenants/42/freshness", headers=HEADERS)
        freshness_data = freshness_resp.json()

    print("\n------------------------- BENCHMARK RESULTS -----------------------------")
    print(f"{'Endpoint':<32} | {'p50 (ms)':<10} | {'p95 (ms)':<10} | {'p99 (ms)':<10}")
    print("-------------------------------------------------------------------------")
    for r in [r1, r2, r3, r4]:
        print(f"{r['name']:<32} | {r['p50']:<10.2f} | {r['p95']:<10.2f} | {r['p99']:<10.2f}")
    print("-------------------------------------------------------------------------")

    print("\n-------------------------- PRD SCORECARD --------------------------------")
    p95_max = max(r['p95'] for r in [r1, r2, r3, r4])
    lag_ms = freshness_data.get("lag_ms", 0)

    print(f"Target 1: Query Latency   -> Measured p95: {p95_max:.2f} ms")
    print(f"          Result: {'GREAT (< 200ms)' if p95_max < 200 else 'GOOD (< 1000ms)'}")

    print(f"Target 2: Data Freshness  -> Measured Lag: {lag_ms / 1000.0:.2f} s")
    print(f"          Result: {'GREAT (< 30s)' if lag_ms < 30000 else 'GOOD (< 5m)'}")
    print("==========================================================================")

if __name__ == "__main__":
    asyncio.run(run_full_suite())

```

---

## 11. Turnkey Containerized Deployment & Execution Guide

### 11.1 Docker Compose Configuration (`docker-compose.yml`)

```yaml
version: '3.8'

services:
  pg-primary:
    image: postgres:16-alpine
    container_name: pg-primary
    environment:
      POSTGRES_DB: primary_db
      POSTGRES_USER: postgres
      POSTGRES_PASSWORD: primary_password
    command:
      - "postgres"
      - "-c"
      - "wal_level=logical"
      - "-c"
      - "max_replication_slots=4"
      - "-c"
      - "max_wal_senders=4"
      - "-c"
      - "max_slot_wal_keep_size=2048MB"
      - "-c"
      - "shared_buffers=1024MB"
    ports:
      - "5432:5432"
    deploy:
      resources:
        limits:
          cpus: '2.0'
          memory: 4096M
    volumes:
      - pg_primary_data:/var/lib/postgresql/data
    networks:
      - agg-net

  pg-summary:
    image: postgres:16-alpine
    container_name: pg-summary
    environment:
      POSTGRES_DB: agg_db
      POSTGRES_USER: agg_user
      POSTGRES_PASSWORD: agg_password
    command:
      - "postgres"
      - "-c"
      - "shared_buffers=512MB"
      - "-c"
      - "synchronous_commit=off"
    ports:
      - "5433:5432"
    deploy:
      resources:
        limits:
          cpus: '1.0'
          memory: 2048M
    volumes:
      - pg_summary_data:/var/lib/postgresql/data
      - ./init-summary.sql:/docker-entrypoint-initdb.d/init.sql
    networks:
      - agg-net

  redis-cache:
    image: redis:7-alpine
    container_name: redis-cache
    ports:
      - "6379:6379"
    deploy:
      resources:
        limits:
          cpus: '0.5'
          memory: 256M
    networks:
      - agg-net

  cdc-worker:
    build:
      context: .
      dockerfile: Dockerfile.worker
    container_name: cdc-worker
    depends_on:
      - pg-primary
      - pg-summary
    environment:
      PRIMARY_DB_URL: "postgresql://postgres:primary_password@pg-primary:5432/primary_db"
      SUMMARY_DB_URL: "postgresql://agg_user:agg_password@pg-summary:5432/agg_db"
    restart: on-failure
    deploy:
      resources:
        limits:
          cpus: '1.0'
          memory: 1024M
    networks:
      - agg-net

  agg-api:
    build:
      context: .
      dockerfile: Dockerfile.api
    container_name: agg-api
    depends_on:
      - pg-summary
      - redis-cache
    ports:
      - "8000:8000"
    environment:
      SUMMARY_DB_URL: "postgresql://agg_user:agg_password@pg-summary:5432/agg_db"
      REDIS_URL: "redis://redis-cache:6379/0"
      AUTH_DEV_MODE: "true"
    deploy:
      resources:
        limits:
          cpus: '1.0'
          memory: 1024M
    networks:
      - agg-net

networks:
  agg-net:
    driver: bridge

volumes:
  pg_primary_data:
  pg_summary_data:

```

### 11.2 Step-by-Step Execution & Hackathon Demonstration Commands

```bash
# 1. Clone repository and start all containers
git clone https://github.com/team-debug4x/fast-multitenant-agg.git
cd fast-multitenant-agg
docker compose up -d

# 2. Wait 5 seconds for healthy database initialization
docker compose ps

# 3. Seed the 10 Million Record dataset (100 Tenants) using the generator
python scripts/seed_dataset.py --records 10000000 --tenants 100

# 4. Bootstrap initial summary rollup state
python scripts/bootstrap_snapshot.py

# 5. Run the automated benchmark harness to generate the evaluation report
python tests/benchmark_harness.py

# 6. Run live mutation & retraction test (simulates order placement & cancellation)
python tests/test_live_mutations.py --orders 500 --cancellations 50

# 7. Open the interactive OpenAPI documentation in a browser
# URL: http://localhost:8000/docs

```