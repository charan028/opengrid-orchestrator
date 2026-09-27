"""R3.1 firmware wiring in og-guardian: `GuardianService.sign_firmware_payload` signs exactly what the hub
verifies, and `mqtt_io.publish_firmware_command` validates the envelope and publishes it on
`<root>/cmd/fw/<hub_id>` at QoS 1 -- end to end through `firmware.guardian_flow.process_pending_firmware`."""

from __future__ import annotations

import json
from typing import Any

import pytest

from opengrid.core.crypto import private_key_from_seed, verify_payload
from opengrid.firmware.guardian_flow import process_pending_firmware
from opengrid.firmware.model import FirmwareCommand
from opengrid.guardian import mqtt_io
from opengrid.platform.config import Config
from opengrid.platform.mqtt import SchemaValidationError

from ..firmware.test_guardian_flow_and_schema import CFG, FACTS, GRANT, Port
from .conftest import service_with


class _Client:
    def __init__(self) -> None:
        self.published: list[tuple[str, bytes, int, bool]] = []

    async def publish(self, topic: str, payload: bytes, qos: int, retain: bool) -> None:
        self.published.append((topic, payload, qos, retain))


async def test_the_guardian_signs_and_publishes_an_eligible_firmware_command(
    fakes, guardian_config, signing_seed
):
    service = service_with(fakes, guardian_config, signing_seed)
    client, cfg = _Client(), Config({"mqtt": {"topic_root": "ogtest/fw"}})
    traces: list[tuple[str, str, dict[str, Any]]] = []

    async def publish(command: FirmwareCommand) -> None:
        await mqtt_io.publish_firmware_command(client, cfg, command)

    async def trace(stream: str, event_class: str, payload: dict[str, Any]) -> None:
        traces.append((stream, event_class, payload))

    port = Port(FACTS, GRANT)
    count = await process_pending_firmware(
        port,
        sign=service.sign_firmware_payload,
        publish=publish,
        trace=trace,
        cfg=CFG,
        key_id="guardian-2026a",
    )

    assert count == 1
    ((topic, payload, qos, retain),) = client.published
    assert (topic, qos, retain) == ("ogtest/fw/cmd/fw/hub-00001", 1, False)
    wire = json.loads(payload)
    command = FirmwareCommand.model_validate(wire)
    public = private_key_from_seed(signing_seed).public_key().public_bytes_raw()
    assert verify_payload(public, command.signing_payload(), command.signature)  # what the hub verifies
    assert traces and traces[0][2]["outcome"] == "SIGNED"


async def test_a_malformed_firmware_envelope_is_never_published():
    client, cfg = _Client(), Config({"mqtt": {"topic_root": "ogtest/fw"}})
    port = Port(FACTS, GRANT)
    command = FirmwareCommand(
        command_id=port.request.command_id,
        campaign_id=port.request.campaign_id,
        job_id=port.request.job_id,
        hub_id="hub-00001",
        action="UPDATE",
        target_version="1.5.0",
        from_version="1.4.2",
        sha256="not-a-sha",
        hardware_revision="revB",
        attempt=1,
        epoch=1,
        seq=1,
        issued_at=port.request.issued_at,
        expires_at=port.request.expires_at,
        key_id="guardian-2026a",
        signature="c2ln",
    )
    with pytest.raises(SchemaValidationError):
        await mqtt_io.publish_firmware_command(client, cfg, command)
    assert client.published == []
