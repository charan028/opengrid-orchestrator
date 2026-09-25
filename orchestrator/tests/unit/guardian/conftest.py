"""In-memory fakes for every `opengrid.guardian.ports` Protocol, plus builders for a default
"everything passes" proposal/batch pair that individual tests mutate to trigger one violation at a time.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from opengrid.core.crypto import generate_keypair
from opengrid.core.models.engine import CommandBatchRow
from opengrid.core.physics import BankParams, HubParams
from opengrid.guardian.config import GuardianConfig
from opengrid.guardian.ports import (
    BankSnapshot,
    GuardianPorts,
    HubSnapshot,
    L2Instruction,
    ProposedBatch,
    ProposedItem,
)
from opengrid.guardian.service import GuardianService

NOW = datetime(2026, 9, 26, 18, 0, 0, tzinfo=UTC)
HUB_ID = "hub-0001"
BANK_ID = "bank-01"


class FakeClock:
    def __init__(self, offset_ms: float = 0.0) -> None:
        self.offset_ms = offset_ms

    async def offset_from_ntp_ms(self) -> float:
        return self.offset_ms


class FakeProposals:
    def __init__(self) -> None:
        self.batches: dict[UUID, ProposedBatch] = {}

    def add(self, batch: ProposedBatch) -> None:
        self.batches[batch.command_batch_id] = batch

    async def fetch(self, command_batch_id: UUID) -> ProposedBatch | None:
        return self.batches.get(command_batch_id)


class FakeTrace:
    def __init__(self, *, preimage_exists: bool = True) -> None:
        self.preimage_exists = preimage_exists
        self.appended: list[tuple[UUID, dict]] = []

    async def exists_preimage(self, decision_ref: UUID) -> bool:
        return self.preimage_exists

    async def append_verdict(self, batch_id: UUID, payload: dict) -> None:
        self.appended.append((batch_id, payload))


class FailingTrace(FakeTrace):
    async def append_verdict(self, batch_id: UUID, payload: dict) -> None:
        raise RuntimeError("boom")


class FakeHubs:
    def __init__(self) -> None:
        self.hubs: dict[str, HubSnapshot] = {}

    async def snapshot(self, hub_id: str) -> HubSnapshot | None:
        return self.hubs.get(hub_id)


class FakeBanks:
    def __init__(self) -> None:
        self.banks: dict[str, BankSnapshot] = {}

    async def snapshot(self, bank_id: str) -> BankSnapshot | None:
        return self.banks.get(bank_id)


class FakeLedger:
    def __init__(self, version: int = 1) -> None:
        self.version = version

    async def ledger_version(self) -> int:
        return self.version


class FakeCommitments:
    def __init__(self) -> None:
        self.frozen: dict[UUID, Decimal] = {}

    async def active_kw(self, obligation_id: UUID, cycle_id: str) -> Decimal:
        return self.frozen.get(obligation_id, Decimal(0))


class FakePriorGrants:
    def __init__(self) -> None:
        self.prior: dict[UUID, Decimal] = {}

    async def prior_granted_kw(self, obligation_id: UUID) -> Decimal | None:
        return self.prior.get(obligation_id)


class FakeLeases:
    def __init__(self) -> None:
        self.last: dict[str, tuple[int, int]] = {}

    async def last_accepted(self, bank_id: str) -> tuple[int, int]:
        return self.last.get(bank_id, (0, 0))


class FakeL2Instructions:
    def __init__(self) -> None:
        self.active: dict[str, L2Instruction] = {}

    async def active_instruction(self, bank_id: str) -> L2Instruction | None:
        return self.active.get(bank_id)


class FakeSafeStop:
    def __init__(self) -> None:
        self.stopped: set[tuple[str, str]] = set()

    async def is_stopped(self, scope: str, scope_ref: str) -> bool:
        return (scope, scope_ref) in self.stopped


@dataclass
class Fakes:
    clock: FakeClock
    proposals: FakeProposals
    trace: FakeTrace
    hubs: FakeHubs
    banks: FakeBanks
    ledger: FakeLedger
    commitments: FakeCommitments
    prior_grants: FakePriorGrants
    leases: FakeLeases
    l2_instructions: FakeL2Instructions
    safe_stop: FakeSafeStop

    def as_ports(self) -> GuardianPorts:
        return GuardianPorts(
            clock=self.clock,
            proposals=self.proposals,
            trace=self.trace,
            hubs=self.hubs,
            banks=self.banks,
            ledger=self.ledger,
            commitments=self.commitments,
            prior_grants=self.prior_grants,
            leases=self.leases,
            l2_instructions=self.l2_instructions,
            safe_stop=self.safe_stop,
        )


@pytest.fixture
def fakes() -> Fakes:
    return Fakes(
        FakeClock(),
        FakeProposals(),
        FakeTrace(),
        FakeHubs(),
        FakeBanks(),
        FakeLedger(),
        FakeCommitments(),
        FakePriorGrants(),
        FakeLeases(),
        FakeL2Instructions(),
        FakeSafeStop(),
    )


@pytest.fixture
def guardian_config() -> GuardianConfig:
    return GuardianConfig(key_path="", cycle_interval_s=2.0)


@pytest.fixture
def signing_seed() -> bytes:
    seed, _public = generate_keypair()
    return seed


def make_hub_snapshot(*, soc_kwh: float = 20.0, prev_p_kw: float = 0.0, p_kw: float = 5.0) -> HubSnapshot:
    return HubSnapshot(
        params=HubParams(e_kwh=39.2, r_kwh=7.84, p_kw=p_kw),
        soc_kwh=soc_kwh,
        prev_p_kw=prev_p_kw,
        health="online",
    )


def make_bank_snapshot(*, bank_load_kva: float = 10.0, kva_rating: float = 75.0) -> BankSnapshot:
    return BankSnapshot(
        params=BankParams(kva_rating=kva_rating, reserve_kva=5.0),
        bank_load_kva=bank_load_kva,
        feeder_id=None,
        feeder_ceiling_kw_per_min=None,
    )


def make_proposal(
    *,
    command_batch_id: UUID | None = None,
    hub_id: str = HUB_ID,
    bank_id: str = BANK_ID,
    p_kw_setpoint: float = 3.0,
    epoch: int = 1,
    seq: int = 1,
    ledger_version: int = 1,
    obligation_id: UUID | None = None,
    obligation_granted_kw: Decimal | None = None,
    reason_code: str = "SELECTOR",
    is_firm_event: bool = False,
) -> ProposedBatch:
    return ProposedBatch(
        command_batch_id=command_batch_id or uuid4(),
        bank_id=bank_id,
        cycle_id="cycle-1",
        epoch=epoch,
        seq=seq,
        issued_at=NOW,
        expires_at=NOW + timedelta(seconds=10),
        ledger_version=ledger_version,
        items=[
            ProposedItem(
                hub_id=hub_id,
                p_kw_setpoint=p_kw_setpoint,
                reason_code=reason_code,
                obligation_id=obligation_id,
                obligation_granted_kw=obligation_granted_kw,
            )
        ],
        is_firm_event=is_firm_event,
    )


def make_batch_row(proposal: ProposedBatch, *, merkle_root: str = "deadbeef") -> CommandBatchRow:
    return CommandBatchRow(
        command_batch_id=proposal.command_batch_id,
        cycle_id=proposal.cycle_id,
        ledger_version=proposal.ledger_version,
        submission_id=str(proposal.command_batch_id),
        command_count=len(proposal.items),
        merkle_root=merkle_root,
        trace_pre_image_id=uuid4(),
    )


def wire_default_passing_scenario(fakes: Fakes, proposal: ProposedBatch) -> None:
    """Populates `fakes` so `proposal` (and its matching batch row) sails through every check."""
    fakes.proposals.add(proposal)
    fakes.ledger.version = proposal.ledger_version
    for item in proposal.items:
        fakes.hubs.hubs[item.hub_id] = make_hub_snapshot(prev_p_kw=item.p_kw_setpoint)
    fakes.banks.banks[proposal.bank_id] = make_bank_snapshot()
    fakes.leases.last[proposal.bank_id] = (proposal.epoch - 1, 0)


def service_with(
    fakes: Fakes, config: GuardianConfig, seed: bytes, *, now: datetime = NOW
) -> GuardianService:
    return GuardianService(ports=fakes.as_ports(), config=config, signing_seed=seed, now_fn=lambda: now)
