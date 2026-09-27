"""Issue #43 A6: an ERCOT_ENERGY candidate's wear and recharge cost are counted once, not twice.

Intake admits an arbitrage interval when its spread (discharge - charge / eta_rt - wear) is positive; the
selector's objective charges wear on every discharged kWh (09 D8) and the recharge through its charge
variables (09 C27/D5). When intake also emitted the spread as the candidate's value, both costs were paid
twice and a paying one-candidate case was declined. Hand computation (eta 1.0, no M1, no headroom):
charge at $30/MWh in interval 0, deliver 40 kW x 0.25 h = 10 kWh in interval 1 at a P50 of $80/MWh with
$20/MWh wear -> 10 kWh x (80 - 30 - 20) $/MWh = $0.30.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from opengrid.contracts.intake.energy import compute_energy_candidates
from opengrid.selector.extract import extract_plan
from opengrid.selector.model import build_mode_o_model
from opengrid.selector.solve import highs_solve
from opengrid.selector.types import BankSnapshot, ModelInputs, ScenarioPrice, SolverSettings
from unit.selector.factories import binary_candidate

NOW = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)
EXACT = SolverSettings(mip_rel_gap=0.0, time_limit_s=10.0)


def test_one_energy_candidate_pays_its_spread_once() -> None:
    [candidate] = compute_energy_candidates(
        now=NOW,
        charge_price_usd_per_mwh=30.0,
        discharge_p50_by_interval={NOW + timedelta(minutes=15): 80.0},
        degradation_usd_per_kwh=Decimal("0.02"),
        eta_rt=1.0,
        horizon_intervals=1,
    )
    assert candidate.spread_usd_per_mwh == Decimal("30.00")
    bank = BankSnapshot(
        bank_id="B1",
        max_discharge_kw={0: 40.0, 1: 40.0},
        max_charge_kw={0: 40.0, 1: 40.0},
        initial_soc_kwh=0.0,  # empty: interval 1's delivery must be charged in interval 0
        capacity_kwh=10.0,
        eta_c=1.0,
        eta_d=1.0,
        self_discharge_kwh_per_h=0.0,
        wear_usd_per_kwh=0.02,
        free_market_access=False,  # no FREE headroom: the candidate is the only way to sell
    )
    inputs = ModelInputs(
        intervals=(0, 1),
        interval_minutes=15.0,
        banks=(bank,),
        scenarios=(ScenarioPrice(scenario="P50", probability=1.0, price_usd_per_mwh={0: 30.0, 1: 30.0}),),
        committed=(),
        candidates=(binary_candidate("e1", 40.0, float(candidate.value_per_mwh), (1,), ("B1",)),),
    )

    built = build_mode_o_model(inputs)
    outcome = highs_solve(built, EXACT)
    plan = extract_plan(built, outcome, "L-ID")

    assert plan.selected_x["e1"] is True
    assert abs(outcome.objective_value - 0.30) < 1e-6
