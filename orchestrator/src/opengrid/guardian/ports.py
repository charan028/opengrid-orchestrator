"""Guardian's independently-read input ports (02a S6.1: "on its own independently-read inputs").

The guardian must never trust the allocator's claim that a limit was respected -- it re-reads hub
telemetry, SCADA, the ledger version, commitments and prior grants itself and re-runs the same
`opengrid.core.limits`/`opengrid.core.timeutil` functions the allocator used at planning time. These
`Protocol`s are the seams: `opengrid.guardian.repo` implements them against Postgres/MQTT for the real
process; tests implement them with plain in-memory fakes so `GuardianService` (service.py) stays free of
any I/O import, per BUILD.md S5a "pure logic separated from I/O".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Literal, Protocol
from uuid import UUID

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
class HubSnapshot:
    """Guardian's own, independently-read view of one hub: its physical params and the last
    hub-reported telemetry (never the allocator's planning-time assumption)."""

    params: HubParams
    soc_kwh: float
    prev_p_kw: float
    health: Literal["online", "stale", "offline", "fault"]


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
