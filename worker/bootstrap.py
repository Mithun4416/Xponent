#!/usr/bin/env python3
"""Bootstrap the summary store from the primary at a consistent LSN (task T3).

Decision D4 (seed-quiesced-then-stream): run this with NO writes flowing and the
CDC worker stopped. Order of operations guarantees a race-free handoff:

  1. (Re)create the logical replication slot on the primary and capture L0, the
     exact LSN from which the worker will later stream. On a quiesced DB the
     committed state equals the state at L0.
  2. Recompute every rollup (daily / hourly / minutely order buckets and daily
     product sums) with set-based GROUP BY on the primary, streaming each result
     straight into the summary store via COPY.
  3. Rebuild per-(tenant, day) verification digests with the canonical row hash
     (worker/hashing.py) so B-2 has ground truth to verify against.
  4. Record L0 in cdc_checkpoint. The worker then streams from L0 forward.

Fresh + idempotent: truncates the summary rollups first, so re-running is safe.

Usage:
    python3 -m worker.bootstrap \
        --primary-dsn postgresql://postgres:primary_password@localhost:5432/primary_db \
        --summary-dsn postgresql://agg_user:agg_password@localhost:5433/agg_db \
        --slot agg_slot
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone

import psycopg

from common.registry import load_sources
from worker.hashing import MOD, row_hash64

STATUSES = ("placed", "shipped", "cancelled")


# --- LSN helpers -------------------------------------------------------------
def lsn_str_to_int(lsn: str) -> int:
    """'16/B374A048' -> 64-bit integer (hi << 32 | lo)."""
    hi, lo = lsn.split("/")
    return (int(hi, 16) << 32) | int(lo, 16)


# --- Slot management ---------------------------------------------------------
def recreate_slot(primary: psycopg.Connection, slot: str, force: bool) -> str:
    with primary.cursor() as cur:
        cur.execute("SELECT active FROM pg_replication_slots WHERE slot_name = %s", (slot,))
        row = cur.fetchone()
        if row is not None:
            if row[0] and not force:
                raise SystemExit(
                    f"Slot '{slot}' is ACTIVE. Stop the CDC worker before bootstrapping "
                    f"(decision D4), or pass --force."
                )
            cur.execute("SELECT pg_drop_replication_slot(%s)", (slot,))
        cur.execute(
            "SELECT lsn FROM pg_create_logical_replication_slot(%s, 'wal2json')", (slot,)
        )
        lsn = cur.fetchone()[0]  # psycopg returns pg_lsn as str
    primary.commit()
    print(f"[bootstrap] slot '{slot}' created at L0 = {lsn}")
    return str(lsn)


# --- Rollup seeding (GROUP BY on primary -> COPY into summary) ----------------
def _copy_from_select(primary, summary, select_sql: str, copy_sql: str, label: str) -> int:
    n = 0
    with primary.cursor(name=f"cur_{label}") as pc:
        pc.itersize = 10000
        pc.execute(select_sql)
        with summary.cursor().copy(copy_sql) as cp:
            for rec in pc:
                cp.write_row(rec)
                n += 1
    print(f"[bootstrap] {label}: {n:,} rows")
    return n


def truncate_summary(summary: psycopg.Connection) -> None:
    with summary.cursor() as cur:
        cur.execute(
            "TRUNCATE agg_orders_daily, agg_orders_hourly, agg_orders_minutely, "
            "agg_products_daily, bucket_digests"
        )
    summary.commit()


def seed_rollups(primary: psycopg.Connection, summary: psycopg.Connection, truncate: bool = True) -> None:
    if truncate:
        truncate_summary(summary)
    # net_* excludes cancelled; gross_* includes all statuses (decision D2).
    net_case = "SUM(CASE WHEN status <> 'cancelled' THEN total_amount_cents ELSE 0 END)"

    _copy_from_select(
        primary, summary,
        f"""SELECT tenant_id, (created_at AT TIME ZONE 'UTC')::date AS day_bucket, status,
                   COUNT(*), SUM(total_amount_cents), {net_case}
            FROM orders GROUP BY tenant_id, day_bucket, status""",
        "COPY agg_orders_daily (tenant_id, day_bucket, status, order_count, gross_sales_cents, net_sales_cents) FROM STDIN",
        "agg_orders_daily",
    )
    _copy_from_select(
        primary, summary,
        f"""SELECT tenant_id, date_trunc('hour', created_at AT TIME ZONE 'UTC') AT TIME ZONE 'UTC' AS hb, status,
                   COUNT(*), SUM(total_amount_cents), {net_case}
            FROM orders GROUP BY tenant_id, hb, status""",
        "COPY agg_orders_hourly (tenant_id, hour_bucket, status, order_count, gross_sales_cents, net_sales_cents) FROM STDIN",
        "agg_orders_hourly",
    )
    _copy_from_select(
        primary, summary,
        f"""SELECT tenant_id, date_trunc('minute', created_at AT TIME ZONE 'UTC') AT TIME ZONE 'UTC' AS mb, status,
                   COUNT(*), SUM(total_amount_cents), {net_case}
            FROM orders GROUP BY tenant_id, mb, status""",
        "COPY agg_orders_minutely (tenant_id, minute_bucket, status, order_count, gross_sales_cents, net_sales_cents) FROM STDIN",
        "agg_orders_minutely",
    )
    # Products: gross = all; net = non-cancelled orders only (join order status).
    _copy_from_select(
        primary, summary,
        """SELECT oi.tenant_id, (oi.order_created_at AT TIME ZONE 'UTC')::date AS day_bucket, oi.product_id,
                  COALESCE(MAX(p.product_name), ''), COUNT(DISTINCT oi.order_id), SUM(oi.quantity),
                  SUM(oi.line_amount_cents),
                  SUM(CASE WHEN o.status <> 'cancelled' THEN oi.line_amount_cents ELSE 0 END)
           FROM order_items oi
           JOIN orders o ON o.order_id = oi.order_id
           LEFT JOIN products p ON p.product_id = oi.product_id
           GROUP BY oi.tenant_id, day_bucket, oi.product_id""",
        "COPY agg_products_daily (tenant_id, day_bucket, product_id, product_name, order_count, qty_sold, gross_revenue_cents, net_revenue_cents) FROM STDIN",
        "agg_products_daily",
    )
    summary.commit()


# --- Digest seeding (canonical hash; streamed in (tenant, day) order) ---------
def seed_digests(primary: psycopg.Connection, summary: psycopg.Connection, l0_int: int) -> int:
    buckets = 0
    cur_key = None
    row_count = 0
    sum_amt = 0
    hash_sum = 0

    def flush(copy, key):
        nonlocal buckets
        t, d = key
        copy.write_row((t, d, row_count, sum_amt, hash_sum, l0_int))
        buckets += 1

    with primary.cursor(name="cur_digest") as pc:
        pc.itersize = 50000
        pc.execute(
            "SELECT tenant_id, (created_at AT TIME ZONE 'UTC')::date AS day_bucket, "
            "order_id, status, total_amount_cents "
            "FROM orders ORDER BY tenant_id, day_bucket"
        )
        with summary.cursor().copy(
            "COPY bucket_digests (tenant_id, day_bucket, row_count, sum_amount_cents, hash_sum_mod64, updated_lsn) FROM STDIN"
        ) as cp:
            for tenant_id, day_bucket, order_id, status, amount in pc:
                key = (tenant_id, day_bucket)
                if cur_key is not None and key != cur_key:
                    flush(cp, cur_key)
                    row_count, sum_amt, hash_sum = 0, 0, 0
                cur_key = key
                row_count += 1
                sum_amt += amount
                hash_sum = (hash_sum + row_hash64(order_id, status, amount)) % MOD
            if cur_key is not None:
                flush(cp, cur_key)
    summary.commit()
    print(f"[bootstrap] bucket_digests: {buckets:,} (tenant, day) buckets")
    return buckets


def write_checkpoint(summary: psycopg.Connection, slot: str, l0_str: str, l0_int: int) -> None:
    with summary.cursor() as cur:
        cur.execute(
            """INSERT INTO cdc_checkpoint (slot_name, last_applied_lsn, last_applied_lsn_str, as_of_timestamp, updated_at)
               VALUES (%s, %s, %s, NOW(), NOW())
               ON CONFLICT (slot_name) DO UPDATE SET
                   last_applied_lsn = EXCLUDED.last_applied_lsn,
                   last_applied_lsn_str = EXCLUDED.last_applied_lsn_str,
                   as_of_timestamp = EXCLUDED.as_of_timestamp,
                   updated_at = NOW()""",
            (slot, l0_int, l0_str),
        )
    summary.commit()
    print(f"[bootstrap] checkpoint set: slot={slot} last_applied_lsn={l0_int} ({l0_str})")


def _bootstrap_source(summary, dsn: str, slot: str, label: str, force: bool) -> str:
    with psycopg.connect(dsn, autocommit=False) as primary:
        l0_str = recreate_slot(primary, slot, force)
        l0_int = lsn_str_to_int(l0_str)
        seed_rollups(primary, summary, truncate=False)   # summary truncated once by caller
        seed_digests(primary, summary, l0_int)
        write_checkpoint(summary, slot, l0_str, l0_int)
    print(f"[bootstrap:{label}] seeded; slot={slot} L0={l0_str}")
    return l0_str


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Xponent summary-store bootstrap at L0 (one slot per tenant source)")
    ap.add_argument("--summary-dsn", required=True, help="summary DB (owner; bypasses RLS to write all tenants)")
    ap.add_argument("--primary-dsn", help="single shared source DSN (used when TENANT_SOURCES is unset)")
    ap.add_argument("--slot", default="agg_slot", help="slot name for the single shared source")
    ap.add_argument("--force", action="store_true", help="drop ACTIVE slots (stop the worker first)")
    a = ap.parse_args(argv)

    default_dsn = a.primary_dsn or os.getenv("PRIMARY_DB_URL", "")
    sources = load_sources(default_dsn, a.slot)
    if any(not s.dsn for s in sources):
        raise SystemExit("No source DSN: pass --primary-dsn or set PRIMARY_DB_URL / TENANT_SOURCES.")

    started = datetime.now(timezone.utc)
    with psycopg.connect(a.summary_dsn, autocommit=False) as summary:
        truncate_summary(summary)   # wipe derived summary once, then seed every source
        print(f"[bootstrap] {len(sources)} source(s): " + ", ".join(f"{s.label}:{s.slot}" for s in sources))
        for s in sources:
            _bootstrap_source(summary, s.dsn, s.slot, s.label, a.force)

    print(f"[bootstrap] done in {(datetime.now(timezone.utc) - started).total_seconds():.1f}s "
          f"across {len(sources)} source(s). Worker may now stream.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
