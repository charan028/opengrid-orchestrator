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

## Canonical series keys (must match what `feeds` actually writes to `feed_obs`)

`ForecastKind` is currently `"price" | "load"` only -- what MVP-S's selector actually consumes (02a
S3.2's `v^E_{b,t,omega}`, one ERCOT load-zone price feeding every bank). `feeds.normalize` (source of
truth: `orchestrator/src/opengrid/feeds/normalize.py`) also writes wind/solar and NWS weather series to
`feed_obs` under their own `product`/`series` pairs; they are not yet a `ForecastKind` this module
scenario-izes for the selector (see "Known gap" below) -- the UI agent should still read them directly
from `feed_obs` for display, using the keys below.

| `feed_obs.product` | `feed_obs.series` | Meaning | Unit | `forecast` kind |
|---|---|---|---|---|
| `np6-905-cd` | one of `[fleet].zones` (e.g. `LZ_NORTH`) | ERCOT settlement point price (queried with `settlementPointType=LZ` -- a **load-zone** code, never a hub code like `HB_HUBAVG`) | `usd_per_mwh` | `price` |
| `np6-345-cd` | one of `[fleet].zones` (e.g. `LZ_NORTH`) | ERCOT actual system load by weather zone | `mw` | `load` |
| `np4-732-cd` | `actual` \| `forecast` | ERCOT system-wide wind output (no zonal breakdown) | `mw` | not modeled yet (gap) |
| `np4-737-cd` | `actual` \| `forecast` | ERCOT system-wide solar output (no zonal breakdown) | `mw` | not modeled yet (gap) |
| `np4-188-cd` | ancillary product code (e.g. `NSPIN`, `RRS`, `ECRS`) | Day-ahead AS clearing price | `usd_per_mwh` | not modeled yet (gap) |
| `eia-demand` | `ERCOT_SYSTEM` | EIA standby system load (quality `ESTIMATED`, used only when ERCOT load is stale) | `mw` | `load` (fallback source, same series semantics) |
| `nws-hourly` | `temperature` \| `dewpoint` \| `sky_cover` | NWS hourly forecast | `degc` \| `degc` \| `pct` | not modeled (weather, not price/load) |

`price_series`/`load_series` (`[forecast]` config, `service.py`) both default to `[fleet].zones` when
unset -- **not** a hub code -- because that is the only series key the LZ-scoped price/load feeds ever
actually populate (this was previously a bug: the default was `HB_HUBAVG`, a settlement point
`np6-905-cd` never returns under `settlementPointType=LZ`, so no price forecast was ever produced from
the real feed under defaults). Any deployment that widens `np6-905-cd`'s query to include hub-type
settlement points, or wants a distinct price/load series set, must set `[forecast].price_series`/
`load_series` explicitly.

**Known gap:** wind, solar and AS-price series have no `ForecastKind`/scenario path into the selector
yet (02a S3.2 only calls for the one price series per bank for MVP-S's 5 services). Widening
`ForecastKind` to include them is future work, not part of this fix; the UI agent should read
`np4-732-cd`/`np4-737-cd`/`np4-188-cd` rows directly from `feed_obs` (via whatever `feeds`/`api` query
surface exists) rather than through `forecast.scenarios()`.

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
