"""Per-tenant 'verified' badge for API responses (Receipts, B-2).

A cheap, indexed read of bucket_digests so every dashboard response can say
whether the tenant's rollups are currently attested or have a flagged drift.
bucket_digests is not RLS-protected, so queries are explicitly tenant-scoped.
"""
from __future__ import annotations


async def tenant_badge(summary_pool, tenant_id: int) -> dict:
    row = await summary_pool.fetchrow(
        "SELECT COUNT(*)::bigint total, "
        "COUNT(*) FILTER (WHERE is_corrupted)::bigint corrupted, "
        "MAX(verified_at) last_verified "
        "FROM bucket_digests WHERE tenant_id=$1",
        tenant_id,
    )
    total = int(row["total"]) if row else 0
    corrupted = int(row["corrupted"]) if row else 0
    return {
        "verified": corrupted == 0,
        "total_buckets": total,
        "corrupted_buckets": corrupted,
        "last_verified_at": row["last_verified"].isoformat() if row and row["last_verified"] else None,
    }
