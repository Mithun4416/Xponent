"""Unit tests for the CDC coalescer's Z-set / digest arithmetic (task T4).

Dependency-free: a stub hash_fn is injected so the test runs without xxhash.
Verifies insert/update(cancel)/delete produce correct signed deltas across all
three order grains and the digest, that a status-change update retracts the old
bucket and inserts the new one, and that item insert/delete drive product deltas.

Run: python3 tests/correctness/test_coalescer.py   (or under pytest)
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from worker.coalescer import MicroBatchCoalescer  # noqa: E402
from worker.events import ChangeEvent, ItemRow, OrderRow  # noqa: E402

UTC = timezone.utc
# Stub hash: deterministic, no xxhash needed. Distinct per (id,status,amount).
_HASH = lambda oid, status, amt: (oid * 1_000_003) ^ (hash(status) & 0xFFFF) ^ (amt << 1)


def _order(tenant, oid, status, amt, dt):
    return OrderRow(tenant, oid, status, amt, dt)


def _collect(coalescer):
    daily = {(t, b, s): (c, g, n) for g_, t, b, s, c, g, n in coalescer.order_rows() if g_ == "daily"}
    hourly = {(t, b, s): (c, g, n) for g_, t, b, s, c, g, n in coalescer.order_rows() if g_ == "hourly"}
    minutely = {(t, b, s): (c, g, n) for g_, t, b, s, c, g, n in coalescer.order_rows() if g_ == "minutely"}
    return daily, hourly, minutely


def test_insert_adds_all_grains_and_digest():
    c = MicroBatchCoalescer(hash_fn=_HASH)
    dt = datetime(2026, 7, 2, 10, 17, 23, tzinfo=UTC)
    c.apply(ChangeEvent(lsn=1, commit_ts=dt, table="orders", op="I",
                        new=_order(42, 9001, "placed", 15000, dt)))
    daily, hourly, minutely = _collect(c)
    assert daily[(42, dt.date(), "placed")] == (1, 15000, 15000)
    assert hourly[(42, dt.replace(minute=0, second=0, microsecond=0), "placed")] == (1, 15000, 15000)
    assert minutely[(42, dt.replace(second=0, microsecond=0), "placed")] == (1, 15000, 15000)
    digests = {(t, d): (rc, s, h) for t, d, rc, s, h in c.digest_rows()}
    assert digests[(42, dt.date())][0] == 1 and digests[(42, dt.date())][1] == 15000


def test_cancel_update_retracts_placed_inserts_cancelled():
    c = MicroBatchCoalescer(hash_fn=_HASH)
    dt = datetime(2026, 7, 2, 10, 17, 23, tzinfo=UTC)
    old = _order(42, 9001, "placed", 15000, dt)
    new = _order(42, 9001, "cancelled", 15000, dt)
    c.apply(ChangeEvent(lsn=2, commit_ts=dt, table="orders", op="U", old=old, new=new))
    daily, _, _ = _collect(c)
    # placed: -1 count, -gross, -net ; cancelled: +1 count, +gross, net stays 0
    assert daily[(42, dt.date(), "placed")] == (-1, -15000, -15000)
    assert daily[(42, dt.date(), "cancelled")] == (1, 15000, 0)
    # digest net change: row_count delta 0 (one out, one in), sum delta 0, hash changes.
    digests = {(t, d): (rc, s, h) for t, d, rc, s, h in c.digest_rows()}
    rc, s, h = digests[(42, dt.date())]
    assert rc == 0 and s == 0 and h != 0


def test_delete_retracts():
    c = MicroBatchCoalescer(hash_fn=_HASH)
    dt = datetime(2026, 7, 2, 10, 0, 0, tzinfo=UTC)
    c.apply(ChangeEvent(lsn=3, commit_ts=dt, table="orders", op="D",
                        old=_order(7, 500, "shipped", 2000, dt)))
    daily, _, _ = _collect(c)
    assert daily[(7, dt.date(), "shipped")] == (-1, -2000, -2000)


def test_created_at_change_moves_bucket():
    c = MicroBatchCoalescer(hash_fn=_HASH)
    old_dt = datetime(2026, 7, 2, 10, 0, 0, tzinfo=UTC)
    new_dt = datetime(2026, 7, 5, 11, 0, 0, tzinfo=UTC)
    c.apply(ChangeEvent(lsn=4, commit_ts=new_dt, table="orders", op="U",
                        old=_order(1, 10, "placed", 500, old_dt),
                        new=_order(1, 10, "placed", 500, new_dt)))
    daily, _, _ = _collect(c)
    assert daily[(1, old_dt.date(), "placed")] == (-1, -500, -500)
    assert daily[(1, new_dt.date(), "placed")] == (1, 500, 500)


def test_item_insert_and_delete_drive_products():
    c = MicroBatchCoalescer(hash_fn=_HASH)
    dt = datetime(2026, 7, 2, 9, 0, 0, tzinfo=UTC)
    c.apply(ChangeEvent(lsn=5, commit_ts=dt, table="order_items", op="I",
                        new=ItemRow(3, 77, 5012, 2, 8000, dt)))
    prods = {(t, d, p): (oc, q, g, n) for t, d, p, oc, q, g, n in c.product_rows()}
    assert prods[(3, dt.date(), 5012)] == (1, 2, 8000, 8000)
    c.apply(ChangeEvent(lsn=6, commit_ts=dt, table="order_items", op="D",
                        old=ItemRow(3, 77, 5012, 2, 8000, dt)))
    prods = {(t, d, p): (oc, q, g, n) for t, d, p, oc, q, g, n in c.product_rows()}
    assert (3, dt.date(), 5012) not in prods  # net-zero delta is dropped


def test_lsn_and_commit_tracking():
    c = MicroBatchCoalescer(hash_fn=_HASH)
    t1 = datetime(2026, 7, 2, 10, 0, 0, tzinfo=UTC)
    t2 = datetime(2026, 7, 2, 10, 5, 0, tzinfo=UTC)
    c.apply(ChangeEvent(lsn=10, commit_ts=t1, table="orders", op="I", new=_order(1, 1, "placed", 100, t1)))
    c.apply(ChangeEvent(lsn=25, commit_ts=t2, table="orders", op="I", new=_order(1, 2, "placed", 200, t2)))
    assert c.max_lsn == 25
    assert c.latest_commit_ts == t2


def _run_all() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in tests:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {fn.__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run_all())
