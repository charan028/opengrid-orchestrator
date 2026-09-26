"""Hand-computable Mode O instances with known optima (BUILD.md "small hand-computable instances")."""

from __future__ import annotations

import math

from opengrid.selector.extract import extract_plan
from opengrid.selector.model import build_mode_o_model
from opengrid.selector.solve import highs_solve
from opengrid.selector.types import SolverSettings
from opengrid.selector.validate import validate_plan
from unit.selector.factories import (
    binary_candidate,
    committed,
    continuous_candidate,
    make_bank,
    semi_continuous_candidate,
    simple_inputs,
    zero_price_scenario,
)

FAST_SETTINGS = SolverSettings(mip_rel_gap=0.001, time_limit_s=10.0)


def test_ts_05_hand_computable_binary_beats_continuous_on_scarce_capacity():
    """1 bank, 10kW cap, 2 intervals. c1 (BINARY, 10kW, $100/MWh) vs c2 (CONTINUOUS, 5kW, $50/MWh);
    they cannot both fit. Hand optimum: take c1 fully (obj=0.5), leave c2 at 0 (its obj would be 0.125)."""
    bank = make_bank("B1", 10.0, range(2))
    scenario = zero_price_scenario(range(2))
    c1 = binary_candidate("c1", 10.0, 100.0, (0, 1), ("B1",))
    c2 = continuous_candidate("c2", 5.0, 50.0, (0, 1), ("B1",))
    inputs = simple_inputs((bank,), (scenario,), (), (c1, c2), n_intervals=2)

    built = build_mode_o_model(inputs)
    outcome = highs_solve(built, FAST_SETTINGS)
    plan = extract_plan(built, outcome, "L-ID")

    assert outcome.status == "OPTIMAL"
    assert plan.selected_x["c1"] is True
    assert plan.selected_q.get("c2", 0.0) == 0.0
    assert math.isclose(plan.objective_value, 0.5, rel_tol=1e-6)

    ok, violations = validate_plan(inputs, plan)
    assert ok, violations


def test_ts_05_02_commitment_lock_wins_over_higher_value_candidate():
    """K13: a committed 6kW obligation on a 10kW bank must be delivered in full even though a much
    richer (BINARY, 10kW, $1000/MWh) candidate cannot then fit in the remaining 4kW."""
    bank = make_bank("B1", 10.0, range(1))
    scenario = zero_price_scenario(range(1))
    locked = committed("o-commit", {0: 6.0}, ("B1",))
    rich_candidate = binary_candidate("c1", 10.0, 1000.0, (0,), ("B1",))
    inputs = simple_inputs((bank,), (scenario,), (locked,), (rich_candidate,), n_intervals=1)

    built = build_mode_o_model(inputs)
    outcome = highs_solve(built, FAST_SETTINGS)
    plan = extract_plan(built, outcome, "L-ID")

    assert outcome.status == "OPTIMAL"
    assert plan.selected_x["c1"] is False
    assert plan.bank_interval_allocation["o-commit", "B1", 0] == 6.0

    ok, violations = validate_plan(inputs, plan)
    assert ok, violations


def test_ts_04_semi_continuous_snaps_to_increment():
    """SEMI_CONTINUOUS: min_qty=2, increment=1, requested(max)=5; only 4.5kW of headroom -- optimum
    quantizes down to 4kW (the richest representable point <= 4.5), never a fractional 4.5."""
    bank = make_bank("B1", 4.5, range(1))
    scenario = zero_price_scenario(range(1))
    c = semi_continuous_candidate("c1", 5.0, 2.0, 1.0, 10.0, (0,), ("B1",))
    inputs = simple_inputs((bank,), (scenario,), (), (c,), n_intervals=1)

    built = build_mode_o_model(inputs)
    outcome = highs_solve(built, FAST_SETTINGS)
    plan = extract_plan(built, outcome, "L-ID")

    assert outcome.status == "OPTIMAL"
    assert plan.selected_q["c1"] == 4.0

    ok, violations = validate_plan(inputs, plan)
    assert ok, violations


def test_zero_value_firm_candidate_with_degradation_cost_is_never_selected():
    """Regression, real-shaped (`qa/merge-notes.md` S15): before the intake fix, a DIST_DEFERRAL
    opportunity was admitted with `value_per_mwh=None` -> persisted as null -> read back by
    `selector.gate.load_candidates` as `0.0`. This model's objective coefficient for a candidate is
    `value_per_mwh/1000 - degradation_cost_per_kwh` (`model.py`'s objective section); with a real
    demo-shaped degradation cost (0.03 $/kWh, `0002_seed_demo.sql`) and zero value, that coefficient is
    strictly negative, so the *correct* optimum -- given that flawed input -- is `x_o=0` no matter how
    much free capacity is available. This was never a solver bug: 94 `OPTIMAL` solves that all
    correctly refused a candidate whose only recorded value was zero. Locks in that this is still true
    (so nobody "fixes" it by special-casing the model), and that a real positive value flips the
    decision -- the actual fix belongs in intake (`contracts/intake/deferral.py`'s
    `DEFAULT_DEFERRAL_VALUE_USD_PER_MWH`), not here."""
    bank = make_bank("B1", 500.0, range(1))
    scenario = zero_price_scenario(range(1))
    unpriced = semi_continuous_candidate(
        "deferral-o1", 500.0, 1.0, 1.0, 0.0, (0,), ("B1",), category="FIRM", degradation_cost_per_kwh=0.03
    )
    inputs = simple_inputs((bank,), (scenario,), (), (unpriced,), n_intervals=1)

    built = build_mode_o_model(inputs)
    outcome = highs_solve(built, FAST_SETTINGS)
    plan = extract_plan(built, outcome, "L-ID")

    assert outcome.status == "OPTIMAL"
    assert plan.selected_q.get("deferral-o1", 0.0) == 0.0

    # The real fix: intake records a real capacity-payment value (e.g. $120/MWh), which clears the
    # degradation cost and flips the optimum to fully select the candidate.
    priced = semi_continuous_candidate(
        "deferral-o1", 500.0, 1.0, 1.0, 120.0, (0,), ("B1",), category="FIRM", degradation_cost_per_kwh=0.03
    )
    inputs_priced = simple_inputs((bank,), (scenario,), (), (priced,), n_intervals=1)
    built_priced = build_mode_o_model(inputs_priced)
    outcome_priced = highs_solve(built_priced, FAST_SETTINGS)
    plan_priced = extract_plan(built_priced, outcome_priced, "L-ID")

    assert outcome_priced.status == "OPTIMAL"
    assert plan_priced.selected_q["deferral-o1"] == 500.0

    ok, violations = validate_plan(inputs_priced, plan_priced)
    assert ok, violations


