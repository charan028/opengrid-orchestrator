"""Row shapes for the engine-owned tables (02a S1), schema `og`. Field names and types mirror
orchestrator/migrations/0001_init.sql exactly.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

ServiceType = Literal["HOME", "ERCOT_ENERGY", "ERCOT_AS", "DIST_DEFERRAL", "PARTNER_CAPACITY", "DATA_CENTER"]
Tier = Literal["L0", "L1", "L2", "T1", "T2", "T3", "T4"]


class _Row(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Contract(_Row):
    contract_id: UUID
    customer_id: UUID
    service_type: ServiceType
    variant: str | None = None
    tier: Tier
    profile_ref: str
    territory_id: UUID | None = None
    start_at: datetime
    end_at: datetime | None = None
    renomination_allowed: bool = False
    penalty_alpha: Decimal | None = None
    penalty_beta: Decimal | None = None
    penalty_theta: Decimal | None = None
    degradation_cost: Decimal = Decimal("0.03")
    fallback_allowed: bool = False
    status: Literal["ACTIVE", "SUSPENDED", "ENDED"] = "ACTIVE"


VariableKind = Literal["CONTINUOUS", "SEMI_CONTINUOUS", "BINARY"]


class ProductRule(_Row):
    product_rule_id: UUID
    contract_id: UUID
    product_code: str
    min_qty_kw: Decimal = Decimal("0")
    increment_kw: Decimal = Decimal("0.1")
    block: bool = False
    duration_minutes: int
    variable_kind: VariableKind


OpportunityState = Literal["OFFERED", "SELECTED", "REJECTED", "EXPIRED"]


class Opportunity(_Row):
    opportunity_id: UUID
    contract_id: UUID
    product_rule_id: UUID | None = None
    window_start: datetime
    window_end: datetime
    requested_kw: Decimal
    value_per_mwh: Decimal | None = None
    scenario_basis: Literal["P10", "P50", "P90"] = "P50"
    state: OpportunityState = "OFFERED"
    reason_code: str | None = None
    admitted_at: datetime
    decided_at: datetime | None = None
    gate_id: UUID | None = None


ObligationState = Literal[
    "OFFERED",
    "SELECTED",
    "COMMITTED",
    "DELIVERING",
    "FULFILLED",
    "SHORTFALL",
    "SETTLED",
    "REJECTED",
    "EXPIRED",
]


class Obligation(_Row):
    obligation_id: UUID
    opportunity_id: UUID
    contract_id: UUID
    service_type: ServiceType
    tier: Tier
    window_start: datetime
    window_end: datetime
    committed_qty_kw: Decimal
    state: ObligationState = "OFFERED"
    at_risk: bool = False
    last_reason_code: str | None = None
    version: int = 1
    # Latest continuous energy-sufficiency snapshot (opengrid.allocator.energy_sufficiency,
    # og.obligation_energy_status, migrations/0009) -- None until the allocator's
    # EnergySufficiencyGateway has evaluated this obligation at least once (e.g. it isn't
    # COMMITTED/DELIVERING yet). The UI (dispatch.html) renders "--" for either field when None.
    energy_margin_kwh: Decimal | None = None
    time_to_depletion_h: Decimal | None = None


class Commitment(_Row):
    commitment_id: UUID
    obligation_id: UUID
    plan_id: UUID
    interval_start: datetime
    interval_end: datetime
    committed_kw: Decimal
    variable_kind: VariableKind
    supersedes: UUID | None = None
    reason_code: str = "R-GATE-SELECT"


class RenominationPoint(_Row):
    renomination_point_id: UUID
    contract_id: UUID
    obligation_id: UUID | None = None
    scheduled_at: datetime
    exercised_at: datetime | None = None
    outcome: Literal["RESELECTED", "CONFIRMED", "SKIPPED"] | None = None
    plan_id: UUID | None = None


class Plan(_Row):
    plan_id: UUID
    plan_mode: Literal["L-DA", "L-ID", "RULE_FALLBACK"]
    gate_kind: Literal["SCHEDULED_15MIN", "ADMISSION", "RENOMINATION"]
    horizon_start: datetime
    horizon_end: datetime
    scenario_set: list[dict[str, Any]]
    solver_status: str
    solver_gap: Decimal | None = None
    solver_time_ms: int | None = None
    objective_value: Decimal | None = None
    superseded_by: UUID | None = None


class Reservation(_Row):
    reservation_id: UUID
    obligation_id: UUID
    bank_id: str  # topology id (`bank-000`); og.reservation.bank_id is text since migration 0004
    kind: Literal["POWER_KW", "ENERGY_KWH"]
    amount: Decimal
    interval_start: datetime
    interval_end: datetime
    ledger_version: int
    released_at: datetime | None = None
    release_reason: str | None = None


class Grant(_Row):
    grant_id: UUID
    cycle_id: str
    obligation_id: UUID | None = None
    bank_id: str  # topology id (`bank-000`); og.grant.bank_id is text since migration 0004
    granted_kw: Decimal
    is_headroom: bool = False
    ledger_version: int
    command_batch_id: UUID | None = None


class CommandBatchRow(_Row):
    command_batch_id: UUID
    cycle_id: str
    ledger_version: int
    submission_id: str
    command_count: int
    merkle_root: str
    trace_pre_image_id: UUID | None = None


VerdictOutcome = Literal["PASS", "PARTLY_VETOED", "VETOED", "TIMEOUT"]


class Verdict(_Row):
    verdict_id: UUID
    command_batch_id: UUID
    outcome: VerdictOutcome
    vetoed_rule_ids: list[str] = []
    latency_ms: int
    inputs_hash: str
    signature: str | None = None
    signed_at: datetime | None = None


class StopEventRow(_Row):
    stop_event_id: UUID
    scope_kind: Literal["BANK", "ZONE", "FLEET"]
    scope_ref: str
    action: Literal["ENGAGE", "RELEASE"]
    initiator_kind: Literal["OPERATOR", "GUARDIAN", "SAFESTOP_AUTHORITY", "UTILITY"]
    initiator_ref: str
    reason: str
    approver_ref: str | None = None
    signature: str


class MeterInterval(_Row):
    meter_interval_id: UUID
    obligation_id: UUID
    interval_start: datetime
    interval_end: datetime
    delivered_kwh: Decimal
    baseline_kwh: Decimal | None = None
    source: Literal["DIRECT_HUB_METER", "AMI_INTERVAL", "SCADA_OUTCOME", "ESTIMATED"]
    quality_flag: Literal["GOOD", "ESTIMATED", "DISPUTED"] = "GOOD"
    version: int = 1
    superseded_by: UUID | None = None


class Performance(_Row):
    performance_id: UUID
    obligation_id: UUID
    interval_start: datetime
    interval_end: datetime
    compliance_pct: Decimal
    season_pct: Decimal | None = None
    availability_pct: Decimal | None = None
    response_time_s: int | None = None
    passed_threshold: bool


class InvoiceLine(_Row):
    invoice_line_id: UUID
    contract_id: UUID
    obligation_id: UUID
    period_start: date
    period_end: date
    line_type: Literal[
        "CAPACITY_PAYMENT", "ENERGY", "AVAILABILITY_PAYMENT", "LD_PENALTY", "DERATE", "BUYBACK", "FIXED_FEE"
    ]
    quantity: Decimal | None = None
    unit: str | None = None
    rate: Decimal | None = None
    amount: Decimal
    status: Literal["PROVISIONAL", "FINAL", "CORRECTED"] = "PROVISIONAL"
    supersedes: UUID | None = None
    trace_roll_up: str | None = None
    version: int = 1


class Pnl(_Row):
    pnl_id: UUID
    obligation_id: UUID
    interval_start: datetime
    interval_end: datetime
    revenue: Decimal = Decimal("0")
    energy_cost: Decimal = Decimal("0")
    degradation_cost: Decimal = Decimal("0")
    penalty: Decimal = Decimal("0")
    net_value: Decimal
    rule_baseline_value: Decimal | None = None
    forgone_upside: Decimal | None = Decimal("0")


DecisionType = Literal[
    "DA_PLAN",
    "ID_PLAN",
    "ADMISSION",
    "COMMITMENT",
    "RENOMINATION",
    "RT_ALLOCATION",
    "SUBSTITUTION",
    "GUARDIAN_VERDICT",
    "SAFE_STOP",
    "SHORTFALL",
    "OPERATOR_ACTION",
    "FEED_CHANGE",
    "ALERT",
    "SETTLEMENT",
    # MVP-S+ additions (06-service-profiles-and-power-quality.md S5.5.7, S8.5; migrations/0011_asset_health.sql
    # extends og.trace's decision_type CHECK constraint to match):
    "ASSET_STATE_TRANSITION",
    "CALIBRATION_ATTEMPT",
]


class TraceRow(_Row):
    trace_id: UUID
    parent_trace_id: UUID | None = None
    decision_type: DecisionType
    event_class: str
    stream_id: str
    seq: int
    scope: dict[str, Any] | None = None
    payload: dict[str, Any]
    reason_codes: list[str] | None = None
    prev_hash: str | None = None
    hash: str


class TraceCheckpoint(_Row):
    checkpoint_id: UUID
    checkpoint_at: datetime
    stream_heads: dict[str, Any]
    checkpoint_hash: str
    anchor_ref: str | None = None


class RetentionPolicy(_Row):
    event_class: str
    retention_days: int
    prune_after_checkpoint: bool = True


class OperatorAction(_Row):
    operator_action_id: UUID
    operator_ref: str
    action_kind: Literal[
        "MANUAL_COMMAND", "SAFE_STOP_ENGAGE", "SAFE_STOP_RELEASE", "APPROVAL", "CONFIG_CHANGE"
    ]
    target_ref: str | None = None
    tier: Literal["PRE_AUTHORIZED", "ENGAGE", "TIER1", "TIER2"] | None = None
    reason: str | None = None
    confirmed_at: datetime | None = None
    approver_ref: str | None = None
    trace_id: UUID | None = None
