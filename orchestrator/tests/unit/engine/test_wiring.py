"""og-engine process-wiring unit tests: gate scheduling, the command-batch/guardian handoff, and the
guardian-hold degraded mode. Uses fakes for `EngineBackend` and for the sibling stub modules
(`selector`/`allocator`/`fleet`) per BUILD.md ("where one isn't ready, use a fake in tests only")."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from opengrid import engine
from opengrid.core.models.engine import CommandBatchRow, Grant

NOW = datetime(2026, 9, 26, 18, 0, 0, tzinfo=UTC)


@dataclass
class FakeTraceStore:
    """Stands in for `opengrid.trace.TraceStore`: records every append and hands back a fresh
    `trace_id` each time, exactly like the real store's `TraceRecordRef`."""

    appended: list[tuple[str, str, str, dict]] = field(default_factory=list)

    async def append(self, stream_id: str, decision_type: str, event_class: str, payload: dict):
        from opengrid.trace.store import TraceRecordRef

        self.appended.append((stream_id, decision_type, event_class, payload))
        return TraceRecordRef(trace_id=uuid4(), stream_id=stream_id, seq=len(self.appended), hash="h")


@dataclass
class FakeHubCap:
    hub_id: str
    bank_id: str
    free_discharge_kw: float
    health: str = "online"


class FakeFleetModule:
    """Stands in for the `opengrid.fleet` module's `hub_capabilities` -- only what
    `_distribute_hub_items` calls."""

    def __init__(self, hubs_by_bank: dict[str, list[FakeHubCap]] | None = None) -> None:
        self._hubs_by_bank = hubs_by_bank or {}

    def hub_capabilities(self, bank_id: str) -> list[FakeHubCap]:
        return self._hubs_by_bank.get(bank_id, [])


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


def test_admission_gate_runs_at_most_once_per_contract_per_slot() -> None:
    """Regression (live 2026-09-26): an OFFERED opportunity the gate did not select stays pending, so
    the same contract's ADMISSION gate (a full 24 h solve) re-ran on every 2 s tick, starving the
    allocator. Unselected offers wait for the next scheduled gate instead."""
    scheduler = engine.GateScheduler()
    contract = uuid4()
    t0 = datetime(2026, 9, 26, 12, 1, tzinfo=UTC)

    def _admissions(now: datetime) -> list[UUID | None]:
        triggers = scheduler.due_triggers(
            now, pending_admission_contract_ids=[contract], due_renomination_contract_ids=[]
        )
        return [t.contract_scope for t in triggers if t.gate_kind == "ADMISSION"]

    assert _admissions(t0) == [contract]
    assert _admissions(t0 + timedelta(seconds=2)) == []
    assert _admissions(t0 + timedelta(minutes=13)) == []
    assert _admissions(t0 + timedelta(minutes=14)) == [contract]  # next slot (12:15)


# --- command-batch build / guardian handoff -------------------------------------------------------


def test_build_command_batch_row_counts_only_non_headroom_grants() -> None:
    grants = [_grant(kw="10"), _grant(kw="5", headroom=True)]
    row = engine.build_command_batch_row(
        command_batch_id=uuid4(),
        trace_pre_image_id=uuid4(),
        cycle_id="c1",
        bank_id="b1",
        grants=grants,
        ledger_version=7,
    )
    assert row.command_count == 1
    assert row.cycle_id == "c1"
    assert row.ledger_version == 7
    assert row.submission_id == "c1:b1"
    assert len(row.merkle_root) == 64  # sha256 hex digest
    assert row.trace_pre_image_id is not None


def test_build_command_batch_row_is_deterministic_for_the_same_grants() -> None:
    grants = [_grant(kw="10")]
    row1 = engine.build_command_batch_row(
        command_batch_id=uuid4(),
        trace_pre_image_id=uuid4(),
        cycle_id="c1",
        bank_id="b1",
        grants=grants,
        ledger_version=1,
    )
    row2 = engine.build_command_batch_row(
        command_batch_id=uuid4(),
        trace_pre_image_id=uuid4(),
        cycle_id="c1",
        bank_id="b1",
        grants=grants,
        ledger_version=1,
    )
    assert row1.merkle_root == row2.merkle_root  # same content -> same hash (JCS canonicalization)


