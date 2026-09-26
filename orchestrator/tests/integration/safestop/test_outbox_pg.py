"""`og.stop_outbox` (migration 0035) against the workspace database: queued once per (stop_id, action),
drained oldest first, acknowledged rows never returned again. Rows are removed after the test."""

from __future__ import annotations

import os
from uuid import uuid4

import pytest
from psycopg_pool import AsyncConnectionPool

from opengrid.platform.config import load_config
from opengrid.platform.db import build_dsn, migrate_sync
from opengrid.safestop.pg_backend import PgStopEventBackend

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


async def test_outbox_queues_once_drains_in_order_and_forgets_acknowledged_rows(pool):
    backend = PgStopEventBackend(pool)
    first, second = uuid4(), uuid4()
    try:
        await backend.enqueue_publication(
            stop_id=first, action="ENGAGE", topic_suffix=f"stop/bank/b1/{first}", payload={"n": 1}
        )
        await backend.enqueue_publication(
            stop_id=second, action="ENGAGE", topic_suffix=f"stop/bank/b2/{second}", payload={"n": 2}
        )
        await backend.enqueue_publication(  # the same event again: a no-op
            stop_id=first, action="ENGAGE", topic_suffix=f"stop/bank/b1/{first}", payload={"n": 1}
        )
        mine = [e for e in await backend.pending_publications(limit=1000) if e.stop_id in (first, second)]
        assert [e.stop_id for e in mine] == [first, second]
        assert mine[0].payload == {"n": 1}

        await backend.record_publish_failure(mine[0].seq, "broker down")
        await backend.mark_published(mine[0].seq)
        left = [
            e.stop_id for e in await backend.pending_publications(limit=1000) if e.stop_id in (first, second)
        ]
        assert left == [second]
    finally:
        async with pool.connection() as conn, conn.cursor() as cur:
            await cur.execute("DELETE FROM og.stop_outbox WHERE stop_id = ANY(%s)", ([first, second],))
