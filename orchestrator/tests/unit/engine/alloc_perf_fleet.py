"""A synthetic production-shaped fleet driving the engine's allocator and energy_check phases in-process
(no Postgres, no MQTT), exactly as `opengrid.engine._engine_tick` calls them: `allocator.run_cycle` +
`allocator.hub_allocations()` (the `allocator` phase) and `EnergySufficiencyGateway.run` (the `energy_check`
phase), over the real engine gateways (`opengrid.engine.gateways`), the real fleet twin (`opengrid.fleet`,
loaded from an in-memory backend) and the real cycle extras (`EngineCycleExtras`, K15 territory and flow
limits on, the PQ context configured from the shipped service profiles) -- only the database is faked.

Shared by the perf-optimization equivalence test (`test_alloc_perf_equivalence.py`, the old code as the
oracle) and the benchmark (`tests-perf/bench_allocator.py`). Production proportions: 50 hubs per bank,
20 % dual-unit homes (`fleet.seed.build_topology`), 4/7 of the banks in the four ERCOT load zones and 1/7
each in LZ_AEN (regulated, contracted), LZ_LCRA and LZ_RAYBN (regulated, UNAVAILABLE, D-37), plus one
substation bank carrying the utility toll (D-29). About half of the available banks carry obligations
(ERCOT energy/AS holds and calls, partner capacity, home programs, distribution deferral, AEN regulated
capacity), some spanning several banks, some in SHORTFALL, some grandfathered on an unavailable bank.
"""

from __future__ import annotations

import contextlib
import copy
import random
import time
from collections.abc import AsyncIterator, Callable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

from opengrid import allocator, contracts, fleet, ledger
from opengrid.allocator.energy_sufficiency import EnergySufficiencyResult
from opengrid.allocator.models import CycleResult, FlowLimits
from opengrid.core.models.engine import Grant
from opengrid.core.models.platform import Bank, Hub, HubState
from opengrid.engine import gateways as gw
from opengrid.engine import pq_eligibility
from opengrid.engine.dispatch_extras import EngineCycleExtras
from opengrid.engine.flow_topology import (
    _BANK_FEEDER_SQL,
    _BANK_SUBSTATION_SQL,
    _FEEDER_SQL,
    _HUBS_SQL,
    _SUBSTATION_SQL,
    _XFMR_SQL,
    FlowTopology,
)
from opengrid.fleet.seed import SimFleetTopologyConfig, ZoneBlockConfig, build_topology
from opengrid.market.availability import GRANDFATHERED_SQL
from opengrid.market.model import load_market_model
from opengrid.platform.config import Config
from opengrid.trace.store import TraceStore

NOW = datetime(2026, 9, 27, 3, 0, 0, tzinfo=UTC)
_TARIFFS = Path(__file__).resolve().parents[3] / "config" / "tdsp_tariffs.toml"
HUBS_PER_BANK = 50
ERCOT_ZONES = ("LZ_NORTH", "LZ_SOUTH", "LZ_HOUSTON", "LZ_WEST")
SUBSTATION_BANK = "bank-sub-01"
LEASE_TTL_S = 30.0


# --------------------------------------------------------------------------------------------- fakes
class FakeCursor:
    """Answers each engine SQL statement from `routes` (unknown SQL: no rows); records every write."""

    def __init__(self, pool: FakePool) -> None:
        self._pool = pool
        self._rows: list[tuple[Any, ...]] = []

    async def execute(self, sql: str, params: Any = None) -> None:
        self._pool.executed.append((sql, params))
        self._rows = list(self._pool.routes.get(sql, []))

    async def executemany(self, sql: str, params_seq: Sequence[Any]) -> None:
        for params in params_seq:
            self._pool.executed.append((sql, params))
        self._rows = []

    async def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows

    async def __aenter__(self) -> FakeCursor:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


