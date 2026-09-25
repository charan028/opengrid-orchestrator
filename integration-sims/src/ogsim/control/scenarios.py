"""Scenario YAML runner: timed sequences of anomaly injections.

Scenario file shape:

    name: "..."
    description: "..."
    steps:
      - at_s: 0
        type: price_spike
        target: np6-905-cd
        params: {value_usd_per_mwh: 5000}
        duration: 300
      - at_s: 30
        type: bank_overload
        target: BANK_07
        params: {kva_over_rating_pct: 25}
        duration: 120
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from ogsim.control.injector import Injector

REQUIRED_STEP_FIELDS = {"at_s", "type", "target"}


class ScenarioError(ValueError):
    pass


@dataclass
class ScenarioStep:
    at_s: float
    type: str
    target: str
    params: dict[str, Any]
    duration: float


@dataclass
class Scenario:
    name: str
    description: str
    steps: list[ScenarioStep]
    path: str | None = None


def load_scenario(path: str | Path) -> Scenario:
    path = Path(path)
    with path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    if not isinstance(raw, dict) or "steps" not in raw:
        raise ScenarioError(f"{path}: scenario must be a mapping with a 'steps' list")
    steps = []
    for i, s in enumerate(raw["steps"]):
        missing = REQUIRED_STEP_FIELDS - s.keys()
        if missing:
            raise ScenarioError(f"{path}: step {i} missing fields {sorted(missing)}")
        steps.append(
            ScenarioStep(
                at_s=float(s["at_s"]),
                type=s["type"],
                target=s["target"],
                params=dict(s.get("params", {})),
                duration=float(s.get("duration", 60.0)),
            )
        )
    steps.sort(key=lambda s: s.at_s)
    return Scenario(
        name=raw.get("name", path.stem), description=raw.get("description", ""), steps=steps, path=str(path)
    )


def load_scenarios_dir(directory: str | Path) -> list[Scenario]:
    directory = Path(directory)
    if not directory.exists():
        return []
    return [load_scenario(p) for p in sorted(directory.glob("*.yaml"))]


async def run_scenario(injector: Injector, scenario: Scenario, speed: float = 1.0) -> list[str]:
    """Runs the scenario in real time (or faster, at `speed`), injecting each
    step at its scheduled offset. Returns the injected anomaly ids in order."""
    t0 = time.monotonic()
    injected_ids: list[str] = []
    for step in scenario.steps:
        target_time = t0 + step.at_s / max(speed, 1e-9)
        delay = target_time - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)
        record = await injector.inject(
            type_=step.type,
            target=step.target,
            params=step.params,
            duration=step.duration,
            anomaly_id=f"{scenario.name}:{step.type}:{step.at_s:g}",
            source="scenario",
        )
        injected_ids.append(record.id)
    return injected_ids
