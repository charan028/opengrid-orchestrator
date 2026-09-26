"""ERCOT_AS energy hold in the selector (Frank #6, NPRR1282): an AS award is a capacity hold. It locks
bank kW (capacity rows) but does NOT drain SoC every interval; instead the bank keeps
`kW x hold_h / eta_d` above its reserve floor while it is held (Non-Spin 4 h, ECRS 1 h)."""

from __future__ import annotations

from dataclasses import replace

from opengrid.selector.extract import extract_plan
from opengrid.selector.model import build_mode_o_model
from opengrid.selector.solve import highs_solve
from opengrid.selector.types import BankSnapshot, ModelInputs, SolverSettings
from opengrid.selector.validate import validate_plan
from unit.selector.factories import binary_candidate, committed, zero_price_scenario

FAST_SETTINGS = SolverSettings(mip_rel_gap=0.01, time_limit_s=10.0)


def _bank(initial_soc_kwh: float) -> BankSnapshot:
    return BankSnapshot(
        bank_id="B1",
        max_discharge_kw={0: 100.0, 1: 100.0},
        max_charge_kw={},
        initial_soc_kwh=initial_soc_kwh,
        capacity_kwh=500.0,
        reserve_kwh=100.0,
        eta_c=1.0,
        eta_d=1.0,
        self_discharge_kwh_per_h=0.0,
    )


def _solve(inputs: ModelInputs):
    built = build_mode_o_model(inputs)
    outcome = highs_solve(built, FAST_SETTINGS)
    return outcome, extract_plan(built, outcome, "L-ID")


def _inputs(initial_soc_kwh: float, *, hold_h: float, committed_rows=()) -> ModelInputs:
    candidate = replace(
        binary_candidate("as-1", 50.0, 20.0, (0, 1), ("B1",)), category="AS", energy_hold_h=hold_h
    )
    return ModelInputs(
        intervals=(0, 1),
        interval_minutes=15.0,
        banks=(_bank(initial_soc_kwh),),
        scenarios=(zero_price_scenario(range(2)),),
        committed=tuple(committed_rows),
        candidates=(candidate,),
        terminal_soc_slack_kwh=500.0,
    )


def test_a_non_spin_award_is_selected_only_where_its_four_hour_energy_is_held():
    """50 kW Non-Spin needs 200 kWh above the 100 kWh reserve. 350 kWh SoC holds it (and the award does
    not drain it); 250 kWh does not, so the award is declined even though the bank has the kW."""
    outcome, plan = _solve(_inputs(350.0, hold_h=4.0))
    assert outcome.is_feasible, outcome.status
    assert plan.selected_x.get("as-1") is True
    # A hold does not discharge: SoC is unchanged across the held intervals.
    assert abs(plan.soc_by_bank_interval_scenario[("B1", 1, "P50")] - 350.0) < 1e-6
    ok, violations = validate_plan(_inputs(350.0, hold_h=4.0), plan)
    assert ok, violations

    outcome, plan = _solve(_inputs(250.0, hold_h=4.0))
    assert outcome.is_feasible, outcome.status
    assert plan.selected_x.get("as-1", False) is False


def test_a_committed_hold_the_bank_cannot_cover_stays_feasible():
    """An award already COMMITTED stays locked (K13) even if the bank cannot cover its energy hold: the
    shortfall is a priced slack, never an infeasible gate (RULE_FALLBACK)."""
    held = replace(committed("as-committed", {0: 80.0, 1: 80.0}, ("B1",)), energy_hold_h=4.0)
    inputs = replace(_inputs(150.0, hold_h=4.0), committed=(held,))
    outcome, plan = _solve(inputs)
    assert outcome.is_feasible, outcome.status
    assert plan.bank_interval_allocation.get(("as-committed", "B1", 0), 0.0) == 80.0
    assert plan.selected_x.get("as-1", False) is False  # no new award on a bank already short
