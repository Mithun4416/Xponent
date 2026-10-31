#!/usr/bin/env python3
"""Corrupt ONE (tenant, day) summary bucket to demonstrate Receipts detect+heal (B-2).

Writes ONLY to the summary store (never the primary). It perturbs the rollup and
leaves the digest stale so verification flags a mismatch — exactly the drift the
targeted repair then heals. Connect as the summary OWNER (agg_user).

Usage:
    python3 scripts/inject_fault.py --tenant 1 --day 2026-07-02 \
        --dsn postgresql://agg_user:agg_password@localhost:5433/agg_db
"""
from __future__ import annotations

import argparse
import sys
from datetime import date

import psycopg


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Inject a summary-bucket fault for the Receipts demo")
    ap.add_argument("--tenant", type=int, required=True)
    ap.add_argument("--day", required=True, help="YYYY-MM-DD")
    ap.add_argument("--delta", type=int, default=7, help="phantom order_count to add")
    ap.add_argument("--dsn", required=True, help="summary OWNER connection string")
    a = ap.parse_args(argv)
    day = date.fromisoformat(a.day)

    with psycopg.connect(a.dsn, autocommit=False) as conn, conn.cursor() as cur:
        # Corrupt the served rollup so the dashboard number is visibly wrong...
        cur.execute(
            "UPDATE agg_orders_daily SET order_count = order_count + %s "
            "WHERE tenant_id=%s AND day_bucket=%s "
            "AND status=(SELECT status FROM agg_orders_daily WHERE tenant_id=%s AND day_bucket=%s LIMIT 1)",
            (a.delta, a.tenant, day, a.tenant, day),
        )
        affected = cur.rowcount
        # ...and perturb the digest so it disagrees with source (what verification checks).
        # Real drift (e.g. a missed CDC event) corrupts both consistently; this simulates it.
        cur.execute(
            "UPDATE bucket_digests SET row_count = row_count + %s "
            "WHERE tenant_id=%s AND day_bucket=%s",
            (a.delta, a.tenant, day),
        )
        conn.commit()

    if affected == 0:
        print(f"[fault] no bucket found for tenant={a.tenant} day={a.day}; pick a (tenant,day) that has orders.")
        return 1
    print(f"[fault] corrupted tenant={a.tenant} day={a.day}: added +{a.delta} phantom orders to one status. "
          f"bucket_digests left stale -> verification will flag a mismatch.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
