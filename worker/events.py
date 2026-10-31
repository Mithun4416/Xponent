"""Normalized CDC change events (transport-agnostic).

The replication transport (wal2json today; swappable) parses raw logical-decoding
output into these typed structs so the coalescer never sees wire format. Values
are already typed: created_at is a tz-aware UTC datetime, amounts are ints.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional


@dataclass(frozen=True)
class OrderRow:
    tenant_id: int
    order_id: int
    status: str
    total_amount_cents: int
    created_at: datetime


@dataclass(frozen=True)
class ItemRow:
    tenant_id: int
    order_id: int
    product_id: int
    quantity: int
    line_amount_cents: int
    order_created_at: datetime


@dataclass(frozen=True)
class ChangeEvent:
    """One row-level change. op in {'I','U','D'}; table in {'orders','order_items'}.

    old/new are OrderRow or ItemRow (or None): insert has new only, delete has old
    only, update has both (old = before-image from REPLICA IDENTITY FULL).
    """
    lsn: int
    commit_ts: datetime
    table: str
    op: str
    old: Optional[object] = None
    new: Optional[object] = None
