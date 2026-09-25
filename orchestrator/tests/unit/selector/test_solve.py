"""Direct `solve.py` coverage: warm start and the infeasible/no-incumbent path (BUILD.md degraded-path
requirement: infeasible or timeout must be detected cleanly, not crash)."""

from __future__ import annotations

from opengrid.selector.model import build_mode_o_model
from opengrid.selector.solve import highs_solve
from opengrid.selector.types import SolverSettings
from unit.selector.factories import binary_candidate, make_bank, simple_inputs, zero_price_scenario

FAST_SETTINGS = SolverSettings(mip_rel_gap=0.01, time_limit_s=5.0)


def test_warm_start_hint_does_not_change_the_optimum():
    bank = make_bank("B1", 10.0, range(1))
    scenario = zero_price_scenario(range(1))
    c1 = binary_candidate("c1", 10.0, 100.0, (0,), ("B1",))
    inputs = simple_inputs((bank,), (scenario,), (), (c1,), n_intervals=1)

    built = build_mode_o_model(inputs)
    outcome = highs_solve(built, FAST_SETTINGS, x_hint={"c1": 1.0}, q_hint={"unrelated": 5.0})

    assert outcome.status == "OPTIMAL"
    assert outcome.primal.x["c1"] == 1.0


def test_structurally_infeasible_instance_reports_infeasible_f1():
    """A committed obligation needs 5kW on the ONLY eligible bank, but that bank's own interval
    capacity is a nonzero-but-tiny value the model's `_clamped` guard rounds to 0kW -- so C24's
    equality (sum of ybar == 5) can never be met: `highs_solve` must report `INFEASIBLE_F1` cleanly,
    never raise, so `gate.solve_gate` can fall back to F2."""
    from unit.selector.factories import committed

    zero_bank = make_bank("B1", 0.0, range(1))
    scenario = zero_price_scenario(range(1))
    locked = committed("o-commit", {0: 5.0}, ("B1",))
    inputs = simple_inputs((zero_bank,), (scenario,), (locked,), (), n_intervals=1)

    built = build_mode_o_model(inputs)
    outcome = highs_solve(built, FAST_SETTINGS)

    assert outcome.status == "INFEASIBLE_F1"
    assert outcome.is_feasible is False
    assert outcome.primal.x == {}
    assert outcome.bank_capacity_duals == {}
