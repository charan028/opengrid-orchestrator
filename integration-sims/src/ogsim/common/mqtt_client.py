"""ogsim.common.mqtt_client -- aiomqtt wrapper for fleet/scada.

Connects as `og_sim` (password from `OG_MQTT_SIM_PASSWORD`) under the
configured topic root (`OG_MQTT_ROOT`, default `og/v1`), validates
outbound messages against `interfaces/mqtt/*.schema.json` before publish,
and offers a batched-publish helper so a 2,000-hub telemetry tick does not
pay one-coroutine-per-message overhead.

Boundary for tests: `SimMqttClient` depends only on `MqttTransport`, a
tiny protocol matching the subset of `aiomqtt.Client` this module uses.
Unit tests substitute a stub transport; no live broker is required.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from typing import Any, Protocol

from ogsim.common.schemas import validate

logger = logging.getLogger(__name__)


class MqttMessage(Protocol):
    topic: Any
    payload: Any


class MqttTransport(Protocol):
    """The subset of `aiomqtt.Client` this wrapper needs, so tests can stub it."""

    async def publish(self, topic: str, payload: bytes, qos: int = 0, retain: bool = False) -> None: ...

    async def subscribe(self, topic: str, qos: int = 0) -> None: ...

    def messages(self) -> AsyncIterator[MqttMessage]: ...


class SimMqttClient:
    """Schema-validating publish/subscribe helper shared by fleet and scada."""

    def __init__(self, transport: MqttTransport, topic_root: str) -> None:
        self._transport = transport
        self.topic_root = topic_root

    def topic(self, suffix: str) -> str:
        return f"{self.topic_root}/{suffix}"

    async def publish_validated(
        self, schema_name: str, suffix: str, message: dict[str, Any], qos: int = 0, retain: bool = False
    ) -> None:
        """Validates `message` against `schema_name` then publishes it."""
        validate(schema_name, message)
        payload = json.dumps(message).encode("utf-8")
        await self._transport.publish(self.topic(suffix), payload=payload, qos=qos, retain=retain)

    async def publish_batch(
        self, schema_name: str, items: list[tuple[str, dict[str, Any]]], qos: int = 0
    ) -> None:
        """Publishes many (suffix, message) pairs against one schema without
        the overhead of `create_task` per message -- awaited sequentially on
        one connection, which is what actually matters for a 2,000-hub
        telemetry tick (the cost is serialization + socket writes, not
        coroutine scheduling)."""
        for suffix, message in items:
            validate(schema_name, message)
            payload = json.dumps(message).encode("utf-8")
            await self._transport.publish(self.topic(suffix), payload=payload, qos=qos)

    async def subscribe(self, suffix: str, qos: int = 1) -> None:
        await self._transport.subscribe(self.topic(suffix), qos=qos)

    async def messages(self) -> AsyncIterator[MqttMessage]:
        async for msg in self._transport.messages():
            yield msg


def mqtt_settings(host: str, port: int, password: str, username: str = "og_sim") -> dict[str, Any]:
    """Builds the `aiomqtt.Client` kwargs. Callers pass `MqttSettings.username`, which is the workspace
    user (`ogw_<ws>`) in a workspace run and `og_sim` in production (`config.resolve_mqtt_credentials`)."""
    return {"hostname": host, "port": port, "username": username, "password": password}


class AiomqttTransportAdapter:
    """Adapts a real `aiomqtt.Client` to the `MqttTransport` protocol above,
    shared by `ogsim.fleet.__main__` and `ogsim.scada.__main__`."""

    def __init__(self, client: Any) -> None:
        self._client = client

    async def publish(self, topic: str, payload: bytes, qos: int = 0, retain: bool = False) -> None:
        await self._client.publish(topic, payload=payload, qos=qos, retain=retain)

    async def subscribe(self, topic: str, qos: int = 0) -> None:
        await self._client.subscribe(topic, qos=qos)

    def messages(self) -> Any:
        return self._client.messages
