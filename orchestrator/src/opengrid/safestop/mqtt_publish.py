"""Real `StopPublisher`: publishes the retained `<root>/stop/<scope>/<id>` message (topics.md: QoS 1,
retained) over the process's one long-lived connection -- in `main.py` an `opengrid.platform.mqtt_session.
MqttSession`, which reconnects after a broker disconnect (a failed publish raises, it is never dropped).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import aiomqtt

from opengrid.platform.config import Config
from opengrid.platform.mqtt import SchemaValidationError, topic, validate_payload
from opengrid.platform.mqtt_session import MqttPublisher

STOP_QOS = 1


class StopPublishError(Exception):
    """Raised when a stop payload fails schema validation or the MQTT publish itself fails."""


@dataclass
class AiomqttStopPublisher:
    client: MqttPublisher
    config: Config

    async def publish_retained(self, topic_suffix: str, payload: dict[str, Any]) -> None:
        try:
            validate_payload("stop", payload)
        except SchemaValidationError as exc:
            raise StopPublishError(f"refusing to publish invalid stop payload: {exc}") from exc

        full_topic = topic(self.config, topic_suffix)
        try:
            await self.client.publish(
                full_topic,
                payload=json.dumps(payload).encode("utf-8"),
                qos=STOP_QOS,
                retain=True,
            )
        except aiomqtt.MqttError as exc:
            raise StopPublishError(f"MQTT publish failed for {full_topic}: {exc}") from exc

    async def clear_retained(self, topic_suffix: str) -> None:
        """Zero-length retained publish: deletes the broker's retained message on the topic (topics.md:
        housekeeping only, never a stop-state change)."""
        full_topic = topic(self.config, topic_suffix)
        try:
            await self.client.publish(full_topic, payload=b"", qos=STOP_QOS, retain=True)
        except aiomqtt.MqttError as exc:
            raise StopPublishError(f"MQTT retained clear failed for {full_topic}: {exc}") from exc
