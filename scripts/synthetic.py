"""Deterministic synthetic data model for Xponent (task T2).

No organizer dataset exists, so we generate our own. Every record is derived
*independently* from (global_seed, id) via a per-id RNG, which gives two
properties we rely on:

  1. Reproducibility — same seed/params => byte-identical tables, so any
     correctness mismatch is reproducible.
  2. O(1) memory at 10M scale — the seeder streams rows into COPY without
     holding the dataset in memory, and a second pass can re-derive an order's
     line items identically (guaranteeing orders.total_amount_cents == SUM of
     its order_items.line_amount_cents, an invariant the oracle checks).

This module holds ONLY pure derivation helpers (no DB, no side effects) so the
seeder, the mutation generator, and tests can share one source of truth.
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

STATUSES = ("placed", "shipped", "cancelled")

# A fixed epoch so timestamps are stable across runs regardless of wall clock.
DEFAULT_START = datetime(2026, 7, 1, tzinfo=timezone.utc)

_KNUTH = 2654435761  # multiplicative hash constant for per-id RNG seeding
_MASK64 = (1 << 64) - 1


@dataclass(frozen=True)
class GenParams:
    rows: int
    tenants: int
    seed: int = 42
    span_days: int = 120
    products_per_tenant: int = 1000
    customers_per_tenant: int = 5000
    items_per_order_max: int = 5
    cancel_frac: float = 0.06
    skew_alpha: float = 1.16
    sub_hour_density: bool = True
    start: datetime = DEFAULT_START


def pareto_weights(n_tenants: int, alpha: float) -> list[float]:
    """Power-law tenant weights (a few whales, a long tail). alpha~1.16 ~ 80/20."""
    w = [(i + 1) ** (-alpha) for i in range(n_tenants)]
    s = sum(w)
    return [x / s for x in w]


def _cumulative(weights: list[float]) -> list[float]:
    acc, out = 0.0, []
    for w in weights:
        acc += w
        out.append(acc)
    return out


def _order_rng(seed: int, order_id: int) -> random.Random:
    return random.Random(((seed * _KNUTH) ^ (order_id * 0x9E3779B1)) & _MASK64)


def product_block_start(tenant_id: int, p: GenParams) -> int:
    return (tenant_id - 1) * p.products_per_tenant + 1


def customer_block_start(tenant_id: int, p: GenParams) -> int:
    return (tenant_id - 1) * p.customers_per_tenant + 1


def product_unit_price_cents(product_id: int) -> int:
    """Deterministic price in [500, 50000) derived from the product id alone."""
    return 500 + ((product_id * _KNUTH) % 49500)


def product_name(product_id: int) -> str:
    return f"Product {product_id}"


def product_sku(tenant_id: int, local_index: int) -> str:
    return f"SKU-{tenant_id}-{local_index:06d}"


@dataclass(frozen=True)
class OrderItem:
    product_id: int
    quantity: int
    line_amount_cents: int


@dataclass(frozen=True)
class Order:
    order_id: int
    tenant_id: int
    customer_id: int
    status: str
    total_amount_cents: int
    created_at: datetime
    items: tuple[OrderItem, ...]


def pick_tenant(r: random.Random, cum_weights: list[float]) -> int:
    x = r.random()
    lo, hi = 0, len(cum_weights) - 1
    while lo < hi:  # binary search the cumulative weight bucket
        mid = (lo + hi) // 2
        if x <= cum_weights[mid]:
            hi = mid
        else:
            lo = mid + 1
    return lo + 1  # tenant_id is 1-based


def gen_order(order_id: int, p: GenParams, cum_weights: list[float]) -> Order:
    """Derive one complete order (and its line items) deterministically.

    Load-time status is mostly placed/shipped; most cancellations arrive later
    via the mutation stream so retraction is exercised as a live transition.
    """
    r = _order_rng(p.seed, order_id)
    tenant_id = pick_tenant(r, cum_weights)

    # Timestamp to the second (sub_hour_density keeps sub-minute offsets too, so
    # non-aligned ranges have non-empty boundary buckets and AC-02 isn't vacuous).
    offset_s = r.randrange(p.span_days * 86400)
    created = p.start + timedelta(seconds=offset_s)
    if p.sub_hour_density:
        created = created + timedelta(microseconds=r.randrange(1_000_000))

    customer_id = customer_block_start(tenant_id, p) + r.randrange(p.customers_per_tenant)

    n_items = r.randint(1, p.items_per_order_max)
    items: list[OrderItem] = []
    total = 0
    pstart = product_block_start(tenant_id, p)
    for _ in range(n_items):
        product_id = pstart + r.randrange(p.products_per_tenant)
        qty = r.randint(1, 5)
        line = qty * product_unit_price_cents(product_id)
        total += line
        items.append(OrderItem(product_id, qty, line))

    if r.random() < p.cancel_frac * 0.3:
        status = "cancelled"
    else:
        status = "shipped" if r.random() < 0.5 else "placed"

    return Order(order_id, tenant_id, customer_id, status, total, created, tuple(items))
