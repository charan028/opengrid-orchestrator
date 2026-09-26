"""Regression guard: every target in every shipped scenario file resolves to something real.

The fleet/SCADA resolvers return an empty list for an unknown target and raise nothing, so a
scenario step aimed at a mistyped id (e.g. `BANK_12` instead of the fleet's `bank-012`) silently
does nothing when it runs. This loads EVERY `scenarios/*.yaml` and resolves each step through the
same path the running simulators use:

- market steps: the target must be a product id the market simulator serves (or `*`/`eia`/`nws`);
- fleet steps: `catalogue.infer_wire_target_kind` (what `Injector` puts on the wire), then the
  default `FleetEngine`'s own resolver -- at least 1 hub;
- SCADA steps: the same wire kind, then the default `ScadaEngine`'s resolver -- at least 1 bank,
  and those banks must hold at least 1 hub in the default fleet.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from ogsim.common.config import load_fleet_config, load_scada_config
from ogsim.control import catalogue
from ogsim.control.scenarios import ScenarioStep, load_scenarios_dir
from ogsim.fleet.pq import PQ_ANOMALY_TYPES
from ogsim.fleet.runtime import FleetEngine
from ogsim.market.data import PRODUCTS
from ogsim.scada.runtime import ScadaEngine

SCENARIOS_DIR = Path(__file__).resolve().parents[1] / "scenarios"

# ogsim.market.anomalies: a market anomaly's target is a product id, "eia", "nws", or "*".
KNOWN_MARKET_TARGETS = set(PRODUCTS) | {"*", "eia", "nws"}


def _scenario_steps() -> list[Any]:
    params = []
    for scenario in load_scenarios_dir(SCENARIOS_DIR):
        file_name = Path(scenario.path or scenario.name).name
        for i, step in enumerate(scenario.steps):
            params.append(pytest.param(step, id=f"{file_name}[{i}]:{step.type}:{step.target}"))
    return params


@pytest.fixture
def fleet() -> FleetEngine:
    return FleetEngine(load_fleet_config(), seed=0)


@pytest.fixture
def scada() -> ScadaEngine:
    return ScadaEngine(load_scada_config(), seed=0)


def test_every_scenario_file_is_covered() -> None:
    files = sorted(SCENARIOS_DIR.glob("*.yaml"))
    assert files, f"no scenario files found in {SCENARIOS_DIR}"
    assert len(load_scenarios_dir(SCENARIOS_DIR)) == len(files)


@pytest.mark.parametrize("step", _scenario_steps())
def test_scenario_step_target_resolves(step: ScenarioStep, fleet: FleetEngine, scada: ScadaEngine) -> None:
    entry = catalogue.BY_ID.get(step.type)
    assert entry is not None, f"unknown anomaly type {step.type!r}"

    if entry.owner == "market":
        assert step.target in KNOWN_MARKET_TARGETS, (
            f"market target {step.target!r} is not a known product id ({sorted(KNOWN_MARKET_TARGETS)})"
        )
        return

    wire_kind = catalogue.infer_wire_target_kind(entry, step.target)
    if entry.owner == "scada":
        banks = scada.anomalies._resolve_targets(wire_kind, step.target)
        assert banks, f"SCADA target {step.target!r} (kind={wire_kind}) matches no bank"
        bank_set = set(banks)
        hubs = [i for i, b in enumerate(fleet.state.bank_ids) if b in bank_set]
    elif step.type in PQ_ANOMALY_TYPES:
        hubs = fleet.pq_anomalies._resolve_units(wire_kind, step.target)
    else:
        hubs = fleet.anomalies._resolve_targets(wire_kind, step.target)
    assert hubs, (
        f"{entry.owner} target {step.target!r} (kind={wire_kind}) resolves to 0 hubs in the default "
        "fleet; ids are hub-NNNNN / bank-NNN / LZ_* (see ogsim.fleet.state)"
    )
