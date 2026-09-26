"""Daily range partitions for og.telemetry / og.telemetry_1m: create ahead, swap a populated default
partition out without moving rows or blocking ingest, detach/reattach/drop for retention.

The default swap (first run after migration 0033 on a database whose rows all sit in
`<parent>_default`) never rewrites data:

1. `ADD CONSTRAINT lifecycle_swap_range CHECK (col >= L AND col < T) NOT VALID` on the default -- a brief
   lock, no scan;
2. `VALIDATE CONSTRAINT` -- scans the default under SHARE UPDATE EXCLUSIVE, which does not block inserts;
3. one short transaction (lock_timeout): DETACH the default, RENAME it `<parent>_pre<yyyymmdd>`, ATTACH it
   back as the range partition [L, T) -- the validated constraint lets Postgres skip the scan -- and create
   a new, empty default.

All rows stay queryable through the parent the whole time; rows up to T keep landing in the legacy range
partition, rows from T on land in the daily partitions `og.lifecycle_ensure_daily_partitions()` then
creates. Retention drops the legacy range partition like any other once T is past `keep_days`.
T = the lower bound of the first existing daily partition above L, else the next UTC midnight (the one
after, when midnight is less than 15 minutes away, so no in-flight row can fail the CHECK).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

import psycopg
from psycopg import sql
from psycopg_pool import AsyncConnectionPool

from opengrid.lifecycle.policy import PARTITIONED_TABLES, LifecycleConfig, day_start, utc_today

logger = logging.getLogger(__name__)

SWAP_CONSTRAINT = "lifecycle_swap_range"
_MIDNIGHT_MARGIN = timedelta(minutes=15)
_DETACH_ATTEMPTS = 3


class PartitionError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class PartitionInfo:
    parent: str
    name: str
    is_default: bool
    lower: datetime | None
    upper: datetime | None
    total_bytes: int
    est_rows: int


_LIST_SQL = """
SELECT parent, partition, is_default, lower_ts, upper_ts, total_bytes, est_rows
FROM og.lifecycle_partitions WHERE parent = %(parent)s
ORDER BY is_default, upper_ts NULLS LAST, partition
"""


def _column(parent: str) -> str:
    try:
        return PARTITIONED_TABLES[parent]
    except KeyError as exc:
        raise PartitionError(f"og.{parent} is not a lifecycle-partitioned table") from exc


async def list_partitions(pool: AsyncConnectionPool, parent: str) -> list[PartitionInfo]:
    _column(parent)
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(_LIST_SQL, {"parent": parent})
        rows = await cur.fetchall()
    return [PartitionInfo(r[0], r[1], bool(r[2]), r[3], r[4], int(r[5]), int(r[6])) for r in rows]


def swap_bounds(partitions: list[PartitionInfo], now: datetime) -> tuple[datetime | None, datetime]:
    """(L, T) for the legacy range partition; L None = MINVALUE. Pure (see the module docstring)."""
    ranged = [p for p in partitions if not p.is_default]
    closed_uppers = [p.upper for p in ranged if p.upper is not None and p.upper <= now]
    lower = max(closed_uppers) if closed_uppers else None
    candidates = [p.lower for p in ranged if p.lower is not None and (lower is None or p.lower >= lower)]
    if candidates:
        return lower, min(candidates)
    upper = day_start(utc_today(now)) + timedelta(days=1)
    if upper - now < _MIDNIGHT_MARGIN:
        upper += timedelta(days=1)
    return lower, upper


async def default_has_rows(pool: AsyncConnectionPool, parent: str) -> bool:
    _column(parent)
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            sql.SQL("SELECT EXISTS (SELECT 1 FROM og.{})").format(sql.Identifier(f"{parent}_default"))
        )
        row = await cur.fetchone()
    return bool(row and row[0])


async def _free_name(conn: psycopg.AsyncConnection[tuple[object, ...]], base: str) -> str:
    name, n = base, 1
    while True:
        cur = await conn.execute("SELECT to_regclass(%s)", (f"og.{name}",))
        row = await cur.fetchone()
        if row is None or row[0] is None:
            return name
        n += 1
        name = f"{base}_{n}"


def _bound_sql(lower: datetime | None, upper: datetime) -> sql.Composed:
    lo = sql.SQL("MINVALUE") if lower is None else sql.Literal(lower)
    return sql.SQL("FROM ({}) TO ({})").format(lo, sql.Literal(upper))


async def swap_default(pool: AsyncConnectionPool, cfg: LifecycleConfig, parent: str, now: datetime) -> str:
    """Turn a populated `<parent>_default` into the legacy range partition and attach a new empty
    default. Returns the legacy partition's name. On any failure the temporary CHECK is removed and
    PartitionError is raised (the table is left exactly as before)."""
    col = _column(parent)
    default = f"{parent}_default"
    lower, upper = swap_bounds(await list_partitions(pool, parent), now)
    check = sql.SQL("{} < {}").format(sql.Identifier(col), sql.Literal(upper))
    if lower is not None:
        check = sql.SQL("{} >= {} AND {}").format(sql.Identifier(col), sql.Literal(lower), check)
    lock_timeout = sql.SQL("SET lock_timeout = {}").format(sql.Literal(f"{cfg.lock_timeout_s}s"))
    drop_check = sql.SQL("ALTER TABLE og.{} DROP CONSTRAINT IF EXISTS {}").format(
        sql.Identifier(default), sql.Identifier(SWAP_CONSTRAINT)
    )

    async with pool.connection() as conn:
        await conn.set_autocommit(True)
        try:
            await conn.execute(lock_timeout)
            await conn.execute(drop_check)
            await conn.execute(
                sql.SQL("ALTER TABLE og.{} ADD CONSTRAINT {} CHECK ({}) NOT VALID").format(
                    sql.Identifier(default), sql.Identifier(SWAP_CONSTRAINT), check
                )
            )
            try:
                await conn.execute(
                    sql.SQL("ALTER TABLE og.{} VALIDATE CONSTRAINT {}").format(
                        sql.Identifier(default), sql.Identifier(SWAP_CONSTRAINT)
                    )
                )
                legacy = await _free_name(conn, f"{parent}_pre{upper:%Y%m%d}")
                async with conn.transaction():
                    await conn.execute(
                        sql.SQL("SET LOCAL lock_timeout = {}").format(sql.Literal(f"{cfg.lock_timeout_s}s"))
                    )
                    await conn.execute(
                        sql.SQL("ALTER TABLE og.{} DETACH PARTITION og.{}").format(
                            sql.Identifier(parent), sql.Identifier(default)
                        )
                    )
                    await conn.execute(
                        sql.SQL("ALTER TABLE og.{} RENAME TO {}").format(
                            sql.Identifier(default), sql.Identifier(legacy)
                        )
                    )
                    await conn.execute(
                        sql.SQL("ALTER TABLE og.{} ATTACH PARTITION og.{} FOR VALUES {}").format(
                            sql.Identifier(parent), sql.Identifier(legacy), _bound_sql(lower, upper)
                        )
                    )
                    await conn.execute(
                        sql.SQL("CREATE TABLE og.{} PARTITION OF og.{} DEFAULT").format(
                            sql.Identifier(default), sql.Identifier(parent)
                        )
                    )
            except psycopg.Error as exc:
                await conn.execute(drop_check)
                raise PartitionError(f"default swap of og.{parent} failed: {exc}") from exc
        finally:
            await conn.execute("RESET lock_timeout")
            await conn.set_autocommit(False)
    logger.info(
        "lifecycle default partition swapped",
        extra={"parent": parent, "legacy": legacy, "lower": str(lower), "upper": upper.isoformat()},
    )
    return legacy


async def ensure_partitions(
    pool: AsyncConnectionPool,
    cfg: LifecycleConfig,
    parent: str,
    now: datetime,
    *,
    days_back: int | None = None,
) -> dict[str, object]:
    """Create today - days_back .. today + days_ahead; swap a populated default out first."""
    _column(parent)
    back = cfg.days_back if days_back is None else days_back
    result: dict[str, object] = {"swapped": None}
    created = await _call_ensure(pool, parent, back, cfg.days_ahead)
    if created < 0:
        result["swapped"] = await swap_default(pool, cfg, parent, now)
        created = await _call_ensure(pool, parent, back, cfg.days_ahead)
    result["created"] = max(created, 0)
    return result


async def _call_ensure(pool: AsyncConnectionPool, parent: str, back: int, ahead: int) -> int:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute("SELECT og.lifecycle_ensure_daily_partitions(%s, %s, %s)", (parent, back, ahead))
        row = await cur.fetchone()
    return int(row[0]) if row else 0


async def detach(pool: AsyncConnectionPool, cfg: LifecycleConfig, info: PartitionInfo) -> None:
    """Plain DETACH (CONCURRENTLY is not allowed while a default partition exists): a brief ACCESS
    EXCLUSIVE lock on the parent, bounded by lock_timeout and retried."""
    stmt = sql.SQL("ALTER TABLE og.{} DETACH PARTITION og.{}").format(
        sql.Identifier(info.parent), sql.Identifier(info.name)
    )
    last: Exception | None = None
    for _attempt in range(_DETACH_ATTEMPTS):
        try:
            async with pool.connection() as conn, conn.transaction():
                await conn.execute(
                    sql.SQL("SET LOCAL lock_timeout = {}").format(sql.Literal(f"{cfg.lock_timeout_s}s"))
                )
                await conn.execute(stmt)
            return
        except psycopg.errors.LockNotAvailable as exc:
            last = exc
    raise PartitionError(f"could not detach og.{info.name}: {last}")


async def reattach(pool: AsyncConnectionPool, info: PartitionInfo) -> None:
    """Undo a detach after a failed export (the attach validates the range under SHARE UPDATE
    EXCLUSIVE, which does not block ingest)."""
    if info.upper is None:
        raise PartitionError(f"og.{info.name} has no upper bound; cannot reattach")
    async with pool.connection() as conn:
        await conn.execute(
            sql.SQL("ALTER TABLE og.{} ATTACH PARTITION og.{} FOR VALUES {}").format(
                sql.Identifier(info.parent), sql.Identifier(info.name), _bound_sql(info.lower, info.upper)
            )
        )


async def drop_detached(pool: AsyncConnectionPool, info: PartitionInfo) -> None:
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT 1 FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'og' AND c.relname = %s",
            (info.name,),
        )
        if await cur.fetchone() is not None:
            raise PartitionError(f"og.{info.name} is still attached; refusing to drop")
        await conn.execute(sql.SQL("DROP TABLE og.{}").format(sql.Identifier(info.name)))


async def rows_per_day(pool: AsyncConnectionPool, info: PartitionInfo) -> dict[datetime, int]:
    """{UTC day start: rows} of one (detached) partition -- a single scan."""
    col = _column(info.parent)
    stmt = sql.SQL(
        "SELECT date_trunc('day', {c}, 'UTC') AS d, count(*) FROM og.{t} GROUP BY 1 ORDER BY 1"
    ).format(c=sql.Identifier(col), t=sql.Identifier(info.name))
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(stmt)
        rows = await cur.fetchall()
    return {r[0]: int(r[1]) for r in rows}
