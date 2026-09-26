"""I/O contract for `settle.settle()` (02a S7), mirroring `opengrid.trace.store`'s
backend-protocol pattern so the orchestration logic in `opengrid.settle.__init__` stays testable
without a database (BUILD.md S5a: "pure logic separated from I/O").

`PgSettleBackend` (the real implementation) lives in `opengrid.settle.pg_backend` so this module has
no `psycopg` import; unit tests use a plain in-memory fake instead.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Literal, Protocol
from uuid import UUID

from opengrid.core.models.market import Utility, UtilityId
from opengrid.settle.models import (
    InvoiceLineDraft,
    ObligationSettlementContext,
    PowerSample,
    QualityFlag,
    ZoneChargeEnergy,
)


@dataclass(frozen=True, slots=True)
class ExistingMeterInterval:
    meter_interval_id: UUID
    delivered_kwh: Decimal
    version: int


@dataclass(frozen=True, slots=True)
class ExistingInvoiceLineRow:
    invoice_line_id: UUID
    amount: Decimal
    version: int


@dataclass(frozen=True, slots=True)
class ExistingPnl:
    pnl_id: UUID
    net_value: Decimal
    version: int


@dataclass(frozen=True, slots=True)
class InvoiceLineExportRow:
    """One `og.invoice_line` row, flattened for CSV export (02a S7's ES08-S05)."""

    invoice_line_id: UUID
    contract_id: UUID
    obligation_id: UUID
    period_start: date
    period_end: date
    line_type: str
    quantity: Decimal | None
    unit: str | None
    rate: Decimal | None
    amount: Decimal
    status: str
    supersedes: UUID | None
    version: int


@dataclass(frozen=True, slots=True)
class MeterIntervalExportRow:
    """One `og.meter_interval` row, flattened for CSV export (M&V records, ES08-S05)."""

    meter_interval_id: UUID
    obligation_id: UUID
    interval_start: datetime
    interval_end: datetime
    delivered_kwh: Decimal
    baseline_kwh: Decimal | None
    source: str
    quality_flag: QualityFlag
    version: int
    superseded_by: UUID | None


class SettleBackend(Protocol):
    """Everything `settle()` reads/writes. One obligation-interval per call (02a S7's granularity);
    `opengrid.settle.__init__.settle` is safe to call concurrently for different obligations because
    every write here is scoped to a single `obligation_id`/`interval_start`."""

    async def fetch_context(
        self, obligation_id: UUID, interval_start: datetime | None = None
    ) -> ObligationSettlementContext:
        """Contract/obligation terms needed for M&V, billing and profitability. Raises `LookupError`
        if the obligation does not exist. With `interval_start`, `wholesale_price_per_kwh` is the
        market price for that 15-minute interval (independent of the contract price)."""
        ...

    async def fetch_power_samples(
        self, obligation_id: UUID, interval_start: datetime, interval_end: datetime
    ) -> list[PowerSample]: ...

    async def fetch_measured_need_kwh(
        self, obligation_id: UUID, interval_start: datetime, interval_end: datetime
    ) -> Decimal | None:
        """D-18 need-basis settlement: the customer's measured need for this interval (migration
        0026's `og.customer_site_meter_reading`, average import `p_kw` over the interval x its
        duration), or `None` if there is no reading -- never called unless `ObligationSettlementContext
        .is_need_basis` is `True` (`performance.is_need_basis_compliant` treats `None` as compliant by
        default, so a missing reading never manufactures a penalty)."""
        ...

    async def fetch_zone_charge_energy(
        self, zone: str, window_start: datetime, window_end: datetime
    ) -> ZoneChargeEnergy:
        """09 D5's M1 fleet-level proxy: every hub in `zone`'s charging over `[window_start, window_end)`
        from `og.telemetry`, split into total and grid-drawn (beyond the home's PV surplus). Only ever
        called for a zone that resolves to a TDSP (never a regulated zone). `settle()` turns its
        `grid_share` into the obligation-interval's grid-charged kWh
        (`tariffs.grid_charged_kwh_for_delivery`)."""
        ...

    async def fetch_shortfall_risk_open(
        self, obligation_id: UUID, interval_start: datetime, interval_end: datetime
    ) -> bool:
        """Whether an `ALR-ENERGY-SHORTFALL-RISK` alert for this obligation was open at any time during the
        interval (opened before its end, not cleared before its start)."""
        ...

    async def fetch_pjm_emergency_rate(
        self, obligation_id: UUID, interval_start: datetime, interval_end: datetime
    ) -> Decimal | None:
        """`opengrid.settle.services_extra.pjm_non_performance_charge`'s `non_performance_rate_per_kwh`
        when this interval falls inside a declared PJM emergency performance hour for this
        obligation, else `None` (not an emergency hour -- the ordinary capacity payment applies with
        no extra charge). MVP-S has no live PJM emergency-hour declaration feed yet (PJM stays
        simulated, per the owner's 2026-09-26 decision) -- `PgSettleBackend` returns `None` until one
        exists, logged rather than silently assuming an emergency hour never happened."""
        ...

    async def fetch_utility(self, utility_id: UtilityId) -> Utility | None:
        """The regulated utility's terms (`og.utility`, migration 0025), or `None` if no row exists
        for it yet -- `opengrid.settle.__init__` then falls back to `opengrid.market.config.
        DEFAULT_UTILITIES`'s planning values (the same fallback `opengrid.market` itself documents),
        never a guess of its own."""
        ...

    async def fetch_active_meter_interval(
        self, obligation_id: UUID, interval_start: datetime
    ) -> ExistingMeterInterval | None:
        """The current (`superseded_by IS NULL`) meter_interval row, if any (idempotency check)."""
        ...

    async def insert_meter_interval(
        self,
        *,
        obligation_id: UUID,
        interval_start: datetime,
        interval_end: datetime,
        delivered_kwh: Decimal,
        baseline_kwh: Decimal | None,
        source: str,
        quality_flag: QualityFlag,
        version: int,
        supersedes: UUID | None = None,
    ) -> UUID:
        """Insert a new `meter_interval` row (never an UPDATE of its data columns); returns its id.

        When `supersedes` is given, retiring the old row (pointing its `superseded_by` at this new
        row's id) and inserting this row happen atomically, in ONE transaction, with the retirement
        applied FIRST -- never the other way around. `ux_meter_active`'s partial unique index allows
        at most one active (`superseded_by IS NULL`) row per (obligation_id, interval_start); an
        insert-then-retire order would violate it the instant the new row is added while the old one
        is still active. Retiring first requires the self-referencing FK to be deferred (checked at
        COMMIT, migration 0007), since the old row's `superseded_by` briefly points at an id that does
        not exist as a row yet.
        """
        ...

    async def insert_performance(
        self,
        *,
        obligation_id: UUID,
        interval_start: datetime,
        interval_end: datetime,
        compliance_pct: Decimal | None,
        passed_threshold: bool,
    ) -> UUID: ...

    async def fetch_active_invoice_line(
        self,
        contract_id: UUID,
        obligation_id: UUID,
        period_start: date,
        period_end: date,
        line_type: str,
    ) -> ExistingInvoiceLineRow | None:
        """The latest (highest-`version`) line for this (contract, obligation, period, line_type)."""
        ...

    async def insert_invoice_line(
        self,
        *,
        contract_id: UUID,
        obligation_id: UUID,
        period_start: date,
        period_end: date,
        draft: InvoiceLineDraft,
        status: Literal["PROVISIONAL", "FINAL", "CORRECTED"],
        version: int,
        supersedes: UUID | None,
    ) -> UUID: ...

    async def fetch_active_pnl(self, obligation_id: UUID, interval_start: datetime) -> ExistingPnl | None:
        """The current (`superseded_by IS NULL`) `pnl` row for this obligation-interval, if any --
        insert-only + versioned exactly like `meter_interval` (02a S1's insert-only-table rule; a DB
        partial unique index on `(obligation_id, interval_start) WHERE superseded_by IS NULL` is the
        actual idempotency guard, not just this read-then-compare check)."""
        ...

    async def insert_pnl(
        self,
        *,
        obligation_id: UUID,
        interval_start: datetime,
        interval_end: datetime,
        revenue: Decimal,
        energy_cost: Decimal,
        degradation_cost: Decimal,
        penalty: Decimal,
        delivery_charge: Decimal,
        net_value: Decimal,
        rule_baseline_value: Decimal | None,
        forgone_upside: Decimal,
        version: int,
        supersedes: UUID | None = None,
    ) -> UUID:
        """Insert a new `pnl` row (never an UPDATE of its data columns); returns its id. `supersedes`
        behaves exactly as `insert_meter_interval`'s: retire-then-insert, atomically, in one
        transaction (`ux_pnl_active`, migration 0006/0007)."""
        ...

    async def fetch_rule_baseline_delivered_kwh(
        self, obligation_id: UUID, interval_start: datetime, interval_end: datetime
    ) -> Decimal | None:
        """Shadow rule-allocator's hypothetical delivered kWh for the same tick (02a S7.4), or `None`
        if no shadow run covers this interval yet."""
        ...

    async def fetch_best_competing_value_per_kwh(
        self, obligation_id: UUID, interval_start: datetime, interval_end: datetime
    ) -> Decimal | None:
        """The highest `value_per_mwh`/1000 among candidate opportunities that could have used this
        obligation's committed capacity this interval, had the lock not held it (02a S7.4 forgone
        upside), or `None` if no such candidate exists."""
        ...

    async def fetch_invoice_lines_for_period(
        self, contract_id: UUID, period_start: date, period_end: date
    ) -> list[InvoiceLineExportRow]:
        """Every invoice-line version (not just the active one) in the period, for CSV export
        (ES08-S05: "the CSV matches the underlying records exactly, including superseded-line
        links")."""
        ...

    async def fetch_meter_intervals_for_period(
        self, obligation_id: UUID, period_start: datetime, period_end: datetime
    ) -> list[MeterIntervalExportRow]:
        """Every meter-interval version for the obligation in the period, for CSV export."""
        ...

    async def fetch_pending_intervals(self) -> list[tuple[UUID, datetime, datetime]]:
        """`(obligation_id, interval_start, interval_end)` tuples due for settlement this tick: every
        completed 15-min interval of every `DELIVERING`/`FULFILLED`/`SHORTFALL` obligation that has
        not yet been metered (02a S2.1's `FULFILLED/SHORTFALL -> SETTLED` transition trigger). Used
        by `opengrid.settle.main`'s tick; the pure `settle()` orchestration above does not call this
        itself so unit tests can call `settle()` directly for one obligation-interval."""
        ...

    async def fetch_settleable_obligations(self) -> list[UUID]:
        """`FULFILLED`/`SHORTFALL` obligations whose every window interval is metered, with P&L posted and
        (for every service but HOME) an invoice line -- ready for `-> SETTLED` (02a S2.1)."""
        ...
