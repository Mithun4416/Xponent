"""Demo actions — write test orders into a tenant's OWN source DB so the change
flows through CDC → rollup (judge flow). Admin-guarded, clearly marked demo/test.

Writes require the tenant's source pool to allow writes (true in the per-tenant
topology, where sources use a superuser DSN). Returns the order_id so the UI can
correlate with the live activity feed.
"""
from __future__ import annotations

import random

from fastapi import APIRouter, Body, Header, HTTPException

from app.core import config
from app.core.db import primary_for_tenant

router = APIRouter(prefix="/api/v1/demo", tags=["Demo actions"])


def _auth(x_admin_key: str | None) -> None:
    if not config.ADMIN_KEY or x_admin_key != config.ADMIN_KEY:
        raise HTTPException(status_code=403, detail="Admin key required.")


@router.post("/order")
async def create_order(payload: dict = Body(...), x_admin_key: str | None = Header(default=None)):
    _auth(x_admin_key)
    t = int(payload["tenant_id"])
    amount = int(payload.get("amount_cents") or random.randint(1500, 6000))
    pool = primary_for_tenant(t)
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                ids = await conn.fetchrow(
                    "SELECT COALESCE(MAX(order_id),0)+1 AS oid, "
                    "(SELECT COALESCE(MAX(order_item_id),0)+1 FROM order_items) AS iid FROM orders")
                oid, iid = int(ids["oid"]), int(ids["iid"])
                cust = await conn.fetchval("SELECT customer_id FROM customers WHERE tenant_id=$1 LIMIT 1", t)
                prod = await conn.fetchrow("SELECT product_id, product_name FROM products WHERE tenant_id=$1 LIMIT 1", t)
                await conn.execute(
                    "INSERT INTO orders (order_id, tenant_id, customer_id, status, total_amount_cents, created_at, updated_at) "
                    "VALUES ($1,$2,$3,'placed',$4,NOW(),NOW())", oid, t, cust, amount)
                await conn.execute(
                    "INSERT INTO order_items (order_item_id, order_id, tenant_id, product_id, quantity, line_amount_cents, order_created_at) "
                    "VALUES ($1,$2,$3,$4,1,$5,NOW())", iid, oid, t, prod["product_id"], amount)
        return {"demo": True, "action": "create", "tenant_id": t, "order_id": oid,
                "amount_cents": amount, "product": prod["product_name"],
                "note": "flows through CDC → rollup; watch the activity feed, then re-run the query"}
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"demo write failed (is this tenant's source writable?): {e}")


@router.post("/order/{tenant_id}/{order_id}/cancel")
async def cancel_order(tenant_id: int, order_id: int, x_admin_key: str | None = Header(default=None)):
    _auth(x_admin_key)
    pool = primary_for_tenant(tenant_id)
    n = await pool.execute(
        "UPDATE orders SET status='cancelled', updated_at=NOW() WHERE tenant_id=$1 AND order_id=$2 AND status<>'cancelled'",
        tenant_id, order_id)
    return {"demo": True, "action": "cancel", "tenant_id": tenant_id, "order_id": order_id, "updated": n}


@router.post("/order/{tenant_id}/{order_id}/update")
async def update_order(tenant_id: int, order_id: int, payload: dict = Body(...),
                       x_admin_key: str | None = Header(default=None)):
    _auth(x_admin_key)
    amount = int(payload.get("amount_cents", 0))
    if amount <= 0:
        raise HTTPException(status_code=400, detail="amount_cents > 0 required.")
    pool = primary_for_tenant(tenant_id)
    n = await pool.execute(
        "UPDATE orders SET total_amount_cents=$3, updated_at=NOW() WHERE tenant_id=$1 AND order_id=$2",
        tenant_id, order_id, amount)
    return {"demo": True, "action": "update", "tenant_id": tenant_id, "order_id": order_id,
            "amount_cents": amount, "updated": n}
