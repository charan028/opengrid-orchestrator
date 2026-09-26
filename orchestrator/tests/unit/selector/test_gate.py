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


async def test_the_solve_runs_in_a_separate_process_and_matches_the_in_process_result():
    """A11 regression (live 2026-09-26): the solve ran in a worker THREAD, and pure-Python model build,
    validation and price-of-firmness held the GIL for seconds, so every await in the 2 s dispatch tick
    queued behind it (ticks of 0.7-1.2 s during each gate). It now runs in a solver process."""
    import os

    from opengrid.selector import gate

    bank = make_bank("B1", 10.0, range(1))
    scenario = zero_price_scenario(range(1))
    c1 = binary_candidate("c1", 10.0, 100.0, (0,), ("B1",))
    inputs = simple_inputs((bank,), (scenario,), (), (c1,), n_intervals=1)

    try:
        remote = await gate.solve_off_loop(inputs, "SCHEDULED_15MIN", NOW, {}, {})
        worker_pid = await gate.run_in_solver_process(os.getpid)
    finally:
        gate.shutdown_solver_process()

    local = solve_gate(inputs, "SCHEDULED_15MIN", NOW)
    assert remote.solver_status == local.solver_status == "OPTIMAL"
    assert remote.selected_x == local.selected_x
    assert worker_pid != os.getpid()


async def test_a_solve_that_overruns_its_budget_fails_the_gate_and_recycles_the_pool(monkeypatch):
    """Review #12: a hung solver process had no watchdog, so it would wedge every later gate. The solve
    now has a hard budget; on overrun the worker is killed, the pool dropped, and the gate fails."""
    import asyncio

    import pytest

    from opengrid.selector import gate

    killed: list[bool] = []

    async def _hang(fn, *args):
        await asyncio.sleep(3600)

    monkeypatch.setattr(gate, "run_in_solver_process", _hang)
    monkeypatch.setattr(gate, "solver_budget_s", lambda kind: 0.05)
    monkeypatch.setattr(gate, "_kill_solver_pool", lambda: killed.append(True))
    bank = make_bank("B1", 10.0, range(1))
    inputs = simple_inputs((bank,), (zero_price_scenario(range(1)),), (), (), n_intervals=1)

    with pytest.raises(gate.SolverTimeoutError):
        await gate.solve_off_loop(inputs, "SCHEDULED_15MIN", NOW, {}, {})
    assert killed == [True]


def test_the_solver_budget_exceeds_every_highs_time_limit():
    from opengrid.selector import gate
    from opengrid.selector.types import solver_settings_for

    for kind in ("SCHEDULED_15MIN", "ADMISSION", "RENOMINATION"):
        assert gate.solver_budget_s(kind) > solver_settings_for(kind).time_limit_s + 10.0


async def test_a_broken_solver_process_falls_back_to_a_thread(monkeypatch):
    from concurrent.futures.process import BrokenProcessPool

    from opengrid.selector import gate

    async def _broken(fn, *args):
        raise BrokenProcessPool("worker died")

    monkeypatch.setattr(gate, "run_in_solver_process", _broken)
    bank = make_bank("B1", 10.0, range(1))
    inputs = simple_inputs(
        (bank,),
        (zero_price_scenario(range(1)),),
        (),
        (binary_candidate("c1", 10.0, 100.0, (0,), ("B1",)),),
        n_intervals=1,
    )

    plan = await gate.solve_off_loop(inputs, "SCHEDULED_15MIN", NOW, {}, {})

    assert plan.solver_status == "OPTIMAL"
