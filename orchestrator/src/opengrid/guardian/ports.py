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
    health: Literal["online", "stale", "fault"]


@dataclass(frozen=True, slots=True)
class BankSnapshot:
    params: BankParams
    bank_load_kva: float
    feeder_id: str | None
    feeder_ceiling_kw_per_min: float | None


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


class HubStatePort(Protocol):
    async def snapshot(self, hub_id: str) -> HubSnapshot | None: ...


class BankStatePort(Protocol):
    async def snapshot(self, bank_id: str) -> BankSnapshot | None: ...


class LedgerPort(Protocol):
    async def ledger_version(self) -> int: ...


class CommitmentPort(Protocol):
    async def active_kw(self, obligation_id: UUID, cycle_id: str) -> Decimal:
        """K13: the frozen committed kw for this obligation's current interval, read fresh -- never the
        allocator's cached copy."""
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


class L2InstructionPort(Protocol):
    async def active_instruction(self, bank_id: str) -> L2Instruction | None: ...


class SafeStopPort(Protocol):
    async def is_stopped(self, scope: SafeStopScope, scope_ref: str) -> bool:
        """Whether an ENGAGE stop_event with no matching RELEASE is in force for this scope/scope_ref,
        or for a containing scope (FLEET stops everything; ZONE stops its banks)."""
        ...


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
