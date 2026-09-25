"""A Postgres `opengrid.trace.store.TraceBackend` for `og-settle`'s own process use.

BUILD.md S4 assigns the *canonical* Postgres `TraceBackend` (`opengrid.trace.pg_backend`) to the
`health` agent, not `settle` -- it does not exist yet. `og-settle` still has a hard requirement
("every billing run traced", BUILD.md's settle row) that cannot wait on that module, so this is a
narrow, settle-owned implementation of the same public `TraceBackend` Protocol
(`opengrid.trace.store.TraceBackend`), reading/writing only `og.trace` / `og.trace_checkpoint` /
`og.retention_policy` (already defined by `orchestrator/migrations/0001_init.sql`, architect-owned --
no migration touched here). Once `opengrid.trace.pg_backend` ships, `settle.main` should switch to
it and this file can be deleted; no other module imports this one.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from opengrid.core.tracehash import ChainRecord

_DEFAULT_RETENTION_DAYS = 400  # 02b S1.4: unknown event_class defaults to 400 days

_LAST_HEAD_SQL = "SELECT seq, hash FROM og.trace WHERE stream_id = %(stream_id)s ORDER BY seq DESC LIMIT 1"

_INSERT_TRACE_SQL = """
INSERT INTO og.trace
    (trace_id, decision_type, event_class, stream_id, seq, payload, reason_codes, prev_hash, hash, created_at)
VALUES (%(trace_id)s, %(decision_type)s, %(event_class)s, %(stream_id)s, %(seq)s, %(payload)s,
        %(reason_codes)s, %(prev_hash)s, %(hash)s, %(created_at)s)
"""

_EXISTS_PREIMAGE_SQL = "SELECT 1 FROM og.trace WHERE trace_id = %(trace_id)s"

_FETCH_RANGE_SQL = """
SELECT seq, decision_type, event_class, payload, prev_hash, hash
FROM og.trace WHERE stream_id = %(stream_id)s AND seq >= %(from_seq)s ORDER BY seq
"""

_STREAM_IDS_SQL = "SELECT DISTINCT stream_id FROM og.trace"

_INSERT_CHECKPOINT_SQL = """
INSERT INTO og.trace_checkpoint (checkpoint_id, checkpoint_at, stream_heads, checkpoint_hash)
VALUES (%(checkpoint_id)s, %(checkpoint_at)s, %(stream_heads)s, %(checkpoint_hash)s)
"""

_PRUNE_BEFORE_SQL = "DELETE FROM og.trace WHERE stream_id = %(stream_id)s AND seq < %(keep_from_seq)s"

_RETENTION_DAYS_SQL = "SELECT retention_days FROM og.retention_policy WHERE event_class = %(event_class)s"


@dataclass(frozen=True, slots=True)
class SettleTracePgBackend:
    pool: AsyncConnectionPool

    async def last_head(self, stream_id: str) -> tuple[int, str | None]:
        async with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(_LAST_HEAD_SQL, {"stream_id": stream_id})
            row = await cur.fetchone()
        return (row["seq"], row["hash"]) if row else (-1, None)

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
        async with self.pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                _INSERT_TRACE_SQL,
                {
                    "trace_id": trace_id,
                    "decision_type": decision_type,
                    "event_class": event_class,
                    "stream_id": stream_id,
                    "seq": seq,
                    "payload": payload,
                    "reason_codes": reason_codes,
                    "prev_hash": prev_hash,
                    "hash": record_hash,
                    "created_at": created_at,
                },
            )

    async def exists_preimage(self, decision_ref: UUID) -> bool:
        async with self.pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_EXISTS_PREIMAGE_SQL, {"trace_id": decision_ref})
            return await cur.fetchone() is not None

    async def fetch_range(self, stream_id: str, *, from_seq: int) -> list[ChainRecord]:
        async with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(_FETCH_RANGE_SQL, {"stream_id": stream_id, "from_seq": from_seq})
            rows = await cur.fetchall()
        return [
            ChainRecord(
                r["seq"], r["decision_type"], r["event_class"], r["payload"], r["prev_hash"], r["hash"]
            )
            for r in rows
        ]

    async def stream_ids(self) -> list[str]:
        async with self.pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_STREAM_IDS_SQL)
            rows = await cur.fetchall()
        return [r[0] for r in rows]

    async def insert_checkpoint(
        self,
        *,
        checkpoint_id: UUID,
        checkpoint_at: datetime,
        stream_heads: dict[str, dict[str, Any]],
        checkpoint_hash_hex: str,
    ) -> None:
        async with self.pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                _INSERT_CHECKPOINT_SQL,
                {
                    "checkpoint_id": checkpoint_id or uuid4(),
                    "checkpoint_at": checkpoint_at,
                    "stream_heads": stream_heads,
                    "checkpoint_hash": checkpoint_hash_hex,
                },
            )

    async def prune_before(self, stream_id: str, *, keep_from_seq: int) -> int:
        async with self.pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_PRUNE_BEFORE_SQL, {"stream_id": stream_id, "keep_from_seq": keep_from_seq})
            return cur.rowcount

    async def retention_days_for(self, event_class: str) -> int:
        async with self.pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_RETENTION_DAYS_SQL, {"event_class": event_class})
            row = await cur.fetchone()
        return int(row[0]) if row else _DEFAULT_RETENTION_DAYS
