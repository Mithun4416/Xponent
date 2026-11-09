"""asyncpg connection pools (task T7, extended for per-tenant source DBs).

Pools:
  * summary        — the central rollup store (reader role, RLS-constrained).
  * summary_owner  — owner (writes) for admin Receipts repair only (optional).
  * source_pools   — one pool per distinct tenant source DSN (per-tenant-DB
                     topology). `primary_for_tenant()` resolves a tenant to its
                     source pool; `primary` is the default/shared source.

With TENANT_SOURCES unset there is a single shared source and this behaves
exactly as before.
"""
from __future__ import annotations

import asyncpg

from app.core import config
from common.registry import SourceEntry, load_sources, source_for_tenant


class Pools:
    summary: asyncpg.Pool | None = None
    primary: asyncpg.Pool | None = None          # default/shared source pool
    summary_owner: asyncpg.Pool | None = None
    source_pools: dict[str, asyncpg.Pool] = {}
    sources: list[SourceEntry] = []


pools = Pools()


async def connect() -> None:
    pools.summary = await asyncpg.create_pool(
        dsn=config.SUMMARY_DB_URL, min_size=config.POOL_MIN, max_size=config.POOL_MAX, command_timeout=10.0
    )
    # One small pool per distinct tenant source database.
    pools.sources = load_sources(config.PRIMARY_DB_URL, config.SLOT_NAME)
    pools.source_pools = {}
    for s in pools.sources:
        if s.dsn and s.dsn not in pools.source_pools:
            pools.source_pools[s.dsn] = await asyncpg.create_pool(
                dsn=s.dsn, min_size=1, max_size=4, command_timeout=30.0
            )
    default = source_for_tenant(pools.sources, -1)  # no tenant match -> shared/first
    pools.primary = pools.source_pools.get(default.dsn)

    if config.SUMMARY_DB_OWNER_URL:
        pools.summary_owner = await asyncpg.create_pool(
            dsn=config.SUMMARY_DB_OWNER_URL, min_size=1, max_size=4, command_timeout=30.0
        )
        # The live-activity feed reads cdc_activity. On a summary volume created
        # before that table existed, the init schema never re-ran, so the owner
        # ensures it here (idempotent) — otherwise /org/activity would 500.
        await pools.summary_owner.execute(
            "CREATE TABLE IF NOT EXISTS cdc_activity ("
            " id BIGSERIAL PRIMARY KEY, tenant_id INTEGER NOT NULL, op VARCHAR(8) NOT NULL,"
            " tbl VARCHAR(32) NOT NULL, order_id BIGINT, lsn BIGINT NOT NULL,"
            " commit_ts TIMESTAMPTZ, applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW())"
        )
        await pools.summary_owner.execute(
            "CREATE INDEX IF NOT EXISTS idx_cdc_activity_at ON cdc_activity (applied_at DESC)"
        )
        # Platform-wide reads filter on the time bucket across ALL tenants; the
        # primary keys lead with tenant_id, so without these they scan every row.
        # Idempotent, and also in db/summary/005_org_indexes.sql for fresh volumes.
        for tbl, col in (("agg_orders_daily", "day_bucket"), ("agg_orders_hourly", "hour_bucket"),
                         ("agg_orders_minutely", "minute_bucket")):
            await pools.summary_owner.execute(
                f"CREATE INDEX IF NOT EXISTS idx_{tbl}_bucket ON {tbl} ({col}) "
                f"INCLUDE (tenant_id, order_count, gross_sales_cents, net_sales_cents)"
            )


def all_source_pools() -> list[asyncpg.Pool]:
    """Every distinct source-DB pool (one for the shared topology, one per tenant DB otherwise)."""
    return list(pools.source_pools.values()) or ([pools.primary] if pools.primary else [])


def primary_for_tenant(tenant_id: int) -> asyncpg.Pool:
    """Resolve the source-DB pool that holds this tenant's base rows."""
    s = source_for_tenant(pools.sources, tenant_id)
    return pools.source_pools.get(s.dsn, pools.primary)


def slot_for_tenant(tenant_id: int) -> str:
    """Resolve the CDC slot (and thus the freshness checkpoint) for this tenant."""
    return source_for_tenant(pools.sources, tenant_id).slot


async def disconnect() -> None:
    closed = set()
    for p in (pools.summary, pools.summary_owner, *pools.source_pools.values()):
        if p and id(p) not in closed:
            closed.add(id(p))
            await p.close()
