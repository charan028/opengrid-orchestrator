"""opengrid.engine -- process og-engine wiring (02b S1.2): hosts `fleet` (read side), `contracts`,
`selector`, `ledger`, `allocator`, and the `trace` writer for this process's streams. Owner: engine
agent (BUILD.md S4: "engine = fleet twin + og-engine process wiring").

`opengrid.engine.main` composes the other modules' PUBLIC interfaces only -- it holds no independent
business logic of its own beyond scheduling (when to call what) and the engine -> guardian handoff.
I/O (Postgres reads for scheduling triggers, the command-batch queue write/NOTIFY) is isolated behind
the `EngineBackend` protocol so the scheduling logic is unit-testable with fakes (mirrors
`opengrid.fleet`'s `FleetBackend` split, BUILD.md S5a "pure logic separated from I/O"); the real
implementation is `opengrid.engine.pg_backend.PgEngineBackend`.

Engine -> guardian handoff (INTERFACES.md: "e.g. DB queue/NOTIFY or an internal socket"). This build
uses **Postgres as the queue**: engine inserts one `og.command_batch` row per bank per cycle that has a
non-empty grant set, then issues `NOTIFY og_command_batch` with the new row's id as payload. `og-guardian`
(a separate process, its own signing key) is expected to `LISTEN og_command_batch` (or poll the table) and
run `guardian.evaluate_and_sign` against it -- this module never imports `opengrid.guardian` or calls it
directly, matching the K3/K8 process-separation requirement (02b S1.2-1.3). Building the actual per-hub
`CommandItem` list and Ed25519-signing it is guardian's/allocator's concern reading finer-grained state;
this module's `command_batch` row carries the cycle-level summary (`merkle_root` over the grant set,
`command_count`) that lets guardian and the pre-image trace row agree on exactly what was proposed (K10).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal, Protocol
from uuid import UUID, uuid4

import aiomqtt
from psycopg_pool import AsyncConnectionPool

from opengrid.core.crypto import sha256_hex_of_json
from opengrid.core.models.engine import CommandBatchRow, Grant
from opengrid.core.physics import apply_ramp_limit
from opengrid.core.timeutil import floor_to_interval
from opengrid.engine.background import BackgroundIngest, run_periodic
from opengrid.engine.escalation import ShortfallEscalator, merge_signals
from opengrid.engine.gates import run_due_gates
from opengrid.engine.latency import CycleLatencyWindow, LoopLagProbe, PhaseTimer
from opengrid.engine.lifecycle import LifecycleBackend, advance_obligations
from opengrid.health.queries import raise_alert
from opengrid.platform.config import Config
from opengrid.platform.heartbeat import write_heartbeat
from opengrid.platform.process import Cadence, run_forever

if TYPE_CHECKING:
    from opengrid.allocator.energy_sufficiency import EnergySufficiencyResult
    from opengrid.allocator.gateways import FleetGateway, LedgerGateway, ScadaGateway, ScheduleGateway
    from opengrid.engine.gateways import EnergySufficiencyGateway
    from opengrid.trace import TraceStore

logger = logging.getLogger(__name__)

PROCESS_NAME = "engine"
GATE_INTERVAL_MINUTES = 15
GUARDIAN_PROCESS_NAME = "guardian"
# K7: how long a signed command batch's lease holds (00-invariants.md K7's "30 s during events, 60 s
# otherwise" -- the conservative default, matching opengrid.allocator.cycle's own lease-horizon default).
DEFAULT_LEASE_TTL_S = 30.0


class EngineBackend(Protocol):
    """Storage contract the engine's scheduler/handoff needs. A real backend reads/writes Postgres
    (`opengrid.engine.pg_backend.PgEngineBackend`); wiring tests use an in-memory fake."""

    async def pending_admission_contract_ids(self) -> list[UUID]:
        """Contracts with an `OFFERED` opportunity not yet attached to any gate's plan (02a S3.1's
        "admission" trigger)."""
        ...

    async def due_renomination_contract_ids(self, now: datetime) -> list[UUID]:
        """Contracts with a `renomination_point.scheduled_at <= now` not yet exercised."""
        ...

    async def process_heartbeat_age_s(self, process: str, *, now: datetime | None = None) -> float | None:
        """Seconds since `process`'s last heartbeat, or `None` if it has never reported one."""
        ...

    async def next_epoch(self) -> int:
        """K6: an epoch strictly greater than any the guardian has accepted (`og.lease_state`)."""
        ...

    async def insert_command_batch(self, row: CommandBatchRow) -> None: ...

    async def notify_guardian(self, command_batch_id: UUID) -> None:
        """Wake `og-guardian` (Postgres `NOTIFY`, per this module's docstring)."""
        ...


