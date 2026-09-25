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


def build_validator(schema: dict[str, Any]) -> jsonschema.protocols.Validator:
    """Builds a `jsonschema` validator for `schema`, checking the schema
    itself is well-formed first. The one place that knows how to turn a raw
    JSON Schema dict into a validator -- shared with
    `ogsim.control.schema_validation`, which validates `scenario_control`
    messages against a schema it locates itself (with a graceful no-op
    fallback the control plane needs that this cached-by-name loader does
    not provide), so it reuses this builder rather than re-implementing it.
    """
    validator_cls = jsonschema.validators.validator_for(schema)
    validator_cls.check_schema(schema)
    return validator_cls(schema)


def first_error_message(validator: jsonschema.protocols.Validator, message: dict[str, Any]) -> str | None:
    """Returns the first schema violation's message (with its JSON path),
    or `None` if `message` is valid. Errors are sorted by path so repeated
    calls with the same invalid message report the same violation first."""
    errors = sorted(validator.iter_errors(message), key=lambda e: e.path)
    if not errors:
        return None
    first = errors[0]
    return f"{first.message} at {list(first.path)}"


@cache
def _validator(name: str) -> jsonschema.protocols.Validator:
    return build_validator(_load_schema(name))


class SchemaValidationError(ValueError):
    """Raised when an outbound message fails validation against its schema."""


def validate(schema_name: str, message: dict[str, Any]) -> None:
    """Validates `message` against the named interfaces/mqtt schema.

    Raises `SchemaValidationError` with the first violation on failure.
    """
    error = first_error_message(_validator(schema_name), message)
    if error is not None:
        raise SchemaValidationError(f"{schema_name}: {error}")
