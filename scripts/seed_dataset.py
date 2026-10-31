#!/usr/bin/env python3
"""Deterministic synthetic seeder for the Xponent primary DB (task T2).

Streams tenants, products, customers, orders, and order_items into the primary
database via COPY. Memory is O(1): orders and their items are re-derived from
(seed, order_id) in two passes, so orders.total_amount_cents always equals the
SUM of the order's order_items.line_amount_cents by construction.

Seed-quiesced-then-stream (decision D4): run this BEFORE starting the CDC
worker, so bootstrap reads a static dataset.

Usage:
    python3 scripts/seed_dataset.py --rows 1000000 --tenants 10 --seed 42 \
        --dsn postgresql://postgres:primary_password@localhost:5432/primary_db

This writes a run manifest (seed, params, counts) next to --manifest so results
are reproducible. These are DATA VOLUMES, not performance measurements.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone

import psycopg

from synthetic import (
    GenParams,
    customer_block_start,
    gen_order,
    pareto_weights,
    product_block_start,
    product_name,
    product_sku,
    product_unit_price_cents,
    _cumulative,
)


def _git_sha() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
        return out.stdout.strip() or "n/a"
    except Exception:
        return "n/a"


def _parse_args(argv: list[str]) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Deterministic Xponent synthetic seeder")
    ap.add_argument("--rows", type=int, required=True, help="total order count")
    ap.add_argument("--tenants", type=int, required=True)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--span-days", type=int, default=120)
    ap.add_argument("--products-per-tenant", type=int, default=1000)
    ap.add_argument("--customers-per-tenant", type=int, default=5000)
    ap.add_argument("--items-per-order-max", type=int, default=5)
    ap.add_argument("--cancel-frac", type=float, default=0.06)
    ap.add_argument("--skew-alpha", type=float, default=1.16)
    ap.add_argument("--no-sub-hour-density", action="store_true",
                    help="disable sub-minute timestamp jitter (weakens non-aligned tests)")
    ap.add_argument("--dsn", required=True, help="primary DB connection string (superuser)")
    ap.add_argument("--assign-tenant", type=int, default=None,
                    help="per-tenant-DB mode: write ALL rows with this tenant_id (use with --tenants 1)")
    ap.add_argument("--batch", type=int, default=50000, help="progress print interval")
    ap.add_argument("--manifest", default="results/seed_manifest.json")
    return ap.parse_args(argv)


def _seed_tenants_products_customers(cur, p: GenParams, assign: int | None) -> None:
    print(f"[seed] tenants={p.tenants} products={p.tenants * p.products_per_tenant} "
          f"customers={p.tenants * p.customers_per_tenant}"
          + (f" (all rows tenant_id={assign})" if assign is not None else ""))

    with cur.copy("COPY tenants (tenant_id, store_name, plan_tier) FROM STDIN") as cp:
        for t in range(1, p.tenants + 1):
            tier = "premium" if t <= max(1, p.tenants // 20) else "standard"
            wt = assign if assign is not None else t
            cp.write_row((wt, f"Store {wt}", tier))

    with cur.copy("COPY products (product_id, tenant_id, sku, product_name, unit_price_cents) FROM STDIN") as cp:
        for t in range(1, p.tenants + 1):
            base = product_block_start(t, p)
            wt = assign if assign is not None else t
            for i in range(p.products_per_tenant):
                pid = base + i
                cp.write_row((pid, wt, product_sku(wt, i), product_name(pid), product_unit_price_cents(pid)))

    with cur.copy("COPY customers (customer_id, tenant_id, email_hash) FROM STDIN") as cp:
        for t in range(1, p.tenants + 1):
            base = customer_block_start(t, p)
            wt = assign if assign is not None else t
            for i in range(p.customers_per_tenant):
                cid = base + i
                cp.write_row((cid, wt, f"hash-{cid:012d}"))


def _seed_orders_and_items(cur, p: GenParams, batch: int, assign: int | None) -> int:
    cum = _cumulative(pareto_weights(p.tenants, p.skew_alpha))

    print(f"[seed] orders={p.rows} (pass 1: orders)")
    with cur.copy(
        "COPY orders (order_id, tenant_id, customer_id, status, total_amount_cents, created_at, updated_at) FROM STDIN"
    ) as cp:
        for oid in range(1, p.rows + 1):
            o = gen_order(oid, p, cum)
            wt = assign if assign is not None else o.tenant_id
            cp.write_row((o.order_id, wt, o.customer_id, o.status,
                          o.total_amount_cents, o.created_at, o.created_at))
            if oid % batch == 0:
                print(f"       ...{oid:,} orders")

    print("[seed] order_items (pass 2: re-derive items — totals match by construction)")
    item_id = 0
    with cur.copy(
        "COPY order_items (order_item_id, order_id, tenant_id, product_id, quantity, line_amount_cents, order_created_at) FROM STDIN"
    ) as cp:
        for oid in range(1, p.rows + 1):
            o = gen_order(oid, p, cum)
            wt = assign if assign is not None else o.tenant_id
            for it in o.items:
                item_id += 1
                cp.write_row((item_id, o.order_id, wt, it.product_id,
                              it.quantity, it.line_amount_cents, o.created_at))
            if oid % batch == 0:
                print(f"       ...{oid:,} orders' items ({item_id:,} items)")
    return item_id


def main(argv: list[str]) -> int:
    a = _parse_args(argv)
    p = GenParams(
        rows=a.rows, tenants=a.tenants, seed=a.seed, span_days=a.span_days,
        products_per_tenant=a.products_per_tenant, customers_per_tenant=a.customers_per_tenant,
        items_per_order_max=a.items_per_order_max, cancel_frac=a.cancel_frac,
        skew_alpha=a.skew_alpha, sub_hour_density=not a.no_sub_hour_density,
    )
    started = datetime.now(timezone.utc)
    with psycopg.connect(a.dsn, autocommit=False) as conn:
        with conn.cursor() as cur:
            _seed_tenants_products_customers(cur, p, a.assign_tenant)
            n_items = _seed_orders_and_items(cur, p, a.batch, a.assign_tenant)
        conn.commit()

    manifest = {
        "generator": "seed_dataset.py",
        "git_sha": _git_sha(),
        "started_utc": started.isoformat(),
        "finished_utc": datetime.now(timezone.utc).isoformat(),
        "params": {
            "rows": p.rows, "tenants": p.tenants, "seed": p.seed, "span_days": p.span_days,
            "products_per_tenant": p.products_per_tenant, "customers_per_tenant": p.customers_per_tenant,
            "items_per_order_max": p.items_per_order_max, "cancel_frac": p.cancel_frac,
            "skew_alpha": p.skew_alpha, "sub_hour_density": p.sub_hour_density,
            "start": p.start.isoformat(),
        },
        "counts": {
            "tenants": p.tenants,
            "products": p.tenants * p.products_per_tenant,
            "customers": p.tenants * p.customers_per_tenant,
            "orders": p.rows,
            "order_items": n_items,
        },
        "note": "Data volumes only. NOT performance results.",
    }
    os.makedirs(os.path.dirname(a.manifest) or ".", exist_ok=True)
    with open(a.manifest, "w") as f:
        json.dump(manifest, f, indent=2)
    print(f"[seed] done. {p.rows:,} orders, {n_items:,} items. Manifest -> {a.manifest}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
