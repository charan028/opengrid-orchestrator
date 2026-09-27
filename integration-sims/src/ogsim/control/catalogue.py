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
        id="pjm_emergency_performance_event",
        owner="market",
        target_kind="pjm zone (e.g. pjm-zone-aep-01)",
        params={
            "committed_kw": {"type": "number", "default": 5000.0},
            "declared_by": {"type": "string", "default": "SIMULATED_ISO_INSTRUCTION"},
            "recall": {"type": "boolean", "default": False},
        },
        description=(
            "Declares (or recalls, `recall: true`) a simulated PJM RPM capacity-performance emergency "
            "event for a PJM_CAPACITY contract (config/service_profiles/pjm_capacity.toml). PJM stays "
            "fully simulated -- this is the only source of a declared emergency-performance hour. "
            "Registered 2026-09-26 for `svc-pjm-capacity.yaml` (SERVICES agent); shares the same "
            "market-side deployment-event mechanism as `as_deployment` below."
        ),
        wire_type="MARKET_PJM_EMERGENCY_PERFORMANCE_EVENT",
    ),
    AnomalyType(
        id="as_deployment",
        owner="market",
        target_kind="AS product (e.g. RRS, REGUP, ECRS) or *",
        params={
            "service": {
                "type": "string",
                "default": "RRS",
                "enum": ["REGUP", "REGDN", "RRS", "NSPIN", "ECRS"],
            },
            "deployed_mw": {"type": "number", "default": 50.0},
            "recall": {"type": "boolean", "default": False},
        },
        description=(
            "Publishes a simulated ERCOT AS deployment instruction (build phase 2026-09-26, FLEET-SIM): "
            "the engine treats this exactly like an operator/ISO deployment (`og.as_deployment`), "
            "randomly (Poisson, `ogsim.control.random_engine`) or via manual/scenario injection. "
            "`recall: true` ends a deployment already in progress. See "
            "`ogsim.market.as_deployment`/the FLEET-SIM build report's DISPATCH wiring notes."
        ),
        wire_type="MARKET_AS_DEPLOYMENT",
    ),
    # ---- ERCOT AS dispatch instructions on the MMS/EWS endpoint (D-35, ogsim.market.as_dispatch) ----
    # One-shot: each injection publishes the instruction(s) at once; `duration` only bounds the log
    # entry. Never drawn by random mode (not listed in config/random.yaml).
    AnomalyType(
        id="ercot_as_deploy",
        owner="market",
        target_kind="ERCOT resource (e.g. OG_ESR_1, or * for the first award)",
        params={
            "service": {
                "type": "string",
                "default": "ECRS",
                "enum": ["ECRS", "RRS", "REGUP", "REGDN", "NSPIN"],
            },
            "mw": {"type": "number", "default": 0.0, "description": "0 = the sim's award MW"},
            "duration_min": {"type": "number", "default": 30.0},
            "ramp_min": {"type": "integer", "default": 10},
            "start_in_s": {"type": "number", "default": 0.0},
        },
        description="ERCOT deploys an awarded AS (DEPLOY_AS dispatch instruction on /mms/ews/).",
        wire_type="MARKET_ERCOT_AS_DEPLOY",
    ),
    AnomalyType(
        id="ercot_as_recall",
        owner="market",
        target_kind="ERCOT resource",
        params={"service": {"type": "string", "default": "ECRS"}},
        description="ERCOT recalls the newest deployment of that resource and service (RECALL_AS).",
        wire_type="MARKET_ERCOT_AS_RECALL",
    ),
    AnomalyType(
        id="ercot_as_duplicate",
        owner="market",
        target_kind="ERCOT resource",
        params={"service": {"type": "string", "default": "ECRS"}, "mw": {"type": "number", "default": 0.0}},
        description="A DEPLOY_AS delivered again after it was acknowledged (at-least-once transport).",
        wire_type="MARKET_ERCOT_AS_DUPLICATE",
    ),
    AnomalyType(
        id="ercot_as_out_of_order",
        owner="market",
        target_kind="ERCOT resource",
        params={"service": {"type": "string", "default": "ECRS"}, "mw": {"type": "number", "default": 0.0}},
        description="A DEPLOY_AS and its RECALL_AS, the recall delivered first (out-of-order delivery).",
        wire_type="MARKET_ERCOT_AS_OUT_OF_ORDER",
    ),
    AnomalyType(
        id="ercot_as_malformed",
        owner="market",
        target_kind="ERCOT resource",
        params={"service": {"type": "string", "default": "ECRS"}},
        description="A DEPLOY_AS whose MW and start time cannot be parsed (the QSE must reject it).",
        wire_type="MARKET_ERCOT_AS_MALFORMED",
    ),
    AnomalyType(
        id="ercot_as_unknown_award",
        owner="market",
        target_kind="ERCOT resource with no award (default OG_ESR_UNKNOWN)",
        params={"service": {"type": "string", "default": "ECRS"}, "mw": {"type": "number", "default": 0.5}},
        description="A DEPLOY_AS for a resource/service the QSE holds no award for.",
        wire_type="MARKET_ERCOT_AS_UNKNOWN_AWARD",
    ),
    AnomalyType(
        id="ercot_as_exceed_award",
        owner="market",
        target_kind="ERCOT resource",
        params={
            "service": {"type": "string", "default": "ECRS"},
            "factor": {"type": "number", "default": 3.0},
        },
        description="A DEPLOY_AS for more MW than awarded (factor x the award).",
        wire_type="MARKET_ERCOT_AS_EXCEED_AWARD",
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
    AnomalyType(
        id="mobile_deployment_start",
        owner="fleet",
        target_kind="asset (mobile trailer, e.g. trailer-mb-01)",
        params={
            "site_id": {"type": "string", "default": ""},
            "site_lat": {"type": "number", "default": None},
            "site_lon": {"type": "number", "default": None},
            "committed_kw": {"type": "number", "default": 500.0},
            "arrival_soc_pct": {"type": "number", "default": 90.0},
        },
        description=(
            "Deploys a mobile battery/trailer asset to `site_id` for a MOBILE_STORAGE contract "
            "(config/service_profiles/mobile_storage.toml): delivered kW is metered from the moment of "
            "arrival, with `arrival_soc_pct` as the deployment's starting condition. With `site_lat`/"
            "`site_lon`, ogsim.fleet drives a simulated truck there (its device_info position follows; D-31: "
            "it never charges away from its home station). Registered 2026-09-26 for "
            "`svc-mobile-storage.yaml`."
        ),
        wire_type="FLEET_MOBILE_DEPLOYMENT_START",
        wire_target_kind="asset",
    ),
    AnomalyType(
        id="mobile_deployment_relocate",
        owner="fleet",
        target_kind="asset (mobile trailer)",
        params={
            "from_site_id": {"type": "string", "default": ""},
            "to_site_id": {"type": "string", "default": ""},
            "to_site_lat": {"type": "number", "default": None},
            "to_site_lon": {"type": "number", "default": None},
            "committed_kw": {"type": "number", "default": 500.0},
        },
        description=(
            "Mid-contract relocation of a mobile trailer already deployed by `mobile_deployment_start`: "
            "changes `site_id` (and its feedback-signal/M&V point) cleanly between two sites for the "
            "SAME trailer, without touching the profile template."
        ),
        wire_type="FLEET_MOBILE_DEPLOYMENT_RELOCATE",
        wire_target_kind="asset",
    ),
    AnomalyType(
        id="mobile_home_station_charge",
        owner="fleet",
        target_kind="asset (mobile trailer, e.g. trailer-mb-01)",
        params={
            "home_station_id": {"type": "string", "default": ""},
            "start_soc_pct": {"type": "number", "default": 40.0},
            "target_soc_pct": {"type": "number", "default": 92.0},
        },
        description=(
            "A mobile trailer charges at its registered home station (D-31, 2026-09-26: docs/orchestrator/"
            "07-delivery/11-decision-log.md -- NEVER from the fleet or a customer deployment site), from "
            "that station's own grid connection. The only legitimate source of a mobile unit's charge in "
            "any ogsim-driven simulation of a MOBILE_STORAGE contract. Registered 2026-09-26 for "
            "`svc-mobile-storage.yaml` (SERVICES agent), which had used this type as a stand-in ahead of "
            "its catalogue registration; validated against fleet.yaml's `mobile_units:` registry (#33 "
            "target-check)."
        ),
        wire_type="FLEET_MOBILE_HOME_STATION_CHARGE",
        wire_target_kind="asset",
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
    # ---- Customer operators (owner: customer, applied by ogsim.customer over MQTT and the
    # orchestrator's customer API) ----
    AnomalyType(
        id="load_step_datacenter",
        owner="customer",
        target_kind="site",
        params={"step_kw": {"type": "number", "default": 500.0}},
        description="Steps a DATA_CENTER site's real-power draw up sharply (load step).",
        wire_type="CUSTOMER_LOAD_STEP",
        wire_target_kind="site",
    ),
    AnomalyType(
        id="pipeline_current_surge",
        owner="customer",
        target_kind="corridor",
        params={"surge_a": {"type": "number", "default": 20.0}},
        description="Surges a PIPELINE_AC corridor's induced AC current above its mitigation limit.",
        wire_type="CUSTOMER_PIPELINE_CURRENT_SURGE",
        wire_target_kind="corridor",
    ),
    AnomalyType(
        id="request_burst",
        owner="customer",
        target_kind="customer",
        params={"count": {"type": "integer", "default": 5}},
        description="Submits a burst of opportunity/service requests in quick succession.",
        wire_type="CUSTOMER_REQUEST_BURST",
        wire_target_kind="customer",
    ),
    AnomalyType(
        id="malformed_request",
        owner="customer",
        target_kind="customer",
        params={},
        description="Submits a malformed or oversize request; must be rejected (R-ADMIT-REJECT).",
        wire_type="CUSTOMER_MALFORMED_REQUEST",
        wire_target_kind="customer",
    ),
    AnomalyType(
        id="late_cancellation",
        owner="customer",
        target_kind="customer",
        params={},
        description="Requests cancellation of an obligation inside its contractual notice window.",
        wire_type="CUSTOMER_LATE_CANCELLATION",
        wire_target_kind="customer",
    ),
    AnomalyType(
        id="invoice_dispute",
        owner="customer",
        target_kind="customer",
        params={"reason_code": {"type": "string", "default": "USAGE_MISMATCH"}},
        description="Disputes the customer's most recent invoice.",
        wire_type="CUSTOMER_INVOICE_DISPUTE",
        wire_target_kind="customer",
    ),
    AnomalyType(
        id="large_load_curtailment_request",
        owner="customer",
        target_kind="site",
        params={"step_kw": {"type": "number", "default": 2000.0}},
        description=(
            "A large flexible load's own curtailment/ride-through load-step signal for a LARGE_LOAD "
            "contract (config/service_profiles/large_load.toml). Mirrors `load_step_datacenter`'s wire "
            "shape 1:1 (both are a generic CUSTOMER load step at a site meter) but is correctly "
            "described for LARGE_LOAD rather than DATA_CENTER. Registered 2026-09-26 for "
            "`svc-large-load.yaml` (SERVICES agent), which had used `load_step_datacenter` as a stand-in."
        ),
        wire_type="CUSTOMER_LARGE_LOAD_CURTAILMENT_REQUEST",
        wire_target_kind="site",
    ),
    AnomalyType(
        id="site_meter_stale",
        owner="customer",
        target_kind="site",
        params={},
        description="Stops publishing a DATA_CENTER site's meter readings (stale telemetry).",
        wire_type="CUSTOMER_SITE_METER_STALE",
        wire_target_kind="site",
    ),
]

BY_ID: dict[str, AnomalyType] = {a.id: a for a in CATALOGUE}
MARKET_TYPES = {a.id for a in CATALOGUE if a.owner == "market"}
SCADA_TYPES = {a.id for a in CATALOGUE if a.owner == "scada"}
FLEET_TYPES = {a.id for a in CATALOGUE if a.owner == "fleet"}
CUSTOMER_TYPES = {a.id for a in CATALOGUE if a.owner == "customer"}


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
    ("site", "site"),
    ("corridor", "corridor"),
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


def as_list() -> list[dict[str, Any]]:
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
