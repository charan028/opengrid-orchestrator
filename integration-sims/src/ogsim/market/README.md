# ogsim.market

Simulates the three external data sources the orchestrator's `feeds` process
polls: ERCOT Public API, EIA v2, and NWS. Runs as a FastAPI app on port 8090
(`python -m ogsim.market`).

## Endpoints

- **ERCOT**: `POST /token` (Azure AD B2C-shaped ROPC token, accepts any
  configured test user, 1h expiry) and the five products:
  `np6-905-cd/spp_node_zone_hub`, `np6-345-cd/act_sys_load_by_wzn`,
  `np4-732-cd/wpp_hrly_avrg_actl_fcast`, `np4-737-cd/spp_hrly_avrg_actl_fcast`,
  `np4-188-cd/dam_clear_price_for_cap`, all under `/api/public-reports/`,
  paginated in ERCOT's real `{_meta, fields, data}` shape and gated by the
  `Ocp-Apim-Subscription-Key` header (primary/secondary, configurable).
- **EIA**: `GET /v2/electricity/rto/region-data/data` (requires `api_key`).
- **NWS**: `GET /points/{lat},{lon}` and
  `GET /gridpoints/{office}/{x},{y}/forecast/hourly` (both require a
  non-empty `User-Agent`).
- **Admin** (internal, used by `ogsim.control`): `GET/POST /admin/anomalies`,
  `DELETE /admin/anomalies/{id}`, `GET /admin/healthz`.

## Data modes

Set `OGSIM_MARKET_DATA_MODE=synthetic` (default) or `replay`:

- **synthetic** (`synthetic.py`): diurnal price/load/wind/solar/AS curves
  with seeded noise (`OGSIM_MARKET_SEED`).
- **replay** (`replay.py`): loops the 2.5 days of
  `/var/lib/opengrid/import/mariadb_history_signals.tsv`
  (`wholesale_price_mwh` -> SPP hub prices, `substation_load_kw` scaled ->
  zone load), time-shifted so "now" always falls somewhere in the recorded
  window. Falls back to synthetic per-value if the file is missing or a
  requested signal isn't in it.

## Anomalies

Applied per product (`target`) or globally (`target: "*"`) via the admin API;
see `ogsim.control.catalogue` for the full list and param schemas. Handled
here: `price_spike`, `negative_price`, `as_price_jump` (override the next
value), `http_5xx`/`http_429`/`http_401_primary` (only rejects the *primary*
subscription key, to exercise key rotation)/`malformed_payload`/
`slow_response` (all short-circuit the HTTP response before data
generation), `stale_posting` (freezes the simulated clock for that product),
and `nws_extreme_weather` (overrides every forecast period).

## Key modules

- `config.py` - all settings read from `OGSIM_MARKET_*` env vars.
- `anomalies.py` - the in-process `AnomalyStore` (thread-safe, time-windowed).
- `data.py` - builds every response payload; pure functions of `(now, ...)`.
- `security.py` - test-user token issuance and subscription-key checking.
- `runtime.py` - wires config/anomalies/data together; injectable clock.
