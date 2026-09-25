"""Heartbeat writer used by every process (02b S6.4). Upserts one row into `og.heartbeat` every
`health.heartbeat_interval_s` seconds; `health` (running inside og-settle) reads all 7 rows and marks a
process DOWN after `health.heartbeat_miss_threshold` missed intervals.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

from psycopg_pool import AsyncConnectionPool

_UPSERT_SQL = """
INSERT INTO og.heartbeat (process, pid, ts, status)
VALUES (%(process)s, %(pid)s, %(ts)s, %(status)s)
ON CONFLICT (process) DO UPDATE SET pid = EXCLUDED.pid, ts = EXCLUDED.ts, status = EXCLUDED.status
"""


async def write_heartbeat(pool: AsyncConnectionPool, process: str, *, status: str = "ok") -> None:
    """Write one heartbeat row for `process`. Never raises on a transient DB error -- a missed
    heartbeat is exactly what `health`'s miss-threshold logic is designed to detect and alert on;
    the caller's control loop must not crash because the DB hiccuped (K7: degrade, don't trip)."""
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            _UPSERT_SQL,
            {"process": process, "pid": os.getpid(), "ts": datetime.now(UTC), "status": status},
        )
