"""Freshness watermark block attached to every response (Bonus B-1, task T7)."""
from __future__ import annotations

from datetime import datetime, timezone

import asyncpg

from app.core import config


async def freshness_block(summary_pool: asyncpg.Pool, slot: str | None = None) -> dict:
    row = await summary_pool.fetchrow(
        "SELECT as_of_timestamp, last_applied_lsn_str FROM cdc_checkpoint WHERE slot_name = $1",
        slot or config.SLOT_NAME,
    )
    now = datetime.now(timezone.utc)
    if row is None:
        return {"as_of": None, "last_applied_lsn": "0/0", "lag_ms": None,
                "human_readable": "no checkpoint yet", "status": "STALE_WARNING"}
    as_of = row["as_of_timestamp"]
    lag_ms = max(0, int((now - as_of).total_seconds() * 1000))
    return {
        "as_of": as_of.isoformat(),
        "last_applied_lsn": row["last_applied_lsn_str"],
        "lag_ms": lag_ms,
        "human_readable": f"updated {lag_ms / 1000.0:.1f} seconds ago",
        "status": "FRESH" if lag_ms < config.STALE_AFTER_MS else "STALE_WARNING",
    }