GateKind = Literal["SCHEDULED_15MIN", "ADMISSION", "RENOMINATION"]


@dataclass(frozen=True, slots=True)
class GateTrigger:
    gate_kind: GateKind
    contract_scope: UUID | None = None


class GateScheduler:
    """Pure scheduling decision logic for 02a S3.1's three gate triggers -- no I/O of its own; callers
    supply the "is something due" facts and this class decides what to run this tick. Kept separate from
    `EngineBackend` reads so the wall-clock-alignment logic (the tricky part to get right) is unit
    testable without any fake backend at all.
    """

    def __init__(self) -> None:
        self._last_scheduled_slot: datetime | None = None
        # contract_id -> slot of its last ADMISSION gate: an offer the gate did not select stays
        # pending, and re-solving it every 2 s tick would starve the allocator.
        self._admitted_slot: dict[UUID, datetime] = {}

    def due_triggers(
        self,
        now: datetime,
        *,
        pending_admission_contract_ids: list[UUID],
        due_renomination_contract_ids: list[UUID],
    ) -> list[GateTrigger]:
        triggers: list[GateTrigger] = []
        current_slot = floor_to_interval(now, GATE_INTERVAL_MINUTES)
        if current_slot != self._last_scheduled_slot:
            self._last_scheduled_slot = current_slot
            triggers.append(GateTrigger("SCHEDULED_15MIN"))
        for cid in pending_admission_contract_ids:
            if self._admitted_slot.get(cid) != current_slot:
                self._admitted_slot[cid] = current_slot
                triggers.append(GateTrigger("ADMISSION", cid))
        triggers.extend(GateTrigger("RENOMINATION", cid) for cid in due_renomination_contract_ids)
        return triggers


def build_command_batch_row(
    *,
    command_batch_id: UUID,
    trace_pre_image_id: UUID,
    cycle_id: str,
    bank_id: str,
    grants: list[Grant],
    ledger_version: int,
) -> CommandBatchRow:
    """S8 command build (02a S5.1/S6): summarize one bank's grants for this cycle into the
    `og.command_batch` row engine hands to guardian. `merkle_root` here is a single SHA-256 over the
    JCS-canonical grant list (a placeholder single-leaf "tree" -- MVP-S has no need for inclusion
    proofs). `trace_pre_image_id` is REQUIRED and must be the `trace_id` of an ALREADY-COMMITTED
    `RT_ALLOCATION` trace row (K10: "no command is signed unless its decision pre-image is durably
    written to the trace") -- callers get it from `TraceStore.append()`'s return value, never invent
    their own (qa/merge-notes.md S17: a `None`/mismatched id here is why every guardian verdict was
    VETOED on G-14 before this fix)."""
    payload = [
        {
            "obligation_id": str(g.obligation_id) if g.obligation_id else None,
            "bank_id": str(g.bank_id),
            "granted_kw": str(g.granted_kw),
            "is_headroom": g.is_headroom,
        }
        for g in grants
    ]
    return CommandBatchRow(
        command_batch_id=command_batch_id,
        cycle_id=cycle_id,
        ledger_version=ledger_version,
        submission_id=f"{cycle_id}:{bank_id}",
        command_count=sum(1 for g in grants if not g.is_headroom),
        merkle_root=sha256_hex_of_json(payload),
        trace_pre_image_id=trace_pre_image_id,
    )


#: Fraction of the hub's G-04 ramp bound the engine uses per cycle, so a setpoint built from the twin's
#: telemetry stays inside the guardian's bound computed from its own (slightly different) telemetry.
RAMP_SAFETY_FACTOR = 0.9


def _ramped_setpoint_kw(hub: Any, target_kw: float, cycle_interval_s: float | None) -> float:
    """K4/G-04: move from the hub's measured power toward `target_kw` by at most one cycle's ramp. The
    obligation's grant is unchanged (G-19 compares the grant); only the per-hub command ramps."""
    prev_kw = getattr(hub, "p_kw", None)
    ramp_kw_per_s = getattr(hub, "ramp_kw_per_s", 0.0)
    if cycle_interval_s is None or prev_kw is None or ramp_kw_per_s <= 0:
        return target_kw
    return apply_ramp_limit(prev_kw, target_kw, cycle_interval_s, ramp_kw_per_s * RAMP_SAFETY_FACTOR)


