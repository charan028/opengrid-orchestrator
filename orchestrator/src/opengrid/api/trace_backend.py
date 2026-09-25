"""Postgres-backed `opengrid.trace.TraceBackend` used only for `api`'s own writes (every mutating
endpoint records `operator_action` + `trace`, BUILD.md api row).

`opengrid.trace.pg_backend` (the shared Postgres trace backend, 02b S1.2) is owned by the `health`
agent and used by `engine`/`guardian`/`settle`'s much higher-volume writers. `api` writes at most a few
rows per operator action, so rather than block on that module landing, this file implements the same
`TraceBackend` protocol directly against `og.trace`/`og.trace_checkpoint`/`og.retention_policy` --
no hash-chain or canonicalization logic is duplicated (that all stays in `opengrid.core.tracehash` /
`opengrid.trace.store`), this is only the I/O adapter `TraceStore` already expects any backend to
supply.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from opengrid.core.tracehash import ChainRecord

_DEFAULT_RETENTION_DAYS = 400  # 02b S1.4: unknown classes default to 400 days, never silently dropped


class PgTraceBackend:
    """One instance per request/pool; every method opens its own pooled connection."""

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def last_head(self, stream_id: str) -> tuple[int, str | None]:
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                "SELECT seq, hash FROM og.trace WHERE stream_id = %s ORDER BY seq DESC LIMIT 1",
                (stream_id,),
            )
            row = await cur.fetchone()
            if row is None:
                return -1, None
            return int(row[0]), str(row[1])

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
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO og.trace
                    (trace_id, decision_type, event_class, stream_id, seq, payload, reason_codes,
                     prev_hash, hash, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    trace_id,
                    decision_type,
                    event_class,
                    stream_id,
                    seq,
                    _json(payload),
                    reason_codes,
                    prev_hash,
                    record_hash,
                    created_at,
                ),
            )

    async def exists_preimage(self, decision_ref: UUID) -> bool:
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                "SELECT 1 FROM og.trace WHERE payload ->> 'decision_ref' = %s LIMIT 1",
                (str(decision_ref),),
            )
            return await cur.fetchone() is not None

    async def fetch_range(self, stream_id: str, *, from_seq: int) -> list[ChainRecord]:
        async with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT seq, decision_type, event_class, payload, prev_hash, hash
                FROM og.trace WHERE stream_id = %s AND seq >= %s ORDER BY seq ASC
                """,
                (stream_id, from_seq),
            )
            rows = await cur.fetchall()
        return [
            ChainRecord(
                seq=row["seq"],
                decision_type=row["decision_type"],
                event_class=row["event_class"],
                payload=row["payload"],
                prev_hash=row["prev_hash"],
                hash=row["hash"],
            )
            for row in rows
        ]

    async def stream_ids(self) -> list[str]:
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute("SELECT DISTINCT stream_id FROM og.trace")
            return [row[0] for row in await cur.fetchall()]

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
                """
                INSERT INTO og.trace_checkpoint (checkpoint_id, checkpoint_at, stream_heads, checkpoint_hash)
                VALUES (%s, %s, %s, %s)
                """,
                (checkpoint_id, checkpoint_at, _json(stream_heads), checkpoint_hash_hex),
            )

    async def prune_before(self, stream_id: str, *, keep_from_seq: int) -> int:
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                "DELETE FROM og.trace WHERE stream_id = %s AND seq < %s", (stream_id, keep_from_seq)
            )
            return cur.rowcount

    async def retention_days_for(self, event_class: str) -> int:
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                "SELECT retention_days FROM og.retention_policy WHERE event_class = %s", (event_class,)
            )
            row = await cur.fetchone()
            return int(row[0]) if row is not None else _DEFAULT_RETENTION_DAYS


def _json(obj: dict[str, Any]) -> Any:
    """psycopg needs an explicit Json() wrapper to send a dict as `jsonb`."""
    from psycopg.types.json import Jsonb

    return Jsonb(obj)
