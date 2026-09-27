"""ogsim.common.scenario -- `<root>/scenario/cmd` parsing.

Parses the single schema-conformant shape published by `ogsim.control`
(`mqtt_pub.py`/`injector.py`) and consumed by `ogsim.fleet`/`ogsim.scada`,
per `interfaces/mqtt/scenario_control.schema.json`: `target: {kind, ref}`,
`type` an uppercase `FLEET_*`/`SCADA_*`/`MARKET_*`/`PARTNER_CALL` enum
value, `start` an RFC3339 string, `duration_s` in seconds. `ogsim.control`
and `ogsim.fleet`/`ogsim.scada` are both owned by this agent, so there is
one wire shape, not a tolerant fallback.
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
    "SCADA_METER_MISMATCH": "meter_mismatch",
    "SCADA_SITE_SAG_SWELL": "site_sag_swell",
    # PQ / inverter imperfection (06-service-profiles-and-power-quality.md §7.2/§7.5).
    "FLEET_FREQUENCY_DRIFT": "frequency_drift",
    "FLEET_HARMONIC_INJECTION": "harmonic_injection",
    "FLEET_PHASE_IMBALANCE_INJECTION": "phase_imbalance_injection",
    "FLEET_CALIBRATION_DRIFT_CORRECTABLE": "calibration_drift_correctable",
    "FLEET_CALIBRATION_DRIFT_HARDWARE": "calibration_drift_hardware",
    "FLEET_REPLACE_INVERTER": "replace_inverter",
    # Service-catalogue additions (build phase, 2026-09-26; SERVICES agent's scenario files,
    # FLEET-SIM wiring).
    "MARKET_PJM_EMERGENCY_PERFORMANCE_EVENT": "pjm_emergency_performance_event",
    "MARKET_AS_DEPLOYMENT": "as_deployment",
    "FLEET_MOBILE_DEPLOYMENT_START": "mobile_deployment_start",
    "FLEET_MOBILE_DEPLOYMENT_RELOCATE": "mobile_deployment_relocate",
    "FLEET_MOBILE_HOME_STATION_CHARGE": "mobile_home_station_charge",
    # ogsim.customer (customer-operator simulators): shared here per coordination with the
    # lead so ogsim.customer can reuse parse_scenario_cmd unchanged.
    "CUSTOMER_LOAD_STEP": "load_step_datacenter",
    "CUSTOMER_PIPELINE_CURRENT_SURGE": "pipeline_current_surge",
    "CUSTOMER_REQUEST_BURST": "request_burst",
    "CUSTOMER_MALFORMED_REQUEST": "malformed_request",
    "CUSTOMER_LATE_CANCELLATION": "late_cancellation",
    "CUSTOMER_INVOICE_DISPUTE": "invoice_dispute",
    "CUSTOMER_SITE_METER_STALE": "site_meter_stale",
    "CUSTOMER_LARGE_LOAD_CURTAILMENT_REQUEST": "large_load_curtailment_request",
    # ogsim.utility_aen (the simulated utility EMS, D-29/D-33).
    "UTILITY_CALL_NORMAL": "utility_call_normal",
    "UTILITY_CALL_OVERLAP": "utility_call_overlap",
    "UTILITY_CALL_OVER_CAP": "utility_call_over_cap",
    "UTILITY_CALL_CHARGE": "utility_call_charge",
    "UTILITY_CALL_CANCEL_MID": "utility_call_cancel_mid",
}


@dataclass(frozen=True)
class ScenarioCommand:
    """Normalized scenario command, parsed from the schema-conformant wire shape."""

    id: str
    catalogue_type: str  # lowercase ogsim.control.catalogue id
    target_kind: str  # sim/asset/zone/bank/hub
    target_ref: str  # bank_id/hub_id/zone name/"*"
    params: dict[str, Any]
    start_epoch: float
    duration_s: float | None


def _parse_start(value: Any) -> float:
    if isinstance(value, int | float):
        return float(value)
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()


def parse_scenario_cmd(raw: dict[str, Any]) -> ScenarioCommand:
    """Parses a `<root>/scenario/cmd` payload conforming to
    `interfaces/mqtt/scenario_control.schema.json`. Raises `ValueError` if
    `target` is not the `{kind, ref}` shape that schema requires."""
    target = raw.get("target")
    if not (isinstance(target, dict) and "kind" in target and "ref" in target):
        raise ValueError(f"scenario/cmd payload has no valid target shape: {raw!r}")
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


def _optional_float(value: Any) -> float | None:
    return None if value is None else float(value)


def utc_timestamp(epoch_seconds: float) -> str:
    """Formats a Unix-epoch float as the RFC3339 `...Z` timestamps these
    schemas require (telemetry.ts, ack.ts, scada_bank_signal.ts, ...)."""
    return datetime.fromtimestamp(epoch_seconds, tz=UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