def _distribute_hub_items(
    bank_id: str, grants: list[Grant], *, fleet_module: Any, cycle_interval_s: float | None = None
) -> list[dict[str, object]]:
    """S8 command build (02a S1.10: "per-hub detail ... derivable from the command log referenced by
    command_batch_id"): distribute each bank-level `Grant`'s kW across the bank's currently-online hubs,
    proportional to each hub's `free_discharge_kw` share -- the per-hub `ProposedItem` list guardian's
    G-01/G-01-ENERGY/G-02/G-04 checks need, since `og.grant` itself is bank-aggregate only (02a S1.10).
    Every MVP-S grant (committed delivery or price-responsive spot export) is a DISCHARGE amount, so
    `p_kw_setpoint` is the negative of the hub's share (opengrid.core.physics' +charge/-discharge
    convention). A bank with no online/free hubs this cycle contributes no items -- the guardian's
    per-item checks then have nothing to (dis)approve, matching K7 degrade-don't-trip.
    """
    hubs = [
        h for h in fleet_module.hub_capabilities(bank_id) if h.health == "online" and h.free_discharge_kw > 0
    ]
    total_free_kw = sum(h.free_discharge_kw for h in hubs)
    items: list[dict[str, object]] = []
    if not hubs or total_free_kw <= 0:
        return items
    for grant in grants:
        granted_kw = float(grant.granted_kw)
        if granted_kw <= 0:
            continue
        reason_code = "R-GRANT-HEADROOM" if grant.is_headroom else "R-GRANT-COMMITTED"
        for hub in hubs:
            share_kw = granted_kw * (hub.free_discharge_kw / total_free_kw)
            if share_kw <= 1e-9:
                continue
            items.append(
                {
                    "hub_id": hub.hub_id,
                    "p_kw_setpoint": _ramped_setpoint_kw(hub, -share_kw, cycle_interval_s),
                    "reason_code": reason_code,
                    "obligation_id": str(grant.obligation_id) if grant.obligation_id else None,
                    # This hub's share of the grant: guardian G-19 sums the shares per obligation.
                    # (The whole grant on every item claimed it once per hub -- 50x on a 50-hub bank.)
                    "obligation_granted_kw": str(share_kw) if grant.obligation_id else None,
                }
            )
    return items


async def propose_batch_to_guardian(
    *,
    backend: EngineBackend,
    trace: TraceStore,
    fleet_module: Any,
    cycle_id: str,
    bank_id: str,
    grants: list[Grant],
    ledger_version: int,
    epoch: int,
    seq: int,
    now: datetime,
    lease_ttl_s: float = DEFAULT_LEASE_TTL_S,
    cycle_interval_s: float | None = None,
) -> UUID | None:
    """Build this bank's command-batch summary, durably write its `RT_ALLOCATION` decision pre-image to
    the trace FIRST (K10), then persist `og.command_batch` (carrying that SAME trace row's id as
    `trace_pre_image_id`) and only THEN notify `og-guardian` (see module docstring) -- in that order, so
    the guardian can never be woken for a batch whose pre-image is not yet committed (qa/merge-notes.md
    S17's incident: previously no `RT_ALLOCATION` trace row was written at all, `trace_pre_image_id` was
    always `None`, and every verdict VETOed on G-14 `PROPOSAL_NOT_FOUND`). Returns the new
    `command_batch_id`, or `None` if there is nothing to propose (empty grant set -- nothing changed
    this cycle, no reason to wake the guardian)."""
    if not grants:
        return None

    command_batch_id = uuid4()
    issued_at = now
    expires_at = now + timedelta(seconds=lease_ttl_s)
    items = _distribute_hub_items(
        bank_id, grants, fleet_module=fleet_module, cycle_interval_s=cycle_interval_s
    )
    trace_payload = {
        "command_batch_id": str(command_batch_id),
        "bank_id": bank_id,
        "cycle_id": cycle_id,
        "epoch": epoch,
        "seq": seq,
        "issued_at": issued_at.isoformat(),
        "expires_at": expires_at.isoformat(),
        "ledger_version": ledger_version,
        "items": items,
        "is_firm_event": False,
    }
    trace_ref = await trace.append(f"allocator-{bank_id}", "RT_ALLOCATION", "RT_ALLOCATION", trace_payload)

    row = build_command_batch_row(
        command_batch_id=command_batch_id,
        trace_pre_image_id=trace_ref.trace_id,
        cycle_id=cycle_id,
        bank_id=bank_id,
        grants=grants,
        ledger_version=ledger_version,
    )
    await backend.insert_command_batch(row)
    await backend.notify_guardian(row.command_batch_id)
    return row.command_batch_id


async def guardian_is_available(
    backend: EngineBackend, *, now: datetime | None = None, miss_threshold_s: float
) -> bool:
    """Degraded mode (02b S6.5): "guardian process down / verdict timeout -> hold". Engine skips
    proposing new batches when the guardian's heartbeat is missing or older than the configured
    miss threshold, rather than piling up unread `command_batch` rows no one will ever sign."""
    age_s = await backend.process_heartbeat_age_s(GUARDIAN_PROCESS_NAME, now=now)
    if age_s is None:
        return False
    return age_s <= miss_threshold_s


