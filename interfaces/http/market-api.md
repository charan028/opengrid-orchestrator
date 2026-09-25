# HTTP market/data API surface the orchestrator consumes

Authoritative source: `05-integrations-guide.md`, `02b-mvp-s-spec-platform.md` §2 (`feeds`). This is the
subset of ERCOT Public API / EIA v2 / NWS shapes `opengrid.feeds` actually calls, so `ogsim.market` (the
market simulator) can mimic them byte-for-byte on `http://127.0.0.1:8090` in test config
(`orchestrator/config/test.toml`).

## 1. ERCOT Public API

Base URL (prod): `https://api.ercot.com/api/public-reports`. Base URL (sim): `http://127.0.0.1:8090/ercot`.

Auth headers on every request:
```
Authorization: Bearer <token from Azure AD B2C password grant>
Ocp-Apim-Subscription-Key: <ERCOT_PUBLIC_API_KEY_PRIMARY|SECONDARY>
```
The simulator does not need to validate the bearer token's cryptographic content for MVP-S; it accepts any
non-empty `Authorization` header and may honor a scenario-injected `MARKET_401_KEY_REJECT` to return 401.

| Product | Path | Query params | Poll |
|---|---|---|---|
| NP6-905-CD | `GET /np6-905-cd/spp_node_zone_hub` | `settlementPointType=LZ`, `deliveryDateFrom`, `deliveryDateTo` (ISO date, optional; default = latest) | 5 min |
| NP6-345-CD | `GET /np6-345-cd/act_sys_load_by_wzn` | `operatingDateFrom`, `operatingDateTo` (optional) | hourly |
| NP4-732-CD | `GET /np4-732-cd/wpp_hrly_avrg_actl_fcast` | none required; optional `postedDatetimeFrom/To` | 30 min |
| NP4-737-CD | `GET /np4-737-cd/spp_hrly_avrg_actl_fcast` | none required; optional `postedDatetimeFrom/To` | 30 min |
| NP4-188-CD | `GET /np4-188-cd/dam_clear_price_for_cap` | `deliveryDateFrom`, `deliveryDateTo` (optional) | daily 14:00 CT |

### Response envelope (all five products share this shape)

```json
{
  "data": [
    ["2026-09-26", "2026-09-26T18:00:00", "LZ_NORTH", "42.17"],
    ["2026-09-26", "2026-09-26T18:00:00", "LZ_SOUTH", "41.85"]
  ],
  "fields": [
    {"name": "deliveryDate", "dataType": "DATE"},
    {"name": "deliveryDateTime", "dataType": "TIMESTAMP"},
    {"name": "settlementPoint", "dataType": "STRING"},
    {"name": "settlementPointPrice", "dataType": "STRING"}
  ],
  "_meta": {"totalRecords": 2, "pageSize": 1000, "totalPages": 1, "currentPage": 1}
}
```

`opengrid.feeds` reads `fields[i].name` to map each row's positional array into a dict, so the simulator
must keep `fields` consistent with `data`'s column order for the product being served. Field-name mapping
per product (only the columns `feeds.ercot.normalize` actually consumes):

| Product | Columns consumed |
|---|---|
| NP6-905-CD | `deliveryDateTime`, `settlementPoint`, `settlementPointPrice` ($/MWh) |
| NP6-345-CD | `operatingDateTime` (or `hourEnding` + `operatingDate`), `weatherZone`, `load` (MW) |
| NP4-732-CD | `postedDatetime`, `actualSystemWideWindOutput` (MW), `windOutputForecastSystemWide` (MW) |
| NP4-737-CD | `postedDatetime`, `actualSystemWideSolarOutput` (MW), `solarOutputForecastSystemWide` (MW) |
| NP4-188-CD | `deliveryDate`, `hourEnding`, `ancillaryType` (`REGUP`/`REGDN`/`RRS`/`NSPIN`/`ECRS`), `mcpc` ($/MW-h) |

### Error/edge behavior the simulator should reproduce on request (scenario injection)

