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
    assert _range("my sales this week")[2] == "this week"
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


def test_last_n_months_differ():
    """The reported bug: 'last six months' and 'last seven months' must differ."""
    six = _range("compare restaurants last six months")
    seven = _range("compare restaurants last seven months")
    assert six[0] != seven[0], (six, seven)
    assert six[2] == "last 6 months" and seven[2] == "last 7 months", (six[2], seven[2])
    # seven months reaches further back than six.
    assert seven[0] < six[0]


def test_relative_units():
    assert _range("sales last 2 weeks")[2] == "last 2 weeks"
    assert _range("orders past 3 days")[2] == "last 3 days"
    assert _range("revenue last quarter")[2] == "last 1 quarter"
    assert _range("orders in the last 5 hours")[2] == "last 5 hours"


def test_named_month_and_span():
    frm, to, label = _range("sales in july")
    assert frm == datetime(2026, 7, 1, tzinfo=UTC)
    assert to == datetime(2026, 8, 1, tzinfo=UTC)
    frm, to, _ = _range("orders between june and august")
    assert frm == datetime(2026, 6, 1, tzinfo=UTC)
    assert to == datetime(2026, 9, 1, tzinfo=UTC)


def test_calendar_periods():
    # this year = Jan 1 .. now; last year = whole previous year.
    assert _range("sales this year")[0] == datetime(2026, 1, 1, tzinfo=UTC)
    ly = _range("sales last year")
    assert ly[0] == datetime(2025, 1, 1, tzinfo=UTC) and ly[1] == datetime(2026, 1, 1, tzinfo=UTC)
    # last month = previous full calendar month (Oct, since NOW is Nov 1).
    lm = _range("orders last month")
    assert lm[0] == datetime(2026, 10, 1, tzinfo=UTC) and lm[1] == datetime(2026, 11, 1, tzinfo=UTC)


def test_org_vs_seller_routing():
    def plan(qq):
        return nlq.parse_deterministic(qq, NOW)
    # Ranking restaurants -> org compare, NOT a random seller query.
    assert plan("top tenant between two months").scope == "org"
    assert plan("which restaurant sold most this month").metric == "org_compare"
    assert plan("compare restaurants last six months").scope == "org"
    # Product ranking stays a seller query.
    assert plan("top products this month").metric == "top_products"
    assert plan("top products this month").scope == "seller"
    # A specific restaurant pins the seller.
    assert plan("sales for restaurant 2 in july").tenant_id == 2


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
