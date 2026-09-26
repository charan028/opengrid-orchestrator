"""Tests for `ogsim.fleet.runtime.run_fleet`'s async publish loop -- BLOCKER fix (qa/merge-notes.md
section 12): og-sim-fleet went idle after one telemetry burst per restart, with nothing logged and no
crash/restart to explain why. `run_fleet` must keep publishing telemetry every
`config.telemetry_interval_s` indefinitely, and a single bad tick (a transient publish failure, for
example) must be logged rather than silently ending the loop."""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
from dataclasses import replace as dc_replace
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from ogsim.common.clock import FakeClock
from ogsim.common.config import MqttSettings, load_fleet_config
from ogsim.fleet.runtime import FleetEngine, run_fleet

MQTT = MqttSettings(host="127.0.0.1", port=1883, username="og_sim", password="", topic_root="og/v1")


@dataclass
class _FakeBatchClient:
    """Records every `subscribe`/`publish_batch` call instead of touching a real broker. `fail_next`
    lets a test make one publish raise, to prove the loop survives it."""

    topic_root: str = "og/v1"
    subscriptions: list[str] = field(default_factory=list)
    publish_batches: list[list[tuple[str, dict[str, Any]]]] = field(default_factory=list)
    fail_next: bool = False

    def topic(self, suffix: str) -> str:
        return f"{self.topic_root}/{suffix}"

    async def subscribe(self, suffix: str, qos: int = 1) -> None:
        self.subscriptions.append(suffix)

    async def publish_batch(
        self, schema_name: str, items: list[tuple[str, dict[str, Any]]], qos: int = 0
    ) -> None:
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("simulated transient publish failure")
        self.publish_batches.append(items)


@pytest.fixture
def engine() -> FleetEngine:
    config = dc_replace(load_fleet_config(), mqtt=MQTT, hub_count=4, bank_count=1, zones=("LZ_NORTH",))
    return FleetEngine(config, seed=1)


@pytest.fixture
def guardian_key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


def guardian_key_stub() -> Any:
    return Ed25519PrivateKey.generate().public_key()


async def test_run_fleet_publishes_telemetry_every_tick_indefinitely(engine: FleetEngine) -> None:
    client = _FakeBatchClient()
    clock = FakeClock()
    task = asyncio.create_task(run_fleet(client, engine, clock, guardian_key_stub()))
    try:
        # Let the loop reach its first `clock.sleep` (first tick already published), then advance
        # through several more intervals -- each advance must produce exactly one more publish.
        for _ in range(50):
            await asyncio.sleep(0)
        for tick in range(1, 6):
            clock.advance(engine.config.telemetry_interval_s)
            for _ in range(50):
                await asyncio.sleep(0)
            assert len(client.publish_batches) == tick + 1, "expected one publish per elapsed interval"
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    assert len(client.publish_batches) == 6
    # Every published batch actually carries telemetry for the fleet's hubs.
    for batch in client.publish_batches:
        assert len(batch) == engine.config.hub_count


async def test_run_fleet_survives_a_transient_publish_failure_and_keeps_ticking(
    engine: FleetEngine,
) -> None:
    """Regression: a single bad tick (here, a publish that raises) must be logged and skipped, not
    silently end telemetry for the rest of the process's life."""
    client = _FakeBatchClient()
    clock = FakeClock()
    task = asyncio.create_task(run_fleet(client, engine, clock, guardian_key_stub()))
    try:
        for _ in range(50):
            await asyncio.sleep(0)
        assert len(client.publish_batches) == 1

        client.fail_next = True
        clock.advance(engine.config.telemetry_interval_s)
        for _ in range(50):
            await asyncio.sleep(0)
        # The failing tick did not add a recorded batch (it raised inside publish_batch)...
        assert len(client.publish_batches) == 1

        # ...but the loop is still alive and publishes normally on the next tick.
        clock.advance(engine.config.telemetry_interval_s)
        for _ in range(50):
            await asyncio.sleep(0)
        assert len(client.publish_batches) == 2
        assert not task.done(), "run_fleet must still be running after a transient tick failure"
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
