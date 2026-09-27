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

from prometheus_client import Counter

from opengrid.core.models.mqtt import (
    FLOW_TELEMETRY_FIELDS,
    Ack,
    ScadaBankSignal,
    ScadaUtilityInstruction,
    Telemetry,
)
from opengrid.core.models.platform import Bank, Hub, HubState
from opengrid.core.physics import (
    DEFAULT_ETA_D,
    BankParams,
    HubParams,
    bank_capability,
    hub_capability,
    hub_ramp_kw_per_s,
    recharge_headroom,
)
from opengrid.core.timeutil import is_stale
from opengrid.health.model import HealthThresholds
from opengrid.health.rules import classify_hub_health
from opengrid.platform.config import Config
from opengrid.platform.metrics import hubs as hubs_gauge
from opengrid.platform.metrics import telemetry_fresh_ratio

logger = logging.getLogger(__name__)

fleet_rows_dropped_total = Counter(
    "og_fleet_rows_dropped_total",
    "Buffered fleet rows dropped after failed database writes (oldest first, bounded requeue).",
    labelnames=("kind",),  # telemetry | ack
)

HubHealth = str  # "online" | "stale" | "offline" | "fault" -- opengrid.health.rules.classify_hub_health


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
    # Measured power now (+charge/-discharge; `None` when excluded) and the hub's G-04 ramp bound, so
    # the engine can ramp each per-hub setpoint from where the hub actually is (K4).
    p_kw: float | None = None
    ramp_kw_per_s: float = 0.0
    # Nameplate discharge kW (`params.p_kw`) regardless of health/SoC: lets the allocator attribute a
    # shortfall to a device fault (L0) or the reserve floor (L1), K13.
    rated_kw: float | None = None
    # Latest optional discharge-flow telemetry (migration 0027; `None` until the hub reports it or while
    # excluded): cell temperature and BMS limits for dispatch derating, meter/home/PV for flow checks.
    home_load_kw: float | None = None
    pv_kw: float | None = None
    meter_kw: float | None = None
    cell_temp_c: float | None = None
    p_dis_max_kw: float | None = None
    p_ch_max_kw: float | None = None
    peak_power_budget_kws: float | None = None
    # Battery/inverter units in the home (og.hub.units, migration 0032): G-02's per-unit cap.
    units: int | None = None
    #: The hub's bank is an og.asset SUBSTATION (D-29 toll): rated at nameplate, not the home unit cap.
    utility_scale: bool = False


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
    # Optional discharge-flow telemetry (migration 0027), in `FLOW_TELEMETRY_FIELDS` order; None = absent.
    flow: tuple[float | None, ...] = (None,) * len(FLOW_TELEMETRY_FIELDS)


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

    async def insert_acks(self, acks: list[Ack]) -> None:
        """Persists hub command acknowledgements to `og.command_ack` (idempotent per batch/hub).
        Called in batch from `flush`, never per message."""
        ...

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
    #: Latest optional discharge-flow telemetry, `FLOW_TELEMETRY_FIELDS` name -> value (absent = None).
    flow: dict[str, float | None] = field(default_factory=dict)


@dataclass(slots=True)
class _BankRuntime:
    bank_id: str
    zone: str
    params: BankParams
    feeder_id: str | None
    hub_ids: set[str] = field(default_factory=set)


_HUB_STALE_S_DEFAULT = 25.0  # [health].hub_stale_s at the 10 s telemetry cadence
_HUB_OFFLINE_S_DEFAULT = 30.0
_TELEMETRY_INTERVAL_S_DEFAULT = 2.0

_backend: FleetBackend | None = None
_hub_offline_s: float = _HUB_OFFLINE_S_DEFAULT
_telemetry_interval_s: float = _TELEMETRY_INTERVAL_S_DEFAULT
#: The ONE hub-health classifier's thresholds (`opengrid.health.rules.classify_hub_health`): online
#: <= 2 x telemetry interval, offline after `hub_offline_s`. Cached once per `configure()`.
_thresholds: HealthThresholds = HealthThresholds(
    hub_stale_s=_HUB_STALE_S_DEFAULT,
    hub_offline_s=_HUB_OFFLINE_S_DEFAULT,
    telemetry_interval_s=_TELEMETRY_INTERVAL_S_DEFAULT,
)

