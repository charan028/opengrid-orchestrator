"""r3.4.3 HIGH-A: the guardian's startup reload of G-04 signed anchors (og.verdict PASS + the batch's
RT_ALLOCATION trace) and its stop-since-signing read run on the real schema (read-only)."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from psycopg_pool import AsyncConnectionPool

from opengrid.guardian.repo import PgSafeStopPort, load_signed_anchors, mark_verdict_published
from opengrid.platform.config import load_config
from opengrid.platform.db import build_dsn, migrate_sync

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(not os.environ.get("OG_DB"), reason="needs the workspace database (tools/remote.ps1)"),
]


async def test_signed_anchor_reload_and_stop_read_run_on_the_real_schema():
    dsn = build_dsn(load_config())
    migrate_sync(dsn)
    now = datetime.now(UTC)
    async with AsyncConnectionPool(dsn, min_size=1, max_size=1, open=False) as pool:
        anchors = await load_signed_anchors(pool, now=now)
        assert all(entry[2] > now for entry in anchors.values())  # live leases only
        assert await PgSafeStopPort(pool).last_engaged_at("BANK", "bank-does-not-exist") is None
        # the publish stamp never raises, with or without the og.verdict.published_at column
        await mark_verdict_published(pool, uuid4())
