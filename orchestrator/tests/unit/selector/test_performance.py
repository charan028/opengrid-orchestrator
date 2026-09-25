"""Performance (BUILD.md): 40 banks x 96 intervals x 3 scenarios must solve in < 30s. Reports the
measured wall-clock and HiGHS-reported solve time via the assertion message (run with `pytest -s -q
tests/unit/selector/test_performance.py` to see the printed line too)."""

from __future__ import annotations

import time

from opengrid.selector.extract import extract_plan
from opengrid.selector.model import build_mode_o_model
from opengrid.selector.solve import highs_solve
from opengrid.selector.types import BankSnapshot, ScenarioPrice, solver_settings_for
from unit.selector.factories import (
    binary_candidate,
    committed,
    continuous_candidate,
    semi_continuous_candidate,
    simple_inputs,
)

N_BANKS = 40
N_INTERVALS = 96
N_CANDIDATES = 120


def _perf_inputs():
    intervals = range(N_INTERVALS)
    banks = tuple(
        BankSnapshot(bank_id=f"B{i}", max_discharge_kw=dict.fromkeys(intervals, 250.0))
        for i in range(N_BANKS)
    )
    scenarios = tuple(
        ScenarioPrice(
            scenario=name,
            probability=weight,
            price_usd_per_mwh=dict.fromkeys(intervals, 20.0 + 5.0 * offset),
        )
        for name, weight, offset in (("P10", 0.25, -1), ("P50", 0.5, 0), ("P90", 0.25, 1))
    )
    committed_obligations = tuple(
        committed(f"o-commit-{i}", dict.fromkeys(range(4 * i, 4 * i + 4), 20.0), (f"B{i % N_BANKS}",))
        for i in range(N_BANKS)
    )
    candidates = []
    for i in range(N_CANDIDATES):
        bank_id = f"B{i % N_BANKS}"
        window = tuple(range(min(4, N_INTERVALS)))
        if i % 3 == 0:
            candidates.append(binary_candidate(f"c{i}", 10.0, 30.0 + i, window, (bank_id,)))
        elif i % 3 == 1:
            candidates.append(continuous_candidate(f"c{i}", 15.0, 25.0 + i, window, (bank_id,)))
        else:
            candidates.append(
                semi_continuous_candidate(f"c{i}", 12.0, 2.0, 1.0, 28.0 + i, window, (bank_id,))
            )

    return simple_inputs(banks, scenarios, committed_obligations, tuple(candidates), n_intervals=N_INTERVALS)


def test_kpi_40_banks_96_intervals_3_scenarios_solves_under_30s():
    inputs = _perf_inputs()
    settings = solver_settings_for("SCHEDULED_15MIN")

    wall_start = time.monotonic()
    built = build_mode_o_model(inputs)
    outcome = highs_solve(built, settings)
    wall_elapsed_s = time.monotonic() - wall_start

    n_vars = built.highs.numVariables
    n_rows = built.highs.numConstrs
    message = (
        f"{N_BANKS} banks x {N_INTERVALS} intervals x 3 scenarios, {N_CANDIDATES} candidates: "
        f"{n_vars} vars / {n_rows} rows, wall={wall_elapsed_s:.2f}s, solver={outcome.time_ms}ms, "
        f"status={outcome.status}, gap={outcome.gap}"
    )
    print(message)
    assert wall_elapsed_s < 30.0, message
    assert outcome.status in ("OPTIMAL", "TIME_LIMIT_GAP"), message

    plan = extract_plan(built, outcome, "L-DA")
    assert plan.solver_time_ms >= 0