@dataclass(slots=True)
class _EngineState:
    cfg: Config
    backend: EngineBackend
    trace: TraceStore
    gate_scheduler: GateScheduler
    cycle_interval_s: float
    heartbeat_interval_s: float
    heartbeat_miss_threshold: int
    telemetry_interval_s: float
    heartbeat_pool: AsyncConnectionPool
    fleet_gateway: FleetGateway
    ledger_gateway: LedgerGateway
    scada_gateway: ScadaGateway
    schedule_gateway: ScheduleGateway
    energy_sufficiency_gateway: EnergySufficiencyGateway | None = None
    lifecycle_backend: LifecycleBackend | None = None
    lease_ttl_s: float = DEFAULT_LEASE_TTL_S
    cycle_seq: int = 0
    # K6: one epoch per engine process, strictly above every (epoch, seq) the guardian has durably
    # accepted (`EngineBackend.next_epoch`); `seq` (per tick) orders batches within it. A fixed epoch
    # restarted `seq` at 1 below the guardian's high-water mark, so every batch after an engine restart
    # was VETOED on G-13 (live 2026-09-26).
    epoch: int = 1
    latency: CycleLatencyWindow = field(default_factory=CycleLatencyWindow)
    phase_timer: PhaseTimer = field(default_factory=PhaseTimer)
    escalator: ShortfallEscalator = field(default_factory=ShortfallEscalator)
    pq_flush: Cadence | None = None
    gate_task: asyncio.Task[int] | None = None
    gate_backlog: list[GateTrigger] = field(
        default_factory=list
    )  # `[pq_ingest].flush_interval_s`; None when waveform ingest is off


async def timed_tick(state: _EngineState) -> None:
    """One engine tick, timed into `state.latency` (A11) whether it succeeds or raises; publishes the
    window's p50/p99/max as an `RT_ALLOCATION` / `CYCLE_LATENCY` trace event when a report is due."""
    started = time.monotonic()
    state.phase_timer = PhaseTimer()
    try:
        await _engine_tick(state)
    finally:
        finished = time.monotonic()
        state.latency.record((finished - started) * 1000.0, state.phase_timer.phases)
        if state.latency.report_due(finished):
            summary = state.latency.summary()
            logger.info("engine cycle latency", extra=summary)
            try:
                await state.trace.append("engine-cycle-latency", "RT_ALLOCATION", "CYCLE_LATENCY", summary)
            except Exception:
                logger.exception("failed to trace engine cycle latency")


async def escalate_sustained_shortfalls(
    state: _EngineState, energy_results: list[EnergySufficiencyResult], transition: Any
) -> list[tuple[str, str]]:
    """02a S2.1 mid-window `DELIVERING -> SHORTFALL` for a sustained allocator shortfall (L2 instruction,
    no substitute) or energy infeasibility after substitution (`opengrid.engine.escalation`). An
    obligation not yet delivering is skipped (the state machine refuses the edge). Returns what escalated."""
    allocator_shortfalls = getattr(state.ledger_gateway, "last_shortfalls", [])
    infeasible = [r.obligation_id for r in energy_results if r.at_risk and r.margin_kwh < 0]
    escalated: list[tuple[str, str]] = []
    for obligation_id, reason in state.escalator.observe(merge_signals(allocator_shortfalls, infeasible)):
        try:
            await transition(
                UUID(obligation_id),
                "SHORTFALL",
                reason_code=reason,
                payload={"escalation": "sustained", "sustain_cycles": state.escalator.sustain_cycles},
            )
        except Exception:
            logger.info("shortfall escalation not applicable", extra={"obligation_id": obligation_id})
            continue
        logger.warning(
            "obligation escalated to SHORTFALL", extra={"obligation_id": obligation_id, "reason_code": reason}
        )
        escalated.append((obligation_id, reason))
    return escalated


async def exercise_due_renomination_points(
    contract_id: UUID, plan_id: UUID | None, now: datetime
) -> list[str]:
    """After a RENOMINATION gate for `contract_id`: exercise each of its due points (02a S1.7/S2.1). A
    point whose obligation is DELIVERING is `RESELECTED` (the `R-RENOM-GATE` self-loop, traced); any other
    point (obligation not delivering, or none) is `CONFIRMED` -- the gate ran, nothing changed. Returns
    the outcomes."""
    from opengrid import contracts

    outcomes: list[str] = []
    for point in await contracts.due_renomination_points(as_of=now):
        if point.contract_id != contract_id:
            continue
        outcome = "RESELECTED" if point.obligation_id is not None else "CONFIRMED"
        try:
            await contracts.exercise_renomination_point(point.renomination_point_id, outcome, plan_id=plan_id)
        except contracts.IllegalTransitionError:
            outcome = "CONFIRMED"  # obligation not DELIVERING (yet / any more)
            await contracts.exercise_renomination_point(point.renomination_point_id, outcome, plan_id=plan_id)
        outcomes.append(outcome)
    return outcomes


