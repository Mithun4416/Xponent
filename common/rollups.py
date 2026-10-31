"""Pure absolute recompute of one (tenant, day) bucket from source order rows.

Used by targeted repair (Receipts): given the day's source orders, recompute the
exact daily/hourly/minutely order rollups and the bucket digest, which then
atomically overwrite the (possibly drifted) summary rows. Pure and unit-tested so
the repair arithmetic is verified without a database.

net = amount when status != 'cancelled' else 0 (decision D2; gross carries all).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from common.hashing import MOD, row_hash64


def _net(status: str, amount: int) -> int:
    return amount if status != "cancelled" else 0


@dataclass
class RecomputedBucket:
    # keyed by status -> [count, gross, net]
    daily: dict = field(default_factory=dict)
    # keyed by (hour_ts, status) / (minute_ts, status) -> [count, gross, net]
    hourly: dict = field(default_factory=dict)
    minutely: dict = field(default_factory=dict)
    row_count: int = 0
    sum_amount_cents: int = 0
    hash_sum_mod64: int = 0


def recompute_order_grains(rows, hash_fn: Callable[[int, str, int], int] = row_hash64) -> RecomputedBucket:
    """rows: iterable of (order_id, status, amount_cents, created_at:datetime).

    hash_fn is injectable so the recompute arithmetic can be unit-tested without
    xxhash; production uses the canonical row_hash64.
    """
    rb = RecomputedBucket()
    for order_id, status, amount, created_at in rows:
        amount = int(amount)
        net = _net(status, amount)
        hour_ts = created_at.replace(minute=0, second=0, microsecond=0)
        minute_ts = created_at.replace(second=0, microsecond=0)

        d = rb.daily.setdefault(status, [0, 0, 0])
        d[0] += 1
        d[1] += amount
        d[2] += net

        h = rb.hourly.setdefault((hour_ts, status), [0, 0, 0])
        h[0] += 1
        h[1] += amount
        h[2] += net

        m = rb.minutely.setdefault((minute_ts, status), [0, 0, 0])
        m[0] += 1
        m[1] += amount
        m[2] += net

        rb.row_count += 1
        rb.sum_amount_cents += amount
        rb.hash_sum_mod64 = (rb.hash_sum_mod64 + hash_fn(int(order_id), str(status), amount)) % MOD
    return rb
