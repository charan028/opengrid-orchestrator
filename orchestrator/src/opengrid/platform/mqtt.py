"""aiomqtt client factory with topic-root prefixing and JSON-Schema validation against `interfaces/`.

02b S1.1, S6.1-S6.2. Every process that touches MQTT (`og_engine`, `og_guardian`, `og_safestop`,
`og_sim`, `og_api`) builds its client through `build_client()` so the topic root
(`[mqtt].topic_root` / `OG_MQTT_ROOT`) is applied consistently and every inbound/outbound payload is
validated against the matching `interfaces/mqtt/*.schema.json` before use.
"""

from __future__ import annotations

import json
import os
from functools import cache
from pathlib import Path
from typing import Any

import aiomqtt
import jsonschema

from opengrid.platform.config import Config, ConfigError

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
    # WP-G (06-service-profiles-and-power-quality.md S6.4-S6.5): waveform ingest.
    "pq_waveform_summary": "pq_waveform_summary.schema.json",
    "pq_waveform_raw": "pq_waveform_raw.schema.json",
    "waveform_capture_request": "waveform_capture_request.schema.json",
    # S6.7 remote calibration: guardian-signed command out, hub ack in.
    "calibration_command": "calibration_command.schema.json",
    "calibration_ack": "calibration_ack.schema.json",
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


#: The production `[mqtt].client_id_prefix`. Production client ids are `og-<process>` (e.g. `og-guardian`)
#: and must stay stable; nothing else may use this prefix.
PRODUCTION_CLIENT_ID_PREFIX = "og"
_ENV_WORKSPACE = "OG_WS"
_LEGACY_ID_PREFIX = f"{PRODUCTION_CLIENT_ID_PREFIX}-"


class MqttIdentityError(ConfigError):
    """A non-production run (a workspace, or `[general].env` other than "prod") configured with the
    production client-id prefix. The broker drops the older session on a duplicate client id, so such a
    run would hijack a production process's MQTT session."""


def compose_client_id(cfg: Config, process: str) -> str:
    """`<[mqtt].client_id_prefix>[-<OG_WS>]-<process>`. Production (no `OG_WS`, env "prod") keeps its
    existing ids, e.g. `og-guardian`. Refuses (MqttIdentityError) when `OG_WS` is set or env is not
    "prod" while the prefix is still the production "og"."""
    prefix = str(cfg.get("mqtt.client_id_prefix", PRODUCTION_CLIENT_ID_PREFIX)).strip()
    workspace = os.environ.get(_ENV_WORKSPACE, "").strip()
    env = str(cfg.get("general.env", "prod")).strip()
    if not prefix or not process:
        raise MqttIdentityError("MQTT client id needs a non-empty [mqtt].client_id_prefix and process name")
    if (workspace or env != "prod") and prefix == PRODUCTION_CLIENT_ID_PREFIX:
        raise MqttIdentityError(
            f"refusing the production MQTT client-id prefix {prefix!r} outside production "
            f"(OG_WS={workspace!r}, general.env={env!r}): set [mqtt].client_id_prefix, e.g. 'og-test'"
        )
    return "-".join(part for part in (prefix, workspace, process) if part)


#: Per-workspace broker credentials (deploy/mosquitto/provision_ws_users.py writes them to
#: /opt/opengrid/work/<ws>/.mqtt.env; tools/remote.ps1 exports them). User `ogw_<ws>` may reach only
#: `ogtest/<ws>/#`.
ENV_WS_USER = "OG_MQTT_WS_USER"
ENV_WS_PASSWORD = "OG_MQTT_WS_PASSWORD"  # noqa: S105 -- an env-var name, not a secret


def resolve_mqtt_credentials(username: str, password: str) -> tuple[str, str]:
    """Which broker credentials this process uses. Precedence:

    1. `OG_MQTT_WS_USER`/`OG_MQTT_WS_PASSWORD` (a workspace run) always win over the caller's role user;
    2. otherwise the caller's production role user (`og_guardian`, ...) and its password.

    A workspace (`OG_WS` set) without workspace credentials is refused -- it must never fall back to a
    production role user -- and a workspace user without a password is refused too."""
    ws_user = os.environ.get(ENV_WS_USER, "").strip()
    ws_password = os.environ.get(ENV_WS_PASSWORD, "")
    if ws_user:
        if not ws_password:
            raise MqttIdentityError(f"{ENV_WS_USER} is set but {ENV_WS_PASSWORD} is empty")
        return ws_user, ws_password
    if os.environ.get(_ENV_WORKSPACE, "").strip():
        raise MqttIdentityError(
            f"OG_WS is set but {ENV_WS_USER} is not: a workspace never uses production MQTT users "
            "(provision it with deploy/mosquitto/provision_ws_users.py; tools/remote.ps1 loads .mqtt.env)"
        )
    return username, password


def build_client(
    cfg: Config,
    *,
    username: str,
    password: str,
    process: str | None = None,
    client_id: str | None = None,
) -> aiomqtt.Client:
    """Construct (but do not yet connect) an aiomqtt client using the process's own credentials.
    Callers use `async with build_client(...) as client:` per aiomqtt's context-manager protocol.

    The broker client id is always composed by `compose_client_id` from `process` (e.g. "guardian").
    `client_id` is the deprecated spelling: a legacy full id such as "og-engine" is read as the process
    name "engine", so production ids are unchanged while callers migrate to `process=`."""
    if process is None:
        if client_id is None:
            raise MqttIdentityError("build_client needs process= (the process name, e.g. 'guardian')")
        process = client_id.removeprefix(_LEGACY_ID_PREFIX)
    host = cfg.get("mqtt.host", "127.0.0.1")
    port = cfg.get("mqtt.port", 1883)
    keepalive_s = cfg.get("mqtt.keepalive_s", 20)
    broker_user, broker_password = resolve_mqtt_credentials(username, password)
    return aiomqtt.Client(
        hostname=host,
        port=port,
        username=broker_user,
        password=broker_password,
        identifier=compose_client_id(cfg, process),
        keepalive=keepalive_s,
    )
