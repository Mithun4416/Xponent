"""Regression tests for explicit-date parsing in the NL query parser.

Guards the bug where every dated question ("...2026-07-01", "...2026-07-02")
collapsed to the same default "last 30 days" window and returned identical
numbers. A typed date must map to exactly that day/span.

Dependency-free: runs under pytest OR directly
(`python3 tests/correctness/test_nlq_dates.py`) — no DB, no network.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from app.services import nlq  # noqa: E402

UTC = timezone.utc
NOW = datetime(2026, 11, 1, tzinfo=UTC)


def _range(q: str):
    return nlq._resolve_range(q.lower(), NOW)


def test_single_date_is_that_whole_day():
    frm, to, label = _range("give me total order count 2026-07-01")
    assert frm == datetime(2026, 7, 1, tzinfo=UTC), frm
    assert to == datetime(2026, 7, 2, tzinfo=UTC), to       # [00:00, next 00:00)
    assert label == "2026-07-01", label


def test_two_adjacent_dates_differ():
    """The exact bug the user hit: consecutive days must NOT be identical."""
    d1 = _range("total order count 2026-07-01")
    d2 = _range("total order count 2026-07-02")
    assert d1[0] != d2[0] and d1[1] != d2[1], (d1, d2)
    assert d2[0] == datetime(2026, 7, 2, tzinfo=UTC)


def test_explicit_range_from_to():
    frm, to, label = _range("orders from 2026-07-01 to 2026-07-07")
    assert frm == datetime(2026, 7, 1, tzinfo=UTC)
    assert to == datetime(2026, 7, 8, tzinfo=UTC)           # inclusive end -> +1 day
    assert "2026-07-01" in label and "2026-07-07" in label


def test_slash_format_and_reversed_order():
    frm, to, _ = _range("sales 2026/07/10 and 2026/07/05")   # min..max, order-agnostic
    assert frm == datetime(2026, 7, 5, tzinfo=UTC)
    assert to == datetime(2026, 7, 11, tzinfo=UTC)


def test_bare_month_is_whole_month():
    frm, to, label = _range("sales in 2026-07")
    assert frm == datetime(2026, 7, 1, tzinfo=UTC)
    assert to == datetime(2026, 8, 1, tzinfo=UTC)
    assert label == "2026-07"


def test_keyword_ranges_still_work():
    assert _range("my sales this week")[2] == "last 7 days"
    assert _range("orders today")[2] == "today"
    assert _range("orders yesterday")[2] == "yesterday"


def test_dated_question_is_fast_path_confident():
    # A date + an intent word -> rules fast path (instant, no LLM round-trip).
    assert nlq.is_deterministic_confident("total order count 2026-07-01") is True
    assert nlq.is_deterministic_confident("sales in 2026-07") is True
    # Off-topic question -> not confident -> would defer to the LLM if configured.
    assert nlq.is_deterministic_confident("explain quantum tunnelling") is False


def test_invalid_date_does_not_crash_and_falls_back():
    # 2026-13-40 is not a real date; parser ignores it and uses the default window.
    frm, to, label = _range("orders on 2026-13-40")
    assert label == "last 30 days", label


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