class FakeConn:
    def __init__(self, pool: FakePool) -> None:
        self._pool = pool

    def cursor(self) -> FakeCursor:
        return FakeCursor(self._pool)

    async def commit(self) -> None:
        self._pool.commits += 1

    async def __aenter__(self) -> FakeConn:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


class FakePool:
    def __init__(self, routes: dict[str, list[tuple[Any, ...]]]) -> None:
        self.routes = routes
        self.executed: list[tuple[str, Any]] = []
        self.commits = 0

    def connection(self) -> FakeConn:
        return FakeConn(self)


class FakeTraceBackend:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, Any]] = []

    async def last_head(self, stream_id: str) -> tuple[int, str | None]:
        return -1, None

    async def insert_trace_row(self, **kwargs: Any) -> None:
        self.rows.append((kwargs["stream_id"], kwargs["decision_type"], kwargs["payload"]))

    async def exists_preimage(self, decision_ref: Any) -> bool:
        return False

    async def fetch_range(self, *args: Any, **kwargs: Any) -> list[Any]:
        return []

    async def stream_ids(self) -> list[str]:
        return []

    async def insert_checkpoint(self, **kwargs: Any) -> None:
        return None

    async def prune_before(self, *args: Any, **kwargs: Any) -> int:
        return 0

    async def retention_days_for(self, event_class: str) -> int:
        return 400


class _FleetBackend:
    def __init__(self, hubs: list[Hub], banks: list[Bank], states: list[HubState]) -> None:
        self._hubs, self._banks, self._states = hubs, banks, states

    async def load_hubs(self) -> list[Hub]:
        return self._hubs

    async def load_banks(self) -> list[Bank]:
        return self._banks

    async def load_hub_states(self) -> list[HubState]:
        return self._states

    async def upsert_hub_states(self, states: list[HubState]) -> None:
        return None

    async def copy_telemetry(self, rows: list[Any]) -> None:
        return None

    async def insert_acks(self, acks: list[Any]) -> None:
        return None

    async def record_scada_observations(self, signals: list[Any]) -> None:
        return None


class _Clock:
    """The engine's wall clock in the harness: the simulated tick time (`set_clock`), frozen (equivalence:
    every read of a tick sees the same instant, so outputs are a function of the seed) or ticking (the
    benchmark: each read a later instant, as in production, so no read shares another's instant)."""

    base: datetime = NOW
    ticking: bool = False
    pc0: float = 0.0


def set_clock(at: datetime, *, ticking: bool = False) -> None:
    _Clock.base, _Clock.ticking, _Clock.pc0 = at, ticking, time.perf_counter()


class _FrozenDatetime(datetime):
    """`datetime` with `now()` from `_Clock` (patched into `opengrid.fleet` and `opengrid.engine.gateways`)."""

    @classmethod
    def now(cls, tz: Any = None) -> _FrozenDatetime:
        at = _Clock.base
        if _Clock.ticking:
            at = at + timedelta(seconds=time.perf_counter() - _Clock.pc0)
        return cls.fromtimestamp(at.timestamp(), tz=tz)


# ---------------------------------------------------------------------------------------- the fleet
@dataclass
class SyntheticFleet:
    n_hubs: int
    seed: int
    hubs: list[Hub]
    banks: list[Bank]
    states: list[HubState]
    routes: dict[str, list[tuple[Any, ...]]]
    unavailable: set[str]
    utility_scale: set[str]
    excluded_hub_ids: frozenset[str] = frozenset()
    operator_hub_ids: frozenset[str] = frozenset()
    device_excluded_hub_ids: frozenset[str] = frozenset()
    obligations: int = 0
    call_rows: int = 0
    banks_with_calls: int = 0


def _uuid(rng: random.Random) -> UUID:
    return UUID(int=rng.getrandbits(128), version=4)


