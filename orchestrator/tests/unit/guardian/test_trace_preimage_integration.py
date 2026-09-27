"""Regression test for qa/merge-notes.md S17: "100% of guardian verdicts VETO on G-14
PROPOSAL_NOT_FOUND". Exercises the REAL `opengrid.trace.TraceStore` (over an in-memory backend, no
Postgres) end to end through `opengrid.engine.propose_batch_to_guardian`'s write path and
`opengrid.guardian.service.GuardianService`'s read path, proving the two now agree on one canonical
key (`trace_id` == `CommandBatchRow.trace_pre_image_id`) and that a valid batch is signed PASS.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from opengrid import engine
from opengrid.core.crypto import generate_keypair
from opengrid.core.models.engine import CommandBatchRow, Grant
from opengrid.core.physics import BankParams, HubParams
from opengrid.guardian.config import GuardianConfig
from opengrid.guardian.ports import BankSnapshot, GuardianPorts, HubSnapshot, ProposedBatch
from opengrid.guardian.repo import _trace_payload_to_proposal
from opengrid.guardian.service import GuardianService
from opengrid.trace.store import TraceStore

from .conftest import FakeClock, FakeL2Instructions, FakeLeases, FakeSafeStop

NOW = datetime(2026, 9, 26, 18, 0, 0, tzinfo=UTC)
BANK_ID = "bank-01"


@dataclass
class _TraceRow:
    trace_id: UUID
    seq: int
    payload: dict


@dataclass
class InMemoryTraceBackend:
    """Same minimal fake as `tests/unit/trace/test_store.py`'s `InMemoryBackend`: `exists_preimage`
    checks `trace_id` membership, exactly like `PgTraceBackend`'s real `SELECT ... WHERE trace_id = ...`
    -- the same lookup semantics that were mismatched against `command_batch_id` before this fix."""

    rows_by_stream: dict[str, list[_TraceRow]] = field(default_factory=dict)
    rows_by_trace_id: dict[UUID, _TraceRow] = field(default_factory=dict)

    async def last_head(self, stream_id: str) -> tuple[int, str | None]:
        rows = self.rows_by_stream.get(stream_id, [])
        return (rows[-1].seq, "h") if rows else (-1, None)

    async def insert_trace_row(self, *, trace_id, stream_id, seq, payload, **_kwargs) -> None:
        row = _TraceRow(trace_id, seq, payload)
        self.rows_by_stream.setdefault(stream_id, []).append(row)
        self.rows_by_trace_id[trace_id] = row

    async def exists_preimage(self, decision_ref: UUID) -> bool:
        return decision_ref in self.rows_by_trace_id

    async def fetch_range(self, stream_id, *, from_seq):
        raise NotImplementedError

    async def stream_ids(self):
        raise NotImplementedError

    async def insert_checkpoint(self, **_kwargs):
        raise NotImplementedError

    async def prune_before(self, stream_id, *, keep_from_seq):
        raise NotImplementedError

    async def retention_days_for(self, event_class):
        raise NotImplementedError

    def find_by_command_batch_id(self, command_batch_id: UUID) -> dict | None:
        """Stands in for `PgProposalPort`'s `payload ->> 'command_batch_id'` query."""
        for rows in self.rows_by_stream.values():
            for row in rows:
                if row.payload.get("command_batch_id") == str(command_batch_id):
                    return row.payload
        return None


@dataclass
class FakeEngineBackend:
    inserted: list[CommandBatchRow] = field(default_factory=list)
    notified: list[UUID] = field(default_factory=list)

    async def insert_command_batch(self, row: CommandBatchRow) -> None:
        self.inserted.append(row)

    async def notify_guardian(self, command_batch_id: UUID) -> None:
        self.notified.append(command_batch_id)


@dataclass
class FakeHubCap:
    hub_id: str
    bank_id: str
    free_discharge_kw: float
    health: str = "online"


class FakeFleetModule:
    def __init__(self, hubs: list[FakeHubCap]) -> None:
        self._hubs = hubs

    def hub_capabilities(self, bank_id: str) -> list[FakeHubCap]:
        return [h for h in self._hubs if h.bank_id == bank_id]


class ProposalPortFromTrace:
    """Guardian's `ProposalPort`, reading the SAME trace backend the engine wrote to -- mirrors
    `opengrid.guardian.repo.PgProposalPort` but against the in-memory backend instead of Postgres."""

    def __init__(self, backend: InMemoryTraceBackend) -> None:
        self._backend = backend

    async def fetch(self, command_batch_id: UUID) -> ProposedBatch | None:
        payload = self._backend.find_by_command_batch_id(command_batch_id)
        return None if payload is None else _trace_payload_to_proposal(payload)


class FakeHubs:
    def __init__(self, hubs: dict[str, HubSnapshot]) -> None:
        self._hubs = hubs

    async def snapshot(self, hub_id: str) -> HubSnapshot | None:
        return self._hubs.get(hub_id)


