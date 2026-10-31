#!/usr/bin/env python3
"""Continuous high-throughput order generator (live ingest demo).

Streams NEW orders into the tenant SOURCE databases at a sustained target rate
(default 500 orders/sec) so you can show Xponent absorbing a real write load:
the orders land in the source OLTP DBs, flow through CDC into the rollups, and
the dashboard's numbers climb while the freshness badge stays green. This is the
"we handle data efficiently, not one row at a time" story — made visible.

It is SEPARATE from the deterministic seeder (`seed_dataset.py`, which builds the
fixed correctness oracle) and from `apply_mutations.py` (which replays a scripted
cancel/ship/amount stream for freshness tests). This one just floods inserts.

Topology: reads the same per-tenant source map as the app (TENANT_SOURCES env or
--sources), or a single --dsn/--tenant. Orders use realistic amounts drawn from
each tenant's own products, a small cancelled fraction, and monotonic ids above
the current MAX (ON CONFLICT DO NOTHING guards against any concurrent manual
insert from the dashboard).

Examples:
    # 500 orders/sec across the per-tenant DBs for 60s, using the app's map:
    TENANT_SOURCES='[{"tenant_id":1,"dsn":"postgresql://postgres:primary_password@localhost:5432/tenant_1"},
                     {"tenant_id":2,"dsn":"postgresql://postgres:primary_password@localhost:5432/tenant_2"}]' \
      python3 scripts/load_generator.py --rate 500 --duration 60

    # single shared DB, two tenants, run until Ctrl-C:
    python3 scripts/load_generator.py \
      --dsn postgresql://postgres:primary_password@localhost:5432/primary_db --tenants 1,2 --rate 300
"""
from __future__ import annotations

import argparse
import json
import os
import random
import signal
import sys
import time
from datetime import datetime, timezone

import psycopg

STATUS_WEIGHTS = [("placed", 0.80), ("shipped", 0.14), ("cancelled", 0.06)]


def _pick_status(rng: random.Random) -> str:
    x = rng.random()
    acc = 0.0
    for s, w in STATUS_WEIGHTS:
        acc += w
        if x <= acc:
            return s
    return "placed"


def _parse_args(argv):
    ap = argparse.ArgumentParser(description="Xponent live load generator")
    ap.add_argument("--sources", default=os.getenv("TENANT_SOURCES", ""),
                    help="JSON list [{tenant_id,dsn}]; defaults to the TENANT_SOURCES env var")
    ap.add_argument("--dsn", help="single source DSN (used when --sources is empty)")
    ap.add_argument("--tenants", default="1,2", help="comma tenant ids for the single-DSN case")
    ap.add_argument("--rate", type=float, default=500.0, help="target orders/sec (default 500)")
    ap.add_argument("--duration", type=float, default=60.0, help="seconds to run (0 = until Ctrl-C)")
    ap.add_argument("--batch", type=int, default=250, help="orders per flush (default 250)")
    ap.add_argument("--skew", type=float, default=1.16, help="Pareto alpha for tenant skew (whales)")
    ap.add_argument("--seed", type=int, default=7)
    return ap.parse_args(argv)


def _resolve_sources(a) -> list[dict]:
    if a.sources:
        return [{"tenant_id": int(s["tenant_id"]), "dsn": s["dsn"]} for s in json.loads(a.sources)]
    if not a.dsn:
        print("error: provide --sources / TENANT_SOURCES, or --dsn + --tenants", file=sys.stderr)
        sys.exit(2)
    return [{"tenant_id": int(t), "dsn": a.dsn} for t in a.tenants.split(",") if t.strip()]


def _pareto_weights(n, alpha):
    w = [(i + 1) ** (-alpha) for i in range(n)]
    s = sum(w)
    return [x / s for x in w]


