# Simulator scenarios

`POST /og/api/scenario/{name}` lets an operator inject a simulator anomaly from the console's scenario panel. It is
a thin, validated bridge: og-api publishes one `scenario_control` message to `<root>/scenario/cmd` over MQTT (as
the `og_api` user, the only process allowed to), and the simulators act on it. og-api implements no scenario logic
itself. Role: **operator**.

```
POST /og/api/scenario/FLEET_HUB_OFFLINE
{"target_kind": "hub", "target_ref": "hub-00042", "params": {}, "duration_s": 60}

-> 200 {"scenario_id": "<uuid>", "type": "FLEET_HUB_OFFLINE", "trace_id": "<uuid>"}
```

| Field | Default | Meaning |
|---|---|---|
| `{name}` (path) | | The wire anomaly type, one of the `type` enum in `interfaces/mqtt/scenario_control.schema.json` (below) |
| `target_kind` | `sim` | `sim`, `asset`, `zone`, `bank` or `hub` |
| `target_ref` | `fleet` | The id the kind refers to, e.g. `hub-00042`, `bank-003`, `LZ_NORTH` |
| `params` | `{}` | Type-specific parameters (numbers, strings, booleans), e.g. `{"mode": "block"}` |
| `duration_s` | none | How long the anomaly lasts; omitted means until cancelled in the simulator |

The payload is validated against the schema before it is published: an unknown `{name}` or a bad target is
rejected (`422`) and nothing reaches the broker. Every trigger is traced (`OPERATOR_ACTION` / `SCENARIO`) and
recorded as an operator action, so the audit trail shows who injected what.

## Types

- **SCADA** (acted on by `ogsim.scada`, target a bank): `SCADA_BANK_OVERLOAD`, `SCADA_LOAD_SPIKE`,
  `SCADA_FROZEN_VALUE`, `SCADA_BAD_QUALITY`, `SCADA_STALE`, `SCADA_OUT_OF_RANGE`, `SCADA_OSCILLATION`,
  `SCADA_PHASE_IMBALANCE`, `SCADA_TOPOLOGY_CHANGE`, `SCADA_COMMS_LOSS`, `SCADA_UTILITY_INSTRUCTION`
  (`params.mode` = `limit` / `block` / `estop`, `params.limit_kw`), `SCADA_TIME_SKEW`, `SCADA_SITE_SAG_SWELL`,
  `SCADA_METER_MISMATCH` (D-38: the bank meter sees only `params.battery_scale` x the battery power, plus
  `params.offset_kw`, so the delivery check marks calls on that bank UNCORROBORATED).
- **Fleet** (acted on by `ogsim.fleet`, target a hub or zone): `FLEET_HUB_OFFLINE`, `FLEET_ZONE_MASS_DISCONNECT`,
  `FLEET_NOT_FOLLOWING_COMMANDS`, `FLEET_INVERTER_TRIP`, `FLEET_SOC_SENSOR_DRIFT`, `FLEET_TELEMETRY_DELAY_BURST`,
  `FLEET_LEASE_LOSS`, `FLEET_CLOCK_SKEW`, `FLEET_TAMPERED_UNSIGNED_COMMAND`, `FLEET_RESERVE_FLOOR_PRESSURE`,
  `FLEET_FREQUENCY_DRIFT`, `FLEET_HARMONIC_INJECTION`, `FLEET_PHASE_IMBALANCE_INJECTION`,
  `FLEET_CALIBRATION_DRIFT_CORRECTABLE`, `FLEET_CALIBRATION_DRIFT_HARDWARE`, `FLEET_REPLACE_INVERTER`.
- **Market** (`MARKET_*`) and `PARTNER_CALL` are valid wire types, but the market simulator takes its anomalies
  over its HTTP admin API, not MQTT. Inject those through the simulator control plane (`ogsim.control`,
  `POST /api/inject`) instead; a `MARKET_*` scenario sent through this route is published but not acted on.

## Scenario route vs. the simulator control plane

| | `POST /og/api/scenario/{name}` | `ogsim.control` (`:8091`, `POST /api/inject`) |
|---|---|---|
| Who | An operator in the console | Test harnesses, demo scripts, the random-anomaly engine |
| Auth | Operator identity (see `auth-and-actions.md`) | None beyond loopback (it is a simulator) |
| Types | The wire type (`FLEET_HUB_OFFLINE`) | The catalogue id (`hub_offline`) |
| Market anomalies | Not acted on | Yes (HTTP admin API) |
| Audit | Traced and recorded as an operator action | Its own JSONL log |

Both paths end in the same simulator behaviour for SCADA and fleet types.
