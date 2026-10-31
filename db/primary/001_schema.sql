-- Xponent — Primary OLTP database schema
-- Source of truth for transactional data. Matches 04_Database_API_Specification.md tables 1-5.
-- Currency is always integer cents (BIGINT) to avoid floating-point drift.
-- orders + order_items use REPLICA IDENTITY FULL so logical-decoding UPDATE/DELETE
-- events carry the full before-image (needed for Z-set retractions).

BEGIN;

CREATE TABLE IF NOT EXISTS tenants (
    tenant_id   INTEGER PRIMARY KEY,
    store_name  VARCHAR(120) NOT NULL,
    plan_tier   VARCHAR(32)  NOT NULL DEFAULT 'standard',
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS products (
    product_id       BIGINT PRIMARY KEY,
    tenant_id        INTEGER NOT NULL REFERENCES tenants(tenant_id),
    sku              VARCHAR(64)  NOT NULL,
    product_name     VARCHAR(255) NOT NULL,
    unit_price_cents BIGINT NOT NULL CHECK (unit_price_cents >= 0),
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS customers (
    customer_id BIGINT PRIMARY KEY,
    tenant_id   INTEGER NOT NULL REFERENCES tenants(tenant_id),
    email_hash  VARCHAR(128) NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS orders (
    order_id           BIGINT PRIMARY KEY,
    tenant_id          INTEGER NOT NULL REFERENCES tenants(tenant_id),
    customer_id        BIGINT  NOT NULL REFERENCES customers(customer_id),
    status             VARCHAR(32) NOT NULL CHECK (status IN ('placed', 'shipped', 'cancelled')),
    total_amount_cents BIGINT NOT NULL CHECK (total_amount_cents >= 0),
    created_at         TIMESTAMPTZ NOT NULL,
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Full before-image in WAL for accurate status/amount/bucket retractions (FR-15).
ALTER TABLE orders REPLICA IDENTITY FULL;

CREATE TABLE IF NOT EXISTS order_items (
    order_item_id    BIGINT PRIMARY KEY,
    order_id         BIGINT  NOT NULL REFERENCES orders(order_id),
    tenant_id        INTEGER NOT NULL,          -- denormalized: bucket product deltas without a join
    product_id       BIGINT  NOT NULL REFERENCES products(product_id),
    quantity         INTEGER NOT NULL CHECK (quantity > 0),
    line_amount_cents BIGINT NOT NULL CHECK (line_amount_cents >= 0),
    order_created_at TIMESTAMPTZ NOT NULL        -- denormalized parent created_at for bucketing
);

ALTER TABLE order_items REPLICA IDENTITY FULL;

COMMIT;
