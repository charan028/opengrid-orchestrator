"""Micro-benchmark: ticks the vectorized fleet state at 2,000 hubs and
reports wall/CPU time per tick (02b §5 CPU measurement ask). Not a
correctness test; asserts only that a tick completes well within the 2s
telemetry cadence, so it also acts as a performance regression guard."""

from __future__ import annotations

from ogsim.fleet.__main__ import run_benchmark

MAX_WALL_S_PER_TICK_AT_2000_HUBS = 0.5  # generous local-machine ceiling; see reported numbers for actuals


def test_2000_hub_tick_is_well_within_the_2s_cadence() -> None:
    result = run_benchmark(hub_count=2000, duration_s=4.0, tick_interval_s=2.0)
    assert result["hub_count"] == 2000
    assert result["wall_s_per_tick"] < MAX_WALL_S_PER_TICK_AT_2000_HUBS
