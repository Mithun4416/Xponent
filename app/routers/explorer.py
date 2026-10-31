"""Read-only database explorer (judge-facing "see the databases").

Lets an operator browse BOTH sides of the architecture from the dashboard:
  * each tenant's SOURCE OLTP database (tenants/products/customers/orders/order_items)
  * the central SUMMARY/rollup store (daily/hourly/minutely aggregates, product
    sums, verification digests, CDC checkpoint + activity)

Safety: admin-key guarded, strictly read-only, and table names are taken from a
fixed ALLOWLIST (never from user input), so no arbitrary table or SQL can be
reached. Row counts use pg_class.reltuples ESTIMATES (not COUNT(*)) so opening
the 10M `orders` table never scans it. Page size is capped by EXPLORER_MAX_ROWS.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from urllib.parse import urlparse

from fastapi import APIRouter, Header, HTTPException, Query

from app.core import config
from app.core.db import pools

router = APIRouter(prefix="/api/v1/explore", tags=["DB explorer"])

# Only these tables may be browsed, per database side.
_SOURCE_TABLES = ["tenants", "products", "customers", "orders", "order_items"]
_SUMMARY_TABLES = ["agg_orders_daily", "agg_orders_hourly", "agg_orders_minutely",
                   "agg_products_daily", "bucket_digests", "cdc_checkpoint",
                   "cdc_activity", "shadow_orders"]


def _auth(x_admin_key: str | None) -> None:
    if not config.ADMIN_KEY or x_admin_key != config.ADMIN_KEY:
        raise HTTPException(status_code=403, detail="Admin key required.")


def _dsn_display(dsn: str) -> tuple[str, str]:
    """(host:port, dbname) for display — never the credentials."""
    try:
        u = urlparse(dsn)
        host = f"{u.hostname or 'localhost'}:{u.port or 5432}"
        return host, (u.path or "/").lstrip("/") or "?"
    except Exception:
        return "?", "?"


def _summary_pool():
    pool = pools.summary_owner or pools.summary
    if pool is None:
        raise HTTPException(status_code=503, detail="Summary pool not connected.")
    return pool


def _jsonable(v):
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, (bytes, memoryview)):
        return "0x" + bytes(v).hex()
    return v


@router.get("/databases")
async def databases(x_admin_key: str | None = Header(default=None)):
    """List the databases a judge can inspect: every tenant source DB + the summary store."""
    _auth(x_admin_key)
    out = []
    seen = set()
    for s in pools.sources:
        if s.dsn in seen:
            continue
        seen.add(s.dsn)
        host, dbname = _dsn_display(s.dsn)
        reachable = s.dsn in pools.source_pools
        out.append({
            "id": f"source:{s.tenant_id}" if s.tenant_id is not None else "source",
            "kind": "source",
            "label": (f"Source DB · Restaurant #{s.tenant_id}" if s.tenant_id is not None
                      else "Source OLTP DB (shared)"),
            "tenant_id": s.tenant_id, "host": host, "dbname": dbname,
            "reachable": reachable, "tables": _SOURCE_TABLES,
            "role": "Where live orders are written (the real app's database)",
        })
    host, dbname = _dsn_display(config.SUMMARY_DB_OWNER_URL or config.SUMMARY_DB_URL)
    out.append({
        "id": "summary", "kind": "summary", "label": "Summary / rollup store (Xponent)",
        "tenant_id": None, "host": host, "dbname": dbname, "reachable": pools.summary is not None,
        "tables": _SUMMARY_TABLES,
        "role": "Pre-computed per-tenant rollups the dashboard reads (fast, DB-safe)",
    })
    return {"databases": out}


def _resolve(database: str):
    """Return (pool, allowed_tables) for a database id, or 404/503."""
    if database == "summary":
        return _summary_pool(), _SUMMARY_TABLES
    if database.startswith("source:"):
        try:
            tid = int(database.split(":", 1)[1])
        except ValueError:
            raise HTTPException(status_code=400, detail="bad database id")
        for s in pools.sources:
            if s.tenant_id == tid:
                pool = pools.source_pools.get(s.dsn)
                if pool is None:
                    raise HTTPException(status_code=503, detail=f"source for tenant {tid} not reachable")
                return pool, _SOURCE_TABLES
    raise HTTPException(status_code=404, detail="unknown database")


@router.get("/rows")
async def rows(
    database: str = Query(...),
    table: str = Query(...),
    limit: int = Query(25, ge=1),
    offset: int = Query(0, ge=0),
    x_admin_key: str | None = Header(default=None),
):
    """A page of rows from one allowlisted table, with its real columns."""
    _auth(x_admin_key)
    pool, allowed = _resolve(database)
    if table not in allowed:
        raise HTTPException(status_code=404, detail="table not available in this database")
    limit = min(limit, config.EXPLORER_MAX_ROWS)

    async with pool.acquire() as conn:
        cols = await conn.fetch(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name=$1 ORDER BY ordinal_position", table)
        if not cols:
            raise HTTPException(status_code=404, detail="table has no columns / does not exist")
        col_names = [c["column_name"] for c in cols]
        # Identifiers are from information_schema for an allowlisted table — safe to quote inline.
        select_list = ", ".join(f'"{c}"' for c in col_names)
        data = await conn.fetch(
            f'SELECT {select_list} FROM "{table}" ORDER BY 1 LIMIT $1 OFFSET $2', limit, offset)
        # Estimated row count (fast; never scans the 10M table).
        est = await conn.fetchval(
            "SELECT reltuples::bigint FROM pg_class WHERE oid = to_regclass($1)", f"public.{table}")

    return {
        "database": database, "table": table,
        "columns": [{"name": c["column_name"], "type": c["data_type"]} for c in cols],
        "rows": [[_jsonable(r[c]) for c in col_names] for r in data],
        "row_count": len(data), "limit": limit, "offset": offset,
        "estimated_total": int(est) if est is not None else None,
    }
