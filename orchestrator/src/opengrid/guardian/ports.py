"""Guardian's independently-read input ports (02a S6.1: "on its own independently-read inputs").

The guardian must never trust the allocator's claim that a limit was respected -- it re-reads hub
telemetry, SCADA, the ledger version, commitments and prior grants itself and re-runs the same
`opengrid.core.limits`/`opengrid.core.timeutil` functions the allocator used at planning time. These
`Protocol`s are the seams: `opengrid.guardian.repo` implements them against Postgres/MQTT for the real
process; tests implement them with plain in-memory fakes so `GuardianService` (service.py) stays free of
any I/O import, per BUILD.md S5a "pure logic separated from I/O".
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Literal, Protocol
from uuid import UUID

from opengrid.core.models.market import UtilityId
from opengrid.core.physics import BankParams, HubParams
from opengrid.guardian.pq_ports import (
    CalibrationHistoryPort,
    CalibrationLedgerPort,
    FirmwareCalibrationBoundsPort,
    HubAssetStatePort,
    PqEnvelopeStatePort,
    PqMeasurementPort,
    SensitiveGrantPort,
)

SafeStopScope = Literal["FLEET", "ZONE", "BANK"]
UtilityInstructionKind = Literal["LIMIT", "BLOCK", "ESTOP"]


@dataclass(frozen=True, slots=True)
class ProposedItem:
    """One hub-level command inside a proposed (unsigned) batch.

    `obligation_id`/`obligation_granted_kw` are the engine's own claim of how much of this item's
    setpoint counts toward a specific committed obligation -- G-19 re-derives the *lock* independently
    (against guardian's own reads of `commitment`/prior `grant`), but it needs the engine's mapping of
    which obligation each item serves (the allocator, not guardian, decides service assignment).
    """

    hub_id: str
    p_kw_setpoint: float  # +charge / -discharge, same convention as core.physics
    reason_code: str
    obligation_id: UUID | None = None
    obligation_granted_kw: Decimal | None = None


@dataclass(frozen=True, slots=True)
class ProposedBatch:
    """The full proposed command batch content for `command_batch_id` -- the "engine -> guardian:
    proposed batch" hand-off (02b S5, "internal, not MQTT"). Guardian never re-derives this from
    anything the allocator computed; it is read back from the durable trace pre-image (K10: the batch
    cannot be proposed to guardian before its decision pre-image is written), keyed by
    `command_batch_id`, via `ProposalPort.fetch`.
    """

    command_batch_id: UUID
    bank_id: str
    cycle_id: str
    epoch: int
    seq: int
    issued_at: datetime
    expires_at: datetime
    ledger_version: int
    items: list[ProposedItem]
    is_firm_event: bool = False


@dataclass(frozen=True, slots=True)
class L2Instruction:
    kind: UtilityInstructionKind
    limit_kw: float | None


@dataclass(frozen=True, slots=True)
class Reading:
    """One telemetry value the guardian itself received, and how old it is now."""

    value: float
    age_s: float


@dataclass(frozen=True, slots=True)
class HubFlowTelemetry:
    """The hub telemetry the flow-limit checks read (09 S2.6, additive telemetry fields). A field is None
    when this hub has never reported it (the static limits apply until it does, unless
    `flow_telemetry_required`); a reported value older than the limit is stale (fail closed)."""

    meter_kw: Reading | None = None  # meter net import, + = import
    pv_kw: Reading | None = None
    cell_temp_c: Reading | None = None
    p_dis_max_kw: Reading | None = None  # BMS discharge limit (magnitude)
    p_ch_max_kw: Reading | None = None
    peak_budget_kws: Reading | None = None


@dataclass(frozen=True, slots=True)
class HubSnapshot:
    """Guardian's own, independently-read view of one hub: its physical params and the last
    hub-reported telemetry (never the allocator's planning-time assumption)."""

    params: HubParams
    soc_kwh: float
    prev_p_kw: float
    health: Literal["online", "stale", "offline", "fault"]
    flow: HubFlowTelemetry = HubFlowTelemetry()
    #: the hub's own timestamp of the telemetry sample `prev_p_kw` came from (None: none yet); G-04 anchors a
    #: utility-scale hub at the guardian's last signed setpoint only while that is not clearly older than this
    telemetry_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class HubSite:
    """Static premise data for one hub (og.hub / the interconnection agreement), read by the guardian."""

    export_limit_kw: float | None  # X_exp; None = unknown (0: discharge only to the measured load)
    service_kw: float | None  # S_svc; None = unknown (the configured default service rating)
    pv_rated_kw: float
    peak_kw: float | None  # P_pk; None = no peak allowance
    tau_peak_s: float | None
    transformer_id: str | None  # None = unmapped (a group of one at the default per-home rating)


@dataclass(frozen=True, slots=True)
class ServiceTransformer:
    transformer_id: str
    rating_kva: float
    members: tuple[str, ...]  # every hub behind it, not only the batch's


@dataclass(frozen=True, slots=True)
class AggregateFlow:
    """A feeder head, substation transformer or territory boundary: the guardian's own reading of its flow
    (import-positive, None = no reading) and its limits (None = unknown: any increase is vetoed).

    With a signed measurement `flow_kw` is exact. From an unsigned (kVA) reading the flow is an interval:
    `flow_kw` is its import-side end and `flow_low_kw` its export-side end, and both are checked."""

    ref: str
    flow_kw: float | None
    age_s: float
    lower_kw: float | None  # -R_rev
    upper_kw: float | None  # rho * rating
    banks: tuple[str, ...] = ()
    flow_low_kw: float | None = None  # export-side worst case; None = `flow_kw` is exact


@dataclass(frozen=True, slots=True)
class PoiLimit:
    """A substation asset's interconnection limit (og.asset, migration 0025), both directions."""

    asset_id: str
    import_kw: float
    export_kw: float


class GridTopologyPort(Protocol):
    """09 S2.6: static premise, transformer, feeder and substation data plus the guardian's own flow reads."""

    async def hub_site(self, hub_id: str) -> HubSite | None: ...

    async def hub_bank(self, hub_id: str) -> str | None:
        """The bank og.hub places the hub on; None for a hub the topology does not know (fail closed)."""
        ...

    async def transformer(self, transformer_id: str) -> ServiceTransformer | None: ...

    async def feeder_flow(self, feeder_id: str) -> AggregateFlow | None: ...

    async def substation_flow(self, bank_id: str) -> AggregateFlow | None:
        """The substation transformer the bank sits under; None when no substation is configured for it."""
        ...

    async def territory_flow(self, bank_id: str) -> AggregateFlow | None:
        """The regulated-territory boundary the bank sits inside (R_rev = 0, K15); None if competitive."""
        ...

    async def poi_limit(self, bank_id: str) -> PoiLimit | None:
        """A SUBSTATION asset dispatched as this bank; None for home banks."""
        ...


@dataclass(frozen=True, slots=True)
class ObligationMarket:
    """The obligation's contract market as the guardian reads it (og.contract.market / utility_id)."""

    market: str | None
    utility_id: str | None
    service_type: str | None


class TerritoryPort(Protocol):
    """K15/G-33 reads: the obligation's market and the zone -> territory table."""

    def zone_territory(self) -> Mapping[str, UtilityId]: ...

    async def hub_zone(self, hub_id: str) -> str | None:
        """The settlement zone of the hub's bank (og.bank.zone): its territory via `territory_of_zone`."""
        ...

    async def obligation_market(self, obligation_id: UUID) -> ObligationMarket | None: ...

    async def free_access(self, utility_id: str) -> bool: ...


@dataclass(frozen=True, slots=True)
class BankSnapshot:
    """Guardian's own view of one bank. `bank_load_kva` is its latest SCADA apparent-power reading and
    `bank_load_age_s` that reading's age (`inf` when there is none): G-03 treats a missing or stale
    reading as unknown loading and vetoes, never as an empty bank."""

    params: BankParams
    bank_load_kva: float
    feeder_id: str | None
    feeder_ceiling_kw_per_min: float | None
    bank_load_age_s: float = 0.0


class ClockPort(Protocol):
    async def offset_from_ntp_ms(self) -> float:
        """Guardian's own NTP-disciplined clock offset (K12/G-20), never a value the engine reports."""
        ...


class ProposalPort(Protocol):
    async def fetch(self, command_batch_id: UUID) -> ProposedBatch | None: ...


class TracePort(Protocol):
    async def exists_preimage(self, decision_ref: UUID) -> bool:
        """G-14/K10: has the batch's decision pre-image already been durably traced?"""
        ...

    async def append_verdict(self, batch_id: UUID, payload: dict[str, object]) -> None:
        """Trace the verdict itself (GUARDIAN_VERDICT, 02a S8.1) after signing/veto/timeout."""
        ...

    async def append_calibration_verdict(self, calibration_id: UUID, payload: dict[str, object]) -> None:
        """Trace a calibration-command verdict (signed or refused, S6.7/G-25). Raises on failure: a
        signed calibration command is only released once this record is durable (K10)."""
        ...

    async def append_stop_release_verdict(self, operator_action_id: UUID, payload: dict[str, object]) -> None:
        """Trace a stop-RELEASE verdict (signed or refused, K8). Raises on failure: signed RELEASE events
        are handed to og-safestop only from this durable record (K10)."""
        ...


class HubStatePort(Protocol):
    async def snapshot(self, hub_id: str) -> HubSnapshot | None: ...


class BankMembersPort(Protocol):
    async def member_snapshots(self, bank_id: str) -> list[HubSnapshot]:
        """Guardian's own telemetry snapshot of EVERY hub on `bank_id` (membership from `og.hub`
        configuration), used to re-derive the bank's deliverable capability independently of the
        engine (G-19's check of an `R-COMMIT-LOCK-INFEASIBLE`/`-L0`/`-L1` override)."""
        ...

    async def member_hub_ids(self, bank_id: str) -> list[str]:
        """The configured hub ids on `bank_id` (`og.hub`), for per-hub reads such as G-24's asset state in
        G-19's PQ-eligible capability evidence."""
        ...


class BankStatePort(Protocol):
    async def snapshot(self, bank_id: str) -> BankSnapshot | None: ...


class LedgerPort(Protocol):
    async def ledger_version(self) -> int: ...


@dataclass(frozen=True, slots=True)
class ActiveObligation:
    """One obligation with an ACTIVE commitment against `bank_id` for the current interval, read
    independently by guardian from `og.commitment`/`og.reservation` -- never derived from the batch's
    own item list (GUARD-01/K13: a batch that omits an obligation, or relabels it `obligation_id=None`,
    must not be able to hide it from G-19)."""

    obligation_id: UUID
    frozen_kw: Decimal


class CommitmentPort(Protocol):
    async def active_kw(self, obligation_id: UUID, cycle_id: str) -> Decimal:
        """K13: the frozen committed kw for this obligation's current interval, read fresh -- never the
        allocator's cached copy."""
        ...

    async def active_obligations_for_bank(self, bank_id: str, cycle_id: str) -> list[ActiveObligation]:
        """K13/GUARD-01: every obligation with an ACTIVE commitment for `bank_id`'s hubs during
        `cycle_id`'s interval, read independently of the proposed batch. G-19 evaluates EVERY entry this
        returns -- an obligation the batch's items never mention still counts, with new_kw=0 (VETO
        unless an allowed reason code covers it), closing the omission/relabelling bypass."""
        ...


class PriorGrantPort(Protocol):
    async def prior_granted_kw(self, obligation_id: UUID) -> Decimal | None:
        """The PRIOR cycle's actually-granted kw for this obligation (02a S6.2's `prior_grants`), or
        None if there is none yet (falls back to the frozen commitment as the floor)."""
        ...


class LeaseStatePort(Protocol):
    async def last_accepted(self, bank_id: str) -> tuple[int, int]:
        """(epoch, seq) last accepted for this bank_id, or (0, 0) if none yet."""
        ...


class AlertPort(Protocol):
    """Operator alerts through the single `og.alert` writer (`opengrid.health.queries`). Raise is a
    no-op while an alert with the same rule and `condition_key` is open (never an alert storm)."""

    async def raise_alert(
        self,
        rule: str,
        severity: Literal["warning", "critical"],
        summary: str,
        condition_key: str,
        detail: dict[str, object],
    ) -> None: ...

    async def clear_alert(self, rule: str, condition_key: str) -> None: ...

    async def open_condition_keys(self, rule: str) -> list[str]:
        """The `condition_key`s of every open alert of `rule` (escalation reconciliation)."""
        ...


class ScopePosturePort(Protocol):
    """ES06-S04: the per-scope posture og-guardian publishes (`og.scope_posture`) and the safe-stop REQUEST
    it hands to a person (an unconfirmed operator-action proposal). Never engages a stop."""

    async def set_posture(
        self,
        scope_kind: str,
        scope_ref: str,
        *,
        posture: str,
        veto_ratio: float,
        consecutive: int,
        stop_requested: bool,
    ) -> None: ...

    async def propose_safe_stop(self, scope_kind: str, scope_ref: str, reason: str) -> None: ...


class AsAwardPort(Protocol):
    """The guardian's own reads for an `R-GRANT-AS-HOLD` claim (og.obligation, og.as_deployment)."""

    async def service_type(self, obligation_id: UUID) -> str | None: ...

    async def deployment_active(self, obligation_id: UUID) -> bool:
        """An uncancelled og.as_deployment covering now, for this obligation or for every AS award."""
        ...


class MobileUnitPort(Protocol):
    """D-31 / G-35: which hubs are MOBILE_STORAGE units, and whether each is at its home station now."""

    def is_mobile(self, hub_or_bank_id: str) -> bool: ...

    async def at_home_station(self, hub_id: str) -> bool | None:
        """True at its home station, False away, None unknown (G-35 treats unknown as away)."""
        ...


class ManualTargetPort(Protocol):
    """The guardian's own read of live operator targets (`og.trace` MANUAL_TARGET events, not expired)."""

    async def manual_target_hubs(self, hub_ids: list[str]) -> set[str]:
        """Which of `hub_ids` a live (unexpired) MANUAL_TARGET currently covers."""
        ...


class ServiceProfilePort(Protocol):
    async def setpoint_source(self, obligation_id: UUID) -> str | None:
        """The obligation's current service profile `setpoint_source` (e.g. MEASURED_FEEDBACK for a
        need-basis closed-loop profile), read by the guardian from the DB; None if it has none."""
        ...


class L2InstructionPort(Protocol):
    async def active_instruction(self, bank_id: str) -> L2Instruction | None: ...


class SafeStopPort(Protocol):
    async def is_stopped(self, scope: SafeStopScope, scope_ref: str) -> bool:
        """Whether an ENGAGE stop_event with no matching RELEASE is in force for this scope/scope_ref,
        or for a containing scope (FLEET stops everything; ZONE stops its banks)."""
        ...


StopScopeKind = Literal["FLEET", "ZONE", "BANK"]


@dataclass(frozen=True, slots=True)
class ReleaseRequest:
    """A Tier-2 stop-release request as og-api durably recorded it (`og.operator_action`,
    action_kind SAFE_STOP_RELEASE, tier TIER2): who asked, who approved, when, for which scope, and the
    trace row og-api wrote for it. The guardian re-checks every field itself (K8)."""

    operator_action_id: UUID
    requested_by: str
    approved_by: str | None
    scope_kind: StopScopeKind
    scope_ref: str  # "FLEET" for fleet scope, as og.stop_event stores it
    reason: str
    requested_at: datetime
    approved_at: datetime | None
    trace_id: UUID | None


@dataclass(frozen=True, slots=True)
class EngagedStop:
    """One outstanding ENGAGE on a scope (no RELEASE recorded after it), from `og.stop_event`."""

    stop_id: UUID
    initiator_kind: str
    engaged_at: datetime


class StopReleasePort(Protocol):
    async def pending_requests(self, *, max_age_s: float) -> list[ReleaseRequest]:
        """Approved Tier-2 release requests the guardian has not yet decided (no verdict trace row)."""
        ...

    async def outstanding_engages(self, scope_kind: StopScopeKind, scope_ref: str) -> list[EngagedStop]:
        """Every ENGAGE on exactly this scope recorded after its last RELEASE."""
        ...

    async def banks_in_scope(self, scope_kind: StopScopeKind, scope_ref: str) -> list[str]:
        """The banks a scope covers (configuration: `og.bank`)."""
        ...


@dataclass(frozen=True, slots=True)
class PqPorts:
    """K14 inputs (G-21..G-25, 06-service-profiles-and-power-quality.md S5.3/S6.7), each the guardian's
    own independent read (`opengrid.guardian.pq_ports`)."""

    envelopes: PqEnvelopeStatePort
    measurements: PqMeasurementPort
    hub_assets: HubAssetStatePort
    calibration_history: CalibrationHistoryPort
    firmware_bounds: FirmwareCalibrationBoundsPort
    sensitive_grants: SensitiveGrantPort
    # None: no durable command ledger, so no calibration command is ever signed (fail closed).
    calibration_ledger: CalibrationLedgerPort | None = None


@dataclass(frozen=True, slots=True)
class GuardianPorts:
    """Bundles every port `GuardianService` needs. One object so `main.py` wires it once."""

    clock: ClockPort
    proposals: ProposalPort
    trace: TracePort
    hubs: HubStatePort
    banks: BankStatePort
    ledger: LedgerPort
    commitments: CommitmentPort
    prior_grants: PriorGrantPort
    leases: LeaseStatePort
    l2_instructions: L2InstructionPort
    safe_stop: SafeStopPort
    zones_by_bank: dict[str, str] = field(default_factory=dict)
    pq: PqPorts | None = None  # None: K14 checks not wired (tests that predate PQ)
    # None: no independent capability read, so a capability-based G-19 override is never verified (VETO).
    bank_members: BankMembersPort | None = None
    # None: the K8 stop-release path is not wired, so every release request is refused.
    stop_release: StopReleasePort | None = None
    # None: no profile read, so no need-basis (R-GRANT-CLOSED-LOOP) reduction is ever corroborated (VETO).
    service_profiles: ServiceProfilePort | None = None
    # None: no AS-award read, so no R-GRANT-AS-HOLD reduction is ever corroborated (VETO).
    as_awards: AsAwardPort | None = None
    # None: no operator alerts (e.g. ALR-CLOCK-QUALITY); decisions are unaffected.
    alerts: AlertPort | None = None
    # None: the 09 S2.6 flow-limit checks (G-26..G-32) are not wired (tests that predate them). Production
    # always wires it (`repo.build_pg_ports`).
    topology: GridTopologyPort | None = None
    # None: G-33 (K15 territory) is not wired. Production always wires it.
    territory: TerritoryPort | None = None
    # None: no manual-target read, so no R-OPERATOR-OVERRIDE reduction is ever corroborated (VETO), and
    # capability evidence counts operator-owned hubs as available.
    manual_targets: ManualTargetPort | None = None
    # None: no mobile-unit registry wired, so G-35 has nothing to check (no hub is known to be mobile).
    # Production always wires it (`guardian.main`).
    mobile_units: MobileUnitPort | None = None
