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
`telemetry_interval_s` and one `<root>/ack/<hub_id>` per command verdict
(QoS 1, `ack.schema.json`) as `<root>/cmd/+/batch` messages arrive, and
subscribing to `<root>/cmd/+/batch`, `<root>/stop/#`, `<root>/lease/+`,
`<root>/scenario/cmd`.

`python -m ogsim.fleet --no-mqtt --hub-count 2000 --duration-s 10` runs a
standalone benchmark (no network) and logs elapsed wall/CPU time per tick.

`<root>/scenario/cmd` is parsed via `ogsim.common.scenario` against the one
shape `interfaces/mqtt/scenario_control.schema.json` defines (`ogsim.control`
is the only publisher and is owned by this same agent, so there is no wire-
shape mismatch to tolerate). A `<root>/stop/#` message is verified per
crypto.md §2.3 (`ogsim.fleet.stop.verify_stop_event`) before it reaches the
`StopRegistry`: only the safestop key may sign `action="ENGAGE"`, and
`action="RELEASE"` must be signed by the guardian key; a rejected event is
logged and dropped. `FleetConfig.public_key_path()`/`safestop_key_path()`
locate the guardian/safestop public keys respectively.

## How to test

```
.venv\Scripts\python.exe -m pytest integration-sims\tests -k fleet -q
```
