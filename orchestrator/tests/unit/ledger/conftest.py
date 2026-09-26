"""Fakes satisfying `LedgerBackend`/`CapabilityProvider` for unit + hypothesis tests, without a DB."""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager, nullcontext
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import pytest

from opengrid.ledger import CommitmentRecord, ReservationLedger, ReservationRecord


@dataclass
class InMemoryLedgerBackend:
    """A minimal fake satisfying the `LedgerBackend` protocol."""

    rows: dict[UUID, ReservationRecord] = field(default_factory=dict)
    commitments: dict[UUID, CommitmentRecord] = field(default_factory=dict)
    _version: int = 0

    def write_guard(self) -> AbstractAsyncContextManager[None]:
        return nullcontext()

    async def next_version(self) -> int:
        """Mirrors the Postgres backend: MAX(written version) + 1, nothing persisted until a write."""
        return await self.current_version() + 1

    async def current_version(self) -> int:
        return max((r.ledger_version for r in self.rows.values()), default=self._version)

    async def active_reservations(self, bank_id: str, interval_start: datetime) -> list[ReservationRecord]:
        return [
            r
            for r in self.rows.values()
            if r.bank_id == bank_id and r.interval_start == interval_start and r.is_active
        ]

    async def reservations_for_obligation(self, obligation_id: UUID) -> list[ReservationRecord]:
        return [r for r in self.rows.values() if r.obligation_id == obligation_id]

    async def get_reservation(self, reservation_id: UUID) -> ReservationRecord | None:
        return self.rows.get(reservation_id)

    async def insert_reservations(
        self, records: list[ReservationRecord], commitments: list[CommitmentRecord]
    ) -> None:
        active_keys = {(c.obligation_id, c.interval_start) for c in self.commitments.values()}
        for commitment in commitments:
            if (commitment.obligation_id, commitment.interval_start) in active_keys:
                raise ValueError("ux_commitment_active violated")  # mirrors the DB's partial unique index
        for record in records:
            self.rows[record.reservation_id] = record
        for commitment in commitments:
            self.commitments[commitment.commitment_id] = commitment

    async def release_uncommitted(self, *, reason: str, version: int) -> int:
        committed = {c.obligation_id for c in self.commitments.values()}
        released = 0
        for reservation_id, record in list(self.rows.items()):
            if record.is_active and record.obligation_id not in committed:
                await self.mark_released(reservation_id, reason=reason, version=version)
                released += 1
        return released

    async def mark_released(self, reservation_id: UUID, *, reason: str, version: int) -> None:
        record = self.rows[reservation_id]
        self.rows[reservation_id] = replace(
            record,
            released_at=datetime(2026, 1, 1, tzinfo=UTC),
            release_reason=reason,
            ledger_version=version,
        )


@dataclass
class FixedCapability:
    """A `CapabilityProvider` fake: a flat kW capability per bank, defaulting to `default_kw`."""

    default_kw: Decimal = Decimal(100)
    per_bank_kw: dict[str, Decimal] = field(default_factory=dict)

    async def capability_kw(self, bank_id: str, interval_start: datetime) -> Decimal:
        _ = interval_start
        return self.per_bank_kw.get(bank_id, self.default_kw)


@pytest.fixture
def backend() -> InMemoryLedgerBackend:
    return InMemoryLedgerBackend()


@pytest.fixture
def capability() -> FixedCapability:
    return FixedCapability()


@pytest.fixture
def ledger(backend: InMemoryLedgerBackend, capability: FixedCapability) -> ReservationLedger:
    return ReservationLedger(backend, capability)
