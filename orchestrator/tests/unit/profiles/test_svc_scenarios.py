"""Structural checks on the SERVICES-owned scenario files (`integration-sims/scenarios/svc-*.yaml`,
owner decision 2026-09-26: PJM_CAPACITY/MOBILE_STORAGE/LARGE_LOAD). Parsed with plain PyYAML, matching
`ogsim.control.scenarios.load_scenario`'s own required-field rule (`at_s`, `type`, `target`), but without
importing `ogsim` itself -- `integration-sims` is FLEET-SIM's package (BUILD.md ownership map), so this
module only checks the YAML shape the SERVICES agent is responsible for, never `ogsim`'s loader/injector
behaviour.

NOTE for FLEET-SIM (also in the SERVICES agent's 2026-09-26 report): adding these three files to
`integration-sims/scenarios/` raises that directory's file count from 6 to 9, which will break
`integration-sims/tests/test_scenarios.py::test_all_six_shipped_scenarios_load_without_error`'s hardcoded
`len(scenarios) == 6` assertion -- that test file is FLEET-SIM's to update, not touched here."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

_SCENARIOS_DIR = Path(__file__).resolve().parents[4] / "integration-sims" / "scenarios"
_REQUIRED_STEP_FIELDS = {"at_s", "type", "target"}
_SVC_FILES = {
    "svc-pjm-capacity.yaml": "svc_pjm_capacity",
    "svc-mobile-storage.yaml": "svc_mobile_storage",
    "svc-large-load.yaml": "svc_large_load",
}


def _load(filename: str) -> dict[str, Any]:
    path = _SCENARIOS_DIR / filename
    with path.open("r", encoding="utf-8") as handle:
        data: dict[str, Any] = yaml.safe_load(handle)
    return data


def test_all_three_service_scenarios_exist_and_parse() -> None:
    for filename in _SVC_FILES:
        assert (_SCENARIOS_DIR / filename).exists(), filename


def test_each_scenario_has_the_expected_name_and_at_least_one_step() -> None:
    for filename, expected_name in _SVC_FILES.items():
        data = _load(filename)
        assert data["name"] == expected_name
        assert isinstance(data["steps"], list) and len(data["steps"]) >= 1


def test_every_step_carries_the_scenario_runner_required_fields() -> None:
    for filename in _SVC_FILES:
        data = _load(filename)
        for i, step in enumerate(data["steps"]):
            missing = _REQUIRED_STEP_FIELDS - step.keys()
            assert not missing, f"{filename} step {i} missing {missing}"


def test_pjm_scenario_declares_and_recalls_an_emergency_performance_event() -> None:
    steps = _load("svc-pjm-capacity.yaml")["steps"]
    types = [s["type"] for s in steps]
    assert types.count("pjm_emergency_performance_event") == 2
    recall_steps = [s for s in steps if s["params"].get("recall")]
    assert len(recall_steps) == 1


def test_mobile_storage_scenario_relocates_the_same_trailer_between_two_sites() -> None:
    steps = _load("svc-mobile-storage.yaml")["steps"]
    starts = [s for s in steps if s["type"] == "mobile_deployment_start"]
    relocations = [s for s in steps if s["type"] == "mobile_deployment_relocate"]
    assert len(starts) == 2
    assert len(relocations) == 1
    site_ids = {s["params"]["site_id"] for s in starts}
    assert site_ids == {"site-warehouse-01", "site-drilling-pad-07"}
    # Same trailer (target) throughout -- only the site changes.
    assert {s["target"] for s in steps} == {"trailer-mb-01"}


def test_large_load_scenario_combines_a_load_step_with_a_bank_overload() -> None:
    steps = _load("svc-large-load.yaml")["steps"]
    types = {s["type"] for s in steps}
    assert types == {"load_step_datacenter", "bank_overload"}
