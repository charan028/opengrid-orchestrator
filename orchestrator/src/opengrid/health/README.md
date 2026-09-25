# opengrid.health

The health evaluator run inside `og-settle` (02b S6.4). Owner: health agent (BUILD.md S4), which also
owns `opengrid.trace.pg_backend` (the Postgres `TraceBackend`) and its retention pruning job -- see
`../trace/pg_backend.py`.

## Interface (fixed, `orchestrator/INTERFACES.md`)

- `evaluate_heartbeats() -> dict[str, ProcessStatus]` -- all 7 processes, `"down"` after
  `health.heartbeat_miss_threshold` missed `health.heartbeat_interval_s` beats.
- `evaluate_hub_health() -> None` -- classifies every hub online/stale/offline/fault from
  `hub_state.last_seen_at`/`fault_code` and writes the classification back onto `hub_state.health`.
- `evaluate_alerts() -> None` -- runs the MVP-S `ALR-*` rule set and raises/clears `og.alert` rows,
  de-duplicated by a stable `condition_key` (TS-07-06: no alert storm while a condition persists).

Plus the process entry point this build adds:

- `configure(pool, cfg) -> None` -- wires the DB pool and `HealthThresholds` (must be called once before
  any of the above).
- `evaluate_once() -> HealthSnapshot` -- runs one full cycle (hub health, alerts, heartbeats, degraded
  modes) and returns the read model `opengrid.api` exposes at `GET /og/api/health`.
- `run(pool, cfg, *, interval_s=None) -> None` -- `og-settle`'s process-loop entry, built on
  `opengrid.platform.process.run_forever`.

## Architecture

Pure logic (`rules.py`, `model.py`) has no I/O import; database reads/writes live in `queries.py`;
cross-process `/metrics` reads (cycle latency, guardian verdict timeout rate, reserve-breach counter --
each lives in a *different* OS process's own `prometheus_client` registry, 02b S6.6) live in
`metrics_scrape.py`. `__init__.py` only wires these together, mirroring the
`opengrid.ledger`/`opengrid.trace` "pure logic separated from I/O" split (BUILD.md S5a).

Degraded modes (02b S6.5) are derived from the same evaluation, exposed via `HealthSnapshot.degraded_modes`
for the engine to read.

## How to test

```
.venv\Scripts\python.exe -m pytest orchestrator\tests\unit\health -q
```

Integration tests against real Postgres (`tests/integration/health/test_health_integration.py`) prove
hub-health classification and alert raise/clear round-trip through the real schema; they auto-skip
without a database and are meant to run via:

```
powershell -File tools\remote.ps1 -Ws hlth -Cmd "cd orchestrator && bash tools/check.sh"
```
