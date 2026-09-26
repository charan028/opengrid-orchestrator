"""Postgres persistence for `opengrid.site_ingest` (`og.customer_site_meter_reading`,
`og.corridor_current_reading`, `migrations/0026_customer_services.sql`). Kept apart from the package's
pure ingest logic (BUILD.md S5a).

One `executemany` per table per flush, in chunks, with asynchronous commit: these readings are soft
telemetry (the controllers act on the in-memory latest value, never on these rows), and the base
server's Postgres has checkpoint stalls, so a per-message synchronous commit is exactly what must not
happen. A duplicate (same source and ts) is ignored.
"""

from __future__ import annotations

from collections.abc import Sequence

from psycopg_pool import AsyncConnectionPool

from opengrid.site_ingest.models import CorridorCurrentReading, SiteMeterReading

_ASYNC_COMMIT_SQL = "SET LOCAL synchronous_commit TO OFF"
_BATCH_CHUNK_SIZE = 500

_SITE_COLUMNS = (
    "customer_id",
    "site_id",
    "ts",
    "p_kw",
    "q_kvar",
    "v_rms_a_v",
    "v_rms_b_v",
    "v_rms_c_v",
    "i_rms_a_a",
    "i_rms_b_a",
    "i_rms_c_a",
    "freq_hz",
    "pf",
    "thd_v_pct",
    "thd_i_pct",
    "quality",
)
_CORRIDOR_COLUMNS = ("customer_id", "corridor_id", "line_id", "ts", "i_ac_a", "limit_a", "quality")


def _insert_sql(table: str, columns: tuple[str, ...], conflict: str) -> str:
    placeholders = ", ".join(f"%({c})s" for c in columns)
    return (
        f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders}) "  # noqa: S608 -- fixed names
        f"ON CONFLICT ({conflict}) DO NOTHING"
    )


_INSERT_SITE_SQL = _insert_sql("og.customer_site_meter_reading", _SITE_COLUMNS, "customer_id, site_id, ts")
_INSERT_CORRIDOR_SQL = _insert_sql(
    "og.corridor_current_reading", _CORRIDOR_COLUMNS, "customer_id, corridor_id, ts"
)


class PgSiteIngestBackend:
    """`opengrid.site_ingest.SiteIngestBackend` over a `psycopg_pool.AsyncConnectionPool`."""

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def insert_batch(
        self, site_rows: Sequence[SiteMeterReading], corridor_rows: Sequence[CorridorCurrentReading]
    ) -> None:
        if not site_rows and not corridor_rows:
            return
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_ASYNC_COMMIT_SQL)
            for sql, rows in ((_INSERT_SITE_SQL, site_rows), (_INSERT_CORRIDOR_SQL, corridor_rows)):
                for start in range(0, len(rows), _BATCH_CHUNK_SIZE):
                    chunk = rows[start : start + _BATCH_CHUNK_SIZE]
                    await cur.executemany(sql, [row.model_dump() for row in chunk])
            await conn.commit()
