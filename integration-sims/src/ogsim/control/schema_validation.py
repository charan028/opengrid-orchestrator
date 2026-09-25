"""Validates the `<root>/scenario/cmd` MQTT message against the architect's
published `interfaces/mqtt/scenario_control.schema.json` (the "ScenarioControl"
schema): `{id, target: {kind, ref}, type, params, start, duration_s}`, `type`
one of the SCADA_*/FLEET_*/MARKET_*/PARTNER_CALL enum values, `start` an
ISO-8601 date-time string. Falls back to a no-op if the file is somehow
missing, so a stale checkout degrades safely rather than crashing."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

import jsonschema

REPO_ROOT = Path(__file__).resolve().parents[4]
SCENARIO_CONTROL_SCHEMA_PATH = REPO_ROOT / "interfaces" / "mqtt" / "scenario_control.schema.json"


class ScenarioCmdValidationError(ValueError):
    """Raised when a scenario/cmd message fails the published JSON schema."""


@lru_cache(maxsize=1)
def _load_schema() -> dict[str, Any] | None:
    if not SCENARIO_CONTROL_SCHEMA_PATH.exists():
        return None
    with SCENARIO_CONTROL_SCHEMA_PATH.open("r", encoding="utf-8") as f:
        return json.load(f)


def validate_scenario_cmd(message: dict[str, Any]) -> None:
    """No-op if interfaces/mqtt/scenario_control.schema.json is missing;
    otherwise raises ScenarioCmdValidationError on a shape mismatch."""
    schema = _load_schema()
    if schema is None:
        return
    try:
        jsonschema.validate(instance=message, schema=schema)
    except jsonschema.ValidationError as exc:
        raise ScenarioCmdValidationError(str(exc)) from exc
