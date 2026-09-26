"""The full anomaly catalogue (BUILD.md §3), one registry for every
simulator. Each entry: id (the internal `type` used on inject/REST/CLI),
target_kind (what `target` should name), params schema (informal: name ->
{type, default, description}), description, and which simulator owns
applying it.

`market` types are applied by ogsim.market itself (via its admin API) - they
never go over MQTT. `scada` and `fleet` types are applied by ogsim.fleet /
ogsim.scada, driven by the `<root>/scenario/cmd` MQTT message this control
plane publishes, validated against
`interfaces/mqtt/scenario_control.schema.json`; their behaviour is
implemented by the sims agent, not here. `wire_type` is that schema's
`type` enum value (e.g. "SCADA_BANK_OVERLOAD") and `wire_target_kind` is its
`target.kind` enum value (sim/asset/zone/bank/hub) - both required on the
wire, distinct from this catalogue's own lowercase `id`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class AnomalyType:
    id: str
    owner: str  # "market" | "scada" | "fleet"
    target_kind: str  # what `target` identifies, e.g. "product", "bank", "hub", "zone"
    params: dict[str, Any] = field(default_factory=dict)
    description: str = ""
    wire_type: str = ""  # scenario_control.schema.json `type` enum value (scada/fleet only)
    wire_target_kind: str = ""  # scenario_control.schema.json `target.kind` enum value


CATALOGUE: list[AnomalyType] = [
    # ---- Market / data APIs (owner: market, applied inside ogsim.market) ----
    AnomalyType(
        id="price_spike",
        owner="market",
        target_kind="product (e.g. np6-905-cd, or *)",
        params={"value_usd_per_mwh": {"type": "number", "default": 5000.0}},
        description="Overrides the latest settlement point price to a spike value.",
        wire_type="MARKET_PRICE_SPIKE",
    ),
    AnomalyType(
        id="negative_price",
        owner="market",
        target_kind="product",
        params={"value_usd_per_mwh": {"type": "number", "default": -50.0}},
        description="Overrides the latest settlement point price to a negative value.",
        wire_type="MARKET_NEGATIVE_PRICE",
    ),
    AnomalyType(
        id="as_price_jump",
        owner="market",
        target_kind="product (np4-188-cd)",
        params={
            "service": {
                "type": "string",
                "default": "RRS",
                "enum": ["REGUP", "REGDN", "RRS", "NSPIN", "ECRS"],
            },
            "value_usd_per_mwh": {"type": "number", "default": 500.0},
        },
        description="Overrides one AS product's DAM clearing price (market-api.md ancillaryType codes).",
        wire_type="MARKET_AS_PRICE_JUMP",
    ),
    AnomalyType(
        id="http_5xx",
        owner="market",
        target_kind="product or *",
        params={"status": {"type": "integer", "default": 503}},
        description="Returns an HTTP 5xx for the targeted product (outage).",
        wire_type="MARKET_HTTP_5XX",
    ),
    AnomalyType(
        id="http_429",
        owner="market",
        target_kind="product or *",
        params={"retry_after_s": {"type": "integer", "default": 30}},
        description="Returns HTTP 429 with a Retry-After header (throttling).",
        wire_type="MARKET_429_THROTTLE",
    ),
    AnomalyType(
        id="http_401_primary",
        owner="market",
        target_kind="product or *",
        params={},
        description="Returns HTTP 401 only when the caller used the primary key (tests key rotation).",
        wire_type="MARKET_401_KEY_REJECT",
    ),
    AnomalyType(
        id="stale_posting",
        owner="market",
        target_kind="product or *",
        params={},
        description="Freezes the simulated clock for this product so no new data posts.",
        wire_type="MARKET_STALE_POSTING",
    ),
    AnomalyType(
        id="malformed_payload",
        owner="market",
        target_kind="product or *",
        params={},
        description="Returns a syntactically broken JSON body.",
        wire_type="MARKET_MALFORMED_PAYLOAD",
    ),
    AnomalyType(
        id="slow_response",
        owner="market",
        target_kind="product or *",
        params={"latency_s": {"type": "number", "default": 5.0}},
        description="Adds latency before responding.",
        wire_type="MARKET_SLOW_RESPONSE",
    ),
    AnomalyType(
        id="nws_extreme_weather",
        owner="market",
        target_kind="nws",
        params={
            "condition": {"type": "string", "default": "Severe Thunderstorm"},
            "temperature_c": {"type": "number", "default": 42.0},
            "wind_kph": {"type": "number", "default": 90.0},
        },
        description="Overrides the NWS hourly forecast with extreme conditions.",
        wire_type="MARKET_NWS_EXTREME_WEATHER",
    ),
    # ---- SCADA (owner: scada, applied by ogsim.scada over MQTT) ----
    AnomalyType(
        id="bank_overload",
        owner="scada",
        target_kind="bank",
        params={"kva_over_rating_pct": {"type": "number", "default": 20.0}},
        description="Reports bank load above its kVA rating.",
        wire_type="SCADA_BANK_OVERLOAD",
        wire_target_kind="bank",
    ),
    AnomalyType(
        id="load_spike",
        owner="scada",
        target_kind="bank",
        params={"multiplier": {"type": "number", "default": 2.0}},
        description="Steps the bank load up sharply.",
        wire_type="SCADA_LOAD_SPIKE",
        wire_target_kind="bank",
    ),
    AnomalyType(
        id="frozen_value",
        owner="scada",
        target_kind="bank",
        params={},
        description="Freezes the reported SCADA reading.",
        wire_type="SCADA_FROZEN_VALUE",
        wire_target_kind="bank",
    ),
    AnomalyType(
        id="bad_quality_flag",
        owner="scada",
        target_kind="bank",
        params={},
        description="Marks the SCADA reading with a bad-quality flag.",
        wire_type="SCADA_BAD_QUALITY",
        wire_target_kind="bank",
    ),
    AnomalyType(
        id="stale_no_update",
        owner="scada",
        target_kind="bank",
        params={},
        description="Stops publishing SCADA updates for the bank.",
        wire_type="SCADA_STALE",
        wire_target_kind="bank",
    ),
    AnomalyType(
        id="out_of_range_value",
        owner="scada",
        target_kind="bank",
        params={"value": {"type": "number", "default": -1.0}},
        description="Publishes a physically impossible reading.",
        wire_type="SCADA_OUT_OF_RANGE",
        wire_target_kind="bank",
    ),
    AnomalyType(
        id="oscillation",
        owner="scada",
        target_kind="bank",
        params={
            "amplitude_pct": {"type": "number", "default": 15.0},
            "period_s": {"type": "number", "default": 10.0},
        },
        description="Oscillates the reported load rapidly.",
        wire_type="SCADA_OSCILLATION",
        wire_target_kind="bank",
    ),
    AnomalyType(
        id="phase_imbalance",
        owner="scada",
        target_kind="bank",
        params={"imbalance_pct": {"type": "number", "default": 25.0}},
        description="Introduces per-phase load imbalance.",
        wire_type="SCADA_PHASE_IMBALANCE",
        wire_target_kind="bank",
    ),
    AnomalyType(
        id="breaker_open",
        owner="scada",
        target_kind="bank",
        params={},
        description="Simulates a breaker open / topology change.",
        wire_type="SCADA_TOPOLOGY_CHANGE",
        wire_target_kind="bank",
    ),
    AnomalyType(
        id="comms_loss",
        owner="scada",
        target_kind="bank",
        params={},
        description="Simulates loss of SCADA comms for the bank.",
        wire_type="SCADA_COMMS_LOSS",
        wire_target_kind="bank",
    ),
    AnomalyType(
        id="utility_instruction",
        owner="scada",
        target_kind="bank",
        params={
            "mode": {"type": "string", "default": "limit", "enum": ["limit", "block", "estop"]},
            "limit_kw": {"type": "number", "default": 0.0},
        },
        description="Injects a utility limit/block/ESTOP instruction.",
        wire_type="SCADA_UTILITY_INSTRUCTION",
        wire_target_kind="bank",
    ),
    AnomalyType(
        id="time_skew",
        owner="scada",
        target_kind="bank",
        params={"skew_s": {"type": "number", "default": 300.0}},
        description="Skews the SCADA reading's timestamp.",
        wire_type="SCADA_TIME_SKEW",
        wire_target_kind="bank",
    ),
    # ---- Fleet (hubs) (owner: fleet, applied by ogsim.fleet over MQTT) ----
    AnomalyType(
        id="hub_offline",
        owner="fleet",
        target_kind="hub",
        params={},
        description="Takes a hub offline (stops telemetry).",
        wire_type="FLEET_HUB_OFFLINE",
        wire_target_kind="hub",
    ),
    AnomalyType(
        id="zone_mass_disconnect",
        owner="fleet",
        target_kind="zone",
        params={},
        description="Stops telemetry for all hubs in a zone.",
        wire_type="FLEET_ZONE_MASS_DISCONNECT",
        wire_target_kind="zone",
    ),
    AnomalyType(
        id="not_following_commands",
        owner="fleet",
        target_kind="hub or bank",
        params={"mode": {"type": "string", "default": "partial", "enum": ["partial", "none"]}},
        description="Hub ignores part or all of commanded setpoints.",
        wire_type="FLEET_NOT_FOLLOWING_COMMANDS",
        wire_target_kind="hub",
    ),
    AnomalyType(
        id="inverter_trip",
        owner="fleet",
        target_kind="hub",
        params={},
        description="Simulates an inverter trip (forced to 0 kW, fault flag).",
        wire_type="FLEET_INVERTER_TRIP",
        wire_target_kind="hub",
    ),
    AnomalyType(
        id="soc_sensor_drift",
        owner="fleet",
        target_kind="hub",
        params={"drift_kwh_per_min": {"type": "number", "default": 0.5}},
        description="Reported SoC drifts away from true SoC.",
        wire_type="FLEET_SOC_SENSOR_DRIFT",
        wire_target_kind="hub",
    ),
    AnomalyType(
        id="telemetry_delay_burst",
        owner="fleet",
        target_kind="hub or zone",
        params={"delay_s": {"type": "number", "default": 10.0}},
        description="Delays telemetry, then bursts it.",
        wire_type="FLEET_TELEMETRY_DELAY_BURST",
        wire_target_kind="hub",
    ),
    AnomalyType(
        id="lease_loss",
        owner="fleet",
        target_kind="hub",
        params={},
        description="Forces the hub's lease to expire immediately.",
        wire_type="FLEET_LEASE_LOSS",
        wire_target_kind="hub",
    ),
    AnomalyType(
        id="clock_skew",
        owner="fleet",
        target_kind="hub",
        params={"skew_s": {"type": "number", "default": 300.0}},
        description="Skews the hub's local clock.",
        wire_type="FLEET_CLOCK_SKEW",
        wire_target_kind="hub",
    ),
    AnomalyType(
        id="tampered_unsigned_command",
        owner="fleet",
        target_kind="hub or bank",
        params={},
        description="Injects a command with an invalid/missing signature (must be rejected).",
        wire_type="FLEET_TAMPERED_UNSIGNED_COMMAND",
        wire_target_kind="hub",
    ),
    AnomalyType(
        id="reserve_floor_pressure",
        owner="fleet",
        target_kind="hub or zone",
        params={"home_load_kw": {"type": "number", "default": 6.0}},
        description="Raises simulated home load toward the reserve floor.",
        wire_type="FLEET_RESERVE_FLOOR_PRESSURE",
        wire_target_kind="hub",
    ),
    # ---- Power quality / inverter imperfection (owner: fleet, 06-service-profiles-and-
    # power-quality.md §7.2/§7.5) -- applied by ogsim.fleet.pq.PqAnomalyManager. ----
    AnomalyType(
        id="frequency_drift",
        owner="fleet",
        target_kind="hub",
        params={"target_offset_hz": {"type": "number", "default": 0.3}},
        description="Ramps an inverter's frequency offset toward a target (PLL fault/firmware regression).",
        wire_type="FLEET_FREQUENCY_DRIFT",
        wire_target_kind="hub",
    ),
    AnomalyType(
        id="harmonic_injection",
        owner="fleet",
        target_kind="hub",
        params={
            "thd_target_pct": {"type": "number", "default": 8.0},
            "order": {"type": "integer", "default": 5, "enum": [3, 5, 7]},
        },
        description="Steps THD_I up and reweights harmonics toward one order (failing DC-link/PWM fault).",
        wire_type="FLEET_HARMONIC_INJECTION",
        wire_target_kind="hub",
    ),
    AnomalyType(
        id="phase_imbalance_injection",
        owner="fleet",
        target_kind="hub or zone",
        params={"bias_kw": {"type": "number", "default": 2.0}},
        description=(
            "Biases a hub's effective dispatched kW to skew one phase's aggregate (fleet-side "
            "companion to the SCADA-side `phase_imbalance` reporting artifact)."
        ),
        wire_type="FLEET_PHASE_IMBALANCE_INJECTION",
        wire_target_kind="hub",
    ),
    AnomalyType(
        id="calibration_drift_correctable",
        owner="fleet",
        target_kind="hub",
        params={
            "target_freq_offset_hz": {"type": "number", "default": 0.2},
            "target_voltage_offset_pct": {"type": "number", "default": 2.0},
        },
        description=(
            "Drifts frequency/voltage/phase-angle offsets toward a target; a subsequent "
            "CalibrationCommand fully or partially removes the drift (§5.5.4 ladder CORRECTED/IMPROVED)."
        ),
        wire_type="FLEET_CALIBRATION_DRIFT_CORRECTABLE",
        wire_target_kind="hub",
    ),
    AnomalyType(
        id="calibration_drift_hardware",
        owner="fleet",
        target_kind="hub",
        params={
            "target_freq_offset_hz": {"type": "number", "default": 0.2},
            "target_voltage_offset_pct": {"type": "number", "default": 2.0},
        },
        description=(
            "Same drift symptom as calibration_drift_correctable, but no CalibrationCommand has any "
            "effect (simulated failed component) -- exercises the DEGRADED/replacement escalation path."
        ),
        wire_type="FLEET_CALIBRATION_DRIFT_HARDWARE",
        wire_target_kind="hub",
    ),
    AnomalyType(
        id="site_sag_swell",
        owner="scada",
        target_kind="bank",
        params={
            "mode": {"type": "string", "default": "sag", "enum": ["sag", "swell"]},
            "pu_level": {"type": "number", "default": 0.85},
        },
        description=(
            "Dips or raises reported per-phase voltage to a configured pu level, IEEE 1159 sag/swell "
            "shaped, testing ride-through and G-23 (implemented by ogsim.scada, not this agent's lane)."
        ),
        wire_type="SCADA_SITE_SAG_SWELL",
        wire_target_kind="bank",
    ),
    AnomalyType(
        id="replace_inverter",
        owner="fleet",
        target_kind="hub",
        params={
            "new_serial": {"type": "string", "default": ""},
            "new_firmware": {"type": "string", "default": ""},
        },
        description=(
            "Physical inverter replacement (§ terminology: never a dispatch 'swap'): resets a hub's "
            "unit(s) to freshly-drawn 'new unit' offsets/serial and clears any active calibration-drift "
            "anomaly on it. An instantaneous control action, not a timed anomaly."
        ),
        wire_type="FLEET_REPLACE_INVERTER",
        wire_target_kind="hub",
    ),
]

BY_ID: dict[str, AnomalyType] = {a.id: a for a in CATALOGUE}
MARKET_TYPES = {a.id for a in CATALOGUE if a.owner == "market"}
SCADA_TYPES = {a.id for a in CATALOGUE if a.owner == "scada"}
FLEET_TYPES = {a.id for a in CATALOGUE if a.owner == "fleet"}


def owner_of(anomaly_type: str) -> str | None:
    entry = BY_ID.get(anomaly_type)
    return entry.owner if entry else None


# Naming conventions used across ogsim.fleet/ogsim.scada for target refs
# (fleet/state.py's f"hub-{i:05d}"/f"bank-{i:03d}", common/config.py's
# DEFAULT_ZONES "LZ_*"), used only to disambiguate a catalogue entry whose
# `target_kind` names more than one possible wire kind (e.g. "hub or bank").
# An unrecognized ref (e.g. an opaque test id) falls back to `wire_target_kind`.
_REF_PREFIX_TO_WIRE_KIND: tuple[tuple[str, str], ...] = (
    ("bank", "bank"),
    ("hub", "hub"),
    ("lz_", "zone"),
    ("zone", "zone"),
)


def infer_wire_target_kind(entry: AnomalyType, target: str) -> str:
    """Infers the wire `target.kind` for one injection of `entry`, based on
    the *actual* target ref rather than the catalogue's single default.

    Several catalogue entries apply to more than one wire kind (e.g.
    `not_following_commands`: "hub or bank", `reserve_floor_pressure`: "hub
    or zone"): `entry.wire_target_kind` alone would always report the same
    kind even when a given injection actually targets a different one. This
    matches `target` against known ref naming conventions and only accepts
    a match that `entry.target_kind` actually allows; otherwise it falls
    back to `entry.wire_target_kind`.
    """
    normalized = target.strip().lower()
    for prefix, kind in _REF_PREFIX_TO_WIRE_KIND:
        if normalized.startswith(prefix) and kind in entry.target_kind:
            return kind
    return entry.wire_target_kind


def as_list() -> list[dict]:
    return [
        {
            "id": a.id,
            "owner": a.owner,
            "target_kind": a.target_kind,
            "params": a.params,
            "description": a.description,
        }
        for a in CATALOGUE
    ]
