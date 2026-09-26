"""ERCOT_AS energy hold in the selector (Frank #6, NPRR1282): an AS award is a capacity hold. It locks
bank kW (capacity rows) but does NOT drain SoC every interval; instead the bank keeps
`kW x hold_h / eta_d` above its reserve floor while it is held (Non-Spin 4 h, ECRS 1 h)."""

from __future__ import annotations

from dataclasses import replace

import pytest

from opengrid.selector.extract import extract_plan
from opengrid.selector.gate import as_energy_hold_h
from opengrid.selector.model import build_mode_o_model
from opengrid.selector.solve import highs_solve
from opengrid.selector.types import BankSnapshot, ModelInputs, SolverSettings
from opengrid.selector.validate import validate_plan
from unit.selector.factories import binary_candidate, committed, continuous_candidate, zero_price_scenario

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


# --- sizing cases salvaged from ftbrown's #13 (bug list #6), on main's capacity-hold model --------------------


def _small_bank() -> BankSnapshot:
    """A 10 kWh bank at full charge with a 2 kWh reserve (8 kWh usable), 40 kW of power, lossless."""
    return BankSnapshot(
        bank_id="B1",
        max_discharge_kw={0: 40.0},
        initial_soc_kwh=10.0,
        capacity_kwh=10.0,
        reserve_kwh=2.0,
        eta_c=1.0,
        eta_d=1.0,
        self_discharge_kwh_per_h=0.0,
    )


def _awarded_kw(service_type: str, duration_minutes: float | None, *, n_awards: int = 1) -> float:
    """Total kW selected for `n_awards` high-value continuous 40 kW asks on the one bank, in one 15-min
    interval, each held for the hours the gate derives from (service_type, duration_minutes)."""
    hold_h = as_energy_hold_h(service_type, duration_minutes)
    category = "AS" if hold_h > 0 else "MARKET"
    candidates = tuple(
        replace(
            continuous_candidate(f"c{i}", 40.0, 500.0, (0,), ("B1",), category=category),
            service_type=service_type,
            energy_hold_h=hold_h,
        )
        for i in range(n_awards)
    )
    inputs = ModelInputs(
        intervals=(0,),
        interval_minutes=15.0,
        banks=(_small_bank(),),
        scenarios=(zero_price_scenario(range(1)),),
        committed=(),
        candidates=candidates,
        terminal_soc_slack_kwh=10.0,
    )
    outcome, plan = _solve(inputs)
    assert outcome.is_feasible, outcome.status
    ok, violations = validate_plan(inputs, plan)
    assert ok, violations
    return sum(plan.selected_q.get(c.opportunity_id, 0.0) for c in candidates)


@pytest.mark.parametrize(
    ("service_type", "duration_minutes", "expected_kw"),
    [
        ("ERCOT_AS", 240, 2.0),  # Non-Spin: 8 kWh usable / 4 h
        ("ERCOT_AS", 60, 8.0),  # ECRS: 8 kWh / 1 h
        ("ERCOT_AS", None, 8.0),  # no product duration: main's conservative ECRS default (1 h), not unheld
        (
            "ERCOT_ENERGY",
            240,
            32.0,
        ),  # non-AS: a duration is ignored, only the window energy binds (8 / 0.25 h)
    ],
)
def test_as_award_is_sized_by_its_stored_energy_duration(
    service_type: str, duration_minutes: float | None, expected_kw: float
) -> None:
    assert abs(_awarded_kw(service_type, duration_minutes) - expected_kw) < 1e-6


def test_holds_of_several_awards_on_one_bank_add_up() -> None:
    """Two Non-Spin asks on the same bank share its 8 kWh: 2 kW in total, not 2 kW each."""
    assert abs(_awarded_kw("ERCOT_AS", 240, n_awards=2) - 2.0) < 1e-6


def test_only_as_service_types_are_held() -> None:
    assert as_energy_hold_h("ERCOT_AS", 240) == 4.0
    assert as_energy_hold_h("ERCOT_AS", None) == 1.0
    for service_type in ("ERCOT_ENERGY", "HOME", "DIST_DEFERRAL", "DATA_CENTER", "UNKNOWN", None):
        assert as_energy_hold_h(service_type, 240) == 0.0
