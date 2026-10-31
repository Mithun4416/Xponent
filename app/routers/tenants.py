"""Tenant dashboard endpoints FR-1..FR-4 (task T7).

Every endpoint binds the tenant via get_tenant_context (isolation barrier 1) and
reads through query_service, which activates RLS (barrier 2) and uses the shared
decomposer for exactness on non-aligned ranges.
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Body, Depends, HTTPException, Path, Query

from app.core import config
from app.core.db import pools, primary_for_tenant, slot_for_tenant
from app.core.limits import heavy_query_limit
from app.core.security import get_tenant_context
from app.services import query_service
from app.services.freshness import freshness_block
from app.services.verification import tenant_badge

router = APIRouter(prefix="/api/v1/tenants/{tenant_id}", tags=["dashboard"])


def _window(from_ts: datetime, to_ts: datetime) -> tuple[datetime, datetime]:
    frm = from_ts if from_ts.tzinfo else from_ts.replace(tzinfo=timezone.utc)
    to = to_ts if to_ts.tzinfo else to_ts.replace(tzinfo=timezone.utc)
    frm, to = frm.astimezone(timezone.utc), to.astimezone(timezone.utc)
    if frm >= to:
        raise HTTPException(status_code=400, detail="`from` must be strictly before `to`.")
    if to - frm > timedelta(days=config.MAX_WINDOW_DAYS):
        raise HTTPException(status_code=400, detail=f"Window exceeds {config.MAX_WINDOW_DAYS} days.")
    return frm, to


@router.get("/orders/summary")
async def orders_summary(
    tenant_id: int = Path(..., ge=1),
    from_ts: datetime = Query(..., alias="from"),
    to_ts: datetime = Query(..., alias="to"),
    tenant: int = Depends(get_tenant_context),
    _rl=Depends(heavy_query_limit),
):
    frm, to = _window(from_ts, to_ts)
    data = await query_service.summary(pools.summary, primary_for_tenant(tenant), tenant, frm, to)
    return {"tenant_id": tenant, "from": frm.isoformat(), "to": to.isoformat(),
            "data": data, "freshness": await freshness_block(pools.summary, slot_for_tenant(tenant)),
            "verification": await tenant_badge(pools.summary, tenant)}


@router.get("/orders/trends")
async def orders_trends(
    tenant_id: int = Path(..., ge=1),
    from_ts: datetime = Query(..., alias="from"),
    to_ts: datetime = Query(..., alias="to"),
    granularity: str = Query("day", pattern="^(hour|day)$"),
    tenant: int = Depends(get_tenant_context),
    _rl=Depends(heavy_query_limit),
):
    frm, to = _window(from_ts, to_ts)
    series = await query_service.trends(pools.summary, tenant, frm, to, granularity)
    return {"tenant_id": tenant, "granularity": granularity, "from": frm.isoformat(), "to": to.isoformat(),
            "series": series, "freshness": await freshness_block(pools.summary, slot_for_tenant(tenant))}


@router.get("/products/top")
async def products_top(
    tenant_id: int = Path(..., ge=1),
    from_ts: datetime = Query(..., alias="from"),
    to_ts: datetime = Query(..., alias="to"),
    limit: int = Query(10, ge=1, le=100),
    sort_by: str = Query("gross_sales", pattern="^(gross_sales|net_sales|qty_sold)$"),
    tenant: int = Depends(get_tenant_context),
    _rl=Depends(heavy_query_limit),
):
    frm, to = _window(from_ts, to_ts)
    items = await query_service.top_products(pools.summary, tenant, frm, to, limit, sort_by)
    return {"tenant_id": tenant, "from": frm.isoformat(), "to": to.isoformat(), "limit": limit,
            "sort_by": sort_by, "items": items, "freshness": await freshness_block(pools.summary, slot_for_tenant(tenant))}


@router.get("/orders/by-status")
async def orders_by_status(
    tenant_id: int = Path(..., ge=1),
    from_ts: datetime = Query(..., alias="from"),
    to_ts: datetime = Query(..., alias="to"),
    tenant: int = Depends(get_tenant_context),
    _rl=Depends(heavy_query_limit),
):
    frm, to = _window(from_ts, to_ts)
    categories = await query_service.by_status(pools.summary, primary_for_tenant(tenant), tenant, frm, to)
    return {"tenant_id": tenant, "from": frm.isoformat(), "to": to.isoformat(),
            "categories": categories, "freshness": await freshness_block(pools.summary, slot_for_tenant(tenant))}


# -------- Seller-scoped order writes (tenant enforced by get_tenant_context) --------
# A seller authenticated for tenant N can only create/change/delete tenant N's orders;
# the path tenant must match the asserted identity (403 otherwise). Writes go to the
# tenant's OWN source DB, so they flow through CDC into that tenant's rollup.

@router.post("/orders", status_code=201)
async def create_order(
    tenant_id: int = Path(..., ge=1),
    payload: dict = Body(default={}),
    tenant: int = Depends(get_tenant_context),
):
    amount = int(payload.get("amount_cents") or random.randint(1500, 60000))
    if amount <= 0:
        raise HTTPException(status_code=400, detail="amount_cents must be positive.")
    pool = primary_for_tenant(tenant)
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                ids = await conn.fetchrow(
                    "SELECT COALESCE(MAX(order_id),0)+1 AS oid, "
                    "(SELECT COALESCE(MAX(order_item_id),0)+1 FROM order_items) AS iid FROM orders")
                oid, iid = int(ids["oid"]), int(ids["iid"])
                cust = await conn.fetchval("SELECT customer_id FROM customers WHERE tenant_id=$1 LIMIT 1", tenant)
                prod = await conn.fetchrow(
                    "SELECT product_id, product_name FROM products WHERE tenant_id=$1 "
                    "AND ($2::bigint IS NULL OR product_id=$2) LIMIT 1", tenant, payload.get("product_id"))
                if prod is None:
                    raise HTTPException(status_code=400, detail="no product for this restaurant.")
                await conn.execute(
                    "INSERT INTO orders (order_id, tenant_id, customer_id, status, total_amount_cents, created_at, updated_at) "
                    "VALUES ($1,$2,$3,'placed',$4,NOW(),NOW())", oid, tenant, cust, amount)
                await conn.execute(
                    "INSERT INTO order_items (order_item_id, order_id, tenant_id, product_id, quantity, line_amount_cents, order_created_at) "
                    "VALUES ($1,$2,$3,$4,1,$5,NOW())", iid, oid, tenant, prod["product_id"], amount)
        return {"ok": True, "action": "create", "tenant_id": tenant, "order_id": oid,
                "amount_cents": amount, "product_name": prod["product_name"], "status": "placed"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"create failed: {e}")


@router.post("/orders/{order_id}/cancel")
async def cancel_order(tenant_id: int = Path(..., ge=1), order_id: int = Path(...),
                       tenant: int = Depends(get_tenant_context)):
    pool = primary_for_tenant(tenant)
    n = await pool.execute(
        "UPDATE orders SET status='cancelled', updated_at=NOW() WHERE tenant_id=$1 AND order_id=$2 AND status<>'cancelled'",
        tenant, order_id)
    if n.endswith(" 0"):
        raise HTTPException(status_code=404, detail="order not found (for this restaurant) or already cancelled.")
    return {"ok": True, "action": "cancel", "tenant_id": tenant, "order_id": order_id}


@router.patch("/orders/{order_id}")
async def update_order(tenant_id: int = Path(..., ge=1), order_id: int = Path(...),
                       payload: dict = Body(...), tenant: int = Depends(get_tenant_context)):
    amount = int(payload.get("amount_cents", 0))
    if amount <= 0:
        raise HTTPException(status_code=400, detail="amount_cents must be positive.")
    pool = primary_for_tenant(tenant)
    n = await pool.execute(
        "UPDATE orders SET total_amount_cents=$3, updated_at=NOW() WHERE tenant_id=$1 AND order_id=$2",
        tenant, order_id, amount)
    if n.endswith(" 0"):
        raise HTTPException(status_code=404, detail="order not found for this restaurant.")
    return {"ok": True, "action": "update", "tenant_id": tenant, "order_id": order_id, "amount_cents": amount}


@router.delete("/orders/{order_id}")
async def delete_order(tenant_id: int = Path(..., ge=1), order_id: int = Path(...),
                       tenant: int = Depends(get_tenant_context)):
    pool = primary_for_tenant(tenant)
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("DELETE FROM order_items WHERE tenant_id=$1 AND order_id=$2", tenant, order_id)
            n = await conn.execute("DELETE FROM orders WHERE tenant_id=$1 AND order_id=$2", tenant, order_id)
    if n.endswith(" 0"):
        raise HTTPException(status_code=404, detail="order not found for this restaurant.")
    return {"ok": True, "action": "delete", "tenant_id": tenant, "order_id": order_id}


@router.get("/orders")
async def list_orders(tenant_id: int = Path(..., ge=1), limit: int = Query(20, ge=1, le=100),
                      tenant: int = Depends(get_tenant_context)):
    """Recent orders from the tenant's own source DB (for the Orders view / CRUD targets)."""
    pool = primary_for_tenant(tenant)
    rows = await pool.fetch(
        "SELECT order_id, status, total_amount_cents, created_at FROM orders "
        "WHERE tenant_id=$1 ORDER BY order_id DESC LIMIT $2", tenant, limit)
    return {"tenant_id": tenant, "orders": [
        {"order_id": r["order_id"], "status": r["status"], "amount_cents": int(r["total_amount_cents"]),
         "created_at": r["created_at"].isoformat()} for r in rows]}
