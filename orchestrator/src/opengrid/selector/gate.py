"""Gate orchestration (02a S3.1/S3.8): assembles `ModelInputs`, solves Mode O, validates, falls back to
F2, persists the `plan` row, and transitions opportunities through `contracts`/`ledger`.

Each I/O step is a small, separately named async function so tests can monkeypatch exactly the seam
they need (BUILD.md "use fakes for siblings") without touching the pure `model`/`solve`/`extract`/
`validate`/`rule_fallback` core, which is tested directly with hand-built `ModelInputs`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import multiprocessing
from collections.abc import Callable, Sequence
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID, uuid4

from opengrid import forecast, ledger
from opengrid.core.models.engine import Plan
from opengrid.core.physics import DEFAULT_ETA_C, DEFAULT_ETA_D
from opengrid.core.timeutil import floor_to_interval
from opengrid.fleet import capability as fleet_capability
from opengrid.fleet import hub_capabilities as fleet_hub_capabilities
from opengrid.fleet import rated_discharge_kw as fleet_rated_discharge_kw
from opengrid.selector import db
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

logger = logging.getLogger(__name__)

INTERVAL_MINUTES = 15.0
SCHEDULED_HORIZON_INTERVALS = 96  # 24h / 15min, 02a S3.2

# 02a S3.7's "firm first, then AS, then market" F2 priority bucket, keyed by `contract.service_type`
# (`CandidateOpportunity.category`'s docstring). `ERCOT_ENERGY` (spot-like) falls back to `MARKET`.
_CATEGORY_BY_SERVICE_TYPE: dict[str, Literal["FIRM", "AS", "MARKET"]] = {
    "HOME": "FIRM",
    "DIST_DEFERRAL": "FIRM",
    "PARTNER_CAPACITY": "FIRM",
    "DATA_CENTER": "FIRM",  # firm bridging capacity (06-service-profiles S4.b)
    "ERCOT_AS": "AS",
    "ERCOT_ENERGY": "MARKET",
}

#: Full-deployment duration for an ERCOT_AS award whose product rule has none (ECRS 1 h, the shortest).
DEFAULT_AS_HOLD_MINUTES = 60.0


def as_energy_hold_h(service_type: object, duration_minutes: object) -> float:
    """Energy-hold hours for the selector's SoC model: an ERCOT_AS award is a capacity hold that must be
    deployable for its product's full duration (Non-Spin 4 h, ECRS 1 h, NPRR1282); 0 for every other
    service (those discharge their profile). Keyed on the service's AS category, so any AS service type
    added to `_CATEGORY_BY_SERVICE_TYPE` is held too (from ftbrown's #13)."""
    if not isinstance(service_type, str) or _CATEGORY_BY_SERVICE_TYPE.get(service_type) != "AS":
        return 0.0
    minutes = float(str(duration_minutes)) if duration_minutes else DEFAULT_AS_HOLD_MINUTES
    return minutes / 60.0


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


async def load_scenarios(
    horizon_start: datetime, horizon_end: datetime, bank_ids: tuple[str, ...] = ()
) -> tuple[ScenarioPrice, ...]:
    """P10/P50/P90 price scenarios via `forecast.scenarios` (02b S3), per bank zone.

    Architect finding (a): this took the LAST row per interval regardless of series or kind -- load-
    forecast rows (MW) were folded into the price path and every bank was priced at whichever zone came
    last (LZ_WEST). Now only `kind == "price"` points count; each bank gets its own load zone's path,
    and the fleet path (for a bank with no zone path) is the mean of the zones."""
    points = await forecast.scenarios(horizon_start, horizon_end)
    zone_by_bank = await db.load_bank_zones(list(bank_ids)) if bank_ids else {}
    return scenarios_from_points(points, horizon_start, zone_by_bank)


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
    (02a S2.2). All configured banks are eligible for redistribution (bank *substitution*, not a
    reduction -- see `types.CommittedObligation`); a future refinement can narrow this per obligation
    once `contracts`/`ledger` expose an eligibility query."""
    frozen = await db.load_frozen_commitments(horizon_start.isoformat(), horizon_end.isoformat())
    as_minutes = await db.load_as_hold_minutes([str(o) for o in frozen])
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
            result.append(
                CommittedObligation(
                    obligation_id=str(obligation_id),
                    eligible_bank_ids=bank_ids,
                    committed_kw_by_interval=by_index,
                    energy_hold_h=(
                        as_energy_hold_h("ERCOT_AS", as_minutes[str(obligation_id)])
                        if str(obligation_id) in as_minutes
                        else 0.0
                    ),
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

    Every configured bank is eligible for every candidate: no `contracts`/`ledger` query exists yet to
    narrow eligibility per opportunity either (`load_committed`'s docstring documents the identical gap
    for committed obligations)."""
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
        candidates.append(
            CandidateOpportunity(
                opportunity_id=str(row["opportunity_id"]),
                obligation_id=str(row["obligation_id"]),
                contract_id=str(row["contract_id"]),
                eligible_bank_ids=bank_ids,
                window_intervals=window_intervals,
                requested_kw=float(row["requested_kw"]),
                value_per_mwh=float(row["value_per_mwh"]) if row["value_per_mwh"] is not None else 0.0,
                variable_kind=row["variable_kind"] or "CONTINUOUS",
                min_qty_kw=float(row["min_qty_kw"] or 0.0),
                increment_kw=float(row["increment_kw"] or 0.0),
                degradation_cost_per_kwh=float(row["degradation_cost"] or 0.03),
                tier=row["tier"] or "T4",
                category=_CATEGORY_BY_SERVICE_TYPE.get(row["service_type"], "MARKET"),
                service_type=str(row["service_type"] or ""),
                energy_hold_h=as_energy_hold_h(row["service_type"], row.get("duration_minutes")),
            )
        )
    return tuple(candidates)


def _plan_mode_for(gate_kind: GateKind, horizon_start: datetime) -> str:
    if gate_kind == "SCHEDULED_15MIN" and horizon_start.hour == 0 and horizon_start.minute < INTERVAL_MINUTES:
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

    if outcome.status in ("INFEASIBLE_F1", "TIME_LIMIT_GAP"):
        return rule_fallback_f2(inputs)

    ok, _violations = validate_plan(inputs, result)
    if not ok:
        return rule_fallback_f2(inputs)
    return result


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

    banks, scenarios, committed, candidates = (
        await load_banks(horizon_start, horizon_end, bank_ids),
        await load_scenarios(horizon_start, horizon_end, bank_ids),
        await load_committed(horizon_start, horizon_end, bank_ids),
        await load_candidates(horizon_start, horizon_end, bank_ids, contract_scope),
    )

    n_intervals = int((horizon_end - horizon_start).total_seconds() // (INTERVAL_MINUTES * 60))
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
        await reject_structurally_infeasible(unselected, rated_kw_by_bank, plan_id)

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
