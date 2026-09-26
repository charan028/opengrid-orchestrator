"""Keeps og-engine's MQTT ingest connected (live 2026-09-26 16:37:41: a keepalive timeout ended the ingest
loop for good; hubs aged out while the tick and heartbeat looked healthy).

`supervise_ingest` runs the ingest loop inside a client context and, whenever the connection ends (an
`MqttError`, a keepalive timeout, or the message stream closing), reconnects with exponential backoff and
jitter (0.5 s doubling to a 30 s cap by default), resubscribing through the loop itself. One client at a
time, always built by the same factory, so the broker sees the SAME client id and never two concurrent
sessions (client ids are global; a duplicate hijacks the other session).

`IngestHealth` records when ingest went down: `beat_if_ticking` stops writing og-engine's heartbeat once
ingest has been down longer than `[mqtt].ingest_down_heartbeat_s` (default 30 s), so health raises
ALR-PROCESS-DOWN. If reconnecting keeps failing for `[mqtt].ingest_give_up_s` (default 120 s), `on_give_up`
runs (the engine exits non-zero so systemd's `Restart=always` starts a fresh process).
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Any

from prometheus_client import Counter

logger = logging.getLogger(__name__)

DEFAULT_BACKOFF_INITIAL_S = 0.5
DEFAULT_BACKOFF_MAX_S = 30.0
DEFAULT_INGEST_DOWN_HEARTBEAT_S = 30.0
DEFAULT_GIVE_UP_S = 120.0
#: Exit status when ingest cannot be restored (non-zero: systemd restarts the unit).
EXIT_MQTT_INGEST_LOST = 3

mqtt_reconnects_total = Counter(
    "og_engine_mqtt_reconnects_total",
    "Reconnect attempts of og-engine's MQTT ingest after its connection ended.",
)


@dataclass
class IngestHealth:
    """Whether MQTT ingest is connected, and since when it has been down (monotonic seconds)."""

    connected: bool = False
    down_since: float | None = None
    reconnects: int = 0

    def mark_up(self) -> None:
        self.connected = True
        self.down_since = None

    def mark_down(self, now: float) -> None:
        self.connected = False
        if self.down_since is None:
            self.down_since = now

    def down_for_s(self, now: float) -> float:
        return 0.0 if self.down_since is None else max(now - self.down_since, 0.0)


async def supervise_ingest(
    make_client: Callable[[], AbstractAsyncContextManager[Any]],
    run: Callable[[Any], Awaitable[None]],
    health: IngestHealth,
    *,
    on_give_up: Callable[[], None],
    backoff_initial_s: float = DEFAULT_BACKOFF_INITIAL_S,
    backoff_max_s: float = DEFAULT_BACKOFF_MAX_S,
    give_up_after_s: float = DEFAULT_GIVE_UP_S,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    clock: Callable[[], float] = time.monotonic,
    jitter: Callable[[], float] = random.random,
) -> None:
    """Run `run(client)` for as long as the process lives, reconnecting after every disconnect."""
    delay = backoff_initial_s
    # Down until the first connection succeeds, so a broker unreachable at start-up also counts.
    health.mark_down(clock())
    while True:
        try:
            async with make_client() as client:
                health.mark_up()
                delay = backoff_initial_s
                logger.info("mqtt ingest connected")
                await run(client)
                logger.warning("mqtt ingest stream ended; reconnecting")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("mqtt ingest disconnected; reconnecting", extra={"error": repr(exc)})
        health.mark_down(clock())
        if health.down_for_s(clock()) >= give_up_after_s:
            logger.critical(
                "mqtt ingest could not reconnect; giving up", extra={"down_s": health.down_for_s(clock())}
            )
            on_give_up()
            return
        health.reconnects += 1
        mqtt_reconnects_total.inc()
        await sleep(delay * (1.0 + 0.2 * jitter()))
        delay = min(delay * 2.0, backoff_max_s)
