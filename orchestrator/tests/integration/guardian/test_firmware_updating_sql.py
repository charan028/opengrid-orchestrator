"""r3.4.3: the guardian's firmware-updating read (G-19 capability evidence) runs on the real schema (read-only)."""

from __future__ import annotations

import os

import pytest
from psycopg_pool import AsyncConnectionPool

from opengrid.guardian.repo import PgFirmwareUpdatingPort
from opengrid.platform.config import load_config
from opengrid.platform.db import build_dsn, migrate_sync

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(not os.environ.get("OG_DB"), reason="needs the workspace database (tools/remote.ps1)"),
]


async def test_updating_hub_read_runs_on_the_real_schema():
    dsn = build_dsn(load_config())
    migrate_sync(dsn)
    async with AsyncConnectionPool(dsn, min_size=1, max_size=1, open=False) as pool:
        assert await PgFirmwareUpdatingPort(pool).updating_hub_ids("bank-does-not-exist") == set()
