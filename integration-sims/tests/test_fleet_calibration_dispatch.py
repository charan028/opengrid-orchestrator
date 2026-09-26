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
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from ogsim.common.clock import FakeClock
from ogsim.common.config import MqttSettings, load_fleet_config
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
