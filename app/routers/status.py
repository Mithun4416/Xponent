"""Live pipeline status — only real stage data (no fabrication).

Stage 1 Tenant source DBs: reachability + that tenant's replication slot (active, LSN).
Stage 2 CDC → summary: per-slot checkpoint (as_of, lag, applied LSN) from cdc_checkpoint.
Stage 3 Query API: UP (we answered).
Fields that can't be read are omitted rather than invented.
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter

from app.core.db import pools

router = APIRouter(prefix="/api/v1/status", tags=["Status"])


@router.get("/pipeline")
async def pipeline():
    sources = []
    for s in pools.sources:
        entry = {"tenant_id": s.tenant_id, "slot": s.slot, "reachable": False,
                 "slot_active": None, "slot_lsn": None}
        pool = pools.source_pools.get(s.dsn)
        if pool is not None:
            try:
                row = await pool.fetchrow(
                    "SELECT active, restart_lsn::text AS lsn FROM pg_replication_slots WHERE slot_name=$1", s.slot)
                entry["reachable"] = True
                if row is not None:
                    entry["slot_active"] = row["active"]
                    entry["slot_lsn"] = row["lsn"]
            except Exception:
                entry["reachable"] = False
        sources.append(entry)

    checkpoints = []
    now = datetime.now(timezone.utc)
    try:
        rows = await pools.summary.fetch(
            "SELECT slot_name, last_applied_lsn_str, as_of_timestamp FROM cdc_checkpoint ORDER BY slot_name")
        for r in rows:
            as_of = r["as_of_timestamp"]
            checkpoints.append({
                "slot": r["slot_name"], "last_applied_lsn": r["last_applied_lsn_str"],
                "as_of": as_of.isoformat() if as_of else None,
                "lag_ms": max(0, int((now - as_of).total_seconds() * 1000)) if as_of else None,
            })
    except Exception:
        pass

    return {
        "stages": [
            {"name": "Tenant source DB", "detail": sources},
            {"name": "CDC → Aggregation → Summary store", "detail": checkpoints},
            {"name": "Query API", "detail": {"status": "UP"}},
        ],
        "checked_at": now.isoformat(),
    }
