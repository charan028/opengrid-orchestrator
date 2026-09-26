"""og-engine cycle-latency window (A11: "real-time cycle p99 < 500 ms at 2k hubs").

The engine records each 2 s tick's wall time here and, once per report interval, publishes p50/p99/max
over the recent window as an `RT_ALLOCATION` / `CYCLE_LATENCY` trace event (and a log line), so A11 is
measurable from Postgres without a metrics port.
"""

from __future__ import annotations

import math
from collections import deque

DEFAULT_WINDOW = 150  # ~5 min of 2 s cycles (02b S6.4 "p99 over the last 5 minutes")


def percentile(sorted_values: list[float], pct: float) -> float:
    """Nearest-rank percentile of an ascending list (0 for an empty list)."""
    if not sorted_values:
        return 0.0
    rank = max(1, math.ceil(pct / 100.0 * len(sorted_values)))
    return sorted_values[rank - 1]


class CycleLatencyWindow:
    """Rolling window of cycle durations (ms) with a due-check for periodic reporting."""

    def __init__(self, *, window: int = DEFAULT_WINDOW, report_every_s: float = 60.0) -> None:
        self._samples: deque[float] = deque(maxlen=window)
        self._report_every_s = report_every_s
        self._last_report_at: float | None = None

    def record(self, duration_ms: float) -> None:
        self._samples.append(duration_ms)

    def summary(self) -> dict[str, float]:
        ordered = sorted(self._samples)
        return {
            "samples": float(len(ordered)),
            "p50_ms": round(percentile(ordered, 50), 1),
            "p99_ms": round(percentile(ordered, 99), 1),
            "max_ms": round(ordered[-1], 1) if ordered else 0.0,
        }

    def report_due(self, monotonic_now: float) -> bool:
        if self._last_report_at is not None and monotonic_now - self._last_report_at < self._report_every_s:
            return False
        self._last_report_at = monotonic_now
        return True
