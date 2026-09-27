"""r3.4.3 K13 hold/supersedes queries against the real schema (workspace DB): both statements plan and run,
read-only. The hold-window statement is run for an obligation with no deployments (empty result) -- its
per-type behaviour is covered by tests/unit/invariants/test_k13_hold.py."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from psycopg_pool import AsyncConnectionPool

from opengrid.invariants import queries
from opengrid.platform.config import load_config
from opengrid.platform.db import build_dsn, migrate_sync

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(not os.environ.get("OG_DB"), reason="needs the workspace database (tools/remote.ps1)"),
]


@pytest.fixture
async def pool():
    dsn = build_dsn(load_config())
    migrate_sync(dsn)
    async with AsyncConnectionPool(dsn, min_size=1, max_size=2, open=False) as opened:
        yield opened


async def test_lock_candidates_query_runs_on_the_real_schema(pool):
    now = datetime.now(UTC)
    rows, _ = await queries.fetch_lock_commitment_candidates(pool, since=now - timedelta(days=1), now=now)
    assert isinstance(rows, list)


async def test_hold_window_queries_run_on_the_real_schema(pool):
    now = datetime.now(UTC)
    unknown = uuid4()
    assert (
        await queries.fetch_hold_windows(
            pool,
            obligation_id=unknown,
            committed_kw=5000.0,
            window_start=now - timedelta(hours=1),
            window_end=now,
        )
        is None
    )
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            queries._HOLD_WINDOWS_SQL,
            {
                "obligation_id": unknown,
                "committed_kw": 5000.0,
                "window_start": now - timedelta(hours=1),
                "window_end": now,
            },
        )
        assert await cur.fetchall() == []
