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
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal, Protocol
from uuid import UUID, uuid4

import aiomqtt
from psycopg_pool import AsyncConnectionPool

from opengrid.core.crypto import sha256_hex_of_json
from opengrid.core.models.engine import CommandBatchRow, Grant
from opengrid.core.timeutil import floor_to_interval
from opengrid.platform.config import Config
from opengrid.platform.heartbeat import write_heartbeat
from opengrid.platform.process import run_forever

if TYPE_CHECKING:
    from opengrid.allocator.gateways import FleetGateway, LedgerGateway, ScadaGateway, ScheduleGateway

logger = logging.getLogger(__name__)

PROCESS_NAME = "engine"
GATE_INTERVAL_MINUTES = 15
GUARDIAN_PROCESS_NAME = "guardian"


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
        triggers.extend(GateTrigger("ADMISSION", cid) for cid in pending_admission_contract_ids)
        triggers.extend(GateTrigger("RENOMINATION", cid) for cid in due_renomination_contract_ids)
        return triggers


def build_command_batch_row(
    *, cycle_id: str, bank_id: str, grants: list[Grant], ledger_version: int
) -> CommandBatchRow:
    """S8 command build (02a S5.1/S6): summarize one bank's grants for this cycle into the
    `og.command_batch` row engine hands to guardian. `merkle_root` here is a single SHA-256 over the
    JCS-canonical grant list (a placeholder single-leaf "tree" -- MVP-S has no need for inclusion
    proofs); guardian/allocator derive the actual signed per-hub `CommandItem` list independently from
    `grant`/`hub_state` before publishing to MQTT.
    """
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
        command_batch_id=uuid4(),
        cycle_id=cycle_id,
        ledger_version=ledger_version,
        submission_id=f"{cycle_id}:{bank_id}",
        command_count=sum(1 for g in grants if not g.is_headroom),
        merkle_root=sha256_hex_of_json(payload),
        trace_pre_image_id=None,
    )


