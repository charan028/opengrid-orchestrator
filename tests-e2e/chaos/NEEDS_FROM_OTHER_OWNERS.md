# Needs from other owners (found while building Q3; qa may not edit these paths)

Each item makes a chaos row or perf check fail against the real system today even when the product behaves
correctly. The tooling asserts the *specified* behaviour (BUILD.md, 02b S6.4/S6.5), not the current code.

## api (`orchestrator/src/opengrid/api/`)

1. **`GET /og/api/health` does not expose `degraded_modes`.** `routers/health.py _health_payload` returns
   `processes`, `feeds`, `hub_health_counts`, `alerts`, counters, KPIs -- no `degraded_modes`, although
   `health.model.HealthSnapshot.degraded_modes` exists and 02b S7.1 lists it. Every chaos row with a degraded mode
   (engine -> `HOLD_LOCAL_AUTONOMY`, guardian -> `HOLD`) fails with "`degraded_modes` missing from the health
   payload" until it is added as a JSON list of strings.
2. **`processes[x].status` is the raw `og.heartbeat.status` column, not the ok/down classification.**
   `store.PgStore.health_snapshot` selects `status` straight from the table and ignores its
   `heartbeat_miss_threshold_s` argument; every process writes `status="ok"` (`platform/heartbeat.py`), so a dead
   process keeps reading `ok` forever. Either derive `down` from `ts` age there (`health/rules.py
   classify_process_status` is the pure function to call) or have the evaluator write the classification back.
   Until then no "down within 30 s" check can pass.
3. Health `alerts[]` items carry `rule` -- good, the runner keys on it. Keep that field.

## health / settle (`orchestrator/src/opengrid/health/`, `orchestrator/src/opengrid/settle/`)

4. **`og-settle` heartbeats as `"og-settle"`** (`settle/main.py _PROCESS_NAME = "og-settle"`, plus
   `"og-settle-trace"`) while `health.model.ALL_PROCESSES` and the api key it as `"settle"`. The settle row can
   never read `ok`/recover under the spec'd key.
5. **`og-api` and the sims write no heartbeat** (`grep write_heartbeat` finds none under `api/` or
   `integration-sims/`), so `api` and `sim` can never be `ok` in `processes` -- `must_stay_ok` fails on every row.
6. `settle/main.py` does not appear to call `health.run`/`evaluate_once`, so the evaluator (hub health, alerts,
   degraded modes) is not actually running in `og-settle` yet. The `settle` chaos row assumes it is.

## engine / feeds / safestop / api metrics (`platform/metrics.py` consumers)

7. **Only `og-guardian` starts a `/metrics` server** (`guardian/main.py start_http_server(cfg.get(
   "metrics.guardian_port", 9103))`). `og_control_tick_duration_seconds`, `og_control_ticks_total`,
   `og_eventloop_lag_seconds`, `og_hubs`, `og_telemetry_fresh_ratio` are defined but not scrapeable, so
   `perf/capture.py` finds no histogram and fails "RT cycle p99". Each process needs the same two lines with its
   own `metrics.<process>_port`; the perf tooling assumes `engine=9102` by default and is configurable.
8. `[metrics]` in `orchestrator/config/orchestrator.toml` only has `bind_host`; `health.engine_metrics_url` /
   `health.guardian_metrics_url` / `health.metrics_scrape_timeout_s` that `health/__init__.py` reads are absent, so
   the health evaluator's cycle-latency and reserve-breach reads are silently disabled (architect owns the config).

## deploy / lead

9. `dev/docker-compose.yml` (WORKBOARD D0) does not exist. `DockerComposeController` assumes services named like
   the units (`og-engine`, ..., `postgresql`, `mosquitto`) and `docker compose -f <file> kill|start <service>`;
   templates are overridable, no code change needed if names differ.
10. `og-sim-fleet.service` is sized for 2,000 hubs (`MemoryMax=3G`, `deploy/README.md`); a 10k perf run needs the
    deploy role to raise it and the lead to set `hub_count` in `integration-sims/config/fleet.yaml`.
11. `[feeds.staleness] ercot_price_fresh_s = 600` means `NO_NEW_COMMITMENTS` appears 10 minutes after killing
    `og-feeds` -- outside any reasonable chaos window, so the feeds row documents it as *eventual* and does not
    assert it. If the lead wants it asserted on the server, lower the threshold for the run (architect's config).
