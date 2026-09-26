"""Independent plan validator (02a S3.8's `independent_validator.check`): re-derives the constraints
from raw numbers, never from the highspy model object, so a modeling bug in `model.py` cannot also
hide itself from the check. Defense-in-depth for K1/K2/K13/C16/product-rule feasibility.
"""

from __future__ import annotations

from opengrid.selector.types import CandidateOpportunity, ExtractedPlan, ModelInputs
from opengrid.selector.value import kw_by_obligation_interval

_TOL_KW = 1e-6
_STEP_REL_TOL = 1e-4  # fraction of one increment step; absorbs float noise, not a real mis-quantization
#: SoC re-derivation tolerance (kWh; 0.1 Wh, physically nil). HiGHS accepts a MIP incumbent with row
#: residuals up to `mip_feasibility_tolerance` (1e-6) EACH, and the C1 recurrence accumulates them over
#: up to 96 steps, so the 1e-6 kW tolerance used for single rows spuriously rejected valid MIP plans
#: (Frank's flaky `test_soc_stays_within_bounds_in_every_scenario`; in production: a needless F2 fallback).
_TOL_SOC_KWH = 1e-4


def validate_plan(inputs: ModelInputs, plan: ExtractedPlan) -> tuple[bool, tuple[str, ...]]:
    """Returns (ok, violations). Any violation means the caller (02a S3.8) must fall back to F2."""
    violations: list[str] = []
    violations.extend(_check_commitment_lock(inputs, plan))
    violations.extend(_check_one_buyer(inputs, plan))
    violations.extend(_check_bank_bounds(inputs, plan))
    violations.extend(_check_product_rules(inputs, plan))
    violations.extend(_check_soc_dynamics(inputs, plan))
    violations.extend(check_mobile_storage(inputs, plan))
    return not violations, tuple(violations)


def check_mobile_storage(inputs: ModelInputs, plan: ExtractedPlan) -> list[str]:
    """Owner decision D-31: no plan row feeds a mobile unit from the fleet (a MOBILE_STORAGE obligation
    allocated to a non-mobile bank), and no plan row charges a mobile unit away from its home station."""
    violations = []
    mobile_ids = inputs.mobile_service_ids()
    mobile_banks = {b.bank_id: b for b in inputs.banks if b.is_mobile}
    for (oid, bank_id, t), kw in plan.bank_interval_allocation.items():
        if kw > _TOL_KW and oid in mobile_ids and bank_id not in mobile_banks:
            violations.append(
                f"D-31: mobile obligation {oid} fed {kw:.3f}kW from fleet bank {bank_id} at {t}"
            )
    for charges in (plan.charge_by_bank_interval_scenario, plan.solar_charge_by_bank_interval_scenario):
        for (bank_id, t, scenario), kw in charges.items():
            bank = mobile_banks.get(bank_id)
            if bank is not None and kw > _TOL_KW and not bank.charging_allowed(t):
                violations.append(
                    f"D-31: mobile unit {bank_id} charges {kw:.3f}kW away from its home station "
                    f"(interval {t}, scenario {scenario})"
                )
    return violations


def _caps(inputs: ModelInputs) -> dict[str, dict[int, float]]:
    return {bank.bank_id: bank.max_discharge_kw for bank in inputs.banks}


def _check_commitment_lock(inputs: ModelInputs, plan: ExtractedPlan) -> list[str]:
    """K13/C24: the frozen total for every committed obligation/interval must be reproduced exactly
    by the plan's bank allocation (an equality, never a reduction)."""
    violations = []
    by_oid_t = kw_by_obligation_interval(plan)
    for co in inputs.committed:
        for t, frozen_kw in co.committed_kw_by_interval.items():
            delivered = by_oid_t.get((co.obligation_id, t), 0.0)
            if delivered < frozen_kw - _TOL_KW:
                violations.append(
                    f"K13: obligation {co.obligation_id} interval {t} delivered {delivered:.3f}kW "
                    f"< frozen {frozen_kw:.3f}kW"
                )
    return violations


def _check_one_buyer(inputs: ModelInputs, plan: ExtractedPlan) -> list[str]:
    """K2: no bank/interval/scenario oversold across obligations + AS holds + free headroom."""
    violations = []
    demand: dict[tuple[str, int], float] = {}
    for (_oid, bank_id, t), kw in plan.bank_interval_allocation.items():
        demand[bank_id, t] = demand.get((bank_id, t), 0.0) + kw
    caps = _caps(inputs)
    for (bank_id, t, _scenario), kw in plan.headroom_schedule.items():
        cap = caps.get(bank_id, {}).get(t, 0.0)
        total = demand.get((bank_id, t), 0.0) + kw
        if total > cap + _TOL_KW:
            violations.append(
                f"K2: bank {bank_id} interval {t} committed+headroom {total:.3f}kW > capacity {cap:.3f}kW"
            )
    return violations