- `401`/`403` — expired/invalid bearer or subscription key (`MARKET_401_KEY_REJECT`); triggers `feeds`'
  key-rotation path (§1.5 of `02b`).
- `429` with `Retry-After: <seconds>` — quota exhaustion (`MARKET_429_THROTTLE`).
- `5xx` — outage (`MARKET_HTTP_5XX`); `feeds` retries GETs up to 2x with exponential backoff, then opens its
  circuit breaker after 5 consecutive failures.
- Slow response (`MARKET_SLOW_RESPONSE`) — delay the response body beyond `feeds`' client timeout (default
  10s) to exercise timeout handling.
- Malformed payload (`MARKET_MALFORMED_PAYLOAD`) — omit `fields`, or emit a `data` row with wrong arity.
- Stale posting (`MARKET_STALE_POSTING`) — keep returning the same `deliveryDateTime` past the product's
  staleness threshold (`feeds.staleness.*` in `orchestrator.toml`) so `feed_status` flips to `STALE`.

## 2. EIA Open Data v2 (standby fallback for system load)

Base URL (prod): `https://api.eia.gov/v2`. Base URL (sim): `http://127.0.0.1:8090/eia`.

```
GET /electricity/rto/region-data/data/
    ?api_key=<EIA_API_KEY>
    &frequency=hourly
    &data[]=value
    &facets[respondent][]=ERCO
    &facets[type][]=D
    &sort[0][column]=period&sort[0][direction]=desc
    &offset=0&length=24
```

Response shape:

```json
{
  "response": {
    "total": "24",
    "data": [
      {"period": "2026-09-26T18", "respondent": "ERCO", "type": "D", "type-name": "Demand", "value": "52104", "value-units": "megawatthours"}
    ]
  },
  "request": {"command": "/v2/electricity/rto/region-data/data/", "params": {}}
}
```

`feeds` stores EIA-sourced load with `quality="ESTIMATED"` (never mistaken for ERCOT data). The simulator
should reject requests missing `api_key` with `403` to exercise the same key-check path.

## 3. NWS api.weather.gov

Base URL (prod): `https://api.weather.gov`. Base URL (sim): `http://127.0.0.1:8090/nws`. No API key; a
`User-Agent` header naming the app and a contact address is required (`feeds.nws.user_agent` config); the
simulator may return `400` if `User-Agent` is absent, to exercise that guard.

### Grid point resolution (once at startup)

```
GET /points/{lat},{lon}
->
{
  "properties": {
    "gridId": "EWX", "gridX": 156, "gridY": 91,
    "forecastHourly": "https://api.weather.gov/gridpoints/EWX/156,91/forecast/hourly"
  }
}
```

### Hourly forecast (polled hourly, with `If-Modified-Since`)

```
GET /gridpoints/{office}/{x},{y}/forecast/hourly
->
{
  "properties": {
    "updated": "2026-09-26T17:00:00+00:00",
    "periods": [
      {
        "number": 1,
        "startTime": "2026-09-26T18:00:00-05:00",
        "endTime": "2026-09-26T19:00:00-05:00",
        "temperature": 91,
        "temperatureUnit": "F",
        "dewpoint": {"unitCode": "wmoUnit:degC", "value": 21.1},
        "skyCover": 20,
        "shortForecast": "Sunny"
      }
    ]
  }
}
```

A `304 Not Modified` response (matching `If-Modified-Since`) means `feeds` keeps its cached value and only
updates the age counter. `MARKET_NWS_EXTREME_WEATHER` scenario injection overrides `temperature`/
`shortForecast` to extreme values without changing `updated`, to test that `feeds` still treats it as fresh
data (not stale) while forecast/demand react to the extreme reading.

## 4. Consistency note

Both sides (orchestrator and `ogsim.market`) validate against this document, not against each other's code.
If ERCOT/EIA/NWS change a real field name, this file and `orchestrator/src/opengrid/feeds/` are updated
together; `ogsim` is updated separately to match. Neither package imports the other (BUILD.md §1).
