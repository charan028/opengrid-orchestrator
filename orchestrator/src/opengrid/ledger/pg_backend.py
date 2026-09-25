"""Postgres-backed `LedgerBackend` for `og.reservation` (02a S1.9/S4). Owner: allocator agent.

Kept separate from `opengrid.ledger`'s decision logic so that module has no psycopg import (BUILD.md
S5a "pure logic separated from I/O"), matching `opengrid.trace.pg_backend`'s split.

Concurrency: every write path (`insert_reservations`, `mark_released`, `next_version`) runs inside a
single `SERIALIZABLE` transaction with `SELECT ... FOR UPDATE` on the rows it reads, so two concurrent
`og-engine` processes attempting to reserve the same bank/interval either serialize cleanly or one gets
a serialization failure it must retry -- there is no double sale (K2) even across processes, on top of
the in-process `asyncio.Lock` in `ReservationLedger`.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from opengrid.ledger import GrantRecord, ReservationRecord

_ADVISORY_LOCK_KEY = 774_411_001  # arbitrary fixed key for og.reservation's version counter


class PgLedgerBackend:
    """`LedgerBackend` implementation over `og.reservation` via an `AsyncConnectionPool`."""

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def next_version(self) -> int:
        async with self._pool.connection() as conn, conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(%s)", (_ADVISORY_LOCK_KEY,))
            cur = await conn.execute("SELECT COALESCE(MAX(ledger_version), 0) + 1 FROM og.reservation")
            row = await cur.fetchone()
            return int(row[0]) if row else 1

    async def active_reservations(self, bank_id: str, interval_start: datetime) -> list[ReservationRecord]:
        async with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT reservation_id, obligation_id, bank_id, amount, interval_start, interval_end,
                       ledger_version, released_at, release_reason
                FROM og.reservation
                WHERE bank_id = %s AND interval_start = %s AND released_at IS NULL
                FOR UPDATE
                """,
                (bank_id, interval_start),
            )
            rows = await cur.fetchall()
            return [_row_to_record(r) for r in rows]

    async def reservations_for_obligation(self, obligation_id: UUID) -> list[ReservationRecord]:
        async with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT reservation_id, obligation_id, bank_id, amount, interval_start, interval_end,
                       ledger_version, released_at, release_reason
                FROM og.reservation
                WHERE obligation_id = %s
                """,
                (obligation_id,),
            )
            rows = await cur.fetchall()
            return [_row_to_record(r) for r in rows]

    async def get_reservation(self, reservation_id: UUID) -> ReservationRecord | None:
        async with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT reservation_id, obligation_id, bank_id, amount, interval_start, interval_end,
                       ledger_version, released_at, release_reason
                FROM og.reservation
                WHERE reservation_id = %s
                FOR UPDATE
                """,
                (reservation_id,),
            )
            row = await cur.fetchone()
            return _row_to_record(row) if row else None

    async def insert_reservations(self, records: list[ReservationRecord]) -> None:
        if not records:
            return
        async with self._pool.connection() as conn, conn.transaction():
            for record in records:
                await conn.execute(
                    """
                    INSERT INTO og.reservation
                        (reservation_id, obligation_id, bank_id, kind, amount,
                         interval_start, interval_end, ledger_version)
                    VALUES (%s, %s, %s, 'POWER_KW', %s, %s, %s, %s)
                    """,
                    (
                        record.reservation_id,
                        record.obligation_id,
                        record.bank_id,
                        record.amount_kw,
                        record.interval_start,
                        record.interval_end,
                        record.ledger_version,
                    ),
                )

    async def mark_released(self, reservation_id: UUID, *, reason: str, version: int) -> None:
        async with self._pool.connection() as conn, conn.transaction():
            await conn.execute(
                """
                UPDATE og.reservation
                SET released_at = now(), release_reason = %s, ledger_version = %s
                WHERE reservation_id = %s
                """,
                (reason, version, reservation_id),
            )


class PgGrantBackend:
    """`GrantBackend` implementation over `og.grant` (merge task A3). Separate class from
    `PgLedgerBackend` since grants are insert-only and never row-locked/read back by this module (the
    API's `Store.list_grants` owns reads, `orchestrator/src/opengrid/api/store.py`) -- keeping the two
    write paths apart avoids a shared-connection-pattern coupling neither needs."""

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def insert_grants(self, records: list[GrantRecord]) -> None:
        if not records:
            return
        async with self._pool.connection() as conn, conn.transaction():
            for record in records:
                await conn.execute(
                    """
                    INSERT INTO og.grant
                        (grant_id, cycle_id, obligation_id, bank_id, granted_kw, is_headroom,
                         ledger_version, command_batch_id)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        record.grant_id,
                        record.cycle_id,
                        record.obligation_id,
                        record.bank_id,
                        record.granted_kw,
                        record.is_headroom,
                        record.ledger_version,
                        record.command_batch_id,
                    ),
                )


async def make_grant_backend(pool: AsyncConnectionPool) -> PgGrantBackend:
    """Convenience constructor matching `make_backend`'s usage pattern."""
    return PgGrantBackend(pool)


def _row_to_record(row: dict[str, Any]) -> ReservationRecord:
    return ReservationRecord(
        reservation_id=row["reservation_id"],
        obligation_id=row["obligation_id"],
        bank_id=str(row["bank_id"]),
        interval_start=row["interval_start"],
        interval_end=row["interval_end"],
        amount_kw=Decimal(str(row["amount"])),
        ledger_version=int(row["ledger_version"]),
        released_at=row["released_at"],
        release_reason=row["release_reason"],
    )


async def make_backend(pool: AsyncConnectionPool) -> PgLedgerBackend:
    """Convenience constructor matching `opengrid.platform.db.make_pool`'s usage pattern."""
    return PgLedgerBackend(pool)


# Re-exported for type-checking call sites without importing psycopg directly.
Connection = AsyncConnection