def _check_bank_bounds(inputs: ModelInputs, plan: ExtractedPlan) -> list[str]:
    violations = []
    caps = _caps(inputs)
    for (_oid, bank_id, t), kw in plan.bank_interval_allocation.items():
        if kw < -_TOL_KW:
            violations.append(f"bound: negative allocation {kw:.3f}kW on bank {bank_id} interval {t}")
        cap = caps.get(bank_id, {}).get(t, 0.0)
        if kw > cap + _TOL_KW:
            violations.append(
                f"bound: allocation {kw:.3f}kW on bank {bank_id} interval {t} exceeds capability {cap:.3f}kW"
            )
    for (bank_id, t, _scenario), kw in plan.headroom_schedule.items():
        if kw < -_TOL_KW:
            violations.append(f"bound: negative headroom {kw:.3f}kW on bank {bank_id} interval {t}")
    return violations


def _check_soc_dynamics(inputs: ModelInputs, plan: ExtractedPlan) -> list[str]:
    """K1/C2 defense-in-depth: re-derives every modeled bank's SoC bounds and its energy-balance step
    from the plan's own reported allocations/headroom/charge, independent of `model.py`'s highspy rows
    (02a S3.8's "re-derives the constraints from raw numbers")."""
    violations = []
    held = set(inputs.energy_hold_hours())  # ERCOT_AS holds lock energy, they do not drain it
    drain_by_bt: dict[tuple[str, int], float] = {}
    for (oid, b, ti), kw in plan.bank_interval_allocation.items():
        if oid not in held:
            drain_by_bt[b, ti] = drain_by_bt.get((b, ti), 0.0) + kw
    for bank in inputs.banks:
        if not bank.models_soc:
            continue
        intervals = sorted(bank.max_discharge_kw)
        for scenario in {s.scenario for s in inputs.scenarios}:
            expected_soc = bank.initial_soc_kwh
            for t in intervals:
                reported = plan.soc_by_bank_interval_scenario.get((bank.bank_id, t, scenario))
                if reported is not None and abs(reported - expected_soc) > _TOL_SOC_KWH:
                    violations.append(
                        f"C1: bank {bank.bank_id} interval {t} scenario {scenario} SoC {reported:.3f}kWh "
                        f"!= balance-derived {expected_soc:.3f}kWh"
                    )
                if not (bank.reserve_kwh - _TOL_SOC_KWH <= expected_soc <= bank.capacity_kwh + _TOL_SOC_KWH):
                    violations.append(
                        f"C2: bank {bank.bank_id} interval {t} scenario {scenario} SoC "
                        f"{expected_soc:.3f}kWh outside [{bank.reserve_kwh}, {bank.capacity_kwh}]kWh"
                    )
                discharge_total = drain_by_bt.get((bank.bank_id, t), 0.0) + plan.headroom_schedule.get(
                    (bank.bank_id, t, scenario), 0.0
                )
                charge = plan.charge_by_bank_interval_scenario.get(
                    (bank.bank_id, t, scenario), 0.0
                ) + plan.solar_charge_by_bank_interval_scenario.get((bank.bank_id, t, scenario), 0.0)
                dt_h = inputs.interval_hours
                expected_soc = (
                    expected_soc
                    + bank.eta_c * charge * dt_h
                    - (dt_h / bank.eta_d) * discharge_total
                    - bank.self_discharge_kwh_per_h * dt_h
                )
            # C15 (terminal energy) is a penalized target in the model, not a hard bound, so ending
            # below it is priced, never a validity violation; C1/C2 above stay hard.
    return violations


def _is_representable(quantity: float, c: CandidateOpportunity) -> bool:
    if quantity > c.requested_kw + _TOL_KW:
        return False
    if c.variable_kind == "BINARY":
        return abs(quantity - c.requested_kw) <= _TOL_KW
    if c.increment_kw <= _TOL_KW:
        return quantity >= c.min_qty_kw - _TOL_KW
    if quantity < c.min_qty_kw - _TOL_KW:
        return False
    steps = (quantity - c.min_qty_kw) / c.increment_kw
    return abs(steps - round(steps)) <= _STEP_REL_TOL


def _check_product_rules(inputs: ModelInputs, plan: ExtractedPlan) -> list[str]:
    """S3.6: a selected candidate's realized quantity must be representable under its product rule.

    Checked with direct modular arithmetic (not by round-tripping `core.products.round_quantity` a
    second time): re-rounding an already-rounded float can drift by one increment step purely from
    float representation noise (`Decimal(str(x))` does not recover the exact rational `x` was derived
    from), which would make this independent check flag a perfectly valid plan.
    """
    violations = []
    by_oid_t = kw_by_obligation_interval(plan)
    for c in inputs.candidates:
        if c.variable_kind == "BINARY":
            selected = plan.selected_x.get(c.opportunity_id, False)
            expected = c.requested_kw if selected else 0.0
        else:
            expected = plan.selected_q.get(c.opportunity_id, 0.0)

        if expected > _TOL_KW and not _is_representable(expected, c):
            violations.append(
                f"product-rule: opportunity {c.opportunity_id} quantity {expected:.3f}kW is not "
                f"representable under min_qty={c.min_qty_kw}/increment={c.increment_kw}"
            )

        for t in c.window_intervals:
            delivered = by_oid_t.get((c.opportunity_id, t), 0.0)
            if abs(delivered - expected) > _TOL_KW:
                violations.append(
                    f"product-rule: opportunity {c.opportunity_id} interval {t} delivered "
                    f"{delivered:.3f}kW != selected quantity {expected:.3f}kW"
                )
    return violations