def start_gates_in_background(
    state: Any, triggers: list[GateTrigger], run: Callable[[list[GateTrigger]], Coroutine[Any, Any, int]]
) -> bool:
    """Gates run as ONE background task, never awaited by the 2 s tick: a 24 h gate (capability load,
    intake, LP) took 10-20 s and held dispatch for committed obligations that long (A11 p99, live
    2026-09-26). Triggers arriving while a gate task runs wait in a de-duplicated backlog. Returns True
    if a gate task was started now."""
    for trigger in triggers:
        if trigger not in state.gate_backlog:
            state.gate_backlog.append(trigger)
    if not state.gate_backlog or (state.gate_task is not None and not state.gate_task.done()):
        return False
    batch, state.gate_backlog = state.gate_backlog, []
    state.gate_task = asyncio.create_task(run(batch))
    return True


async def _flush_pq_summaries(state: _EngineState) -> None:
    """Write buffered waveform summaries on `[pq_ingest].flush_interval_s` (one batched insert). A failed
    flush is logged and retried next time; it never costs the dispatch tick (K7)."""
    from opengrid import pq_ingest

    if state.pq_flush is None or not state.pq_flush.due():
        return
    try:
        await pq_ingest.flush_summaries()
    except Exception:
        logger.exception("pq_ingest flush failed; retrying next interval")


async def persist_fleet_state(state: Any) -> None:
    """One fleet-persistence pass, run by `run_periodic` beside (never inside) the dispatch tick: flush
    the twin's buffered telemetry/SCADA/acks and hub_state rows, then the buffered PQ summaries. A failed
    fleet flush is logged and the summaries are still written (K7)."""
    from opengrid import fleet

    try:
        await fleet.flush()
    except Exception:
        logger.exception("fleet flush failed; retrying next interval")
    await _flush_pq_summaries(state)


async def _engine_tick(state: _EngineState) -> None:
    """One driving tick of the og-engine process, run every `[allocator].cycle_interval_s` (default
    2 s). A single tick drives every cadence this process owns (allocator cycle, gate scheduling,
    fleet maintenance, heartbeat) off wall-clock checks rather than several independent
    `run_forever` loops, because `opengrid.platform.process.run_forever` installs one signal handler
    per call -- running several concurrently would only let the last-registered one see SIGTERM.
    """
    from opengrid import allocator, contracts, fleet, selector
    from opengrid.contracts import intake

    now = datetime.now(UTC)
    state.cycle_seq += 1
    cycle_id = f"{int(now.timestamp())}-{state.cycle_seq}"

    # Fleet persistence (telemetry COPY, hub_state upsert, SCADA/ack rows, PQ summaries) runs in its own
    # periodic task (`persist_fleet_state`): nothing in this tick reads it back -- dispatch uses the
    # in-memory twin and the guardian its own MQTT view -- and it was ~700 ms of the tick's p99 (A11).
    phase = state.phase_timer.phase
    with phase("heartbeat"):
        await write_heartbeat(state.heartbeat_pool, PROCESS_NAME)

    with phase("gate_schedule"):
        triggers = state.gate_scheduler.due_triggers(
            now,
            pending_admission_contract_ids=await state.backend.pending_admission_contract_ids(),
            due_renomination_contract_ids=await state.backend.due_renomination_contract_ids(now),
        )
        # A failed gate is traced + alerted inside run_due_gates (K7/K13).
        start_gates_in_background(
            state,
            triggers,
            lambda batch: run_due_gates(
                batch,
                now=now,
                run_intake=intake.run_intake_gate,
                run_gate=selector.run_gate,
                trace=state.trace,
                raise_alert=lambda finding: raise_alert(state.heartbeat_pool, finding, opened_at=now),
                on_renomination=lambda contract_id, plan_id: exercise_due_renomination_points(
                    contract_id, plan_id, now
                ),
            ),
        )

    if state.lifecycle_backend is not None:
        with phase("lifecycle"):
            try:
                await advance_obligations(
                    state.lifecycle_backend, contracts.transition_obligation, contracts.expire_unselected, now
                )
            except Exception:
                logger.exception("obligation lifecycle step failed", extra={"cycle_id": cycle_id})

    with phase("allocator"):
        grants = await allocator.run_cycle(
            cycle_id,
            fleet=state.fleet_gateway,
            ledger=state.ledger_gateway,
            scada_gateway=state.scada_gateway,
            schedule_gateway=state.schedule_gateway,
            now=now,
        )

    # K1 (user requirement: energy above reserve checked continuously, EVERY cycle, not just power
    # headroom): independent of the S1-S7 power-capability path above. Degrade, don't trip (K7) -- a
    # failure here must never block the allocator's own grant/guardian handoff this cycle.
    energy_results: list[EnergySufficiencyResult] = []
    if state.energy_sufficiency_gateway is not None:
        with phase("energy_check"):
            try:
                energy_results = await state.energy_sufficiency_gateway.run(now)
            except Exception:
                logger.exception("energy-sufficiency check failed this cycle", extra={"cycle_id": cycle_id})
    with phase("escalation"):
        try:
            await escalate_sustained_shortfalls(state, energy_results, contracts.transition_obligation)
        except Exception:
            logger.exception("shortfall escalation failed this cycle", extra={"cycle_id": cycle_id})
    with phase("guardian_check"):
        available = await guardian_is_available(
            state.backend,
            now=now,
            miss_threshold_s=state.heartbeat_interval_s * state.heartbeat_miss_threshold,
        )
    if not available:
        logger.warning(
            "guardian unavailable this cycle -- holding, no new batches proposed",
            extra={"cycle_id": cycle_id},
        )
        return

    grants_by_bank: dict[str, list[Grant]] = {}
    for grant in grants:
        grants_by_bank.setdefault(str(grant.bank_id), []).append(grant)
    with phase("propose"):
        for bank_id, bank_grants in grants_by_bank.items():
            await propose_batch_to_guardian(
                backend=state.backend,
                trace=state.trace,
                fleet_module=fleet,
                cycle_id=cycle_id,
                bank_id=bank_id,
                grants=bank_grants,
                ledger_version=max((g.ledger_version for g in bank_grants), default=0),
                epoch=state.epoch,
                seq=state.cycle_seq,
                now=now,
                lease_ttl_s=state.lease_ttl_s,
                cycle_interval_s=state.cycle_interval_s,
            )


