"""Exact query execution via the shared range decomposer (task T7).

Every summary read runs inside a transaction that first binds the tenant with
`set_config('app.current_tenant_id', ..., true)`, activating RLS (fix C4). The
decomposer splits the window into day/hour/minute pieces (served from the summary
store) plus sub-minute edges (served from the primary) so FR-1 counts/totals and
FR-4 status breakdowns are EXACT for arbitrary non-aligned ranges.

Trends (FR-2) and Top-N (FR-3) are reported at their natural bucket grain
(day/hour; day for products), the standard semantics of those queries — the
window is snapped to the grain. Top-N default ranking is gross (decision D2).
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import asyncpg

from app.core import config
from app.services import metrics
from app.services.range_decomposer import decompose

_SORT_COL = {"gross_sales": "gross_revenue_cents", "net_sales": "net_revenue_cents", "qty_sold": "qty_sold"}


def _cents(v) -> float:
    # SUM(bigint) returns numeric -> Decimal in asyncpg; coerce to int first so
    # arithmetic and JSON serialization never choke on Decimal (fixes the 500).
    return round(int(v or 0) / 100.0, 2)


async def org_overview(owner_pool, frm, to) -> dict:
    """ORGANIZATION-WIDE query (the platform owner's view): aggregate across ALL
    sellers/tenants. Uses the owner pool (bypasses RLS) and groups by tenant_id.
    Bucket-grain (day/hour/minute) — sub-minute edges are omitted here since an
    owner leaderboard doesn't need per-second precision.
    """
    plan = decompose(frm, to)
    acc: dict[int, list[int]] = {}

    def add(tid, c, g, n):
        v = acc.setdefault(int(tid), [0, 0, 0])
        v[0] += int(c or 0)
        v[1] += int(g or 0)
        v[2] += int(n or 0)

    grain_queries = []
    for d0, d1 in plan.days:
        grain_queries.append((
            "SELECT tenant_id, SUM(order_count)::bigint c, SUM(gross_sales_cents)::bigint g, SUM(net_sales_cents)::bigint n "
            "FROM agg_orders_daily WHERE day_bucket BETWEEN $1 AND $2 GROUP BY tenant_id", (d0, d1)))
    for a, b in plan.hours:
        grain_queries.append((
            "SELECT tenant_id, SUM(order_count)::bigint c, SUM(gross_sales_cents)::bigint g, SUM(net_sales_cents)::bigint n "
            "FROM agg_orders_hourly WHERE hour_bucket >= $1 AND hour_bucket < $2 GROUP BY tenant_id", (a, b)))
    for a, b in plan.minutes:
        grain_queries.append((
            "SELECT tenant_id, SUM(order_count)::bigint c, SUM(gross_sales_cents)::bigint g, SUM(net_sales_cents)::bigint n "
            "FROM agg_orders_minutely WHERE minute_bucket >= $1 AND minute_bucket < $2 GROUP BY tenant_id", (a, b)))

    async with owner_pool.acquire() as conn:
        for sql, args in grain_queries:
            for r in await conn.fetch(sql, *args):
                add(r["tenant_id"], r["c"], r["g"], r["n"])
        ver = await conn.fetchrow(
            "SELECT COUNT(*)::bigint total, COUNT(*) FILTER (WHERE is_corrupted)::bigint corrupted FROM bucket_digests")

    leaderboard = sorted(
        ({"tenant_id": t, "order_count": v[0], "gross_sales": _cents(v[1]), "net_sales": _cents(v[2])}
         for t, v in acc.items()),
        key=lambda x: x["gross_sales"], reverse=True)
    totals = {
        "order_count": sum(v[0] for v in acc.values()),
        "gross_sales": _cents(sum(v[1] for v in acc.values())),
        "net_sales": _cents(sum(v[2] for v in acc.values())),
    }
    return {
        "scope": "organization",
        "currency": config.CURRENCY,
        "tenants": len(acc),
        "totals": totals,
        "leaderboard": leaderboard,
        "verification": {"total_buckets": int(ver["total"]), "corrupted_buckets": int(ver["corrupted"]),
                         "status": "VERIFIED" if int(ver["corrupted"]) == 0 else "DRIFT_DETECTED"},
    }


async def _bind_tenant(conn: asyncpg.Connection, tenant_id: int) -> None:
    await conn.execute("SELECT set_config('app.current_tenant_id', $1, true)", str(tenant_id))


async def status_totals(summary_pool, primary_pool, tenant_id, frm, to) -> dict:
    """Per-status [count, gross_cents, net_cents] over [frm, to), exact for any range."""
    plan = decompose(frm, to)
    acc: dict[str, list[int]] = {}

    def add(status, c, g, n):
        v = acc.setdefault(status, [0, 0, 0])
        v[0] += int(c or 0)
        v[1] += int(g or 0)
        v[2] += int(n or 0)

    async with summary_pool.acquire() as conn:
        async with conn.transaction():
            await _bind_tenant(conn, tenant_id)
            for d0, d1 in plan.days:
                for r in await conn.fetch(
                    "SELECT status, SUM(order_count)::bigint c, SUM(gross_sales_cents)::bigint g, SUM(net_sales_cents)::bigint n "
                    "FROM agg_orders_daily WHERE tenant_id=$1 AND day_bucket BETWEEN $2 AND $3 GROUP BY status",
                    tenant_id, d0, d1,
                ):
                    add(r["status"], r["c"], r["g"], r["n"])
            for a, b in plan.hours:
                for r in await conn.fetch(
                    "SELECT status, SUM(order_count)::bigint c, SUM(gross_sales_cents)::bigint g, SUM(net_sales_cents)::bigint n "
                    "FROM agg_orders_hourly WHERE tenant_id=$1 AND hour_bucket >= $2 AND hour_bucket < $3 GROUP BY status",
                    tenant_id, a, b,
                ):
                    add(r["status"], r["c"], r["g"], r["n"])
            for a, b in plan.minutes:
                for r in await conn.fetch(
                    "SELECT status, SUM(order_count)::bigint c, SUM(gross_sales_cents)::bigint g, SUM(net_sales_cents)::bigint n "
                    "FROM agg_orders_minutely WHERE tenant_id=$1 AND minute_bucket >= $2 AND minute_bucket < $3 GROUP BY status",
                    tenant_id, a, b,
                ):
                    add(r["status"], r["c"], r["g"], r["n"])

    # Sub-minute edges come from the primary (per-row), bounded and tiny.
    # Acquire the primary ONCE for all edges (was once per edge — wasted round-trips).
    if plan.edges:
        async with primary_pool.acquire() as pconn:
            for a, b in plan.edges:
                for r in await pconn.fetch(
                    "SELECT status, COUNT(*)::bigint c, SUM(total_amount_cents)::bigint g, "
                    "SUM(CASE WHEN status <> 'cancelled' THEN total_amount_cents ELSE 0 END)::bigint n "
                    "FROM orders WHERE tenant_id=$1 AND created_at >= $2 AND created_at < $3 GROUP BY status",
                    tenant_id, a, b,
                ):
                    add(r["status"], r["c"], r["g"], r["n"])
    return acc


async def summary(summary_pool, primary_pool, tenant_id, frm, to) -> dict:
    acc = await status_totals(summary_pool, primary_pool, tenant_id, frm, to)
    count = sum(v[0] for v in acc.values())
    gross = sum(v[1] for v in acc.values())
    net = sum(v[2] for v in acc.values())
    # Base variables for config-defined derived metrics (Bonus B-4).
    base = {
        "orders": count,
        "placed": acc.get("placed", [0])[0],
        "shipped": acc.get("shipped", [0])[0],
        "cancelled": acc.get("cancelled", [0])[0],
        "gross": _cents(gross),
        "net": _cents(net),
    }
    return {"order_count": count, "gross_sales": _cents(gross), "net_sales": _cents(net),
            "currency": config.CURRENCY, "derived": metrics.compute(base)}


async def by_status(summary_pool, primary_pool, tenant_id, frm, to) -> list[dict]:
    acc = await status_totals(summary_pool, primary_pool, tenant_id, frm, to)
    total = sum(v[0] for v in acc.values()) or 1
    out = [
        {"status": s, "order_count": v[0], "gross_sales": _cents(v[1]), "net_sales": _cents(v[2]),
         "percentage_orders": round(100.0 * v[0] / total, 2)}
        for s, v in acc.items()
    ]
    out.sort(key=lambda x: (-x["order_count"], x["status"]))
    return out


async def trends(summary_pool, tenant_id, frm, to, granularity) -> list[dict]:
    """Bucket-granular time series (window snapped to the grain)."""
    if granularity == "hour":
        table, col = "agg_orders_hourly", "hour_bucket"
        lo = frm.replace(minute=0, second=0, microsecond=0)
        hi = (to - timedelta(microseconds=1)).replace(minute=0, second=0, microsecond=0)
        params = (tenant_id, lo, hi)
        pred = f"{col} >= $2 AND {col} <= $3"
    else:
        table, col = "agg_orders_daily", "day_bucket"
        lo = frm.date()
        hi = (to - timedelta(microseconds=1)).date()
        params = (tenant_id, lo, hi)
        pred = f"{col} BETWEEN $2 AND $3"
    async with summary_pool.acquire() as conn:
        async with conn.transaction():
            await _bind_tenant(conn, tenant_id)
            rows = await conn.fetch(
                f"SELECT {col} AS bucket, SUM(order_count)::bigint c, SUM(gross_sales_cents)::bigint g, SUM(net_sales_cents)::bigint n "
                f"FROM {table} WHERE tenant_id=$1 AND {pred} GROUP BY {col} ORDER BY {col} ASC",
                *params,
            )
    return [
        {"bucket_start": (r["bucket"].isoformat() if hasattr(r["bucket"], "isoformat") else str(r["bucket"])),
         "order_count": r["c"], "gross_sales": _cents(r["g"]), "net_sales": _cents(r["n"])}
        for r in rows
    ]


async def top_products(summary_pool, tenant_id, frm, to, limit, sort_by) -> list[dict]:
    col = _SORT_COL.get(sort_by, "gross_revenue_cents")  # D2 default: gross
    lo = frm.date()
    hi = (to - timedelta(microseconds=1)).date()
    async with summary_pool.acquire() as conn:
        async with conn.transaction():
            await _bind_tenant(conn, tenant_id)
            rows = await conn.fetch(
                f"SELECT product_id, MAX(product_name) name, SUM(qty_sold)::bigint q, SUM(order_count)::bigint oc, "
                f"SUM(gross_revenue_cents)::bigint g, SUM(net_revenue_cents)::bigint n "
                f"FROM agg_products_daily WHERE tenant_id=$1 AND day_bucket BETWEEN $2 AND $3 "
                f"GROUP BY product_id HAVING SUM({col}) > 0 ORDER BY SUM({col}) DESC, product_id ASC LIMIT $4",
                tenant_id, lo, hi, limit,
            )
    return [
        {"rank": i + 1, "product_id": r["product_id"], "product_name": r["name"],
         "qty_sold": r["q"], "order_count": r["oc"],
         "gross_sales": _cents(r["g"]), "net_sales": _cents(r["n"])}
        for i, r in enumerate(rows)
    ]


# ------------------------------------------------------------------ #
# Baseline comparison: "recompute from scratch" vs Xponent's rollups.
# The naive path scans RAW orders in the source DB(s) — the work a dashboard
# without pre-aggregation is forced to do, and the work Xponent avoids. Same
# net/gross semantics as the rollups (net excludes cancelled) so the numbers
# match and only the TIME differs.
# ------------------------------------------------------------------ #
_NAIVE_SQL = (
    "SELECT COUNT(*)::bigint c, COALESCE(SUM(total_amount_cents),0)::bigint g, "
    "COALESCE(SUM(CASE WHEN status <> 'cancelled' THEN total_amount_cents ELSE 0 END),0)::bigint n "
    "FROM orders WHERE {where} created_at >= ${a} AND created_at < ${b}"
)


async def naive_tenant_scan(source_pool, tenant_id, frm, to) -> tuple[dict, int]:
    """Recompute one tenant's totals by scanning its raw orders. Returns (totals, rows_scanned)."""
    sql = _NAIVE_SQL.format(where="tenant_id=$1 AND", a=2, b=3)
    async with source_pool.acquire() as conn:
        r = await conn.fetchrow(sql, tenant_id, frm, to)
    data = {"order_count": int(r["c"]), "gross_sales": _cents(r["g"]), "net_sales": _cents(r["n"])}
    return data, int(r["c"])


async def naive_org_scan(source_pools, frm, to) -> tuple[dict, int, int]:
    """Recompute platform totals by scanning raw orders across every source DB.
    Returns (totals, rows_scanned, source_db_count)."""
    tot = [0, 0, 0]
    rows = 0
    sql = _NAIVE_SQL.format(where="", a=1, b=2)
    dbs = list(dict.fromkeys(source_pools))  # distinct pools, order-preserving
    for pool in dbs:
        async with pool.acquire() as conn:
            r = await conn.fetchrow(sql, frm, to)
        tot[0] += int(r["c"]); tot[1] += int(r["g"]); tot[2] += int(r["n"]); rows += int(r["c"])
    data = {"order_count": tot[0], "gross_sales": _cents(tot[1]), "net_sales": _cents(tot[2])}
    return data, rows, len(dbs)


async def rollup_rows_tenant(summary_pool, tenant_id, frm, to) -> int:
    """How many pre-aggregated daily rollup rows the tenant read covers (the small
    number Xponent touches instead of every raw order)."""
    lo, hi = frm.date(), (to - timedelta(microseconds=1)).date()
    async with summary_pool.acquire() as conn:
        async with conn.transaction():
            await _bind_tenant(conn, tenant_id)
            n = await conn.fetchval(
                "SELECT COUNT(*) FROM agg_orders_daily WHERE tenant_id=$1 AND day_bucket BETWEEN $2 AND $3",
                tenant_id, lo, hi)
    return int(n or 0)


async def rollup_rows_org(owner_pool, frm, to) -> int:
    """Pre-aggregated daily rollup rows the platform-wide read covers (all tenants)."""
    lo, hi = frm.date(), (to - timedelta(microseconds=1)).date()
    async with owner_pool.acquire() as conn:
        n = await conn.fetchval(
            "SELECT COUNT(*) FROM agg_orders_daily WHERE day_bucket BETWEEN $1 AND $2", lo, hi)
    return int(n or 0)


async def timed_best(factory, k: int = 5):
    """Run an async call k times and return (last_result, best_ms). At sub-10ms a
    single shot is dominated by connection/scheduler noise; the warm best is the
    fair, representative latency — applied identically to both engines so neither
    is favoured."""
    best = None
    result = None
    for _ in range(max(1, k)):
        t = time.perf_counter()
        result = await factory()
        ms = (time.perf_counter() - t) * 1000.0
        best = ms if best is None else min(best, ms)
    return result, round(best or 0.0, 1)
