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
    ["2026-09-26", 1, 1, "LZ_NORTH", "LZ", 42.17, false],
    ["2026-09-26", 1, 1, "LZ_SOUTH", "LZ", 41.85, false]
  ],
  "fields": [
    {"name": "deliveryDate", "dataType": "DATE"},
    {"name": "deliveryHour", "dataType": "INTEGER"},
    {"name": "deliveryInterval", "dataType": "INTEGER"},
    {"name": "settlementPoint", "dataType": "STRING"},
    {"name": "settlementPointType", "dataType": "STRING"},
    {"name": "settlementPointPrice", "dataType": "NUMBER"},
    {"name": "DSTFlag", "dataType": "BOOLEAN"}
  ],
  "_meta": {"totalRecords": 2, "pageSize": 1000, "totalPages": 1, "currentPage": 1}
}
```

**Corrected against a live call (BUILD.md follow-up finding)**: the shape above (NP6-905-CD) replaces an
earlier, incorrect assumption of a single ready-made `deliveryDateTime` column. `opengrid.feeds` reads
`fields[i].name` to map each row's positional array into a dict, so the simulator must keep `fields`
consistent with `data`'s column order for the product being served.

**Timestamps are America/Chicago local wall-clock, never UTC and never carrying their own offset.**
Every product's delivery interval is `deliveryDate`/`operatingDay` + an hour-ending value (`deliveryHour`
or `hourEnding`, 1-24; `hourEnding` is formatted `"HH:MM"` on some products, a bare integer on others --
see the table) + an optional 15-minute `deliveryInterval` (1-4) within that hour, localized to UTC using
`DSTFlag`. `DSTFlag` is `true` only for the one repeated wall-clock hour on the fall-back transition day
(it is not a general "currently observing DST" indicator) -- `opengrid.feeds.normalize` maps it directly
onto Python's `datetime.fold` to disambiguate that one ambiguous hour; every other row's UTC offset comes
from ordinary America/Chicago calendar rules.

Field-name mapping per product (only the columns `feeds.ercot.normalize` actually consumes; columns
marked "wide" mean one column per named series, not a single generic column):

| Product | Columns consumed |
|---|---|
| NP6-905-CD | `deliveryDate`, `deliveryHour` (int, hour-ending), `deliveryInterval` (int 1-4), `DSTFlag`, `settlementPoint`, `settlementPointPrice` ($/MWh) |
| NP6-345-CD | `operatingDay`, `hourEnding` (`"HH:MM"` string, hour-ending), `DSTFlag`, then one column per weather zone (wide): `coast`, `east`, `farWest`, `north`, `northC`, `southern`, `southC`, `west`, `total` (MW; each becomes its own `series`, `null` values skipped) |
| NP4-732-CD | `deliveryDate`, `hourEnding` (int, hour-ending), `DSTFlag`, `genSystemWide` (actual MW, `null` for a future delivery hour), `STWPFSystemWide` (Short-Term Wind Power Forecast, MW) |
| NP4-737-CD | `deliveryDate`, `hourEnding` (int, hour-ending), `DSTFlag`, `genSystemWide` (actual MW, `null` for a future delivery hour), `STPPFSystemWide` (Short-Term Photovoltaic Power Forecast, MW) |
| NP4-188-CD | `deliveryDate`, `hourEnding` (`"HH:MM"` string, hour-ending), `DSTFlag`, `ancillaryType` (`REGUP`/`REGDN`/`RRS`/`NSPIN`/`ECRS`), `MCPC` ($/MW-h) |

**Query params confirmed live** (differ from the table in S1 above for these four products, which
otherwise return an empty `data` array with no date filter -- NP6-905-CD is the only one of the five that
defaults to "latest" with no params): NP6-345-CD takes `operatingDayFrom`/`operatingDayTo` (NOT
`operatingDateFrom`/`operatingDateTo` -- the API rejects those names with a 400), NP4-732-CD/NP4-737-CD
take `postedDatetimeFrom`/`postedDatetimeTo`, NP4-188-CD takes `deliveryDateFrom`/`deliveryDateTo`.
`opengrid.feeds.ercot` always sends a 1-day lookback window for these four products (`og-feeds` polls
frequently enough that this always includes the latest posting).

**Market simulator alignment**: `ogsim.market` was built to the original (incorrect) field-name
assumptions above; it needs updating to serve the real shapes in this table, including NP6-345-CD's wide
per-zone format and NP4-732-CD/NP4-737-CD's `genSystemWide`/`STWPFSystemWide`/`STPPFSystemWide` names, and
to honor the corrected query-param names for those four products. Routed to the `market`/`sims` agents by
the lead; this file and `opengrid.feeds` are the source of truth for what changed.

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
