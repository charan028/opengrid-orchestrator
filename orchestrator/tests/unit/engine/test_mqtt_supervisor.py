"""og-engine MQTT ingest reconnects after a broker disconnect (live 2026-09-26 16:37:41 keepalive timeout)."""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import aiomqtt
import pytest

from opengrid import engine
from opengrid.engine.mqtt_supervisor import IngestHealth, supervise_ingest

pytestmark = pytest.mark.asyncio


class _Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += seconds

    sleeps: list[float]


async def test_a_disconnect_reconnects_with_the_same_factory_and_messages_flow_again() -> None:
    import asyncio

    clock = _Clock()
    clock.sleeps = []
    connections: list[int] = []
    received: list[str] = []
    health = IngestHealth()

    @asynccontextmanager
    async def make_client():
        connections.append(len(connections))
        yield SimpleNamespace(n=len(connections))

    async def run(client: Any) -> None:
        if client.n == 1:
            received.append("m1")
            raise aiomqtt.MqttError("keepalive timeout")
        received.append("m2")
        assert health.connected and health.down_since is None
        raise asyncio.CancelledError  # end the test run (a shutdown propagates out of the supervisor)

    gave_up: list[bool] = []
    with pytest.raises(asyncio.CancelledError):
        await supervise_ingest(
            make_client,
            run,
            health,
            on_give_up=lambda: gave_up.append(True),
            sleep=clock.sleep,
            clock=clock,
            jitter=lambda: 0.0,
            give_up_after_s=120.0,
        )
    assert received == ["m1", "m2"]  # messages flow again on the new connection
    assert connections == [0, 1]  # one client at a time, rebuilt by the same factory (same client id)
    assert clock.sleeps == [0.5]  # backoff starts at 0.5 s
    assert health.reconnects == 1
    assert gave_up == []


async def test_a_persistent_failure_backs_off_to_the_cap_then_gives_up() -> None:
    clock = _Clock()
    clock.sleeps = []
    health = IngestHealth()

    @asynccontextmanager
    async def make_client():
        raise aiomqtt.MqttError("connection refused")
        yield  # pragma: no cover

    async def run(client: Any) -> None:  # pragma: no cover - never connected
        return None

    gave_up: list[bool] = []
    await supervise_ingest(
        make_client,
        run,
        health,
        on_give_up=lambda: gave_up.append(True),
        sleep=clock.sleep,
        clock=clock,
        jitter=lambda: 0.0,
        backoff_max_s=30.0,
        give_up_after_s=120.0,
    )
    assert gave_up == [True]
    assert clock.sleeps[:7] == [0.5, 1.0, 2.0, 4.0, 8.0, 16.0, 30.0]
    assert max(clock.sleeps) == 30.0
    assert not health.connected and health.down_for_s(clock()) >= 120.0


async def test_heartbeat_stops_once_ingest_has_been_down_too_long(monkeypatch) -> None:
    written: list[str] = []

    async def beat(pool, name):
        written.append(name)

    monkeypatch.setattr(engine, "write_heartbeat", beat)
    health = IngestHealth()
    state = SimpleNamespace(
        last_tick_at=100.0,
        cycle_interval_s=2.0,
        heartbeat_pool=None,
        ingest_health=health,
        ingest_down_heartbeat_s=30.0,
    )
    health.mark_up()
    assert await engine.beat_if_ticking(state, monotonic_now=101.0) is True
    health.mark_down(80.0)
    assert await engine.beat_if_ticking(state, monotonic_now=101.0) is True  # down 21 s: still within 30 s
    health.mark_down(60.0)  # already down since 80 s; the first down time is kept
    state.last_tick_at = 115.0
    assert await engine.beat_if_ticking(state, monotonic_now=115.0) is False  # down 35 s
    assert written == ["engine", "engine"]