_hubs: dict[str, _HubRuntime] = {}
_banks: dict[str, _BankRuntime] = {}
_pending_telemetry: list[TelemetryRow] = []
_bank_scada: dict[tuple[str, str], ScadaBankSignal] = {}  # (bank_id, signal) -> latest reading
_pending_scada: dict[
    tuple[str, str], ScadaBankSignal
] = {}  # latest unpersisted per (bank, signal), for `flush`
_pending_acks: list[Ack] = []  # hub acknowledgements not yet written, for `flush`
_utility_instructions: dict[str, ScadaUtilityInstruction] = {}


def configure(backend: FleetBackend, cfg: Config) -> None:
    """Wire the twin to its storage backend and read its two health thresholds from config
    (`[health].hub_stale_s`/`hub_offline_s`, 02b S1.4). Called once by `opengrid.engine.main` at
    startup; safe to call again in tests to reset module state between cases."""
    global _backend, _hub_offline_s, _telemetry_interval_s, _thresholds
    _backend = backend
    _hub_offline_s = float(cfg.get("health.hub_offline_s", _HUB_OFFLINE_S_DEFAULT))
    _telemetry_interval_s = float(cfg.get("fleet.telemetry_interval_s", _TELEMETRY_INTERVAL_S_DEFAULT))
    # hub_stale_s was never loaded here, so the twin classified stale at the dataclass default (6 s)
    # whatever the config said (R3 review, HEALTH).
    hub_stale_s = float(cfg.get("health.hub_stale_s", _HUB_STALE_S_DEFAULT))
    _thresholds = HealthThresholds(
        hub_stale_s=hub_stale_s, hub_offline_s=_hub_offline_s, telemetry_interval_s=_telemetry_interval_s
    )
    _hubs.clear()
    _banks.clear()
    _pending_telemetry.clear()
    _bank_scada.clear()
    _pending_scada.clear()
    _pending_acks.clear()
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
                e_kwh=hub.e_kwh,
                r_kwh=hub.r_kwh,
                p_kw=hub.p_kw,
                eta_c=hub.eta_c,
                eta_d=hub.eta_d,
                units=hub.units,
                utility_scale=hub.utility_scale,
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
        runtime.flow = {name: getattr(state, name) for name in FLOW_TELEMETRY_FIELDS}

    _banks.clear()
    _banks.update(banks_by_id)
    _hubs.clear()
    _hubs.update(hubs_by_id)
    logger.info("fleet topology loaded", extra={"hubs": len(_hubs), "banks": len(_banks)})


