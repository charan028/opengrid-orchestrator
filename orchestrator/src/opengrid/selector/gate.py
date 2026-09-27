"""Gate orchestration (02a S3.1/S3.8): assembles `ModelInputs`, solves Mode O, validates, falls back to
F2, persists the `plan` row, and transitions opportunities through `contracts`/`ledger`.

Each I/O step is a small, separately named async function so tests can monkeypatch exactly the seam
they need (BUILD.md "use fakes for siblings") without touching the pure `model`/`solve`/`extract`/
`validate`/`rule_fallback` core, which is tested directly with hand-built `ModelInputs`.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import multiprocessing
import os
import time
import tomllib
from collections.abc import Callable, Collection, Mapping, Sequence
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, get_args
from uuid import UUID, uuid4

from opengrid import forecast, ledger
from opengrid.core import geo
from opengrid.core.charge_windows import Topology, in_windows, resolve
from opengrid.core.models.engine import Plan
from opengrid.core.models.market import ERCOT_COMPETITIVE, Utility, UtilityId
from opengrid.core.physics import DEFAULT_ETA_C, DEFAULT_ETA_D
from opengrid.core.reasons import R_DEGRADED_NO_NEW_COMMIT
from opengrid.core.services import (
    DATA_CENTER_SERVICE_TYPE,
    DIST_DEFERRAL_SERVICE_TYPE,
    ERCOT_AS_SERVICE_TYPE,
    ERCOT_ENERGY_SERVICE_TYPE,
    HOME_SERVICE_TYPE,
    LARGE_LOAD_SERVICE_TYPE,
    MOBILE_STORAGE_SERVICE_TYPE,
    PARTNER_CAPACITY_SERVICE_TYPE,
    PIPELINE_AC_SERVICE_TYPE,
    PJM_CAPACITY_SERVICE_TYPE,
    REGULATED_CAPACITY_SERVICE_TYPE,
)
from opengrid.core.solar_share import SolarShare
from opengrid.core.timeutil import floor_to_interval, to_market_tz
from opengrid.fleet import bank_feeder as fleet_bank_feeder
from opengrid.fleet import capability as fleet_capability
from opengrid.fleet import hub_capabilities as fleet_hub_capabilities
from opengrid.fleet import rated_discharge_kw as fleet_rated_discharge_kw
from opengrid.health.model import HealthThresholds
from opengrid.market import (
    MarketModel,
    MarketModelError,
    MarketRef,
    load_market_model,
    market_of,
)
from opengrid.market.availability import (
    grandfathered_banks_by_obligation,
    parse_availability,
    unavailable_bank_ids,
)
from opengrid.market.capacity import capacity_value_usd_per_mwh
from opengrid.market.territory import utility_of_territory
from opengrid.platform.config import load_config
from opengrid.selector import db, energy_value, solar_history
from opengrid.selector.commit import (
    commit_candidate,
    reject_structurally_infeasible,
    selected_kw_by_interval_key,
)
from opengrid.selector.extract import extract_plan
from opengrid.selector.model import build_mode_o_model
from opengrid.selector.rule_fallback import rule_fallback_f2
from opengrid.selector.solve import PRICE_OF_FIRMNESS_TIME_LIMIT_S, highs_solve
from opengrid.selector.types import (
    BankSnapshot,
    CandidateOpportunity,
    CommittedObligation,
    ExtractedPlan,
    GateKind,
    ModelInputs,
    ScenarioPrice,
    solver_settings_for,
)
from opengrid.selector.validate import validate_plan
from opengrid.selector.value import shadow_comparison

logger = logging.getLogger(__name__)

INTERVAL_MINUTES = 15.0
SCHEDULED_HORIZON_INTERVALS = 96  # 24h / 15min, 02a S3.2

# 02a S3.7's "firm first, then AS, then market" F2 priority bucket, keyed by `contract.service_type`
# (`CandidateOpportunity.category`'s docstring). `ERCOT_ENERGY` (spot-like) falls back to `MARKET`.
# Every `core.models.engine.ServiceType` value must be here (tested).
_CATEGORY_BY_SERVICE_TYPE: dict[str, Literal["FIRM", "AS", "MARKET"]] = {
    HOME_SERVICE_TYPE: "FIRM",
    DIST_DEFERRAL_SERVICE_TYPE: "FIRM",
    PARTNER_CAPACITY_SERVICE_TYPE: "FIRM",
    DATA_CENTER_SERVICE_TYPE: "FIRM",  # firm bridging capacity (06-service-profiles S4.b)
    PIPELINE_AC_SERVICE_TYPE: "FIRM",
    REGULATED_CAPACITY_SERVICE_TYPE: "FIRM",  # regulated market: selected first, in stage R (09 D3)
    PJM_CAPACITY_SERVICE_TYPE: "MARKET",  # a simulated ISO capacity market (SERVICES agent)
    MOBILE_STORAGE_SERVICE_TYPE: "FIRM",
    LARGE_LOAD_SERVICE_TYPE: "FIRM",
    ERCOT_AS_SERVICE_TYPE: "AS",
    ERCOT_ENERGY_SERVICE_TYPE: "MARKET",
}

#: Capacity-hold services outside the AS category: a regulated capacity commitment is a need-basis
#: reservation (09 D9) -- kW locked, energy held for its sustain duration, nothing drained while held.
_CAPACITY_HOLD_SERVICE_TYPES = frozenset({REGULATED_CAPACITY_SERVICE_TYPE})

#: 09 S1.3 psi: expected share of a held ERCOT_AS award actually deployed, for its wear (D8). A planning
#: ASSUMPTION (~30 min/day for Non-Spin/ECRS) until settle measures deployment from og.as_deployment.
AS_EXPECTED_DEPLOYMENT_SHARE = 0.02

#: Full-deployment duration for an ERCOT_AS award whose product rule has none (ECRS 1 h, the shortest).
DEFAULT_AS_HOLD_MINUTES = 60.0
#: D-29(a): a utility toll (REGULATED_CAPACITY / TOLLING) is a 90-min product: 0 kW until called
#: (DISPATCH), with 90 min of its reserved kW held as energy. Used when its product rule has no duration.
DEFAULT_TOLL_HOLD_MINUTES = 90.0

#: 09 S1.2 c^deg by asset class, $ per AC kWh discharged: homes $0.03 (A-DE-16). Every `og.bank` is a
#: home bank today; substation assets ($0.015, OQ-15) join with the asset registry.
HOME_BANK_WEAR_USD_PER_KWH = 0.03


def as_energy_hold_h(service_type: object, duration_minutes: object) -> float:
    """Energy-hold hours for the selector's SoC model: an ERCOT_AS award is a capacity hold that must be
    deployable for its product's full duration (Non-Spin 4 h, ECRS 1 h, NPRR1282), and so is a regulated
    capacity commitment (09 D9 need basis); 0 for every other service (those discharge their profile).
    Keyed on the service's AS category, so any AS service type added to `_CATEGORY_BY_SERVICE_TYPE` is
    held too (from ftbrown's #13)."""
    if not isinstance(service_type, str):
        return 0.0
    if (
        _CATEGORY_BY_SERVICE_TYPE.get(service_type) != "AS"
        and service_type not in _CAPACITY_HOLD_SERVICE_TYPES
    ):
        return 0.0
    default = (
        DEFAULT_TOLL_HOLD_MINUTES if service_type in _CAPACITY_HOLD_SERVICE_TYPES else DEFAULT_AS_HOLD_MINUTES
    )
    minutes = float(str(duration_minutes)) if duration_minutes else default
    return minutes / 60.0


def expected_deployment_share(service_type: object) -> float:
    """psi for a held award of `service_type`: the AS assumption for ERCOT_AS; 0 otherwise (a regulated
    need-basis reserve has no deployment statistic yet)."""
    if isinstance(service_type, str) and _CATEGORY_BY_SERVICE_TYPE.get(service_type) == "AS":
        return AS_EXPECTED_DEPLOYMENT_SHARE
    return 0.0


# Simple warm-start memory: previous gate's selection, shifted one interval by the caller if needed
# (02a S3.7 "previous plan shifted one interval"). Kept in-process only -- a restart just solves cold.
_last_hint_x: dict[str, float] = {}
_last_hint_q: dict[str, float] = {}

# The solve runs in ONE long-lived solver process, not a worker thread: model build, validation and
# price-of-firmness are pure Python and held the GIL for seconds per gate, so every await of og-engine's
# 2 s dispatch tick queued behind them (A11, live 2026-09-26). `spawn`, not `fork`: the engine process
# has an event loop, a DB pool and threads.
_solver_pool: ProcessPoolExecutor | None = None


def _get_solver_pool() -> ProcessPoolExecutor:
    global _solver_pool
    if _solver_pool is None:
        _solver_pool = ProcessPoolExecutor(max_workers=1, mp_context=multiprocessing.get_context("spawn"))
    return _solver_pool


def shutdown_solver_process() -> None:
    """Stop the solver process (tests, orderly shutdown); the next solve starts a fresh one."""
    global _solver_pool
    if _solver_pool is not None:
        _solver_pool.shutdown(wait=True, cancel_futures=True)
        _solver_pool = None


R_PQ_ELIGIBLE_CAPACITY = "R-PQ-ELIGIBLE-CAPACITY"
#: `opengrid.health.model.DegradedMode` value that freezes new selection.
NO_NEW_COMMITMENTS = "NO_NEW_COMMITMENTS"

#: `(service_type, bank_id) -> eligible kW` for PQ-sensitive profiles (`None` for other services); wired by
#: og-engine to `opengrid.engine.pq_eligibility.eligible_kw`. Unset: no PQ cap (tests, tools).
_pq_capacity: Callable[[str, str], float | None] | None = None


def configure_pq_capacity(provider: Callable[[str, str], float | None] | None) -> None:
    global _pq_capacity
    _pq_capacity = provider


def exceeds_pq_eligible_capacity(candidate: CandidateOpportunity, selected_kw: dict[str, Decimal]) -> bool:
    """True if any (bank, interval) of the selection asks more than the bank's PQ-eligible kW for the
    candidate's (PQ-sensitive) service type."""
    if _pq_capacity is None or not candidate.service_type:
        return False
    for key, kw in selected_kw.items():
        bank_id, _start, _end = ledger.decode_interval_key(key)
        cap = _pq_capacity(candidate.service_type, bank_id)
        if cap is not None and float(kw) > cap + 1e-9:
            return True
    return False


#: Wall-clock allowance on top of HiGHS's own time limits: model build, validation and IPC.
SOLVER_BUDGET_MARGIN_S = 60.0


class SolverTimeoutError(RuntimeError):
    """The solver process overran its hard budget and was recycled; the gate is failed (K7)."""

    reason_code = "R-SOLVER-TIMEOUT"


def solver_budget_s(gate_kind: GateKind) -> float:
    """Hard budget for one gate's solve: HiGHS's time limit, the price-of-firmness re-solve's own
    limit, and a margin for model build/validation -- strictly above what a healthy solve can take."""
    return (
        solver_settings_for(gate_kind).time_limit_s + PRICE_OF_FIRMNESS_TIME_LIMIT_S + SOLVER_BUDGET_MARGIN_S
    )


def _kill_solver_pool() -> None:
    """Terminate the solver worker(s) (a hung HiGHS run cannot be cancelled) and drop the pool."""
    global _solver_pool
    pool, _solver_pool = _solver_pool, None
    if pool is None:
        return
    for process in list(getattr(pool, "_processes", {}).values()):
        process.terminate()
    pool.shutdown(wait=False, cancel_futures=True)


async def run_in_solver_process[T](fn: Callable[..., T], *args: Any) -> T:
    return await asyncio.get_running_loop().run_in_executor(_get_solver_pool(), fn, *args)


async def solve_off_loop(
    inputs: ModelInputs,
    gate_kind: GateKind,
    horizon_start: datetime,
    x_hint: dict[str, float],
    q_hint: dict[str, float],
) -> ExtractedPlan:
    """`solve_gate` in the solver process, under a hard wall-clock budget (review #12). If that process
    has died it is replaced and this solve runs in a thread instead (K7: a gate is never lost to a
    crashed worker); if it overruns the budget it is killed, the pool is recycled and the gate fails
    (`SolverTimeoutError` -> `ALR-SELECTOR-GATE-FAILED`), rather than wedging every later gate."""
    global _solver_pool
    budget_s = solver_budget_s(gate_kind)
    try:
        return await asyncio.wait_for(
            run_in_solver_process(solve_gate, inputs, gate_kind, horizon_start, x_hint, q_hint),
            timeout=budget_s,
        )
    except TimeoutError as exc:
        logger.error(
            "selector solve overran its budget; recycling the solver process", extra={"budget_s": budget_s}
        )
        _kill_solver_pool()
        raise SolverTimeoutError(f"{gate_kind} solve exceeded {budget_s:.0f} s") from exc
    except BrokenProcessPool:
        logger.warning("selector solver process died; solving this gate in a thread")
        _solver_pool = None
        return await asyncio.to_thread(solve_gate, inputs, gate_kind, horizon_start, x_hint, q_hint)


async def compute_horizon(gate_kind: GateKind, now: datetime) -> tuple[datetime, datetime]:
    """24h horizon starting at the 15-min interval containing `now` (02a S3.2: fixed 96 market
    intervals). Aligning is what makes interval keys comparable across gates: reservations (K2) and
    commitments (C24) of an earlier gate are only found by an exact interval-start match.
    `RENOMINATION` narrows to the obligation's own window in `run_gate` (02a S3.1), so this returns the
    same default for all three gate kinds."""
    start = floor_to_interval(now, int(INTERVAL_MINUTES))
    return start, start + timedelta(hours=24)


def _bank_energy_envelope(bank_id: str) -> tuple[float, float, float, float, float]:
    """Live per-bank energy envelope from the fleet twin (user requirement: "energy above reserve
    must be checked continuously" -- the selector must use the LIVE initial SoC per bank from the
    twin, not a guessed/zero one). Aggregates `fleet.hub_capabilities(bank_id)` (already exposing
    `soc_kwh`/`reserve_kwh`/`e_kwh`/`eta_d` per hub for exactly this purpose, per that function's own
    docstring) over the hubs currently reporting live state (`soc_kwh is not None`, i.e. "online" this
    instant) -- never over every configured hub regardless of health, which would silently invent an
    unobserved SoC for an offline/stale hub.

    Deliberately hardware-agnostic (no per-service or per-hub-model special-casing): whatever mix of
    hub sizes a bank actually has (e.g. 39.2 kWh/11 kW single-unit vs 78.4 kWh/20 kW 2-unit homes) is
    summed as reported by the twin, never assumed.

    Returns `(capacity_kwh, reserve_kwh, initial_soc_kwh, eta_c, eta_d)`. When no hub on this bank is
    currently online, returns `(0.0, 0.0, 0.0, DEFAULT_ETA_C, DEFAULT_ETA_D)` -- `BankSnapshot.capacity_kwh
    <= 0` is the documented sentinel for "no energy envelope this cycle", which cleanly skips SoC
    modeling for the bank (K7 degrade, don't trip) instead of pinning `soc == initial_soc` against
    `[reserve_kwh, capacity_kwh]` bounds computed from a *different, larger* hub set than the live
    reading -- which would make the model spuriously `INFEASIBLE_F1` under a partial/total fleet outage
    (confirmed live 2026-09-25/26: every hub across all 40 banks went "offline" during an `og-engine`
    restart loop)."""
    hubs = fleet_hub_capabilities(bank_id)
    online = [h for h in hubs if h.soc_kwh is not None and h.reserve_kwh is not None and h.e_kwh is not None]
    if not online:
        return 0.0, 0.0, 0.0, DEFAULT_ETA_C, DEFAULT_ETA_D
    capacity_kwh = sum(h.e_kwh for h in online if h.e_kwh is not None)
    reserve_kwh = sum(h.reserve_kwh for h in online if h.reserve_kwh is not None)
    initial_soc_kwh = sum(h.soc_kwh for h in online if h.soc_kwh is not None)
    eta_d = sum(h.eta_d for h in online) / len(online)
    return capacity_kwh, reserve_kwh, initial_soc_kwh, DEFAULT_ETA_C, eta_d


async def load_banks(
    horizon_start: datetime, horizon_end: datetime, bank_ids: tuple[str, ...]
) -> tuple[BankSnapshot, ...]:
    """Bank discharge-capability snapshot via `fleet.capability` (02b S4), one call per bank/interval,
    plus the bank's live energy envelope (`_bank_energy_envelope`, once per bank -- current SoC/
    capacity/reserve are a live-now reading, not something that varies per future horizon interval)."""
    n_intervals = int((horizon_end - horizon_start).total_seconds() // (INTERVAL_MINUTES * 60))
    snapshots = []
    for bank_id in bank_ids:
        # `fleet.capability` is a live-now reading (it ignores `interval_start`), so it is read once per
        # bank and applied to every horizon interval -- 96 identical reads per bank took seconds of the
        # event loop per gate (A11). The charge envelope was never passed before, so the model could
        # never recharge a bank (live 2026-09-26: every plan RULE_FALLBACK).
        cap = await fleet_capability(bank_id, horizon_start)
        by_interval = dict.fromkeys(range(n_intervals), cap.max_discharge_kw)
        charge_by_interval = dict.fromkeys(range(n_intervals), cap.max_charge_kw)
        capacity_kwh, reserve_kwh, initial_soc_kwh, eta_c, eta_d = _bank_energy_envelope(bank_id)
        snapshots.append(
            BankSnapshot(
                bank_id=bank_id,
                max_discharge_kw=by_interval,
                max_charge_kw=charge_by_interval,
                capacity_kwh=capacity_kwh,
                reserve_kwh=reserve_kwh,
                initial_soc_kwh=initial_soc_kwh,
                eta_c=eta_c,
                eta_d=eta_d,
            )
        )
    return tuple(snapshots)


@dataclass(frozen=True, slots=True)
class BankMarketTerms:
    """A bank's market position for one gate (09 D1/D2/D5/D8, D-22, D-28), applied onto its snapshot."""

    zone: str | None
    territory: str | None
    wear_usd_per_kwh: float
    delivery_charge_usd_per_kwh: float
    charge_price_usd_per_kwh: dict[int, float]
    free_market_access: bool
    solar_cost_usd_per_kwh: float | None = None
    grid_charge_intervals: frozenset[int] | None = None
    solar_shares: Mapping[int, SolarShare] = dataclasses.field(default_factory=dict)


#: Owner decisions D-29(c) / D-30: a utility-toll bank charges from the grid on the OWNER's schedule (or
#: from local solar), not at the utility's direction. Windows are `"HH:MM-HH:MM"` (America/Chicago, may
#: wrap midnight), per utility or per bank (a bank's own entry wins), from, in order:
#:   1. `og.owner_charge_window` (edited on the Fleet page; FOLLOWUPS' migration 0038);
#:   2. config `[selector.owner_charge_windows]` (`{utility_id or bank_id = [...]}`);
#:   3. the built-in default `DEFAULT_OWNER_CHARGE_WINDOWS` (D-30: 22:00-06:00).
OWNER_CHARGE_WINDOWS_KEY = "selector.owner_charge_windows"
DEFAULT_OWNER_CHARGE_WINDOWS: tuple[str, ...] = ("22:00-06:00",)
#: The DB windows are re-read at most this often (every gate in practice: gates run every 15 min).
OWNER_CHARGE_WINDOWS_CACHE_S = 60.0


def owner_charge_intervals(
    windows: Sequence[str], horizon_start: datetime, n_intervals: int
) -> frozenset[int]:
    """The horizon intervals whose local (America/Chicago) start falls in any owner window, by the one
    D-30 definition (`core.charge_windows.in_windows`). A malformed window raises `ChargeWindowError`
    (a ValueError: never silently allow or bar charging); an empty list means no grid charging."""
    return frozenset(
        t
        for t in range(n_intervals)
        if in_windows(windows, to_market_tz(horizon_start + timedelta(minutes=INTERVAL_MINUTES * t)).time())
    )


def config_owner_charge_windows() -> dict[str, list[str]]:
    """`[selector.owner_charge_windows]` from the orchestrator config; {} when unset or unreadable."""
    try:
        raw = load_config().get(OWNER_CHARGE_WINDOWS_KEY, {}) or {}
    except Exception:
        logger.warning("selector: orchestrator config unreadable; no configured owner charge windows")
        return {}
    return {str(key): [str(w) for w in value] for key, value in dict(raw).items()}


#: One owner charge window set, keyed by (scope_kind, scope_ref) as in `og.owner_charge_window`.
ScopedWindows = Mapping[tuple[str, str], Sequence[str]]


def config_windows_as_scopes(config: Mapping[str, Sequence[str]]) -> dict[tuple[str, str], list[str]]:
    """Config keys are a utility id (PROVIDER scope) or a bank id (BANK scope)."""
    utility_ids = set(get_args(UtilityId))
    return {
        ("PROVIDER" if key in utility_ids else "BANK", str(key)): list(value) for key, value in config.items()
    }


def resolve_owner_charge_windows(
    scoped: ScopedWindows,
    *,
    bank_id: str,
    feeder: str | None,
    zone: str | None,
    provider: str | None,
    substation: str | None = None,
    hub_id: str | None = None,
) -> Sequence[str] | None:
    """D-30: the windows of the most specific scope with a row (`core.charge_windows.resolve`, the ONE
    resolver the API uses too): HUB > BANK > FEEDER > SUBSTATION > ZONE > PROVIDER > FLEET. None: no row
    at any scope (the caller applies the built-in default). The plan is per bank, so HUB applies only
    where the caller names the bank's single hub; SUBSTATION only where the bank's substation is known."""
    effective = resolve(
        scoped,
        Topology(
            hub_id=hub_id,
            bank_id=bank_id,
            feeder_id=feeder,
            substation_id=substation,
            zone=zone,
            provider=provider,
        ),
    )
    return None if effective is None else list(effective.windows)


_owner_windows_cache: tuple[float, dict[tuple[str, str], list[str]]] | None = None


async def load_owner_charge_windows() -> dict[tuple[str, str], list[str]]:
    """D-30 owner charge windows for this gate: `og.owner_charge_window`'s rows over the config's, re-read
    at most every `OWNER_CHARGE_WINDOWS_CACHE_S`. A missing table (0038 not applied) or DB error is config
    only; `bank_market_terms` applies the built-in default where no scope has a window."""
    global _owner_windows_cache
    now = time.monotonic()
    if _owner_windows_cache is not None and now - _owner_windows_cache[0] < OWNER_CHARGE_WINDOWS_CACHE_S:
        return _owner_windows_cache[1]
    try:
        db_rows = await db.load_owner_charge_window_rows()
    except Exception:
        logger.warning("selector: og.owner_charge_window unreadable; config/default windows", exc_info=True)
        db_rows = {}
    merged = config_windows_as_scopes(config_owner_charge_windows())
    merged.update(db_rows)
    _owner_windows_cache = (now, merged)
    return merged


def _bank_feeder(bank_id: str) -> str | None:
    try:
        return fleet_bank_feeder(bank_id)
    except LookupError:
        return None


def clear_owner_charge_windows_cache() -> None:
    """Forget the cached windows (tests; an operator edit shows at the next gate after 60 s anyway)."""
    global _owner_windows_cache
    _owner_windows_cache = None


#: D-31 home-station registry (SERVICES), config-first until its DB table lands.
MOBILE_HOME_STATIONS_FILE = Path("service_profiles") / "mobile_storage_home_stations.toml"


def resolve_mobile_home_stations_path() -> Path:
    """Next to `OG_CONFIG` (as `tdsp_tariffs.toml`), else this checkout's `orchestrator/config`."""
    og_config = os.environ.get("OG_CONFIG")
    base = Path(og_config).parent if og_config else Path(__file__).resolve().parents[3] / "config"
    return base / MOBILE_HOME_STATIONS_FILE


def parse_mobile_home_stations(raw: Mapping[str, Any]) -> dict[str, str]:
    """`bank_id -> home station zone` for every `[[assignment]]` (joined to its `[[home_station]]`). An
    assignment to an unknown station raises: never silently treat a truck as a fleet bank (D-31)."""
    zone_by_station = {str(s["home_station_id"]): str(s["zone"]) for s in raw.get("home_station", [])}
    out: dict[str, str] = {}
    for assignment in raw.get("assignment", []):
        station = str(assignment["home_station_id"])
        if station not in zone_by_station:
            raise ValueError(
                f"mobile unit {assignment['bank_id']} assigned to unknown home station {station}"
            )
        out[str(assignment["bank_id"])] = zone_by_station[station]
    return out


def parse_mobile_home_station_sites(raw: Mapping[str, Any]) -> dict[str, tuple[float, float]]:
    """`unit id -> (lat, lon)` of its home station, keyed by every `[[assignment]]`'s `bank_id` and, when
    given, its `hub_id` (a truck's hub id differs from its `bank-<id>` bank id). Same join and same
    unknown-station error as `parse_mobile_home_stations`."""
    parse_mobile_home_stations(raw)  # validates every assignment's station
    site_by_station = {
        str(s["home_station_id"]): (float(s["lat"]), float(s["lon"])) for s in raw.get("home_station", [])
    }
    out: dict[str, tuple[float, float]] = {}
    for assignment in raw.get("assignment", []):
        site = site_by_station[str(assignment["home_station_id"])]
        out[str(assignment["bank_id"])] = site
        if assignment.get("hub_id"):
            out[str(assignment["hub_id"])] = site
    return out


def _load_mobile_registry() -> dict[str, Any] | None:
    path = resolve_mobile_home_stations_path()
    if not path.exists():
        return None
    with path.open("rb") as fh:
        return tomllib.load(fh)


def load_mobile_units() -> dict[str, str]:
    """D-31 mobile units and their home-station zones. No file = no mobile units; a malformed file
    raises (a config error fails the gate loudly)."""
    raw = _load_mobile_registry()
    return {} if raw is None else parse_mobile_home_stations(raw)


def load_mobile_home_station_sites() -> dict[str, tuple[float, float]]:
    """D-31 home-station coordinates per mobile unit (`parse_mobile_home_station_sites`), for G-35's
    at-home check. No file = no mobile units; a malformed file raises."""
    raw = _load_mobile_registry()
    return {} if raw is None else parse_mobile_home_station_sites(raw)


async def load_bank_zones(bank_ids: Sequence[str]) -> dict[str, str]:
    """Each bank's zone: `og.bank.zone`, except a mobile unit's, which is its HOME STATION's (D-31: it
    charges there, at that station's zone/tariff; `og.bank.zone` is meaningless for a relocating unit)."""
    zones = await db.load_bank_zones(list(bank_ids)) if bank_ids else {}
    mobile = load_mobile_units()
    return {bank_id: mobile.get(bank_id, zone) for bank_id, zone in zones.items()}


def mark_mobile_units(
    banks: tuple[BankSnapshot, ...],
    mobile: Mapping[str, str],
    at_home: Mapping[str, bool] | None = None,
    n_intervals: int = 0,
) -> tuple[BankSnapshot, ...]:
    """Flag the mobile units (D-31) and set where each may charge. A unit parked at its home station now
    (`at_home[bank_id] is True`, `mobile_units_at_home`) may charge in every horizon interval: there is no
    deployment schedule yet (the requested `og.mobile_deployment`), so "at home now" is taken to hold for
    the horizon, and the guardian's G-35 re-checks the position on every dispatch (a truck that leaves is
    never charged away from home). Any other unit -- away, or position unknown -- gets None: no charging at
    all (fail closed). Grid charging is further limited to the owner charge window (`bank_market_terms`)."""
    homes = at_home or {}
    everywhere = frozenset(range(n_intervals))
    return tuple(
        dataclasses.replace(
            bank,
            is_mobile=True,
            home_station_intervals=everywhere if homes.get(bank.bank_id) is True else None,
        )
        if bank.bank_id in mobile
        else bank
        for bank in banks
    )


def mobile_units_at_home(
    mobile_bank_ids: Sequence[str],
    sites: Mapping[str, tuple[float, float]],
    positions: Mapping[str, tuple[float, float]],
) -> dict[str, bool]:
    """Pure: which mobile units are at their home station now, by the one D-31 rule G-35 also uses
    (`core.geo.at_home_station`, 250 m): the unit's fresh device-reported position (`db.load_hub_positions`)
    vs its station's coordinates. Only units known to be at home are True; away, or a missing or stale
    report, is False (fail closed)."""
    return {
        bank_id: geo.at_home_station(positions.get(bank_id), sites.get(bank_id)) is True
        for bank_id in mobile_bank_ids
    }


def _hub_stale_s() -> float | None:
    """`[health].hub_stale_s` for the stationary position rule (`core.geo.fresh_positions`); None (report
    age only, stricter) when the config is unreadable."""
    try:
        return float(HealthThresholds.from_config(load_config()).hub_stale_s)
    except Exception:
        logger.warning(
            "selector: config unreadable; mobile positions judged on report age only", exc_info=True
        )
        return None


async def load_mobile_units_at_home(mobile_bank_ids: Sequence[str]) -> dict[str, bool]:
    """`mobile_units_at_home` over the registry's station coordinates and each unit's fresh device-reported
    position (never the seeded `og.hub.lat/lon`, which is the home station). A failed position read plans
    no mobile charging this gate (fail closed), never a crash."""
    if not mobile_bank_ids:
        return {}
    try:
        positions = await db.load_hub_positions(list(mobile_bank_ids), telemetry_max_age_s=_hub_stale_s())
    except Exception:
        logger.warning(
            "selector: mobile unit positions unreadable; no mobile charging this gate", exc_info=True
        )
        return dict.fromkeys(mobile_bank_ids, False)
    return mobile_units_at_home(mobile_bank_ids, load_mobile_home_station_sites(), positions)


async def load_market(bank_ids: tuple[str, ...]) -> tuple[MarketModel, dict[str, str]]:
    """The gate's `opengrid.market.MarketModel` (territories, utilities, TDSP tariffs from
    `tdsp_tariffs.toml`) over the banks' zones (`load_bank_zones`), plus that zone map. A missing tariffs
    file raises (config error, the gate fails loudly): without the territory table a regulated zone would
    be priced as the competitive market."""
    zone_by_bank = await load_bank_zones(bank_ids)
    return load_market_model(banks=list(zone_by_bank.items())), zone_by_bank


def _owner_grid_intervals(
    windows: ScopedWindows,
    bank_id: str,
    feeder_by_bank: Mapping[str, str | None] | None,
    zone: str,
    provider: str | None,
    horizon_start: datetime,
    n_intervals: int,
) -> frozenset[int]:
    """The bank's owner charge-window intervals (D-29 c / D-30): its resolved schedule, else the default."""
    schedule = resolve_owner_charge_windows(
        windows, bank_id=bank_id, feeder=(feeder_by_bank or {}).get(bank_id), zone=zone, provider=provider
    )
    return owner_charge_intervals(
        schedule if schedule is not None else DEFAULT_OWNER_CHARGE_WINDOWS, horizon_start, n_intervals
    )


def bank_market_terms(
    market: MarketModel,
    zone_by_bank: Mapping[str, str],
    bank_ids: Sequence[str],
    horizon_start: datetime,
    n_intervals: int,
    solar_shares: Mapping[str, Mapping[int, SolarShare]] | None = None,
    owner_charge_windows: ScopedWindows | None = None,
    feeder_by_bank: Mapping[str, str | None] | None = None,
    mobile: Collection[str] = frozenset(),
) -> dict[str, BankMarketTerms]:
    """Pure: each bank's territory, M1 and charging terms from the `MarketModel` (the single owner of
    the territory predicate and the charging-cost model; M1 resolves through `settle.tariffs`).

    - ERCOT competitive area: grid kWh at the zone price + the TDSP's M1 (`MarketModel.charging_cost`'s
      `delivery_usd_per_kwh`; the zone price itself is per scenario, so it is added in the model); PV
      surplus at its forgone export credit, no M1.
    - Regulated territory (Austin Energy, CPS Energy; utility-toll banks): grid kWh at the utility's grid
      rate, in the owner's charge windows (D-29 c, `owner_charge_windows`; unrestricted without one);
      solar kWh at the utility's solar price, as available (D-28 measured share); no M1; no FREE
      headroom unless the utility granted wholesale access (K15 b).
    - Unknown zone or territory: no FREE headroom and (`prepare_obligations`) no obligation: K15 fails
      closed.

    - Mobile units (`mobile`, D-31 trucks; their zone is the HOME STATION's, `load_bank_zones`): priced as
      above at the station's zone/tariff, grid charging only in the owner charge window (D-30) in either
      market, and no behind-the-meter solar in a competitive zone (a depot has no PV). Where they may
      charge at all (only at the home station) is `BankSnapshot.home_station_intervals`
      (`mark_mobile_units`); they are never charged from fleet assets (D-31, `validate.check_mobile_storage`).

    `solar_shares` is each bank's D-28 measured share per interval (`solar_history.planned_shares`)."""
    shares = solar_shares or {}
    windows = owner_charge_windows or {}
    out: dict[str, BankMarketTerms] = {}
    for bank_id in bank_ids:
        zone = zone_by_bank.get(bank_id)
        territory = market.territory_of_bank(bank_id)
        delivery = 0.0
        charge_price: dict[int, float] = {}
        solar_cost: float | None = None
        grid_intervals: frozenset[int] | None = None
        if (
            territory is None
            or zone is None
            or (territory != ERCOT_COMPETITIVE and utility_of_territory(territory) is None)
        ):
            # Unknown, or NOIE (a co-op/municipal zone that is not our customer): serves neither market.
            logger.warning(
                "selector: bank has no usable territory; it serves nothing this gate (K15 fail-closed)",
                extra={"bank_id": bank_id, "zone": zone, "territory": territory},
            )
        elif territory == ERCOT_COMPETITIVE:
            cost = market.charging_cost(zone, horizon_start, wholesale_usd_per_kwh=Decimal("0"))
            if cost.tariff_ref.endswith("M1-NONE"):
                logger.warning("selector: no TDSP tariff for zone; M1 priced at 0", extra={"zone": zone})
            delivery = float(cost.delivery_usd_per_kwh)
            if bank_id in mobile:
                # D-30/D-31: a truck grid-charges at its depot only in the owner charge window, also in a
                # competitive zone (priced at the station's zone + its TDSP's M1, as any competitive bank).
                grid_intervals = _owner_grid_intervals(
                    windows, bank_id, feeder_by_bank, zone, None, horizon_start, n_intervals
                )
        else:
            for t in range(n_intervals):
                cost = market.charging_cost(
                    zone, horizon_start + timedelta(minutes=INTERVAL_MINUTES * t), solar_share=Decimal("0")
                )
                charge_price[t] = float(cost.grid_energy_usd_per_kwh)
                solar_cost = float(cost.solar_usd_per_kwh)
            grid_intervals = _owner_grid_intervals(
                windows,
                bank_id,
                feeder_by_bank,
                zone,
                utility_of_territory(territory),
                horizon_start,
                n_intervals,
            )
        # A truck's depot has no behind-the-meter PV: in a competitive zone it charges from the grid only.
        # In a regulated territory the utility's contract solar (priced at its solar rate) still applies.
        no_btm_pv = bank_id in mobile and territory == ERCOT_COMPETITIVE
        out[bank_id] = BankMarketTerms(
            zone=zone,
            territory=territory,
            wear_usd_per_kwh=HOME_BANK_WEAR_USD_PER_KWH,
            delivery_charge_usd_per_kwh=delivery,
            charge_price_usd_per_kwh=charge_price,
            free_market_access=market.free_access(territory),
            solar_cost_usd_per_kwh=solar_cost,
            grid_charge_intervals=grid_intervals,
            solar_shares={} if no_btm_pv else shares.get(bank_id, {}),
        )
    return out


async def load_solar_shares(
    zone_by_bank: Mapping[str, str], horizon_start: datetime, n_intervals: int, now: datetime
) -> dict[str, dict[int, SolarShare]]:
    """D-28 measured solar share per bank and planned interval: sample this gate's fleet telemetry into
    the trailing-week history, then the zone's same-hour measured share, else ERCOT's same-hour solar
    share (guarded: an absent feed or a DB error is simply no ERCOT source), else the 30% assumption."""

    def _hubs(bank_id: str) -> list[Any]:
        try:
            return list(fleet_hub_capabilities(bank_id))
        except LookupError:
            return []

    solar_history.sample_fleet(zone_by_bank, now, _hubs)
    try:
        ercot = await db.load_ercot_solar_share_by_hour()
    except Exception:
        logger.warning("ERCOT solar share unreadable; measured share or the 30% assumption", exc_info=True)
        ercot = {}
    starts = [(t, horizon_start + timedelta(minutes=INTERVAL_MINUTES * t)) for t in range(n_intervals)]
    by_zone = {
        zone: solar_history.planned_shares(zone, starts, now, ercot) for zone in set(zone_by_bank.values())
    }
    return {bank_id: by_zone[zone] for bank_id, zone in zone_by_bank.items()}


def apply_market_terms(
    banks: tuple[BankSnapshot, ...], terms: Mapping[str, BankMarketTerms]
) -> tuple[BankSnapshot, ...]:
    """Copy each bank's market terms onto its snapshot (banks without terms are left as they are). The
    solar charging available per interval is the measured share of the bank's charge envelope (D-28)."""
    out = []
    for bank in banks:
        term = terms.get(bank.bank_id)
        if term is None:
            out.append(bank)
            continue
        shares = term.solar_shares
        out.append(
            dataclasses.replace(
                bank,
                zone=term.zone,
                territory=term.territory,
                wear_usd_per_kwh=term.wear_usd_per_kwh,
                delivery_charge_usd_per_kwh=term.delivery_charge_usd_per_kwh,
                charge_price_usd_per_kwh=term.charge_price_usd_per_kwh,
                free_market_access=term.free_market_access,
                solar_cost_usd_per_kwh=term.solar_cost_usd_per_kwh,
                grid_charge_intervals=term.grid_charge_intervals,
                solar_charge_kw={
                    t: float(s.share) * bank.max_charge_kw.get(t, 0.0)
                    for t, s in shares.items()
                    if s.share > 0
                },
                solar_share={t: float(s.share) for t, s in shares.items()},
                solar_share_source={t: s.source for t, s in shares.items()},
            )
        )
    return tuple(out)


def _market_ref(market: str, utility_id: str | None) -> MarketRef | None:
    try:
        return market_of(market=market, utility_id=utility_id)
    except MarketModelError:
        return None


def _eligible(market: MarketModel, ref: MarketRef | None, bank_ids: tuple[str, ...]) -> tuple[str, ...]:
    """K15 (09 C25 a/b) through `MarketModel.bank_eligible` (the one predicate)."""
    if ref is None:
        return ()
    return tuple(b for b in bank_ids if market.bank_eligible(b, ref))


def prepare_obligations(
    candidates: tuple[CandidateOpportunity, ...],
    committed: tuple[CommittedObligation, ...],
    market: MarketModel,
) -> tuple[tuple[CandidateOpportunity, ...], tuple[CommittedObligation, ...]]:
    """Apply the market model to the gate's obligations.

    - K15: every obligation's eligible banks narrow to those its market allows -- a REGULATED obligation
      only its utility's territory, a FREE one only the competitive area (and regulated banks whose
      utility granted wholesale access). A committed obligation keeps whatever banks remain (C24
      substitutes among them; with none left the validator reports the K13 shortfall and F2 takes over).
    - A regulated candidate without a recorded value is valued at its utility's capacity price
      (`regulated_value_per_mwh`), so stage R (09 D3) has something to maximise."""
    narrowed_candidates = []
    for c in candidates:
        ref = _market_ref(c.market, c.utility_id)
        value = c.value_per_mwh
        if ref is not None and ref.is_regulated and value <= 0.0 and ref.utility_id is not None:
            value = regulated_value_per_mwh(market.utility(ref.utility_id))
        narrowed_candidates.append(
            dataclasses.replace(
                c, eligible_bank_ids=_eligible(market, ref, c.eligible_bank_ids), value_per_mwh=value
            )
        )
    narrowed_committed = []
    for co in committed:
        eligible = _eligible(market, _market_ref(co.market, co.utility_id), co.eligible_bank_ids)
        if not eligible:
            logger.error(
                "committed obligation has no territory-eligible bank (K15)",
                extra={"obligation_id": co.obligation_id, "market": co.market, "utility_id": co.utility_id},
            )
        narrowed_committed.append(dataclasses.replace(co, eligible_bank_ids=eligible))
    return tuple(narrowed_candidates), tuple(narrowed_committed)


@dataclass(frozen=True, slots=True)
class GateAvailability:
    """D-37 bank availability for one gate: the UNAVAILABLE banks, and the K13-grandfathered
    `(obligation -> banks)` pairs (`opengrid.market.availability`, the one rule)."""

    unavailable: frozenset[str] = frozenset()
    grandfathered: Mapping[str, frozenset[str]] = dataclasses.field(default_factory=dict)


async def load_availability() -> GateAvailability:
    """`og.bank.availability` and the grandfathered pairs. An unreadable table raises: the gate fails
    loudly rather than plan on banks whose availability it cannot see (migration 0046 is a precondition)."""
    rows = await db.load_bank_availability()
    pairs = await db.load_grandfathered_pairs()
    return GateAvailability(
        unavailable=unavailable_bank_ids(parse_availability(b, a, r, s) for b, a, r, s in rows),
        grandfathered=grandfathered_banks_by_obligation(pairs),
    )


def apply_availability(
    banks: tuple[BankSnapshot, ...],
    candidates: tuple[CandidateOpportunity, ...],
    committed: tuple[CommittedObligation, ...],
    availability: GateAvailability,
    bank_ids: Sequence[str],
) -> tuple[tuple[BankSnapshot, ...], tuple[CandidateOpportunity, ...], tuple[CommittedObligation, ...]]:
    """D-37: nothing is offered or planned on an UNAVAILABLE bank.

    - Candidates (new commitments, ERCOT or utility): never eligible there.
    - Committed obligations: never newly placed there, but a K13-grandfathered obligation keeps the
      unavailable banks it already holds (and only those), even where K15 (`prepare_obligations`) removed
      them: it was committed while the zone was ERCOT competitive and completes untouched.
    - The bank itself: no FREE headroom, no charging (idle hold: no grid window, no solar, 0 kW charge
      envelope), and no discharge envelope unless a grandfathered obligation is on it."""
    unavailable = availability.unavailable
    if not unavailable:
        return banks, candidates, committed
    order = {b: i for i, b in enumerate(bank_ids)}
    out_candidates = tuple(
        dataclasses.replace(
            c, eligible_bank_ids=tuple(b for b in c.eligible_bank_ids if b not in unavailable)
        )
        for c in candidates
    )
    out_committed = []
    for co in committed:
        kept = {b for b in co.eligible_bank_ids if b not in unavailable}
        kept |= {b for b in availability.grandfathered.get(co.obligation_id, frozenset()) if b in order}
        out_committed.append(
            dataclasses.replace(co, eligible_bank_ids=tuple(sorted(kept, key=lambda b: order.get(b, 0))))
        )
    carrying = {b for banks_of in availability.grandfathered.values() for b in banks_of}
    out_banks = []
    for bank in banks:
        if bank.bank_id not in unavailable:
            out_banks.append(bank)
            continue
        out_banks.append(
            dataclasses.replace(
                bank,
                free_market_access=False,
                grid_charge_intervals=frozenset(),
                solar_charge_kw={},
                max_charge_kw=dict.fromkeys(bank.max_charge_kw, 0.0),
                max_discharge_kw=(
                    bank.max_discharge_kw
                    if bank.bank_id in carrying
                    else dict.fromkeys(bank.max_discharge_kw, 0.0)
                ),
            )
        )
    return tuple(out_banks), out_candidates, tuple(out_committed)


async def new_commitments_allowed() -> bool:
    """02b S6.5 row 1: False while health's NO_NEW_COMMITMENTS mode is active (a feed crossed STALE).
    An unreadable mode counts as active (fail closed): no new commitment on data that may be stale."""
    try:
        return NO_NEW_COMMITMENTS not in await db.load_degraded_modes()
    except Exception:
        logger.exception(
            "degraded-mode state unreadable; no new commitments this gate (fail closed)",
            extra={"reason_code": R_DEGRADED_NO_NEW_COMMIT},
        )
        return False


def withhold_unfit_series(
    candidates: tuple[CandidateOpportunity, ...],
    banks: tuple[BankSnapshot, ...],
    scenarios: tuple[ScenarioPrice, ...],
    unfit_series: frozenset[str],
) -> tuple[CandidateOpportunity, ...]:
    """02b S6.5: no selection priced off a series forecast flags `NOT_FOR_FIRM` (its feed is STALE or its
    history too short). Each candidate loses the banks priced off such a series: a bank in that zone, and
    -- while any series is unfit -- a bank with no zone path of its own (it is priced at the fleet mean,
    which includes the unfit series). The candidate stays selectable on the remaining banks."""
    if not unfit_series:
        return candidates
    priced_off_unfit = {
        bank.bank_id
        for bank in banks
        if (bank.zone is not None and bank.zone in unfit_series)
        or not _has_zone_path(scenarios, bank.bank_id)
    }
    out = []
    for c in candidates:
        kept = tuple(b for b in c.eligible_bank_ids if b not in priced_off_unfit)
        if len(kept) < len(c.eligible_bank_ids):
            logger.warning(
                "candidate withheld from banks priced off a NOT_FOR_FIRM series",
                extra={"obligation_id": c.obligation_id, "reason_code": R_DEGRADED_NO_NEW_COMMIT},
            )
        out.append(dataclasses.replace(c, eligible_bank_ids=kept))
    return tuple(out)


async def freeze_new_selection(
    candidates: tuple[CandidateOpportunity, ...],
    banks: tuple[BankSnapshot, ...],
    scenarios: tuple[ScenarioPrice, ...],
    horizon_start: datetime,
    horizon_end: datetime,
) -> tuple[CandidateOpportunity, ...]:
    """The candidates this gate may still select (02b S6.5). Under NO_NEW_COMMITMENTS none: they stay
    OFFERED, nothing is reserved, committed obligations keep delivering (K13 is unaffected by feed
    staleness). Otherwise each loses the banks priced off an unfit series (`withhold_unfit_series`); an
    unreadable fitness flag withholds every candidate (fail closed)."""
    if not candidates:
        return candidates
    if not await new_commitments_allowed():
        logger.warning(
            "NO_NEW_COMMITMENTS active: the gate selects nothing new",
            extra={"reason_code": R_DEGRADED_NO_NEW_COMMIT, "candidates_withheld": len(candidates)},
        )
        return ()
    try:
        unfit = await db.load_unfit_price_series(horizon_start, horizon_end)
    except Exception:
        logger.exception(
            "forecast firm-fitness unreadable; no new commitments this gate (fail closed)",
            extra={"reason_code": R_DEGRADED_NO_NEW_COMMIT},
        )
        return ()
    return withhold_unfit_series(candidates, banks, scenarios, unfit)


def regulated_value_per_mwh(utility: Utility) -> float:
    """The utility's capacity price as $ per MWh-held (`market.capacity.capacity_value_usd_per_mwh`, the
    one owner), so the objective's `value x kW x dt` term pays exactly the pro-rated capacity payment
    (09 S1.5 stage R)."""
    if utility.capacity_price_usd_per_kw is None:
        logger.warning(
            "regulated utility has no capacity price; valued at 0", extra={"utility": utility.utility_id}
        )
    return float(capacity_value_usd_per_mwh(utility.capacity_price_usd_per_kw, utility.payment_basis))


async def load_scenarios(
    horizon_start: datetime, horizon_end: datetime, bank_ids: tuple[str, ...] = ()
) -> tuple[ScenarioPrice, ...]:
    """P10/P50/P90 price scenarios via `forecast.scenarios` (02b S3), per bank zone.

    Architect finding (a): this took the LAST row per interval regardless of series or kind -- load-
    forecast rows (MW) were folded into the price path and every bank was priced at whichever zone came
    last (LZ_WEST). Now only `kind == "price"` points count; each bank gets its own load zone's path,
    and the fleet path (for a bank with no zone path) is the mean of the zones."""
    points = await forecast.scenarios(horizon_start, horizon_end)
    zone_by_bank = await load_bank_zones(bank_ids)
    scenarios = scenarios_from_points(points, horizon_start, zone_by_bank)
    unpriced = sorted(
        {zone for bank_id, zone in zone_by_bank.items() if not _has_zone_path(scenarios, bank_id)}
    )
    if unpriced:
        # e.g. LZ_AEN/LZ_CPS banks before those zones are in `[fleet].zones` (forecast's price series).
        logger.warning(
            "selector: no price forecast for these bank zones; their banks use the fleet mean path",
            extra={"zones": unpriced},
        )
    return scenarios


def _has_zone_path(scenarios: tuple[ScenarioPrice, ...], bank_id: str) -> bool:
    return bool(scenarios) and all(bank_id in s.price_by_bank for s in scenarios)


def scenarios_from_points(
    points: Sequence[Any], horizon_start: datetime, zone_by_bank: dict[str, str]
) -> tuple[ScenarioPrice, ...]:
    """Pure part of `load_scenarios`: group price points into per-zone paths per scenario."""
    by_zone: dict[str, dict[str, dict[int, float]]] = {}  # scenario -> zone -> t -> price
    probability_by_scenario: dict[str, float] = {}
    for point in points:
        if point.kind != "price":
            continue
        t = int((point.interval_start - horizon_start).total_seconds() // (INTERVAL_MINUTES * 60))
        by_zone.setdefault(point.scenario, {}).setdefault(point.series_key, {})[t] = float(point.value)
        probability_by_scenario[point.scenario] = point.probability
    result = []
    for name, zones in by_zone.items():
        fleet_path: dict[int, float] = {}
        for t in sorted({t for path in zones.values() for t in path}):
            values = [path[t] for path in zones.values() if t in path]
            fleet_path[t] = sum(values) / len(values)
        result.append(
            ScenarioPrice(
                scenario=name,  # type: ignore[arg-type]
                probability=probability_by_scenario[name],
                price_usd_per_mwh=fleet_path,
                price_by_bank={
                    bank_id: dict(zones[zone]) for bank_id, zone in zone_by_bank.items() if zone in zones
                },
            )
        )
    return tuple(result)


async def load_committed(
    horizon_start: datetime, horizon_end: datetime, bank_ids: tuple[str, ...]
) -> tuple[CommittedObligation, ...]:
    """Committed/delivering obligations overlapping the horizon, frozen per `db.load_frozen_commitments`
    (02a S2.2). Each starts with every configured bank as a redistribution candidate (bank
    *substitution*, not a reduction -- see `types.CommittedObligation`); the gate then narrows that set
    to the banks its market allows (K15, `prepare_obligations`). No `contracts`/`ledger` eligibility
    query narrows it further per obligation yet."""
    frozen = await db.load_frozen_commitments(horizon_start.isoformat(), horizon_end.isoformat())
    terms = await db.load_obligation_terms([str(o) for o in frozen])
    n_intervals = int((horizon_end - horizon_start).total_seconds() // (INTERVAL_MINUTES * 60))
    interval_index_by_iso = {
        (horizon_start + timedelta(minutes=INTERVAL_MINUTES * t)).isoformat(): t for t in range(n_intervals)
    }
    result = []
    for obligation_id, by_interval_iso in frozen.items():
        by_index = {
            interval_index_by_iso[iso]: kw
            for iso, kw in by_interval_iso.items()
            if iso in interval_index_by_iso
        }
        if by_index:
            term = terms.get(str(obligation_id), {})
            value = term.get("value_per_mwh")
            result.append(
                CommittedObligation(
                    obligation_id=str(obligation_id),
                    eligible_bank_ids=bank_ids,
                    committed_kw_by_interval=by_index,
                    energy_hold_h=as_energy_hold_h(term.get("service_type"), term.get("duration_minutes")),
                    expected_deployment_share=expected_deployment_share(term.get("service_type")),
                    service_type=str(term.get("service_type") or ""),
                    value_per_mwh=float(value) if value is not None else 0.0,
                    market="REGULATED" if term.get("market") == "REGULATED" else "FREE",
                    utility_id=term.get("utility_id"),
                )
            )
    return tuple(result)


async def load_candidates(
    horizon_start: datetime, horizon_end: datetime, bank_ids: tuple[str, ...], contract_scope: UUID | None
) -> tuple[CandidateOpportunity, ...]:
    """`OFFERED` opportunities for the horizon (`ADMISSION`/`RENOMINATION` narrow via `contract_scope`),
    read from `og.opportunity`/`og.contract`/`og.product_rule` via `selector.db` (that module's own
    docstring already scopes selector to read those tables read-only). `contracts` exposes no public
    opportunity-listing query beyond `admit`/`product_rules_for` (INTERFACES.md's fixed four) -- see the
    module's final-report note asking the merge agent to add one there instead, so this reads the
    tables directly rather than staying a permanent placeholder.

    Each candidate starts with every configured bank; the gate narrows that set afterwards -- K15
    territory (`prepare_obligations`), NOT_FOR_FIRM price series (`withhold_unfit_series`) -- and the
    model applies D-31's mobile-unit rule (`ModelInputs.may_serve`). No `contracts`/`ledger` query
    narrows it per opportunity beyond those (`load_committed`'s docstring notes the same for committed
    obligations)."""
    rows = await db.load_offered_opportunities_rows(
        horizon_start.isoformat(), horizon_end.isoformat(), contract_scope
    )
    n_intervals = int((horizon_end - horizon_start).total_seconds() // (INTERVAL_MINUTES * 60))
    candidates = []
    for row in rows:
        window_start = max(row["window_start"], horizon_start)
        window_end = min(row["window_end"], horizon_end)
        start_t = int((window_start - horizon_start).total_seconds() // (INTERVAL_MINUTES * 60))
        end_t = int((window_end - horizon_start).total_seconds() // (INTERVAL_MINUTES * 60))
        window_intervals = tuple(range(max(start_t, 0), min(end_t, n_intervals)))
        if not window_intervals:
            continue  # window does not actually overlap this horizon after clamping/rounding
        try:
            ref = market_of(
                market=row.get("market"), utility_id=row.get("utility_id"), service_type=row["service_type"]
            )
        except MarketModelError:
            logger.warning(
                "opportunity with an inconsistent market is not offered to the selector (K15 fail-closed)",
                extra={"opportunity_id": str(row["opportunity_id"])},
            )
            continue
        # A regulated candidate with no recorded value is priced from its utility in `prepare_obligations`.
        value_per_mwh = float(row["value_per_mwh"]) if row["value_per_mwh"] is not None else 0.0
        candidates.append(
            CandidateOpportunity(
                opportunity_id=str(row["opportunity_id"]),
                obligation_id=str(row["obligation_id"]),
                contract_id=str(row["contract_id"]),
                eligible_bank_ids=bank_ids,
                window_intervals=window_intervals,
                requested_kw=float(row["requested_kw"]),
                value_per_mwh=value_per_mwh,
                variable_kind=row["variable_kind"] or "CONTINUOUS",
                min_qty_kw=float(row["min_qty_kw"] or 0.0),
                increment_kw=float(row["increment_kw"] or 0.0),
                degradation_cost_per_kwh=float(row["degradation_cost"] or 0.03),
                tier=row["tier"] or "T4",
                category=_CATEGORY_BY_SERVICE_TYPE.get(row["service_type"], "MARKET"),
                service_type=str(row["service_type"] or ""),
                energy_hold_h=as_energy_hold_h(row["service_type"], row.get("duration_minutes")),
                expected_deployment_share=expected_deployment_share(row["service_type"]),
                market=ref.market,
                utility_id=ref.utility_id,
            )
        )
    return tuple(candidates)


def _plan_mode_for(gate_kind: GateKind, horizon_start: datetime) -> str:
    """02a S3.1: `L-DA` labels the one daily scheduled solve whose 24 h horizon is exactly an ERCOT
    operating day, i.e. starts at 00:00 America/Chicago (issue #43 A11: it fired at 00:00 UTC, 7 PM
    CT). It seeds the operating day; it is not a DAM run -- MVP-S submits no DAM offers, and ERCOT's
    DAM for that day closed at 10:00 CT the day before."""
    local_start = to_market_tz(horizon_start)
    if gate_kind == "SCHEDULED_15MIN" and local_start.hour == 0 and local_start.minute < INTERVAL_MINUTES:
        return "L-DA"
    return "L-ID"


async def persist_plan(
    plan_mode: str,
    gate_kind: GateKind,
    horizon_start: datetime,
    horizon_end: datetime,
    scenarios: tuple[ScenarioPrice, ...],
    result: ExtractedPlan,
) -> UUID:
    """Insert the `og.plan` row (02a S1.8) and return its id."""
    plan_id = uuid4()
    pool = await db.get_pool()
    scenario_set = json.dumps([{"scenario": s.scenario, "prob": s.probability} for s in scenarios])
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO og.plan (plan_id, plan_mode, gate_kind, horizon_start, horizon_end,
                                  scenario_set, solver_status, solver_gap, solver_time_ms, objective_value)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                plan_id,
                plan_mode,
                gate_kind,
                horizon_start,
                horizon_end,
                scenario_set,
                result.solver_status,
                result.solver_gap,
                result.solver_time_ms,
                Decimal(str(result.objective_value)),
            ),
        )
    return plan_id


def solve_gate(
    inputs: ModelInputs,
    gate_kind: GateKind,
    horizon_start: datetime,
    x_hint: dict[str, float] | None = None,
    q_hint: dict[str, float] | None = None,
) -> ExtractedPlan:
    """The solver core (02a S3.8, no I/O): build, solve, validate, fall back to F2 if needed. Exercised
    directly by unit/property tests against hand-built `ModelInputs`; `run_gate` wraps it with the
    DB/`contracts`/`ledger`/`fleet`/`forecast` I/O the fixed interface requires end to end. Warm-start
    hints are passed in (the solver process has no memory of earlier gates); `None` uses this process's."""
    plan_mode = _plan_mode_for(gate_kind, horizon_start)
    settings = solver_settings_for(gate_kind)
    built = build_mode_o_model(inputs)
    outcome = highs_solve(
        built,
        settings,
        x_hint=_last_hint_x if x_hint is None else x_hint,
        q_hint=_last_hint_q if q_hint is None else q_hint,
    )
    result = extract_plan(built, outcome, plan_mode)
    # ES05-S07: the rule baseline runs on EVERY gate, on the same inputs, as the KPI-22 shadow (and it
    # is the plan itself whenever the LP cannot be used).
    rule_plan = rule_fallback_f2(inputs)

    chosen = result
    if outcome.status in ("INFEASIBLE_F1", "TIME_LIMIT_GAP"):
        chosen = rule_plan
    else:
        ok, violations = validate_plan(inputs, result)
        if not ok:
            logger.warning(
                "selector plan failed validation; rule fallback", extra={"violations": violations[:5]}
            )
            chosen = rule_plan
    return dataclasses.replace(chosen, shadow=shadow_comparison(inputs, chosen, rule_plan))


def plan_analytics_rows(
    plan_id: UUID, horizon_start: datetime, inputs: ModelInputs, result: ExtractedPlan
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    """Rows for `db.insert_plan_analytics` (migration 0030): plan value, shadow obligation-intervals and
    the per-bank stored-energy value."""
    shadow = result.shadow
    step = timedelta(minutes=INTERVAL_MINUTES)
    value_row: dict[str, Any] = {
        "plan_id": plan_id,
        "lp_net_value": Decimal(str(round(shadow.lp_value.net, 4))) if shadow else Decimal("0"),
        "rule_net_value": Decimal(str(round(shadow.rule_value.net, 4))) if shadow else Decimal("0"),
        "value_added": Decimal(str(round(shadow.value_added, 4))) if shadow else Decimal("0"),
        "forgone_upside": Decimal(str(round(shadow.forgone_upside, 4))) if shadow else Decimal("0"),
        "stage_r_objective": (
            Decimal(str(round(result.stage_r_objective, 4))) if result.stage_r_objective is not None else None
        ),
        "breakdown": json.dumps(
            {
                "plan_mode": result.plan_mode,
                "lp": dataclasses.asdict(shadow.lp_value) if shadow else None,
                "rule": dataclasses.asdict(shadow.rule_value) if shadow else None,
            }
        ),
    }
    shadow_rows = [
        {
            "plan_id": plan_id,
            "obligation_id": row.obligation_id,
            "interval_start": horizon_start + step * row.interval,
            "interval_end": horizon_start + step * (row.interval + 1),
            "lp_kw": Decimal(str(round(row.lp_kw, 3))),
            "rule_kw": Decimal(str(round(row.rule_kw, 3))),
            "best_competing_value_per_kwh": (
                Decimal(str(round(row.best_competing_value_per_kwh, 6)))
                if row.best_competing_value_per_kwh is not None
                else None
            ),
        }
        for row in (shadow.obligation_intervals if shadow else ())
    ]
    energy_rows = [
        {
            "plan_id": plan_id,
            "bank_id": value.bank_id,
            "horizon_start": value.horizon_start,
            "horizon_end": value.horizon_end,
            "interval_minutes": value.interval_minutes,
            "water_value_usd_per_mwh": list(value.water_value_usd_per_mwh),
            "discharge_threshold_usd_per_mwh": list(value.discharge_threshold_usd_per_mwh),
            "planned_floor_kwh": list(value.planned_floor_kwh),
            "hold_floor_kwh": list(value.hold_floor_kwh),
            "solar_share": list(value.solar_share),
            "solar_share_source": list(value.solar_share_source),
        }
        for value in energy_value.build_energy_values(inputs, result, horizon_start).values()
    ]
    return value_row, shadow_rows, energy_rows


async def persist_plan_analytics(
    plan_id: UUID, horizon_start: datetime, inputs: ModelInputs, result: ExtractedPlan
) -> None:
    """Publish the stored-energy value in-process (DISPATCH's read API) and write the plan analytics.
    Analytics never fail a gate: a DB error (e.g. migration 0030 not applied yet) is logged."""
    energy_value.publish(energy_value.build_energy_values(inputs, result, horizon_start))
    try:
        await db.insert_plan_analytics(*plan_analytics_rows(plan_id, horizon_start, inputs, result))
    except Exception:
        logger.exception("selector plan analytics not persisted", extra={"plan_id": str(plan_id)})
    try:
        pruned = await db.prune_plan_energy_value()
        if pruned:
            logger.info("pruned old stored-energy values", extra={"rows": pruned})
    except Exception:
        logger.exception("stored-energy value retention prune failed")


async def run_gate(gate_kind: GateKind, contract_scope: UUID | None = None) -> Plan:
    """Run one selector gate (02a S3.1/S3.8): assemble inputs, solve, validate, fall back if needed,
    persist the plan, and transition every candidate opportunity through `contracts`/`ledger`.

    Never reduces a `COMMITTED`/`DELIVERING` obligation's frozen `commitment.committed_kw` (K13):
    committed obligations are injected as C24 equality parameters (`load_committed`), not re-decided.
    """
    if gate_kind == "RENOMINATION" and contract_scope is None:
        raise ValueError("RENOMINATION requires contract_scope (02a S1.7)")

    now = datetime.now(UTC)
    horizon_start, horizon_end = await compute_horizon(gate_kind, now)
    bank_ids = tuple(sorted(set(await _configured_bank_ids())))

    n_intervals = int((horizon_end - horizon_start).total_seconds() // (INTERVAL_MINUTES * 60))
    banks, scenarios, committed, candidates, (market, zone_by_bank) = (
        await load_banks(horizon_start, horizon_end, bank_ids),
        await load_scenarios(horizon_start, horizon_end, bank_ids),
        await load_committed(horizon_start, horizon_end, bank_ids),
        await load_candidates(horizon_start, horizon_end, bank_ids, contract_scope),
        await load_market(bank_ids),
    )
    shares = await load_solar_shares(zone_by_bank, horizon_start, n_intervals, now)
    mobile = load_mobile_units()
    mobile_in_gate = [bank_id for bank_id in bank_ids if bank_id in mobile]
    banks = mark_mobile_units(banks, mobile, await load_mobile_units_at_home(mobile_in_gate), n_intervals)
    banks = apply_market_terms(
        banks,
        bank_market_terms(
            market,
            zone_by_bank,
            bank_ids,
            horizon_start,
            n_intervals,
            shares,
            await load_owner_charge_windows(),
            {bank_id: _bank_feeder(bank_id) for bank_id in bank_ids},
            frozenset(mobile_in_gate),
        ),
    )
    candidates, committed = prepare_obligations(candidates, committed, market)
    # D-37: nothing offered or planned on an UNAVAILABLE bank; K13-grandfathered obligations keep theirs.
    banks, candidates, committed = apply_availability(
        banks, candidates, committed, await load_availability(), bank_ids
    )
    # 02b S6.5 row 1: no new selection on stale data. The structural (pre-freeze) eligibility is kept for
    # the R-ADMIT-REJECT check, so a candidate withheld only for a stale series is never rejected for it.
    structural_by_id = {c.opportunity_id: c for c in candidates}
    candidates = await freeze_new_selection(candidates, banks, scenarios, horizon_start, horizon_end)

    inputs = ModelInputs(
        intervals=tuple(range(n_intervals)),
        interval_minutes=INTERVAL_MINUTES,
        banks=banks,
        scenarios=scenarios,
        committed=committed,
        candidates=candidates,
    )

    # Off the event loop AND off this process's GIL: a 24 h Mode O solve takes 0.1-30 s (A11 budget), and
    # og-engine's 2 s dispatch cycle and MQTT ingest share this loop.
    result = await solve_off_loop(inputs, gate_kind, horizon_start, dict(_last_hint_x), dict(_last_hint_q))

    _last_hint_x.clear()
    _last_hint_x.update({k: 1.0 if v else 0.0 for k, v in result.selected_x.items()})
    _last_hint_q.clear()
    _last_hint_q.update(result.selected_q)

    plan_id = await persist_plan(result.plan_mode, gate_kind, horizon_start, horizon_end, scenarios, result)
    await persist_plan_analytics(plan_id, horizon_start, inputs, result)

    unselected: list[CandidateOpportunity] = []
    for c in candidates:
        selected = (
            result.selected_x.get(c.opportunity_id, False) or result.selected_q.get(c.opportunity_id, 0.0) > 0
        )
        if not selected:
            unselected.append(c)
            continue
        # Keys are `encode_interval_key(bank, start, end)` per bank (a bare interval index collided
        # across banks), and the obligation -- not the opportunity -- owns the reservation (FK).
        selected_kw = selected_kw_by_interval_key(c, result, horizon_start, INTERVAL_MINUTES)
        if selected_kw and exceeds_pq_eligible_capacity(c, selected_kw):
            # WP-D (owner decision): a PQ-sensitive obligation is never committed beyond the capacity of
            # its PQ-eligible hubs -- not selected, rather than silently over-committed.
            logger.warning(
                "selection exceeds PQ-eligible capacity; not committed",
                extra={"obligation_id": c.obligation_id, "reason_code": R_PQ_ELIGIBLE_CAPACITY},
            )
            unselected.append(c)
            continue
        if selected_kw:
            try:
                await commit_candidate(c, selected_kw, plan_id)
            except Exception:
                # Review #8: one candidate's commit error (DB, optimistic lock) never aborts the rest;
                # a half-done commit is finished or undone by og-engine's stuck-SELECTED sweep.
                logger.exception(
                    "commit failed for a selected candidate", extra={"obligation_id": c.obligation_id}
                )
    rated_kw_by_bank = _rated_kw_by_bank(bank_ids) if unselected else None
    if unselected and rated_kw_by_bank is not None:
        await reject_structurally_infeasible(
            [structural_by_id.get(c.opportunity_id, c) for c in unselected], rated_kw_by_bank, plan_id
        )

    return Plan(
        plan_id=plan_id,
        plan_mode=result.plan_mode,
        gate_kind=gate_kind,
        horizon_start=horizon_start,
        horizon_end=horizon_end,
        scenario_set=[{"scenario": s.scenario, "prob": s.probability} for s in scenarios],
        solver_status=result.solver_status,
        solver_gap=Decimal(str(result.solver_gap)) if result.solver_gap is not None else None,
        solver_time_ms=result.solver_time_ms,
        objective_value=Decimal(str(result.objective_value)),
    )


def _rated_kw_by_bank(bank_ids: tuple[str, ...]) -> dict[str, float] | None:
    """Structural (rated) discharge per bank from the fleet twin, for the admission-reject check; `None`
    (skip the check -- never reject on missing data) if the twin does not know a bank."""
    try:
        return {b: fleet_rated_discharge_kw(b) for b in bank_ids}
    except LookupError:
        logger.warning("fleet twin lacks a configured bank; structural admission check skipped")
        return None


async def _configured_bank_ids() -> tuple[str, ...]:
    """The real bank list, read from `og.bank` (02b S4.2), the fleet topology's single source of truth
    (seeded by `opengrid.fleet.seed` from `integration-sims/config/fleet.yaml`, id scheme `bank-000`..
    `bank-039`). Never fabricated from a count + format guess: an earlier version of this function
    synthesised `f"bank-{i:02d}"` ids (`"bank-01".."bank-NN"`), which are a completely disjoint id space
    from the real `bank-000`-style ids, so every `fleet.capability(bank_id, ...)` call raised
    `LookupError` and `run_gate` never reserved anything (`qa/merge-notes.md` section 11). Overridden by
    tests via `db.load_bank_ids_rows`."""
    bank_ids = await db.load_bank_ids_rows()
    return tuple(bank_ids)
