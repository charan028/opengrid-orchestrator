"""Bounded background worker for slow MQTT-ingest side effects (og-engine).

The MQTT ingest loop must never wait on disk or a synchronous commit: when it falls behind, every hub's
telemetry is applied late and the whole fleet ages to stale (live 2026-09-26: raw waveform captures --
a blob write plus a synchronously committed index row, ~7/s from the simulator -- delayed telemetry by
minutes). Payloads are queued here and handled one at a time by a separate task; when the queue is full
the new payload is dropped and counted (a raw capture is an audit sample, never control state).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_QUEUE_MAX = 500

Handler = Callable[[dict[str, Any]], Awaitable[object]]


class BackgroundIngest:
    """`submit()` never blocks; `run()` (one task) drains the queue through `handler`."""

    def __init__(self, name: str, handler: Handler, *, queue_max: int = DEFAULT_QUEUE_MAX) -> None:
        self.name = name
        self._handler = handler
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=queue_max)
        self.dropped = 0
        self.handled = 0

    def submit(self, payload: dict[str, Any]) -> bool:
        try:
            self._queue.put_nowait(payload)
        except asyncio.QueueFull:
            self.dropped += 1
            if self.dropped == 1 or self.dropped % 100 == 0:
                logger.warning(
                    "background ingest queue full; dropping",
                    extra={"worker": self.name, "dropped": self.dropped},
                )
            return False
        return True

    async def run(self) -> None:
        while True:
            payload = await self._queue.get()
            try:
                await self._handler(payload)
                self.handled += 1
            except Exception:
                logger.exception("background ingest handler failed", extra={"worker": self.name})
            finally:
                self._queue.task_done()

    async def drain(self) -> None:
        """Wait until everything submitted so far has been handled (tests, orderly shutdown)."""
        await self._queue.join()


async def run_periodic(
    name: str,
    interval_s: float,
    job: Callable[[], Awaitable[object]],
    *,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
) -> None:
    """Run `job` every `interval_s` as its own task (never inside the 2 s dispatch tick) until cancelled.
    A failed run is logged and the next run still happens (K7)."""
    while True:
        started = clock()
        try:
            await job()
        except Exception:
            logger.exception("periodic job failed", extra={"job": name})
        await sleep(max(0.0, interval_s - (clock() - started)))
