"""In-memory worlds and builders the K1-K13 property tests share.

Everything here drives public interfaces only: `GuardianService` over its ports, `TraceStore` over its
backend, and `ReservationLedger` over its backend. Hypothesis cannot drive `async def` tests, so each
property calls `run()` on a coroutine.
"""

from __future__ import annotations

import asyncio
import atexit
from collections.abc import Coroutine
from contextlib import AbstractAsyncContextManager, nullcontext
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from opengrid.core.crypto import generate_keypair
from opengrid.core.models.engine import CommandBatchRow, Verdict
from opengrid.core.physics import BankParams, HubParams
from opengrid.core.tracehash import ChainRecord
from opengrid.guardian.config import GuardianConfig
from opengrid.guardian.ports import (
    ActiveObligation,
    BankSnapshot,
    GuardianPorts,
    HubSnapshot,
    L2Instruction,
    ProposedBatch,
    ProposedItem,
)
from opengrid.guardian.service import GuardianService
from opengrid.ledger import CommitmentRecord, ReservationRecord

NOW = datetime(2026, 9, 26, 18, 0, 0, tzinfo=UTC)
BANK_ID = "bank-000"
HUB_ID = "hub-00001"
BASE_HUB = HubParams(e_kwh=39.2, r_kwh=7.84, p_kw=11.0)
BASE_BANK = BankParams(kva_rating=600.0, reserve_kva=5.0)
LEASE_S = 10.0
DEFAULT_SOC_KWH = 20.0
DEFAULT_SETPOINT_KW = 3.0


_RUNNER = asyncio.Runner()
atexit.register(_RUNNER.close)


def run[T](coro: Coroutine[Any, Any, T]) -> T:
    """Drive `coro` on one shared event loop; creating a loop per call can hang on Windows."""
    return _RUNNER.run(coro)


class _Hubs:
    def __init__(self, hub: HubSnapshot | None) -> None:
        self._hub = hub

    async def snapshot(self, hub_id: str) -> HubSnapshot | None:
        return self._hub


class _Banks:
    def __init__(self, bank: BankSnapshot) -> None:
        self._bank = bank

    async def snapshot(self, bank_id: str) -> BankSnapshot | None:
        return self._bank


class _LedgerView:
    def __init__(self, world: World) -> None:
        self._world = world

    async def ledger_version(self) -> int:
        return self._world.ledger_version


@dataclass
class World:
    """Guardian's independently-read state. One instance implements every scalar port."""

    proposal: ProposedBatch
    hub: HubSnapshot | None
    bank: BankSnapshot = field(
        default_factory=lambda: BankSnapshot(BASE_BANK, 10.0, feeder_id=None, feeder_ceiling_kw_per_min=None)
    )
    offset_ms: float = 0.0
    preimage_exists: bool = True
    trace_lookup: Any = None
    pre_image_id: UUID | None = field(default_factory=uuid4)
    ledger_version: int = 1
    active: list[ActiveObligation] = field(default_factory=list)
    prior: dict[UUID, Decimal] = field(default_factory=dict)
    last_lease: tuple[int, int] = (0, 0)
    l2: L2Instruction | None = None
    stopped: bool = False

    async def offset_from_ntp_ms(self) -> float:
        return self.offset_ms

    async def fetch(self, command_batch_id: UUID) -> ProposedBatch | None:
        return self.proposal

    async def exists_preimage(self, decision_ref: UUID) -> bool:
        if self.trace_lookup is not None:
            return await self.trace_lookup(decision_ref)
        return self.preimage_exists

    async def append_verdict(self, batch_id: UUID, payload: dict[str, object]) -> None:
        return None

    async def active_kw(self, obligation_id: UUID, cycle_id: str) -> Decimal:
        return next((o.frozen_kw for o in self.active if o.obligation_id == obligation_id), Decimal(0))

    async def active_obligations_for_bank(self, bank_id: str, cycle_id: str) -> list[ActiveObligation]:
        return list(self.active)

    async def prior_granted_kw(self, obligation_id: UUID) -> Decimal | None:
        return self.prior.get(obligation_id)

    async def last_accepted(self, bank_id: str) -> tuple[int, int]:
        return self.last_lease

    async def active_instruction(self, bank_id: str) -> L2Instruction | None:
        return self.l2

    async def is_stopped(self, scope: str, scope_ref: str) -> bool:
        return self.stopped and scope == "BANK"

    def ports(self) -> GuardianPorts:
        return GuardianPorts(
            clock=self,
            proposals=self,
            trace=self,
            hubs=_Hubs(self.hub),
            banks=_Banks(self.bank),
            ledger=_LedgerView(self),
            commitments=self,
            prior_grants=self,
            leases=self,
            l2_instructions=self,
            safe_stop=self,
        )


def make_hub(
    *, soc_kwh: float = DEFAULT_SOC_KWH, prev_p_kw: float = DEFAULT_SETPOINT_KW, params: HubParams = BASE_HUB
) -> HubSnapshot:
    return HubSnapshot(params=params, soc_kwh=soc_kwh, prev_p_kw=prev_p_kw, health="online")


def make_proposal(
    items: list[ProposedItem] | None = None,
    *,
    epoch: int = 1,
    seq: int = 1,
    lease_s: float = LEASE_S,
) -> ProposedBatch:
    default_item = ProposedItem(HUB_ID, DEFAULT_SETPOINT_KW, "SELECTOR")
    return ProposedBatch(
        command_batch_id=uuid4(),
        bank_id=BANK_ID,
        cycle_id="cycle-1",
        epoch=epoch,
        seq=seq,
        issued_at=NOW,
        expires_at=NOW + timedelta(seconds=lease_s),
        ledger_version=1,
        items=items if items is not None else [default_item],
    )


