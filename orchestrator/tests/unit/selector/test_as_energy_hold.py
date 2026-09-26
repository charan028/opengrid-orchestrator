"""Selector AS stored-energy hold (NPRR1282: Non-Spin 4 h, ECRS 1 h; bug list #6).

An AS award must be backed by `sustained_hours` of energy per awarded kW above the bank's reserve for as
long as it is held. Before this fix the SoC balance only charged a 15-min Non-Spin window for its own
window, so a bank could "hold" Non-Spin it could sustain for minutes, not hours.
"""

from __future__ import annotations

from dataclasses import replace

from opengrid.selector.extract import extract_plan
from opengrid.selector.model import build_mode_o_model
from opengrid.selector.solve import highs_solve
from opengrid.selector.types import BankSnapshot, ModelInputs, SolverSettings
from opengrid.selector.validate import validate_plan
from unit.selector.factories import continuous_candidate, zero_price_scenario

FAST_SETTINGS = SolverSettings(mip_rel_gap=0.01, time_limit_s=10.0)


def _solve(sustained_hours: float, *, category: str = "AS") -> float:
    """A 10 kWh bank at full charge with a 2 kWh reserve (8 kWh usable), 40 kW of power, lossless.
    One 15-min interval; a high-value continuous candidate asks for 40 kW."""
    bank = BankSnapshot(
        bank_id="B1",
        max_discharge_kw={0: 40.0},
        initial_soc_kwh=10.0,
        capacity_kwh=10.0,
        reserve_kwh=2.0,
        eta_c=1.0,
        eta_d=1.0,
        self_discharge_kwh_per_h=0.0,
    )
    candidate = replace(
        continuous_candidate("as1", 40.0, 500.0, (0,), ("B1",), category=category),
        sustained_hours=sustained_hours,
    )
    inputs = ModelInputs(
        intervals=(0,),
        interval_minutes=15.0,
        banks=(bank,),
        scenarios=(zero_price_scenario(range(1)),),
        committed=(),
        candidates=(candidate,),
        terminal_soc_slack_kwh=10.0,
    )
    built = build_mode_o_model(inputs)
    outcome = highs_solve(built, FAST_SETTINGS)
    plan = extract_plan(built, outcome, "L-ID")
    assert outcome.is_feasible, outcome.status
    ok, violations = validate_plan(inputs, plan)
    assert ok, violations
    return plan.selected_q.get("as1", 0.0)


def test_non_spin_award_is_capped_by_four_hours_of_energy_above_reserve() -> None:
    """8 kWh usable / 4 h = 2 kW of Non-Spin, not the 32 kW the 15-min window alone would allow."""
    assert abs(_solve(4.0) - 2.0) < 1e-6


def test_ecrs_award_is_capped_by_one_hour_of_energy_above_reserve() -> None:
    assert abs(_solve(1.0) - 8.0) < 1e-6


def test_without_a_duration_only_the_window_energy_binds() -> None:
    """Regression baseline: no stored-energy requirement -> 8 kWh / 0.25 h = 32 kW (the old behavior)."""
    assert abs(_solve(0.0) - 32.0) < 1e-6


def test_hold_applies_to_as_candidates_only() -> None:
    """A MARKET candidate carrying a duration (e.g. a mislabelled row) is not held to it."""
    assert abs(_solve(4.0, category="MARKET") - 32.0) < 1e-6
