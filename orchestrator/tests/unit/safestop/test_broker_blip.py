"""r3.4.4 live MEDIUM: every broker blip of ~15 s or more restarted og-safestop. The L2 listener backed off 5 -> 10 -> 20 s,
so after a 20 s outage its next attempt landed past the 30 s exit timer ("safestop-l2 down for 32s: exiting").
Now both stop-path connections retry from 1 s, at most 5 s apart, and the exit timer is the guardian's 60 s."""

from __future__ import annotations

import aiomqtt
import pytest

from opengrid.platform.config import Config
from opengrid.platform.mqtt_session import MqttSession
from opengrid.safestop import main as safestop_main
from opengrid.safestop.l2_intake import DEFAULT_MAX_BACKOFF_S, DEFAULT_RECONNECT_DELAY_S, build_l2_session


class _Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def monotonic(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.t += seconds


class _Broker:
    """Down until `back_at` (seconds): connecting raises; after that a connection opens and its stream ends."""

    def __init__(self, clock: _Clock, back_at: float) -> None:
        self.clock, self.back_at = clock, back_at
        self.connected_at: float | None = None

    def client(self):
        broker = self

        class _Client:
            async def __aenter__(self):
                if broker.clock.t < broker.back_at:
                    raise aiomqtt.MqttError("connection refused")
                broker.connected_at = broker.clock.t
                return self

            async def __aexit__(self, *exc):
                return False

            async def subscribe(self, *_a, **_k):
                return None

            @property
            def messages(self):
                async def _none():
                    return
                    yield

                return _none()

        return _Client()


def _stop_path_session(clock: _Clock, broker: _Broker, name: str) -> MqttSession:
    return MqttSession(
        broker.client,
        name=name,
        min_backoff_s=DEFAULT_RECONNECT_DELAY_S,
        max_backoff_s=DEFAULT_MAX_BACKOFF_S,
        monotonic_fn=clock.monotonic,
        sleep_fn=clock.sleep,
    )


def test_the_stop_path_backs_off_from_1s_to_at_most_5s_and_exits_after_the_guardians_60s():
    assert DEFAULT_RECONNECT_DELAY_S == 1.0 and DEFAULT_MAX_BACKOFF_S <= 5.0
    assert safestop_main.DEFAULT_MQTT_DOWN_EXIT_S == 60.0
    session = build_l2_session(
        Config({"mqtt": {"topic_root": "ogtest/x"}}),
        username="u",
        password="p",
        engage_fn=None,  # type: ignore[arg-type]
        already_acted_fn=None,  # type: ignore[arg-type]
    )
    assert (session._min_backoff_s, session._max_backoff_s) == (1.0, DEFAULT_MAX_BACKOFF_S)


async def test_a_20s_broker_outage_recovers_without_an_exit():
    clock = _Clock()
    broker = _Broker(clock, back_at=20.0)
    session = _stop_path_session(clock, broker, "safestop-l2")
    await session.run(max_connections=1)
    assert broker.connected_at is not None
    # reconnected within one max backoff of the broker's return, far inside the 60 s exit timer
    assert broker.connected_at <= 20.0 + DEFAULT_MAX_BACKOFF_S
    assert broker.connected_at < safestop_main.DEFAULT_MQTT_DOWN_EXIT_S


def test_an_outage_over_60s_still_exits_and_a_shorter_one_does_not():
    clock = _Clock()
    session = _stop_path_session(clock, _Broker(clock, back_at=1e9), "safestop")
    exit_after = safestop_main.DEFAULT_MQTT_DOWN_EXIT_S
    clock.t = 32.0  # the live failure's "down for 32s": no longer an exit
    assert safestop_main.stop_path_ready(session, exit_after_s=exit_after) is False
    clock.t = 61.0
    with pytest.raises(SystemExit):
        safestop_main.stop_path_ready(session, exit_after_s=exit_after)
