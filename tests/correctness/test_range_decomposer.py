"""Property + unit tests for the range decomposer (task T6).

Dependency-free: runs under pytest OR directly (`python3 tests/correctness/test_range_decomposer.py`)
so it can be validated without installing anything. The core property: the
pieces are pairwise disjoint and union EXACTLY to the requested [frm, to).
"""
from __future__ import annotations

import os
import random
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from app.services.range_decomposer import decompose  # noqa: E402

UTC = timezone.utc


def _assert_covers_exactly(frm: datetime, to: datetime) -> None:
    plan = decompose(frm, to)
    intervals = plan.as_intervals()
    if frm >= to:
        assert intervals == [], (frm, to, intervals)
        return
    # Contiguous, disjoint, and exactly [frm, to).
    assert intervals[0][0] == frm, ("left edge", frm, intervals)
    assert intervals[-1][1] == to, ("right edge", to, intervals)
    for (a_start, a_end), (b_start, b_end) in zip(intervals, intervals[1:]):
        assert a_end == b_start, ("gap/overlap", (a_start, a_end), (b_start, b_end))
        assert a_start < a_end, ("empty/neg piece", (a_start, a_end))


def test_random_ranges() -> None:
    rng = random.Random(1234)
    base = datetime(2026, 7, 1, tzinfo=UTC)
    for _ in range(5000):
        a = base + timedelta(seconds=rng.randrange(6 * 86400), microseconds=rng.randrange(1_000_000))
        b = base + timedelta(seconds=rng.randrange(6 * 86400), microseconds=rng.randrange(1_000_000))
        frm, to = (a, b) if a <= b else (b, a)
        _assert_covers_exactly(frm, to)


def test_aligned_ranges_touch_no_primary() -> None:
    # Whole-day aligned range: only day buckets, no edges, no primary scan.
    frm = datetime(2026, 7, 2, 0, 0, 0, tzinfo=UTC)
    to = datetime(2026, 7, 9, 0, 0, 0, tzinfo=UTC)
    plan = decompose(frm, to)
    assert plan.days == [(frm.date(), (to - timedelta(days=1)).date())]
    assert plan.hours == [] and plan.minutes == [] and plan.edges == []
    assert plan.touches_primary is False
    _assert_covers_exactly(frm, to)


def test_minute_aligned_no_edges() -> None:
    # Minute-aligned (but not hour-aligned) boundaries -> no sub-minute edges.
    frm = datetime(2026, 7, 2, 10, 17, 0, tzinfo=UTC)
    to = datetime(2026, 7, 2, 14, 42, 0, tzinfo=UTC)
    plan = decompose(frm, to)
    assert plan.edges == []
    assert plan.touches_primary is False
    _assert_covers_exactly(frm, to)


def test_non_aligned_canonical_case() -> None:
    # The headline AC-02 case: 10:17:23 -> 14:42:09 with sub-minute parts.
    frm = datetime(2026, 7, 2, 10, 17, 23, tzinfo=UTC)
    to = datetime(2026, 7, 5, 14, 42, 9, tzinfo=UTC)
    plan = decompose(frm, to)
    assert plan.edges, "sub-second boundaries must produce sub-minute edges"
    assert plan.days, "multi-day span must produce interior day buckets"
    assert plan.touches_primary is True
    _assert_covers_exactly(frm, to)


def test_sub_minute_window() -> None:
    frm = datetime(2026, 7, 2, 10, 17, 23, tzinfo=UTC)
    to = datetime(2026, 7, 2, 10, 17, 45, tzinfo=UTC)
    plan = decompose(frm, to)
    assert plan.edges == [(frm, to)]
    assert plan.days == [] and plan.hours == [] and plan.minutes == []
    _assert_covers_exactly(frm, to)


def test_empty_and_inverted() -> None:
    t = datetime(2026, 7, 2, 10, 0, 0, tzinfo=UTC)
    assert decompose(t, t).as_intervals() == []
    assert decompose(t, t - timedelta(hours=1)).as_intervals() == []


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
