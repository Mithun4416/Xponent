"""Receipts (B-2): verify a (tenant, day) bucket against source, and targeted repair.

Verify recomputes the source digest over the bounded (tenant, day) slice and
compares it to the incrementally-maintained `bucket_digests`. Repair recomputes
that one bucket's rollups + digest from the same bounded slice and atomically
overwrites only those rows — reading a tiny fraction of the rows a full rebuild
would, and never touching other tenants/days or the OLTP write path.

Writes use the summary OWNER pool (superuser; bypasses RLS to touch all tenants);
source reads use the PRIMARY pool (bounded, index-only on idx_orders_tenant_created).
"""
from __future__ import annotations

import time
from datetime import date, datetime, timedelta, timezone

import asyncpg

from common.hashing import digest_of_rows
from common.rollups import recompute_order_grains


def _day_bounds(day: date) -> tuple[datetime, datetime]:
    start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    return start, start + timedelta(days=1)


async def _source_rows(pool_for, tenant_id: int, day: date):
    start, end = _day_bounds(day)
    # Read from THIS tenant's source database (per-tenant-DB topology).
    return await pool_for(tenant_id).fetch(
        "SELECT order_id, status, total_amount_cents, created_at FROM orders "
        "WHERE tenant_id=$1 AND created_at >= $2 AND created_at < $3",
        tenant_id, start, end,
    )


async def verify_bucket(owner_pool, pool_for, tenant_id: int, day: date) -> dict:
    rows = await _source_rows(pool_for, tenant_id, day)
    count, total, hsum = digest_of_rows((r["order_id"], r["status"], r["total_amount_cents"]) for r in rows)

    summary = await owner_pool.fetchrow(
        "SELECT row_count, sum_amount_cents, hash_sum_mod64 FROM bucket_digests WHERE tenant_id=$1 AND day_bucket=$2",
        tenant_id, day,
    )
    if summary is None:
        match = count == 0
    else:
        match = (int(summary["row_count"]) == count
                 and int(summary["sum_amount_cents"]) == total
                 and int(summary["hash_sum_mod64"]) == hsum)
        await owner_pool.execute(
            "UPDATE bucket_digests SET is_corrupted = $3, verified_at = NOW() WHERE tenant_id=$1 AND day_bucket=$2",
            tenant_id, day, not match,
        )
    return {
        "tenant_id": tenant_id, "day_bucket": day.isoformat(), "match": match,
        "source": {"row_count": count, "sum_amount_cents": total, "hash_sum_mod64": str(hsum)},
        "summary": None if summary is None else {
            "row_count": int(summary["row_count"]), "sum_amount_cents": int(summary["sum_amount_cents"]),
            "hash_sum_mod64": str(int(summary["hash_sum_mod64"])),
        },
    }


async def verify_sweep(owner_pool, pool_for, tenant_id: int | None, sample: int) -> dict:
    where = "WHERE tenant_id=$1" if tenant_id is not None else ""
    args = (tenant_id,) if tenant_id is not None else ()
    buckets = await owner_pool.fetch(
        f"SELECT tenant_id, day_bucket FROM bucket_digests {where} ORDER BY random() LIMIT {int(sample)}",
        *args,
    )
    checked, mismatches = 0, []
    for b in buckets:
        res = await verify_bucket(owner_pool, pool_for, b["tenant_id"], b["day_bucket"])
        checked += 1
        if not res["match"]:
            mismatches.append({"tenant_id": b["tenant_id"], "day_bucket": b["day_bucket"].isoformat()})
    return {"checked": checked, "mismatched": len(mismatches), "buckets": mismatches}


