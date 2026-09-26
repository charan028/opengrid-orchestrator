"""The guardian's new hand-off queries (K8 stop release, S6.7 calibration queue, G-25 rate limit, G-03
bank load) parse and run against the real schema in the workspace database (`og_t_<ws>`). Read-only
apart from a pg_notify on the workspace DB."""

from __future__ import annotations

import os

import pytest
from psycopg_pool import AsyncConnectionPool

from opengrid.guardian import pq_repo, repo
from opengrid.platform.config import load_config
from opengrid.platform.db import build_dsn, migrate_sync

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(not os.environ.get("OG_DB"), reason="needs the workspace database (tools/remote.ps1)"),
]


@pytest.fixture
async def pool():
    cfg = load_config()
    dsn = build_dsn(cfg)
    migrate_sync(dsn)
    async with AsyncConnectionPool(dsn, min_size=1, max_size=2, open=False) as opened:
        yield opened


async def test_release_port_queries_run(pool):
    port = repo.PgStopReleasePort(pool)
    assert isinstance(await port.pending_requests(max_age_s=300.0), list)
    assert isinstance(await port.outstanding_engages("BANK", "bank-000"), list)
    for kind, ref in (("BANK", "bank-000"), ("ZONE", "LZ_NORTH"), ("FLEET", "FLEET")):
        assert isinstance(await port.banks_in_scope(kind, ref), list)
    assert isinstance(await port.unpublished_release_events(max_age_s=300.0), list)
    await port.hand_to_safestop({"probe": True})


async def test_calibration_and_bank_queries_run(pool):
    assert isinstance(await repo.PgCalibrationQueuePort(pool).pending(max_age_s=600.0), list)
    last = await pq_repo.PgCalibrationHistoryPort(pool).last_attempt_epoch_s("hub-00000")
    assert last is None or isinstance(last, float)
    await repo.PgBankStatePort(pool).snapshot("bank-000")


async def test_service_profile_query_runs(pool):
    from uuid import uuid4

    assert await repo.PgServiceProfilePort(pool).setpoint_source(uuid4()) is None


async def test_as_award_queries_follow_the_engine_coverage_rule(pool):
    """R-GRANT-AS-HOLD reads: an all-AS deployment (obligation_id NULL) covering now counts for any award;
    a cancelled one does not."""
    from uuid import uuid4

    port = repo.PgAsAwardPort(pool)
    award = uuid4()
    assert await port.service_type(award) is None
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO og.as_deployment (obligation_id, start_at, end_at, source, reason)"
            " VALUES (NULL, now() - interval '1 minute', now() + interval '5 minutes', 'SCENARIO', 'it-guard')"
            " RETURNING deployment_id"
        )
        row = await cur.fetchone()
        assert row is not None
        await conn.commit()
    try:
        assert await port.deployment_active(award) is True
        async with pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                "UPDATE og.as_deployment SET cancelled_at = now() WHERE deployment_id = %s", (row[0],)
            )
            await conn.commit()
        assert await port.deployment_active(award) is False
    finally:
        async with pool.connection() as conn, conn.cursor() as cur:
            await cur.execute("DELETE FROM og.as_deployment WHERE deployment_id = %s", (row[0],))
            await conn.commit()


async def test_scope_posture_and_zone_queries_run(pool):
    port = repo.PgScopePosturePort(pool)
    await port.set_posture(
        "BANK", "it-guard-bank", posture="CONSERVATIVE", veto_ratio=0.5, consecutive=1, stop_requested=False
    )
    await port.set_posture(
        "BANK", "it-guard-bank", posture="NORMAL", veto_ratio=0.0, consecutive=0, stop_requested=False
    )
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute("SELECT posture FROM og.scope_posture WHERE scope_ref = 'it-guard-bank'")
        assert (await cur.fetchone()) == ("NORMAL",)
        await cur.execute("DELETE FROM og.scope_posture WHERE scope_ref = 'it-guard-bank'")
        await conn.commit()
    assert isinstance(await repo.load_zones_by_bank(pool), dict)


async def test_flow_topology_and_territory_queries_run(pool):
    """Migration 0029 applies and the G-26..G-33 adapters' queries run against the real schema."""
    import math
    from uuid import uuid4

    from opengrid.guardian import flow_repo
    from opengrid.guardian.config import GuardianConfig

    zones = {"LZ_AEN": "AUSTIN_ENERGY", "LZ_CPS": "CPS_ENERGY"}
    topology = flow_repo.PgGridTopologyPort(pool, GuardianConfig(key_path=""), zones)
    assert await topology.hub_site("no-such-hub") is None
    assert await topology.transformer("no-such-xfmr") is None
    feeder = await topology.feeder_flow("no-such-feeder")
    assert feeder is not None and feeder.flow_kw is None and math.isinf(feeder.age_s)
    assert await topology.substation_flow("no-such-bank") is None
    assert await topology.territory_flow("no-such-bank") is None
    assert await topology.poi_limit("no-such-bank") is None
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute("SELECT bank_id FROM og.bank LIMIT 1")
        row = await cur.fetchone()
    if row is not None:
        await topology.substation_flow(row[0])
        await topology.territory_flow(row[0])

    territory = flow_repo.PgTerritoryPort(pool, zones)
    assert await territory.obligation_market(uuid4()) is None
    assert await territory.free_access("AUSTIN_ENERGY") in (True, False)
    assert await territory.hub_zone("no-such-hub") is None
