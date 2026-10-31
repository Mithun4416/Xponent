"""Organization-owner endpoints — the platform's cross-tenant view (Zomato-as-a-whole).

Distinct from the seller endpoints (`/tenants/{id}/...`, scoped + RLS-enforced):
these aggregate across ALL sellers and are guarded by the organization admin key.
A seller cannot call these; the org owner sees the whole platform.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import asyncpg
from fastapi import APIRouter, Header, HTTPException, Query

from app.core import config
from app.core.db import pools
from app.services import query_service

router = APIRouter(prefix="/api/v1/org", tags=["Organization"])


def _auth(x_admin_key: str | None) -> None:
    if not config.ADMIN_KEY or x_admin_key != config.ADMIN_KEY:
        raise HTTPException(status_code=403, detail="Organization admin key required.")


def _owner():
    if pools.summary_owner is None:
        raise HTTPException(status_code=503, detail="Owner pool not configured (set SUMMARY_DB_OWNER_URL).")
    return pools.summary_owner


def _window(frm: datetime, to: datetime) -> tuple[datetime, datetime]:
    frm = (frm if frm.tzinfo else frm.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)
    to = (to if to.tzinfo else to.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)
    if frm >= to:
        raise HTTPException(status_code=400, detail="`from` must be strictly before `to`.")
    if to - frm > timedelta(days=config.MAX_WINDOW_DAYS):
        raise HTTPException(status_code=400, detail=f"Window exceeds {config.MAX_WINDOW_DAYS} days.")
    return frm, to


@router.get("/activity")
async def activity(tenant_id: int | None = None, limit: int = 50, x_admin_key: str | None = Header(default=None)):
    """Recent real CDC events (live activity feed), newest first; optional tenant filter."""
    _auth(x_admin_key)
    owner = _owner()
    limit = max(1, min(int(limit), 500))
    where = "WHERE tenant_id=$1" if tenant_id is not None else ""
    args = (tenant_id,) if tenant_id is not None else ()
    try:
        rows = await owner.fetch(
            f"SELECT id, tenant_id, op, tbl, order_id, lsn, commit_ts, applied_at "
            f"FROM cdc_activity {where} ORDER BY id DESC LIMIT {limit}", *args)
    except asyncpg.UndefinedTableError:
        # No events table yet (worker has not applied anything). Empty, not an error.
        return {"events": [], "note": "No CDC events yet — the worker populates this on the first change."}
    op_name = {"I": "INSERT", "U": "UPDATE", "D": "DELETE"}
    return {"events": [
        {"id": r["id"], "tenant_id": r["tenant_id"],
         "op": op_name.get(r["op"], r["op"]), "table": r["tbl"],
         "order_id": r["order_id"], "lsn": r["lsn"],
         "commit_ts": r["commit_ts"].isoformat() if r["commit_ts"] else None,
         "applied_at": r["applied_at"].isoformat() if r["applied_at"] else None}
        for r in rows]}


@router.get("/overview")
async def overview(
    from_ts: datetime = Query(..., alias="from"),
    to_ts: datetime = Query(..., alias="to"),
    x_admin_key: str | None = Header(default=None),
):
    """Platform totals across all sellers + a per-seller leaderboard + fleet verification health."""
    _auth(x_admin_key)
    frm, to = _window(from_ts, to_ts)
    data = await query_service.org_overview(_owner(), frm, to)
    return {"from": frm.isoformat(), "to": to.isoformat(), **data}
