"""Tenant isolation guard (task T7, decision D3 — no JWT).

Identity is the URL path tenant_id, asserted by one of:
  * dev mode: header X-Dev-Tenant-ID must equal the path tenant_id, or
  * a shared key: header X-Tenant-Key must equal TENANT_API_KEY AND
    X-Tenant-Id must equal the path tenant_id.
A mismatch is 403 (cross-tenant), a missing assertion is 401. This is the first
isolation barrier; PostgreSQL RLS (SET via set_config in the query layer) is the
second. The PRD excludes real authentication (OOS-002); this is deliberately minimal.
"""
from __future__ import annotations

from fastapi import HTTPException, Path, Request

from app.core import config


async def get_tenant_context(request: Request, tenant_id: int = Path(..., ge=1)) -> int:
    dev = request.headers.get("X-Dev-Tenant-ID")
    if config.AUTH_DEV_MODE and dev is not None:
        if _as_int(dev) != tenant_id:
            raise HTTPException(status_code=403, detail="Cross-tenant access forbidden.")
        return tenant_id

    if config.TENANT_API_KEY:
        key = request.headers.get("X-Tenant-Key")
        asserted = request.headers.get("X-Tenant-Id")
        if key == config.TENANT_API_KEY and _as_int(asserted) == tenant_id:
            return tenant_id
        if key is not None:
            raise HTTPException(status_code=403, detail="Cross-tenant access forbidden.")

    raise HTTPException(status_code=401, detail="Tenant authentication missing.")


def _as_int(v: str | None) -> int | None:
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None