async def main(cfg: Config) -> None:
    """Entry point for `python -m opengrid.engine.main`: opens the DB pool and MQTT client, rehydrates
    the fleet twin from Postgres (restart recovery), then drives the 15-min selector gate schedule and
    the 2-second allocator cycle via a single ticking loop, writing heartbeats and handling the
    engine -> guardian handoff. Degraded modes: a feed crossing `STALE` is enforced inside `selector`
    itself (it is the only module that knows which series matters per contract, 02b S6.5); an
    unavailable guardian is enforced here (`guardian_is_available`, "hold, don't pile up batches").
    """
    import opengrid.allocator as allocator_mod
    import opengrid.contracts as contracts_mod
    import opengrid.feeds as feeds_mod
    import opengrid.fleet as fleet_mod
    import opengrid.ledger as ledger_mod
    import opengrid.pq_ingest as pq_mod
    from opengrid.engine.gateways import (
        DEFAULT_ENERGY_LOOKAHEAD_S,
        EnergySufficiencyGateway,
        FleetCapabilityProvider,
        build_gateways,
    )
    from opengrid.engine.pg_backend import PgEngineBackend
    from opengrid.fleet.pg_backend import PgFleetBackend
    from opengrid.forecast import configure as configure_forecast
    from opengrid.forecast.pg_backend import PgForecastBackend
    from opengrid.ledger import ReservationLedger
    from opengrid.ledger.pg_backend import PgGrantBackend, PgLedgerBackend
    from opengrid.platform.config import resolve_secret
    from opengrid.platform.db import make_pool
    from opengrid.platform.log import configure_logging
    from opengrid.platform.mqtt import build_client
    from opengrid.pq_ingest.blob_store import FileBlobStore
    from opengrid.pq_ingest.pg_backend import PgPqIngestBackend

    configure_logging(PROCESS_NAME)
    pool = await make_pool(cfg)
    try:
        fleet_mod.configure(PgFleetBackend(pool), cfg)
        await fleet_mod.load_topology()

        # `opengrid.forecast`'s module-level facade is also a per-process singleton (like `ledger`
        # below): `selector.run_gate`'s `forecast.scenarios()` call runs inside this og-engine process,
        # not og-feeds's, so it needs its own `configure()` call here too, even though `run_forecast_cycle`
        # (the only caller of `history`) only ever runs in og-feeds -- `scenarios()` itself is a read-only
        # `og.forecast` query and never touches `history`, so passing `opengrid.feeds` unconfigured in
        # this process is safe (qa/merge-notes.md's "og-sim-fleet idle" investigation traced A4/A5's
        # missing commitments the rest of the way to `RuntimeError: opengrid.forecast.configure() must be
        # called before scenarios()` -- never wired here, so every gate crashed before ever reserving).
        configure_forecast(cfg, history=feeds_mod, backend=PgForecastBackend(pool))

        # opengrid.ledger's module-level facade (reserve/release/persist_grants/ledger_version) is a
        # per-process singleton wired exactly once, here -- selector.run_gate's ledger.reserve() calls
        # and the allocator gateway's persist_grants/ledger_version both run inside this same og-engine
        # process (02a S4: "one Python module, one process") and share this one instance.
        ledger_mod.configure(
            ReservationLedger(
                PgLedgerBackend(pool),
                FleetCapabilityProvider(),
                grant_backend=PgGrantBackend(pool),
            )
        )

        # opengrid.contracts.intake (BUILD.md intake task): wired here, in this same og-engine process,
        # for the identical reason forecast/ledger are wired here rather than in og-feeds -- _engine_tick
        # calls intake.run_intake_gate() ahead of every selector.run_gate() call, on this process's own
        # pool/trace store. `opengrid.forecast.scenarios` is already configured above; passed through
        # directly rather than re-imported at call time (BUILD.md S1 no-duplication).
        from opengrid.contracts.intake import configure as configure_intake
        from opengrid.contracts.intake.pg_ports import PgMarketDataPort
        from opengrid.contracts.pg_repo import PgContractsRepo
        from opengrid.forecast import scenarios as forecast_scenarios
        from opengrid.trace import TraceStore
        from opengrid.trace.pg_backend import PgTraceBackend

        # One TraceStore instance for this process (shared with intake) -- `propose_batch_to_guardian`
        # writes each cycle's RT_ALLOCATION pre-image through this same store (qa/merge-notes.md S17).
        trace_store = TraceStore(PgTraceBackend(pool))
        contracts_repo = PgContractsRepo(pool)
        configure_intake(
            contracts_repo,
            trace_store,
            PgMarketDataPort(pool),
            forecast_scenarios=forecast_scenarios,
        )
        # The selector's commit step and the lifecycle step transition obligations through
        # `opengrid.contracts` in this process, so its module facade needs the same repo/trace pair.
        contracts_mod.configure(contracts_repo, trace_store)
        pq_mod.configure(
            PgPqIngestBackend(pool),
            FileBlobStore(str(cfg.get("pq_ingest.blob_store_dir", "/var/lib/opengrid/pq_waveform"))),
            summary_buffer_max=int(
                cfg.get("pq_ingest.summary_buffer_max", pq_mod.DEFAULT_SUMMARY_BUFFER_MAX)
            ),
            flush_batch_size=int(cfg.get("pq_ingest.flush_batch_size", pq_mod.DEFAULT_FLUSH_BATCH_SIZE)),
        )
        released = await ledger_mod.release_uncommitted()
        logger.info("released uncommitted reservations at start-up", extra={"released_count": released})

        backend = PgEngineBackend(pool)
        fleet_gateway, ledger_gateway, scada_gateway, schedule_gateway = build_gateways(pool, trace_store)
        allocator_mod.configure(ledger_gateway)
        state = _EngineState(
            cfg=cfg,
            backend=backend,
            trace=trace_store,
            gate_scheduler=GateScheduler(),
            cycle_interval_s=float(cfg.get("allocator.cycle_interval_s", 2.0)),
            heartbeat_interval_s=float(cfg.get("health.heartbeat_interval_s", 5.0)),
            heartbeat_miss_threshold=int(cfg.get("health.heartbeat_miss_threshold", 3)),
            telemetry_interval_s=float(cfg.get("fleet.telemetry_interval_s", 2.0)),
            heartbeat_pool=pool,
            fleet_gateway=fleet_gateway,
            ledger_gateway=ledger_gateway,
            scada_gateway=scada_gateway,
            schedule_gateway=schedule_gateway,
            energy_sufficiency_gateway=EnergySufficiencyGateway(
                pool,
                trace_store,
                lookahead_s=float(cfg.get("allocator.energy_check_lookahead_s", DEFAULT_ENERGY_LOOKAHEAD_S)),
            ),
            lifecycle_backend=backend,
            lease_ttl_s=float(cfg.get("allocator.lease_ttl_s", DEFAULT_LEASE_TTL_S)),
            epoch=await backend.next_epoch(),
            pq_flush=Cadence(float(cfg.get("pq_ingest.flush_interval_s", pq_mod.DEFAULT_FLUSH_INTERVAL_S))),
            latency=CycleLatencyWindow(lag_probe=LoopLagProbe()),
        )
        logger.info("engine epoch", extra={"epoch": state.epoch})

        mqtt_password = resolve_secret("OG_MQTT_ENGINE_PASSWORD")
        async with build_client(
            cfg, username="og_engine", password=mqtt_password, client_id="og-engine"
        ) as client:
            raw_worker = BackgroundIngest("pq-raw", ingest_raw_capture_off_loop)
            raw_task = asyncio.create_task(raw_worker.run())
            ingest_task = asyncio.create_task(_mqtt_ingest_loop(client, cfg, raw_worker))
            ingest_task.add_done_callback(_log_ingest_exit)
            lag_probe = state.latency.lag_probe
            lag_task = asyncio.create_task(lag_probe.run()) if lag_probe is not None else raw_task
            persist_task = asyncio.create_task(
                run_periodic("fleet-persist", state.cycle_interval_s, lambda: persist_fleet_state(state))
            )
            try:
                await run_forever(
                    lambda: timed_tick(state), interval_s=state.cycle_interval_s, process_name=PROCESS_NAME
                )
            finally:
                for task in {ingest_task, raw_task, lag_task, persist_task}:
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task
    finally:
        await pool.close()


