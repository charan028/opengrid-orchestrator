"""Fakes for `settle()`/`run_settle_cycle()`/`run_trace_pruning_cycle()` unit tests -- no DB, no MQTT
(BUILD.md S5: those belong to tests/integration)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest

from opengrid.core.models.engine import ServiceType
from opengrid.core.tracehash import ChainRecord
from opengrid.settle.backend import (
    ExistingInvoiceLineRow,
    ExistingMeterInterval,
    ExistingPnl,
    InvoiceLineExportRow,
    MeterIntervalExportRow,
)
from opengrid.settle.models import (
    InvoiceLineDraft,
    ObligationSettlementContext,
    PenaltyParams,
    PowerSample,
    QualityFlag,
)
from opengrid.trace.store import TraceStore


@dataclass
class FakeObligationSetup:
    """What a test wires up for one obligation before calling `settle()`."""

    context: ObligationSettlementContext
    samples: list[PowerSample]
    rule_baseline_delivered_kwh: Decimal | None = None
    best_competing_value_per_kwh: Decimal | None = None


@dataclass
class FakeSettleBackend:
    """An in-memory `SettleBackend` -- exercises the exact idempotency/versioning contract the
    Postgres implementation must also satisfy (mirrored by `tests/integration/settle`)."""

    obligations: dict[UUID, FakeObligationSetup] = field(default_factory=dict)
    meter_intervals: dict[tuple[UUID, datetime], ExistingMeterInterval] = field(default_factory=dict)
    invoice_lines: dict[tuple[UUID, str], ExistingInvoiceLineRow] = field(default_factory=dict)
    pnl_rows: dict[tuple[UUID, datetime], ExistingPnl] = field(default_factory=dict)
    pending: list[tuple[UUID, datetime, datetime]] = field(default_factory=list)

    insert_meter_interval_calls: int = 0
    insert_invoice_line_calls: int = 0
    insert_pnl_calls: int = 0

    #: append-only logs keyed by (obligation_id, interval_start) so property tests sharing one fixture
    #: instance across many generated examples can still count insertions per obligation-interval.
    meter_interval_insert_log: list[tuple[UUID, datetime]] = field(default_factory=list)
    pnl_insert_log: list[tuple[UUID, datetime]] = field(default_factory=list)

    async def fetch_context(self, obligation_id: UUID) -> ObligationSettlementContext:
        return self.obligations[obligation_id].context

    async def fetch_power_samples(
        self, obligation_id: UUID, interval_start: datetime, interval_end: datetime
    ) -> list[PowerSample]:
        return self.obligations[obligation_id].samples

    async def fetch_active_meter_interval(
        self, obligation_id: UUID, interval_start: datetime
    ) -> ExistingMeterInterval | None:
        return self.meter_intervals.get((obligation_id, interval_start))

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
        self.insert_meter_interval_calls += 1
        self.meter_interval_insert_log.append((obligation_id, interval_start))
        new_id = uuid4()
        self.meter_intervals[(obligation_id, interval_start)] = ExistingMeterInterval(
            meter_interval_id=new_id, delivered_kwh=delivered_kwh, version=version
        )
        return new_id

    async def insert_performance(
        self,
        *,
        obligation_id: UUID,
        interval_start: datetime,
        interval_end: datetime,
        compliance_pct: Decimal | None,
        passed_threshold: bool,
    ) -> UUID:
        return uuid4()

    async def fetch_active_invoice_line(
        self,
        contract_id: UUID,
        obligation_id: UUID,
        period_start: date,
        period_end: date,
        line_type: str,
    ) -> ExistingInvoiceLineRow | None:
        return self.invoice_lines.get((obligation_id, line_type))

    async def insert_invoice_line(
        self,
        *,
        contract_id: UUID,
        obligation_id: UUID,
        period_start: date,
        period_end: date,
        draft: InvoiceLineDraft,
        status: str,
        version: int,
        supersedes: UUID | None,
    ) -> UUID:
        self.insert_invoice_line_calls += 1
        new_id = uuid4()
        self.invoice_lines[(obligation_id, draft.line_type)] = ExistingInvoiceLineRow(
            invoice_line_id=new_id, amount=draft.amount, version=version
        )
        return new_id

    async def fetch_active_pnl(self, obligation_id: UUID, interval_start: datetime) -> ExistingPnl | None:
        return self.pnl_rows.get((obligation_id, interval_start))

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
        net_value: Decimal,
        rule_baseline_value: Decimal | None,
        forgone_upside: Decimal,
        version: int,
        supersedes: UUID | None = None,
    ) -> UUID:
        self.insert_pnl_calls += 1
        self.pnl_insert_log.append((obligation_id, interval_start))
        new_id = uuid4()
        self.pnl_rows[(obligation_id, interval_start)] = ExistingPnl(
            pnl_id=new_id, net_value=net_value, version=version
        )
        return new_id

    async def fetch_rule_baseline_delivered_kwh(
        self, obligation_id: UUID, interval_start: datetime, interval_end: datetime
    ) -> Decimal | None:
        return self.obligations[obligation_id].rule_baseline_delivered_kwh

    async def fetch_best_competing_value_per_kwh(
        self, obligation_id: UUID, interval_start: datetime, interval_end: datetime
    ) -> Decimal | None:
        return self.obligations[obligation_id].best_competing_value_per_kwh

    async def fetch_invoice_lines_for_period(
        self, contract_id: UUID, period_start: date, period_end: date
    ) -> list[InvoiceLineExportRow]:
        return []

    async def fetch_meter_intervals_for_period(
        self, obligation_id: UUID, period_start: datetime, period_end: datetime
    ) -> list[MeterIntervalExportRow]:
        return []

    async def fetch_pending_intervals(self) -> list[tuple[UUID, datetime, datetime]]:
        return self.pending


@dataclass
class FakeTraceRow:
    trace_id: UUID
    seq: int
    decision_type: str
    event_class: str
    payload: dict[str, Any]
    prev_hash: str | None
    hash: str
    created_at: datetime


@dataclass
class FakeTraceBackend:
    """Copy of the in-memory `TraceBackend` fake used by `tests/unit/trace/test_store.py`, kept local
    so `settle`'s tests do not depend on another module's test file."""

    streams: dict[str, list[FakeTraceRow]] = field(default_factory=dict)
    checkpoints: list[dict[str, Any]] = field(default_factory=list)
    preimages: set[UUID] = field(default_factory=set)

    async def last_head(self, stream_id: str) -> tuple[int, str | None]:
        rows = self.streams.get(stream_id, [])
        return (rows[-1].seq, rows[-1].hash) if rows else (-1, None)

    async def insert_trace_row(
        self,
        *,
        trace_id,
        stream_id,
        seq,
        decision_type,
        event_class,
        payload,
        reason_codes,
        prev_hash,
        record_hash,
        created_at,
    ) -> None:
        rows = self.streams.setdefault(stream_id, [])
        rows.append(
            FakeTraceRow(
                trace_id, seq, decision_type, event_class, payload, prev_hash, record_hash, created_at
            )
        )
        self.preimages.add(trace_id)

    async def exists_preimage(self, decision_ref: UUID) -> bool:
        return decision_ref in self.preimages

    async def fetch_range(self, stream_id: str, *, from_seq: int) -> list[ChainRecord]:
        rows = self.streams.get(stream_id, [])
        return [
            ChainRecord(r.seq, r.decision_type, r.event_class, r.payload, r.prev_hash, r.hash)
            for r in rows
            if r.seq >= from_seq
        ]

    async def stream_ids(self) -> list[str]:
        return list(self.streams.keys())

    async def insert_checkpoint(
        self, *, checkpoint_id, checkpoint_at, stream_heads, checkpoint_hash_hex
    ) -> None:
        self.checkpoints.append({"stream_heads": stream_heads, "hash": checkpoint_hash_hex})

    async def prune_before(self, stream_id: str, *, keep_from_seq: int) -> int:
        rows = self.streams.get(stream_id, [])
        kept = [r for r in rows if r.seq >= keep_from_seq]
        deleted = len(rows) - len(kept)
        self.streams[stream_id] = kept
        return deleted

    async def retention_days_for(self, event_class: str) -> int:
        return 400


def make_context(
    *,
    obligation_id: UUID | None = None,
    contract_id: UUID | None = None,
    service_type: ServiceType = "DIST_DEFERRAL",
    committed_kw: Decimal = Decimal("10"),
    price_per_kwh: Decimal = Decimal("0.10"),
    wholesale_price_per_kwh: Decimal = Decimal("0.03"),
    eta_d: Decimal = Decimal("0.9487"),
    degradation_cost_per_kwh: Decimal = Decimal("0.03"),
    penalty: PenaltyParams | None = None,
    period_start: date = date(2026, 9, 26),
    period_end: date = date(2026, 9, 26),
) -> ObligationSettlementContext:
    return ObligationSettlementContext(
        obligation_id=obligation_id or uuid4(),
        contract_id=contract_id or uuid4(),
        service_type=service_type,
        committed_kw=committed_kw,
        price_per_kwh=price_per_kwh,
        wholesale_price_per_kwh=wholesale_price_per_kwh,
        eta_d=eta_d,
        degradation_cost_per_kwh=degradation_cost_per_kwh,
        penalty=penalty,
        period_start=period_start,
        period_end=period_end,
    )


@pytest.fixture
def fake_backend() -> FakeSettleBackend:
    return FakeSettleBackend()


@pytest.fixture
def fake_trace_store() -> TraceStore:
    return TraceStore(FakeTraceBackend())


@pytest.fixture(autouse=True)
def _configure_settle(fake_backend: FakeSettleBackend, fake_trace_store: TraceStore):
    """Every test in this package gets a fresh `configure()` call -- `settle`'s module-level
    backend/trace-store singletons must not leak between tests."""
    import opengrid.settle as settle_module

    settle_module.configure(fake_backend, fake_trace_store)
    yield
    settle_module._backend = None
    settle_module._trace_store = None
