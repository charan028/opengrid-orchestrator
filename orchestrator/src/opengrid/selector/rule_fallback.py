"""Rule-based selector fallback F2 (02a S3.7): firm first, then AS, then market/no-arbitrage
free-headroom scheduling. Runs when the LP/MILP is infeasible, times out ungapped, or fails the
independent validator (02a S3.8); also the KPI-22 baseline the LP's value-added is measured against
(03 epics ES05-S07, 08 profitability §8.4).

Greedy, deterministic, no solver: committed obligations are funded first and in full from any eligible
bank with headroom (K13), then candidates are granted capacity in `category` order (`FIRM` > `AS` >
`MARKET`) and, within a category, richest `value_per_mwh` first, rounded to each product's rule
(`opengrid.core.products.round_quantity`). Whatever capacity is left becomes the free-headroom spot
schedule, taken only where the scenario price is non-negative (no self-harm), only from stored energy
above the reserve and the held awards' energy holds, and never from a bank without FREE access (K15 b).
Its `objective_value` is `selector.value.plan_net_value`, the evaluator the LP plan is valued by too.
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

from opengrid.core.products import ProductRule, round_quantity
from opengrid.selector.types import CandidateOpportunity, ExtractedPlan, ModelInputs
from opengrid.selector.value import plan_net_value

_TOL = 1e-9
_CATEGORY_ORDER = {"FIRM": 0, "AS": 1, "MARKET": 2}


def _category_key(c: CandidateOpportunity) -> tuple[int, float]:
    return _CATEGORY_ORDER.get(c.category, 2), -c.value_per_mwh


def rule_fallback_f2(inputs: ModelInputs) -> ExtractedPlan:
    """Run F2 over `inputs`, returning an `ExtractedPlan` with `plan_mode="RULE_FALLBACK"`."""
    remaining = _initial_remaining(inputs)
    allocation: dict[tuple[str, str, int], float] = {}

    for co in inputs.committed:
        banks = tuple(b for b in co.eligible_bank_ids if inputs.may_serve(co.obligation_id, b))  # D-31
        for t, kw in co.committed_kw_by_interval.items():
            _greedy_take(allocation, remaining, co.obligation_id, banks, t, kw)

    selected_x: dict[str, bool] = {}
    selected_q: dict[str, float] = {}
    for candidate in sorted(inputs.candidates, key=_category_key):
        c = replace(
            candidate,
            eligible_bank_ids=tuple(
                b for b in candidate.eligible_bank_ids if inputs.may_serve(candidate.opportunity_id, b)
            ),
        )
        quantity = _feasible_quantity(c, remaining)
        if c.variable_kind == "BINARY":
            selected_x[c.opportunity_id] = quantity > _TOL
        else:
            selected_q[c.opportunity_id] = quantity
        if quantity <= _TOL:
            continue
        for t in c.window_intervals:
            _greedy_take(allocation, remaining, c.opportunity_id, c.eligible_bank_ids, t, quantity)

    headroom, soc = _headroom_schedule(inputs, remaining, allocation)
    committed_profile = {co.obligation_id: dict(co.committed_kw_by_interval) for co in inputs.committed}

    plan = ExtractedPlan(
        solver_status="RULE_FALLBACK",
        plan_mode="RULE_FALLBACK",
        objective_value=0.0,
        solver_gap=None,
        solver_time_ms=0,
        selected_x=selected_x,
        selected_q=selected_q,
        bank_interval_allocation=allocation,
        committed_profile=committed_profile,
        headroom_schedule=headroom,
        bank_capacity_duals={},
        soc_by_bank_interval_scenario=soc,
    )
    # Valued by the same evaluator as the LP plan (the KPI-22 baseline must compare like with like).
    return replace(plan, objective_value=plan_net_value(inputs, plan).net)


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
    inputs: ModelInputs,
    remaining: dict[tuple[str, int], float],
    allocation: dict[tuple[str, str, int], float],
) -> tuple[dict[tuple[str, int, str], float], dict[tuple[str, int, str], float]]:
    """Free headroom where the price is non-negative (no self-harm), never charging (no arbitrage), and
    -- for a bank with an energy envelope -- only from energy above its reserve and every held award's
    energy hold, after its obligations' own drain. Returns `(headroom, soc)`; `soc` is the SoC at the
    start of each interval (plus the terminal index), as the LP reports it."""
    dt_h = inputs.interval_hours
    held = inputs.energy_hold_hours()
    drain: dict[tuple[str, int], float] = {}
    hold_kwh: dict[tuple[str, int], float] = {}
    bank_by_id = {b.bank_id: b for b in inputs.banks}
    for (oid, bank_id, t), kw in allocation.items():
        if oid in held:
            eta_d = bank_by_id[bank_id].eta_d if bank_id in bank_by_id else 1.0
            hold_kwh[bank_id, t] = hold_kwh.get((bank_id, t), 0.0) + kw * held[oid] / eta_d
        else:
            drain[bank_id, t] = drain.get((bank_id, t), 0.0) + kw

    headroom: dict[tuple[str, int, str], float] = {}
    soc: dict[tuple[str, int, str], float] = {}
    for scenario in inputs.scenarios:
        for bank in inputs.banks:
            e = bank.initial_soc_kwh
            for t in sorted(bank.max_discharge_kw):
                left = remaining.get((bank.bank_id, t), 0.0)
                allowed = bank.free_market_access and scenario.price_at(bank.bank_id, t) >= 0
                if not bank.models_soc:
                    headroom[bank.bank_id, t, scenario.scenario] = left if allowed else 0.0
                    continue
                soc[bank.bank_id, t, scenario.scenario] = e
                e -= (dt_h / bank.eta_d) * drain.get(
                    (bank.bank_id, t), 0.0
                ) + bank.self_discharge_kwh_per_h * dt_h
                floor = bank.reserve_kwh + hold_kwh.get((bank.bank_id, t), 0.0)
                energy_kw = max(0.0, (e - floor) * bank.eta_d / dt_h) if dt_h > 0 else 0.0
                h = min(left, energy_kw) if allowed else 0.0
                headroom[bank.bank_id, t, scenario.scenario] = h
                e -= (dt_h / bank.eta_d) * h
            if bank.models_soc and bank.max_discharge_kw:
                soc[bank.bank_id, max(bank.max_discharge_kw) + 1, scenario.scenario] = e
    return headroom, soc
