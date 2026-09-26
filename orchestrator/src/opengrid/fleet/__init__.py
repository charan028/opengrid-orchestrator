"""opengrid.fleet -- digital twin (state + eligibility) inside og-engine (02b S4). Owner: engine
agent (BUILD.md S4).

Never simulates physics forward (that is `ogsim`'s/`sim`'s job); only stores/aggregates reported state
and derives `capability(bank, t)` from it via `opengrid.core.physics`/`opengrid.core.limits`
(02b S12: "fleet never simulates -- it only stores and aggregates").

I/O (Postgres reads/writes) is isolated behind the `FleetBackend` protocol so the twin's classification
and aggregation logic stays unit-testable without a database, mirroring `opengrid.trace`'s
`TraceBackend` split (BUILD.md S5a "pure logic separated from I/O"); the real implementation lives in
`opengrid.fleet.pg_backend` (kept psycopg-free here).

Public interface fixed by `orchestrator/INTERFACES.md`: `capability(bank_id, interval_start)` and
`ingest_telemetry(payload)`. Every other function here (`configure`, `load_topology`, `flush`,
`ingest_scada_signal`, `ingest_utility_instruction`, `bank_scada_signal`, `utility_instruction`) is an
engine-internal extension used only by `opengrid.engine`/`opengrid.allocator`, not a wire/API contract.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal, NamedTuple, Protocol

from opengrid.core.models.mqtt import ScadaBankSignal, ScadaUtilityInstruction, Telemetry
from opengrid.core.models.platform import Bank, Hub, HubState
from opengrid.core.physics import (
    DEFAULT_ETA_D,
    BankParams,
    HubParams,
    bank_capability,
    hub_capability,
    recharge_headroom,
)
from opengrid.core.timeutil import is_stale
from opengrid.platform.config import Config
from opengrid.platform.metrics import hubs as hubs_gauge
from opengrid.platform.metrics import telemetry_fresh_ratio

logger = logging.getLogger(__name__)

HubHealth = str  # "online" | "stale" | "offline" | "fault" -- see _classify_health


class AvailableCapability(NamedTuple):
    bank_id: str
    interval_start: datetime
    max_discharge_kw: float
    max_charge_kw: float
    excluded_hub_ids: frozenset[str]  # stale/offline hubs excluded this interval (02b S6.5)


class HubCapabilitySnapshot(NamedTuple):
    """One hub's real-time eligibility state, fleet's own (allocator-independent) shape (merge task A3,
    BUILD.md merge role). `opengrid.engine` (the sole wiring owner, 02b S1.2) maps a list of these onto
    `opengrid.allocator.models.HubSnapshot` for the allocator's `FleetGateway.fleet_state` -- fleet never
    imports `opengrid.allocator` itself (dependency direction: core <- platform <- modules, BUILD.md
    S5a), so this stays a fleet-owned type at the same abstraction level as `AvailableCapability`.
    """

    hub_id: str
    bank_id: str
    free_discharge_kw: float  # reserve-safe (K1): from hub_capability(), 0.0 if not "online"
    health: HubHealth
    last_seen_at: datetime | None
    # Energy-sufficiency pass (K1, user requirement "energy above reserve must be checked
    # continuously"): live SoC/reserve/capacity/efficiency so `opengrid.engine.gateways` can populate
    # the allocator's `HubSnapshot` fully -- `None` only for a hub excluded this instant (stale/offline/
    # fault), matching `free_discharge_kw=0.0`'s own "don't trust it" contract above.
    soc_kwh: float | None = None
    reserve_kwh: float | None = None
    e_kwh: float | None = None
    eta_d: float = DEFAULT_ETA_D


@dataclass(frozen=True, slots=True)
class TelemetryRow:
    """One row for the append-only `og.telemetry` partitioned table (02b S4.2)."""

    hub_id: str
    ts: datetime
    soc_kwh: float
    p_kw: float
    seq: int
    epoch: int
    health: str


class FleetBackend(Protocol):
    """Storage contract the twin needs. A real backend persists to `og.hub`/`og.bank`/`og.hub_state`/
    `og.telemetry`; tests use an in-memory fake (BUILD.md S5a: "pure logic separated from I/O")."""

    async def load_hubs(self) -> list[Hub]: ...

    async def load_banks(self) -> list[Bank]: ...

    async def load_hub_states(self) -> list[HubState]:
        """Restart recovery (02b S1.2): rehydrate the in-memory twin from the last-persisted snapshot."""
        ...

    async def upsert_hub_states(self, states: list[HubState]) -> None: ...

    async def copy_telemetry(self, rows: list[TelemetryRow]) -> None: ...

    async def record_scada_observations(self, signals: list[ScadaBankSignal]) -> None:
        """Persists SCADA bank readings to `og.feed_obs` (source='scada') -- the ONLY writer of that row
        shape, read by other processes (`opengrid.guardian.repo.PgBankStatePort`'s G-03 bank-kVA check,
        `opengrid.health`'s `ALR-SCADA-OVERLOAD`). Called in batch from `flush`, never per message."""
        ...


@dataclass(slots=True)
class _HubRuntime:
    """In-memory latest state for one hub -- the only thing `capability()` reads (02b S4.2:
    "`hub_state` is the only table the 2-s allocator reads for current fleet capability")."""

    hub_id: str
    bank_id: str
    zone: str
    params: HubParams
    soc_kwh: float = 0.0
    p_kw: float = 0.0
    fault_code: str | None = None
    lease_epoch: int = 0
    lease_expires_at: datetime | None = None
    last_command_id: Any = None
    last_seen_at: datetime | None = None
    seq: int = -1
    epoch: int = -1


@dataclass(slots=True)
class _BankRuntime:
    bank_id: str
    zone: str
    params: BankParams
    feeder_id: str | None
    hub_ids: set[str] = field(default_factory=set)


_HUB_STALE_S_DEFAULT = 6.0
_HUB_OFFLINE_S_DEFAULT = 30.0
_TELEMETRY_INTERVAL_S_DEFAULT = 2.0

_backend: FleetBackend | None = None
_hub_stale_s: float = _HUB_STALE_S_DEFAULT
_hub_offline_s: float = _HUB_OFFLINE_S_DEFAULT
_telemetry_interval_s: float = _TELEMETRY_INTERVAL_S_DEFAULT

_hubs: dict[str, _HubRuntime] = {}
_banks: dict[str, _BankRuntime] = {}
_pending_telemetry: list[TelemetryRow] = []
_bank_scada: dict[str, ScadaBankSignal] = {}
_pending_scada: dict[str, ScadaBankSignal] = {}  # latest unpersisted reading per bank, for `flush`
_utility_instructions: dict[str, ScadaUtilityInstruction] = {}


def configure(backend: FleetBackend, cfg: Config) -> None:
    """Wire the twin to its storage backend and read its two health thresholds from config
    (`[health].hub_stale_s`/`hub_offline_s`, 02b S1.4). Called once by `opengrid.engine.main` at
    startup; safe to call again in tests to reset module state between cases."""
    global _backend, _hub_stale_s, _hub_offline_s, _telemetry_interval_s
    _backend = backend
    _hub_stale_s = float(cfg.get("health.hub_stale_s", _HUB_STALE_S_DEFAULT))
    _hub_offline_s = float(cfg.get("health.hub_offline_s", _HUB_OFFLINE_S_DEFAULT))
    _telemetry_interval_s = float(cfg.get("fleet.telemetry_interval_s", _TELEMETRY_INTERVAL_S_DEFAULT))
    _hubs.clear()
    _banks.clear()
    _pending_telemetry.clear()
    _bank_scada.clear()
    _pending_scada.clear()
    _utility_instructions.clear()


def _require_backend() -> FleetBackend:
    if _backend is None:
        raise RuntimeError("opengrid.fleet.configure() must be called before use")
    return _backend


async def load_topology() -> None:
    """Load `hub`/`bank` rows and rehydrate the in-memory twin from the last `hub_state` snapshot
    (restart recovery, 02b S1.2/S1.3: og-engine rebuilds its view from Postgres on every restart)."""
    backend = _require_backend()
    banks_by_id: dict[str, _BankRuntime] = {}
    for bank in await backend.load_banks():
        banks_by_id[bank.bank_id] = _BankRuntime(
            bank_id=bank.bank_id,
            zone=bank.zone,
            params=BankParams(kva_rating=bank.kva_rating, reserve_kva=bank.reserve_kva),
            feeder_id=bank.feeder_id,
        )

    hubs_by_id: dict[str, _HubRuntime] = {}
    for hub in await backend.load_hubs():
        hubs_by_id[hub.hub_id] = _HubRuntime(
            hub_id=hub.hub_id,
            bank_id=hub.bank_id,
            zone=hub.zone,
            params=HubParams(
                e_kwh=hub.e_kwh, r_kwh=hub.r_kwh, p_kw=hub.p_kw, eta_c=hub.eta_c, eta_d=hub.eta_d
            ),
        )
        bank_rt = banks_by_id.get(hub.bank_id)
        if bank_rt is not None:
            bank_rt.hub_ids.add(hub.hub_id)

    for state in await backend.load_hub_states():
        runtime = hubs_by_id.get(state.hub_id)
        if runtime is None:
            continue
        runtime.soc_kwh = state.soc_kwh
        runtime.p_kw = state.p_kw
        runtime.fault_code = state.fault_code
        runtime.lease_epoch = state.lease_epoch
        runtime.lease_expires_at = state.lease_expires_at
        runtime.last_command_id = state.last_command_id
        runtime.last_seen_at = state.last_seen_at

    _banks.clear()
    _banks.update(banks_by_id)
    _hubs.clear()
    _hubs.update(hubs_by_id)
    logger.info("fleet topology loaded", extra={"hubs": len(_hubs), "banks": len(_banks)})


def _classify_health(*, fault_code: str | None, last_seen_at: datetime | None, now: datetime) -> HubHealth:
    """K7/02b S6.4: fault (hub-reported) is independent of timing; otherwise online/stale/offline is
    purely `age = now - last_seen_at` compared against the two configured thresholds, via the single
    shared `core.timeutil.is_stale` primitive (02b S12) -- never re-derived here."""
    if fault_code:
        return "fault"
    if is_stale(last_seen_at, _hub_offline_s, now=now):
        return "offline"
    if is_stale(last_seen_at, _hub_stale_s, now=now):
        return "stale"
    return "online"


def hub_health(hub_id: str, *, now: datetime | None = None) -> HubHealth:
    """Current classification for one hub (TS-03-04). Raises `LookupError` for an unknown hub."""
    runtime = _hubs.get(hub_id)
    if runtime is None:
        raise LookupError(f"unknown hub_id: {hub_id}")
    now = now or datetime.now(UTC)
    return _classify_health(fault_code=runtime.fault_code, last_seen_at=runtime.last_seen_at, now=now)


async def ingest_telemetry(payload: dict[str, Any]) -> None:
    """Upsert `hub_state` and append to `telemetry` from a validated `Telemetry` MQTT message.

    The caller (the engine's MQTT subscriber) has already validated `payload` against
    `interfaces/mqtt/telemetry.schema.json` via `opengrid.platform.mqtt.validate_payload`; this function
    additionally parses it into the typed `Telemetry` model for defense in depth. Unknown hubs are
    logged and skipped rather than raising, so one bad/unregistered publisher never stalls ingestion for
    the other 1,999 hubs (K7: degrade, don't trip).
    """
    telemetry = Telemetry.model_validate(payload)
    runtime = _hubs.get(telemetry.hub_id)
    if runtime is None:
        logger.warning("telemetry for unknown hub_id", extra={"hub_id": telemetry.hub_id})
        return

    runtime.soc_kwh = telemetry.soc_kwh
    runtime.p_kw = telemetry.p_kw
    runtime.fault_code = telemetry.fault_code
    runtime.last_seen_at = telemetry.ts
    runtime.seq = telemetry.seq
    runtime.epoch = telemetry.epoch

    _pending_telemetry.append(
        TelemetryRow(
            hub_id=telemetry.hub_id,
            ts=telemetry.ts,
            soc_kwh=telemetry.soc_kwh,
            p_kw=telemetry.p_kw,
            seq=telemetry.seq,
            epoch=telemetry.epoch,
            health=telemetry.health,
        )
    )


@dataclass(frozen=True, slots=True)
class FlushStats:
    telemetry_rows: int
    hub_states: int


async def flush(*, now: datetime | None = None) -> FlushStats:
    """Periodic maintenance tick (02b S4.2): bulk `COPY` the buffered telemetry rows, bulk-upsert
    `hub_state`, reclassify every hub's health from `last_seen_at` (catches hubs that simply stopped
    publishing, not only ones that sent a fault telemetry -- TS-03-04's "excluded within one detection
    cycle"), and refresh the fleet-health Prometheus gauges. Called by `opengrid.engine.main` on its own
    `[fleet].telemetry_interval_s` timer.
    """
    backend = _require_backend()
    now = now or datetime.now(UTC)

    rows, _pending_telemetry[:] = list(_pending_telemetry), []
    if rows:
        await backend.copy_telemetry(rows)
    scada = list(_pending_scada.values())
    _pending_scada.clear()
    if scada:
        await backend.record_scada_observations(scada)

    states: list[HubState] = []
    health_counts: dict[str, int] = {"online": 0, "stale": 0, "offline": 0, "fault": 0}
    fresh_by_zone: dict[str, list[bool]] = {}
    fresh_threshold_s = 2 * _telemetry_interval_s  # 02b S6.6: "no older than 2x telemetry interval"

    for runtime in _hubs.values():
        classification = _classify_health(
            fault_code=runtime.fault_code, last_seen_at=runtime.last_seen_at, now=now
        )
        health_counts[classification] = health_counts.get(classification, 0) + 1
        fresh = not is_stale(runtime.last_seen_at, fresh_threshold_s, now=now)
        fresh_by_zone.setdefault(runtime.zone, []).append(fresh)
        if runtime.last_seen_at is None:
            continue
        # HubState.health accepts the full vocabulary (online/stale/offline/fault), so persist as classified.
        persisted_health: Literal["online", "stale", "offline", "fault"] = classification  # type: ignore[assignment]
        states.append(
            HubState(
                hub_id=runtime.hub_id,
                soc_kwh=runtime.soc_kwh,
                p_kw=runtime.p_kw,
                health=persisted_health,
                lease_epoch=runtime.lease_epoch,
                lease_expires_at=runtime.lease_expires_at,
                last_command_id=runtime.last_command_id,
                last_seen_at=runtime.last_seen_at,
                fault_code=runtime.fault_code,
            )
        )

    if states:
        await backend.upsert_hub_states(states)

    for health, count in health_counts.items():
        hubs_gauge.labels(health=health).set(count)
    for zone, flags in fresh_by_zone.items():
        ratio = sum(1 for f in flags if f) / len(flags) if flags else 1.0
        telemetry_fresh_ratio.labels(zone=zone).set(ratio)

    return FlushStats(telemetry_rows=len(rows), hub_states=len(states))


async def ingest_scada_signal(payload: dict[str, Any]) -> None:
    """Store the latest simulated-utility SCADA bank reading (02b S6.2 `<root>/scada/<bank_id>`),
    quality flag included, for the allocator's `DIST_DEFERRAL` PI loop and `capability()`'s charge
    headroom to read. `fleet` stores/aggregates only -- it never runs the PI loop itself (02b S12).

    The reading is also buffered (latest per bank) for `flush` to persist to `og.feed_obs`, where
    `og-guardian`'s G-03 check and the `ALR-SCADA-OVERLOAD` alert read it from other processes. No I/O
    here: a per-message commit on the MQTT ingest path fell behind under load (live 2026-09-26) and
    delayed every hub's telemetry by minutes."""
    signal = ScadaBankSignal.model_validate(payload)
    _bank_scada[signal.bank_id] = signal
    _pending_scada[signal.bank_id] = signal


def bank_scada_signal(bank_id: str) -> ScadaBankSignal | None:
    """Latest stored SCADA reading for `bank_id`, or `None` if none has ever arrived."""
    return _bank_scada.get(bank_id)


async def ingest_utility_instruction(payload: dict[str, Any]) -> None:
    """Store the latest utility/ISO instruction for a bank (02b S6.2 `<root>/scada/instruction/<bank_id>`,
    K5/G-15: an L2 hard constraint, never traded for commercial value). `capability()` folds an active
    `BLOCK`/`ESTOP`/`LIMIT` into its returned envelope so the allocator never even proposes beyond it;
    `guardian` G-15 re-checks it independently before signing."""
    instruction = ScadaUtilityInstruction.model_validate(payload)
    _utility_instructions[instruction.bank_id] = instruction


def utility_instruction(bank_id: str) -> ScadaUtilityInstruction | None:
    """Latest stored utility instruction for `bank_id`, or `None`."""
    return _utility_instructions.get(bank_id)


def _active_utility_limit_kw(bank_id: str, *, now: datetime) -> float | None:
    """Returns the effective discharge ceiling (kW) from an unexpired `LIMIT`/`BLOCK`/`ESTOP`
    instruction, or `None` if no instruction is active (K5)."""
    instruction = _utility_instructions.get(bank_id)
    if instruction is None:
        return None
    if instruction.expires_at is not None and instruction.expires_at <= now:
        return None
    if instruction.kind in ("BLOCK", "ESTOP"):
        return 0.0
    if instruction.kind == "LIMIT":
        return instruction.limit_kw if instruction.limit_kw is not None else None
    return None


async def capability(bank_id: str, interval_start: datetime) -> AvailableCapability:
    """The single entry point `selector`/`allocator` call for a bank's capability at an interval
    (02b S4 interface). Derives its numbers from `opengrid.core.physics.hub_capability`/
    `bank_capability` applied to the latest `hub_state` rows, excluding hubs marked stale/offline/fault,
    and folds in any active L2 utility instruction (K5) as a hard ceiling.

    Raises `LookupError` if `bank_id` is not a known bank.
    """
    bank_rt = _banks.get(bank_id)
    if bank_rt is None:
        raise LookupError(f"unknown bank_id: {bank_id}")

    now = datetime.now(UTC)
    excluded: set[str] = set()
    discharge_kw: list[float] = []
    charge_kw = 0.0

    for hub_id in bank_rt.hub_ids:
        runtime = _hubs[hub_id]
        classification = _classify_health(
            fault_code=runtime.fault_code, last_seen_at=runtime.last_seen_at, now=now
        )
        if classification != "online":
            excluded.add(hub_id)
            continue
        d_kw, c_kw = hub_capability(runtime.soc_kwh, runtime.params)
        discharge_kw.append(d_kw)
        charge_kw += c_kw

    max_discharge_kw = bank_capability(discharge_kw, bank_rt.params)

    scada = _bank_scada.get(bank_id)
    bank_load_kva = scada.value if scada is not None and scada.signal == "APPARENT_POWER_KVA" else 0.0
    headroom_kw = recharge_headroom(bank_load_kva, bank_rt.params)
    max_charge_kw = min(charge_kw, headroom_kw)

    utility_limit = _active_utility_limit_kw(bank_id, now=now)
    if utility_limit is not None:
        max_discharge_kw = min(max_discharge_kw, utility_limit)
        max_charge_kw = min(max_charge_kw, utility_limit)

    return AvailableCapability(
        bank_id=bank_id,
        interval_start=interval_start,
        max_discharge_kw=max_discharge_kw,
        max_charge_kw=max_charge_kw,
        excluded_hub_ids=frozenset(excluded),
    )


def known_bank_ids() -> list[str]:
    """Every bank the twin has topology for (merge task, dispatch-live pass): the engine's
    `FleetGateway.bank_ids()` adapter reads this so `opengrid.allocator.run_cycle` covers every real
    bank each 2 s cycle, not a hardcoded/guessed list."""
    return list(_banks.keys())


def bank_zone(bank_id: str) -> str:
    """The bank's configured zone (`og.bank.zone`), for the allocator's `BankSnapshot.zone` (ZONE-scoped
    L2 instructions/safe-stop grouping). Raises `LookupError` if `bank_id` is not a known bank."""
    bank_rt = _banks.get(bank_id)
    if bank_rt is None:
        raise LookupError(f"unknown bank_id: {bank_id}")
    return bank_rt.zone


def hub_capabilities(bank_id: str) -> list[HubCapabilitySnapshot]:
    """Per-hub eligibility snapshot for every hub on `bank_id` (merge task A3): the hub-level detail
    `capability()`'s bank-aggregate return throws away, needed so `opengrid.engine` can build the
    allocator's `HubSnapshot` sequence for its 2 s water-filling cycle (`opengrid.allocator.cycle`,
    `opengrid.allocator.waterfill`) instead of `run_cycle` raising `NotImplementedError` for lack of a
    hub-level fleet read (`opengrid.allocator.gateways.FleetGateway.fleet_state`).

    A hub excluded this instant (stale/offline/fault) is still returned, with `free_discharge_kw=0.0`,
    so the allocator can report *why* a hub got nothing (health) rather than seeing it silently vanish
    from the bank's roster. Raises `LookupError` if `bank_id` is not a known bank.
    """
    bank_rt = _banks.get(bank_id)
    if bank_rt is None:
        raise LookupError(f"unknown bank_id: {bank_id}")

    now = datetime.now(UTC)
    snapshots: list[HubCapabilitySnapshot] = []
    for hub_id in bank_rt.hub_ids:
        runtime = _hubs[hub_id]
        classification = _classify_health(
            fault_code=runtime.fault_code, last_seen_at=runtime.last_seen_at, now=now
        )
        free_discharge_kw = 0.0
        soc_kwh: float | None = None
        reserve_kwh: float | None = None
        e_kwh: float | None = None
        if classification == "online":
            free_discharge_kw, _charge_kw = hub_capability(runtime.soc_kwh, runtime.params)
            soc_kwh = runtime.soc_kwh
            reserve_kwh = runtime.params.r_kwh
            e_kwh = runtime.params.e_kwh
        snapshots.append(
            HubCapabilitySnapshot(
                hub_id=hub_id,
                bank_id=bank_id,
                free_discharge_kw=free_discharge_kw,
                health=classification,
                last_seen_at=runtime.last_seen_at,
                soc_kwh=soc_kwh,
                reserve_kwh=reserve_kwh,
                e_kwh=e_kwh,
                eta_d=runtime.params.eta_d,
            )
        )
    return snapshots
