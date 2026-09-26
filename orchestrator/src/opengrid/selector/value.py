"""Plan economics shared by the LP objective, the rule baseline and the shadow comparison (ES05-S07,
KPI-22). Pure: no I/O, no solver.

- `wear_usd_per_kwh`: the D8 wear coefficient per discharged kWh, derived from the ONE wear formula
  (`opengrid.core.economics.wear_cost`) so the objective, this evaluator and settle can never drift.
- `plan_net_value`: the expected net value of ANY plan (LP or rule) from its own schedules, so the
  LP-vs-rule value-added figure compares like with like. The LP's `objective_value` is not used for
  this: it carries penalty terms (terminal/hold slacks) the rule plan has no counterpart for.
- `shadow_comparison`: the LP and rule plans side by side, per obligation-interval, with the best
  competing candidate value for each committed obligation's locked capacity (settle's forgone-upside
  input, `settle.profitability.compute_forgone_upside`).
"""

from __future__ import annotations

from decimal import Decimal

from opengrid.core.economics import wear_cost
from opengrid.selector.types import (
    BankSnapshot,
    ExtractedPlan,
    ModelInputs,
    PlanValue,
    ShadowComparison,
    ShadowObligationInterval,
)

_ONE_KWH = Decimal("1")
_TOL_KW = 1e-6


def wear_usd_per_kwh(bank: BankSnapshot) -> float:
    """09 D8: the wear of one AC kWh discharged by `bank`, at its asset-class rate."""
    return float(wear_cost(_ONE_KWH, Decimal(repr(bank.wear_usd_per_kwh))))


def discharged_by_bank(inputs: ModelInputs, plan: ExtractedPlan) -> dict[tuple[str, int, str], float]:
    """Expected discharge power (kW) per bank/interval/scenario: every non-hold delivery plus headroom.
    Capacity holds (AS awards, regulated need-basis reserves) discharge nothing while held (D8)."""
    held = inputs.energy_hold_hours()
    delivered: dict[tuple[str, int], float] = {}
    for (oid, bank_id, t), kw in plan.bank_interval_allocation.items():
        if oid not in held:
            delivered[bank_id, t] = delivered.get((bank_id, t), 0.0) + kw
    out: dict[tuple[str, int, str], float] = {}
    for scenario in inputs.scenarios:
        for bank in inputs.banks:
            for t in bank.max_discharge_kw:
                headroom = plan.headroom_schedule.get((bank.bank_id, t, scenario.scenario), 0.0)
                out[bank.bank_id, t, scenario.scenario] = delivered.get((bank.bank_id, t), 0.0) + headroom
    return out


def soc_trajectory(
    inputs: ModelInputs,
    plan: ExtractedPlan,
    bank: BankSnapshot,
    scenario: str,
    discharged: dict[tuple[str, int, str], float],
) -> dict[int, float]:
    """SoC (kWh) at the start of each interval (and the terminal index), re-derived from the plan's own
    schedules (`discharged` = `discharged_by_bank(inputs, plan)`) with the model's affine C1 step."""
    dt_h = inputs.interval_hours
    intervals = sorted(bank.max_discharge_kw)
    soc = {intervals[0]: bank.initial_soc_kwh} if intervals else {}
    e = bank.initial_soc_kwh
    for t in intervals:
        key = (bank.bank_id, t, scenario)
        charge = plan.charge_by_bank_interval_scenario.get(
            key, 0.0
        ) + plan.solar_charge_by_bank_interval_scenario.get(key, 0.0)
        e = (
            e
            + bank.eta_c * charge * dt_h
            - (dt_h / bank.eta_d) * discharged.get((bank.bank_id, t, scenario), 0.0)
            - bank.self_discharge_kwh_per_h * dt_h
        )
        soc[t + 1] = e
    return soc


def replacement_cost_usd_per_kwh_dc(inputs: ModelInputs, bank: BankSnapshot) -> float:
    """What one stored (DC) kWh costs `bank` to put back: its expected charging cost over the horizon
    (zone price + M1, or the regulated tariff), grossed up for charging losses. Used to value the change
    in stored energy between the start and the end of the horizon, identically for every plan."""
    total = 0.0
    weight = 0.0
    for scenario in inputs.scenarios:
        for t in bank.max_discharge_kw:
            total += scenario.probability * bank.charge_cost_usd_per_kwh(
                scenario.price_at(bank.bank_id, t), t
            )
            weight += scenario.probability
    if weight <= 0.0 or bank.eta_c <= 0.0:
        return 0.0
    return total / weight / bank.eta_c


