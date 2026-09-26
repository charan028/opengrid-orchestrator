"""Retention executor over `og.data_retention`: hot->cold pre-export, then deletion past keep_days.

PARTITION mode: every partition whose upper bound is at or before the cutoff is detached (brief lock),
its per-day row counts are compared with the registered exports (re-exported on a mismatch), every export
is re-verified from disk, and only then is the table dropped. Any failure reattaches the partition and
stops that table (the data is never dropped unarchived).

DELETE mode: day by day from the oldest deletable row, export + verify, then DELETE in batches of at most
`batch_rows` rows, one short autocommitted transaction per batch.

`legal_hold` or `protected` stops all deletion for a table; tables outside the whitelist in
`opengrid.lifecycle.policy.DELETABLE_TABLES` are never touched.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from psycopg import sql
from psycopg_pool import AsyncConnectionPool

from opengrid.lifecycle import partitions as parts
from opengrid.lifecycle.archive import ArchiveError, ensure_day_archived, export_range, registry_days
from opengrid.lifecycle.policy import (
    DELETABLE_TABLES,
    PARTITIONED_TABLES,
    LifecycleConfig,
    RetentionRow,
    TableSpec,
    can_delete,
    day_start,
    days_between,
    drop_cutoff,
    last_exportable_day,
    utc_today,
)

logger = logging.getLogger(__name__)

_ONE_DAY = timedelta(days=1)

_POLICY_SQL = """
SELECT table_name, ts_column, mode, hot_days, keep_days, archive, legal_hold, protected
FROM og.data_retention ORDER BY table_name
"""


@dataclass(slots=True)
class TableOutcome:
    table: str
    exported_days: int = 0
    dropped_partitions: list[str] = field(default_factory=list)
    deleted_rows: int = 0
    blobs_deleted: int = 0
    skipped: str | None = None
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"table": self.table}
        for key in ("exported_days", "deleted_rows", "blobs_deleted"):
            if getattr(self, key):
                out[key] = getattr(self, key)
        if self.dropped_partitions:
            out["dropped_partitions"] = self.dropped_partitions
        if self.skipped:
            out["skipped"] = self.skipped
        if self.error:
            out["error"] = self.error
        return out


async def load_policies(pool: AsyncConnectionPool) -> list[RetentionRow]:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(_POLICY_SQL)
        rows = await cur.fetchall()
    return [RetentionRow(r[0], r[1], r[2], r[3], r[4], bool(r[5]), bool(r[6]), bool(r[7])) for r in rows]


async def run_retention(
    pool: AsyncConnectionPool, cfg: LifecycleConfig, now: datetime, *, only: set[str] | None = None
) -> list[TableOutcome]:
    """One pass over every policy row (or just the tables in `only`). Never raises for a single table's
    failure: it is recorded in the outcome (and the run is reported not-ok by the runner)."""
    outcomes: list[TableOutcome] = []
    for row in await load_policies(pool):
        if only is not None and row.table_name not in only:
            continue
        spec = DELETABLE_TABLES.get(row.table_name)
        outcome = TableOutcome(row.table_name)
        outcomes.append(outcome)
        if spec is None or row.mode == "NONE":
            outcome.skipped = "protected" if row.protected else "not managed"
            continue
        try:
            if row.mode == "PARTITION":
                await _partition_table(pool, cfg, row, spec, now, outcome)
            else:
                await _delete_table(pool, cfg, row, spec, now, outcome)
        except (ArchiveError, parts.PartitionError) as exc:
            outcome.error = str(exc)
            logger.error(
                "lifecycle retention failed; nothing dropped",
                extra={"table": row.table_name, "error": str(exc)},
            )
    return outcomes


# ---------------------------------------------------------------------------------------------------
# PARTITION mode
# ---------------------------------------------------------------------------------------------------


async def _partition_table(
    pool: AsyncConnectionPool,
    cfg: LifecycleConfig,
    row: RetentionRow,
    spec: TableSpec,
    now: datetime,
    outcome: TableOutcome,
) -> None:
    infos = [p for p in await parts.list_partitions(pool, row.table_name) if not p.is_default]
    arch = spec.archives[0] if spec.archives else None

    # Hot -> cold pre-export of closed days (spreads the export work ahead of the drop).
    newest = last_exportable_day(now, row.hot_days)
    if row.archive and arch is not None and newest is not None:
        done = await registry_days(pool, arch.name)
        for info in infos:
            first = await _first_day(pool, row.table_name, info)
            if first is None or info.upper is None:
                continue
            last = min(utc_today(info.upper - _ONE_DAY), newest)
            for d in days_between(first, last):
                if d not in done and outcome.exported_days < cfg.max_preexport_days_per_run:
                    lo = day_start(d)
                    await export_range(pool, cfg, arch, day=d, lo=lo, hi=lo + _ONE_DAY)
                    outcome.exported_days += 1

    if not can_delete(row):
        outcome.skipped = "legal_hold" if row.legal_hold else "kept (no keep_days)"
        return
    keep_days = row.keep_days if row.keep_days is not None else 0
    cutoff = drop_cutoff(now, keep_days)
    for info in infos:
        if info.upper is None or info.upper > cutoff:
            continue
        await parts.detach(pool, cfg, info)
        try:
            if row.archive and arch is not None:
                for day_ts, count in (await parts.rows_per_day(pool, info)).items():
                    archived = await ensure_day_archived(
                        pool, cfg, arch, utc_today(day_ts), expected_rows=count, source=info.name
                    )
                    if archived is not None:
                        outcome.exported_days += 0 if archived.revision == 1 else 1
        except ArchiveError:
            await parts.reattach(pool, info)
            raise
        await parts.drop_detached(pool, info)
        outcome.dropped_partitions.append(info.name)
        logger.info(
            "lifecycle partition dropped", extra={"partition": info.name, "upper": info.upper.isoformat()}
        )


async def _first_day(pool: AsyncConnectionPool, parent: str, info: parts.PartitionInfo) -> date | None:
    if info.lower is not None:
        return utc_today(info.lower)
    col = PARTITIONED_TABLES[parent]
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            sql.SQL("SELECT min({}) FROM og.{}").format(sql.Identifier(col), sql.Identifier(info.name))
        )
        res = await cur.fetchone()
    return utc_today(res[0]) if res and res[0] is not None else None


# ---------------------------------------------------------------------------------------------------
# DELETE mode
# ---------------------------------------------------------------------------------------------------


def _where(spec: TableSpec) -> sql.Composable:
    base = sql.SQL("t.{c} >= %(lo)s AND t.{c} < %(hi)s").format(c=sql.Identifier(spec.ts_column))
    if spec.delete_guard:
        return sql.SQL("{} AND {}").format(base, sql.SQL(spec.delete_guard))
    return base


async def _oldest(pool: AsyncConnectionPool, spec: TableSpec, before: datetime | None) -> datetime | None:
    """Oldest deletable row (guard applied), optionally before `before` (BRIN narrows the scan)."""
    guard = sql.SQL(" AND {}").format(sql.SQL(spec.delete_guard)) if spec.delete_guard else sql.SQL("")
    cond = (
        sql.SQL("WHERE t.{c} < %(before)s").format(c=sql.Identifier(spec.ts_column))
        if before
        else sql.SQL("WHERE true")
    )
    stmt = sql.SQL("SELECT min(t.{c}) FROM og.{t} t {cond}{guard}").format(
        c=sql.Identifier(spec.ts_column), t=sql.Identifier(spec.table), cond=cond, guard=guard
    )
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(stmt, {"before": before})
        res = await cur.fetchone()
    return res[0] if res else None


async def _count(pool: AsyncConnectionPool, ts_sql: str, lo: datetime, hi: datetime) -> int:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(ts_sql, {"lo": lo, "hi": hi})
        res = await cur.fetchone()
    return int(res[0]) if res else 0


async def _delete_table(
    pool: AsyncConnectionPool,
    cfg: LifecycleConfig,
    row: RetentionRow,
    spec: TableSpec,
    now: datetime,
    outcome: TableOutcome,
) -> None:
    # Hot -> cold pre-export (kept-forever tables such as telemetry_15m get their cold copy this way).
    newest = last_exportable_day(now, row.hot_days)
    if row.archive and spec.archives and newest is not None:
        oldest_any = await _oldest(pool, spec, day_start(newest) + _ONE_DAY)
        if oldest_any is not None:
            for arch in spec.archives:
                done = await registry_days(pool, arch.name)
                for d in days_between(utc_today(oldest_any), newest):
                    if d in done:
                        continue
                    lo = day_start(d)
                    if outcome.exported_days >= cfg.max_preexport_days_per_run:
                        break
                    if await _count(pool, arch.ts_sql, lo, lo + _ONE_DAY):
                        await export_range(pool, cfg, arch, day=d, lo=lo, hi=lo + _ONE_DAY)
                        outcome.exported_days += 1

    if not can_delete(row):
        outcome.skipped = "legal_hold" if row.legal_hold else "kept (no keep_days)"
        return
    keep_days = row.keep_days if row.keep_days is not None else 0
    cutoff = drop_cutoff(now, keep_days)
    oldest = await _oldest(pool, spec, cutoff)
    if oldest is None:
        return
    for d in days_between(utc_today(oldest), utc_today(cutoff - _ONE_DAY)):
        lo = day_start(d)
        hi = lo + _ONE_DAY
        if row.archive:
            for arch in spec.archives:
                count = await _count(pool, arch.ts_sql, lo, hi)
                await ensure_day_archived(pool, cfg, arch, d, expected_rows=count)
        deleted, blobs = await _delete_day(pool, cfg, spec, lo, hi)
        outcome.deleted_rows += deleted
        outcome.blobs_deleted += blobs


async def _delete_day(
    pool: AsyncConnectionPool, cfg: LifecycleConfig, spec: TableSpec, lo: datetime, hi: datetime
) -> tuple[int, int]:
    params = {"lo": lo, "hi": hi, "n": cfg.batch_rows}
    table = sql.Identifier(spec.table)
    if spec.child_verdicts:
        select_ids = sql.SQL("SELECT t.command_batch_id FROM og.{t} t WHERE {w} LIMIT %(n)s").format(
            t=table, w=_where(spec)
        )
    else:
        returning = (
            sql.SQL(" RETURNING {}").format(sql.Identifier(spec.blob_ref_column))
            if spec.blob_ref_column
            else sql.SQL("")
        )
        delete_stmt = sql.SQL(
            "DELETE FROM og.{t} WHERE ctid = ANY(ARRAY(SELECT t.ctid FROM og.{t} t WHERE {w} LIMIT %(n)s)){r}"
        ).format(t=table, w=_where(spec), r=returning)
    total = blobs = 0
    for _batch in range(cfg.max_delete_batches_per_table):
        async with pool.connection() as conn, conn.cursor() as cur:
            if spec.child_verdicts:
                await cur.execute(select_ids, params)
                ids = [r[0] for r in await cur.fetchall()]
                if ids:
                    await cur.execute("DELETE FROM og.verdict WHERE command_batch_id = ANY(%s)", (ids,))
                    await cur.execute("DELETE FROM og.command_batch WHERE command_batch_id = ANY(%s)", (ids,))
                n = len(ids)
            else:
                await cur.execute(delete_stmt, params)
                n = cur.rowcount
                if spec.blob_ref_column and n > 0:
                    blobs += _unlink_blobs(cfg.pq_blob_dir, [r[0] for r in await cur.fetchall()])
        total += max(n, 0)
        if n < cfg.batch_rows:
            break
    return total, blobs


def _unlink_blobs(root: Path | None, refs: list[str]) -> int:
    """Delete raw PQ capture files referenced by deleted index rows (only inside the blob root)."""
    if root is None:
        return 0
    base = root.resolve()
    removed = 0
    for ref in refs:
        path = (base / ref).resolve()
        if not path.is_relative_to(base):
            logger.warning("lifecycle: blob ref outside blob root ignored", extra={"ref": ref})
            continue
        if path.exists():
            path.unlink()
            removed += 1
            parent = path.parent
            while parent != base and parent.is_relative_to(base) and not any(parent.iterdir()):
                parent.rmdir()
                parent = parent.parent
    return removed
