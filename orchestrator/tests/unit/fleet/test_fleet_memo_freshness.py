"""r3.4.3 PERF-OPT: the fleet twin's per-hub memos never serve a stale read. Telemetry updates a hub's runtime in
place (new SoC, power, last-seen, flow and fault objects); the very next `hub_capabilities`/`capability`/engine
`fleet_state` read must show them -- the G-04 ramp anchor reads `p_kw`/`last_seen_at` from these snapshots."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from opengrid import fleet
from opengrid.core.models.platform import Bank, Hub, HubState
from opengrid.engine.gateways import EngineFleetGateway
from opengrid.platform.config import Config

NOW = datetime.now(UTC)


class _Backend:
    async def load_hubs(self) -> list[Hub]:
        return [Hub(hub_id="h1", bank_id="b1", zone="LZ_NORTH", e_kwh=39.2, r_kwh=7.84, p_kw=11.0)]

    async def load_banks(self) -> list[Bank]:
        return [Bank(bank_id="b1", zone="LZ_NORTH", kva_rating=600.0)]

    async def load_hub_states(self) -> list[HubState]:
        return [HubState(hub_id="h1", soc_kwh=30.0, p_kw=-2.0, last_seen_at=NOW - timedelta(seconds=1))]

    async def upsert_hub_states(self, states: list[Any]) -> None: ...

    async def copy_telemetry(self, rows: list[Any]) -> None: ...

    async def insert_acks(self, acks: list[Any]) -> None: ...

    async def record_scada_observations(self, signals: list[Any]) -> None: ...


@pytest.fixture
async def twin():
    saved = (dict(fleet._hubs), dict(fleet._banks), fleet._backend, fleet._thresholds)
    fleet.configure(_Backend(), Config({}))  # type: ignore[arg-type]
    await fleet.load_topology()
    yield
    fleet.configure(saved[2], Config({}))  # type: ignore[arg-type]
    fleet._hubs.update(saved[0])
    fleet._banks.update(saved[1])
    fleet._thresholds = saved[3]


def _telemetry(**overrides: Any) -> dict[str, Any]:
    payload = {
        "hub_id": "h1",
        "bank_id": "b1",
        "zone": "LZ_NORTH",
        "ts": datetime.now(UTC).isoformat(),
        "soc_kwh": 25.0,
        "p_kw": -5.5,
        "health": "online",
        "seq": 2,
        "epoch": 1,
        "meter_kw": -3.0,
    }
    payload.update(overrides)
    return payload


async def test_telemetry_is_visible_on_the_very_next_read(twin: None) -> None:
    (before,) = fleet.hub_capabilities("b1")
    assert fleet.hub_capabilities("b1")[0] is before  # unchanged inputs: the memo answers
    await fleet.ingest_telemetry(_telemetry())
    (after,) = fleet.hub_capabilities("b1")
    assert (after.soc_kwh, after.p_kw, after.meter_kw) == (25.0, -5.5, -3.0)
    assert after.last_seen_at == fleet._hubs["h1"].last_seen_at != before.last_seen_at
    cap = await fleet.capability("b1", NOW)
    assert cap.max_discharge_kw == fleet.bank_capability(
        [fleet.hub_capability(25.0, fleet._hubs["h1"].params)[0]], fleet._banks["b1"].params
    )


async def test_in_place_runtime_writes_are_visible_too(twin: None) -> None:
    fleet.hub_capabilities("b1")
    runtime = fleet._hubs["h1"]
    runtime.p_kw = 0.0  # e.g. a hub_state reload writing the runtime directly
    runtime.soc_kwh = 12.5
    runtime.last_seen_at = datetime.now(UTC)
    (snap,) = fleet.hub_capabilities("b1")
    assert (snap.p_kw, snap.soc_kwh, snap.last_seen_at) == (0.0, 12.5, runtime.last_seen_at)


async def test_a_fault_and_a_silent_hub_are_classified_at_once(twin: None) -> None:
    fleet.hub_capabilities("b1")
    await fleet.ingest_telemetry(_telemetry(health="fault", fault_code="BMS_FAULT"))
    assert fleet.hub_capabilities("b1")[0].health == "fault"
    runtime = fleet._hubs["h1"]
    runtime.fault_code = None
    runtime.last_seen_at = datetime.now(UTC) - timedelta(hours=1)
    assert fleet.hub_capabilities("b1")[0].health == "offline"


async def test_the_engine_fleet_state_follows_telemetry(twin: None) -> None:
    gateway = EngineFleetGateway()
    first = await gateway.fleet_state(["b1"], NOW)
    again = await gateway.fleet_state(["b1"], NOW)
    assert again.hubs[0] is first.hubs[0]
    await fleet.ingest_telemetry(_telemetry(p_kw=-7.25, soc_kwh=20.0))
    (hub,) = (await gateway.fleet_state(["b1"], NOW)).hubs
    assert (hub.p_kw, hub.soc_kwh) == (-7.25, 20.0)
