"""Hybrid time-range decomposer (task T6) — the shared module ALL four query
endpoints use so non-aligned ranges stay exact (fixes gaps C1 and H4).

Convention: every range is half-open ``[start, end)`` in UTC. The API and the
correctness oracle both use ``created_at >= from AND created_at < to`` so the
two agree bit-for-bit.

A query window ``[frm, to)`` is split, by a layered minute→hour→day peel, into
disjoint pieces that exactly cover it:

    edges   sub-minute residual(s)   -> primary index-only scan (per-row)
    minutes minute-aligned residual  -> agg_orders_minutely
    hours   hour-aligned residual    -> agg_orders_hourly
    days    full interior days       -> agg_orders_daily

Minute buckets (decision D1) keep all sub-hour evaluation inside the summary
store; the only time the primary is touched is a sub-minute residual (< 60 s on
each side), and only when the caller's timestamps are not minute-aligned. If the
team chooses to require minute-aligned query boundaries, ``edges`` is always
empty and the primary is never read.

The pieces are guaranteed disjoint and to union exactly to ``[frm, to)`` — see
tests/correctness/test_range_decomposer.py.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

_MIN = timedelta(minutes=1)
_HOUR = timedelta(hours=1)
_DAY = timedelta(days=1)


def _floor_minute(dt: datetime) -> datetime:
    return dt.replace(second=0, microsecond=0)


def _floor_hour(dt: datetime) -> datetime:
    return dt.replace(minute=0, second=0, microsecond=0)


def _floor_day(dt: datetime) -> datetime:
    return dt.replace(hour=0, minute=0, second=0, microsecond=0)


def _ceil(dt: datetime, floor_fn, step: timedelta) -> datetime:
    f = floor_fn(dt)
    return f if f == dt else f + step


@dataclass
class RangePlan:
    """Disjoint pieces covering the requested [frm, to) exactly.

    days:    list of (start_date, end_date_inclusive) for agg_orders_daily
    hours:   list of (start_dt, end_dt) half-open for agg_orders_hourly
    minutes: list of (start_dt, end_dt) half-open for agg_orders_minutely
    edges:   list of (start_dt, end_dt) half-open sub-minute residuals (primary)
    """
    frm: datetime
    to: datetime
    days: list[tuple[date, date]] = field(default_factory=list)
    hours: list[tuple[datetime, datetime]] = field(default_factory=list)
    minutes: list[tuple[datetime, datetime]] = field(default_factory=list)
    edges: list[tuple[datetime, datetime]] = field(default_factory=list)

    @property
    def touches_primary(self) -> bool:
        return bool(self.edges)

    def as_intervals(self) -> list[tuple[datetime, datetime]]:
        """All pieces as half-open [start, end) datetimes (days expanded). For tests."""
        out: list[tuple[datetime, datetime]] = []
        out.extend(self.edges)
        out.extend(self.minutes)
        out.extend(self.hours)
        for d0, d1 in self.days:
            start = datetime(d0.year, d0.month, d0.day, tzinfo=timezone.utc)
            end = datetime(d1.year, d1.month, d1.day, tzinfo=timezone.utc) + _DAY
            out.append((start, end))
        out.sort()
        return out


def decompose(frm: datetime, to: datetime) -> RangePlan:
    """Decompose [frm, to) (UTC, half-open) into day/hour/minute/edge pieces."""
    if frm.tzinfo is None or to.tzinfo is None:
        raise ValueError("decompose requires timezone-aware UTC datetimes")
    frm = frm.astimezone(timezone.utc)
    to = to.astimezone(timezone.utc)
    plan = RangePlan(frm=frm, to=to)
    if frm >= to:
        return plan

    # --- Layer 1: peel sub-minute residuals, leaving a minute-aligned core ---
    core_start = _ceil(frm, _floor_minute, _MIN)
    core_end = _floor_minute(to)
    if core_start >= core_end:
        # Entire window is under one minute wide (or has no minute-aligned core).
        plan.edges.append((frm, to))
        return plan
    if frm < core_start:
        plan.edges.append((frm, core_start))
    if core_end < to:
        plan.edges.append((core_end, to))

    # --- Layer 2: peel minute residuals, leaving an hour-aligned core --------
    h_start = _ceil(core_start, _floor_hour, _HOUR)
    h_end = _floor_hour(core_end)
    if h_start >= h_end:
        plan.minutes.append((core_start, core_end))
        return plan
    if core_start < h_start:
        plan.minutes.append((core_start, h_start))
    if h_end < core_end:
        plan.minutes.append((h_end, core_end))

    # --- Layer 3: peel hour residuals, leaving a day-aligned core ------------
    d_start = _ceil(h_start, _floor_day, _DAY)
    d_end = _floor_day(h_end)
    if d_start >= d_end:
        plan.hours.append((h_start, h_end))
        return plan
    if h_start < d_start:
        plan.hours.append((h_start, d_start))
    if d_end < h_end:
        plan.hours.append((d_end, h_end))

    # --- Day core: [d_start, d_end) are both UTC midnights -------------------
    plan.days.append((d_start.date(), (d_end - _DAY).date()))
    return plan
