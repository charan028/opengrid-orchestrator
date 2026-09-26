"""aiomqtt client factory with topic-root prefixing and JSON-Schema validation against `interfaces/`.

02b S1.1, S6.1-S6.2. Every process that touches MQTT (`og_engine`, `og_guardian`, `og_safestop`,
`og_sim`, `og_api`) builds its client through `build_client()` so the topic root
(`[mqtt].topic_root` / `OG_MQTT_ROOT`) is applied consistently and every inbound/outbound payload is
validated against the matching `interfaces/mqtt/*.schema.json` before use.
"""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path
from typing import Any

import aiomqtt
import jsonschema

from opengrid.platform.config import Config

INTERFACES_MQTT_DIR = Path(__file__).resolve().parents[4] / "interfaces" / "mqtt"

_SCHEMA_BY_KIND = {
    "telemetry": "telemetry.schema.json",
    "ack": "ack.schema.json",
    "command_batch": "command_batch.schema.json",
    "lease": "lease.schema.json",
    "stop": "stop.schema.json",
    "scada_bank_signal": "scada_bank_signal.schema.json",
    "scada_utility_instruction": "scada_utility_instruction.schema.json",
    "scenario_control": "scenario_control.schema.json",
}


@cache
def _load_schema(kind: str) -> dict[str, Any]:
    filename = _SCHEMA_BY_KIND[kind]
    with (INTERFACES_MQTT_DIR / filename).open(encoding="utf-8") as fh:
        data: dict[str, Any] = json.load(fh)
    return data


@cache
def _validator_for(kind: str) -> jsonschema.protocols.Validator:
    """One compiled `Validator` per schema `kind`, built once and reused.

    `jsonschema.validate()` (the convenience function this replaced) re-verifies the *schema itself*
    (`check_schema`) on every single call in addition to validating the instance -- fine for a one-off
    call, but ruinous at MQTT ingest volume (~1,000 msg/s at 2,000 hubs): live profiling
    (`py-spy dump` against `og-engine`) caught `_mqtt_ingest_loop` stuck synchronously inside
    `jsonschema.validators.validate -> check_schema -> iter_errors` on the event loop's only thread,
    which starved every other coroutine (including `opengrid.fleet.flush`'s DB writes) and made the
    ingest path fall further and further behind wall-clock time until every hub read as stale --
    the actual mechanism behind qa/merge-notes.md section 12's "og-sim-fleet goes idle" symptom (the
    simulator itself was confirmed still publishing on schedule; this consumer-side validation cost is
    what made ingestion never catch up). Compiling the validator once and calling
    `Validator.validate()` skips the redundant re-check on every message with no change in what is
    accepted or rejected.
    """
    schema = _load_schema(kind)
    cls = jsonschema.validators.validator_for(schema)
    cls.check_schema(schema)
    return cls(schema)


class SchemaValidationError(ValueError):
    """Raised when an MQTT payload fails validation against its `interfaces/mqtt/*.schema.json`."""


def validate_payload(kind: str, payload: dict[str, Any]) -> None:
    """Validate `payload` against the JSON Schema for `kind` (one of `_SCHEMA_BY_KIND`'s keys).
    Raises SchemaValidationError with the jsonschema-reported reason; never silently accepts an
    invalid message (BUILD.md S5a security: "validate all inbound messages against interfaces/").
    """
    validator = _validator_for(kind)
    try:
        validator.validate(instance=payload)
    except jsonschema.ValidationError as exc:
        raise SchemaValidationError(f"{kind}: {exc.message}") from exc


def topic(cfg: Config, suffix: str) -> str:
    """Join the configured topic root with a relative suffix, e.g. topic(cfg, "tel/+/+/+")."""
    root = cfg.mqtt_topic_root.rstrip("/")
    return f"{root}/{suffix.lstrip('/')}"


def build_client(cfg: Config, *, username: str, password: str, client_id: str) -> aiomqtt.Client:
    """Construct (but do not yet connect) an aiomqtt client using the process's own credentials.
    Callers use `async with build_client(...) as client:` per aiomqtt's context-manager protocol.
    """
    host = cfg.get("mqtt.host", "127.0.0.1")
    port = cfg.get("mqtt.port", 1883)
    keepalive_s = cfg.get("mqtt.keepalive_s", 20)
    return aiomqtt.Client(
        hostname=host,
        port=port,
        username=username,
        password=password,
        identifier=client_id,
        keepalive=keepalive_s,
    )
