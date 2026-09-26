"""og-engine cycle-latency window (A11: "real-time cycle p99 < 500 ms at 2k hubs").

The engine records each 2 s tick's wall time here and, once per report interval, publishes p50/p99/max
over the recent window as an `RT_ALLOCATION` / `CYCLE_LATENCY` trace event (and a log line), so A11 is
measurable from Postgres without a metrics port. Each tick also carries a per-phase breakdown
(`PhaseTimer`), and a `LoopLagProbe` measures how late the event loop wakes a sleeping coroutine: a slow
tick with a large loop lag is CPU/GIL starvation (threads or ingest), one without is an awaited phase.
"""

from __future__ import annotations

import asyncio
import math
import time
from collections import deque
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

DEFAULT_WINDOW = 300  # ~10 min of 2 s cycles; A11 is judged over >= 300 ticks


def percentile(sorted_values: list[float], pct: float) -> float:
    """Nearest-rank percentile of an ascending list (0 for an empty list)."""
    if not sorted_values:
        return 0.0
    rank = max(1, math.ceil(pct / 100.0 * len(sorted_values)))
    return sorted_values[rank - 1]


class PhaseTimer:
    """Accumulates wall time (ms) per named phase of one tick; a phase that raises is still recorded."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self.phases: dict[str, float] = {}

    @contextmanager
    def phase(self, name: str) -> Iterator[None]:
        started = self._clock()
        try:
            yield
        finally:
            self.phases[name] = round(self.phases.get(name, 0.0) + (self._clock() - started) * 1000.0, 3)


class LoopLagProbe:
    """Sleeps `interval_s` in a loop and records how late each wake-up is (ms)."""

    def __init__(
        self,
        *,
        interval_s: float = 0.1,
        window: int = 3000,
        on_sample: Callable[[float], None] | None = None,
    ) -> None:
        self._interval_s = interval_s
        self._on_sample = on_sample
        self.samples: deque[float] = deque(maxlen=window)

    def observe(self, *, expected: float, actual: float) -> None:
        lag_ms = max(0.0, (actual - expected) * 1000.0)
        self.samples.append(lag_ms)
        if self._on_sample is not None:
            self._on_sample(lag_ms)

    async def run(self) -> None:
        while True:
            expected = time.monotonic() + self._interval_s
            await asyncio.sleep(self._interval_s)
            self.observe(expected=expected, actual=time.monotonic())


class CycleLatencyWindow:
    """Rolling window of cycle durations (ms) with a due-check for periodic reporting."""

    def __init__(
        self,
        *,
        window: int = DEFAULT_WINDOW,
        report_every_s: float = 60.0,
        lag_probe: LoopLagProbe | None = None,
    ) -> None:
        self._samples: deque[tuple[float, dict[str, float]]] = deque(maxlen=window)
        self._report_every_s = report_every_s
        self._last_report_at: float | None = None
        self.lag_probe = lag_probe

    def record(self, duration_ms: float, phases: dict[str, float] | None = None) -> None:
        self._samples.append((duration_ms, dict(phases or {})))

    def summary(self) -> dict[str, Any]:
        ordered = sorted(ms for ms, _ in self._samples)
        summary: dict[str, Any] = {
            "samples": float(len(ordered)),
            "p50_ms": round(percentile(ordered, 50), 1),
            "p99_ms": round(percentile(ordered, 99), 1),
            "max_ms": round(ordered[-1], 1) if ordered else 0.0,
        }
        if self._samples:
            summary["slowest_phases"] = max(self._samples, key=lambda s: s[0])[1]
        by_phase: dict[str, list[float]] = {}
        for _, phases in self._samples:
            for name, ms in phases.items():
                by_phase.setdefault(name, []).append(ms)
        summary["phase_p99_ms"] = {name: round(percentile(sorted(v), 99), 1) for name, v in by_phase.items()}
        if self.lag_probe is not None:
            lags = sorted(self.lag_probe.samples)
            summary["loop_lag_p99_ms"] = round(percentile(lags, 99), 1)
            summary["loop_lag_max_ms"] = round(lags[-1], 1) if lags else 0.0
        return summary

    def report_due(self, monotonic_now: float) -> bool:
        if self._last_report_at is not None and monotonic_now - self._last_report_at < self._report_every_s:
            return False
        self._last_report_at = monotonic_now
        return True
