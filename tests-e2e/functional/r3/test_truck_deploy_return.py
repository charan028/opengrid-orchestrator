"""D-31 end to end (r3.4 review HIGH): a simulated truck is deployed to a customer site and returns to its
depot; the orchestrator refuses to charge it while it is away and allows it once it is home again.

The path is the real one, message by message, with no MQTT broker (a workspace test must never touch the
shared broker, BUILD.md S6):

    ogsim FleetEngine  --scenario/cmd (mobile_deployment_start / mobile_home_station_charge)-->  truck moves
    ogsim device_info message (its position)  --opengrid.fleet.device_info.upsert_device_info-->  og.hub
    og.hub.device_lat/lon/device_info_at  --core.geo.DEVICE_POSITIONS_SQL-->  G-35 port + selector at-home

It needs the workspace database (`OG_DB`, test cluster 5433) with dev/seed/mobile_trucks_seed.sql applied by
the test itself; it is skipped elsewhere. The sim and the orchestrator share no code: they meet only in the
device_info JSON message, validated against interfaces/mqtt/device_info.schema.json on the way in.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import pytest

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not os.environ.get("OG_DB"),
        reason="needs the workspace database (tools/remote.ps1)",
    ),
]

REPO = Path(__file__).resolve().parents[3]
SEED = REPO / "dev" / "seed" / "mobile_trucks_seed.sql"
FLEET_YAML = REPO / "integration-sims" / "config" / "fleet.yaml"
TRUCK = "truck-dfw-01"
SITE = (
    32.7767,
    -96.7970,
)  # a customer site in downtown Dallas, ~17 km from the Irving depot


@pytest.fixture
async def pool():
    from opengrid.platform.config import load_config
    from opengrid.platform.db import build_dsn, migrate_sync
    from psycopg_pool import AsyncConnectionPool

    dsn = build_dsn(load_config())
    migrate_sync(dsn)
    async with AsyncConnectionPool(
        dsn, min_size=1, max_size=2, open=False, kwargs={"autocommit": True}
    ) as opened:
        async with opened.connection() as conn:
            await conn.execute(SEED.read_text(encoding="utf-8"))  # type: ignore[arg-type]
        yield opened


def _engine() -> Any:
    """A one-home fleet plus the shipped truck-dfw-01 (read straight from fleet.yaml, without the broker
    settings `load_fleet_config` resolves), parked at its depot."""
    import yaml
    from ogsim.common.config import FleetConfig, MobileUnitConfig, MqttSettings
    from ogsim.fleet.runtime import FleetEngine

    raw = yaml.safe_load(FLEET_YAML.read_text(encoding="utf-8"))
    truck = MobileUnitConfig(
        **next(u for u in raw["mobile_units"] if u["trailer_id"] == TRUCK)
    )
    mqtt = MqttSettings(
        host="127.0.0.1", port=1883, username="og_sim", password="", topic_root="og/v1"
    )
    return FleetEngine(
        FleetConfig(
            mqtt=mqtt,
            hub_count=1,
            bank_count=1,
            zones=("LZ_NORTH",),
            mobile_units=(truck,),
        )
    )


def _scenario(wire_type: str, params: dict[str, Any]) -> dict[str, Any]:
    from ogsim.common.scenario import utc_timestamp

    return {
        "id": f"e2e-{wire_type.lower()}",
        "type": wire_type,
        "target": {"kind": "asset", "ref": TRUCK},
        "params": params,
        "start": utc_timestamp(time.time()),
    }


async def _report(pool: Any, engine: Any) -> None:
    """The truck's device_info, as the sim publishes it, through the orchestrator's device-info intake."""
    from opengrid.fleet.device_info import upsert_device_info
    from opengrid.trace.pg_backend import PgTraceBackend
    from opengrid.trace.store import TraceStore

    suffix, message = engine.device_info_message(TRUCK, time.time())
    result = await upsert_device_info(
        pool,
        message,
        topic=f"ogtest/e2e/{suffix}",
        trace=TraceStore(PgTraceBackend(pool)),
    )
    assert result.accepted


async def _charge_verdict(pool: Any) -> tuple[bool, bool]:
    """(G-35 passes a 250 kW charge, the selector sees the truck at home), from the orchestrator's own reads."""
    from datetime import UTC, datetime

    from opengrid.core import geo
    from opengrid.guardian import checks
    from opengrid.guardian.repo import ConfigMobileUnitPort
    from opengrid.selector.gate import (
        load_mobile_home_station_sites,
        load_mobile_units,
        mobile_units_at_home,
    )

    sites = load_mobile_home_station_sites()
    port = ConfigMobileUnitPort(load_mobile_units(), sites, pool)
    g35 = checks.check_g35_mobile_charge(
        TRUCK,
        250.0,
        is_mobile=port.is_mobile(TRUCK),
        at_home_station=await port.at_home_station(TRUCK),
    )
    bank = f"bank-{TRUCK}"
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(geo.DEVICE_POSITIONS_SQL, {"ids": [bank]})
        positions = geo.fresh_positions(await cur.fetchall(), datetime.now(UTC))
    return g35.ok, mobile_units_at_home([bank], sites, positions)[bank]


async def test_charging_is_refused_away_from_home_and_allowed_back_at_the_depot(pool):
    engine = _engine()
    idx = engine.state.hub_index[TRUCK]

    # Parked at the depot, reporting: charging allowed by G-35 and planned by the selector.
    await _report(pool, engine)
    assert await _charge_verdict(pool) == (True, True)

    # Deployed to the customer site: the sim moves it and re-reports its position at once.
    assert engine.handle_scenario_cmd(
        _scenario(
            "FLEET_MOBILE_DEPLOYMENT_START",
            {"site_id": "site-dal-01", "site_lat": SITE[0], "site_lon": SITE[1]},
        )
    )
    assert engine.take_mobile_position_changes() == [TRUCK]
    assert (engine.state.lat_deg[idx], engine.state.lon_deg[idx]) == SITE
    await _report(pool, engine)
    assert await _charge_verdict(pool) == (
        False,
        False,
    )  # G-35 vetoes, the selector plans no charging

    # Back at the depot.
    assert engine.handle_scenario_cmd(
        _scenario(
            "FLEET_MOBILE_HOME_STATION_CHARGE", {"home_station_id": "hs-dfw-irving-01"}
        )
    )
    assert engine.take_mobile_position_changes() == [TRUCK]
    await _report(pool, engine)
    assert await _charge_verdict(pool) == (True, True)

    # The seeded og.hub.lat/lon never moved (it is the home station): only the device report did.
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT lat, lon, device_lat, device_lon FROM og.hub WHERE hub_id = %s",
            (TRUCK,),
        )
        lat, lon, device_lat, device_lon = await cur.fetchone()  # type: ignore[misc]
    assert (lat, lon) == (device_lat, device_lon) == (32.8385, -96.973)


async def test_a_truck_that_stops_reporting_is_treated_as_away(pool):
    engine = _engine()
    await _report(pool, engine)
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE og.hub SET device_info_at = now() - interval '1 hour' WHERE hub_id = %s",
            (TRUCK,),
        )
    assert await _charge_verdict(pool) == (False, False)
