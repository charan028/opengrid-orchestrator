"""Selector SoC dynamics (02a S3.2/S3.3 C1 energy balance, C2 SOC bounds, C15 terminal energy).

Fixes the correctness bug where a day-ahead plan, built only from `fleet.capability`'s per-interval
*power* envelope, could commit more energy across a window than a bank's SoC can actually deliver.
`test_energy_exhausted_declines_a_later_commitment` is the hand-computable regression for exactly that
bug; `test_soc_stays_within_bounds_in_every_scenario` is the Hypothesis property BUILD.md asks for.
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from opengrid.selector.extract import extract_plan
from opengrid.selector.model import build_mode_o_model
from opengrid.selector.solve import highs_solve
from opengrid.selector.types import BankSnapshot, ModelInputs, SolverSettings
from opengrid.selector.validate import validate_plan
from unit.selector.factories import binary_candidate, committed, three_point_scenarios, zero_price_scenario

FAST_SETTINGS = SolverSettings(mip_rel_gap=0.01, time_limit_s=10.0)


def _bank_with_soc(**overrides: object) -> BankSnapshot:
    defaults: dict[str, object] = {
        "bank_id": "B1",
        "max_discharge_kw": {0: 40.0, 1: 40.0},
        "max_charge_kw": {},
        "initial_soc_kwh": 10.0,
        "capacity_kwh": 10.0,
        "reserve_kwh": 0.0,
        "eta_c": 1.0,
        "eta_d": 1.0,
        "self_discharge_kwh_per_h": 0.0,
    }
    defaults.update(overrides)
    return BankSnapshot(**defaults)  # type: ignore[arg-type]


def test_energy_exhausted_declines_a_later_commitment() -> None:
    """A 10kWh bank, fully drained by a committed obligation in interval 0 (40kW * 0.25h = 10kWh),
    has 0kWh left -- at its reserve floor -- at the start of interval 1. A binary candidate wanting
    40kW there must be declined even though the bank's raw *power* envelope (40kW) would allow it;
    only the energy (SoC) constraint added by this fix rules it out."""
    bank = _bank_with_soc()
    scenario = zero_price_scenario(range(2))
    locked = (committed("o-commit", {0: 40.0}, ("B1",)),)
    candidate = binary_candidate("c1", 40.0, 100.0, (1,), ("B1",))
    inputs = ModelInputs(
        intervals=(0, 1),
        interval_minutes=15.0,
        banks=(bank,),
        scenarios=(scenario,),
        committed=locked,
        candidates=(candidate,),
        # No charging capability configured on this bank (`max_charge_kw={}`), so ending the 2-interval
        # horizon back at the initial 10kWh is physically impossible -- allow the full drawdown; this
        # test is about C1's energy balance mid-horizon, not C15's terminal-energy floor.
        terminal_soc_slack_kwh=10.0,
    )

    built = build_mode_o_model(inputs)
    outcome = highs_solve(built, FAST_SETTINGS)
    plan = extract_plan(built, outcome, "L-ID")

    assert outcome.is_feasible, outcome.status
    assert plan.selected_x.get("c1", False) is False  # declined: no energy left to serve it
    # K13 still holds: the committed obligation's own frozen delivery is untouched.
    assert plan.bank_interval_allocation.get(("o-commit", "B1", 0), 0.0) == 40.0
    # The bank is at its reserve floor at the start of interval 1, per the hand-computed balance.
    assert abs(plan.soc_by_bank_interval_scenario[("B1", 1, "P50")] - 0.0) < 1e-6

    ok, violations = validate_plan(inputs, plan)
    assert ok, violations


def test_declined_candidate_would_have_fit_the_bare_power_envelope() -> None:
    """Sanity check on the hand-computed case above: without SoC modeling (`capacity_kwh=0`), the same
    candidate is perfectly schedulable -- proving the decline above comes from the energy constraint,
    not some unrelated infeasibility."""
    bank = BankSnapshot(bank_id="B1", max_discharge_kw={0: 40.0, 1: 40.0})  # no energy envelope
    scenario = zero_price_scenario(range(2))
    locked = (committed("o-commit", {0: 40.0}, ("B1",)),)
    candidate = binary_candidate("c1", 40.0, 100.0, (1,), ("B1",))
    inputs = ModelInputs(
        intervals=(0, 1),
        interval_minutes=15.0,
        banks=(bank,),
        scenarios=(scenario,),
        committed=locked,
        candidates=(candidate,),
    )

    built = build_mode_o_model(inputs)
    outcome = highs_solve(built, FAST_SETTINGS)
    plan = extract_plan(built, outcome, "L-ID")

    assert outcome.is_feasible
    assert plan.selected_x.get("c1", False) is True


@given(
    capacity=st.floats(min_value=1.0, max_value=50.0, allow_nan=False, allow_infinity=False),
    reserve_frac=st.floats(min_value=0.0, max_value=0.5, allow_nan=False, allow_infinity=False),
    initial_frac=st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False),
    committed_kw=st.floats(min_value=0.0, max_value=20.0, allow_nan=False, allow_infinity=False),
    candidate_kw=st.floats(min_value=0.1, max_value=20.0, allow_nan=False, allow_infinity=False),
)
@settings(max_examples=40, deadline=None)
def test_soc_stays_within_bounds_in_every_scenario(
    capacity: float, reserve_frac: float, initial_frac: float, committed_kw: float, candidate_kw: float
) -> None:
    """For any randomized bank/commitment/candidate instance, every scenario's SoC trajectory the LP
    reports stays within [reserve, capacity] at every interval (02a S3.3 C2)."""
    reserve = reserve_frac * capacity
    initial_soc = reserve + initial_frac * (capacity - reserve)
    bank = _bank_with_soc(
        max_discharge_kw=dict.fromkeys(range(3), 20.0),
        max_charge_kw=dict.fromkeys(range(3), 10.0),
        capacity_kwh=capacity,
        reserve_kwh=reserve,
        initial_soc_kwh=initial_soc,
        eta_c=0.9487,
        eta_d=0.9487,
    )
    scenarios = three_point_scenarios(range(3), {"P10": 10.0, "P50": 20.0, "P90": 30.0})
    committed_kw = min(committed_kw, 20.0)
    locked = (committed("o-commit", {0: committed_kw}, ("B1",)),) if committed_kw > 1e-9 else ()
    candidate = binary_candidate("c1", candidate_kw, 50.0, (1, 2), ("B1",))
    inputs = ModelInputs(
        intervals=(0, 1, 2),
        interval_minutes=15.0,
        banks=(bank,),
        scenarios=scenarios,
        committed=locked,
        candidates=(candidate,),
    )

    built = build_mode_o_model(inputs)
    outcome = highs_solve(built, FAST_SETTINGS)
    if not outcome.is_feasible:
        return  # an infeasible random instance (e.g. an over-tight commitment) has no SoC to check

    plan = extract_plan(built, outcome, "L-ID")
    assert plan.soc_by_bank_interval_scenario  # SoC was actually modeled for this bank

    tol = 1e-4
    for (_bank_id, _t, _scenario), soc in plan.soc_by_bank_interval_scenario.items():
        assert reserve - tol <= soc <= capacity + tol

    ok, violations = validate_plan(inputs, plan)
    assert ok, violations