def hub_health(hub_id: str, *, now: datetime | None = None) -> HubHealth:
    """Current classification for one hub (TS-03-04). Raises `LookupError` for an unknown hub."""
    runtime = _hubs.get(hub_id)
    if runtime is None:
        raise LookupError(f"unknown hub_id: {hub_id}")
    now = now or datetime.now(UTC)
    return classify_hub_health(
        fault_code=runtime.fault_code, last_seen_at=runtime.last_seen_at, now=now, thresholds=_thresholds
    )


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
    flow = tuple(getattr(telemetry, name) for name in FLOW_TELEMETRY_FIELDS)
    runtime.flow = dict(zip(FLOW_TELEMETRY_FIELDS, flow, strict=True))

    _pending_telemetry.append(
        TelemetryRow(
            hub_id=telemetry.hub_id,
            ts=telemetry.ts,
            soc_kwh=telemetry.soc_kwh,
            p_kw=telemetry.p_kw,
            seq=telemetry.seq,
            epoch=telemetry.epoch,
            health=telemetry.health,
            flow=flow,
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

    # Each buffer is taken before its write; on a failed write the rows go back in front of anything that
    # arrived meanwhile (bounded: the oldest are dropped and counted), so a database error never silently
    # loses telemetry/SCADA/ack evidence (review #13). The first failure is re-raised after requeueing.
    failure: Exception | None = None
    rows, _pending_telemetry[:] = list(_pending_telemetry), []
    if rows:
        try:
            await backend.copy_telemetry(rows)
        except Exception as exc:
            failure = exc
            _requeue(_pending_telemetry, rows, "telemetry")
    scada = list(_pending_scada.values())
    _pending_scada.clear()
    if scada:
        try:
            await backend.record_scada_observations(scada)
        except Exception as exc:
            failure = failure or exc
            for signal in scada:  # keep a newer reading that arrived meanwhile
                _pending_scada.setdefault((signal.bank_id, signal.signal), signal)
    acks, _pending_acks[:] = list(_pending_acks), []
    if acks:
        try:
            await backend.insert_acks(acks)
        except Exception as exc:
            failure = failure or exc
            _requeue(_pending_acks, acks, "ack")

    states: list[HubState] = []
    health_counts: dict[str, int] = {"online": 0, "stale": 0, "offline": 0, "fault": 0}
    fresh_by_zone: dict[str, list[bool]] = {}
    fresh_threshold_s = 2 * _telemetry_interval_s  # 02b S6.6: "no older than 2x telemetry interval"

    for runtime in _hubs.values():
        classification = classify_hub_health(
            fault_code=runtime.fault_code,
            last_seen_at=runtime.last_seen_at,
            now=now,
            thresholds=_thresholds,
        )
        health_counts[classification] = health_counts.get(classification, 0) + 1
        fresh = not is_stale(runtime.last_seen_at, fresh_threshold_s, now=now)
        fresh_by_zone.setdefault(runtime.zone, []).append(fresh)
        if runtime.last_seen_at is None:
            continue
        # HubState.health accepts the full vocabulary (online/stale/offline/fault), so persist as classified.
        persisted_health: Literal["online", "stale", "offline", "fault"] = classification
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
                home_load_kw=runtime.flow.get("home_load_kw"),
                pv_kw=runtime.flow.get("pv_kw"),
                meter_kw=runtime.flow.get("meter_kw"),
                cell_temp_c=runtime.flow.get("cell_temp_c"),
                p_dis_max_kw=runtime.flow.get("p_dis_max_kw"),
                p_ch_max_kw=runtime.flow.get("p_ch_max_kw"),
                peak_power_budget_kws=runtime.flow.get("peak_power_budget_kws"),
                charge_pv_kw=runtime.flow.get("charge_pv_kw"),
                charge_grid_kw=runtime.flow.get("charge_grid_kw"),
            )
        )

    if states:
        await backend.upsert_hub_states(states)

    for health, count in health_counts.items():
        hubs_gauge.labels(health=health).set(count)
    for zone, flags in fresh_by_zone.items():
        ratio = sum(1 for f in flags if f) / len(flags) if flags else 1.0
        telemetry_fresh_ratio.labels(zone=zone).set(ratio)

    if failure is not None:
        raise failure
    return FlushStats(telemetry_rows=len(rows), hub_states=len(states))


#: Most rows of one kind held for a retry after failed writes (~50 s of telemetry at 2,000 hubs).
REQUEUE_MAX_ROWS = 50_000


def _requeue(buffer: list[Any], failed: list[Any], kind: str) -> None:
    """Put `failed` back ahead of rows buffered since, keeping at most `REQUEUE_MAX_ROWS` (newest)."""
    merged = failed + buffer
    dropped = max(0, len(merged) - REQUEUE_MAX_ROWS)
    if dropped:
        fleet_rows_dropped_total.labels(kind=kind).inc(dropped)
        logger.error(
            "fleet persistence backlog full; dropping oldest rows", extra={"kind": kind, "dropped": dropped}
        )
    buffer[:] = merged[dropped:]


async def ingest_scada_signal(payload: dict[str, Any]) -> None:
    """Store the latest simulated-utility SCADA bank reading (02b S6.2 `<root>/scada/<bank_id>`),
    quality flag included, for the allocator's `DIST_DEFERRAL` PI loop and `capability()`'s charge
    headroom to read. `fleet` stores/aggregates only -- it never runs the PI loop itself (02b S12).

    The reading is also buffered (latest per bank) for `flush` to persist to `og.feed_obs`, where
    `og-guardian`'s G-03 check and the `ALR-SCADA-OVERLOAD` alert read it from other processes. No I/O
    here: a per-message commit on the MQTT ingest path fell behind under load (live 2026-09-26) and
    delayed every hub's telemetry by minutes."""
    signal = ScadaBankSignal.model_validate(payload)
    # Keyed per (bank, signal): a bank reports several signals each tick (APPARENT_POWER_KVA and the signed
    # REAL_POWER_KW the guardian's reverse-flow checks need); keyed per bank only, the last one clobbered
    # the others and only one reached og.feed_obs.
    _bank_scada[(signal.bank_id, signal.signal)] = signal
    _pending_scada[(signal.bank_id, signal.signal)] = signal


def bank_scada_signal(bank_id: str, signal: str | None = None) -> ScadaBankSignal | None:
    """Latest stored SCADA reading of `signal` for `bank_id` (any signal, the newest, when `signal` is
    None), or `None` if none has arrived."""
    if signal is not None:
        return _bank_scada.get((bank_id, signal))
    readings = [s for (bank, _name), s in _bank_scada.items() if bank == bank_id]
    return max(readings, key=lambda s: s.ts) if readings else None


