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
from opengrid.core.models.market import Market, UtilityId

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
    #: 09 D5's M1 TDSP delivery charge on kWh drawn from the grid to charge, ERCOT competitive area
    #: only (`opengrid.settle.tariffs`). Zero for a regulated-territory asset or behind-the-meter
    #: solar charging.
    delivery_charge: Decimal
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
    #: What was paid to CHARGE the energy now being discharged -- drives `profitability.compute_pnl`'s
    #: `energy_cost` (09-optimizer-dispatcher-update.md S0.2 finding G4: it is never the
    #: discharge-interval wholesale price). See `charging_cost_flag` for provenance.
    charging_cost_per_kwh: Decimal
    eta_d: Decimal
    degradation_cost_per_kwh: Decimal
    penalty: PenaltyParams | None
    period_start: date
    period_end: date
    #: D-18 (00-invariants.md K13 "commitments are over a period"): `True` when the obligation's
    #: latest `og.service_profile.setpoint_source` is `MEASURED_FEEDBACK` (DATA_CENTER, PIPELINE_AC)
    #: -- its committed kWh is a reserved maximum, not a fixed schedule (see
    #: `performance.is_need_basis_compliant`).
    is_need_basis: bool = False
    #: Where `charging_cost_per_kwh` came from (`opengrid.settle.pg_backend.charging_cost_from_proxy`):
    #: "OBLIGATION_CHARGE" = the obligation's own recorded charging interval(s) (not yet implemented --
    #: MVP-S has no per-obligation charge attribution), "TRAILING_24H_OFFPEAK_PROXY" = the documented
    #: proxy (the trailing 24h off-peak SPP average for the obligation's bank zone), "MISSING" = no
    #: off-peak SPP observation in that window (energy cost 0, logged).
    charging_cost_flag: str = "TRAILING_24H_OFFPEAK_PROXY"
    #: Where `price_per_kwh` came from: "MCPC" (ERCOT_AS: the cleared DAM MCPC of the award's product in
    #: the delivery hour), "OPPORTUNITY_PRICE" (ERCOT_AS with no MCPC observation -- the opportunity's own
    #: figure, logged), "CONTRACT" (every other service: the opportunity/contract price).
    price_flag: str = "CONTRACT"
    #: Informational only (no longer drives `energy_cost`, kept for the settlement trace/audit trail):
    #: the bank zone's real-time SPP AT the discharge interval, "what the market was paying when we
    #: actually delivered" -- see `charging_cost_per_kwh` for what actually prices the energy.
    wholesale_price_per_kwh: Decimal = Decimal("0")
    #: Flag for `wholesale_price_per_kwh`: "SPP" = the exact interval's SPP, "SPP_PRIOR" = the nearest
    #: earlier SPP within 1 h, "MISSING" = none within 1 h.
    wholesale_price_flag: str = "SPP"
    #: The obligation's bank load zone (09 D5's M1 delivery charge: resolves the TDSP via
    #: `opengrid.settle.tariffs.tdsp_for_zone`), or `None` when it cannot be determined -- `None`
    #: resolves to no TDSP, so M1 settles as 0 rather than guessing.
    zone: str | None = None
    #: 08/09 two-market model (`opengrid.core.models.market`, migration 0025): `REGULATED` (a
    #: vertically integrated utility) or `FREE` (ERCOT competitive area). Drives which charging-cost
    #: model and which delivery-charge rule applies in `opengrid.settle.__init__` -- M1
    #: (`opengrid.settle.tariffs`) for FREE, the utility's own terms
    #: (`opengrid.market.charging.regulated_charging_cost`) for REGULATED.
    market: Market = "FREE"
    #: The contract's utility, set iff `market == "REGULATED"` (a DB CHECK enforces it on `og.contract`).
    utility_id: UtilityId | None = None
