"""Time-column BRIN indexes the retention/export scans need on existing high-volume tables, built with
CREATE INDEX CONCURRENTLY (never inside migration 0033's transaction, never blocking ingest). A leftover
INVALID index from an interrupted build is dropped concurrently and rebuilt. BRIN suits these append-only
tables: a few dozen kB per GB of heap, and `ts < cutoff` / one-day range scans touch only matching ranges.
"""

from __future__ import annotations

import logging

from psycopg import sql
from psycopg_pool import AsyncConnectionPool

logger = logging.getLogger(__name__)

#: (index name, table, column) -- all in schema og.
LIFECYCLE_INDEXES: tuple[tuple[str, str, str], ...] = (
    ("ix_feed_obs_ts_brin", "feed_obs", "ts"),
    ("ix_pq_waveform_summary_ts_brin", "pq_waveform_summary", "ts"),
    ("ix_pq_waveform_raw_index_ts_brin", "pq_waveform_raw_index", "ts"),
    ("ix_command_batch_created_brin", "command_batch", "created_at"),
    ("ix_grant_created_brin", "grant", "created_at"),
    ("ix_plan_energy_value_created_brin", "plan_energy_value", "created_at"),
)

_STATE_SQL = """
SELECT i.indisvalid FROM pg_index i
JOIN pg_class c ON c.oid = i.indexrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'og' AND c.relname = %s
"""


async def ensure_indexes(pool: AsyncConnectionPool) -> list[str]:
    """Build every missing index concurrently; returns the names built."""
    built: list[str] = []
    async with pool.connection() as conn:
        await conn.set_autocommit(True)
        try:
            for name, table, column in LIFECYCLE_INDEXES:
                cur = await conn.execute(_STATE_SQL, (name,))
                row = await cur.fetchone()
                if row is not None and row[0]:
                    continue
                if row is not None:
                    logger.warning("lifecycle: rebuilding invalid index", extra={"index": name})
                    await conn.execute(
                        sql.SQL("DROP INDEX CONCURRENTLY IF EXISTS og.{}").format(sql.Identifier(name))
                    )
                await conn.execute(
                    sql.SQL("CREATE INDEX CONCURRENTLY IF NOT EXISTS {} ON og.{} USING brin ({})").format(
                        sql.Identifier(name), sql.Identifier(table), sql.Identifier(column)
                    )
                )
                built.append(name)
        finally:
            await conn.set_autocommit(False)
    return built