async def repair_bucket(owner_pool, pool_for, tenant_id: int, day: date) -> dict:
    t0 = time.perf_counter()
    start, end = _day_bounds(day)
    primary_pool = pool_for(tenant_id)   # this tenant's source database
    rows = await _source_rows(pool_for, tenant_id, day)
    rb = recompute_order_grains((r["order_id"], r["status"], r["total_amount_cents"], r["created_at"]) for r in rows)
    rows_scanned = rb.row_count

    products = await primary_pool.fetch(
        "SELECT oi.product_id, MAX(p.product_name) name, COUNT(DISTINCT oi.order_id)::bigint oc, "
        "SUM(oi.quantity)::bigint q, SUM(oi.line_amount_cents)::bigint g, "
        "SUM(CASE WHEN o.status <> 'cancelled' THEN oi.line_amount_cents ELSE 0 END)::bigint n "
        "FROM order_items oi JOIN orders o ON o.order_id = oi.order_id "
        "LEFT JOIN products p ON p.product_id = oi.product_id "
        "WHERE oi.tenant_id=$1 AND oi.order_created_at >= $2 AND oi.order_created_at < $3 "
        "GROUP BY oi.product_id",
        tenant_id, start, end,
    )

    old = await owner_pool.fetchrow(
        "SELECT hash_sum_mod64, updated_lsn FROM bucket_digests WHERE tenant_id=$1 AND day_bucket=$2",
        tenant_id, day,
    )
    old_hash = str(int(old["hash_sum_mod64"])) if old else None
    keep_lsn = int(old["updated_lsn"]) if old else 0

    async with owner_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("DELETE FROM agg_orders_daily WHERE tenant_id=$1 AND day_bucket=$2", tenant_id, day)
            await conn.execute("DELETE FROM agg_orders_hourly WHERE tenant_id=$1 AND hour_bucket >= $2 AND hour_bucket < $3", tenant_id, start, end)
            await conn.execute("DELETE FROM agg_orders_minutely WHERE tenant_id=$1 AND minute_bucket >= $2 AND minute_bucket < $3", tenant_id, start, end)
            await conn.execute("DELETE FROM agg_products_daily WHERE tenant_id=$1 AND day_bucket=$2", tenant_id, day)

            for status, (c, g, n) in rb.daily.items():
                await conn.execute(
                    "INSERT INTO agg_orders_daily (tenant_id, day_bucket, status, order_count, gross_sales_cents, net_sales_cents) "
                    "VALUES ($1,$2,$3,$4,$5,$6)", tenant_id, day, status, c, g, n)
            for (hour_ts, status), (c, g, n) in rb.hourly.items():
                await conn.execute(
                    "INSERT INTO agg_orders_hourly (tenant_id, hour_bucket, status, order_count, gross_sales_cents, net_sales_cents) "
                    "VALUES ($1,$2,$3,$4,$5,$6)", tenant_id, hour_ts, status, c, g, n)
            for (minute_ts, status), (c, g, n) in rb.minutely.items():
                await conn.execute(
                    "INSERT INTO agg_orders_minutely (tenant_id, minute_bucket, status, order_count, gross_sales_cents, net_sales_cents) "
                    "VALUES ($1,$2,$3,$4,$5,$6)", tenant_id, minute_ts, status, c, g, n)
            for p in products:
                await conn.execute(
                    "INSERT INTO agg_products_daily (tenant_id, day_bucket, product_id, product_name, order_count, qty_sold, gross_revenue_cents, net_revenue_cents) "
                    "VALUES ($1,$2,$3,$4,$5,$6,$7,$8)",
                    tenant_id, day, p["product_id"], p["name"] or "", p["oc"], p["q"], p["g"], p["n"])
            # Upsert: the digest row for (tenant, day) already exists (we only
            # deleted the rollup rows above), so overwrite it in place.
            await conn.execute(
                "INSERT INTO bucket_digests (tenant_id, day_bucket, row_count, sum_amount_cents, hash_sum_mod64, updated_lsn, verified_at, is_corrupted) "
                "VALUES ($1,$2,$3,$4,$5::numeric,$6,NOW(),FALSE) "
                "ON CONFLICT (tenant_id, day_bucket) DO UPDATE SET "
                "    row_count = EXCLUDED.row_count, "
                "    sum_amount_cents = EXCLUDED.sum_amount_cents, "
                "    hash_sum_mod64 = EXCLUDED.hash_sum_mod64, "
                "    updated_lsn = EXCLUDED.updated_lsn, "
                "    verified_at = NOW(), "
                "    is_corrupted = FALSE",
                # hash can exceed int8 (up to 2**64-1); pass as text + cast to avoid asyncpg int overflow.
                tenant_id, day, rb.row_count, rb.sum_amount_cents, str(rb.hash_sum_mod64), keep_lsn)

    full_table_rows = int(await primary_pool.fetchval("SELECT COUNT(*) FROM orders"))
    return {
        "tenant_id": tenant_id, "day_bucket": day.isoformat(), "reconciled": True,
        "rows_scanned": rows_scanned, "full_rebuild_rows": full_table_rows,
        "rows_read_fraction": (rows_scanned / full_table_rows) if full_table_rows else 0.0,
        "old_hash_sum_mod64": old_hash, "new_hash_sum_mod64": str(rb.hash_sum_mod64),
        "duration_ms": round((time.perf_counter() - t0) * 1000, 1),
    }
