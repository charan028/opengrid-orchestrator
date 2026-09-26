"""Rule-based selector fallback F2 (02a S3.7): firm first, then AS, then market/no-arbitrage
free-headroom scheduling. Runs when the LP/MILP is infeasible, times out ungapped, or fails the
independent validator (02a S3.8); also the KPI-22 baseline the LP's value-added is measured against
(03 epics ES05-S07, 08 profitability §8.4).

Greedy, deterministic, no solver: committed obligations are funded first and in full from any eligible
bank with headroom (K13), then candidates are granted capacity in `category` order (`FIRM` > `AS` >
`MARKET`) and, within a category, richest `value_per_mwh` first, rounded to each product's rule
(`opengrid.core.products.round_quantity`). Whatever capacity is left becomes the free-headroom spot
schedule, taken only where the scenario price is non-negative (no self-harm).
"""

from __future__ import annotations

from decimal import Decimal

from opengrid.core.products import ProductRule, round_quantity
from opengrid.selector.types import CandidateOpportunity, ExtractedPlan, ModelInputs

_TOL = 1e-9
_CATEGORY_ORDER = {"FIRM": 0, "AS": 1, "MARKET": 2}


def _category_key(c: CandidateOpportunity) -> tuple[int, float]:
    return _CATEGORY_ORDER.get(c.category, 2), -c.value_per_mwh


def rule_fallback_f2(inputs: ModelInputs) -> ExtractedPlan:
    """Run F2 over `inputs`, returning an `ExtractedPlan` with `plan_mode="RULE_FALLBACK"`."""
    remaining = _initial_remaining(inputs)
    allocation: dict[tuple[str, str, int], float] = {}

    for co in inputs.committed:
        for t, kw in co.committed_kw_by_interval.items():
            _greedy_take(allocation, remaining, co.obligation_id, co.eligible_bank_ids, t, kw)

    selected_x: dict[str, bool] = {}
    selected_q: dict[str, float] = {}
    for c in sorted(inputs.candidates, key=_category_key):
        quantity = _feasible_quantity(c, remaining)
        if c.variable_kind == "BINARY":
            selected_x[c.opportunity_id] = quantity > _TOL
        else:
            selected_q[c.opportunity_id] = quantity
        if quantity <= _TOL:
            continue
        for t in c.window_intervals:
            _greedy_take(allocation, remaining, c.opportunity_id, c.eligible_bank_ids, t, quantity)

    headroom = _headroom_schedule(inputs, remaining)
    committed_profile = {co.obligation_id: dict(co.committed_kw_by_interval) for co in inputs.committed}

    return ExtractedPlan(
        solver_status="RULE_FALLBACK",
        plan_mode="RULE_FALLBACK",
        objective_value=_objective_value(inputs, allocation, headroom),
        solver_gap=None,
        solver_time_ms=0,
        selected_x=selected_x,
        selected_q=selected_q,
        bank_interval_allocation=allocation,
        committed_profile=committed_profile,
        headroom_schedule=headroom,
        bank_capacity_duals={},
    )


def _initial_remaining(inputs: ModelInputs) -> dict[tuple[str, int], float]:
    return {(bank.bank_id, t): cap for bank in inputs.banks for t, cap in bank.max_discharge_kw.items()}


def _greedy_take(
    allocation: dict[tuple[str, str, int], float],
    remaining: dict[tuple[str, int], float],
    obligation_id: str,
    eligible_bank_ids: tuple[str, ...],
    t: int,
    amount_kw: float,
) -> float:
    """Draw `amount_kw` from `eligible_bank_ids` at interval `t`, first-fit. Returns any unmet amount
    (> 0 only when total eligible capacity is insufficient -- the K13 infeasibility/exception path,
    which `gate.py` traces separately; this function never raises)."""
    need = amount_kw
    for bank_id in eligible_bank_ids:
        if need <= _TOL:
            break
        key = (bank_id, t)
        available = remaining.get(key, 0.0)
        if available <= 0:
            continue
        take = min(available, need)
        allocation[obligation_id, bank_id, t] = allocation.get((obligation_id, bank_id, t), 0.0) + take
        remaining[key] = available - take
        need -= take
    return need


def _feasible_quantity(c: CandidateOpportunity, remaining: dict[tuple[str, int], float]) -> float:
    if not c.window_intervals:
        return 0.0
    capacity_per_t = [
        sum(remaining.get((b, t), 0.0) for b in c.eligible_bank_ids) for t in c.window_intervals
    ]
    max_feasible = min(capacity_per_t)
    rule = ProductRule(
        min_qty_kw=Decimal(str(c.min_qty_kw)),
        increment_kw=Decimal(str(c.increment_kw)),
        block=c.variable_kind == "BINARY",
    )
    return float(round_quantity(Decimal(str(max_feasible)), rule, Decimal(str(c.requested_kw))))


def _headroom_schedule(
    inputs: ModelInputs, remaining: dict[tuple[str, int], float]
) -> dict[tuple[str, int, str], float]:
    headroom: dict[tuple[str, int, str], float] = {}
    for scenario in inputs.scenarios:
        for (bank_id, t), left in remaining.items():
            price = scenario.price_at(bank_id, t)
            headroom[bank_id, t, scenario.scenario] = left if price >= 0 else 0.0
    return headroom


def _objective_value(
    inputs: ModelInputs,
    allocation: dict[tuple[str, str, int], float],
    headroom: dict[tuple[str, int, str], float],
) -> float:
    dt_h = inputs.interval_hours
    value_per_mwh = {c.opportunity_id: c.value_per_mwh for c in inputs.candidates}
    degradation = {c.opportunity_id: c.degradation_cost_per_kwh for c in inputs.candidates}
    degradation.update({co.obligation_id: co.degradation_cost_per_kwh for co in inputs.committed})

    total = 0.0
    for (oid, _bank_id, _t), kw in allocation.items():
        total += (value_per_mwh.get(oid, 0.0) / 1000.0 - degradation.get(oid, 0.0)) * dt_h * kw
    for scenario in inputs.scenarios:
        for (bank_id, t, scenario_name), kw in headroom.items():
            if scenario_name != scenario.scenario:
                continue
            price = scenario.price_at(bank_id, t)
            total += scenario.probability * dt_h * (price / 1000.0) * kw
    return total
