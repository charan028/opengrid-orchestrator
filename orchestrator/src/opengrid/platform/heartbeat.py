"""Heartbeat writer used by every process (02b S6.4). Upserts one row into `og.heartbeat` every
`health.heartbeat_interval_s` seconds; `health` (running inside og-settle) reads all 7 rows and marks a
process DOWN after `health.heartbeat_miss_threshold` missed intervals.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import UTC, datetime

from psycopg_pool import AsyncConnectionPool

logger = logging.getLogger(__name__)

_UPSERT_SQL = """
INSERT INTO og.heartbeat (process, pid, ts, status)
VALUES (%(process)s, %(pid)s, %(ts)s, %(status)s)
ON CONFLICT (process) DO UPDATE SET pid = EXCLUDED.pid, ts = EXCLUDED.ts, status = EXCLUDED.status
"""

HEARTBEAT_WRITE_TIMEOUT_S = 5.0  # PLAT-004: a stuck connection must not stall the caller's cycle forever


async def write_heartbeat(pool: AsyncConnectionPool, process: str, *, status: str = "ok") -> None:
    """Write one heartbeat row for `process`. Never raises on a transient DB error or a timeout -- a
    missed heartbeat is exactly what `health`'s miss-threshold logic is designed to detect and alert on;
    the caller's control loop must not crash, or hang, because the DB hiccuped (K7: degrade, don't trip).
    """
    try:

        async def _write() -> None:
            async with pool.connection() as conn, conn.cursor() as cur:
                await cur.execute(
                    _UPSERT_SQL,
                    {"process": process, "pid": os.getpid(), "ts": datetime.now(UTC), "status": status},
                )

        await asyncio.wait_for(_write(), timeout=HEARTBEAT_WRITE_TIMEOUT_S)
    except Exception:
        # "process" collides with LogRecord's own reserved `process` attribute (the OS PID) -- use
        # "proc_name" so this actually gets logged instead of raising KeyError from makeRecord.
        logger.exception("failed to write heartbeat", extra={"proc_name": process})
