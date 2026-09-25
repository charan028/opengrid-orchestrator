"""Async token bucket rate limiter (02b S2.5): the ERCOT 24 req/min budget.

Owned exclusively by `feeds`; nothing else needs client-side rate limiting in MVP-S.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable


class TokenBucket:
    """Capacity/refill-rate token bucket. `acquire()` waits (never drops) until a token is free,
    per TS-02-02 ("excess requests queued or deferred, not dropped silently")."""

    def __init__(
        self, capacity: float, refill_per_s: float, *, clock: Callable[[], float] | None = None
    ) -> None:
        if capacity <= 0 or refill_per_s <= 0:
            raise ValueError("capacity and refill_per_s must be positive")
        self._capacity = capacity
        self._refill_per_s = refill_per_s
        self._clock = clock or time.monotonic
        self._tokens = capacity
        self._last_refill = self._clock()
        self._lock = asyncio.Lock()

    def _refill(self) -> None:
        now = self._clock()
        elapsed = max(now - self._last_refill, 0.0)
        self._tokens = min(self._capacity, self._tokens + elapsed * self._refill_per_s)
        self._last_refill = now

    async def acquire(self) -> None:
        """Block until one token is available, then consume it."""
        while True:
            async with self._lock:
                self._refill()
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                deficit = 1.0 - self._tokens
                wait_s = deficit / self._refill_per_s
            await asyncio.sleep(wait_s)
