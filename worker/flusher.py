"""Atomic flush of a coalesced micro-batch to the summary store (task T4).

All rollup upserts, product upserts, digest upserts, and the LSN checkpoint
advance commit in a SINGLE transaction on the summary DB. Combined with the
per-event idempotency guard (dropping events with lsn <= last_applied_lsn before
coalescing, fix H1), this gives exactly-once aggregate effects across crashes and
redeliveries: a crash mid-batch rolls back cleanly, and replay from
last_applied_lsn re-produces the same state.

Uses psycopg (v3) for the summary connection.
"""
from __future__ import annotations

from datetime import datetime, timezone

import psycopg

from worker.coalescer import MicroBatchCoalescer

_GRAIN_TABLE = {
    "daily": ("agg_orders_daily", "day_bucket"),
    "hourly": ("agg_orders_hourly", "hour_bucket"),
    "minutely": ("agg_orders_minutely", "minute_bucket"),
}


def flush(summary: psycopg.Connection, slot: str, batch: MicroBatchCoalescer) -> bool:
    """Apply a batch atomically. Returns True if applied, False if a no-op/duplicate."""
    if batch.is_empty():
        return False

    with summary.transaction():
        with summary.cursor() as cur:
            cur.execute(
                "SELECT last_applied_lsn FROM cdc_checkpoint WHERE slot_name = %s FOR UPDATE",
                (slot,),
            )
            row = cur.fetchone()
            last_lsn = row[0] if row else 0
            if batch.max_lsn <= last_lsn:
                return False  # whole batch already applied (belt-and-suspenders; H1 also filters per-event)

            for grain, tenant_id, bucket, status, dcount, dgross, dnet in batch.order_rows():
                table, col = _GRAIN_TABLE[grain]
                cur.execute(
                    f"""INSERT INTO {table} (tenant_id, {col}, status, order_count, gross_sales_cents, net_sales_cents)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        ON CONFLICT (tenant_id, {col}, status) DO UPDATE SET
                            order_count = {table}.order_count + EXCLUDED.order_count,
                            gross_sales_cents = {table}.gross_sales_cents + EXCLUDED.gross_sales_cents,
                            net_sales_cents = {table}.net_sales_cents + EXCLUDED.net_sales_cents""",
                    (tenant_id, bucket, status, dcount, dgross, dnet),
                )

            for tenant_id, day, product_id, dcount, dqty, dgross, dnet in batch.product_rows():
                cur.execute(
                    """INSERT INTO agg_products_daily
                           (tenant_id, day_bucket, product_id, order_count, qty_sold, gross_revenue_cents, net_revenue_cents)
                       VALUES (%s, %s, %s, %s, %s, %s, %s)
                       ON CONFLICT (tenant_id, day_bucket, product_id) DO UPDATE SET
                           order_count = agg_products_daily.order_count + EXCLUDED.order_count,
                           qty_sold = agg_products_daily.qty_sold + EXCLUDED.qty_sold,
                           gross_revenue_cents = agg_products_daily.gross_revenue_cents + EXCLUDED.gross_revenue_cents,
                           net_revenue_cents = agg_products_daily.net_revenue_cents + EXCLUDED.net_revenue_cents""",
                    (tenant_id, day, product_id, dcount, dqty, dgross, dnet),
                )

            for tenant_id, day, dcount, dsum, dhash in batch.digest_rows():
                # hash_sum_mod64 kept in [0, 2^64) via modular add of the signed delta.
                cur.execute(
                    """INSERT INTO bucket_digests
                           (tenant_id, day_bucket, row_count, sum_amount_cents, hash_sum_mod64, updated_lsn)
                       VALUES (%s, %s, %s, %s, %s, %s)
                       ON CONFLICT (tenant_id, day_bucket) DO UPDATE SET
                           row_count = bucket_digests.row_count + EXCLUDED.row_count,
                           sum_amount_cents = bucket_digests.sum_amount_cents + EXCLUDED.sum_amount_cents,
                           hash_sum_mod64 = ((bucket_digests.hash_sum_mod64 + EXCLUDED.hash_sum_mod64) %% (2::numeric ^ 64))::numeric(20,0),
                           updated_lsn = GREATEST(bucket_digests.updated_lsn, EXCLUDED.updated_lsn)""",
                    (tenant_id, day, dcount, dsum, dhash, batch.max_lsn),
                )

            # Live activity feed (per event), transactionally consistent with the rollups.
            for tenant_id, op, tbl, order_id, lsn, cts in batch.activity:
                cur.execute(
                    "INSERT INTO cdc_activity (tenant_id, op, tbl, order_id, lsn, commit_ts) "
                    "VALUES (%s, %s, %s, %s, %s, %s)",
                    (tenant_id, op, tbl, order_id, lsn, cts),
                )

            lsn_hi, lsn_lo = batch.max_lsn >> 32, batch.max_lsn & 0xFFFFFFFF
            as_of = batch.latest_commit_ts or datetime.now(timezone.utc)
            cur.execute(
                """INSERT INTO cdc_checkpoint (slot_name, last_applied_lsn, last_applied_lsn_str, as_of_timestamp, updated_at)
                   VALUES (%s, %s, %s, %s, NOW())
                   ON CONFLICT (slot_name) DO UPDATE SET
                       last_applied_lsn = EXCLUDED.last_applied_lsn,
                       last_applied_lsn_str = EXCLUDED.last_applied_lsn_str,
                       as_of_timestamp = EXCLUDED.as_of_timestamp,
                       updated_at = NOW()""",
                (slot, batch.max_lsn, f"{lsn_hi:X}/{lsn_lo:X}", as_of),
            )
    return True


def heartbeat(summary: psycopg.Connection, slot: str, as_of: datetime) -> None:
    """Advance only the freshness watermark when the stream is caught up and idle (fix H5).

    Keeps lag_ms near zero during a quiet demo instead of letting it inflate just
    because no new orders have arrived. Never moves last_applied_lsn.
    """
    with summary.cursor() as cur:
        cur.execute(
            "UPDATE cdc_checkpoint SET as_of_timestamp = %s, updated_at = NOW() WHERE slot_name = %s",
            (as_of, slot),
        )
    summary.commit()
