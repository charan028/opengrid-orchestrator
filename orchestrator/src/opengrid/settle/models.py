"""Value objects shared across `settle`'s pure computation modules (02a S7).

Kept separate from `opengrid.core.models.engine` because these are *intermediate* computation
results, not row shapes -- the orchestration layer (`opengrid.settle.__init__`) maps them onto the
`og.meter_interval`/`og.performance`/`og.invoice_line`/`og.pnl` row shapes before persisting.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from opengrid.core.models.engine import ServiceType

QualityFlag = Literal["GOOD", "ESTIMATED", "DISPUTED"]
MeterSource = Literal["DIRECT_HUB_METER", "AMI_INTERVAL", "SCADA_OUTCOME", "ESTIMATED"]

#: 02a S7.2: an interval needs >= 13 of its 15 one-minute samples present to be graded GOOD.
MIN_GOOD_SAMPLE_FRACTION = Decimal("13") / Decimal("15")


@dataclass(frozen=True, slots=True)
class PowerSample:
    """One 1-minute telemetry reading feeding interval metering (02a S7.2)."""

    hub_id: str
    ts: datetime
    kw: Decimal


@dataclass(frozen=True, slots=True)
class MeteringResult:
    """Output of `metering.meter_interval` -- delivered energy for one obligation-interval."""

    delivered_kwh: Decimal
    quality_flag: QualityFlag
    source: MeterSource
    samples_present: int
    samples_expected: int


@dataclass(frozen=True, slots=True)
class PerformanceResult:
    """Output of `performance.compute_performance` (02a S7.2)."""

    compliance_pct: Decimal | None
    passed_threshold: bool


@dataclass(frozen=True, slots=True)
class PenaltyParams:
    """Contract-level penalty slope/tolerance (`contract.penalty_alpha/beta/theta`, 02a S1.2)."""

    alpha: Decimal
    beta: Decimal
    theta: Decimal


@dataclass(frozen=True, slots=True)
class PnlBreakdown:
    """Output of `profitability.compute_pnl` -- the four decomposed terms plus net (02a S7.4)."""

    revenue: Decimal
    energy_cost: Decimal
    degradation_cost: Decimal
    penalty: Decimal
    net_value: Decimal


@dataclass(frozen=True, slots=True)
class InvoiceLineDraft:
    """A candidate `og.invoice_line` row before insert-only versioning is resolved (02a S7.3)."""

    line_type: Literal[
        "CAPACITY_PAYMENT", "ENERGY", "AVAILABILITY_PAYMENT", "LD_PENALTY", "DERATE", "BUYBACK", "FIXED_FEE"
    ]
    quantity: Decimal | None
    unit: str | None
    rate: Decimal | None
    amount: Decimal


@dataclass(frozen=True, slots=True)
class ObligationSettlementContext:
    """Everything `settle.settle()` needs about the obligation/contract for one interval, read
    once from the backend (02a S7.1-S7.4). Kept as a plain dataclass (not a DB row shape) so the
    pure formula modules never import `opengrid.platform.db`."""

    obligation_id: UUID
    contract_id: UUID
    service_type: ServiceType
    committed_kw: Decimal
    price_per_kwh: Decimal
    wholesale_price_per_kwh: Decimal
    eta_d: Decimal
    degradation_cost_per_kwh: Decimal
    penalty: PenaltyParams | None
    period_start: date
    period_end: date
