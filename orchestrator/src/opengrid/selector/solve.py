"""Solve a built Mode O model with a time limit, optional warm start, and price-of-firmness duals
(02a S3.7). No model construction here (see `model.py`); no persistence (see `gate.py`).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import highspy
import numpy as np

from opengrid.selector.model import BuiltModel, set_column_costs
from opengrid.selector.types import SolverSettings, SolverStatus

#: Time budget for the price-of-firmness LP re-solve, separate from the main solve's `time_limit_s`.
PRICE_OF_FIRMNESS_TIME_LIMIT_S = 10.0


@dataclass(frozen=True, slots=True)
class PrimalSnapshot:
    """Variable values read off the incumbent *before* `_recover_duals` mutates the model to
    recover duals -- `extract.py` builds `ExtractedPlan` from this, never from `built.highs` post-dual."""

    x: dict[str, float]
    q: dict[str, float]
    ybar: dict[tuple[str, str, int], float]
    h: dict[tuple[str, int, str], float]
    soc: dict[tuple[str, int, str], float]
    charge: dict[tuple[str, int, str], float]
    solar_charge: dict[tuple[str, int, str], float] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SolveOutcome:
    status: SolverStatus
    is_feasible: bool
    hit_time_limit: bool
    gap: float | None
    objective_value: float
    time_ms: int
    bank_capacity_duals: dict[tuple[str, int], float]
    primal: PrimalSnapshot
    stored_energy_value: dict[tuple[str, int], float] = field(default_factory=dict)
    """09 D7 water value per (bank, interval): the C1 dual, $ per stored kWh at the end of the interval."""
    stage_r_objective: float | None = None


def _has_incumbent(highs: highspy.Highs) -> bool:
    return highs.getInfo().primal_solution_status == highspy.kSolutionStatusFeasible


def _run(highs: highspy.Highs, time_limit_s: float) -> None:
    """`run()` with `time_limit_s` for THIS run. HiGHS compares `time_limit` with the instance's
    cumulative run clock, so a plain limit made every later run on the same model (stage F after stage
    R, the fixed-integer dual re-solve) start already out of time once the first run was slow: the duals
    silently came back empty whenever the main solve took over 10 s."""
    highs.setOptionValue("time_limit", float(highs.getRunTime()) + time_limit_s)
    highs.run()


def apply_warm_start(built: BuiltModel, x_hint: dict[str, float], q_hint: dict[str, float]) -> None:
    """Warm-start via `setSolution` (02a S3.7): a value hint per decision var, never a constraint --
    the model remains free to move away from it (unlike the frozen-parameter treatment of C24)."""
    indices: list[int] = []
    values: list[float] = []
    for opportunity_id, value in x_hint.items():
        var = built.x_vars.get(opportunity_id)
        if var is not None:
            indices.append(var.index)
            values.append(value)
    for opportunity_id, value in q_hint.items():
        var = built.q_vars.get(opportunity_id)
        if var is not None:
            indices.append(var.index)
            values.append(value)
    if indices:
        built.highs.setSolution(len(indices), indices, values)


def _snapshot_primal(built: BuiltModel) -> PrimalSnapshot:
    """Batched via `allVariableValues()` -- one pybind round trip instead of one `val()` call per
    variable, which matters at MVP-S scale (tens of thousands of `ybar`/`h` variables, BUILD.md's
    40-bank/96-interval/3-scenario performance budget)."""
    highs = built.highs
    values: list[float] = highs.allVariableValues()  # type: ignore[no-untyped-call]
    return PrimalSnapshot(
        x={oid: values[v.index] for oid, v in built.x_vars.items()},
        q={oid: values[v.index] for oid, v in built.q_vars.items()},
        ybar={key: values[v.index] for key, v in built.ybar_vars.items()},
        h={key: values[v.index] for key, v in built.h_vars.items()},
        soc={key: values[v.index] for key, v in built.soc_vars.items()},
        charge={key: values[v.index] for key, v in built.charge_vars.items()},
        solar_charge={key: values[v.index] for key, v in built.solar_charge_vars.items()},
    )


def _recover_duals(
    built: BuiltModel,
) -> tuple[dict[tuple[str, int], float], dict[tuple[str, int], float]]:
    """`(price of firmness, stored-energy value)` per (bank, interval).

    HiGHS does not report meaningful row duals at a MIP incumbent, so integer/binary columns are fixed at
    their solved values and the relaxation is re-solved as an LP (standard MIP-dual practice). Batched via
    `allVariableValues()`/`allConstrDuals()` for the same reason as `_snapshot_primal`.

    - Price of firmness: the scenario-probability-weighted dual of C1/C13's shared-capacity row -- the
      $/kW value of one more kW of headroom there.
    - Stored-energy value (09 D7 water value nu_{b,t}): the C1 SoC-balance row of (b, t, w) defines the
      energy at the END of t, so its dual is the value of one more stored kWh then. The objective is
      already probability-weighted, so the expectation is the plain sum over scenarios. The row is
      `e_{t+1} - e_t - eta_c*g*dt + (dt/eta_d)*d = -self_discharge*dt` (`model._RowBuffer`), so raising
      its right-hand side by 1 adds one kWh to interval t's balance: the dual (d objective / d rhs) IS
      the value of that kWh, positive when the plan would pay to have it."""
    highs = built.highs
    if built.integer_vars:
        values: list[float] = highs.allVariableValues()  # type: ignore[no-untyped-call]
        for var in built.integer_vars:
            value = values[var.index]
            highs.changeColBounds(var.index, value, value)
            highs.setContinuous(var)
        # Its own budget (review #12): the duals are informational; a slow re-solve must never hold the
        # gate beyond the main solve's time limit. No optimal LP in time -> no duals, not a failure.
        _run(highs, PRICE_OF_FIRMNESS_TIME_LIMIT_S)
        if highs.getModelStatus() != highspy.HighsModelStatus.kOptimal:
            return {}, {}

    duals: list[float] = highs.allConstrDuals()  # type: ignore[no-untyped-call]
    firmness: dict[tuple[str, int], float] = {}
    prob_by_scenario: dict[str, float] = {s.scenario: s.probability for s in built.inputs.scenarios}
    for (bank_id, t, scenario_name), row in built.capacity_rows.items():
        weight = prob_by_scenario.get(scenario_name, 0.0)
        firmness[bank_id, t] = firmness.get((bank_id, t), 0.0) + duals[row] * weight
    energy_value: dict[tuple[str, int], float] = {}
    for (bank_id, t, _scenario_name), row in built.soc_balance_rows.items():
        energy_value[bank_id, t] = energy_value.get((bank_id, t), 0.0) + duals[row]
    return firmness, energy_value


def _stage_r(built: BuiltModel, time_limit_s: float) -> float | None:
    """09 D3 stage R: maximise the regulated candidates' capacity value alone, then pin it (within
    `lexicographic_tolerance`) as a constraint for stage F, and restore the stage-F objective. Returns
    the stage-R optimum, or None (no incumbent in time: stage F then runs unconstrained, logged by the
    caller through `stage_r_objective is None`)."""
    highs = built.highs
    set_column_costs(highs, built.stage_r_costs)
    _run(highs, time_limit_s)
    status = highs.getModelStatus()
    feasible = status == highspy.HighsModelStatus.kOptimal or (
        status == highspy.HighsModelStatus.kTimeLimit and _has_incumbent(highs)
    )
    z_r = float(highs.getObjectiveValue()) if feasible else 0.0  # read BEFORE the costs change: that
    set_column_costs(highs, built.costs)  # resets HiGHS's solution and its objective value
    if not feasible:
        return None
    floor = z_r - built.inputs.lexicographic_tolerance * abs(z_r)
    indices = np.array(sorted(built.stage_r_costs), dtype=np.int32)
    coefficients = np.array([built.stage_r_costs[i] for i in indices], dtype=np.float64)
    highs.addRow(floor, highspy.kHighsInf, len(indices), indices, coefficients)
    return z_r


def highs_solve(
    built: BuiltModel,
    settings: SolverSettings,
    *,
    x_hint: dict[str, float] | None = None,
    q_hint: dict[str, float] | None = None,
) -> SolveOutcome:
    """Run HiGHS on `built` with `settings`'s time limit / gap target, then recover price-of-firmness
    duals. Does not fall back to the rule selector -- that decision belongs to `gate.py` (02a S3.8)."""
    highs = built.highs
    highs.setOptionValue("mip_rel_gap", settings.mip_rel_gap)
    start = time.monotonic()
    stage_r_objective: float | None = None
    stage_f_limit_s = settings.time_limit_s
    if built.stage_r_costs:
        # Two stages share the one time limit, so the gate's hard budget (`gate.solver_budget_s`) holds.
        stage_f_limit_s = settings.time_limit_s / 2.0
        stage_r_objective = _stage_r(built, settings.time_limit_s / 2.0)
    if x_hint or q_hint:
        apply_warm_start(built, x_hint or {}, q_hint or {})

    _run(highs, stage_f_limit_s)
    elapsed_ms = int((time.monotonic() - start) * 1000)

    model_status = highs.getModelStatus()
    info = highs.getInfo()
    is_optimal = model_status == highspy.HighsModelStatus.kOptimal
    hit_time_limit = model_status == highspy.HighsModelStatus.kTimeLimit
    has_incumbent = is_optimal or (hit_time_limit and _has_incumbent(highs))
    gap = float(info.mip_gap) if built.integer_vars and has_incumbent else (0.0 if has_incumbent else None)

    if is_optimal:
        status: SolverStatus = "OPTIMAL"
    elif hit_time_limit and has_incumbent and gap is not None and gap <= settings.accept_gap_at_limit:
        status = "OPTIMAL"
    elif hit_time_limit:
        status = "TIME_LIMIT_GAP"
    else:
        status = "INFEASIBLE_F1"

    objective_value = highs.getObjectiveValue() if has_incumbent else 0.0
    primal = (
        _snapshot_primal(built)
        if has_incumbent
        else PrimalSnapshot(x={}, q={}, ybar={}, h={}, soc={}, charge={})
    )
    duals, energy_value = _recover_duals(built) if has_incumbent else ({}, {})

    return SolveOutcome(
        status=status,
        is_feasible=has_incumbent,
        hit_time_limit=hit_time_limit,
        gap=gap,
        objective_value=objective_value,
        time_ms=elapsed_ms,
        bank_capacity_duals=duals,
        primal=primal,
        stored_energy_value=energy_value,
        stage_r_objective=stage_r_objective,
    )
