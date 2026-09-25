"""Postgres access for `og.feed_obs` / `og.feed_status` (02b S2.7). Owned by `feeds` -- the only writer
and reader of these two tables (BUILD.md S1 "no duplicated functions").

Idempotent upsert on the natural key `(source, product, series, ts)`; a re-posting overwrites in place
(no `correction_version` in MVP-S, per 02b S2.7).
"""

from __future__ import annotations

from datetime import UTC, datetime

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from opengrid.core.models.platform import FeedObs
from opengrid.feeds.staleness import effective_quality

_UPSERT_OBS_SQL = """
INSERT INTO og.feed_obs (source, product, series, ts, value, unit, quality, recorded_at)
VALUES (%(source)s, %(product)s, %(series)s, %(ts)s, %(value)s, %(unit)s, %(quality)s, %(recorded_at)s)
ON CONFLICT (source, product, series, ts) DO UPDATE SET
    value = EXCLUDED.value,
    unit = EXCLUDED.unit,
    quality = EXCLUDED.quality,
    recorded_at = EXCLUDED.recorded_at
"""

_LATEST_SQL = """
SELECT source, product, series, ts, value, unit, quality, recorded_at
FROM og.feed_obs
WHERE series = %(series)s
ORDER BY ts DESC
LIMIT 1
"""

_WINDOW_SQL = """
SELECT source, product, series, ts, value, unit, quality, recorded_at
FROM og.feed_obs
WHERE series = %(series)s AND ts >= %(t0)s AND ts < %(t1)s
ORDER BY ts ASC
"""

_UPSERT_STATUS_SQL = """
INSERT INTO og.feed_status
    (source, product, last_value_at, last_success_at, consecutive_failures, breaker_open, active_key)
VALUES
    (%(source)s, %(product)s, %(last_value_at)s, %(last_success_at)s, %(consecutive_failures)s,
     %(breaker_open)s, %(active_key)s)
ON CONFLICT (source, product) DO UPDATE SET
    last_value_at = COALESCE(EXCLUDED.last_value_at, og.feed_status.last_value_at),
    last_success_at = COALESCE(EXCLUDED.last_success_at, og.feed_status.last_success_at),
    consecutive_failures = EXCLUDED.consecutive_failures,
    breaker_open = EXCLUDED.breaker_open,
    active_key = COALESCE(EXCLUDED.active_key, og.feed_status.active_key)
"""


class FeedStore:
    """Thin async wrapper over the connection pool; every method is a single parameterized statement
    (BUILD.md S5a "parameterised SQL only")."""

    def __init__(self, pool: AsyncConnectionPool, *, staleness_cfg: dict[str, float] | None = None) -> None:
        self._pool = pool
        self._staleness_cfg = staleness_cfg or {}

    def _with_effective_quality(self, row: FeedObs, *, now: datetime | None = None) -> FeedObs:
        now = now or datetime.now(UTC)
        age_s = (now - row.ts).total_seconds()
        quality = effective_quality(
            row.quality,
            source=row.source,
            product=row.product,
            age_s=age_s,
            staleness_cfg=self._staleness_cfg,
        )
        return row if quality == row.quality else row.model_copy(update={"quality": quality})

    async def upsert_obs(self, rows: list[FeedObs]) -> None:
        if not rows:
            return
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.executemany(
                _UPSERT_OBS_SQL,
                [
                    {
                        "source": r.source,
                        "product": r.product,
                        "series": r.series,
                        "ts": r.ts,
                        "value": r.value,
                        "unit": r.unit,
                        "quality": r.quality,
                        "recorded_at": r.recorded_at,
                    }
                    for r in rows
                ],
            )

    async def update_status(
        self,
        *,
        source: str,
        product: str,
        last_value_at: datetime | None = None,
        last_success_at: datetime | None = None,
        consecutive_failures: int,
        breaker_open: bool,
        active_key: str | None = None,
    ) -> None:
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                _UPSERT_STATUS_SQL,
                {
                    "source": source,
                    "product": product,
                    "last_value_at": last_value_at,
                    "last_success_at": last_success_at,
                    "consecutive_failures": consecutive_failures,
                    "breaker_open": breaker_open,
                    "active_key": active_key,
                },
            )

    async def latest(self, series: str) -> FeedObs:
        async with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(_LATEST_SQL, {"series": series})
            row = await cur.fetchone()
        if row is None:
            raise LookupError(f"no observation ever recorded for series {series!r}")
        return self._with_effective_quality(FeedObs.model_validate(row))

    async def window(self, series: str, t0: datetime, t1: datetime) -> list[FeedObs]:
        async with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(_WINDOW_SQL, {"series": series, "t0": t0, "t1": t1})
            rows = await cur.fetchall()
        now = datetime.now(UTC)
        return [self._with_effective_quality(FeedObs.model_validate(row), now=now) for row in rows]
