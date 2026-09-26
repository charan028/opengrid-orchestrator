"""A long-lived MQTT connection that survives broker disconnects (review finding 2026-09-26: og-engine's
ingest loop exited on `MqttError` and never reconnected while the process kept heartbeating).

`MqttSession` owns ONE broker client under ONE fixed client id for the process lifetime: it connects,
(re)subscribes, reads messages, and on any disconnect closes that client and opens a new one after an
exponential backoff. The old client is always closed before the next is built, so two concurrent clients
with the same id -- which would make the broker kick one of them in a loop -- can never exist.

Callers read `connected` / `down_for_s()` to fail closed (the guardian holds signing, a process exits so
systemd restarts it); every reconnect increments `og_mqtt_reconnects_total{client}` and
`og_mqtt_connected{client}` shows the state. `publish` waits briefly for a connection and raises
`MqttNotConnectedError` (an `aiomqtt.MqttError`) rather than dropping a message silently.

Shared by og-guardian and og-safestop (both may import `opengrid.platform`; safestop never imports the
guardian, K8).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, Protocol

import aiomqtt
from prometheus_client import Counter, Gauge

logger = logging.getLogger(__name__)

DEFAULT_MIN_BACKOFF_S = 1.0
DEFAULT_MAX_BACKOFF_S = 30.0
DEFAULT_PUBLISH_WAIT_S = 5.0

mqtt_reconnects_total = Counter(
    "og_mqtt_reconnects_total",
    "MQTT connections lost and re-established, by client (process connection name).",
    labelnames=("client",),
)
mqtt_connected = Gauge(
    "og_mqtt_connected",
    "1 while the named MQTT connection is up, else 0.",
    labelnames=("client",),
)


class MqttNotConnectedError(aiomqtt.MqttError):
    """No broker connection within the publish wait: the message was NOT sent."""


class MqttPublisher(Protocol):
    """What a publishing caller needs: `aiomqtt.Client.publish`'s keyword shape (also `MqttSession`)."""

    async def publish(self, topic: str, payload: bytes, qos: int, retain: bool) -> None: ...


MessageHandler = Callable[[Any], Awaitable[None]]


class MqttSession:
    def __init__(
        self,
        client_factory: Callable[[], Any],
        *,
        name: str,
        subscriptions: Sequence[tuple[str, int]] = (),
        on_message: MessageHandler | None = None,
        on_connect: Callable[[], Awaitable[object]] | None = None,
        min_backoff_s: float = DEFAULT_MIN_BACKOFF_S,
        max_backoff_s: float = DEFAULT_MAX_BACKOFF_S,
        monotonic_fn: Callable[[], float] = time.monotonic,
        sleep_fn: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._factory = client_factory
        self.name = name
        self._subscriptions = tuple(subscriptions)
        self._on_message = on_message
        #: Run after every (re)connect and (re)subscribe, e.g. og-safestop draining its stop outbox. A
        #: failure is logged; it never drops the connection.
        self.on_connect = on_connect
        self._min_backoff_s = min_backoff_s
        self._max_backoff_s = max_backoff_s
        self._monotonic = monotonic_fn
        self._sleep = sleep_fn
        self._client: Any | None = None
        self._on_connect_task: asyncio.Task[None] | None = None
        self._up = asyncio.Event()
        self._down_since: float | None = monotonic_fn()  # not yet connected counts as down
        self.connections = 0
        mqtt_connected.labels(client=name).set(0)

    @property
    def connected(self) -> bool:
        return self._client is not None

    def down_for_s(self) -> float:
        """Seconds since the connection was last lost (or since start before the first connect); 0 when up."""
        if self._down_since is None:
            return 0.0
        return max(self._monotonic() - self._down_since, 0.0)

    async def run(self, *, max_connections: int | None = None) -> None:
        """Keep the connection up until cancelled (`max_connections` bounds the loop for tests)."""
        backoff = self._min_backoff_s
        while max_connections is None or self.connections < max_connections:
            client = self._factory()
            try:
                async with client:
                    self._mark_up(client)
                    backoff = self._min_backoff_s
                    for topic_filter, qos in self._subscriptions:
                        await client.subscribe(topic_filter, qos=qos)
                    if self.on_connect is not None:
                        # Its own task: it may publish through this session while messages are read here.
                        self._on_connect_task = asyncio.create_task(self._run_on_connect())
                    async for message in client.messages:
                        if self._on_message is None:
                            continue
                        try:
                            await self._on_message(message)
                        except asyncio.CancelledError:
                            raise
                        except Exception:
                            logger.exception("mqtt message handler failed", extra={"client": self.name})
                    logger.warning("mqtt message stream ended", extra={"client": self.name})
            except asyncio.CancelledError:
                self._mark_down()
                raise
            except aiomqtt.MqttError as exc:
                logger.warning("mqtt connection lost", extra={"client": self.name, "error": str(exc)})
            except Exception:
                logger.exception("mqtt connection failed", extra={"client": self.name})
            self._mark_down()
            if max_connections is not None and self.connections >= max_connections:
                return
            await self._sleep(backoff)
            backoff = min(backoff * 2, self._max_backoff_s)

    async def _run_on_connect(self) -> None:
        try:
            if self.on_connect is not None:
                await self.on_connect()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("mqtt on-connect hook failed", extra={"client": self.name})

    async def publish(
        self, topic: str, payload: bytes, qos: int, retain: bool, *, wait_s: float = DEFAULT_PUBLISH_WAIT_S
    ) -> None:
        """Publish on the live connection, waiting up to `wait_s` for one. Raises `MqttNotConnectedError`
        (nothing sent) or the client's own `MqttError`; never drops a message silently."""
        if self._client is None:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._up.wait(), timeout=wait_s)
        client = self._client
        if client is None:
            raise MqttNotConnectedError(f"MQTT connection {self.name!r} is down; {topic} not published")
        await client.publish(topic, payload=payload, qos=qos, retain=retain)

    def _mark_up(self, client: Any) -> None:
        if self.connections > 0:
            mqtt_reconnects_total.labels(client=self.name).inc()
            logger.info("mqtt reconnected", extra={"client": self.name})
        self.connections += 1
        self._client = client
        self._down_since = None
        self._up.set()
        mqtt_connected.labels(client=self.name).set(1)

    def _mark_down(self) -> None:
        if self._client is not None:
            self._down_since = self._monotonic()
        self._client = None
        self._up.clear()
        mqtt_connected.labels(client=self.name).set(0)
