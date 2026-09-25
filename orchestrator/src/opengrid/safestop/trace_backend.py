"""Minimal Postgres `opengrid.trace.TraceBackend` adapter for `og-safestop`.

`opengrid.trace.pg_backend` (the shared, full-featured Postgres trace backend) is owned by the
`health` agent and does not exist yet. Rather than block on it or import a not-yet-built module,
`og-safestop` carries its own small adapter here -- it satisfies the same `TraceBackend` `Protocol`
(structural typing, no import of a `health`-owned module needed) and only ever writes/reads the
`"safestop"` stream, so there is nothing for two implementations to disagree about. Once
`opengrid.trace.pg_backend` exists, `main.py` can switch to it with no change to `service.py`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from psycopg_pool import AsyncConnectionPool

from opengrid.core.tracehash import ChainRecord

_LAST_HEAD_SQL = "SELECT seq, hash FROM og.trace WHERE stream_id = %(stream_id)s ORDER BY seq DESC LIMIT 1"

_INSERT_SQL = """
INSERT INTO og.trace
    (trace_id, decision_type, event_class, stream_id, seq, payload, reason_codes, prev_hash, hash,
     created_at)
VALUES (%(trace_id)s, %(decision_type)s, %(event_class)s, %(stream_id)s, %(seq)s, %(payload)s,
        %(reason_codes)s, %(prev_hash)s, %(hash)s, %(created_at)s)
"""

_EXISTS_PREIMAGE_SQL = "SELECT 1 FROM og.trace WHERE trace_id = %(trace_id)s"

_FETCH_RANGE_SQL = """
SELECT seq, decision_type, event_class, payload, prev_hash, hash
FROM og.trace
WHERE stream_id = %(stream_id)s AND seq >= %(from_seq)s
ORDER BY seq ASC
"""

_STREAM_IDS_SQL = "SELECT DISTINCT stream_id FROM og.trace"

_INSERT_CHECKPOINT_SQL = """
INSERT INTO og.trace_checkpoint (checkpoint_id, checkpoint_at, stream_heads, checkpoint_hash)
VALUES (%(checkpoint_id)s, %(checkpoint_at)s, %(stream_heads)s, %(checkpoint_hash)s)
"""

_PRUNE_SQL = "DELETE FROM og.trace WHERE stream_id = %(stream_id)s AND seq < %(keep_from_seq)s"

_RETENTION_SQL = "SELECT retention_days FROM og.retention_policy WHERE event_class = %(event_class)s"

DEFAULT_RETENTION_DAYS = 400  # 02b S1.4: unknown event classes default here


@dataclass
class PgSafestopTraceBackend:
    """`opengrid.trace.store.TraceBackend` implementation scoped to og-safestop's own use."""

    pool: AsyncConnectionPool

    async def last_head(self, stream_id: str) -> tuple[int, str | None]:
        async with self.pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_LAST_HEAD_SQL, {"stream_id": stream_id})
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
        async with self.pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                _INSERT_SQL,
                {
                    "trace_id": trace_id,
                    "decision_type": decision_type,
                    "event_class": event_class,
                    "stream_id": stream_id,
                    "seq": seq,
                    "payload": json.dumps(payload),
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
        async with self.pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_FETCH_RANGE_SQL, {"stream_id": stream_id, "from_seq": from_seq})
            rows = await cur.fetchall()
            return [
                ChainRecord(
                    seq=row[0],
                    decision_type=row[1],
                    event_class=row[2],
                    payload=row[3],
                    prev_hash=row[4],
                    hash=row[5],
                )
                for row in rows
            ]

    async def stream_ids(self) -> list[str]:
        async with self.pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_STREAM_IDS_SQL)
            rows = await cur.fetchall()
            return [str(row[0]) for row in rows]

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
                    "checkpoint_id": checkpoint_id,
                    "checkpoint_at": checkpoint_at,
                    "stream_heads": json.dumps(stream_heads),
                    "checkpoint_hash": checkpoint_hash_hex,
                },
            )

    async def prune_before(self, stream_id: str, *, keep_from_seq: int) -> int:
        async with self.pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_PRUNE_SQL, {"stream_id": stream_id, "keep_from_seq": keep_from_seq})
            return cur.rowcount

    async def retention_days_for(self, event_class: str) -> int:
        async with self.pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_RETENTION_SQL, {"event_class": event_class})
            row = await cur.fetchone()
            return DEFAULT_RETENTION_DAYS if row is None else int(row[0])