def build_fleet(n_hubs: int, seed: int, *, load: int = 1) -> SyntheticFleet:
    """A seeded production-shaped fleet of ~`n_hubs` hubs (see the module docstring). `load` multiplies the
    obligations each called bank group carries and their spread (a heavy DELIVERING book: `load=4` puts
    ~4-12 obligations on a called bank, each over up to 16 banks)."""
    rng = random.Random(seed)  # noqa: S311 -- seeded test data, not security
    n_banks = max(n_hubs // HUBS_PER_BANK, 7)
    per_block = max(n_banks // 7, 1)
    base_banks = n_banks - 3 * per_block
    topo = build_topology(
        SimFleetTopologyConfig(
            hub_count=base_banks * HUBS_PER_BANK,
            bank_count=base_banks,
            zones=ERCOT_ZONES,
            zone_blocks=tuple(
                ZoneBlockConfig(zone=z, banks=per_block, homes_per_bank=HUBS_PER_BANK, enabled=True)
                for z in ("LZ_AEN", "LZ_LCRA", "LZ_RAYBN")
            ),
        )
    )
    hubs = list(topo.hubs)
    banks = list(topo.banks)
    # D-29: the substation's utility-scale set (rated at nameplate), one toll on it.
    banks.append(Bank(bank_id=SUBSTATION_BANK, zone="LZ_AEN", kva_rating=25_000.0, feeder_id=None))
    for i in range(10):
        hubs.append(
            Hub(
                hub_id=f"sub-01-{i:02d}",
                bank_id=SUBSTATION_BANK,
                zone="LZ_AEN",
                e_kwh=4_000.0,
                r_kwh=800.0,
                p_kw=2_000.0,
                units=1,
                utility_scale=True,
            )
        )
    zone_of_bank = {b.bank_id: b.zone for b in banks}
    unavailable = {b.bank_id for b in banks if b.zone in ("LZ_LCRA", "LZ_RAYBN")}

    # Live hub states: mostly online, a few stale/offline/fault; flow telemetry on most online hubs.
    states: list[HubState] = []
    for hub in hubs:
        roll = rng.random()
        age_s = 1.0 + rng.random() * 8.0
        fault = None
        if roll < 0.01:
            fault = "BMS_FAULT"
        elif roll < 0.03:
            age_s = 40.0  # stale
        elif roll < 0.05:
            age_s = 600.0  # offline
        elif roll < 0.08:
            age_s = 18.0 + 8.0 * rng.random()  # telemetry overdue: goes stale during the run
        low = rng.random() < 0.1
        soc = hub.r_kwh + (hub.e_kwh - hub.r_kwh) * (rng.random() * 0.08 if low else 0.2 + 0.8 * rng.random())
        flow = rng.random() < 0.9
        p_kw = -rng.random() * hub.p_kw * 0.5
        states.append(
            HubState(
                hub_id=hub.hub_id,
                soc_kwh=soc,
                p_kw=p_kw,
                last_seen_at=NOW - timedelta(seconds=age_s),
                fault_code=fault,
                meter_kw=(p_kw + rng.uniform(0.5, 6.0)) if flow else None,
                home_load_kw=rng.uniform(0.5, 6.0) if flow else None,
                cell_temp_c=rng.uniform(15.0, 42.0) if flow and rng.random() < 0.8 else None,
                p_dis_max_kw=hub.p_kw if flow and rng.random() < 0.3 else None,
            )
        )

    # Obligations: about half of the available banks carry calls.
    available_banks = sorted(
        b.bank_id for b in banks if b.bank_id not in unavailable and b.bank_id != SUBSTATION_BANK
    )
    called_banks = [b for b in available_banks if rng.random() < 0.5]
    # One drained bank carrying calls (every home a few % above reserve): L1 shortfalls and AT_RISK energy.
    if called_banks:
        drained = rng.choice(called_banks)
        params_of = {h.hub_id: h for h in hubs}
        states = [
            s.model_copy(
                update={
                    "soc_kwh": params_of[s.hub_id].r_kwh
                    + 0.03 * (params_of[s.hub_id].e_kwh - params_of[s.hub_id].r_kwh)
                }
            )
            if params_of[s.hub_id].bank_id == drained
            else s
            for s in states
        ]
    call_rows: list[tuple[Any, ...]] = []
    market_rows: list[tuple[Any, ...]] = []
    prior_rows: list[tuple[Any, ...]] = []
    energy_rows: list[tuple[Any, ...]] = []
    grandfathered: list[tuple[Any, ...]] = []
    obligations = 0

    def add_obligation(
        bank_ids: Sequence[str],
        service: str,
        tier: str,
        *,
        market: str,
        utility: str | None,
        kw_range: tuple[float, float],
        held: bool = False,
    ) -> None:
        nonlocal obligations
        obligations += 1
        oid = _uuid(rng)
        state = "SHORTFALL" if rng.random() < 0.05 else rng.choice(("DELIVERING", "DELIVERING", "COMMITTED"))
        deployed = not held
        duration = {"ERCOT_AS": rng.choice((60, 240)), "REGULATED_CAPACITY": 90}.get(service)
        deploy_end = NOW + timedelta(minutes=rng.randint(5, 60)) if (duration and deployed) else None
        total_kw = 0.0
        rows = []
        for bank_id in bank_ids:
            kw = rng.uniform(*kw_range)
            total_kw += kw
            rows.append((bank_id, kw))
        deploy_kw = total_kw * 0.6 if (duration and deployed and rng.random() < 0.3) else None
        window_end = NOW + timedelta(minutes=rng.choice((15, 30, 45, 60, 120)))
        for bank_id, kw in rows:
            call_rows.append(
                (
                    oid,
                    bank_id,
                    kw,
                    service,
                    tier,
                    rng.uniform(20.0, 120.0),
                    state,
                    bool(duration) and deployed,
                    duration,
                    deploy_end,
                    deploy_kw,
                )
            )
            remaining_h = (window_end - NOW).total_seconds() / 3600.0
            energy_rows.append(
                (
                    oid,
                    bank_id,
                    kw * remaining_h,
                    window_end,
                    _uuid(rng),
                    service,
                    kw if duration else None,
                    duration,
                    bool(duration) and deployed,
                    deploy_end,
                )
            )
        market_rows.append((oid, market, utility, service))
        if rng.random() < 0.7:
            prior_rows.append((oid, total_kw * rng.uniform(0.5, 1.0)))

    i = 0
    while i < len(called_banks):
        span = min(rng.choice((1, 1, 1, 2, 3, 4)) * load, len(called_banks) - i)
        group = called_banks[i : i + span]
        i += span
        zones = {zone_of_bank[b] for b in group}
        if zones == {"LZ_AEN"}:
            add_obligation(
                group,
                "REGULATED_CAPACITY",
                "T1",
                market="REGULATED",
                utility="AUSTIN_ENERGY",
                kw_range=(50.0, 250.0),
                held=rng.random() < 0.5,
            )
            continue
        for _ in range(rng.choice((1, 1, 2, 3)) * load):
            service, tier = rng.choice(
                (
                    ("ERCOT_ENERGY", "T3"),
                    ("ERCOT_ENERGY", "T3"),
                    ("ERCOT_AS", "T1"),
                    ("PARTNER_CAPACITY", "T2"),
                    ("HOME", "T4"),
                    ("DIST_DEFERRAL", "T2"),
                )
            )
            if "LZ_AEN" in zones and service != "HOME":
                service, tier = "HOME", "T4"
            add_obligation(
                group,
                service,
                tier,
                market="FREE",
                utility=None,
                # AS awards sized up to a 4 h full deployment beyond some banks' energy: AT_RISK cases.
                kw_range=(150.0, 600.0) if service == "ERCOT_AS" else (40.0, 320.0),
                held=service == "ERCOT_AS" and rng.random() < 0.7,
            )
    # D-29: the toll on the substation (held or called, per seed).
    add_obligation(
        [SUBSTATION_BANK],
        "REGULATED_CAPACITY",
        "T1",
        market="REGULATED",
        utility="AUSTIN_ENERGY",
        kw_range=(8_000.0, 25_000.0),
        held=rng.random() < 0.5,
    )
    # D-37: one grandfathered and one not on an UNAVAILABLE bank.
    for gf in (True, False):
        bank_id = rng.choice(sorted(unavailable))
        before = len(market_rows)
        add_obligation([bank_id], "ERCOT_ENERGY", "T3", market="FREE", utility=None, kw_range=(40.0, 120.0))
        if gf:
            grandfathered.append((market_rows[before][0], bank_id))
    # A PQ-sensitive profile (fail closed: no characterization yet) on one bank, some seeds.
    if called_banks and rng.random() < 0.5:
        add_obligation(
            [rng.choice(called_banks)],
            "DATA_CENTER",
            "T1",
            market="FREE",
            utility=None,
            kw_range=(40.0, 100.0),
        )

    # Flow-limit registry: a service transformer per ~5 homes, some export limits, feeder budgets.
    xfmr_rows: list[tuple[Any, ...]] = []
    hub_rows: list[tuple[Any, ...]] = []
    for idx, hub in enumerate(h for h in hubs if h.bank_id != SUBSTATION_BANK):
        xfmr = f"x-{hub.bank_id}-{idx // 5 % 10}"
        hub_rows.append((hub.hub_id, xfmr, 15.0 if rng.random() < 0.2 else None))
    for x in sorted({r[1] for r in hub_rows}):
        xfmr_rows.append((x, rng.choice((37.5, 50.0, 75.0))))
    feeders = sorted({b.feeder_id for b in banks if b.feeder_id})
    feeder_rows = [(f, rng.uniform(1_500.0, 4_000.0)) for f in feeders if rng.random() < 0.3]
    bank_feeder_rows = [(b.bank_id, b.feeder_id) for b in banks if b.feeder_id]

    zone_prices = [(z, rng.uniform(20.0, 180.0)) for z in (*ERCOT_ZONES, "LZ_AEN")]
    recharge = [(z, rng.uniform(10.0, 40.0)) for z in ERCOT_ZONES]
    conservative = [("BANK", rng.choice(available_banks))] if rng.random() < 0.3 else []

    routes: dict[str, list[tuple[Any, ...]]] = {
        gw._ACTIVE_CALLS_SQL: call_rows,
        gw._PRIOR_GRANTS_SQL: prior_rows,
        gw._CONTRACT_MARKETS_SQL: market_rows,
        GRANDFATHERED_SQL: grandfathered,
        gw._ENERGY_SUFFICIENCY_ROWS_SQL: energy_rows,
        gw._LATEST_ZONE_PRICES_SQL: zone_prices,
        gw._RECHARGE_PRICE_SQL: recharge,
        gw._CONSERVATIVE_SCOPES_SQL: conservative,
        _HUBS_SQL: hub_rows,
        _XFMR_SQL: xfmr_rows,
        _BANK_FEEDER_SQL: bank_feeder_rows,
        _BANK_SUBSTATION_SQL: [],
        _FEEDER_SQL: feeder_rows,
        _SUBSTATION_SQL: [],
    }
    online_ids = [
        s.hub_id for s in states if s.fault_code is None and s.last_seen_at > NOW - timedelta(seconds=20)
    ]
    return SyntheticFleet(
        n_hubs=len(hubs),
        seed=seed,
        hubs=hubs,
        banks=banks,
        states=states,
        routes=routes,
        unavailable=unavailable,
        utility_scale={SUBSTATION_BANK},
        excluded_hub_ids=frozenset(rng.sample(online_ids, k=min(3, len(online_ids)))),
        operator_hub_ids=frozenset(rng.sample(online_ids, k=1)) if rng.random() < 0.3 else frozenset(),
        device_excluded_hub_ids=frozenset(rng.sample(online_ids, k=min(2, len(online_ids)))),
        obligations=obligations,
        call_rows=len(call_rows),
        banks_with_calls=len({r[1] for r in call_rows}),
    )


# ------------------------------------------------------------------------------------- the engine
@dataclass
class CycleOutputs:
    """Everything one engine tick's allocator + energy_check phases produce, for byte comparison."""

    cycle_results: list[CycleResult] = field(default_factory=list)
    grants: list[list[Grant]] = field(default_factory=list)
    hub_allocations: list[dict[tuple[str, str], dict[str, float]]] = field(default_factory=list)
    shortfalls: list[list[tuple[str, str]]] = field(default_factory=list)
    energy: list[list[EnergySufficiencyResult]] = field(default_factory=list)
    as_hold_ids: list[set[str]] = field(default_factory=list)
    at_risk_flags: list[tuple[str, bool]] = field(default_factory=list)
    alerts: list[Any] = field(default_factory=list)
    trace_rows: list[tuple[str, str, Any]] = field(default_factory=list)
    writes: list[tuple[str, Any]] = field(default_factory=list)

    def fingerprint(self) -> str:
        """A canonical text of every output (`repr` keeps every float bit-exact)."""
        return repr(
            (
                self.cycle_results,
                [[g.model_dump() for g in gs] for gs in self.grants],
                self.hub_allocations,
                self.shortfalls,
                self.energy,
                [sorted(s) for s in self.as_hold_ids],
                self.at_risk_flags,
                [repr(a) for a in self.alerts],
                self.trace_rows,
                [(sql, params) for sql, params in self.writes if not str(sql).lstrip().startswith("SELECT")],
            )
        )


@dataclass
class EngineHarness:
    """The engine's gateways over one `SyntheticFleet`, plus the patched I/O seams."""

    fleet_gw: gw.EngineFleetGateway
    ledger_gw: gw.EngineLedgerGateway
    scada_gw: gw.EngineScadaGateway
    schedule_gw: gw.EngineScheduleGateway
    extras_gw: EngineCycleExtras
    energy_gw: gw.EnergySufficiencyGateway
    pool: FakePool
    trace_backend: FakeTraceBackend
    outputs: CycleOutputs


@contextlib.contextmanager
def _module_state_restored() -> Iterator[None]:
    """Leave `opengrid.fleet`'s twin and `opengrid.engine.pq_eligibility`'s profiles as they were (other tests
    rely on an unconfigured twin)."""
    scalars = {name: getattr(fleet, name) for name in _FLEET_SCALARS}
    containers = {name: copy.copy(getattr(fleet, name)) for name in _FLEET_CONTAINERS}
    pq_saved = {name: copy.copy(getattr(pq_eligibility, name)) for name in _PQ_STATE}
    try:
        yield
    finally:
        for name, value in scalars.items():
            setattr(fleet, name, value)
        for name, saved in containers.items():
            live = getattr(fleet, name)
            live.clear()
            if isinstance(live, dict):
                live.update(saved)
            else:
                live.extend(saved)
        for name, saved in pq_saved.items():
            live = getattr(pq_eligibility, name)
            if isinstance(live, dict):
                live.clear()
                live.update(saved)
            else:
                setattr(pq_eligibility, name, saved)


_FLEET_SCALARS = ("_backend", "_hub_offline_s", "_telemetry_interval_s", "_thresholds")
_FLEET_CONTAINERS = tuple(
    name
    for name in (
        "_hubs",
        "_banks",
        "_pending_telemetry",
        "_bank_scada",
        "_pending_scada",
        "_pending_acks",
        "_utility_instructions",
        "_capability_memo",
        "_snapshot_memo",
        "_health_memo",
    )
    if hasattr(fleet, name)
)
_PQ_STATE = ("_configs", "_state", "_phase_by_hub", "_candidates")


@contextlib.contextmanager
def _patched(target: Any, name: str, value: Any) -> Iterator[None]:
    old = getattr(target, name)
    setattr(target, name, value)
    try:
        yield
    finally:
        setattr(target, name, old)


def _reset_allocator_state() -> None:
    allocator._pi_states.clear()
    allocator._dwell_states.clear()
    allocator._last_grants.clear()
    allocator._last_hub_allocations.clear()
    allocator._last_extras.clear()
    allocator._last_shortfalls.clear()
    allocator._pre_cycle_pi.clear()
    allocator._pre_cycle_dwell.clear()
    getattr(allocator, "_prep_memo", {}).clear()


@contextlib.asynccontextmanager
async def engine_harness(sf: SyntheticFleet) -> AsyncIterator[EngineHarness]:
    """Load `sf` into the fleet twin and wire the engine's gateways over a fake pool (module state is
    reset on entry, so each harness starts like a freshly started og-engine)."""
    outputs = CycleOutputs()
    pool = FakePool(sf.routes)
    trace_backend = FakeTraceBackend()
    trace = TraceStore(trace_backend)

    async def _ledger_version() -> int:
        return 7

    async def _persist(cycle_id: str, records: list[Any]) -> None:
        return None

    async def _set_at_risk(obligation_id: Any, at_risk: bool, **kwargs: Any) -> None:
        outputs.at_risk_flags.append((str(obligation_id), at_risk))

    async def _raise_alert(pool_: Any, finding: Any, *, opened_at: Any) -> int:
        outputs.alerts.append(finding)
        return 1

    async def _open_alerts(pool_: Any, rule: str) -> list[dict[str, Any]]:
        return []

    async def _clear_alerts(pool_: Any, rule: str, matches: Callable[[dict[str, Any]], bool]) -> int:
        return 0

    real_cycle = allocator.cycle

    def _recording_cycle(*args: Any, **kwargs: Any) -> CycleResult:
        result = real_cycle(*args, **kwargs)
        outputs.cycle_results.append(result)
        return result

    with contextlib.ExitStack() as stack:
        stack.enter_context(_module_state_restored())
        for target, name, value in (
            (fleet, "datetime", _FrozenDatetime),
            (gw, "datetime", _FrozenDatetime),
            (ledger, "ledger_version", _ledger_version),
            (ledger, "persist_grants", _persist),
            (contracts, "set_obligation_at_risk", _set_at_risk),
            (gw, "raise_alert", _raise_alert),
            (gw, "open_alert_details", _open_alerts),
            (gw, "clear_open_alerts", _clear_alerts),
            (allocator, "cycle", _recording_cycle),
        ):
            stack.enter_context(_patched(target, name, value))
        fleet.configure(
            _FleetBackend(sf.hubs, sf.banks, sf.states),
            Config(
                {
                    "health": {"hub_stale_s": 25.0, "hub_offline_s": 300.0},
                    "fleet": {"telemetry_interval_s": 10.0},
                }
            ),
        )
        await fleet.load_topology()
        gw.set_unavailable_banks(set(sf.unavailable))
        gw.set_utility_scale_banks(set(sf.utility_scale))
        pq_eligibility.configure(pq_eligibility.load_profile_configs())
        _reset_allocator_state()
        market = load_market_model(banks=[(b.bank_id, b.zone) for b in sf.banks], config_path=_TARIFFS)
        fleet_gw, ledger_gw, scada_gw, schedule_gw = gw.build_gateways(pool, trace, market_model=market)  # type: ignore[arg-type]
        flow = FlowLimits(enabled=True, default_export_limit_kw=20.0)
        extras_gw = EngineCycleExtras(
            trace,
            pq_context=pq_eligibility.dispatch_context,
            enforce_territory=True,
            flow_limits=flow,
            flow_topology=FlowTopology(pool, flow),  # type: ignore[arg-type]
            excluded_hub_ids=lambda: sf.excluded_hub_ids,
            operator_hub_ids=lambda: sf.operator_hub_ids,
            device_excluded_hub_ids=lambda: sf.device_excluded_hub_ids,
        )
        energy_gw = gw.EnergySufficiencyGateway(pool, trace)  # type: ignore[arg-type]
        try:
            yield EngineHarness(
                fleet_gw, ledger_gw, scada_gw, schedule_gw, extras_gw, energy_gw, pool, trace_backend, outputs
            )
        finally:
            _reset_allocator_state()
            gw.set_unavailable_banks(set())
            gw.set_utility_scale_banks(set())


async def allocator_phase(h: EngineHarness, now: datetime, cycle_id: str) -> list[Grant]:
    """The engine's `allocator` phase, verbatim (`engine._engine_tick`)."""
    grants = await allocator.run_cycle(
        cycle_id,
        fleet=h.fleet_gw,
        ledger=h.ledger_gw,
        scada_gateway=h.scada_gw,
        schedule_gateway=h.schedule_gw,
        extras_gateway=h.extras_gw,
        now=now,
        lease_ttl_s=LEASE_TTL_S,
    )
    hub_allocations = allocator.hub_allocations()
    h.outputs.grants.append(grants)
    h.outputs.hub_allocations.append(hub_allocations)
    h.outputs.shortfalls.append(list(h.ledger_gw.last_shortfalls))
    return grants


async def energy_check_phase(h: EngineHarness, now: datetime) -> list[EnergySufficiencyResult]:
    """The engine's `energy_check` phase, verbatim (`engine._engine_tick`)."""
    results = await h.energy_gw.run(now)
    h.outputs.energy.append(results)
    h.outputs.as_hold_ids.append(set(h.energy_gw.as_hold_ids))
    return results


def ingest_churn(seed: int, n: int, period: int = 5) -> None:
    """What telemetry ingest does before tick `n` (10 s publish interval, 2 s cycle: every hub reports every
    `period` ticks): the twin's live hubs whose turn it is get a new SoC, power, flow reading and last-seen
    time -- new objects, exactly like `fleet.ingest_telemetry`. Stale/offline/faulted hubs stay silent."""
    rng = random.Random(seed * 1_000 + n)  # noqa: S311 -- seeded test data, not security
    now = NOW + timedelta(seconds=2 * n)
    for index, hub_id in enumerate(sorted(fleet._hubs)):
        runtime = fleet._hubs[hub_id]
        if (index + n) % period or runtime.fault_code is not None:
            continue
        if runtime.last_seen_at is None or runtime.last_seen_at < NOW - timedelta(seconds=20):
            continue
        params = runtime.params
        runtime.soc_kwh = min(max(runtime.soc_kwh + rng.uniform(-0.3, 0.1), 0.0), params.e_kwh)
        runtime.p_kw = -rng.random() * params.p_kw * 0.5
        runtime.last_seen_at = now - timedelta(seconds=rng.random())
        flow = dict(runtime.flow)
        if flow.get("meter_kw") is not None:
            flow["meter_kw"] = runtime.p_kw + rng.uniform(0.5, 6.0)
        runtime.flow = flow


async def run_cycles(h: EngineHarness, cycles: int = 3, *, churn_seed: int | None = None) -> CycleOutputs:
    """`cycles` engine ticks 2 s apart (allocator, then energy_check) on a frozen per-tick clock, collecting
    every output; with `churn_seed`, telemetry arrives between ticks (`ingest_churn`)."""
    for n in range(cycles):
        now = NOW + timedelta(seconds=2 * n)
        set_clock(now)
        if churn_seed is not None and n > 0:
            ingest_churn(churn_seed, n)
        await allocator_phase(h, now, f"{int(now.timestamp())}-{n + 1}")
        await energy_check_phase(h, now)
    h.outputs.trace_rows = list(h.trace_backend.rows)
    h.outputs.writes = list(h.pool.executed)
    return h.outputs
