"""Postgres-backed `EngineBackend` for `opengrid.engine`'s process wiring (02b S1.2). Kept separate
from `opengrid.engine.__init__` so the scheduling/wiring logic has no `psycopg` import and stays
unit-testable with a fake (mirrors `opengrid.fleet.pg_backend`/`opengrid.trace.pg_backend`, BUILD.md
S5a "pure logic separated from I/O").
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from psycopg_pool import AsyncConnectionPool

from opengrid.core.models.engine import CommandBatchRow
from opengrid.engine.lifecycle import ClosingObligation

_PENDING_ADMISSION_SQL = """
SELECT DISTINCT op.contract_id
FROM og.opportunity op
JOIN og.obligation ob ON ob.opportunity_id = op.opportunity_id
WHERE op.state = 'OFFERED' AND op.gate_id IS NULL AND ob.state = 'OFFERED'
"""
_DUE_FOR_DELIVERY_SQL = """
SELECT obligation_id FROM og.obligation WHERE state = 'COMMITTED' AND window_start <= %(now)s
"""
_DUE_FOR_CLOSE_SQL = """
SELECT o.obligation_id,
       EXISTS (SELECT 1 FROM og.performance p
               WHERE p.obligation_id = o.obligation_id AND NOT p.passed_threshold) AS any_failed
FROM og.obligation o
WHERE o.state = 'DELIVERING' AND o.window_end <= %(now)s
"""
_DUE_RENOMINATION_SQL = """
SELECT DISTINCT contract_id FROM og.renomination_point
WHERE scheduled_at <= %(now)s AND exercised_at IS NULL
"""
_HEARTBEAT_TS_SQL = "SELECT ts FROM og.heartbeat WHERE process = %(process)s"
_INSERT_COMMAND_BATCH_SQL = """
INSERT INTO og.command_batch
    (command_batch_id, cycle_id, ledger_version, submission_id, command_count, merkle_root,
     trace_pre_image_id)
VALUES (%(command_batch_id)s, %(cycle_id)s, %(ledger_version)s, %(submission_id)s, %(command_count)s,
        %(merkle_root)s, %(trace_pre_image_id)s)
"""
_NOTIFY_SQL = "SELECT pg_notify('og_command_batch', %(payload)s)"


class PgEngineBackend:
    """`EngineBackend` implementation over a `psycopg_pool.AsyncConnectionPool`."""

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def pending_admission_contract_ids(self) -> list[UUID]:
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_PENDING_ADMISSION_SQL)
            rows = await cur.fetchall()
        return [row[0] for row in rows]

    async def obligations_due_for_delivery(self, now: datetime) -> list[UUID]:
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_DUE_FOR_DELIVERY_SQL, {"now": now})
            rows = await cur.fetchall()
        return [row[0] for row in rows]

    async def obligations_due_for_close(self, now: datetime) -> list[ClosingObligation]:
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_DUE_FOR_CLOSE_SQL, {"now": now})
            rows = await cur.fetchall()
        return [ClosingObligation(obligation_id=row[0], any_interval_failed=bool(row[1])) for row in rows]

    async def due_renomination_contract_ids(self, now: datetime) -> list[UUID]:
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_DUE_RENOMINATION_SQL, {"now": now})
            rows = await cur.fetchall()
        return [row[0] for row in rows]

    async def process_heartbeat_age_s(self, process: str, *, now: datetime | None = None) -> float | None:
        now = now or datetime.now(UTC)
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_HEARTBEAT_TS_SQL, {"process": process})
            row = await cur.fetchone()
        if row is None:
            return None
        last_ts: datetime = row[0]
        return (now - last_ts).total_seconds()

    async def insert_command_batch(self, row: CommandBatchRow) -> None:
        # K10: must be durably committed before `notify_guardian` wakes the guardian -- without an
        # explicit commit, the row is not guaranteed visible to guardian's own connection when it polls
        # `og.command_batch` right after the NOTIFY (qa/merge-notes.md S17 fix).
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                _INSERT_COMMAND_BATCH_SQL,
                {
                    "command_batch_id": row.command_batch_id,
                    "cycle_id": row.cycle_id,
                    "ledger_version": row.ledger_version,
                    "submission_id": row.submission_id,
                    "command_count": row.command_count,
                    "merkle_root": row.merkle_root,
                    "trace_pre_image_id": row.trace_pre_image_id,
                },
            )
            await conn.commit()

    async def notify_guardian(self, command_batch_id: UUID) -> None:
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_NOTIFY_SQL, {"payload": str(command_batch_id)})
            await conn.commit()