async def propose_batch_to_guardian(
    *,
    backend: EngineBackend,
    cycle_id: str,
    bank_id: str,
    grants: list[Grant],
    ledger_version: int,
) -> UUID | None:
    """Build and persist this bank's command-batch summary and notify `og-guardian` (see module
    docstring). Returns the new `command_batch_id`, or `None` if there is nothing to propose (empty
    grant set -- nothing changed this cycle, no reason to wake the guardian)."""
    if not grants:
        return None
    row = build_command_batch_row(
        cycle_id=cycle_id, bank_id=bank_id, grants=grants, ledger_version=ledger_version
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
    cycle_seq: int = 0


async def _engine_tick(state: _EngineState) -> None:
    """One driving tick of the og-engine process, run every `[allocator].cycle_interval_s` (default
    2 s). A single tick drives every cadence this process owns (allocator cycle, gate scheduling,
    fleet maintenance, heartbeat) off wall-clock checks rather than several independent
    `run_forever` loops, because `opengrid.platform.process.run_forever` installs one signal handler
    per call -- running several concurrently would only let the last-registered one see SIGTERM.
    """
    from opengrid import allocator, contracts, fleet, selector

    now = datetime.now(UTC)
    state.cycle_seq += 1
    cycle_id = f"{int(now.timestamp())}-{state.cycle_seq}"

    await write_heartbeat(state.heartbeat_pool, PROCESS_NAME)
    await fleet.flush(now=now)

    triggers = state.gate_scheduler.due_triggers(
        now,
        pending_admission_contract_ids=await state.backend.pending_admission_contract_ids(),
        due_renomination_contract_ids=await state.backend.due_renomination_contract_ids(now),
    )
    for trigger in triggers:
        logger.info(
            "running gate", extra={"gate_kind": trigger.gate_kind, "contract_scope": trigger.contract_scope}
        )
        await selector.run_gate(trigger.gate_kind, trigger.contract_scope)

    grants = await allocator.run_cycle(
        cycle_id,
        fleet=state.fleet_gateway,
        ledger=state.ledger_gateway,
        scada_gateway=state.scada_gateway,
        schedule_gateway=state.schedule_gateway,
        now=now,
    )
    if not await guardian_is_available(
        state.backend, now=now, miss_threshold_s=state.heartbeat_interval_s * state.heartbeat_miss_threshold
    ):
        logger.warning(
            "guardian unavailable this cycle -- holding, no new batches proposed",
            extra={"cycle_id": cycle_id},
        )
        return

    grants_by_bank: dict[str, list[Grant]] = {}
    for grant in grants:
        grants_by_bank.setdefault(str(grant.bank_id), []).append(grant)
    for bank_id, bank_grants in grants_by_bank.items():
        await propose_batch_to_guardian(
            backend=state.backend,
            cycle_id=cycle_id,
            bank_id=bank_id,
            grants=bank_grants,
            ledger_version=max((g.ledger_version for g in bank_grants), default=0),
        )

    _ = contracts  # imported for process-wiring completeness; admission itself is api/contracts' own path


async def main(cfg: Config) -> None:
    """Entry point for `python -m opengrid.engine.main`: opens the DB pool and MQTT client, rehydrates
    the fleet twin from Postgres (restart recovery), then drives the 15-min selector gate schedule and
    the 2-second allocator cycle via a single ticking loop, writing heartbeats and handling the
    engine -> guardian handoff. Degraded modes: a feed crossing `STALE` is enforced inside `selector`
    itself (it is the only module that knows which series matters per contract, 02b S6.5); an
    unavailable guardian is enforced here (`guardian_is_available`, "hold, don't pile up batches").
    """
    import opengrid.fleet as fleet_mod
    import opengrid.ledger as ledger_mod
    from opengrid.engine.gateways import FleetCapabilityProvider, build_gateways
    from opengrid.engine.pg_backend import PgEngineBackend
    from opengrid.fleet.pg_backend import PgFleetBackend
    from opengrid.ledger import ReservationLedger
    from opengrid.ledger.pg_backend import PgGrantBackend, PgLedgerBackend
    from opengrid.platform.config import resolve_secret
    from opengrid.platform.db import make_pool
    from opengrid.platform.log import configure_logging
    from opengrid.platform.mqtt import build_client

    configure_logging(PROCESS_NAME)
    pool = await make_pool(cfg)
    try:
        fleet_mod.configure(PgFleetBackend(pool), cfg)
        await fleet_mod.load_topology()

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

        backend = PgEngineBackend(pool)
        fleet_gateway, ledger_gateway, scada_gateway, schedule_gateway = build_gateways(pool)
        state = _EngineState(
            cfg=cfg,
            backend=backend,
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
        )

        mqtt_password = resolve_secret("OG_MQTT_ENGINE_PASSWORD")
        async with build_client(
            cfg, username="og_engine", password=mqtt_password, client_id="og-engine"
        ) as client:
            ingest_task = asyncio.create_task(_mqtt_ingest_loop(client, cfg))
            try:
                await run_forever(
                    lambda: _engine_tick(state), interval_s=state.cycle_interval_s, process_name=PROCESS_NAME
                )
            finally:
                ingest_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await ingest_task
    finally:
        await pool.close()


async def _mqtt_ingest_loop(client: aiomqtt.Client, cfg: Config) -> None:
    """Subscribe to `<root>/tel/#`, `<root>/scada/#`, `<root>/scada/instruction/#` (topics.md) and route
    validated payloads into the fleet twin. Split out of `main` so it runs concurrently with the 2 s
    driving tick as one cancellable task (graceful shutdown, 02b S1.1-S1.3). `client` is already entered
    (`async with build_client(...)`) by the caller."""
    import json

    from opengrid import fleet
    from opengrid.platform.mqtt import SchemaValidationError, topic, validate_payload

    tel_topic = topic(cfg, "tel/#")
    scada_instruction_topic = topic(cfg, "scada/instruction/#")
    scada_topic = topic(cfg, "scada/#")
    await client.subscribe(tel_topic)
    await client.subscribe(scada_topic)  # also matches scada/instruction/#, disambiguated below

    async for message in client.messages:
        msg_topic = str(message.topic)
        try:
            payload = json.loads(message.payload)
            if message.topic.matches(tel_topic):
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