class DbState:
    """One source database: connection, id counters, per-tenant candidate rows."""

    def __init__(self, dsn: str):
        self.dsn = dsn
        self.conn = psycopg.connect(dsn, autocommit=False)
        with self.conn.cursor() as cur:
            cur.execute("SELECT COALESCE(MAX(order_id),0) FROM orders")
            self.order_id = int(cur.fetchone()[0]) + 1
            cur.execute("SELECT COALESCE(MAX(order_item_id),0) FROM order_items")
            self.item_id = int(cur.fetchone()[0]) + 1
        self.conn.commit()
        self.candidates: dict[int, dict] = {}  # tenant_id -> {customers:[...], products:[(pid,price)]}

    def load_tenant(self, tenant_id: int):
        if tenant_id in self.candidates:
            return
        with self.conn.cursor() as cur:
            cur.execute("SELECT customer_id FROM customers WHERE tenant_id=%s LIMIT 500", (tenant_id,))
            customers = [r[0] for r in cur.fetchall()]
            cur.execute("SELECT product_id, unit_price_cents FROM products WHERE tenant_id=%s LIMIT 500", (tenant_id,))
            products = [(r[0], int(r[1])) for r in cur.fetchall()]
        self.conn.commit()
        if not customers or not products:
            raise SystemExit(f"tenant {tenant_id} has no customers/products in {self.dsn} — seed it first.")
        self.candidates[tenant_id] = {"customers": customers, "products": products}


def main(argv) -> int:
    a = _parse_args(argv)
    rng = random.Random(a.seed)
    sources = _resolve_sources(a)

    # Open one DbState per distinct DSN; map tenant -> its DbState.
    dbs: dict[str, DbState] = {}
    tenant_db: dict[int, DbState] = {}
    tenant_ids: list[int] = []
    for s in sources:
        st = dbs.get(s["dsn"]) or dbs.setdefault(s["dsn"], DbState(s["dsn"]))
        st.load_tenant(s["tenant_id"])
        tenant_db[s["tenant_id"]] = st
        tenant_ids.append(s["tenant_id"])
    tenant_ids = sorted(set(tenant_ids))
    weights = _pareto_weights(len(tenant_ids), a.skew)
    print(f"[load] {len(tenant_ids)} tenant(s) across {len(dbs)} database(s); "
          f"target {a.rate:.0f} orders/sec, batch {a.batch}", flush=True)

    running = {"on": True}
    signal.signal(signal.SIGINT, lambda *_: running.update(on=False))

    total = 0
    start = time.perf_counter()
    last_report = start
    report_count = 0
    interval = max(0.02, a.batch / a.rate)  # seconds per batch to hit the target rate

    while running["on"]:
        t0 = time.perf_counter()
        if a.duration and (t0 - start) >= a.duration:
            break
        # Build this batch: assign each order to a tenant by skew weight.
        per_db: dict[DbState, list[tuple]] = {}
        now = datetime.now(timezone.utc)
        for _ in range(a.batch):
            tid = rng.choices(tenant_ids, weights=weights, k=1)[0]
            st = tenant_db[tid]
            cand = st.candidates[tid]
            cust = rng.choice(cand["customers"])
            pid, price = rng.choice(cand["products"])
            qty = rng.randint(1, 3)
            amount = price * qty
            status = _pick_status(rng)
            oid, iid = st.order_id, st.item_id
            st.order_id += 1
            st.item_id += 1
            per_db.setdefault(st, []).append((oid, iid, tid, cust, pid, qty, amount, status, now))

        inserted = 0
        for st, rows in per_db.items():
            try:
                with st.conn.cursor() as cur:
                    cur.executemany(
                        "INSERT INTO orders (order_id, tenant_id, customer_id, status, total_amount_cents, created_at, updated_at) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (order_id) DO NOTHING",
                        [(o, t, c, stt, amt, ts, ts) for (o, i, t, c, p, q, amt, stt, ts) in rows])
                    cur.executemany(
                        "INSERT INTO order_items (order_item_id, order_id, tenant_id, product_id, quantity, line_amount_cents, order_created_at) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (order_item_id) DO NOTHING",
                        [(i, o, t, p, q, amt, ts) for (o, i, t, c, p, q, amt, stt, ts) in rows])
                st.conn.commit()
                inserted += len(rows)
            except Exception as e:
                st.conn.rollback()
                print(f"[load] batch error on {st.dsn}: {e}", flush=True)
        total += inserted

        # Live throughput report every ~1s.
        report_count += inserted
        nowp = time.perf_counter()
        if nowp - last_report >= 1.0:
            rate = report_count / (nowp - last_report)
            print(f"[load] +{report_count} orders  ({rate:.0f}/sec)  total={total}  elapsed={nowp-start:.0f}s", flush=True)
            last_report, report_count = nowp, 0

        # Pace to the target rate.
        sleep = interval - (time.perf_counter() - t0)
        if sleep > 0:
            time.sleep(sleep)

    dur = time.perf_counter() - start
    print(f"[load] done: {total} orders in {dur:.1f}s ({total/dur:.0f}/sec avg)", flush=True)
    for st in dbs.values():
        st.conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
