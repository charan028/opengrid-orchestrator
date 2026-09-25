"""ogsim.common.clock -- injectable clock so fleet/scada tests never sleep.

BUILD.md §5a forbids flaky sleep-based tests. `RealClock` wraps
`time.time()`/`asyncio.sleep`; `FakeClock` lets tests advance time
deterministically and releases any coroutine parked in `sleep()` whose
deadline has passed.
"""

from __future__ import annotations

import asyncio
import time
from typing import Protocol


class Clock(Protocol):
    """Minimal clock surface used by fleet/scada runtime loops."""

    def now(self) -> float:
        """Returns the current time as a Unix epoch float (seconds)."""
        ...

    async def sleep(self, seconds: float) -> None:
        """Waits `seconds` of this clock's time before resuming."""
        ...


class RealClock:
    """Wall-clock time, for production use."""

    def now(self) -> float:
        return time.time()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(max(0.0, seconds))


class FakeClock:
    """Deterministic clock for tests: `advance()` moves time forward and
    wakes any waiter whose deadline has elapsed. No wall-clock sleeping."""

    def __init__(self, start: float = 0.0) -> None:
        self._now = start
        self._waiters: list[tuple[float, asyncio.Event]] = []

    def now(self) -> float:
        return self._now

    async def sleep(self, seconds: float) -> None:
        if seconds <= 0:
            return
        deadline = self._now + seconds
        event = asyncio.Event()
        self._waiters.append((deadline, event))
        await event.wait()

    def advance(self, seconds: float) -> None:
        """Moves time forward by `seconds` and releases due waiters."""
        self._now += seconds
        still_waiting: list[tuple[float, asyncio.Event]] = []
        for deadline, event in self._waiters:
            if deadline <= self._now:
                event.set()
            else:
                still_waiting.append((deadline, event))
        self._waiters = still_waiting
