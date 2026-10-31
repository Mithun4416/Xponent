"""Canonical verification hashing — THE single definition (Receipts feature, B-2).

Shared by every component that must agree: bootstrap digest seeding, the CDC
worker's incremental digest, and the API verifier/repair. Both container images
(app and worker) copy `common/`, so there is exactly one implementation.

Digest of a (tenant, day) bucket = ( sum of row_hash64(row) ) mod 2**64.
Modular SUM (not XOR) handles multiplicities and makes updates O(1):
  update => -hash(old) + hash(new);  retraction => -hash(old).
Row hash covers order_id, status, amount_cents.
"""
from __future__ import annotations

from typing import Iterable, Tuple

MOD = 1 << 64


def row_hash64(order_id: int, status: str, amount_cents: int) -> int:
    """64-bit xxhash of "order_id:status:amount_cents" (xxhash imported lazily)."""
    import xxhash
    return xxhash.xxh64(f"{order_id}:{status}:{amount_cents}".encode("utf-8")).intdigest()


def digest_add(acc: int, order_id: int, status: str, amount_cents: int) -> int:
    return (acc + row_hash64(order_id, status, amount_cents)) % MOD


def digest_remove(acc: int, order_id: int, status: str, amount_cents: int) -> int:
    return (acc - row_hash64(order_id, status, amount_cents)) % MOD


def digest_of_rows(rows: Iterable[Tuple[int, str, int]]) -> Tuple[int, int, int]:
    """Compute (row_count, sum_amount_cents, hash_sum_mod64) over source rows.

    Order-independent: the result does not depend on iteration order, so it can be
    compared directly to the incrementally-maintained bucket_digests value.
    """
    count = 0
    total = 0
    hsum = 0
    for order_id, status, amount in rows:
        count += 1
        total += int(amount)
        hsum = (hsum + row_hash64(int(order_id), str(status), int(amount))) % MOD
    return count, total, hsum