def plan_net_value(inputs: ModelInputs, plan: ExtractedPlan) -> PlanValue:
    """Expected net value of `plan` (09 S1.5 stage-F terms without penalty slacks), from its schedules."""
    dt_h = inputs.interval_hours
    value_per_kwh = {c.opportunity_id: c.value_per_mwh / 1000.0 for c in inputs.candidates}
    obligation_revenue = sum(
        value_per_kwh.get(oid, 0.0) * kw * dt_h for (oid, _b, _t), kw in plan.bank_interval_allocation.items()
    )
    discharged = discharged_by_bank(inputs, plan)
    # D8 on held awards: only their expected deployment (psi * r) is discharged, so only that pays wear.
    shares = inputs.expected_deployment_shares()
    deployed_kwh_by_bank: dict[str, float] = {}
    for (oid, bank_id, _t), kw in plan.bank_interval_allocation.items():
        if oid in shares:
            deployed_kwh_by_bank[bank_id] = deployed_kwh_by_bank.get(bank_id, 0.0) + shares[oid] * kw * dt_h
    energy_revenue = 0.0
    charging_energy = 0.0
    delivery = 0.0
    wear = Decimal("0")
    terminal = 0.0
    for bank in inputs.banks:
        discharged_kwh = deployed_kwh_by_bank.get(bank.bank_id, 0.0)
        for scenario in inputs.scenarios:
            p = scenario.probability
            for t in bank.max_discharge_kw:
                price = scenario.price_at(bank.bank_id, t)
                headroom = plan.headroom_schedule.get((bank.bank_id, t, scenario.scenario), 0.0)
                energy_revenue += p * headroom * dt_h * price / 1000.0
                discharged_kwh += p * discharged.get((bank.bank_id, t, scenario.scenario), 0.0) * dt_h
                charge_kwh = (
                    plan.charge_by_bank_interval_scenario.get((bank.bank_id, t, scenario.scenario), 0.0)
                    * dt_h
                )
                if charge_kwh > 0.0:
                    all_in = bank.charge_cost_usd_per_kwh(price, t)
                    m1 = 0.0 if t in bank.charge_price_usd_per_kwh else bank.delivery_charge_usd_per_kwh
                    charging_energy += p * charge_kwh * (all_in - m1)
                    delivery += p * charge_kwh * m1
                solar_kwh = (
                    plan.solar_charge_by_bank_interval_scenario.get((bank.bank_id, t, scenario.scenario), 0.0)
                    * dt_h
                )
                if solar_kwh > 0.0:
                    solar_cost = bank.solar_cost_usd_per_kwh
                    charging_energy += p * solar_kwh * (price / 1000.0 if solar_cost is None else solar_cost)
        wear += wear_cost(Decimal(repr(max(discharged_kwh, 0.0))), Decimal(repr(bank.wear_usd_per_kwh)))
        if bank.models_soc and bank.max_discharge_kw:
            replacement = replacement_cost_usd_per_kwh_dc(inputs, bank)
            terminal_t = max(bank.max_discharge_kw) + 1
            for scenario in inputs.scenarios:
                end = soc_trajectory(inputs, plan, bank, scenario.scenario, discharged)[terminal_t]
                terminal += scenario.probability * (end - bank.initial_soc_kwh) * replacement
    return PlanValue(
        obligation_revenue=obligation_revenue,
        energy_revenue=energy_revenue,
        charging_energy_cost=charging_energy,
        delivery_charge=delivery,
        wear=float(wear),
        terminal_energy_value=terminal,
    )


def kw_by_obligation_interval(plan: ExtractedPlan) -> dict[tuple[str, int], float]:
    """The plan's allocation summed per (obligation or opportunity id, interval) over banks."""
    out: dict[tuple[str, int], float] = {}
    for (oid, _b, t), kw in plan.bank_interval_allocation.items():
        out[oid, t] = out.get((oid, t), 0.0) + kw
    return out


def shadow_comparison(
    inputs: ModelInputs, lp_plan: ExtractedPlan, rule_plan: ExtractedPlan
) -> ShadowComparison:
    """ES05-S07: LP vs rule baseline on identical inputs, plus KPI-22's forgone upside.

    A committed obligation's locked capacity has a "best competing value" at interval t when a candidate
    that could use one of its banks at t was left (partly) unserved by the LP: the lock (K13) is what
    kept that value out. The plan-level `forgone_upside` sums `max(0, best - own value) x kW x dt`."""
    dt_h = inputs.interval_hours
    lp_kw = kw_by_obligation_interval(lp_plan)
    rule_kw = kw_by_obligation_interval(rule_plan)
    rows: list[ShadowObligationInterval] = []
    forgone = 0.0

    unserved_at: dict[int, list[tuple[float, frozenset[str]]]] = {}
    for c in inputs.candidates:
        requested = c.requested_kw
        for t in c.window_intervals:
            served = lp_kw.get((c.opportunity_id, t), 0.0)
            rows.append(
                ShadowObligationInterval(
                    obligation_id=c.obligation_id,
                    interval=t,
                    lp_kw=served,
                    rule_kw=rule_kw.get((c.opportunity_id, t), 0.0),
                )
            )
            if served < requested - _TOL_KW:
                unserved_at.setdefault(t, []).append(
                    (c.value_per_mwh / 1000.0, frozenset(c.eligible_bank_ids))
                )

    for co in inputs.committed:
        banks = frozenset(co.eligible_bank_ids)
        own = co.value_per_mwh / 1000.0
        for t, kw in sorted(co.committed_kw_by_interval.items()):
            competing = [value for value, eligible in unserved_at.get(t, []) if eligible & banks]
            best = max(competing) if competing else None
            if best is not None and best > own:
                forgone += (best - own) * kw * dt_h
            rows.append(
                ShadowObligationInterval(
                    obligation_id=co.obligation_id,
                    interval=t,
                    lp_kw=lp_kw.get((co.obligation_id, t), 0.0),
                    rule_kw=rule_kw.get((co.obligation_id, t), 0.0),
                    best_competing_value_per_kwh=best,
                )
            )

    return ShadowComparison(
        lp_value=plan_net_value(inputs, lp_plan),
        rule_value=plan_net_value(inputs, rule_plan),
        rule_plan=rule_plan,
        forgone_upside=forgone,
        obligation_intervals=tuple(rows),
    )