async def ingest_ack(payload: dict[str, Any]) -> None:
    """A3: record a hub's acknowledgement of a signed command batch (`<root>/ack/<hub_id>`). An accepted
    ack sets the hub's `last_command_id` (persisted with `hub_state`); every ack, accepted or rejected
    with its reason, is buffered for `flush` to write to `og.command_ack`. No I/O here (see
    `ingest_scada_signal`)."""
    ack = Ack.model_validate(payload)
    runtime = _hubs.get(ack.hub_id)
    if runtime is not None and ack.accepted:
        runtime.last_command_id = ack.batch_id
    _pending_acks.append(ack)


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
        classification = classify_hub_health(
            fault_code=runtime.fault_code,
            last_seen_at=runtime.last_seen_at,
            now=now,
            thresholds=_thresholds,
        )
        if classification != "online":
            excluded.add(hub_id)
            continue
        d_kw, c_kw = hub_capability(runtime.soc_kwh, runtime.params)
        discharge_kw.append(d_kw)
        charge_kw += c_kw

    max_discharge_kw = bank_capability(discharge_kw, bank_rt.params)

    scada = _bank_scada.get((bank_id, "APPARENT_POWER_KVA"))
    bank_load_kva = scada.value if scada is not None else 0.0
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


def rated_discharge_kw(bank_id: str) -> float:
    """The bank's STRUCTURAL discharge capability: every configured hub at its rated power, bounded by
    the bank's kVA rating (`core.physics.bank_capability`), regardless of health or SoC. What admission
    can ever hope for from this bank -- `capability()` is the live, transient figure. Raises
    `LookupError` for an unknown bank."""
    bank_rt = _banks.get(bank_id)
    if bank_rt is None:
        raise LookupError(f"unknown bank_id: {bank_id}")
    return bank_capability([_hubs[h].params.p_kw for h in bank_rt.hub_ids], bank_rt.params)


def known_hub_ids() -> list[str]:
    """Every hub the twin has topology for (e.g. og-engine's periodic PQ characterization pass)."""
    return list(_hubs.keys())


def known_bank_ids() -> list[str]:
    """Every bank the twin has topology for (merge task, dispatch-live pass): the engine's
    `FleetGateway.bank_ids()` adapter reads this so `opengrid.allocator.run_cycle` covers every real
    bank each 2 s cycle, not a hardcoded/guessed list."""
    return list(_banks.keys())


def bank_feeder(bank_id: str) -> str | None:
    """The bank's feeder (`og.bank.feeder_id`), for feeder-level flow limits (G-28, dispatch); None when
    the bank has no feeder mapping. Raises `LookupError` if `bank_id` is not a known bank."""
    bank_rt = _banks.get(bank_id)
    if bank_rt is None:
        raise LookupError(f"unknown bank_id: {bank_id}")
    return bank_rt.feeder_id


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
        classification = classify_hub_health(
            fault_code=runtime.fault_code,
            last_seen_at=runtime.last_seen_at,
            now=now,
            thresholds=_thresholds,
        )
        free_discharge_kw = 0.0
        soc_kwh: float | None = None
        reserve_kwh: float | None = None
        e_kwh: float | None = None
        p_kw: float | None = None
        if classification == "online":
            free_discharge_kw, _charge_kw = hub_capability(runtime.soc_kwh, runtime.params)
            soc_kwh = runtime.soc_kwh
            reserve_kwh = runtime.params.r_kwh
            e_kwh = runtime.params.e_kwh
            p_kw = runtime.p_kw
        flow = runtime.flow if classification == "online" else {}
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
                p_kw=p_kw,
                ramp_kw_per_s=hub_ramp_kw_per_s(runtime.params),
                rated_kw=runtime.params.p_kw,
                home_load_kw=flow.get("home_load_kw"),
                pv_kw=flow.get("pv_kw"),
                meter_kw=flow.get("meter_kw"),
                cell_temp_c=flow.get("cell_temp_c"),
                p_dis_max_kw=flow.get("p_dis_max_kw"),
                p_ch_max_kw=flow.get("p_ch_max_kw"),
                peak_power_budget_kws=flow.get("peak_power_budget_kws"),
                units=runtime.params.units,
                utility_scale=runtime.params.utility_scale,
            )
        )
    return snapshots
