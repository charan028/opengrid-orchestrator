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

## Confirmed on a local live stack (2026-09-25, Postgres + Mosquitto + all 10 processes)

8. **SCADA bank load is never persisted** (owner: engine/fleet). `ogsim.scada` publishes `APPARENT_POWER_KVA`
   (an injected `bank_overload` on bank-003 read 123 kVA against a 75 kVA rating, plus an auto `LIMIT`
   instruction), the engine subscribes to `<root>/scada/#`, but nothing writes `og.feed_obs` rows with
   `source='scada'`. Both `opengrid.health.queries` (ALR-SCADA-OVERLOAD) and `opengrid.guardian.repo`
   (bank kVA check) read exactly those rows, so the overload alert can never fire and the guardian's kVA
   check always sees "no reading". Items 1-5 above (health evaluator never runs, heartbeat key mismatch,
   `processes[x].status` never `down`) were also confirmed live: `GET /og/api/health` listed `og-settle`
   and never `api`/`sim`, and `og.alert` stayed empty through the overload.
9. **`og-engine` serves no `/metrics`** (item 6) confirmed: `tests-e2e/perf/capture.py` against the live
   stack reports "no histogram found"; only `og-guardian:9103` answers.

## Live feeds against the real APIs (2026-09-26 04:xx Z, real EIA + ERCOT subscription keys)

10. **`og-feeds` cannot ingest the real NWS hourly forecast** (owner: feeds). Two parser assumptions no
    longer hold against `api.weather.gov`: `properties.updated` is now `updateTime`/`generatedAt`, and
    hourly periods carry no `skyCover`. The first raised a KeyError that aborted every feeds tick and
    re-polled NWS every cycle (the poll timer never advanced); the second rejected every period as
    malformed. Fix with tests on branch **`hotfix/feeds-nws-updatetime`** (2 commits on top of `main`,
    ready to cherry-pick); with it applied, real NWS temperature/dewpoint rows land within one tick.
11. **ERCOT public API needs the account username/password** (`ERCOT_API_USER`/`ERCOT_API_PASSWORD`,
    ROPC token flow), not only the subscription keys; the token endpoint returns 400 without them. The
    storage-API keys are not used anywhere in this code.
12. **EIA is a fallback only**, polled when `np6-345-cd`'s own hourly poll comes due while the ERCOT
    breaker is open, so on a fresh start EIA data appears about an hour after ERCOT starts failing. The
    EIA client itself works with the real key (24 hourly ERCOT demand rows, verified). For a demo
    without ERCOT credentials that is a long wait; consider polling EIA on its own schedule.


## Status after main @ c5eda88 (lead's dispatch-live pass, 2026-09-26)

- **Resolved upstream:** item 3 (settle heartbeat key is now `settle`), item 5 (`og-settle` runs
  `health.run`), item 8 (SCADA readings are persisted to `og.feed_obs`, `ALR-SCADA-OVERLOAD` verified live
  by the lead), and the `dev/` stack exists, so `runner.py --backend docker` is now runnable.
- **Still open:** item 1 (`degraded_modes` missing from `GET /og/api/health`), item 2 (`processes[x].status`
  never `down`), item 4 (api and sims write no heartbeat), item 6/9 (engine serves no `/metrics`), items
  10-12 (feeds: NWS parser, hotfix branch `hotfix/feeds-nws-updatetime` rebased on this main; ERCOT ROPC
  credentials; EIA fallback latency), and the G-14 pre-image veto (qa/merge-notes.md section 17 confirms
  it; docs/demo/NEEDS_FROM_OTHER_OWNERS.md item 10 names the cause).
13. **`ALR-FEED-STALE` fires for every feed with "stale for over 15s"** (owner: health, seen live on main
    c5eda88 within a minute of start-up): the evaluator is comparing `feed_status.last_value_at` against
    the 15 s heartbeat window instead of each product's `[feeds.staleness]` threshold (600 s for prices,
    hours for day-ahead products). Combined with item 6 in tests-e2e/ui/NEEDS_FROM_OTHER_OWNERS.md
    (`last_value_at` is a data timestamp), the rule is wrong in both directions.
