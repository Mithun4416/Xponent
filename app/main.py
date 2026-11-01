"""Xponent query API entrypoint (task T7)."""
from __future__ import annotations

import os
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from app.core import rtstats
from app.core.db import connect, disconnect
from app.routers import admin, demo, explorer, health, org, query, status, tenants

_FRONTEND = os.path.join(os.path.dirname(__file__), "..", "frontend")
_DASHBOARD = os.path.join(_FRONTEND, "index.html")
_STATIC = os.path.join(_FRONTEND, "static")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await connect()
    yield
    await disconnect()


app = FastAPI(
    title="Xponent — Multi-Tenant Live Aggregation Layer",
    version="0.1.0",
    lifespan=lifespan,
)
app.include_router(health.router)
app.include_router(tenants.router)
app.include_router(admin.router)
app.include_router(org.router)
app.include_router(query.router)
app.include_router(status.router)
app.include_router(demo.router)
app.include_router(explorer.router)


@app.middleware("http")
async def _record_latency(request: Request, call_next):
    """Time every data query for the monitoring page's p50/p95/p99 (Bonus B-5)."""
    t0 = time.perf_counter()
    response = await call_next(request)
    try:
        if request.url.path.startswith("/api/v1/") and "/status" not in request.url.path:
            rtstats.record((time.perf_counter() - t0) * 1000.0)
    except Exception:
        pass  # monitoring must never affect the response
    return response


if os.path.isdir(_STATIC):
    app.mount("/static", StaticFiles(directory=_STATIC), name="static")


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    """Serve the live monitoring dashboard (Bonus B-5), same-origin with the API."""
    try:
        with open(_DASHBOARD, encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return HTMLResponse("<h1>Xponent API</h1><p>Dashboard not bundled.</p>", status_code=200)
