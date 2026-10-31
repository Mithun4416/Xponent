"""In-memory micro-batch coalescer — the correctness core of the CDC worker (task T4).

Pure and transport-free so it is unit-testable without a database. Converts a
stream of ChangeEvents into net Z-set deltas per summary bucket, plus incremental
verification-digest deltas. The idempotency guard (drop events with
lsn <= last_applied_lsn) is applied by the caller BEFORE events reach here, per
event (fix H1), so the coalescer only ever sees new changes.

Z-set algebra (DBSP): insert = +1 (new row), delete = -1 (old row),
update = -1 (old) + +1 (new). COUNT and SUM are distributive, so each touched
bucket is adjusted by the signed measure. An update whose status / amount /
created_at changed naturally retracts from the OLD bucket (using old.created_at,
fix M1) and inserts into the NEW bucket.

Sales semantics (decision D2, gross default):
  * order rollups carry both gross (all statuses) and net (excl. cancelled).
  * product rollups are driven by order_items insert/delete and ranked by GROSS
    revenue; live net-product maintenance under later order cancellation is a
    documented P2 gap (digests/order rollups remain exact regardless).

The digest covers ORDERS only (canonical hash of order_id:status:amount), so
item events don't touch digests.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Callable

from worker.events import ChangeEvent, ItemRow, OrderRow
from worker.hashing import row_hash64

_MOD = 1 << 64


def _day(dt: datetime):
    return dt.date()


def _hour(dt: datetime) -> datetime:
    return dt.replace(minute=0, second=0, microsecond=0)


def _minute(dt: datetime) -> datetime:
    return dt.replace(second=0, microsecond=0)


def _net(status: str, amount: int) -> int:
    return amount if status != "cancelled" else 0


class MicroBatchCoalescer:
    def __init__(self, hash_fn: Callable[[int, str, int], int] = row_hash64) -> None:
        self._hash_fn = hash_fn
        self.reset()

    def reset(self) -> None:
        # value: [count, gross, net]
        self.daily: dict = defaultdict(lambda: [0, 0, 0])
        self.hourly: dict = defaultdict(lambda: [0, 0, 0])
        self.minutely: dict = defaultdict(lambda: [0, 0, 0])
        # product value: [order_count, qty, gross, net]
        self.products: dict = defaultdict(lambda: [0, 0, 0, 0])
        # digest value: [row_count, sum_amount, hash_delta]
        self.digests: dict = defaultdict(lambda: [0, 0, 0])
        # per-event activity log for the live feed: (tenant_id, op, table, order_id, lsn, commit_ts)
        self.activity: list = []
        self.max_lsn = 0
        self.latest_commit_ts: datetime | None = None
        self._events = 0

    # --- public API ---------------------------------------------------------
    def apply(self, ev: ChangeEvent) -> None:
        self.max_lsn = max(self.max_lsn, ev.lsn)
        if self.latest_commit_ts is None or ev.commit_ts > self.latest_commit_ts:
            self.latest_commit_ts = ev.commit_ts
        self._events += 1
        row = ev.new or ev.old
        if row is not None:
            self.activity.append((row.tenant_id, ev.op, ev.table, getattr(row, "order_id", None),
                                  ev.lsn, ev.commit_ts))
        if ev.table == "orders":
            if ev.old is not None:
                self._apply_order(ev.old, sign=-1)
            if ev.new is not None:
                self._apply_order(ev.new, sign=+1)
        elif ev.table == "order_items":
            if ev.old is not None:
                self._apply_item(ev.old, sign=-1)
            if ev.new is not None:
                self._apply_item(ev.new, sign=+1)
        # other tables (e.g. products catalog) do not affect rollups/digests.

    def is_empty(self) -> bool:
        return self._events == 0

    @property
    def event_count(self) -> int:
        return self._events

    # --- order path ---------------------------------------------------------
    def _apply_order(self, row: OrderRow, sign: int) -> None:
        gross = sign * row.total_amount_cents
        net = sign * _net(row.status, row.total_amount_cents)

        for store, key in (
            (self.daily, (row.tenant_id, _day(row.created_at), row.status)),
            (self.hourly, (row.tenant_id, _hour(row.created_at), row.status)),
            (self.minutely, (row.tenant_id, _minute(row.created_at), row.status)),
        ):
            v = store[key]
            v[0] += sign
            v[1] += gross
            v[2] += net

        d = self.digests[(row.tenant_id, _day(row.created_at))]
        h = self._hash_fn(row.order_id, row.status, row.total_amount_cents)
        d[0] += sign
        d[1] += gross
        d[2] = (d[2] + sign * h) % _MOD

    # --- product path -------------------------------------------------------
    def _apply_item(self, row: ItemRow, sign: int) -> None:
        v = self.products[(row.tenant_id, _day(row.order_created_at), row.product_id)]
        v[0] += sign                              # order_count (best-effort; ranking uses revenue)
        v[1] += sign * row.quantity               # qty_sold
        v[2] += sign * row.line_amount_cents      # gross_revenue
        v[3] += sign * row.line_amount_cents      # net_revenue (treated non-cancelled at item time)

    # --- flush payload ------------------------------------------------------
    def order_rows(self):
        """Yield (grain, tenant_id, bucket, status, dcount, dgross, dnet) for non-zero deltas."""
        for grain, store in (("daily", self.daily), ("hourly", self.hourly), ("minutely", self.minutely)):
            for (tenant_id, bucket, status), (c, g, n) in store.items():
                if c or g or n:
                    yield grain, tenant_id, bucket, status, c, g, n

    def product_rows(self):
        for (tenant_id, day, product_id), (c, q, g, n) in self.products.items():
            if c or q or g or n:
                yield tenant_id, day, product_id, c, q, g, n

    def digest_rows(self):
        for (tenant_id, day), (c, s, h) in self.digests.items():
            if c or s or h:
                yield tenant_id, day, c, s, h % _MOD
