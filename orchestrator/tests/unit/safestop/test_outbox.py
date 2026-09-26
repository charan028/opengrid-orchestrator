"""K8 durable publish outbox: a stop ENGAGED while the broker is down is recorded, queued and published on
reconnect, in acceptance order, exactly once per (stop_id, action)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import aiomqtt
import pytest

from opengrid.core.crypto import generate_keypair
from opengrid.platform.mqtt_session import MqttSession
from opengrid.safestop.backend import OutboxEntry
from opengrid.safestop.keys import StopSigningKey
from opengrid.safestop.mqtt_publish import StopPublishError
from opengrid.safestop.service import SafestopService


@dataclass
class _Backend:
    """In-memory `StopEventBackend` + `StopOutboxBackend` with the same once-per-(stop_id, action) rule."""

    rows: list[dict[str, Any]] = field(default_factory=list)
    outbox: list[dict[str, Any]] = field(default_factory=list)

    async def insert_stop_event(self, **kwargs: Any) -> None:
        self.rows.append(kwargs)

    async def enqueue_publication(
        self, *, stop_id: UUID, action: str, topic_suffix: str, payload: dict
    ) -> None:
        if any(e["stop_id"] == stop_id and e["action"] == action for e in self.outbox):
            return
        self.outbox.append(
            {
                "seq": len(self.outbox) + 1,
                "stop_id": stop_id,
                "action": action,
                "topic_suffix": topic_suffix,
                "payload": payload,
                "published": False,
                "attempts": 0,
            }
        )

    async def pending_publications(self, *, limit: int) -> list[OutboxEntry]:
        return [
            OutboxEntry(e["seq"], e["stop_id"], e["action"], e["topic_suffix"], e["payload"])
            for e in self.outbox
            if not e["published"]
        ][:limit]

    async def mark_published(self, seq: int) -> None:
        self.outbox[seq - 1]["published"] = True

    async def record_publish_failure(self, seq: int, error: str) -> None:
        self.outbox[seq - 1]["attempts"] += 1


@dataclass
class _Publisher:
    up: bool = False
    published: list[tuple[str, str]] = field(default_factory=list)  # (topic_suffix, action)

    async def publish_retained(self, topic_suffix: str, payload: dict[str, Any]) -> None:
        if not self.up:
            raise StopPublishError("MQTT connection 'safestop' is down")
        self.published.append((topic_suffix, payload["action"]))

    async def clear_retained(self, topic_suffix: str) -> None:
        return None


@pytest.fixture
def world() -> tuple[SafestopService, _Backend, _Publisher]:
    seed, _pub = generate_keypair()
    backend, publisher = _Backend(), _Publisher()
    svc = SafestopService(StopSigningKey("safestop-test", seed), backend, publisher, None, outbox=backend)  # type: ignore[arg-type]
    return svc, backend, publisher


async def test_a_stop_engaged_while_the_broker_is_down_is_recorded_and_published_on_reconnect(world):
    svc, backend, publisher = world

    stop_id = await svc.engage("BANK", "bank-07", "overload", "operator:alice")  # does not raise

    assert backend.rows[0]["stop_event_id"] == stop_id  # accepted and recorded
    assert publisher.published == []
    assert [e["published"] for e in backend.outbox] == [False]

    publisher.up = True  # the broker is back: the session's on-connect hook drains
    assert await svc.drain_outbox() == 1
    assert publisher.published == [(backend.outbox[0]["topic_suffix"], "ENGAGE")]
    assert await svc.drain_outbox() == 0  # acknowledged: never re-published


async def test_order_is_preserved_across_an_outage(world):
    svc, _backend, publisher = world
    first = await svc.engage("BANK", "bank-01", "a", "op:a")
    second = await svc.engage("ZONE", "LZ_NORTH", "b", "op:b")
    third = await svc.engage("BANK", "bank-02", "c", "op:c")

    publisher.up = True
    await svc.drain_outbox()

    order = [topic.rsplit("/", 1)[-1] for topic, _action in publisher.published]
    assert order == [str(first), str(second), str(third)]


async def test_a_failure_mid_drain_stops_there_so_order_is_never_broken(world):
    svc, backend, publisher = world
    for ref in ("bank-01", "bank-02", "bank-03"):
        await svc.engage("BANK", ref, "x", "op")
    publisher.up = True
    calls = {"n": 0}
    real = publisher.publish_retained

    async def flaky(topic_suffix: str, payload: dict[str, Any]) -> None:
        calls["n"] += 1
        if calls["n"] == 2:
            raise StopPublishError("dropped mid-drain")
        await real(topic_suffix, payload)

    publisher.publish_retained = flaky  # type: ignore[method-assign]
    assert await svc.drain_outbox() == 1
    assert [e["published"] for e in backend.outbox] == [True, False, False]
    assert await svc.drain_outbox() == 2
    assert [topic.split("/")[2] for topic, _ in publisher.published] == ["bank-01", "bank-02", "bank-03"]


async def test_the_same_event_is_queued_once(world):
    svc, backend, _publisher = world
    stop_id = await svc.engage("BANK", "bank-07", "x", "op")
    entry = backend.outbox[0]
    await backend.enqueue_publication(
        stop_id=stop_id, action="ENGAGE", topic_suffix=entry["topic_suffix"], payload=entry["payload"]
    )
    assert len(backend.outbox) == 1


async def test_the_session_drains_the_outbox_on_every_reconnect(world):
    """End to end over `MqttSession`: the stop is engaged while the connection is down; the next connect's
    on-connect hook publishes it through that connection."""
    svc, backend, _publisher = world
    await svc.engage("BANK", "bank-07", "x", "op")

    sent: list[str] = []
    connected = asyncio.Event()

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc: object) -> bool:
            return False

        async def publish(self, topic: str, payload: bytes, qos: int, retain: bool) -> None:
            sent.append(topic)

        @property
        def messages(self):
            return self._stream()

        async def _stream(self):
            await connected.wait()
            raise aiomqtt.MqttError("Disconnected")
            yield  # pragma: no cover

    session = MqttSession(_Client, name="t-outbox")

    class _SessionPublisher:
        async def publish_retained(self, topic_suffix: str, payload: dict[str, Any]) -> None:
            await session.publish(topic_suffix, payload=b"{}", qos=1, retain=True, wait_s=0.1)

    svc.publisher = _SessionPublisher()  # type: ignore[assignment]

    async def drain_then_drop() -> None:
        await svc.drain_outbox()
        connected.set()

    session.on_connect = drain_then_drop
    await session.run(max_connections=1)

    assert sent == [backend.outbox[0]["topic_suffix"]]
    assert backend.outbox[0]["published"] is True
