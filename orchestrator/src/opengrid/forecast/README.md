# opengrid.forecast

Quantile-persistence forecaster (`02b-mvp-s-spec-platform.md` S3). Produces P10/P50/P90 scenarios for
price ($/MWh) and load (MW) over a 24h/15-min horizon (96 steps), persisted to `og.forecast`
(`migrations/0003_forecast_table.sql`), and consumed by `selector.run_gate` (02a S3) as its 3-scenario
input.

## Method

For each 15-min target slot: pool historical values at the same time-of-day and day-type
(weekday/weekend) over up to the last 14 days. If that pool has fewer than 3 samples (expected while
history is short, e.g. the 2.5-day `HIST` import), fall back to a diurnal profile fit from all
available data plus the spread of that profile's residuals, and flag `NOT_FOR_FIRM`. If the series'
live feed is currently `STALE` (per `FeedObs.quality`, computed by `feeds`), widen the P10/P90 band by
1.3x around P50 and flag `NOT_FOR_FIRM` regardless of sample count.

## Interface

- `scenarios(horizon_start, horizon_end) -> list[ScenarioPoint]` -- fixed public interface
  (`INTERFACES.md`). Reads persisted `og.forecast` rows only.
- `configure(cfg, *, history, backend)` -- wiring hook the host process's `main.py` calls once at
  startup with a `HistoryProvider` (structurally `opengrid.feeds`) and a `ForecastBackend`
  (`PgForecastBackend`).
- `run_forecast_cycle(now=None) -> list[ForecastRow]` -- recompute + persist the full horizon; call on
  a 15-min cadence and on a fresh feed batch (02b S3).

Pure math (`quantiles.py`) takes no I/O and is unit/property-tested directly; `service.py` orchestrates
it against `HistoryProvider`/`ForecastBackend` `Protocol`s, so tests use fakes for both instead of a
database (`opengrid.feeds` and Postgres are otherwise required to exercise this module for real).

## Testing

```
.venv\Scripts\python.exe -m pytest orchestrator\tests\unit\forecast -q
```

Integration (Postgres, workspace `fcst`):

```
powershell -File tools\remote.ps1 -Ws fcst -Cmd "cd orchestrator && python -m opengrid.platform.db migrate && python -m pytest tests/integration/forecast -q"
```
