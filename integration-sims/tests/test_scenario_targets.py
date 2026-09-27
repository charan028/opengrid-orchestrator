"""Regression guard: every target in every shipped scenario file resolves to something real.

The fleet/SCADA resolvers return an empty list for an unknown target and raise nothing, so a
scenario step aimed at a mistyped id (e.g. `BANK_12` instead of the fleet's `bank-012`) silently
does nothing when it runs. This loads EVERY `scenarios/*.yaml` and resolves each step through the
same path the running simulators use:

- market steps: the target must be a product id the market simulator serves, a registered simulated
  zone id (`ogsim.market.data.SIMULATED_PJM_ZONES`), or `*`/`eia`/`nws`;
- customer steps: the target must be an id named somewhere in the shipped sim configs
  (`integration-sims/config/*.yaml`) -- a customer_id, site_id, corridor_id, etc.;
- fleet steps targeting a mobile trailer: the target must be a `trailer_id` in fleet.yaml's
  `mobile_units:` registry (#33 target-check, D-31, 2026-09-26);
- other fleet steps: `catalogue.infer_wire_target_kind` (what `Injector` puts on the wire), then the
  default `FleetEngine`'s own resolver -- at least 1 hub;
- SCADA steps: the same wire kind, then the default `ScadaEngine`'s resolver -- at least 1 bank,
  and those banks must hold at least 1 hub in the default fleet.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from ogsim.common.config import load_fleet_config, load_scada_config
from ogsim.control import catalogue
from ogsim.control.scenarios import ScenarioStep, load_scenarios_dir
from ogsim.fleet.pq import PQ_ANOMALY_TYPES
from ogsim.fleet.runtime import FleetEngine
from ogsim.market.as_dispatch import SIMULATED_ERCOT_RESOURCES
from ogsim.market.data import PRODUCTS, SIMULATED_PJM_ZONES
from ogsim.scada.runtime import ScadaEngine

SCENARIOS_DIR = Path(__file__).resolve().parents[1] / "scenarios"
SIMS_CONFIG_DIR = Path(__file__).resolve().parents[1] / "config"

#: Found by this guard on R2 (6470cfa): shipped scenarios target ids no sim config names, so those steps
#: would do nothing when run. Recorded as expected failures until the sims owner adds them (or retargets
#: the scenarios); remove an entry once its id resolves.
#:
#: #33 target-check, 2026-09-26: both prior entries now resolve -- `site-large-load-crypto-01` was
#: retargeted to the real LARGE_LOAD customer_id (config/customer.yaml), and `pjm-zone-aep-01` is now a
#: registered simulated PJM zone (`ogsim.market.data.SIMULATED_PJM_ZONES`). Empty until the next gap.
KNOWN_UNCONFIGURED_TARGETS: dict[str, str] = {}


def _config_ids(config_dir: Path) -> set[str]:
    """Every string value in the shipped sim configs (site ids, zone ids, ...)."""
    found: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)
        elif isinstance(node, str):
            found.add(node)

    for path in sorted(config_dir.glob("*.yaml")):
        walk(yaml.safe_load(path.read_text(encoding="utf-8")))
    return found


# ogsim.market.anomalies: a market anomaly's target is a real product id, a registered simulated zone
# id (SIMULATED_PJM_ZONES), "eia", "nws", or "*".
KNOWN_MARKET_TARGETS = (
    set(PRODUCTS) | set(SIMULATED_PJM_ZONES) | set(SIMULATED_ERCOT_RESOURCES) | {"*", "eia", "nws"}
)


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
    if step.target in KNOWN_UNCONFIGURED_TARGETS:
        pytest.xfail(KNOWN_UNCONFIGURED_TARGETS[step.target])

    if entry.owner == "market":
        assert step.target in KNOWN_MARKET_TARGETS, (
            f"market target {step.target!r} is not a known product id ({sorted(KNOWN_MARKET_TARGETS)})"
        )
        return

    if entry.owner == "customer":
        # R2's customer operators (ogsim.customer): the target must be an id the shipped config names
        # (a customer site, or a market zone such as a PJM zone) -- a typo would silently do nothing.
        known = _config_ids(SIMS_CONFIG_DIR)
        assert step.target in known, (
            f"customer target {step.target!r} is not named in {SIMS_CONFIG_DIR}/*.yaml"
        )
        return
    if "trailer" in (entry.target_kind or ""):
        # #33 target-check, D-31, 2026-09-26: fleet.yaml's `mobile_units:` registry
        # (`ogsim.common.config.MobileUnitConfig`) now covers this -- no longer a skip.
        trailer_ids = {u.trailer_id for u in fleet.config.mobile_units}
        assert step.target in trailer_ids, (
            f"{step.type} targets a mobile trailer ({step.target!r}) not in fleet.yaml's mobile_units "
            "registry"
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
