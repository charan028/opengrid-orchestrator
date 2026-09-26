"""Tests for the scenario YAML loader and runner, including all 6 shipped
scenarios. `run_scenario` is exercised with `speed` large enough that every
step's computed delay is <= 0, so no real sleeping happens (non-flaky)."""

from __future__ import annotations

from pathlib import Path

import pytest

from ogsim.control.injector import Injector
from ogsim.control.scenarios import ScenarioError, load_scenario, load_scenarios_dir, run_scenario

SCENARIOS_DIR = Path(__file__).resolve().parents[1] / "scenarios"
FAST_SPEED = 1_000_000.0  # collapses every step's wait to ~0s so tests run instantly


CORE_SCENARIOS = {
    "bank_overload_and_utility_limit",
    "compound_stress",
    "feed_outage_and_stale",
    "price_spike_during_delivery",
    "tampered_command",
    "zone_comms_loss",
}


def test_all_shipped_scenarios_load_without_error():
    """The six core scenarios plus the DEMO pack (`demo-*.yaml`, docs/demo) all load, with unique names."""
    scenarios = load_scenarios_dir(SCENARIOS_DIR)
    names = {s.name for s in scenarios}
    assert len(names) == len(scenarios)
    assert names >= CORE_SCENARIOS
    demo_files = sorted(SCENARIOS_DIR.glob("demo-*.yaml"))
    assert len(scenarios) == len(CORE_SCENARIOS) + len(demo_files)


def test_missing_scenarios_dir_returns_empty_list(tmp_path: Path):
    assert load_scenarios_dir(tmp_path / "does-not-exist") == []


def test_load_scenario_rejects_a_step_missing_required_fields(tmp_path: Path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("name: bad\nsteps:\n  - type: price_spike\n", encoding="utf-8")
    with pytest.raises(ScenarioError):
        load_scenario(bad)


def test_load_scenario_sorts_steps_by_at_s(tmp_path: Path):
    path = tmp_path / "ordering.yaml"
    path.write_text(
        "name: ordering\nsteps:\n"
        "  - {at_s: 10, type: price_spike, target: '*'}\n"
        "  - {at_s: 0, type: negative_price, target: '*'}\n",
        encoding="utf-8",
    )
    scenario = load_scenario(path)
    assert [s.at_s for s in scenario.steps] == [0, 10]


async def test_run_scenario_injects_every_step_with_source_scenario():
    scenario = load_scenario(SCENARIOS_DIR / "zone_comms_loss.yaml")
    injector = Injector()
    ids = await run_scenario(injector, scenario, speed=FAST_SPEED)
    assert len(ids) == len(scenario.steps)
    active = injector.active(source="scenario")
    assert len(active) == len(scenario.steps)


async def test_run_scenario_preserves_step_order_in_injected_ids():
    scenario = load_scenario(SCENARIOS_DIR / "compound_stress.yaml")
    injector = Injector()
    ids = await run_scenario(injector, scenario, speed=FAST_SPEED)
    expected = [f"{scenario.name}:{s.type}:{s.at_s:g}" for s in scenario.steps]
    assert ids == expected
