"""Postgres `TraceBackend` (02a S8) -- the only module that issues SQL against `og.trace` /
`og.trace_checkpoint` / `og.retention_policy`. Implements the `opengrid.trace.store.TraceBackend`
protocol as-is (no signature changes -- BUILD.md S4/S5a); `opengrid.trace.store.TraceStore` owns all
hash-chain logic and never appears here.

Concurrency (K11 "no fork, no skipped link"): `TraceStore.append()` calls `last_head()` then
`insert_trace_row()` as two independent awaits, so this backend cannot hold one transaction across
both without changing the protocol. Safety therefore comes from the database itself:
`ux_trace_stream_seq` / `ux_trace_stream_prev` (02a S1.14) make a losing concurrent writer fail with a
`UniqueViolation`, which `insert_trace_row` translates to `TraceAppendConflictError` -- a write-time error,
never a silently corrupted chain (matches `TraceStore.append`'s docstring exactly). Callers may retry
the whole `append()` call.

Retention pruning (K11, 02a S8.3): `prune_before` (called by `TraceStore.prune()`'s generic seq-window
backstop) and `run_retention_prune_job` (the per-event-class daily/on-demand job, BUILD.md S4 "health"
row) both refuse to delete any row that is not covered by at least one `trace_checkpoint` whose
`stream_heads` entry for that stream has `seq >= row.seq` -- so `verify()` from the nearest surviving
checkpoint forward always still passes (02a S8.3).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from opengrid.core.tracehash import ChainRecord

logger = logging.getLogger(__name__)

DEFAULT_RETENTION_DAYS = 400  # 02b S1.4: unknown/unconfigured event classes default here.


class TraceAppendConflictError(RuntimeError):
    """A concurrent append already claimed this stream's next seq or prev_hash (UNIQUE violation on
    `ux_trace_stream_seq` / `ux_trace_stream_prev`). K11: a fork or skipped link is refused at write
    time, never silently accepted. The caller may retry the whole `TraceStore.append()` call."""

    def __init__(self, stream_id: str, seq: int, cause: Exception) -> None:
        super().__init__(f"concurrent append conflict on stream {stream_id!r} at seq {seq}: {cause}")
        self.stream_id = stream_id
        self.seq = seq


_LAST_HEAD_SQL = """
SELECT seq, hash FROM og.trace WHERE stream_id = %(stream_id)s ORDER BY seq DESC LIMIT 1
"""

_INSERT_TRACE_SQL = """
INSERT INTO og.trace (
    trace_id, decision_type, event_class, stream_id, seq, payload, reason_codes,
    prev_hash, hash, created_at
) VALUES (
    %(trace_id)s, %(decision_type)s, %(event_class)s, %(stream_id)s, %(seq)s, %(payload)s,
    %(reason_codes)s, %(prev_hash)s, %(record_hash)s, %(created_at)s
)
"""

_EXISTS_PREIMAGE_SQL = "SELECT EXISTS(SELECT 1 FROM og.trace WHERE trace_id = %(trace_id)s)"

_FETCH_RANGE_SQL = """
SELECT seq, decision_type, event_class, payload, prev_hash, hash
FROM og.trace WHERE stream_id = %(stream_id)s AND seq >= %(from_seq)s
ORDER BY seq
"""

_STREAM_IDS_SQL = "SELECT DISTINCT stream_id FROM og.trace"

_INSERT_CHECKPOINT_SQL = """
INSERT INTO og.trace_checkpoint (checkpoint_id, checkpoint_at, stream_heads, checkpoint_hash)
VALUES (%(checkpoint_id)s, %(checkpoint_at)s, %(stream_heads)s, %(checkpoint_hash)s)
"""

_RETENTION_DAYS_SQL = "SELECT retention_days FROM og.retention_policy WHERE event_class = %(event_class)s"

# Covered = the highest seq per stream that any checkpoint's stream_heads has ever recorded. A row is
# safe to delete only if it is at or below its stream's covered seq (02a S8.3).
_COVERED_MAX_SEQ_CTE = """
WITH covered AS (
    SELECT kv.key AS stream_id, MAX((kv.value ->> 'seq')::bigint) AS max_seq
    FROM og.trace_checkpoint, LATERAL jsonb_each(stream_heads) AS kv
    GROUP BY kv.key
)
"""

_PRUNE_BEFORE_SQL = (
    _COVERED_MAX_SEQ_CTE
    + """
DELETE FROM og.trace t
USING covered c
WHERE t.stream_id = %(stream_id)s
  AND c.stream_id = t.stream_id
  AND t.seq < %(keep_from_seq)s
  AND t.seq <= c.max_seq
"""
)

_PRUNE_BY_CLASS_SQL = (
    _COVERED_MAX_SEQ_CTE
    + """
DELETE FROM og.trace t
USING covered c
WHERE t.event_class = %(event_class)s
  AND t.created_at < %(cutoff)s
  AND c.stream_id = t.stream_id
  AND t.seq <= c.max_seq
