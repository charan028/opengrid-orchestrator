"""Tests for the FLEET-SIM wiring of the FIRMWARE lane's `ogsim.fleet.firmware.FirmwareManager` into
`FleetEngine`/`run_fleet`/`ogsim.fleet.__main__` (OWNER DECISION, 2026-09-26, R3.1: firmware updates
ship in the final release). `FirmwareManager`'s own verification/timeline logic is the FIRMWARE lane's
own responsibility; these tests only cover the glue: engine construction and delegation, telemetry
suppression while updating, the async publish loop (ack/fw/<hub_id> + device_info republish), the
`__main__` dispatch route, and config loading of the `firmware:` YAML block."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import time
import uuid
from dataclasses import dataclass, field
from dataclasses import replace as dc_replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from ogsim.common.clock import FakeClock
from ogsim.common.config import MqttSettings, load_fleet_config
from ogsim.common.crypto import sign
from ogsim.fleet.__main__ import _dispatch_message
from ogsim.fleet.device_info import initial_firmware_version, initial_hardware_revision
from ogsim.fleet.runtime import FleetEngine, run_fleet

MQTT = MqttSettings(host="127.0.0.1", port=1883, username="og_sim", password="", topic_root="og/v1")


@pytest.fixture
def engine() -> FleetEngine:
    config = dc_replace(load_fleet_config(), mqtt=MQTT, hub_count=4, bank_count=1, zones=("LZ_NORTH",))
    return FleetEngine(config, seed=1)


@pytest.fixture
def guardian_key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


def _firmware_command(
    hub_id: str, guardian_key: Ed25519PrivateKey, *, hardware_revision: str, target_version: str = "1.9.0"
) -> dict[str, Any]:
    now = datetime.now(UTC)
    command: dict[str, Any] = {
        "command_id": str(uuid.uuid4()),
        "campaign_id": str(uuid.uuid4()),
        "job_id": str(uuid.uuid4()),
        "hub_id": hub_id,
        "action": "UPDATE",
        "target_version": target_version,
        "sha256": hashlib.sha256(target_version.encode()).hexdigest(),
        "hardware_revision": hardware_revision,
        "attempt": 1,
        "epoch": 1,
        "seq": 1,
        "issued_at": (now - timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
        "expires_at": (now + timedelta(seconds=60)).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
    }
    signed_fields = dict(command)
    command["key_id"] = "guardian-test"
    command["signature"] = sign(guardian_key, signed_fields)
    return command


# ---- FleetEngine construction/delegation --------------------------------------------------------


def test_engine_seeds_firmware_manager_matching_device_info_defaults(engine: FleetEngine) -> None:
    """The engine's `FirmwareManager` must start every hub at exactly the version/hardware_revision
    its OWN device_info would report -- otherwise the very first command's hardware_revision check
    could spuriously mismatch."""
    for hub_id in engine.state.hub_ids:
        assert engine.firmware.version(hub_id) == initial_firmware_version(hub_id)
        assert engine.firmware.hardware_revision(hub_id) == initial_hardware_revision(hub_id)


def test_handle_firmware_command_accepts_a_valid_command(engine: FleetEngine, guardian_key) -> None:
    hub_id = engine.state.hub_ids[0]
    command = _firmware_command(hub_id, guardian_key, hardware_revision=initial_hardware_revision(hub_id))
    status = engine.handle_firmware_command(command, guardian_key.public_key(), time.time())
    assert status["state"] == "ACCEPTED"
    assert status["hub_id"] == hub_id
    assert engine.firmware.is_updating(hub_id) is True


def test_handle_firmware_command_rejects_a_hardware_revision_mismatch(
    engine: FleetEngine, guardian_key
) -> None:
    hub_id = engine.state.hub_ids[0]
    command = _firmware_command(hub_id, guardian_key, hardware_revision="RevZ-not-a-real-revision")
    status = engine.handle_firmware_command(command, guardian_key.public_key(), time.time())
    assert (status["state"], status["reason"]) == ("FAILED", "HARDWARE_INCOMPATIBLE")


# ---- telemetry suppression while updating --------------------------------------------------------


def test_telemetry_messages_skips_a_hub_mid_firmware_update(engine: FleetEngine, guardian_key) -> None:
    hub_id = engine.state.hub_ids[0]
    command = _firmware_command(hub_id, guardian_key, hardware_revision=initial_hardware_revision(hub_id))
    status = engine.handle_firmware_command(command, guardian_key.public_key(), time.time())
    assert status["state"] == "ACCEPTED"

    engine.tick(0.0)
    messages = engine.telemetry_messages(0.0)
    assert not any(msg["hub_id"] == hub_id for _topic, msg in messages)
    # every OTHER hub still publishes normally.
    assert len(messages) == len(engine.state.hub_ids) - 1


def test_device_info_message_reflects_the_firmware_managers_current_version(engine: FleetEngine) -> None:
    hub_id = engine.state.hub_ids[0]
    suffix, message = engine.device_info_message(hub_id, 0.0)
    assert suffix == f"hub/{hub_id}/info"
    assert message["firmware_version"] == initial_firmware_version(hub_id)
    assert message["hardware_revision"] == initial_hardware_revision(hub_id)


# ---- __main__ dispatch ----------------------------------------------------------------------------


@dataclass
class _FakeTransport:
    published: list[tuple[str, bytes, int, bool]] = field(default_factory=list)

    async def publish(self, topic: str, payload: bytes, qos: int = 0, retain: bool = False) -> None:
        self.published.append((topic, payload, qos, retain))


@dataclass
class _FakeClient:
    topic_root: str = "og/v1"
    _transport: _FakeTransport = field(default_factory=_FakeTransport)

    def topic(self, suffix: str) -> str:
        return f"{self.topic_root}/{suffix}"

    async def publish_validated(
        self, schema_name: str, suffix: str, message: dict[str, Any], qos: int = 0, retain: bool = False
    ) -> None:
        self._transport.published.append(
            (self.topic(suffix), json.dumps(message).encode("utf-8"), qos, retain)
        )


async def test_dispatch_routes_a_firmware_command_and_publishes_the_ack(
    engine: FleetEngine, guardian_key
) -> None:
    hub_id = engine.state.hub_ids[0]
    command = _firmware_command(hub_id, guardian_key, hardware_revision=initial_hardware_revision(hub_id))
    client = _FakeClient()

    await _dispatch_message(
        engine,
        client,  # type: ignore[arg-type]
        f"og/v1/cmd/fw/{hub_id}",
        json.dumps(command).encode("utf-8"),
        guardian_key.public_key(),
        None,
    )

    assert len(client._transport.published) == 1
    topic, payload, qos, retain = client._transport.published[0]
    assert topic == f"og/v1/ack/fw/{hub_id}"
    assert qos == 1
    assert retain is False
    published = json.loads(payload)
    assert published["state"] == "ACCEPTED"
    assert published["hub_id"] == hub_id
    # Every published field is one firmware_status.schema.json actually allows (additionalProperties:
    # false) -- proves `_handle_firmware_command` needs no field-narrowing, unlike calibration's ack.
    assert set(published) <= {
        "hub_id",
        "command_id",
        "epoch",
        "seq",
        "state",
        "reason",
        "progress_pct",
        "firmware_version",
        "target_version",
        "ts",
    }


# ---- run_fleet: ack/fw publishing + device_info republish on completion --------------------------


@dataclass
class _FakeBatchClient:
    topic_root: str = "og/v1"
    subscriptions: list[str] = field(default_factory=list)
    publish_batches_by_schema: list[tuple[str, list[tuple[str, dict[str, Any]]]]] = field(
        default_factory=list
    )
    publish_validated_calls: list[tuple[str, str, dict[str, Any], int, bool]] = field(default_factory=list)

    def topic(self, suffix: str) -> str:
        return f"{self.topic_root}/{suffix}"

    async def subscribe(self, suffix: str, qos: int = 1) -> None:
        self.subscriptions.append(suffix)

    async def publish_validated(
        self, schema_name: str, suffix: str, message: dict[str, Any], qos: int = 0, retain: bool = False
    ) -> None:
        self.publish_validated_calls.append((schema_name, suffix, message, qos, retain))

    async def publish_batch(
        self, schema_name: str, items: list[tuple[str, dict[str, Any]]], qos: int = 0
    ) -> None:
        self.publish_batches_by_schema.append((schema_name, items))

    def firmware_status_calls(self) -> list[dict[str, Any]]:
        return [msg for name, _s, msg, _q, _r in self.publish_validated_calls if name == "firmware_status"]

    def device_info_calls(self) -> list[tuple[str, dict[str, Any]]]:
        return [(s, msg) for name, s, msg, _q, _r in self.publish_validated_calls if name == "device_info"]


def guardian_key_stub() -> Any:
    return Ed25519PrivateKey.generate().public_key()


async def test_run_fleet_subscribes_to_firmware_commands(engine: FleetEngine) -> None:
    client = _FakeBatchClient()
    clock = FakeClock()
    task = asyncio.create_task(run_fleet(client, engine, clock, guardian_key_stub()))
    try:
        for _ in range(50):
            await asyncio.sleep(0)
        assert "cmd/fw/+" in client.subscriptions
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def test_run_fleet_publishes_firmware_progress_and_republishes_device_info_on_completion(
    guardian_key,
) -> None:
    # A short, deterministic update duration: FirmwareSimConfig is frozen, so this is set via
    # FleetConfig.firmware (the same dict the shipped fleet.yaml's `firmware:` block populates), not
    # by mutating the manager's config post-construction.
    config = dc_replace(
        load_fleet_config(),
        mqtt=MQTT,
        hub_count=4,
        bank_count=1,
        zones=("LZ_NORTH",),
        firmware={"duration_min_s": 4.0, "duration_max_s": 4.0, "failure_rate": 0.0, "images": {}},
    )
    engine = FleetEngine(config, seed=1)
    hub_id = engine.state.hub_ids[0]

    t0 = time.time()
    command = _firmware_command(
        hub_id, guardian_key, hardware_revision=initial_hardware_revision(hub_id), target_version="9.9.9"
    )
    status = engine.handle_firmware_command(command, guardian_key.public_key(), t0)
    assert status["state"] == "ACCEPTED"

    client = _FakeBatchClient()
    clock = FakeClock(start=t0)
    task = asyncio.create_task(run_fleet(client, engine, clock, guardian_key_stub()))
    try:
        for _ in range(50):
            await asyncio.sleep(0)  # let run_fleet publish its initial connect-time device_info
        # Advance past the 4s update: physics_tick_interval_s is 2.0 by default -> 3 ticks covers it.
        for _ in range(3):
            clock.advance(engine.config.physics_tick_interval_s)
            for _ in range(50):
                await asyncio.sleep(0)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    statuses = client.firmware_status_calls()
    assert any(s["hub_id"] == hub_id and s["state"] == "DONE" for s in statuses)
    # The hub is no longer mid-update, and a fresh device_info was republished with the new version.
    assert engine.firmware.is_updating(hub_id) is False
    device_info_msgs = client.device_info_calls()
    assert any(
        suffix == f"hub/{hub_id}/info" and msg["firmware_version"] == "9.9.9"
        for suffix, msg in device_info_msgs
    )


# ---- config: firmware: YAML block ------------------------------------------------------------------


def test_shipped_fleet_yaml_firmware_block_matches_firmwaresimconfig_defaults() -> None:
    from ogsim.fleet.firmware import FirmwareSimConfig

    config = load_fleet_config()
    built = FirmwareSimConfig(**config.firmware)
    defaults = FirmwareSimConfig()
    assert (built.duration_min_s, built.duration_max_s, built.failure_rate, built.images) == (
        defaults.duration_min_s,
        defaults.duration_max_s,
        defaults.failure_rate,
        defaults.images,
    )


def test_firmware_config_merges_partial_yaml_onto_defaults() -> None:
    from ogsim.common.config import _firmware_from_raw

    default = {"duration_min_s": 30.0, "duration_max_s": 90.0, "failure_rate": 0.0, "images": {}}
    merged = _firmware_from_raw({"failure_rate": 0.1}, default)
    assert merged["failure_rate"] == 0.1
    assert merged["duration_min_s"] == 30.0  # untouched fields fall through to the default


def test_firmware_config_ignores_an_unknown_field_and_a_non_dict_block() -> None:
    from ogsim.common.config import _firmware_from_raw

    default = {"duration_min_s": 30.0}
    assert _firmware_from_raw({"typo_field": 1}, default) == default
    assert _firmware_from_raw("not-a-dict", default) == default


def test_firmware_config_casts_failure_kinds_to_a_tuple() -> None:
    from ogsim.common.config import _firmware_from_raw

    merged = _firmware_from_raw({"failure_kinds": ["INSTALL_ERROR", "BOOT_FAILED"]}, {})
    assert merged["failure_kinds"] == ("INSTALL_ERROR", "BOOT_FAILED")