async def test_propose_batch_to_guardian_persists_and_notifies() -> None:
    backend = FakeEngineBackend()
    trace = FakeTraceStore()
    fleet_module = FakeFleetModule({"b1": [FakeHubCap("h1", "b1", 10.0)]})
    grants = [_grant(kw="10")]
    batch_id = await engine.propose_batch_to_guardian(
        backend=backend,
        trace=trace,
        fleet_module=fleet_module,
        cycle_id="c1",
        bank_id="b1",
        grants=grants,
        ledger_version=2,
        epoch=1,
        seq=1,
        now=NOW,
    )
    assert batch_id is not None
    assert backend.inserted_batches[0].command_batch_id == batch_id
    assert backend.notified == [batch_id]


async def test_propose_batch_to_guardian_skips_empty_grant_sets() -> None:
    backend = FakeEngineBackend()
    trace = FakeTraceStore()
    fleet_module = FakeFleetModule()
    batch_id = await engine.propose_batch_to_guardian(
        backend=backend,
        trace=trace,
        fleet_module=fleet_module,
        cycle_id="c1",
        bank_id="b1",
        grants=[],
        ledger_version=2,
        epoch=1,
        seq=1,
        now=NOW,
    )
    assert batch_id is None
    assert backend.inserted_batches == []
    assert backend.notified == []


async def test_propose_batch_to_guardian_writes_trace_preimage_before_insert_and_notify() -> None:
    """qa/merge-notes.md S17: the RT_ALLOCATION trace pre-image must be written, and its `trace_id`
    threaded through as `og.command_batch.trace_pre_image_id`, BEFORE the batch row is inserted and the
    guardian is notified (K10) -- this is the exact ordering whose absence caused every guardian verdict
    to VETO on G-14 `PROPOSAL_NOT_FOUND`."""
    backend = FakeEngineBackend()
    trace = FakeTraceStore()
    fleet_module = FakeFleetModule({"b1": [FakeHubCap("h1", "b1", 10.0)]})
    grants = [_grant(kw="10")]

    batch_id = await engine.propose_batch_to_guardian(
        backend=backend,
        trace=trace,
        fleet_module=fleet_module,
        cycle_id="c1",
        bank_id="b1",
        grants=grants,
        ledger_version=2,
        epoch=1,
        seq=1,
        now=NOW,
    )

    assert len(trace.appended) == 1
    stream_id, decision_type, event_class, payload = trace.appended[0]
    assert stream_id == "allocator-b1"
    assert decision_type == "RT_ALLOCATION"
    assert event_class == "RT_ALLOCATION"
    assert payload["command_batch_id"] == str(batch_id)
    assert payload["items"]  # hub-level items were distributed, not left empty
    assert payload["items"][0]["hub_id"] == "h1"
    assert payload["items"][0]["p_kw_setpoint"] < 0  # discharge, +charge/-discharge convention

    inserted = backend.inserted_batches[0]
    assert inserted.trace_pre_image_id is not None
    assert inserted.command_batch_id == batch_id


@dataclass
class RampingHubCap:
    hub_id: str
    bank_id: str
    free_discharge_kw: float
    p_kw: float | None
    ramp_kw_per_s: float
    health: str = "online"


def test_hub_setpoints_ramp_from_measured_power_but_keep_the_obligation_grant() -> None:
    """Regression (live 2026-09-26): full setpoints were proposed in one 2 s step from 0 kW, so G-04
    (hub ramp, 02a S6.1 firm Kc/3 per minute) vetoed every batch. Each hub item now moves at most one
    cycle's ramp (with a safety margin) from its measured power; the obligation's bank-level grant is
    unchanged, so G-19 still compares the full grant against the commitment."""
    fleet_module = FakeFleetModule(
        {"b1": [RampingHubCap("h1", "b1", 10.0, p_kw=0.0, ramp_kw_per_s=0.1)]}  # type: ignore[list-item]
    )
    (item,) = engine._distribute_hub_items(
        "b1", [_grant(kw="10")], fleet_module=fleet_module, cycle_interval_s=2.0
    )

    assert item["p_kw_setpoint"] == pytest.approx(-0.1 * 2.0 * engine.RAMP_SAFETY_FACTOR)
    assert float(item["obligation_granted_kw"]) == pytest.approx(10.0)  # the hub's share of the grant