def test_as_capacity_hold_is_not_charged_cycling_degradation_cost():
    """Regression, real-shaped (live 2026-09-25/26 diagnosis after the S15 intake fix deployed): once
    intake correctly generated `ERCOT_AS` opportunities (`value_per_mwh=5.37` -- a real live NSPIN MCPC,
    `og.opportunity` dump), the LP *still* never selected any of them, even with real bank capacity.
    `model.py`'s objective previously charged every candidate the full cycling `degradation_cost_per_kwh`
    (0.03 $/kWh = $30/MWh-equivalent, `0002_seed_demo.sql`) regardless of `category` -- for a capacity
    *hold* (AS/FIRM), whose value is a capacity payment/MCPC, not an energy-arbitrage spread, that
    overwhelmed any realistic MCPC ($5.37/MWh << $30/MWh), so the objective coefficient
    (`value_per_mwh/1000 - degradation_cost_per_kwh`) was always negative and `q_o=0` was the solver's
    correct answer given that flawed cost basis -- not an infeasibility, not a units/window bug. Fixed:
    `degradation_cost_per_kwh` now only applies to `MARKET` (real energy-cycling) candidates."""
    bank = make_bank("B1", 500.0, range(1))
    scenario = zero_price_scenario(range(1))
    as_candidate = semi_continuous_candidate(
        "as-o1", 500.0, 100.0, 100.0, 5.37, (0,), ("B1",), category="AS", degradation_cost_per_kwh=0.03
    )
    inputs = simple_inputs((bank,), (scenario,), (), (as_candidate,), n_intervals=1)

    built = build_mode_o_model(inputs)
    outcome = highs_solve(built, FAST_SETTINGS)
    plan = extract_plan(built, outcome, "L-ID")

    assert outcome.status == "OPTIMAL"
    assert plan.selected_q["as-o1"] == 500.0  # fully selected: no cycling degradation charged against it

    ok, violations = validate_plan(inputs, plan)
    assert ok, violations

    # Same numbers, but MARKET (real energy cycling): the degradation cost still applies and correctly
    # blocks selection -- this fix must not accidentally exempt ERCOT_ENERGY candidates too.
    market_candidate = semi_continuous_candidate(
        "market-o1",
        500.0,
        100.0,
        100.0,
        5.37,
        (0,),
        ("B1",),
        category="MARKET",
        degradation_cost_per_kwh=0.03,
    )
    inputs_market = simple_inputs((bank,), (scenario,), (), (market_candidate,), n_intervals=1)
    built_market = build_mode_o_model(inputs_market)
    outcome_market = highs_solve(built_market, FAST_SETTINGS)
    plan_market = extract_plan(built_market, outcome_market, "L-ID")

    assert outcome_market.status == "OPTIMAL"
    assert plan_market.selected_q.get("market-o1", 0.0) == 0.0


def test_c16_non_anticipativity_is_structural():
    """C16: `x_o`/`q_o`/`ybar_{o,b,t}` are declared exactly once per (o,b,t), never per scenario --
    the model has no way to make a different first-stage choice depending on which of P10/P50/P90
    "turns out true", because there is only ever one such variable, not one per scenario."""
    from unit.selector.factories import three_point_scenarios

    bank = make_bank("B1", 10.0, range(1))
    scenarios = three_point_scenarios(range(1), {"P10": 0.0, "P50": 0.0, "P90": 500.0})
    c1 = binary_candidate("c1", 10.0, 100.0, (0,), ("B1",))
    inputs = simple_inputs((bank,), scenarios, (), (c1,), n_intervals=1)

    built = build_mode_o_model(inputs)

    assert set(built.x_vars) == {"c1"}  # keyed by opportunity_id only, no scenario dimension
    for key in built.ybar_vars:
        assert len(key) == 3  # (obligation_id, bank_id, interval) -- no scenario component
    # only the second-stage headroom schedule is scenario-indexed, by design (C16's recourse variable).
    assert all(len(key) == 3 for key in built.h_vars)
    assert {key[2] for key in built.h_vars} == {"P10", "P50", "P90"}
