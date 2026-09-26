"""Tests for the `ogsim.fleet.__main__`/`runtime.run_fleet` calibration-command wiring (S6.7, WP-I):
subscribing to `cmd/cal/+` and routing an inbound `CalibrationCommand` to `FleetEngine.handle_
calibration_command` (unmodified) then publishing the ack, narrowed to `calibration_ack.schema.json`'s
own field set, on `ack/cal/<hub_id>`. `ogsim.fleet.calibration`'s own verification/outcome logic is
covered by `test_fleet_calibration.py`; these tests only cover the dispatch/publish glue."""

from __future__ import annotations

import asyncio
import contextlib
import json
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
from ogsim.fleet.runtime import FleetEngine, run_fleet

MQTT = MqttSettings(host="127.0.0.1", port=1883, username="og_sim", password="", topic_root="og/v1")


@dataclass
class _FakeTransport:
    published: list[tuple[str, bytes, int, bool]] = field(default_factory=list)

    async def publish(self, topic: str, payload: bytes, qos: int = 0, retain: bool = False) -> None:
        self.published.append((topic, payload, qos, retain))


@dataclass
class _FakeClient:
    topic_root: str = "og/v1"
    _transport: _FakeTransport = field(default_factory=_FakeTransport)
    subscriptions: list[str] = field(default_factory=list)

    def topic(self, suffix: str) -> str:
        return f"{self.topic_root}/{suffix}"

    async def subscribe(self, suffix: str, qos: int = 1) -> None:
        self.subscriptions.append(suffix)


@pytest.fixture
def engine() -> FleetEngine:
    config = dc_replace(load_fleet_config(), mqtt=MQTT, hub_count=4, bank_count=1, zones=("LZ_NORTH",))
    return FleetEngine(config, seed=1)


def _fake_ack(hub_id: str) -> dict[str, Any]:
    """Mirrors `build_calibration_ack`'s shape, including the internal `outcome`/`reject_reason` fields
    that are NOT part of `calibration_ack.schema.json` (additionalProperties: false)."""
    return {
        "calibration_id": "00000000-0000-0000-0000-000000000001",
        "hub_id": hub_id,
        "applied": True,
        "applied_at": "2026-09-26T12:00:00Z",
        "resulting_offsets": {"freq_hz": 0.0, "voltage_pct": 0.0, "phase_deg": 0.0},
        "status": "APPLIED",
        "outcome": "CORRECTED",
        "reject_reason": None,
    }


async def test_run_fleet_subscribes_to_calibration_commands(engine: FleetEngine) -> None:
    client = _FakeClient()

    async def fake_publish_batch(schema_name: str, items: list, qos: int = 0) -> None:
        return None

    client.publish_batch = fake_publish_batch  # type: ignore[attr-defined]
    clock = FakeClock()
    task = asyncio.create_task(run_fleet(client, engine, clock, Ed25519PrivateKey.generate().public_key()))
    try:
        for _ in range(20):
            await asyncio.sleep(0)
        assert "cmd/cal/+" in client.subscriptions
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def test_dispatch_routes_calibration_command_and_publishes_wire_shaped_ack(
    engine: FleetEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    hub_id = "hub-00000"
    monkeypatch.setattr(engine, "handle_calibration_command", lambda command, key, now: _fake_ack(hub_id))

    client = _FakeClient()
    command = {"hub_id": hub_id, "calibration_id": "00000000-0000-0000-0000-000000000001"}
    await _dispatch_message(
        engine,
        client,  # type: ignore[arg-type]
        "og/v1/cmd/cal/hub-00000",
        json.dumps(command).encode("utf-8"),
        object(),
        object(),
    )

    assert len(client._transport.published) == 1
    topic, payload, qos, retain = client._transport.published[0]
    assert topic == "og/v1/ack/cal/hub-00000"
    assert qos == 1
    assert retain is False
    published = json.loads(payload)
    assert set(published) == {
        "calibration_id",
        "hub_id",
        "applied",
        "applied_at",
        "resulting_offsets",
        "status",
        "reject_reason",  # additive wire field (the internal `outcome` stays stripped)
    }
    assert published["hub_id"] == hub_id
    assert published["status"] == "APPLIED"


async def test_dispatch_drops_ack_that_fails_schema_validation(
    engine: FleetEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A malformed ack (e.g. missing a required field) is logged and dropped, never published --
    BUILD.md S5a's "validate all inbound/outbound messages", never a silent malformed publish."""
    hub_id = "hub-00000"
    bad_ack = _fake_ack(hub_id)
    del bad_ack["status"]
    monkeypatch.setattr(engine, "handle_calibration_command", lambda command, key, now: bad_ack)

    client = _FakeClient()
    command = {"hub_id": hub_id, "calibration_id": "00000000-0000-0000-0000-000000000001"}
    await _dispatch_message(
        engine,
        client,  # type: ignore[arg-type]
        "og/v1/cmd/cal/hub-00000",
        json.dumps(command).encode("utf-8"),
        object(),
        object(),
    )
    assert client._transport.published == []


async def test_a_stale_command_is_acked_as_a_rejection_with_its_reason_and_sequence(
    engine: FleetEngine,
) -> None:
    """#11/#15: a REJECTED ack used to carry `resulting_offsets: null`, failed the wire schema and was never
    published, and its reason was stripped. It is now published with the command's (epoch, seq) and the
    hub's reject_reason, so the orchestrator can bind it and tell a protocol error from inverter drift."""
    guardian_key = Ed25519PrivateKey.generate()
    hub_id = engine.state.hub_ids[0]
    engine.pq.last_calibration_epoch[engine.pq.indices_for_hub(hub_id)] = 1
    engine.pq.last_calibration_seq[engine.pq.indices_for_hub(hub_id)] = 9
    now = datetime.now(UTC)
    command: dict[str, Any] = {
        "calibration_id": "00000000-0000-4000-8000-0000000000c9",
        "hub_id": hub_id,
        "epoch": 1,
        "seq": 3,
        "issued_at": (now - timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
        "expires_at": (now + timedelta(seconds=60)).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
        "reference": {"phase_deg": 0.0, "freq_hz": 60.0, "amplitude_v": 240.0, "sync_source": "ptp"},
        "correction": {"freq_hz": 0.01},
        "bounds": {"max_freq_hz": 0.1, "max_voltage_pct": 2.0, "max_phase_deg": 5.0},
    }
    signed_fields = {k: v for k, v in command.items()}
    command["key_id"] = "guardian-test"
    command["signature"] = sign(guardian_key, signed_fields)
    client = _FakeClient()

    await _dispatch_message(
        engine,
        client,
        f"og/v1/cmd/cal/{hub_id}",
        json.dumps(command).encode(),
        guardian_key.public_key(),
        None,
    )

    (topic, payload, qos, _retain) = client._transport.published[0]
    ack = json.loads(payload)
    assert topic == f"og/v1/ack/cal/{hub_id}" and qos == 1
    assert (ack["status"], ack["reject_reason"], ack["epoch"], ack["seq"]) == ("REJECTED", "STALE_SEQ", 1, 3)
    assert set(ack["resulting_offsets"]) == {"freq_hz", "voltage_pct", "phase_deg"}
