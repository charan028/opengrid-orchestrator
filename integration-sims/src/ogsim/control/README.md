# ogsim.control

The single control plane for every OpenGrid integration simulator (market,
fleet, SCADA). Runs as a FastAPI app on port 8091 (`python -m ogsim.control`,
or `python -m ogsim.control serve`).

## Anomaly catalogue

`catalogue.py` is the one registry covering the full BUILD.md §3 catalogue -
market, SCADA and fleet anomaly types, each with an id, owning simulator,
target kind, param schema and description. `owner_of(type)` routes an
injection: `market` types go to `ogsim.market`'s admin HTTP API
(`market_client.py`); `scada`/`fleet` types are published as MQTT messages
(`mqtt_pub.py`) to `<root>/scenario/cmd` for the fleet/SCADA sim to execute -
this control plane contains no fleet/SCADA *behaviour* itself.

## Injection sources

Every anomaly - however it started - goes through
`injector.Injector.inject(..., source=...)`, one of:

- **manual** - REST (`POST /api/inject`), the web UI, or the CLI.
- **scenario** - `scenarios.py`'s YAML runner (see `../../scenarios/*.yaml`).
- **random** - the autonomous engine (`random_engine.py`), driven by
  `config/random.yaml` (`random_config.py`).

All three share one active-anomaly registry (so the random engine's
max-concurrency cap sees manual/scenario anomalies too) and one JSONL log
(`log.py`), each entry carrying `id, source, target, type, params, start,
end`.

## Autonomous random mode

Each enabled `(type, owning sim)` runs its own Poisson-arrival loop
(`rate_per_hour`, scaled by the active intensity profile:
calm/normal/stressed/chaos). An arrival's duration and numeric params are
drawn uniformly from configured ranges. A global `max_concurrent` cap drops
(does not queue) arrivals once reached. Everything is controllable at
runtime:

- REST: `GET /api/random/status`, `POST /api/random/{pause,resume}`,
  `POST /api/random/profile`, `POST /api/random/sims/{sim}`.
- CLI: `python -m ogsim.control random {status,pause,resume,set-profile,set-sim}`.
- Web UI: the "Autonomous random mode" panel (pause-all switch, profile
  selector, per-sim toggles).

All randomness runs through one seeded `numpy.random.Generator`
(`random.yaml`'s `seed`), so a run is reproducible. The Poisson-arrival math
(`plan_arrivals`) and per-arrival sampling are pure functions with no
wall-clock dependency, so they're unit-tested directly rather than by
sleeping in real time.

## Scenario runner

`scenarios.py` loads `../../scenarios/*.yaml` (timed sequences of anomaly
injections: `{at_s, type, target, params, duration}`) and injects each step
at its scheduled offset (optionally sped up via `speed`).

## Web UI

`templates/index.html` - one server-rendered page with a little vanilla JS:
inject an anomaly, run a scenario, control random mode, and watch active
anomalies and the log update every few seconds. No build step.