def test_hub_setpoint_is_the_full_share_once_ramped_up() -> None:
    fleet_module = FakeFleetModule(
        {"b1": [RampingHubCap("h1", "b1", 10.0, p_kw=-9.9, ramp_kw_per_s=0.1)]}  # type: ignore[list-item]
    )
    (item,) = engine._distribute_hub_items(
        "b1", [_grant(kw="10")], fleet_module=fleet_module, cycle_interval_s=2.0
    )

    assert item["p_kw_setpoint"] == pytest.approx(-10.0)


@pytest.mark.parametrize(
    ("grant_reason", "item_reason"),
    [
        (None, "R-GRANT-COMMITTED"),
        ("R-COMMIT-LOCK-OVERRIDE-L0", "R-COMMIT-LOCK-OVERRIDE-L0"),
        ("R-COMMIT-LOCK-OVERRIDE-L1", "R-COMMIT-LOCK-OVERRIDE-L1"),
        ("R-COMMIT-LOCK-INFEASIBLE", "R-COMMIT-LOCK-INFEASIBLE"),
        ("R-GRANT-DIST-DEFERRAL-PI", "R-GRANT-COMMITTED"),  # not a K13 exception: unchanged
    ],
)
def test_a_reduced_grant_carries_its_k13_exception_onto_the_batch_items(grant_reason, item_reason) -> None:
    """K13/G-19: a grant the allocator reduced below its commitment (L0 fault, L1 reserve floor, L2,
    infeasible) must reach the guardian with that exception, or G-19 refuses the whole batch."""
    fleet_module = FakeFleetModule(
        {"b1": [RampingHubCap("h1", "b1", 10.0, p_kw=-10.0, ramp_kw_per_s=0.1)]}  # type: ignore[list-item]
    )
    grant = _grant(kw="6").model_copy(update={"reason_code": grant_reason})
    (item,) = engine._distribute_hub_items("b1", [grant], fleet_module=fleet_module, cycle_interval_s=2.0)

    assert item["reason_code"] == item_reason


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


async def test_pq_summaries_flush_on_their_own_cadence_and_a_failure_never_breaks_the_tick(
    monkeypatch,
) -> None:
    """Wave-2 wiring: buffered waveform summaries are written every [pq_ingest].flush_interval_s; a failed
    flush is logged and retried, never raised into the 2 s dispatch tick (K7)."""
    import types

    import opengrid.pq_ingest as pq_ingest
    from opengrid.platform.process import Cadence

    clock = {"now": 0.0}
    calls: list[int] = []

    async def _flush() -> int:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("db hiccup")
        return 3

    monkeypatch.setattr(pq_ingest, "flush_summaries", _flush)
    state = types.SimpleNamespace(pq_flush=Cadence(2.0, clock=lambda: clock["now"]))

    await engine._flush_pq_summaries(state)  # due, raises inside -> swallowed
    clock["now"] = 1.0
    await engine._flush_pq_summaries(state)  # not due
    clock["now"] = 2.0
    await engine._flush_pq_summaries(state)  # due again

    assert len(calls) == 2


async def test_gates_run_in_one_background_task_with_a_deduplicated_backlog() -> None:
    """A11 (live 2026-09-26): a 24 h gate held the 2 s dispatch tick for 10-20 s. Gates now run in one
    background task; triggers arriving meanwhile wait (once each) and start with the next task."""
    import asyncio
    import types

    release = asyncio.Event()
    batches: list[list] = []

    async def _run(batch):
        batches.append(batch)
        await release.wait()
        return 0

    state = types.SimpleNamespace(gate_task=None, gate_backlog=[])
    scheduled = engine.GateTrigger("SCHEDULED_15MIN")
    renom = engine.GateTrigger("RENOMINATION", uuid4())

    assert engine.start_gates_in_background(state, [scheduled], _run) is True
    await asyncio.sleep(0)
    assert engine.start_gates_in_background(state, [renom], _run) is False  # one gate task at a time
    assert engine.start_gates_in_background(state, [renom], _run) is False
    assert state.gate_backlog == [renom]  # de-duplicated

    release.set()
    await state.gate_task
    assert engine.start_gates_in_background(state, [], _run) is True
    await state.gate_task
    assert batches == [[scheduled], [renom]]
