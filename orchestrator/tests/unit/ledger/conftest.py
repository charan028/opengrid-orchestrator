"""Fakes satisfying `LedgerBackend`/`CapabilityProvider` for unit + hypothesis tests, without a DB."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import pytest

from opengrid.ledger import ReservationLedger, ReservationRecord


@dataclass
class InMemoryLedgerBackend:
    """A minimal fake satisfying the `LedgerBackend` protocol."""

    rows: dict[UUID, ReservationRecord] = field(default_factory=dict)
    _version: int = 0

    async def next_version(self) -> int:
        self._version += 1
        return self._version

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

    async def insert_reservations(self, records: list[ReservationRecord]) -> None:
        for record in records:
            self.rows[record.reservation_id] = record

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
