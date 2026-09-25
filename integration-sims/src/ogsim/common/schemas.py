"""ogsim.common.schemas -- loads and caches interfaces/mqtt/*.schema.json
and validates outbound fleet/scada messages against them before publish.

The schema files are the only thing shared with the orchestrator (BUILD.md
§1); this module never imports opengrid, it only reads the JSON Schema
files from disk.
"""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path
from typing import Any

import jsonschema

_SCHEMA_NAMES = {
    "telemetry": "telemetry.schema.json",
    "ack": "ack.schema.json",
    "command_batch": "command_batch.schema.json",
    "lease": "lease.schema.json",
    "stop": "stop.schema.json",
    "scada_bank_signal": "scada_bank_signal.schema.json",
    "scada_utility_instruction": "scada_utility_instruction.schema.json",
    "scenario_control": "scenario_control.schema.json",
}


def _interfaces_mqtt_dir() -> Path:
    # ogsim is installed from integration-sims/src/ogsim; interfaces/ sits
    # three levels up from this file's package root at the repo root.
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "interfaces" / "mqtt"
        if candidate.is_dir():
            return candidate
    raise FileNotFoundError("could not locate interfaces/mqtt/ from ogsim.common.schemas")


@cache
def _load_schema(name: str) -> dict[str, Any]:
    filename = _SCHEMA_NAMES[name]
    path = _interfaces_mqtt_dir() / filename
    with path.open(encoding="utf-8") as fh:
        loaded: dict[str, Any] = json.load(fh)
        return loaded


@cache
def _validator(name: str) -> jsonschema.protocols.Validator:
    schema = _load_schema(name)
    validator_cls = jsonschema.validators.validator_for(schema)
    validator_cls.check_schema(schema)
    return validator_cls(schema)


class SchemaValidationError(ValueError):
    """Raised when an outbound message fails validation against its schema."""


def validate(schema_name: str, message: dict[str, Any]) -> None:
    """Validates `message` against the named interfaces/mqtt schema.

    Raises `SchemaValidationError` with the first violation on failure.
    """
    validator = _validator(schema_name)
    errors = sorted(validator.iter_errors(message), key=lambda e: e.path)
    if errors:
        first = errors[0]
        raise SchemaValidationError(f"{schema_name}: {first.message} at {list(first.path)}")
