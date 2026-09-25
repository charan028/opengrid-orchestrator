from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

import aiomqtt
import pytest

from opengrid.core.crypto import generate_keypair
from opengrid.platform.config import Config
from opengrid.safestop.events import build_engage_event
from opengrid.safestop.mqtt_publish import AiomqttStopPublisher, StopPublishError


@dataclass
class _FakeMqttClient:
    published: list[dict[str, Any]] = field(default_factory=list)
    raise_error: bool = False

    async def publish(self, topic: str, payload: bytes, qos: int, retain: bool) -> None:
        if self.raise_error:
            raise aiomqtt.MqttError("boom")
        self.published.append({"topic": topic, "payload": payload, "qos": qos, "retain": retain})


@pytest.fixture
def cfg() -> Config:
    return Config({"mqtt": {"topic_root": "ogtest/stop"}})


def _valid_stop_payload() -> dict[str, Any]:
    seed, _pub = generate_keypair()
    event = build_engage_event(
        scope="BANK",
        scope_ref="bank-07",
        reason="drill",
        initiator_ref="operator:alice",
        key_id="safestop-test",
        seed=seed,
    )
    return event.model_dump(mode="json")


async def test_publish_retained_sends_qos1_retained_message(cfg: Config):
    client = _FakeMqttClient()
    publisher = AiomqttStopPublisher(client=client, config=cfg)  # type: ignore[arg-type]
    payload = _valid_stop_payload()

    await publisher.publish_retained(f"stop/bank/bank-07/{uuid4()}", payload)

    assert len(client.published) == 1
    sent = client.published[0]
    assert sent["topic"].startswith("ogtest/stop/stop/bank/bank-07/")
    assert sent["qos"] == 1
    assert sent["retain"] is True
    assert json.loads(sent["payload"])["action"] == "ENGAGE"


async def test_publish_retained_refuses_schema_invalid_payload(cfg: Config):
    client = _FakeMqttClient()
    publisher = AiomqttStopPublisher(client=client, config=cfg)  # type: ignore[arg-type]

    with pytest.raises(StopPublishError):
        await publisher.publish_retained("stop/fleet/x", {"not": "a valid stop event"})

    assert client.published == []


async def test_publish_retained_wraps_mqtt_errors(cfg: Config):
    client = _FakeMqttClient(raise_error=True)
    publisher = AiomqttStopPublisher(client=client, config=cfg)  # type: ignore[arg-type]
    payload = _valid_stop_payload()

    with pytest.raises(StopPublishError):
        await publisher.publish_retained("stop/fleet/x", payload)