def passing_world(
    items: list[ProposedItem] | None = None,
    *,
    soc_kwh: float = DEFAULT_SOC_KWH,
    lease_s: float = LEASE_S,
) -> World:
    """A world in which every G-check passes; tests break exactly one thing at a time."""
    proposal = make_proposal(items, lease_s=lease_s)
    prev_p_kw = proposal.items[0].p_kw_setpoint
    return World(proposal=proposal, hub=make_hub(soc_kwh=soc_kwh, prev_p_kw=prev_p_kw))


def batch_row(proposal: ProposedBatch, pre_image_id: UUID | None) -> CommandBatchRow:
    return CommandBatchRow(
        command_batch_id=proposal.command_batch_id,
        cycle_id=proposal.cycle_id,
        ledger_version=proposal.ledger_version,
        submission_id=str(proposal.command_batch_id),
        command_count=len(proposal.items),
        merkle_root="deadbeef",
        trace_pre_image_id=pre_image_id,
    )


@dataclass
class Signer:
    seed: bytes
    public: bytes

    @staticmethod
    def new() -> Signer:
        seed, public = generate_keypair()
        return Signer(seed, public)


def evaluate(world: World, signer: Signer, *, config: GuardianConfig | None = None) -> Verdict:
    service = GuardianService(
        ports=world.ports(),
        config=config or GuardianConfig(key_path="", cycle_interval_s=2.0),
        signing_seed=signer.seed,
        now_fn=lambda: NOW,
    )
    return run(service.evaluate_and_sign(batch_row(world.proposal, world.pre_image_id)))


@dataclass
class _Row:
    seq: int
    decision_type: str
    event_class: str
    payload: dict[str, Any]
    prev_hash: str | None
    hash: str
    trace_id: UUID


@dataclass
class InMemoryTraceBackend:
    """Minimal `TraceBackend`: append-only rows per stream, prefix pruning, no I/O."""

    streams: dict[str, list[_Row]] = field(default_factory=dict)
    trace_ids: set[UUID] = field(default_factory=set)

    async def last_head(self, stream_id: str) -> tuple[int, str | None]:
        rows = self.streams.get(stream_id, [])
        return (rows[-1].seq, rows[-1].hash) if rows else (-1, None)

    async def insert_trace_row(self, **row: Any) -> None:
        self.streams.setdefault(row["stream_id"], []).append(
            _Row(
                row["seq"],
                row["decision_type"],
                row["event_class"],
                row["payload"],
                row["prev_hash"],
                row["record_hash"],
                row["trace_id"],
            )
        )
        self.trace_ids.add(row["trace_id"])

    async def exists_preimage(self, decision_ref: UUID) -> bool:
        return decision_ref in self.trace_ids

    async def fetch_range(self, stream_id: str, *, from_seq: int) -> list[ChainRecord]:
        return [
            ChainRecord(r.seq, r.decision_type, r.event_class, r.payload, r.prev_hash, r.hash)
            for r in self.streams.get(stream_id, [])
            if r.seq >= from_seq
        ]

    async def stream_ids(self) -> list[str]:
        return list(self.streams)

    async def insert_checkpoint(self, **_checkpoint: Any) -> None:
        return None

    async def prune_before(self, stream_id: str, *, keep_from_seq: int) -> int:
        rows = self.streams.get(stream_id, [])
        kept = [r for r in rows if r.seq >= keep_from_seq]
        self.streams[stream_id] = kept
        return len(rows) - len(kept)

    async def retention_days_for(self, event_class: str) -> int:
        return 400


@dataclass
class InMemoryLedgerBackend:
    """Minimal `LedgerBackend` for the K2 property tests."""

    rows: dict[UUID, ReservationRecord] = field(default_factory=dict)
    commitments: dict[UUID, CommitmentRecord] = field(default_factory=dict)
    _version: int = 0

    def write_guard(self) -> AbstractAsyncContextManager[None]:
        return nullcontext()

    async def next_version(self) -> int:
        self._version += 1
        return self._version

    async def current_version(self) -> int:
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

    async def insert_reservations(
        self, records: list[ReservationRecord], commitments: list[CommitmentRecord]
    ) -> None:
        for record in records:
            self.rows[record.reservation_id] = record
        for commitment in commitments:
            self.commitments[commitment.commitment_id] = commitment

    async def release_uncommitted(self, *, reason: str, version: int) -> int:
        committed = {c.obligation_id for c in self.commitments.values()}
        orphans = [r for r in self.rows.values() if r.is_active and r.obligation_id not in committed]
        for record in orphans:
            await self.mark_released(record.reservation_id, reason=reason, version=version)
        return len(orphans)

    async def mark_released(self, reservation_id: UUID, *, reason: str, version: int) -> None:
        self.rows[reservation_id] = replace(
            self.rows[reservation_id], released_at=NOW, release_reason=reason, ledger_version=version
        )


@dataclass
class FlatCapability:
    """A `CapabilityProvider` reporting the same kW for every bank and interval."""

    capability: Decimal

    async def capability_kw(self, bank_id: str, interval_start: datetime) -> Decimal:
        return self.capability
