"""`opengrid.platform.mqtt_session.MqttSession` against fake clients that drop the connection: it reconnects
with backoff under the same client factory, never holds two clients at once, counts reconnects, and a
publish while down raises instead of being dropped."""

from __future__ import annotations

import asyncio

import aiomqtt
import pytest

from opengrid.platform.mqtt_session import MqttNotConnectedError, MqttSession, mqtt_reconnects_total


class _Broker:
    """Hands out clients whose message stream yields `per_connection` messages and then drops."""

    def __init__(self, per_connection: list[list[str]], *, fail_connects: int = 0) -> None:
        self.per_connection = per_connection
        self.fail_connects = fail_connects
        self.open = 0
        self.max_open = 0
        self.built = 0
        self.subscribed: list[tuple[str, int]] = []
        self.published: list[tuple[str, bytes, int, bool]] = []

    def factory(self) -> _Client:
        self.built += 1
        return _Client(self)


class _Client:
    def __init__(self, broker: _Broker) -> None:
        self.broker = broker

    async def __aenter__(self) -> _Client:
        if self.broker.fail_connects > 0:
            self.broker.fail_connects -= 1
            raise aiomqtt.MqttError("connection refused")
        self.broker.open += 1
        self.broker.max_open = max(self.broker.max_open, self.broker.open)
        return self

    async def __aexit__(self, *exc: object) -> bool:
        self.broker.open -= 1
        return False

    async def subscribe(self, topic_filter: str, qos: int = 0) -> None:
        self.broker.subscribed.append((topic_filter, qos))

    async def publish(self, topic: str, payload: bytes, qos: int, retain: bool) -> None:
        self.broker.published.append((topic, payload, qos, retain))

    @property
    def messages(self):
        return self._stream()

    async def _stream(self):
        batch = self.broker.per_connection.pop(0) if self.broker.per_connection else []
        for m in batch:
            yield m
            await asyncio.sleep(0)
        raise aiomqtt.MqttError("Disconnected during message iteration")


async def test_a_dropped_connection_is_re_established_with_backoff_and_resubscribed():
    broker = _Broker([["a", "boom", "b"], ["c"]], fail_connects=2)
    received: list[str] = []
    sleeps: list[float] = []

    async def on_message(message: str) -> None:
        if message == "boom":
            raise ValueError("a bad message never kills the session")
        received.append(message)

    async def fake_sleep(s: float) -> None:
        sleeps.append(s)

    before = mqtt_reconnects_total.labels(client="t-reconnect")._value.get()
    session = MqttSession(
        broker.factory,
        name="t-reconnect",
        subscriptions=[("root/tel/#", 0)],
        on_message=on_message,
        min_backoff_s=1.0,
        max_backoff_s=4.0,
        sleep_fn=fake_sleep,
    )

    await session.run(max_connections=2)

    assert received == ["a", "b", "c"]
    assert broker.built == 4  # two refused connects, then two sessions
    assert broker.max_open == 1  # never two clients (same id) at once
    assert broker.subscribed == [("root/tel/#", 0), ("root/tel/#", 0)]  # resubscribed after reconnect
    assert sleeps == [1.0, 2.0, 1.0]  # doubles while refused, back to the minimum once connected
    assert mqtt_reconnects_total.labels(client="t-reconnect")._value.get() == before + 1
    assert not session.connected


async def test_publish_while_down_raises_instead_of_dropping():
    session = MqttSession(_Broker([]).factory, name="t-down")
    with pytest.raises(MqttNotConnectedError):
        await session.publish("root/stop/x", b"{}", qos=1, retain=True, wait_s=0.01)
    assert isinstance(MqttNotConnectedError("x"), aiomqtt.MqttError)


async def test_publish_goes_out_on_the_live_connection_and_down_time_is_tracked():
    broker = _Broker([])
    clock = {"t": 10.0}
    gate = asyncio.Event()

    class _Blocking(_Client):
        async def _stream(self):
            await gate.wait()
            raise aiomqtt.MqttError("Disconnected")
            yield  # pragma: no cover

    session = MqttSession(lambda: _Blocking(broker), name="t-live", monotonic_fn=lambda: clock["t"])
    assert not session.connected and session.down_for_s() == 0.0

    task = asyncio.create_task(session.run(max_connections=1))
    await session.publish("root/cmd/b/batch", b"{}", qos=1, retain=False, wait_s=1.0)
    assert session.connected and session.down_for_s() == 0.0
    assert broker.published == [("root/cmd/b/batch", b"{}", 1, False)]

    gate.set()
    await task
    clock["t"] = 25.0
    assert not session.connected and session.down_for_s() == 15.0
