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
    publish_batches_by_schema: list[tuple[str, list[tuple[str, dict[str, Any]]]]] = field(
        default_factory=list
    )
    fail_next: bool = False
    publish_validated_calls: list[tuple[str, str, dict[str, Any], int, bool]] = field(default_factory=list)

    def topic(self, suffix: str) -> str:
        return f"{self.topic_root}/{suffix}"

    async def subscribe(self, suffix: str, qos: int = 1) -> None:
        self.subscriptions.append(suffix)

    async def publish_validated(
        self, schema_name: str, suffix: str, message: dict[str, Any], qos: int = 0, retain: bool = False
    ) -> None:
        # Device-info records this call (bug fix, 2026-09-26, R3) -- distinct from `publish_batch`
        # so `_PUBLISHES_PER_TICK`'s per-tick accounting below is unaffected (this fires once, at
        # connect, before the tick loop starts).
        self.publish_validated_calls.append((schema_name, suffix, message, qos, retain))

    async def publish_batch(
        self, schema_name: str, items: list[tuple[str, dict[str, Any]]], qos: int = 0
    ) -> None:
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("simulated transient publish failure")
        self.publish_batches.append(items)
        self.publish_batches_by_schema.append((schema_name, items))

    def telemetry_batches(self) -> list[list[tuple[str, dict[str, Any]]]]:
        return [items for schema, items in self.publish_batches_by_schema if schema == "telemetry"]


# `run_fleet` makes one `publish_batch` call per PHYSICS tick for each of: the WP-H PQ waveform
# summary and the WP-H rotating raw-capture audit sample (S6.4/S7.4) -- both independently rate-gated
# by their own schedulers, so evaluating them every physics tick is correct even though telemetry
# itself now only publishes every `telemetry_interval_s` (V-32, 2026-09-26: decoupled from the 2 s
# physics tick, see `FleetConfig.physics_tick_interval_s`'s docstring).
_PUBLISHES_PER_PHYSICS_TICK_EXCLUDING_TELEMETRY = 2
# When telemetry IS due on a given physics tick (e.g. the very first tick, or once
# telemetry_interval_s has actually elapsed), all three publish together.
_PUBLISHES_PER_TICK = 3


@pytest.fixture
def engine() -> FleetEngine:
    # Explicit cadences (not just inherited from the shipped fleet.yaml) so this test's timing math
    # is stable regardless of future config changes: a 5:1 telemetry:physics ratio, matching
    # production (V-32, 2026-09-26).
    config = dc_replace(
        load_fleet_config(),
        mqtt=MQTT,
        hub_count=4,
        bank_count=1,
        zones=("LZ_NORTH",),
        physics_tick_interval_s=2.0,
        telemetry_interval_s=10.0,
    )
    return FleetEngine(config, seed=1)


@pytest.fixture
def guardian_key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


def guardian_key_stub() -> Any:
    return Ed25519PrivateKey.generate().public_key()


async def test_run_fleet_physics_ticks_every_2s_but_telemetry_only_every_10s(
    engine: FleetEngine,
) -> None:
    """V-32, 2026-09-26: physics (and the wave/PQ-audit publishes, both independently rate-gated by
    their own schedulers) run on `physics_tick_interval_s` (2 s); telemetry only publishes once
    `telemetry_interval_s` (10 s, a 5:1 ratio in this fixture) has actually elapsed."""
    client = _FakeBatchClient()
    clock = FakeClock()
    task = asyncio.create_task(run_fleet(client, engine, clock, guardian_key_stub()))
    try:
        for _ in range(50):
            await asyncio.sleep(0)  # let the loop reach its first sleep (first tick already published)
        assert len(client.telemetry_batches()) == 1  # always publishes on the very first tick

        ratio = round(engine.config.telemetry_interval_s / engine.config.physics_tick_interval_s)
        assert ratio == 5
        for physics_tick in range(1, ratio):  # physics ticks 2..5: telemetry not yet due
            clock.advance(engine.config.physics_tick_interval_s)
            for _ in range(50):
                await asyncio.sleep(0)
            assert len(client.telemetry_batches()) == 1, (
                f"telemetry re-published early at tick {physics_tick + 1}"
            )
            # Every physics tick still publishes the wave summary + raw-audit batches.
            assert len(client.publish_batches_by_schema) == (physics_tick + 1) * 2 + 1

        clock.advance(engine.config.physics_tick_interval_s)  # the 5th physics tick: telemetry is due
        for _ in range(50):
            await asyncio.sleep(0)
        assert len(client.telemetry_batches()) == 2
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    # Every published telemetry batch actually carries telemetry for the fleet's hubs.
    for batch in client.telemetry_batches():
        # Not `engine.config.hub_count`: a config-driven substation asset (D-29(b)) adds hubs on top
        # of the base home-fleet count, so the real fleet size is `len(engine.state.hub_ids)`.
        assert len(batch) == len(engine.state.hub_ids)


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
        assert len(client.publish_batches) == _PUBLISHES_PER_TICK

        client.fail_next = True
        clock.advance(engine.config.telemetry_interval_s)
        for _ in range(50):
            await asyncio.sleep(0)
        # The failing tick did not add ANY recorded batch for that tick (the first
        # publish_batch call -- telemetry -- raised, and the whole tick body is wrapped
        # in one try/except, so the two PQ-waveform publishes after it never ran either).
        assert len(client.publish_batches) == _PUBLISHES_PER_TICK

        # ...but the loop is still alive and publishes normally on the next tick.
        clock.advance(engine.config.telemetry_interval_s)
        for _ in range(50):
            await asyncio.sleep(0)
        assert len(client.publish_batches) == 2 * _PUBLISHES_PER_TICK
        assert not task.done(), "run_fleet must still be running after a transient tick failure"
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
