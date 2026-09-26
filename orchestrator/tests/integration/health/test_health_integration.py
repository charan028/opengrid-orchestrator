"""Integration tests for `opengrid.health` against real Postgres (`og_t_hlth` on the server, BUILD.md
S5): heartbeat/hub-health classification round-tripping through the real `og.heartbeat`/`og.hub_state`
tables, and alert raise/clear through `og.alert`.

Run via `powershell -File tools\\remote.ps1 -Ws hlth -Cmd "cd orchestrator && bash tools/check.sh"`.
Skipped automatically when no database is reachable.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

import opengrid.health as health
from opengrid.platform.config import load_config
from opengrid.platform.db import build_dsn, migrate_sync

pytestmark = pytest.mark.asyncio

_CONFIG_PATH = os.environ.get(
    "OG_CONFIG", str((__file__.rsplit("orchestrator", 1)[0]) + "orchestrator/config/test.toml")
)


def _dsn() -> str | None:
    try:
        return build_dsn(load_config(_CONFIG_PATH))
    except Exception:
        return None


def _db_reachable(dsn: str) -> bool:
    try:
        with psycopg.connect(dsn, connect_timeout=2):
            return True
    except Exception:
        return False


_DSN = _dsn()
requires_db = pytest.mark.skipif(
    _DSN is None or not _db_reachable(_DSN),
    reason="Postgres not reachable locally; run via tools/remote.ps1 -Ws hlth (BUILD.md S5)",
)


@requires_db
async def test_evaluate_hub_health_round_trips_through_real_tables() -> None:
    assert _DSN is not None
    migrate_sync(_DSN)
    pool = AsyncConnectionPool(_DSN, min_size=1, max_size=4, open=False)
    await pool.open(wait=True)
    try:
        hub_id, bank_id = f"hub-{uuid4()}", f"bank-{uuid4()}"
        async with pool.connection() as conn:
            await conn.execute(
                "INSERT INTO og.bank (bank_id, zone, kva_rating) VALUES (%s, 'LZ_NORTH', 100)",
                (bank_id,),
            )
            await conn.execute(
                "INSERT INTO og.hub (hub_id, bank_id, zone, e_kwh, r_kwh, p_kw) "
                "VALUES (%s, %s, 'LZ_NORTH', 10, 10, 5)",
                (hub_id, bank_id),
            )
            await conn.execute(
                "INSERT INTO og.hub_state (hub_id, soc_kwh, p_kw, last_seen_at) VALUES (%s, 5, 0, %s)",
                (hub_id, datetime.now(UTC) - timedelta(seconds=60)),
            )
            await conn.commit()

        health.configure(pool, load_config(_CONFIG_PATH))
        await health.evaluate_hub_health()

        async with pool.connection() as conn:
            cur = await conn.execute("SELECT health FROM og.hub_state WHERE hub_id = %s", (hub_id,))
            row = await cur.fetchone()
        assert row is not None
        assert row[0] == "offline"
    finally:
        await pool.close()


@requires_db
async def test_evaluate_alerts_raises_and_clears_process_down() -> None:
    assert _DSN is not None
    migrate_sync(_DSN)
    pool = AsyncConnectionPool(_DSN, min_size=1, max_size=4, open=False)
    await pool.open(wait=True)
    try:
        async with pool.connection() as conn:
            await conn.execute("DELETE FROM og.heartbeat")
            await conn.execute("DELETE FROM og.alert WHERE rule = 'ALR-PROCESS-DOWN'")
            await conn.commit()

        health.configure(pool, load_config(_CONFIG_PATH))
        await health.evaluate_alerts()

        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT count(*) FROM og.alert WHERE rule = 'ALR-PROCESS-DOWN' AND cleared_at IS NULL"
            )
            row = await cur.fetchone()
        assert row is not None
        # 5, not all 6 `ALL_PROCESSES` entries: `settle` (this evaluator's own host process, 02b S1.2)
        # is always "ok" regardless of its heartbeat row (defect fix -- see
        # `opengrid.health.rules.classify_all_processes`'s `self_process` docstring).
        assert row[0] == 5

        async with pool.connection() as conn:
            for process in ("feeds", "engine", "guardian", "safestop", "settle", "api"):
                await conn.execute(
                    "INSERT INTO og.heartbeat (process, pid, ts, status) VALUES (%s, 1, now(), 'ok') "
                    "ON CONFLICT (process) DO UPDATE SET ts = now()",
                    (process,),
                )
            await conn.commit()

        await health.evaluate_alerts()

        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT count(*) FROM og.alert WHERE rule = 'ALR-PROCESS-DOWN' AND cleared_at IS NULL"
            )
            row = await cur.fetchone()
        assert row is not None
        assert row[0] == 0
    finally:
        await pool.close()
