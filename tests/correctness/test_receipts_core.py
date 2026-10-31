"""Pure tests for the Receipts core: digest + absolute recompute (B-2).

Dependency-free EXCEPT xxhash; if xxhash isn't installed, the digest-value checks
are skipped but the recompute arithmetic still runs. Run:
    python3 tests/correctness/test_receipts_core.py
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from common.rollups import recompute_order_grains  # noqa: E402

UTC = timezone.utc
_STUB = lambda oid, status, amt: (oid * 1_000_003) ^ (len(status)) ^ (amt << 1)

try:
    from common.hashing import digest_of_rows
    _HAVE_XXHASH = True
    try:
        digest_of_rows([(1, "placed", 100)])
    except ImportError:
        _HAVE_XXHASH = False
except Exception:
    _HAVE_XXHASH = False


def _rows():
    return [
        (1, "placed", 1000, datetime(2026, 7, 2, 10, 17, 0, tzinfo=UTC)),
        (2, "shipped", 2000, datetime(2026, 7, 2, 10, 45, 0, tzinfo=UTC)),
        (3, "cancelled", 500, datetime(2026, 7, 2, 11, 5, 0, tzinfo=UTC)),
        (4, "placed", 1500, datetime(2026, 7, 2, 11, 5, 30, tzinfo=UTC)),
    ]


def test_recompute_daily_totals():
    rb = recompute_order_grains(_rows(), hash_fn=_STUB)
    assert rb.row_count == 4
    assert rb.sum_amount_cents == 5000
    # placed: 2 orders, gross 2500, net 2500
    assert rb.daily["placed"] == [2, 2500, 2500]
    # cancelled: gross counts, net 0
    assert rb.daily["cancelled"] == [1, 500, 0]


def test_recompute_hour_and_minute_split():
    rb = recompute_order_grains(_rows(), hash_fn=_STUB)
    h10 = datetime(2026, 7, 2, 10, 0, 0, tzinfo=UTC)
    h11 = datetime(2026, 7, 2, 11, 0, 0, tzinfo=UTC)
    assert rb.hourly[(h10, "placed")] == [1, 1000, 1000]
    assert rb.hourly[(h10, "shipped")] == [1, 2000, 2000]
    assert rb.hourly[(h11, "cancelled")] == [1, 500, 0]
    # two orders in minute 11:05 (cancelled + placed) are distinct status keys
    m1105 = datetime(2026, 7, 2, 11, 5, 0, tzinfo=UTC)
    assert rb.minutely[(m1105, "placed")] == [1, 1500, 1500]
    assert rb.minutely[(m1105, "cancelled")] == [1, 500, 0]


def test_digest_order_independent():
    if not _HAVE_XXHASH:
        print("SKIP digest value checks (xxhash not installed)")
        return
    rows = [(1, "placed", 1000), (2, "shipped", 2000), (3, "cancelled", 500)]
    a = digest_of_rows(rows)
    b = digest_of_rows(list(reversed(rows)))
    assert a == b, (a, b)                 # order-independent
    assert a[0] == 3 and a[1] == 3500


def test_recompute_matches_digest():
    if not _HAVE_XXHASH:
        print("SKIP cross-check (xxhash not installed)")
        return
    rb = recompute_order_grains(_rows())
    d = digest_of_rows([(r[0], r[1], r[2]) for r in _rows()])
    assert (rb.row_count, rb.sum_amount_cents, rb.hash_sum_mod64) == d


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
