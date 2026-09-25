"""Postgres-backed `ForecastBackend` (02b S3, table `og.forecast`). Kept separate from `service.py` /
`quantiles.py` so those stay import-free of psycopg (BUILD.md S5a "pure logic separated from I/O"),
mirroring `opengrid.trace.pg_backend`'s split from `opengrid.trace.store`.
"""

from __future__ import annotations

from datetime import datetime

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from opengrid.forecast.models import ForecastRow

_UPSERT_SQL = """
INSERT INTO og.forecast
    (series_key, kind, interval_start_utc, horizon_step, p10, p50, p90, firm_fitness)
VALUES (%(series_key)s, %(kind)s, %(interval_start_utc)s, %(horizon_step)s, %(p10)s, %(p50)s,
        %(p90)s, %(firm_fitness)s)
ON CONFLICT (series_key, kind, interval_start_utc) DO UPDATE SET
    horizon_step = EXCLUDED.horizon_step,
    p10 = EXCLUDED.p10,
    p50 = EXCLUDED.p50,
    p90 = EXCLUDED.p90,
    firm_fitness = EXCLUDED.firm_fitness,
    computed_at = now()
"""

_FETCH_RANGE_SQL = """
SELECT series_key, kind, interval_start_utc, horizon_step, p10, p50, p90, firm_fitness, computed_at
FROM og.forecast
WHERE interval_start_utc >= %(horizon_start)s AND interval_start_utc < %(horizon_end)s
ORDER BY series_key, kind, interval_start_utc
"""


class PgForecastBackend:
    """`ForecastBackend` implementation over a shared `psycopg_pool.AsyncConnectionPool`
    (`opengrid.platform.db.make_pool`) -- one pool per process, injected at wiring time."""

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def upsert_rows(self, rows: list[ForecastRow]) -> None:
        if not rows:
            return
        async with self._pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            await cur.executemany(_UPSERT_SQL, [row.model_dump(exclude={"computed_at"}) for row in rows])

    async def fetch_range(self, horizon_start: datetime, horizon_end: datetime) -> list[ForecastRow]:
        async with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(_FETCH_RANGE_SQL, {"horizon_start": horizon_start, "horizon_end": horizon_end})
            rows = await cur.fetchall()
        return [ForecastRow.model_validate(row) for row in rows]
