"""The guardian hand-off (G-36 then sign, trace, mark signed, publish -- K3/K10), the signed wire model
against firmware_command.schema.json (and the hub's verification input), and the status ingest parser."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import jsonschema

from opengrid.core.crypto import canonicalize_json, generate_keypair, sign_payload, verify_payload
from opengrid.firmware.config import FirmwareConfig
from opengrid.firmware.guardian_flow import PendingFirmwareCommand, process_pending_firmware
from opengrid.firmware.ingest import parse_status
from opengrid.firmware.model import FirmwareCommand
from opengrid.guardian.firmware_check import FirmwareCampaignGrant, FirmwareHubFacts

from .fakes import CATALOGUE_CONFIG, SHA_150

MQTT_DIR = Path(__file__).resolve().parents[4] / "interfaces" / "mqtt"
NOW = datetime.now(UTC)
CFG = FirmwareConfig(catalogue=CATALOGUE_CONFIG)


def _validator(name: str) -> Any:
    schema = json.loads((MQTT_DIR / name).read_text(encoding="utf-8"))
    cls = jsonschema.validators.validator_for(schema)
    cls.check_schema(schema)
    return cls(schema, format_checker=cls.FORMAT_CHECKER)


class Port:
    def __init__(self, facts: FirmwareHubFacts, grant: FirmwareCampaignGrant) -> None:
        self.facts, self.grant_value = facts, grant
        self.request = PendingFirmwareCommand(
            command_id=uuid4(),
            job_id=uuid4(),
            campaign_id=uuid4(),
            hub_id="hub-00001",
            attempt=1,
            action="UPDATE",
            target_version="1.5.0",
            from_version="1.4.2",
            sha256=SHA_150,
            hardware_revision="revB",
            issued_at=NOW,
            expires_at=NOW + timedelta(seconds=120),
        )
        self.log: list[str] = []
        self.seq = 0
        self.refused: dict[UUID, str] = {}

    async def pending(self) -> list[PendingFirmwareCommand]:
        return [self.request]

    async def grant(self, campaign_id: UUID) -> FirmwareCampaignGrant | None:
        return self.grant_value

    async def hub_facts(
        self, hub_id: str, *, lookahead_s: float, update_timeout_s: float
    ) -> FirmwareHubFacts:
        return self.facts

    async def catalogue_rows(self) -> list[dict[str, Any]]:
        return []

    async def reserve(self, command_id: UUID, hub_id: str) -> tuple[int, int] | None:
        self.log.append("reserve")
        self.seq += 1
        return (1, self.seq)

    async def mark_signed(self, command_id: UUID, payload: dict[str, Any]) -> None:
        self.log.append("mark_signed")

    async def refuse(self, command_id: UUID, reason: str) -> None:
        self.refused[command_id] = reason

    async def mark_published(self, command_id: UUID) -> None:
        self.log.append("mark_published")


FACTS = FirmwareHubFacts(
    hub_id="hub-00001", online=True, safe_stopped=False, soc_kwh=30.0, reserve_kwh=7.84, capacity_kwh=39.2,
    already_updating=False, hardware_revision="revB", current_version="1.4.2", committed_in_window=False,
    bank_in_flight=0, bank_hub_count=20, feeder_in_flight=0, feeder_hub_count=40,
)  # fmt: skip
GRANT = FirmwareCampaignGrant("RUNNING", False, False, False, 10.0, 10.0)


async def test_eligible_command_is_signed_traced_then_published():
    seed, public = generate_keypair()
    port = Port(FACTS, GRANT)
    published: list[FirmwareCommand] = []
    traces: list[dict[str, Any]] = []

    async def publish(command: FirmwareCommand) -> None:
        port.log.append("publish")
        published.append(command)

    async def trace(stream: str, event_class: str, payload: dict[str, Any]) -> None:
        port.log.append("trace")
        traces.append(payload)

    count = await process_pending_firmware(
        port, sign=lambda p: sign_payload(seed, p), publish=publish, trace=trace, cfg=CFG, key_id="g1"
    )
    assert count == 1
    assert port.log == ["reserve", "trace", "mark_signed", "publish", "mark_published"]
    command = published[0]
    assert (command.epoch, command.seq) == (1, 1)
    assert verify_payload(public, command.signing_payload(), command.signature)
    wire = command.model_dump(mode="json")
    assert not list(_validator("firmware_command.schema.json").iter_errors(wire))
    # The hub verifies JCS(every field except key_id/signature) -- exactly signing_payload().
    hub_fields = {k: v for k, v in wire.items() if k not in ("key_id", "signature")}
    assert canonicalize_json(hub_fields) == canonicalize_json(command.signing_payload())
    assert traces[0]["outcome"] == "SIGNED"


async def test_ineligible_command_is_refused_and_never_signed():
    port = Port(replace(FACTS, safe_stopped=True), GRANT)
    published: list[FirmwareCommand] = []

    async def publish(command: FirmwareCommand) -> None:
        published.append(command)

    async def trace(stream: str, event_class: str, payload: dict[str, Any]) -> None:
        return None

    count = await process_pending_firmware(
        port, sign=lambda p: "sig", publish=publish, trace=trace, cfg=CFG, key_id="g1"
    )
    assert count == 0 and not published and "reserve" not in port.log
    assert port.refused == {port.request.command_id: "FIRMWARE_HUB_SAFE_STOPPED"}


async def test_failed_trace_withholds_the_signed_command():
    seed, _ = generate_keypair()
    port = Port(FACTS, GRANT)
    published: list[FirmwareCommand] = []

    async def publish(command: FirmwareCommand) -> None:
        published.append(command)

    async def trace(stream: str, event_class: str, payload: dict[str, Any]) -> None:
        raise RuntimeError("trace store down")

    count = await process_pending_firmware(
        port, sign=lambda p: sign_payload(seed, p), publish=publish, trace=trace, cfg=CFG, key_id="g1"
    )
    assert count == 0 and not published


def test_status_schema_accepts_hub_messages_and_parser_normalises_ts():
    status = {
        "hub_id": "hub-00001",
        "command_id": str(uuid4()),
        "epoch": 1,
        "seq": 3,
        "state": "INSTALLING",
        "reason": None,
        "progress_pct": 40.0,
        "firmware_version": "1.4.2",
        "target_version": "1.5.0",
        "ts": "2026-09-26T20:00:05Z",
    }
    validator = _validator("firmware_status.schema.json")
    assert not list(validator.iter_errors(status))
    assert list(validator.iter_errors({**status, "state": "EXPLODED"}))
    parsed = parse_status(status)
    assert parsed["ts"] == datetime(2026, 9, 26, 20, 0, 5, tzinfo=UTC)
    assert parsed["state"] == "INSTALLING"


def test_command_schema_rejects_arbitrary_versions_and_bad_hashes():
    validator = _validator("firmware_command.schema.json")
    base = {
        "command_id": str(uuid4()), "campaign_id": str(uuid4()), "job_id": str(uuid4()), "hub_id": "h",
        "action": "UPDATE", "target_version": "1.5.0", "sha256": SHA_150, "hardware_revision": "revB",
        "attempt": 1, "epoch": 1, "seq": 1, "issued_at": "2026-09-26T20:00:00Z",
        "expires_at": "2026-09-26T20:02:00Z", "key_id": "g1", "signature": "x",
    }  # fmt: skip
    assert not list(validator.iter_errors(base))
    assert list(validator.iter_errors({**base, "target_version": "latest"}))
    assert list(validator.iter_errors({**base, "sha256": "ABC"}))
    assert list(validator.iter_errors({**base, "action": "FLASH"}))
    assert list(validator.iter_errors({k: v for k, v in base.items() if k != "signature"}))
