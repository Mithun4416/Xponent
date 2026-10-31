"""Admin endpoints for Receipts (B-2): verification status + targeted repair.

Guarded by a simple X-Admin-Key header (consistent with the no-JWT posture, D3).
Writes require the summary OWNER pool; if it isn't configured the endpoints
return 503 rather than attempting a write with the read-only role.
"""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Body, Header, HTTPException

from app.core import config
from app.core.db import pools, primary_for_tenant
from app.services import receipts

router = APIRouter(prefix="/api/v1/admin", tags=["B-2 Receipts"])


def _auth(x_admin_key: str | None) -> None:
    if not config.ADMIN_KEY or x_admin_key != config.ADMIN_KEY:
        raise HTTPException(status_code=403, detail="Admin key required.")


def _owner():
    if pools.summary_owner is None:
        raise HTTPException(status_code=503, detail="Summary owner pool not configured (set SUMMARY_DB_OWNER_URL).")
    return pools.summary_owner


@router.get("/verification")
async def verification(
    tenant_id: int | None = None,
    run: bool = False,
    sample: int = 20,
    x_admin_key: str | None = Header(default=None),
):
    _auth(x_admin_key)
    owner = _owner()
    result: dict = {}
    if run:
        result["sweep"] = await receipts.verify_sweep(owner, primary_for_tenant, tenant_id, sample)
    where = "WHERE tenant_id=$1" if tenant_id is not None else ""
    args = (tenant_id,) if tenant_id is not None else ()
    summary = await owner.fetchrow(
        f"SELECT COUNT(*)::bigint total, COUNT(*) FILTER (WHERE is_corrupted)::bigint corrupted, "
        f"MAX(verified_at) last_verified FROM bucket_digests {where}", *args)
    corrupted = await owner.fetch(
        f"SELECT tenant_id, day_bucket FROM bucket_digests "
        f"WHERE is_corrupted {'AND tenant_id=$1' if tenant_id is not None else ''} "
        f"ORDER BY tenant_id, day_bucket LIMIT 100", *args)
    result["digest_health"] = {
        "total_buckets": int(summary["total"]),
        "corrupted_buckets": int(summary["corrupted"]),
        "last_verified_at": summary["last_verified"].isoformat() if summary["last_verified"] else None,
        "corrupted": [{"tenant_id": r["tenant_id"], "day_bucket": r["day_bucket"].isoformat()} for r in corrupted],
        "status": "VERIFIED" if int(summary["corrupted"]) == 0 else "DRIFT_DETECTED",
    }
    return result


@router.post("/rebuild")
async def rebuild(
    payload: dict = Body(...),
    x_admin_key: str | None = Header(default=None),
):
    _auth(x_admin_key)
    owner = _owner()
    mode = payload.get("mode", "TARGETED")
    tenant_id = payload.get("tenant_id")
    day_str = payload.get("day_bucket")
    if mode != "TARGETED":
        raise HTTPException(status_code=400, detail="MVP supports mode=TARGETED only.")
    if tenant_id is None or not day_str:
        raise HTTPException(status_code=400, detail="TARGETED rebuild requires tenant_id and day_bucket.")
    try:
        day = date.fromisoformat(day_str)
    except ValueError:
        raise HTTPException(status_code=400, detail="day_bucket must be YYYY-MM-DD.")
    return await receipts.repair_bucket(owner, primary_for_tenant, int(tenant_id), day)