"""
)

_RETENTION_POLICIES_SQL = """
SELECT event_class, retention_days FROM og.retention_policy WHERE prune_after_checkpoint
"""


def _row_to_chain_record(row: tuple[int, str, str, dict[str, Any], str | None, str]) -> ChainRecord:
    seq, decision_type, event_class, payload, prev_hash, record_hash = row
    return ChainRecord(seq, decision_type, event_class, payload, prev_hash, record_hash)


class PgTraceBackend:
    """`TraceBackend` implementation backed by `opengrid.platform.db`'s async connection pool."""

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool
        # Best-effort in-process serialization for same-stream concurrent appends (reduces, but does
        # not replace, the DB unique-constraint safety net above -- see module docstring.
        self._stream_locks: dict[str, asyncio.Lock] = {}

    def _lock_for(self, stream_id: str) -> asyncio.Lock:
        lock = self._stream_locks.get(stream_id)
        if lock is None:
            lock = asyncio.Lock()
            self._stream_locks[stream_id] = lock
        return lock

    async def last_head(self, stream_id: str) -> tuple[int, str | None]:
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_LAST_HEAD_SQL, {"stream_id": stream_id})
            row = await cur.fetchone()
        if row is None:
            return -1, None
        seq, head_hash = row
        return seq, head_hash

    async def insert_trace_row(
        self,
        *,
        trace_id: UUID,
        stream_id: str,
        seq: int,
        decision_type: str,
        event_class: str,
        payload: dict[str, Any],
        reason_codes: list[str] | None,
        prev_hash: str | None,
        record_hash: str,
        created_at: datetime,
    ) -> None:
        async with self._lock_for(stream_id):
            try:
                async with self._pool.connection() as conn, conn.cursor() as cur:
                    await cur.execute(
                        _INSERT_TRACE_SQL,
                        {
                            "trace_id": trace_id,
                            "decision_type": decision_type,
                            "event_class": event_class,
                            "stream_id": stream_id,
                            "seq": seq,
                            "payload": Jsonb(payload),
                            "reason_codes": reason_codes,
                            "prev_hash": prev_hash,
                            "record_hash": record_hash,
                            "created_at": created_at,
                        },
                    )
                    await conn.commit()
            except psycopg.errors.UniqueViolation as exc:
                raise TraceAppendConflictError(stream_id, seq, exc) from exc

    async def exists_preimage(self, decision_ref: UUID) -> bool:
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_EXISTS_PREIMAGE_SQL, {"trace_id": decision_ref})
            row = await cur.fetchone()
        return bool(row[0]) if row else False

    async def fetch_range(self, stream_id: str, *, from_seq: int) -> list[ChainRecord]:
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_FETCH_RANGE_SQL, {"stream_id": stream_id, "from_seq": from_seq})
            rows = await cur.fetchall()
        return [_row_to_chain_record(row) for row in rows]

    async def stream_ids(self) -> list[str]:
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_STREAM_IDS_SQL)
            rows = await cur.fetchall()
        return [row[0] for row in rows]

    async def insert_checkpoint(
        self,
        *,
        checkpoint_id: UUID,
        checkpoint_at: datetime,
        stream_heads: dict[str, dict[str, Any]],
        checkpoint_hash_hex: str,
    ) -> None:
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                _INSERT_CHECKPOINT_SQL,
                {
                    "checkpoint_id": checkpoint_id,
                    "checkpoint_at": checkpoint_at,
                    "stream_heads": Jsonb(stream_heads),
                    "checkpoint_hash": checkpoint_hash_hex,
                },
            )
            await conn.commit()

    async def prune_before(self, stream_id: str, *, keep_from_seq: int) -> int:
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_PRUNE_BEFORE_SQL, {"stream_id": stream_id, "keep_from_seq": keep_from_seq})
            deleted = cur.rowcount
            await conn.commit()
        return deleted

    async def retention_days_for(self, event_class: str) -> int:
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_RETENTION_DAYS_SQL, {"event_class": event_class})
            row = await cur.fetchone()
        return int(row[0]) if row else DEFAULT_RETENTION_DAYS


async def run_retention_prune_job(
    pool: AsyncConnectionPool, *, now: datetime | None = None
) -> dict[str, int]:
    """The retention pruning job (BUILD.md S4 "health" row): daily cron and on-demand entry point.

    For every `og.retention_policy` row with `prune_after_checkpoint`, deletes `og.trace` rows of that
    `event_class` older than `retention_days` -- but only rows already covered by a checkpoint (02a
    S8.3's `prune(event_class)` algorithm), so `TraceStore.verify()` still passes from the nearest
    surviving checkpoint forward (K11). Returns `{event_class: rows_deleted}`.
    """
    now = now or datetime.now(UTC)
    deleted_by_class: dict[str, int] = {}
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(_RETENTION_POLICIES_SQL)
            policies = await cur.fetchall()

        for event_class, retention_days in policies:
            cutoff = now - timedelta(days=retention_days)
            async with conn.cursor() as cur:
                await cur.execute(_PRUNE_BY_CLASS_SQL, {"event_class": event_class, "cutoff": cutoff})
                deleted_by_class[event_class] = cur.rowcount
        await conn.commit()

    logger.info("trace retention prune completed", extra={"deleted_by_class": deleted_by_class})
    return deleted_by_class
