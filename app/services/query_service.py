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

from datetime import datetime, timedelta, timezone

import asyncpg

from app.core import config
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
    for a, b in plan.edges:
        async with primary_pool.acquire() as pconn:
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
    return {"order_count": count, "gross_sales": _cents(gross), "net_sales": _cents(net), "currency": config.CURRENCY}


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
