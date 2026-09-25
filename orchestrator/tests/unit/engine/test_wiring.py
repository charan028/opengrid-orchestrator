"""og-engine process-wiring unit tests: gate scheduling, the command-batch/guardian handoff, and the
guardian-hold degraded mode. Uses fakes for `EngineBackend` and for the sibling stub modules
(`selector`/`allocator`/`fleet`) per BUILD.md ("where one isn't ready, use a fake in tests only")."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

from opengrid import engine
from opengrid.core.models.engine import CommandBatchRow, Grant


@dataclass
class FakeEngineBackend:
    admission_ids: list[UUID] = field(default_factory=list)
    renomination_ids: list[UUID] = field(default_factory=list)
    heartbeat_ages: dict[str, float | None] = field(default_factory=dict)
    inserted_batches: list[CommandBatchRow] = field(default_factory=list)
    notified: list[UUID] = field(default_factory=list)

    async def pending_admission_contract_ids(self) -> list[UUID]:
        return self.admission_ids

    async def due_renomination_contract_ids(self, now: datetime) -> list[UUID]:
        return self.renomination_ids

    async def process_heartbeat_age_s(self, process: str, *, now: datetime | None = None) -> float | None:
        return self.heartbeat_ages.get(process)

    async def insert_command_batch(self, row: CommandBatchRow) -> None:
        self.inserted_batches.append(row)

    async def notify_guardian(self, command_batch_id: UUID) -> None:
        self.notified.append(command_batch_id)


def _grant(
    bank_id: str = "00000000-0000-7000-8000-000000000b01", *, kw: str = "10.0", headroom: bool = False
) -> Grant:
    return Grant(
        grant_id=uuid4(),
        cycle_id="c1",
        obligation_id=None if headroom else uuid4(),
        bank_id=bank_id,
        granted_kw=Decimal(kw),
        is_headroom=headroom,
        ledger_version=3,
    )


# --- GateScheduler ---------------------------------------------------------------------------------


def test_scheduled_gate_fires_once_per_15_minute_slot() -> None:
    scheduler = engine.GateScheduler()
    t0 = datetime(2026, 9, 26, 12, 0, 0, tzinfo=UTC)

    first = scheduler.due_triggers(t0, pending_admission_contract_ids=[], due_renomination_contract_ids=[])
    assert [t.gate_kind for t in first] == ["SCHEDULED_15MIN"]

    # same slot, a few seconds later -- no second fire
    again = scheduler.due_triggers(
        t0 + timedelta(seconds=5), pending_admission_contract_ids=[], due_renomination_contract_ids=[]
    )
    assert again == []

    # next slot -- fires again
    next_slot = scheduler.due_triggers(
        t0 + timedelta(minutes=15), pending_admission_contract_ids=[], due_renomination_contract_ids=[]
    )
    assert [t.gate_kind for t in next_slot] == ["SCHEDULED_15MIN"]


def test_admission_and_renomination_triggers_are_scoped_to_their_contract() -> None:
    scheduler = engine.GateScheduler()
    now = datetime(2026, 9, 26, 12, 0, 1, tzinfo=UTC)
    contract_a, contract_b = uuid4(), uuid4()

    triggers = scheduler.due_triggers(
        now, pending_admission_contract_ids=[contract_a], due_renomination_contract_ids=[contract_b]
    )
    kinds = {(t.gate_kind, t.contract_scope) for t in triggers}
    assert ("SCHEDULED_15MIN", None) in kinds
    assert ("ADMISSION", contract_a) in kinds
    assert ("RENOMINATION", contract_b) in kinds


def test_multiple_pending_admissions_each_get_their_own_trigger() -> None:
    scheduler = engine.GateScheduler()
    now = datetime(2026, 9, 26, 12, 5, 0, tzinfo=UTC)
    ids = [uuid4(), uuid4(), uuid4()]
    triggers = scheduler.due_triggers(
        now, pending_admission_contract_ids=ids, due_renomination_contract_ids=[]
    )
    admission_scopes = [t.contract_scope for t in triggers if t.gate_kind == "ADMISSION"]
    assert admission_scopes == ids


# --- command-batch build / guardian handoff -------------------------------------------------------


def test_build_command_batch_row_counts_only_non_headroom_grants() -> None:
    grants = [_grant(kw="10"), _grant(kw="5", headroom=True)]
    row = engine.build_command_batch_row(cycle_id="c1", bank_id="b1", grants=grants, ledger_version=7)
    assert row.command_count == 1
    assert row.cycle_id == "c1"
    assert row.ledger_version == 7
    assert row.submission_id == "c1:b1"
    assert len(row.merkle_root) == 64  # sha256 hex digest


def test_build_command_batch_row_is_deterministic_for_the_same_grants() -> None:
    grants = [_grant(kw="10")]
    row1 = engine.build_command_batch_row(cycle_id="c1", bank_id="b1", grants=grants, ledger_version=1)
    row2 = engine.build_command_batch_row(cycle_id="c1", bank_id="b1", grants=grants, ledger_version=1)
    assert row1.merkle_root == row2.merkle_root  # same content -> same hash (JCS canonicalization)


async def test_propose_batch_to_guardian_persists_and_notifies() -> None:
    backend = FakeEngineBackend()
    grants = [_grant(kw="10")]
    batch_id = await engine.propose_batch_to_guardian(
        backend=backend, cycle_id="c1", bank_id="b1", grants=grants, ledger_version=2
    )
    assert batch_id is not None
    assert backend.inserted_batches[0].command_batch_id == batch_id
    assert backend.notified == [batch_id]


async def test_propose_batch_to_guardian_skips_empty_grant_sets() -> None:
    backend = FakeEngineBackend()
    batch_id = await engine.propose_batch_to_guardian(
        backend=backend, cycle_id="c1", bank_id="b1", grants=[], ledger_version=2
    )
    assert batch_id is None
    assert backend.inserted_batches == []
    assert backend.notified == []


# --- guardian-hold degraded mode (02b S6.5) --------------------------------------------------------


async def test_guardian_available_when_heartbeat_is_fresh() -> None:
    backend = FakeEngineBackend(heartbeat_ages={"guardian": 1.0})
    assert await engine.guardian_is_available(backend, miss_threshold_s=15.0)


async def test_guardian_unavailable_when_heartbeat_is_stale() -> None:
    backend = FakeEngineBackend(heartbeat_ages={"guardian": 20.0})
    assert not await engine.guardian_is_available(backend, miss_threshold_s=15.0)


async def test_guardian_unavailable_when_never_seen() -> None:
    backend = FakeEngineBackend(heartbeat_ages={})
    assert not await engine.guardian_is_available(backend, miss_threshold_s=15.0)
