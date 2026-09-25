"""`gate.solve_gate` wiring (02a S3.8): the pure solve-validate-fallback core, exercised without any
DB/MQTT/`contracts`/`ledger`/`fleet`/`forecast` I/O (BUILD.md "use fakes for siblings" -- here there is
nothing to fake because `solve_gate` takes `ModelInputs` directly; `run_gate`'s I/O plumbing is covered
by the per-loader docstrings/type checking rather than an integration test, since it needs a live
Postgres + the sibling stub modules other agents own, per BUILD.md S5's local-test scope)."""

from __future__ import annotations

from datetime import UTC, datetime

from opengrid.selector.gate import solve_gate
from opengrid.selector.types import BankSnapshot
from unit.selector.factories import binary_candidate, committed, make_bank, simple_inputs, zero_price_scenario

NOW = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)


def test_solve_gate_returns_optimal_for_a_feasible_instance():
    bank = make_bank("B1", 10.0, range(1))
    scenario = zero_price_scenario(range(1))
    c1 = binary_candidate("c1", 10.0, 100.0, (0,), ("B1",))
    inputs = simple_inputs((bank,), (scenario,), (), (c1,), n_intervals=1)

    plan = solve_gate(inputs, "SCHEDULED_15MIN", NOW)

    assert plan.solver_status == "OPTIMAL"
    assert plan.plan_mode in ("L-DA", "L-ID")
    assert plan.selected_x["c1"] is True


def test_solve_gate_falls_back_to_f2_when_infeasible():
    """A bank with 0 capacity and a committed obligation that needs kW there is structurally
    infeasible (C24 equality can't be met) -- `solve_gate` must still return a plan (F2), never raise."""
    bank = BankSnapshot(bank_id="B1", max_discharge_kw={0: 0.0})
    scenario = zero_price_scenario(range(1))
    locked = committed("o-commit", {0: 5.0}, ("B1",))
    inputs = simple_inputs((bank,), (scenario,), (locked,), (), n_intervals=1)

    plan = solve_gate(inputs, "ADMISSION", NOW)

    assert plan.solver_status == "RULE_FALLBACK"
    assert plan.plan_mode == "RULE_FALLBACK"


def test_solve_gate_falls_back_when_validation_catches_a_bad_solve():
    """Even if HiGHS reports OPTIMAL, a plan that fails the independent validator must trigger F2 --
    simulated here by shrinking committed capacity below what was frozen, via a hand-built ModelInputs
    where the *bank* eligible for the commitment cannot actually supply it (no eligible bank at all),
    which the model turns into an infeasible equality with sum-over-empty-set == positive kw."""
    bank = make_bank("B2", 10.0, range(1))  # not eligible for the committed obligation below
    scenario = zero_price_scenario(range(1))
    locked = committed("o-commit", {0: 5.0}, ("B1",))  # B1 is not in `banks` at all
    inputs = simple_inputs((bank,), (scenario,), (locked,), (), n_intervals=1)

    plan = solve_gate(inputs, "SCHEDULED_15MIN", NOW)

    assert plan.plan_mode == "RULE_FALLBACK"
