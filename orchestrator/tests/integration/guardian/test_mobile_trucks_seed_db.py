"""D-31 trucks on the real schema (workspace database `og_t_<ws>`): migration 0044 admits og.asset
MOBILE_STORAGE, dev/seed/mobile_trucks_seed.sql applies (twice: idempotent), and the guardian's own hub read
(`repo.load_hub_params`) rates every truck at its 500 kW nameplate -- G-02 -- while a home hub keeps the
11 kW single-unit cap. Also the selector's position read (`selector.db` query) and the G-35 at-home port."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from psycopg_pool import AsyncConnectionPool

from opengrid.core.limits import check_hub_power, continuous_power_kw, unit_rating_kw
from opengrid.core.physics import HubParams
from opengrid.guardian import repo
from opengrid.platform.config import load_config
from opengrid.platform.db import build_dsn, migrate_sync
from opengrid.selector.gate import load_mobile_home_station_sites, mobile_units_at_home

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(not os.environ.get("OG_DB"), reason="needs the workspace database (tools/remote.ps1)"),
]

SEED = Path(__file__).resolve().parents[4] / "dev" / "seed" / "mobile_trucks_seed.sql"
TRUCKS = (
    [f"truck-aus-0{i}" for i in (1, 2)]
    + [f"truck-sat-0{i}" for i in (1, 2)]
    + [f"truck-dfw-0{i}" for i in (1, 2, 3, 4)]
)
HOME_HUB = "zz-home-probe-0044"


@pytest.fixture
async def pool():
    cfg = load_config()
    dsn = build_dsn(cfg)
    migrate_sync(dsn)
    async with AsyncConnectionPool(
        dsn, min_size=1, max_size=2, open=False, kwargs={"autocommit": True}
    ) as opened:
        yield opened


async def _seed(pool: AsyncConnectionPool) -> None:
    sql = SEED.read_text(encoding="utf-8")
    async with pool.connection() as conn:
        await conn.execute(sql)  # type: ignore[arg-type]  # a fixed, checked-in script (no parameters)


async def _home_probe(pool: AsyncConnectionPool) -> None:
    """A single-unit home hub on a truck bank's feeder-less twin: seeded at 500 kW, no og.asset row."""
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO og.bank (bank_id, zone, kva_rating) VALUES ('bank-zz-0044', 'LZ_NORTH', 600) "
            "ON CONFLICT (bank_id) DO NOTHING"
        )
        await conn.execute(
            "INSERT INTO og.hub (hub_id, bank_id, zone, e_kwh, r_kwh, p_kw, eta_c, eta_d, units) "
            "VALUES (%s, 'bank-zz-0044', 'LZ_NORTH', 39.2, 7.84, 500, 0.9487, 0.9487, 1) "
            "ON CONFLICT (hub_id) DO NOTHING",
            (HOME_HUB,),
        )


async def test_seed_is_idempotent_and_writes_mobile_storage_assets(pool):
    await _seed(pool)
    await _seed(pool)
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT h.hub_id, h.units, a.asset_class, a.bank_id, a.p_kw FROM og.hub h "
            "JOIN og.asset a ON a.asset_id = h.hub_id WHERE h.hub_id = ANY(%s) ORDER BY 1",
            (TRUCKS,),
        )
        rows = await cur.fetchall()
    assert [r[0] for r in rows] == sorted(TRUCKS)
    for hub_id, units, asset_class, bank_id, p_kw in rows:
        assert (units, asset_class, bank_id, float(p_kw)) == (1, "MOBILE_STORAGE", f"bank-{hub_id}", 500.0)


async def test_guardian_rates_trucks_at_nameplate_and_homes_at_the_unit_cap(pool):
    await _seed(pool)
    await _home_probe(pool)
    hubs = await repo.load_hub_params(pool)
    for hub_id in TRUCKS:
        params = hubs[hub_id].params
        assert params.utility_scale is True and params.units == 1
        assert continuous_power_kw(params) == 500.0
        assert check_hub_power(-500.0, params).ok and not check_hub_power(-500.5, params).ok
    home = hubs[HOME_HUB].params
    assert home.utility_scale is False
    assert unit_rating_kw(home.units) == 11.0 and continuous_power_kw(home) == 11.0
    assert continuous_power_kw(HubParams(e_kwh=1000.0, r_kwh=200.0, p_kw=500.0, units=1)) == 11.0


async def _report(pool: AsyncConnectionPool, lat: float | None, lon: float | None, age: str | None) -> None:
    """Record every truck's device-reported position `age` ago (as the device-info intake does)."""
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE og.hub SET device_lat = %s, device_lon = %s, "
            "device_info_at = CASE WHEN %s::interval IS NULL THEN NULL ELSE now() - %s::interval END "
            "WHERE hub_id = ANY(%s)",
            (lat, lon, age, age, TRUCKS),
        )


async def _at_home(pool: AsyncConnectionPool) -> tuple[dict[str, bool], list[bool | None]]:
    """(selector view by bank id, G-35 view per truck hub) through the one shared position query."""
    from datetime import UTC, datetime

    from opengrid.core import geo

    ids = [f"bank-{t}" for t in TRUCKS]
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(geo.DEVICE_POSITIONS_SQL, {"ids": ids})
        positions = geo.fresh_positions(await cur.fetchall(), datetime.now(UTC))
    selector = mobile_units_at_home(ids, load_mobile_home_station_sites(), positions)
    port = repo.ConfigMobileUnitPort(ids, load_mobile_home_station_sites(), pool)
    return selector, [await port.at_home_station(t) for t in TRUCKS]


async def test_trucks_are_at_home_only_on_a_fresh_device_report_from_the_depot(pool):
    """The seed puts og.hub.lat/lon at the depot, but that is never evidence: with no device report every
    truck is unknown (away, fail closed); a fresh report from the depot is at home; the same report once stale,
    or a fresh one from a deployment site, is not."""
    await _seed(pool)
    ids = [f"bank-{t}" for t in TRUCKS]

    await _report(pool, None, None, None)
    selector, g35 = await _at_home(pool)
    assert selector == dict.fromkeys(ids, False) and g35 == [None] * len(TRUCKS)

    # A fresh report from each truck's own depot: og.hub.lat/lon is the seeded home station.
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE og.hub SET device_lat = lat, device_lon = lon, device_info_at = now() - interval '5 seconds' "
            "WHERE hub_id = ANY(%s)",
            (TRUCKS,),
        )
    selector, g35 = await _at_home(pool)
    assert selector == dict.fromkeys(ids, True) and g35 == [True] * len(TRUCKS)

    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE og.hub SET device_info_at = now() - interval '1 hour' WHERE hub_id = ANY(%s)", (TRUCKS,)
        )
    selector, g35 = await _at_home(pool)
    assert selector == dict.fromkeys(ids, False) and g35 == [None] * len(TRUCKS)

    await _report(pool, 31.9973, -102.0779, "5 seconds")  # all deployed to a Midland site
    selector, g35 = await _at_home(pool)
    assert selector == dict.fromkeys(ids, False) and g35 == [False] * len(TRUCKS)