class FakeBanks:
    def __init__(self, banks: dict[str, BankSnapshot]) -> None:
        self._banks = banks

    async def snapshot(self, bank_id: str) -> BankSnapshot | None:
        return self._banks.get(bank_id)


class FakeLedgerPort:
    def __init__(self, version: int) -> None:
        self._version = version

    async def ledger_version(self) -> int:
        return self._version


class FakeCommitmentsPort:
    async def active_kw(self, obligation_id, cycle_id):
        return Decimal(0)

    async def active_obligations_for_bank(self, bank_id, cycle_id):
        return []  # no other committed obligations to protect -- G-19 has nothing to check here


class FakePriorGrantsPort:
    async def prior_granted_kw(self, obligation_id, bank_id=None, cycle_id=None):
        return None


class FakeTracePort:
    """Adapts `InMemoryTraceBackend` to guardian's narrow `TracePort` (exists_preimage only -- the
    verdict trace append is irrelevant to this test)."""

    def __init__(self, backend: InMemoryTraceBackend) -> None:
        self._backend = backend

    async def exists_preimage(self, decision_ref: UUID) -> bool:
        return await self._backend.exists_preimage(decision_ref)

    async def append_verdict(self, batch_id: UUID, payload: dict) -> None:
        return None


async def test_engine_written_preimage_is_found_and_batch_is_signed_pass() -> None:
    """End-to-end (no Postgres): engine writes the RT_ALLOCATION pre-image and command_batch row through
    the real TraceStore; guardian reads them back through its own ports and PASSES -- proving the
    engine -> guardian hand-off's key now agrees end to end (qa/merge-notes.md S17 regression test)."""
    trace_backend = InMemoryTraceBackend()
    trace_store = TraceStore(trace_backend)
    engine_backend = FakeEngineBackend()
    fleet_module = FakeFleetModule([FakeHubCap("hub-0001", BANK_ID, free_discharge_kw=10.0)])
    grant = Grant(
        grant_id=uuid4(),
        cycle_id="cycle-1",
        obligation_id=uuid4(),
        bank_id=BANK_ID,
        granted_kw=Decimal("10.0"),
        is_headroom=False,
        ledger_version=1,
    )

    batch_id = await engine.propose_batch_to_guardian(
        backend=engine_backend,
        trace=trace_store,
        fleet_module=fleet_module,
        cycle_id="cycle-1",
        bank_id=BANK_ID,
        grants=[grant],
        ledger_version=1,
        epoch=1,
        seq=1,
        now=NOW,
    )
    assert batch_id is not None
    assert engine_backend.notified == [batch_id]  # NOTIFY only after the insert above already ran
    inserted_row = engine_backend.inserted[0]
    assert inserted_row.trace_pre_image_id is not None
    # The pre-image is durably "committed" (present in the backend) strictly before the row referencing
    # it was even built -- `propose_batch_to_guardian` awaits `trace.append` first by construction.
    assert await trace_backend.exists_preimage(inserted_row.trace_pre_image_id)

    ports = GuardianPorts(
        clock=FakeClock(),
        proposals=ProposalPortFromTrace(trace_backend),
        trace=FakeTracePort(trace_backend),
        hubs=FakeHubs(
            {
                "hub-0001": HubSnapshot(
                    params=HubParams(e_kwh=39.2, r_kwh=7.84, p_kw=11.0),
                    soc_kwh=39.2,
                    prev_p_kw=-10.0,
                    health="online",
                )
            }
        ),
        banks=FakeBanks(
            {
                BANK_ID: BankSnapshot(
                    params=BankParams(kva_rating=500.0, reserve_kva=0.0),
                    bank_load_kva=0.0,
                    feeder_id=None,
                    feeder_ceiling_kw_per_min=None,
                )
            }
        ),
        ledger=FakeLedgerPort(1),
        commitments=FakeCommitmentsPort(),
        prior_grants=FakePriorGrantsPort(),
        leases=FakeLeases(),
        l2_instructions=FakeL2Instructions(),
        safe_stop=FakeSafeStop(),
    )
    seed, _public = generate_keypair()
    service = GuardianService(
        ports=ports,
        config=GuardianConfig(key_path="", cycle_interval_s=2.0),
        signing_seed=seed,
        now_fn=lambda: NOW,
    )
    batch_row = CommandBatchRow(
        command_batch_id=batch_id,
        cycle_id="cycle-1",
        ledger_version=1,
        submission_id="cycle-1:bank-01",
        command_count=1,
        merkle_root=inserted_row.merkle_root,
        trace_pre_image_id=inserted_row.trace_pre_image_id,
    )

    verdict = await service.evaluate_and_sign(batch_row)

    assert verdict.outcome == "PASS", verdict.vetoed_rule_ids
    assert verdict.signature is not None
