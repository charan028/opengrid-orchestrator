# ogsim.fleet

Simulates the hub fleet (02b §4 fleet twin, §5 sim harness): 2,000 (default,
up to 10,000) hubs across ~40 banks / 4 zones, each with vectorized SoC
physics, home load, command verification, lease/local-autonomy, retained
stop handling, and the FLEET_* anomaly catalogue.

## Purpose

- `physics.py` -- vectorized SoC step + reserve/P/E-aware setpoint clipping
  (02b §4.2).
- `household.py` -- diurnal home load + optional PV.
- `state.py` -- struct-of-arrays fleet state (`FleetState`).
- `commands.py` -- `command_batch` signature + epoch/seq/issued_at/expires_at
  verification (pure, no MQTT).
- `lease.py` -- lease expiry -> brief hold -> local autonomy.
- `stop.py` -- retained stop registry + ramp-to-zero.
- `anomalies.py` -- FLEET_* anomaly apply/revert.
- `runtime.py` -- `FleetEngine` (pure per-tick logic) + `run_fleet` (async
  MQTT shell).

## Interface

`python -m ogsim.fleet` connects to MQTT (`OG_MQTT_HOST`/`OG_MQTT_PORT`,
user `og_sim`/`OG_MQTT_SIM_PASSWORD`, root `OG_MQTT_ROOT`) and runs
forever, publishing `<root>/tel/<zone>/<bank>/<hub>` every
`telemetry_interval_s`, subscribing to `<root>/cmd/+/batch`,
`<root>/stop/#`, `<root>/lease/+`, `<root>/scenario/cmd`.

`python -m ogsim.fleet --no-mqtt --hub-count 2000 --duration-s 10` runs a
standalone benchmark (no network) and logs elapsed wall/CPU time per tick.

**Known wire-shape compromise**: `<root>/scenario/cmd` is parsed tolerantly
via `ogsim.common.scenario` because `ogsim.control.mqtt_pub` currently
publishes a legacy flat shape, not `scenario_control.schema.json`'s shape.
See that module's docstring; flagged for the merge agent.

## How to test

```
.venv\Scripts\python.exe -m pytest integration-sims\tests -k fleet -q
```
