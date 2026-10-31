"""Health + freshness endpoints (task T7)."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter

from app.core.db import pools, slot_for_tenant
from app.services.freshness import freshness_block
from app.services.verification import tenant_badge

router = APIRouter()


@router.get("/api/v1/health", tags=["ops"])
async def health():
    components = {"summary_db": "DOWN", "primary_db": "DOWN"}
    try:
        if pools.summary and await pools.summary.fetchval("SELECT 1") == 1:
            components["summary_db"] = "UP"
    except Exception:
        pass
    try:
        if pools.primary and await pools.primary.fetchval("SELECT 1") == 1:
            components["primary_db"] = "UP"
    except Exception:
        pass
    healthy = components["summary_db"] == "UP"
    return {
        "status": "HEALTHY" if healthy else "DEGRADED",
        "components": components,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@router.get("/api/v1/tenants/{tenant_id}/freshness", tags=["B-1"])
async def freshness(tenant_id: int):
    block = await freshness_block(pools.summary, slot_for_tenant(tenant_id))
    badge = await tenant_badge(pools.summary, tenant_id)
    return {"tenant_id": tenant_id, **block, "verification": badge}
