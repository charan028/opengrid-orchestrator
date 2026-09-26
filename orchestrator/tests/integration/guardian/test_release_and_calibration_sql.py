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