async def ingest_raw_capture_off_loop(payload: dict[str, Any]) -> None:
    """Background handler for a raw waveform capture: schema validation (the per-sample array check was
    ~34% of og-engine CPU on the event loop, live 2026-09-26) runs in a worker thread, then the blob and
    index are written. An invalid capture is logged and dropped."""
    from opengrid import pq_ingest
    from opengrid.platform.mqtt import SchemaValidationError, validate_payload

    try:
        await asyncio.to_thread(validate_payload, "pq_waveform_raw", payload)
    except SchemaValidationError:
        logger.warning("dropped invalid raw waveform capture", extra={"hub_id": payload.get("hub_id")})
        return
    await pq_ingest.ingest_raw_capture(payload)


def _log_ingest_exit(task: asyncio.Task[None]) -> None:
    """The MQTT ingest task must never end silently: without it the fleet twin goes stale and every
    hub drops out of dispatch while the engine tick still looks healthy (no process restart follows)."""
    if task.cancelled():
        return
    exc = task.exception()
    logger.error("mqtt ingest loop exited", exc_info=exc)


async def _mqtt_ingest_loop(client: aiomqtt.Client, cfg: Config, raw_worker: BackgroundIngest) -> None:
    """Subscribe to `<root>/tel/#`, `<root>/scada/#`, `<root>/scada/instruction/#` (topics.md) and route
    validated payloads into the fleet twin. Split out of `main` so it runs concurrently with the 2 s
    driving tick as one cancellable task (graceful shutdown, 02b S1.1-S1.3). `client` is already entered
    (`async with build_client(...)`) by the caller."""
    import json

    from opengrid import fleet, pq_ingest
    from opengrid.platform.mqtt import SchemaValidationError, topic, validate_payload

    tel_topic = topic(cfg, "tel/#")
    scada_instruction_topic = topic(cfg, "scada/instruction/#")
    scada_topic = topic(cfg, "scada/#")
    # Waveform topics sit under scada/ (topics.md) and must be routed before the SCADA bank-signal branch.
    wave_summary_topic = topic(cfg, "scada/wave/+/+/+/summary")
    wave_raw_topic = topic(cfg, "scada/wave/+/+/+/raw")
    ack_topic = topic(cfg, "ack/+")  # hub acks; ack/cal/<hub> (calibration) is a different schema
    await client.subscribe(tel_topic)
    await client.subscribe(ack_topic)
    await client.subscribe(scada_topic)  # also matches scada/instruction/#, disambiguated below

    async for message in client.messages:
        msg_topic = str(message.topic)
        try:
            payload = json.loads(message.payload)
            if message.topic.matches(wave_summary_topic):
                validate_payload("pq_waveform_summary", payload)
                await pq_ingest.ingest_summary(payload)
            elif message.topic.matches(wave_raw_topic):
                raw_worker.submit(payload)  # validation, blob write and index insert off the ingest path
            elif message.topic.matches(ack_topic):
                validate_payload("ack", payload)
                await fleet.ingest_ack(payload)
            elif message.topic.matches(tel_topic):
                validate_payload("telemetry", payload)
                await fleet.ingest_telemetry(payload)
            elif message.topic.matches(scada_instruction_topic):
                validate_payload("scada_utility_instruction", payload)
                await fleet.ingest_utility_instruction(payload)
            elif message.topic.matches(scada_topic):
                validate_payload("scada_bank_signal", payload)
                await fleet.ingest_scada_signal(payload)
        except SchemaValidationError:
            logger.warning("dropped invalid mqtt payload", extra={"topic": msg_topic})
        except Exception:
            logger.exception("mqtt ingest failed", extra={"topic": msg_topic})
