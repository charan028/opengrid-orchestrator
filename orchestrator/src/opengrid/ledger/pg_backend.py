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

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from opengrid.ledger import CommitmentRecord, GrantRecord, ReservationRecord

_ADVISORY_LOCK_KEY = 774_411_001  # arbitrary fixed key for og.reservation's version counter
_WRITE_LOCK_KEY = 774_411_002  # arbitrary fixed key serializing reserve() across processes (K2)

_INSERT_COMMITMENT_SQL = """
INSERT INTO og.commitment
    (commitment_id, obligation_id, plan_id, interval_start, interval_end, committed_kw, variable_kind,
     reason_code)
VALUES (%(commitment_id)s, %(obligation_id)s, %(plan_id)s, %(interval_start)s, %(interval_end)s,
        %(committed_kw)s, %(variable_kind)s, %(reason_code)s)
"""

_RELEASE_UNCOMMITTED_SQL = """
UPDATE og.reservation r
SET released_at = now(), release_reason = %(reason)s, ledger_version = %(version)s
WHERE r.released_at IS NULL
  AND NOT EXISTS (
      SELECT 1 FROM og.commitment c WHERE c.obligation_id = r.obligation_id AND c.supersedes IS NULL
  )
"""


def _commitment_params(commitment: CommitmentRecord) -> dict[str, Any]:
    return {
        "commitment_id": commitment.commitment_id,
        "obligation_id": commitment.obligation_id,
        "plan_id": commitment.plan_id,
        "interval_start": commitment.interval_start,
        "interval_end": commitment.interval_end,
        "committed_kw": commitment.committed_kw,
        "variable_kind": commitment.variable_kind,
        "reason_code": commitment.reason_code,
    }


class PgLedgerBackend:
    """`LedgerBackend` implementation over `og.reservation` via an `AsyncConnectionPool`."""

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    @asynccontextmanager
    async def write_guard(self) -> AsyncIterator[None]:
        """Hold a session-level advisory lock on a dedicated connection for one reserve() check-then-
        insert, so two processes can never both pass K2 against the same free headroom. (The
        `FOR UPDATE` reads alone lock nothing: each runs in its own short transaction.)"""
        async with self._pool.connection() as conn:
            await conn.execute("SELECT pg_advisory_lock(%s)", (_WRITE_LOCK_KEY,))
            try:
                yield
            finally:
                await conn.execute("SELECT pg_advisory_unlock(%s)", (_WRITE_LOCK_KEY,))

    async def next_version(self) -> int:
        async with self._pool.connection() as conn, conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(%s)", (_ADVISORY_LOCK_KEY,))
            cur = await conn.execute("SELECT COALESCE(MAX(ledger_version), 0) + 1 FROM og.reservation")
            row = await cur.fetchone()
            return int(row[0]) if row else 1

    async def current_version(self) -> int:
        async with self._pool.connection() as conn:
            cur = await conn.execute("SELECT COALESCE(MAX(ledger_version), 0) FROM og.reservation")
            row = await cur.fetchone()
            return int(row[0]) if row else 0

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

    async def insert_reservations(
        self, records: list[ReservationRecord], commitments: list[CommitmentRecord]
    ) -> None:
        """Reservations and their equality-freeze commitment rows in ONE transaction, so an obligation
        is never left holding K2 headroom without a commitment (or frozen without reservations)."""
        if not records and not commitments:
            return
        async with self._pool.connection() as conn, conn.transaction():
            for commitment in commitments:
                await conn.execute(_INSERT_COMMITMENT_SQL, _commitment_params(commitment))
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

    async def release_uncommitted(self, *, reason: str, version: int) -> int:
        async with self._pool.connection() as conn, conn.transaction():
            cur = await conn.execute(_RELEASE_UNCOMMITTED_SQL, {"reason": reason, "version": version})
            return cur.rowcount


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
            # One grant set per 2 s cycle, re-derived every cycle: commit without waiting on the WAL
            # fsync (see opengrid.fleet.pg_backend._ASYNC_COMMIT_SQL). Reservations/commitments above
            # keep synchronous commit (K2/K13 durability).
            await conn.execute("SET LOCAL synchronous_commit TO OFF")
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
