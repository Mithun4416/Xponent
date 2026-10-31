#!/usr/bin/env python3
"""Apply a mutation stream to the primary DB, recording commit timestamps (task T2).

Each applied row's server-side commit time is captured so freshness tests can
measure commit->visible lag against ground truth. An output log (TSV) records:

    order_id <TAB> op <TAB> applied_commit_ts_iso

Run this against the primary AFTER the CDC worker is streaming, so each change
propagates through CDC into the summary store.

Usage:
    python3 scripts/apply_mutations.py --in results/mutations.tsv \
        --dsn postgresql://postgres:primary_password@localhost:5432/primary_db \
        --log results/applied.tsv --rate 50
"""
from __future__ import annotations

import argparse
import sys
import time

import psycopg


def _parse_args(argv: list[str]) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Apply an Xponent mutation stream")
    ap.add_argument("--in", dest="infile", required=True)
    ap.add_argument("--dsn", required=True, help="primary DB connection string (write access)")
    ap.add_argument("--log", default="results/applied.tsv")
    ap.add_argument("--rate", type=float, default=0.0,
                    help="max ops/sec (0 = as fast as possible)")
    return ap.parse_args(argv)


def _apply_one(cur, parts: list[str]) -> int:
    """Apply one mutation; return the affected order_id (or 0 if skipped)."""
    op = parts[0]
    if op == "insert":
        oid, tenant_id, customer_id, status, amount, created = (
            int(parts[1]), int(parts[2]), int(parts[3]), parts[4], int(parts[5]), parts[6],
        )
        # Insert the order plus a single representative line item so product
        # rollups and the order total stay consistent for the new order.
        cur.execute(
            "INSERT INTO orders (order_id, tenant_id, customer_id, status, total_amount_cents, created_at, updated_at) "
            "VALUES (%s, %s, %s, %s, %s, %s::timestamptz, NOW()) ON CONFLICT (order_id) DO NOTHING",
            (oid, tenant_id, customer_id, status, amount, created),
        )
        cur.execute("SELECT COALESCE(MAX(product_id), %s) FROM products WHERE tenant_id = %s",
                    ((tenant_id - 1) * 1000 + 1, tenant_id))
        product_id = cur.fetchone()[0]
        cur.execute("SELECT COALESCE(MAX(order_item_id), 0) + 1 FROM order_items")
        item_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO order_items (order_item_id, order_id, tenant_id, product_id, quantity, line_amount_cents, order_created_at) "
            "VALUES (%s, %s, %s, %s, 1, %s, %s::timestamptz) ON CONFLICT (order_item_id) DO NOTHING",
            (item_id, oid, tenant_id, product_id, amount, created),
        )
        return oid
    if op == "cancel":
        oid = int(parts[1])
        cur.execute("UPDATE orders SET status='cancelled', updated_at=NOW() WHERE order_id=%s AND status<>'cancelled'", (oid,))
        return oid
    if op == "ship":
        oid = int(parts[1])
        cur.execute("UPDATE orders SET status='shipped', updated_at=NOW() WHERE order_id=%s AND status='placed'", (oid,))
        return oid
    if op == "amount":
        oid, new_amt = int(parts[1]), int(parts[2])
        cur.execute("UPDATE orders SET total_amount_cents=%s, updated_at=NOW() WHERE order_id=%s", (new_amt, oid))
        return oid
    return 0


def main(argv: list[str]) -> int:
    a = _parse_args(argv)
    min_interval = (1.0 / a.rate) if a.rate > 0 else 0.0

    with open(a.infile) as f:
        ops = [ln.rstrip("\n").split("\t") for ln in f if ln.strip()]

    import os
    os.makedirs(os.path.dirname(a.log) or ".", exist_ok=True)
    applied = 0
    with psycopg.connect(a.dsn, autocommit=False) as conn, open(a.log, "w") as log:
        for parts in ops:
            t0 = time.perf_counter()
            with conn.cursor() as cur:
                oid = _apply_one(cur, parts)
                # Capture the commit timestamp of THIS transaction for freshness ground truth.
                cur.execute("SELECT clock_timestamp()")
                commit_ts = cur.fetchone()[0]
            conn.commit()
            if oid:
                log.write(f"{oid}\t{parts[0]}\t{commit_ts.isoformat()}\n")
                applied += 1
            if min_interval:
                elapsed = time.perf_counter() - t0
                if elapsed < min_interval:
                    time.sleep(min_interval - elapsed)
    print(f"[apply] applied {applied}/{len(ops)} ops. Commit-time log -> {a.log}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
