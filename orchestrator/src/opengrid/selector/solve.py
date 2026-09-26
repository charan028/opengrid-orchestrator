"""Solve a built Mode O model with a time limit, optional warm start, and price-of-firmness duals
(02a S3.7). No model construction here (see `model.py`); no persistence (see `gate.py`).
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import highspy

from opengrid.selector.model import BuiltModel
from opengrid.selector.types import SolverSettings, SolverStatus

#: Time budget for the price-of-firmness LP re-solve, separate from the main solve's `time_limit_s`.
PRICE_OF_FIRMNESS_TIME_LIMIT_S = 10.0


@dataclass(frozen=True, slots=True)
class PrimalSnapshot:
    """Variable values read off the incumbent *before* `_price_of_firmness` mutates the model to
    recover duals -- `extract.py` builds `ExtractedPlan` from this, never from `built.highs` post-dual."""

    x: dict[str, float]
    q: dict[str, float]
    ybar: dict[tuple[str, str, int], float]
    h: dict[tuple[str, int, str], float]
    soc: dict[tuple[str, int, str], float]
    charge: dict[tuple[str, int, str], float]


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


def _has_incumbent(highs: highspy.Highs) -> bool:
    return highs.getInfo().primal_solution_status == highspy.kSolutionStatusFeasible


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
    )


def _price_of_firmness(built: BuiltModel) -> dict[tuple[str, int], float]:
    """Shadow price of each bank/interval capacity row: the scenario-probability-weighted dual of
    C1/C13's shared-capacity constraint -- the $/kW value of one more kW of headroom there. HiGHS does
    not report meaningful row duals at a MIP incumbent, so integer/binary columns are fixed at their
    solved values and the relaxation is re-solved as an LP to recover duals (standard MIP-dual practice).
    Batched via `allVariableValues()`/`allConstrDuals()` for the same reason as `_snapshot_primal`.
    """
    highs = built.highs
    if built.integer_vars:
        values: list[float] = highs.allVariableValues()  # type: ignore[no-untyped-call]
        for var in built.integer_vars:
            value = values[var.index]
            highs.changeColBounds(var.index, value, value)
            highs.setContinuous(var)
        # Its own budget (review #12): the duals are informational; a slow re-solve must never hold the
        # gate beyond the main solve's time limit. No optimal LP in time -> no duals, not a failure.
        highs.setOptionValue("time_limit", PRICE_OF_FIRMNESS_TIME_LIMIT_S)
        highs.run()
        if highs.getModelStatus() != highspy.HighsModelStatus.kOptimal:
            return {}

    duals: list[float] = highs.allConstrDuals()  # type: ignore[no-untyped-call]
    totals: dict[tuple[str, int], float] = {}
    prob_by_scenario: dict[str, float] = {s.scenario: s.probability for s in built.inputs.scenarios}
    for (bank_id, t, scenario_name), row in built.capacity_rows.items():
        dual = duals[row.index]
        weight = prob_by_scenario.get(scenario_name, 0.0)
        key = (bank_id, t)
        totals[key] = totals.get(key, 0.0) + dual * weight
    return totals


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
    highs.setOptionValue("time_limit", settings.time_limit_s)
    if x_hint or q_hint:
        apply_warm_start(built, x_hint or {}, q_hint or {})

    start = time.monotonic()
    highs.run()
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
    duals = _price_of_firmness(built) if has_incumbent else {}

    return SolveOutcome(
        status=status,
        is_feasible=has_incumbent,
        hit_time_limit=hit_time_limit,
        gap=gap,
        objective_value=objective_value,
        time_ms=elapsed_ms,
        bank_capacity_duals=duals,
        primal=primal,
    )
