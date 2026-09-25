"""Tests for ogsim.fleet.__main__'s message dispatch: BLOCKER fix -- every
command_batch verdict (accepted or rejected) must produce one ack published
to <root>/ack/<hub_id> at QoS 1 (interfaces/mqtt/ack.schema.json)."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from dataclasses import replace as dc_replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from ogsim.common.config import MqttSettings, load_fleet_config
from ogsim.common.crypto import sign
from ogsim.fleet.__main__ import _dispatch_message
from ogsim.fleet.runtime import FleetEngine

MQTT = MqttSettings(host="127.0.0.1", port=1883, username="og_sim", password="", topic_root="og/v1")


@dataclass
class _FakePublishingClient:
    """Records `publish_validated` calls instead of touching a real broker."""

    topic_root: str = "og/v1"
    calls: list[dict[str, Any]] = field(default_factory=list)

    def topic(self, suffix: str) -> str:
        return f"{self.topic_root}/{suffix}"

    async def publish_validated(
        self, schema_name: str, suffix: str, message: dict[str, Any], qos: int = 0, retain: bool = False
    ) -> None:
        self.calls.append(
            {"schema_name": schema_name, "topic": self.topic(suffix), "message": message, "qos": qos}
        )


@pytest.fixture
def engine() -> FleetEngine:
    config = dc_replace(load_fleet_config(), mqtt=MQTT, hub_count=4, bank_count=1, zones=("LZ_NORTH",))
    return FleetEngine(config, seed=1)


@pytest.fixture
def guardian_key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


def _signed_batch(guardian_key: Ed25519PrivateKey, hub_id: str, *, epoch: int = 1, seq: int = 1) -> dict:
    now = datetime.now(UTC)
    batch = {
        "batch_id": str(uuid.uuid4()),
        "bank_id": "bank-000",
        "epoch": epoch,
        "seq": seq,
        "issued_at": (now - timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
        "expires_at": (now + timedelta(seconds=30)).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
        "items": [{"hub_id": hub_id, "p_kw_setpoint": -1.0, "reason_code": "SELECTOR"}],
    }
    signing_fields = {
        k: batch[k] for k in ("batch_id", "bank_id", "epoch", "seq", "issued_at", "expires_at", "items")
    }
    batch["key_id"] = "guardian-test"
    batch["signature"] = sign(guardian_key, signing_fields)
    return batch


async def test_accepted_verdict_publishes_ack_at_qos_1(
    engine: FleetEngine, guardian_key: Ed25519PrivateKey
) -> None:
    hub_id = engine.state.hub_ids[0]
    batch = _signed_batch(guardian_key, hub_id)
    client = _FakePublishingClient()

    await _dispatch_message(
        engine,
        client,  # type: ignore[arg-type]
        "og/v1/cmd/bank-000/batch",
        json.dumps(batch).encode("utf-8"),
        guardian_key.public_key(),
        Ed25519PrivateKey.generate().public_key(),
    )

    assert len(client.calls) == 1
    call = client.calls[0]
    assert call["topic"] == f"og/v1/ack/{hub_id}"
    assert call["schema_name"] == "ack"
    assert call["qos"] == 1
    ack = call["message"]
    assert ack["hub_id"] == hub_id
    assert ack["batch_id"] == batch["batch_id"]
    assert ack["accepted"] is True
    assert ack["reject_reason"] is None
    assert ack["applied_p_kw"] is not None


async def test_rejected_verdict_still_publishes_an_ack(
    engine: FleetEngine, guardian_key: Ed25519PrivateKey
) -> None:
    hub_id = engine.state.hub_ids[0]
    wrong_key = Ed25519PrivateKey.generate()
    batch = _signed_batch(wrong_key, hub_id)  # signed with the wrong key
    client = _FakePublishingClient()

    await _dispatch_message(
        engine,
        client,  # type: ignore[arg-type]
        "og/v1/cmd/bank-000/batch",
        json.dumps(batch).encode("utf-8"),
        guardian_key.public_key(),
        Ed25519PrivateKey.generate().public_key(),
    )

    assert len(client.calls) == 1
    call = client.calls[0]
    assert call["topic"] == f"og/v1/ack/{hub_id}"
    assert call["qos"] == 1
    ack = call["message"]
    assert ack["accepted"] is False
    assert ack["reject_reason"] == "BAD_SIGNATURE"
    assert ack["applied_p_kw"] is None


async def test_unparseable_payload_is_dropped_without_publishing(engine: FleetEngine) -> None:
    client = _FakePublishingClient()
    await _dispatch_message(
        engine,
        client,  # type: ignore[arg-type]
        "og/v1/cmd/bank-000/batch",
        b"not json",
        Ed25519PrivateKey.generate().public_key(),
        Ed25519PrivateKey.generate().public_key(),
    )
    assert client.calls == []
