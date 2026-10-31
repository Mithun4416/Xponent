"""Natural-language query endpoint (primary dashboard feature).

Admin-guarded operator tool. The LLM only produces a validated query PLAN; this
endpoint runs it through the existing rollup query path and returns the plan, the
representative SQL, the result, timing, source, and freshness. No arbitrary SQL.
"""
from __future__ import annotations

from fastapi import APIRouter, Body, Header, HTTPException, Request

from app.core import config
from app.services import nlq

router = APIRouter(prefix="/api/v1", tags=["NL query"])


@router.post("/nlq")
async def nl_query(payload: dict = Body(...), request: Request = None,
                   x_admin_key: str | None = Header(default=None)):
    question = (payload.get("question") or "").strip()
    if not question:
        raise HTTPException(status_code=400, detail="`question` is required.")

    # Seller path: a dev-mode seller (X-Dev-Tenant-ID) is server-pinned to its own
    # tenant — the model/question cannot select another tenant, no admin key needed.
    dev = request.headers.get("X-Dev-Tenant-ID") if request else None
    if config.AUTH_DEV_MODE and dev is not None:
        try:
            pinned = int(dev)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="Invalid X-Dev-Tenant-ID.")
    else:
        # Operator path: admin key; may target a specific tenant or org.
        if not config.ADMIN_KEY or x_admin_key != config.ADMIN_KEY:
            raise HTTPException(status_code=403, detail="Admin key or tenant identity required.")
        p = payload.get("tenant_id")
        pinned = int(p) if p is not None else None

    try:
        return await nlq.run_nlq(question, pinned_tenant=pinned)
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))
