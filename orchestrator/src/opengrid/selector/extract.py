"""Extract an `ExtractedPlan` from a solved model (02a S3.8, S6.10): selected x_o/q_o, the committed
profile per bank/interval, the free-headroom energy schedule, and duals/price-of-firmness.
"""

from __future__ import annotations

from opengrid.selector.model import BuiltModel
from opengrid.selector.solve import SolveOutcome
from opengrid.selector.types import ExtractedPlan

_SELECTED_THRESHOLD = 0.5  # binary rounding tolerance for x_o read back from the LP relaxation/MIP


def extract_plan(
    built: BuiltModel,
    outcome: SolveOutcome,
    plan_mode: str,
) -> ExtractedPlan:
    """Build the typed `ExtractedPlan` from `outcome.primal` (never re-reads `built.highs`, which
    `solve.highs_solve` has already mutated in place to recover duals -- see `PrimalSnapshot`)."""
    inputs = built.inputs
    primal = outcome.primal

    selected_x = {oid: value >= _SELECTED_THRESHOLD for oid, value in primal.x.items()}
    committed_profile = {co.obligation_id: dict(co.committed_kw_by_interval) for co in inputs.committed}

    return ExtractedPlan(
        solver_status=outcome.status,
        plan_mode=plan_mode,  # type: ignore[arg-type]
        objective_value=outcome.objective_value,
        solver_gap=outcome.gap,
        solver_time_ms=outcome.time_ms,
        selected_x=selected_x,
        selected_q=dict(primal.q),
        bank_interval_allocation=dict(primal.ybar),
        committed_profile=committed_profile,
        headroom_schedule=dict(primal.h),
        bank_capacity_duals=dict(outcome.bank_capacity_duals),
    )
