"""Per-tenant concurrency limit for heavy dashboard queries (Bonus B-3).

A single tenant hammering its dashboard must not exhaust the summary store's
connections and slow everyone else down. Each tenant gets at most
`TENANT_MAX_CONCURRENCY` in-flight heavy reads; past that the API returns 429
(Too Many Requests) immediately instead of queueing. In-process and best-effort
(one API replica) — deliberately simple, matching the no-infra posture.

Usage as a FastAPI dependency::

    @router.get(...)
    async def endpoint(..., _rl=Depends(heavy_query_limit)):
        ...
"""
from __future__ import annotations

import asyncio
from collections import defaultdict

from fastapi import Depends, HTTPException

from app.core import config
from app.core.security import get_tenant_context

# tenant_id -> number of heavy queries currently running for that tenant.
_inflight: dict[int, int] = defaultdict(int)
_lock = asyncio.Lock()


class _Slot:
    """Async context manager that holds one heavy-query slot for a tenant."""

    def __init__(self, tenant_id: int) -> None:
        self.tenant_id = tenant_id

    async def __aenter__(self) -> "_Slot":
        async with _lock:
            if _inflight[self.tenant_id] >= config.TENANT_MAX_CONCURRENCY:
                raise HTTPException(
                    status_code=429,
                    detail=(f"Too many concurrent queries for tenant {self.tenant_id} "
                            f"(limit {config.TENANT_MAX_CONCURRENCY}). Please retry shortly."),
                    headers={"Retry-After": "1"},
                )
            _inflight[self.tenant_id] += 1
        return self

    async def __aexit__(self, *exc) -> None:
        async with _lock:
            _inflight[self.tenant_id] = max(0, _inflight[self.tenant_id] - 1)


async def heavy_query_limit(tenant: int = Depends(get_tenant_context)):
    """Dependency: admit the request only if the tenant is under its concurrency
    cap; the slot is released when the request finishes."""
    async with _Slot(tenant):
        yield


def snapshot() -> dict[int, int]:
    """Current in-flight counts per tenant (for the monitoring page)."""
    return {t: n for t, n in _inflight.items() if n > 0}
