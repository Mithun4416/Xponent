"""In-memory query-latency percentiles for the monitoring page (Bonus B-5 / FR-021).

A fixed-size ring of the most recent API request durations (ms). `percentiles()`
reports p50/p95/p99 + count over that window. In-process and best-effort (one
API replica) — deliberately simple, matching the no-infra posture; it resets on
restart and is for live observation, not a metrics backend.
"""
from __future__ import annotations

from collections import deque

_WINDOW = 500
_samples: deque[float] = deque(maxlen=_WINDOW)


def record(ms: float) -> None:
    try:
        _samples.append(float(ms))
    except (TypeError, ValueError):
        pass


def _pct(sorted_vals: list[float], q: float) -> float:
    """Nearest-rank percentile over an already-sorted list (q in [0,1])."""
    if not sorted_vals:
        return 0.0
    k = max(0, min(len(sorted_vals) - 1, int(round(q * (len(sorted_vals) - 1)))))
    return round(sorted_vals[k], 1)


def percentiles() -> dict:
    vals = sorted(_samples)
    return {
        "count": len(vals),
        "p50_ms": _pct(vals, 0.50),
        "p95_ms": _pct(vals, 0.95),
        "p99_ms": _pct(vals, 0.99),
        "window": _WINDOW,
    }


def reset() -> None:
    _samples.clear()
