"""ogsim.common.scenario -- tolerant `<root>/scenario/cmd` parsing.

OPEN ISSUE for the merge agent (see BUILD.md point 4): `ogsim.control`'s
`mqtt_pub.py`/`injector.py` (owned by the "market" agent, not us) currently
publish a flat legacy shape on `<root>/scenario/cmd`:

    {id, target: "<string>", type: "<lowercase catalogue id>", params,
     start: <unix epoch float>, duration: <seconds float>}

not the `interfaces/mqtt/scenario_control.schema.json` shape this fleet/
scada code was told to treat as ground truth (`target: {kind, ref}`,
`type` an uppercase `FLEET_*`/`SCADA_*` enum, `start` an RFC3339 string,
`duration_s`). Rather than block on that mismatch, this parser tries the
schema-conformant shape first and falls back to the legacy flat shape, so
fleet/scada interoperate with whatever is actually on the wire today. This
is a compromise, not a fix: the two producers/consumers should converge on
one shape when the merge agent reconciles `ogsim.control` and
`ogsim.fleet`/`ogsim.scada`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

# scenario_control.schema.json `type` enum -> ogsim.control.catalogue id.
# Not a simple prefix-strip: several wire names abbreviate the catalogue id
# (e.g. SCADA_BAD_QUALITY -> bad_quality_flag), so the mapping is explicit.
WIRE_TYPE_TO_CATALOGUE_ID: dict[str, str] = {
    "FLEET_HUB_OFFLINE": "hub_offline",
    "FLEET_ZONE_MASS_DISCONNECT": "zone_mass_disconnect",
    "FLEET_NOT_FOLLOWING_COMMANDS": "not_following_commands",
    "FLEET_INVERTER_TRIP": "inverter_trip",
    "FLEET_SOC_SENSOR_DRIFT": "soc_sensor_drift",
    "FLEET_TELEMETRY_DELAY_BURST": "telemetry_delay_burst",
    "FLEET_LEASE_LOSS": "lease_loss",
    "FLEET_CLOCK_SKEW": "clock_skew",
    "FLEET_TAMPERED_UNSIGNED_COMMAND": "tampered_unsigned_command",
    "FLEET_RESERVE_FLOOR_PRESSURE": "reserve_floor_pressure",
    "SCADA_BANK_OVERLOAD": "bank_overload",
    "SCADA_LOAD_SPIKE": "load_spike",
    "SCADA_FROZEN_VALUE": "frozen_value",
    "SCADA_BAD_QUALITY": "bad_quality_flag",
    "SCADA_STALE": "stale_no_update",
    "SCADA_OUT_OF_RANGE": "out_of_range_value",
    "SCADA_OSCILLATION": "oscillation",
    "SCADA_PHASE_IMBALANCE": "phase_imbalance",
    "SCADA_TOPOLOGY_CHANGE": "breaker_open",
    "SCADA_COMMS_LOSS": "comms_loss",
    "SCADA_UTILITY_INSTRUCTION": "utility_instruction",
    "SCADA_TIME_SKEW": "time_skew",
}


@dataclass(frozen=True)
class ScenarioCommand:
    """Normalized scenario command, regardless of which wire shape it arrived in."""

    id: str
    catalogue_type: str  # lowercase ogsim.control.catalogue id
    target_kind: str | None  # sim/asset/zone/bank/hub, if the schema shape was used
    target_ref: str  # bank_id/hub_id/zone name/"*"
    params: dict[str, Any]
    start_epoch: float
    duration_s: float | None


def _parse_start(value: Any) -> float:
    if isinstance(value, int | float):
        return float(value)
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()


def parse_scenario_cmd(raw: dict[str, Any]) -> ScenarioCommand:
    """Parses a `<root>/scenario/cmd` payload, trying the schema-conformant
    shape first and falling back to the legacy flat shape (see module
    docstring). Raises `ValueError` if neither shape fits."""
    target = raw.get("target")
    if isinstance(target, dict) and "kind" in target and "ref" in target:
        wire_type = str(raw.get("type", ""))
        catalogue_type = WIRE_TYPE_TO_CATALOGUE_ID.get(wire_type, wire_type.lower())
        return ScenarioCommand(
            id=str(raw["id"]),
            catalogue_type=catalogue_type,
            target_kind=str(target["kind"]),
            target_ref=str(target["ref"]),
            params=dict(raw.get("params", {})),
            start_epoch=_parse_start(raw["start"]),
            duration_s=_optional_float(raw.get("duration_s")),
        )
    if isinstance(target, str):
        return ScenarioCommand(
            id=str(raw["id"]),
            catalogue_type=str(raw.get("type", "")).lower(),
            target_kind=None,
            target_ref=target,
            params=dict(raw.get("params", {})),
            start_epoch=_parse_start(raw["start"]),
            duration_s=_optional_float(raw.get("duration")),
        )
    raise ValueError(f"scenario/cmd payload has neither a recognized target shape: {raw!r}")


def _optional_float(value: Any) -> float | None:
    return None if value is None else float(value)


def utc_timestamp(epoch_seconds: float) -> str:
    """Formats a Unix-epoch float as the RFC3339 `...Z` timestamps these
    schemas require (telemetry.ts, ack.ts, scada_bank_signal.ts, ...)."""
    return datetime.fromtimestamp(epoch_seconds, tz=UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
