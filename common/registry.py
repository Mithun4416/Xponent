"""Tenant-source registry — the per-tenant-database topology (reviewer model).

Each tenant may have its OWN source database. Xponent ingests from all of them
into one central, tenant-keyed summary store. This registry maps each tenant to
its source DSN + replication slot. Shared by the worker, bootstrap, and the API.

Configuration: env `TENANT_SOURCES` = a JSON array, e.g.
  [{"tenant_id":1,"dsn":"postgresql://.../tenant_1","slot":"agg_slot_t1"},
   {"tenant_id":2,"dsn":"postgresql://.../tenant_2","slot":"agg_slot_t2"}]

If `TENANT_SOURCES` is unset, the registry falls back to a SINGLE shared source
(tenant_id=None) using the given default DSN/slot — i.e. the original
shared-table topology. This keeps the existing single-source prototype working
unchanged; the per-tenant-DB model is opt-in via the env var.

When an entry's tenant_id is set, the worker/bootstrap TAG events with it (the
source database *is* the tenant), ignoring any tenant_id column in the source.
When it is None, tenant_id is read from the row (shared-table mode).
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class SourceEntry:
    tenant_id: int | None   # None => shared source; read tenant_id from the row
    dsn: str
    slot: str

    @property
    def label(self) -> str:
        return f"tenant={self.tenant_id}" if self.tenant_id is not None else "shared"


def load_sources(default_dsn: str, default_slot: str, env: str | None = None) -> list[SourceEntry]:
    raw = env if env is not None else os.getenv("TENANT_SOURCES", "")
    raw = raw.strip()
    if not raw:
        return [SourceEntry(tenant_id=None, dsn=default_dsn, slot=default_slot)]
    data = json.loads(raw)
    out: list[SourceEntry] = []
    for i, e in enumerate(data):
        tid = e.get("tenant_id")
        dsn = e["dsn"]
        slot = e.get("slot") or f"agg_slot_t{tid if tid is not None else i}"
        out.append(SourceEntry(tenant_id=(int(tid) if tid is not None else None), dsn=dsn, slot=slot))
    if not out:
        return [SourceEntry(tenant_id=None, dsn=default_dsn, slot=default_slot)]
    return out


def source_for_tenant(sources: list[SourceEntry], tenant_id: int) -> SourceEntry:
    """Resolve which source DB holds a tenant's data. Exact tenant match wins;
    otherwise the shared source (tenant_id=None) if present; else the first entry."""
    for s in sources:
        if s.tenant_id == tenant_id:
            return s
    for s in sources:
        if s.tenant_id is None:
            return s
    return sources[0]
