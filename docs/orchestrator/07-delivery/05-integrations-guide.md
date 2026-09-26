# OpenGrid Orchestrator — Integrations Guide

As of 2026-09-25

The orchestrator's first release (MVP-S) connects to three external data sources (ERCOT, EIA, NWS) and to the battery
fleet over MQTT. This guide lists each integration and gives a configuration template with placeholders only. The
template is also available as files: [api_keys.env.example](integrations/api_keys.env.example) and
[orchestrator.integrations.toml.example](integrations/orchestrator.integrations.toml.example).

## Integrations at a glance

Four integrations are live in MVP-S. Everything else is reserved for later releases (last section).

| Integration | Direction | Protocol | Auth | Cadence | Used for |
| --- | --- | --- | --- | --- | --- |
| ERCOT Public API | Inbound | HTTPS REST (JSON) | Azure AD B2C token + subscription key | 5 min to daily, per product | Prices, load, wind/solar, AS prices |
| EIA Open Data v2 | Inbound (standby) | HTTPS REST | API key | Only when ERCOT load is unavailable | Fallback system load |
| NWS api.weather.gov | Inbound | HTTPS REST | None (User-Agent required) | Hourly | Temperature, dew point, sky cover |
| Fleet devices (hubs) | Both ways | MQTT 3.1.1/5 | Username/password + ACL; commands Ed25519-signed | Telemetry every 2 s | Monitoring and control |

All inbound data lands in one table (`feed_obs`) with a quality flag (GOOD, ESTIMATED, STALE) and its age. A stale
source is therefore always visible and never silently used.

## ERCOT Public API

Five ERCOT products feed the forecast and the dispatch engine. Steady-state use is about 18 calls per hour, against a
self-imposed budget of 24 per minute (80% of ERCOT's 30).

| Product | Endpoint | Data | Poll |
| --- | --- | --- | --- |
| NP6-905-CD | `/np6-905-cd/spp_node_zone_hub` | Real-time settlement point prices, $/MWh, per hub | Every 5 min |
| NP6-345-CD | `/np6-345-cd/act_sys_load_by_wzn` | Actual load by weather zone, MW | Hourly |
| NP4-732-CD | `/np4-732-cd/wpp_hrly_avrg_actl_fcast` | Wind actual and forecast, MW | Every 30 min |
| NP4-737-CD | `/np4-737-cd/spp_hrly_avrg_actl_fcast` | Solar actual and forecast, MW | Every 30 min |
| NP4-188-CD | `/np4-188-cd/dam_clear_price_for_cap` | Day-ahead AS clearing prices, $/MW-h (RegUp, RegDown, RRS, NonSpin, ECRS) | Daily, 14:00 CT |

**Authentication.** Every call needs two credentials:

- a bearer token from ERCOT's Azure AD B2C sign-in (password grant, 1-hour lifetime, renewed 5 minutes early);
- the `Ocp-Apim-Subscription-Key` header.

To get the key, register at [apiexplorer.ercot.com](https://apiexplorer.ercot.com) and subscribe to the Public API
product.

**Key rotation.** The primary subscription key is used first. On HTTP 401/403 or quota exhaustion, the service retries
once with the secondary key and keeps using it until restart. Each rotation is written to the audit trace.

**Resilience.** Retries apply to GETs only: exponential backoff, at most 2 retries, honouring `Retry-After`. A circuit
breaker opens after 5 consecutive failures, and the last good value keeps serving with its age shown.

## EIA and NWS

**EIA Open Data v2 (standby).** When ERCOT load data is unavailable, the service calls
`GET https://api.eia.gov/v2/electricity/rto/region-data/data/` with `facets[respondent][]=ERCO` and
`facets[type][]=D` (hourly demand). Results are stored with quality `ESTIMATED`, so they are never mistaken for ERCOT
data. A free key is available at [eia.gov/opendata/register.php](https://www.eia.gov/opendata/register.php).

**NWS (api.weather.gov).** At startup the service resolves the forecast grid point once (`GET /points/{lat},{lon}`).
It then polls `GET /gridpoints/{office}/{x},{y}/forecast/hourly` every hour with `If-Modified-Since`. No key is
needed, but NWS rejects requests without a `User-Agent` that names the application and a contact address.

## Device interface (MQTT)

Hubs connect to the broker under the topic root `og/v1/`. Each client has its own user and may only publish or
subscribe to the topics listed for it. A hub executes a command only if the guardian's Ed25519 signature is valid and
the sequence, epoch and lease are current.

| Topic | Direction | QoS | Retained | Publisher |
| --- | --- | --- | --- | --- |
| `og/v1/tel/<zone>/<bank>/<hub>` | Hub → orchestrator | 0 | No | Hub (every 2 s) |
| `og/v1/ack/<hub>` | Hub → orchestrator | 1 | No | Hub |
| `og/v1/cmd/<bank>/batch` | Orchestrator → hubs | 1 | No | Guardian (signed) |
| `og/v1/lease/<hub>` | Orchestrator → hub | 1 | Yes | Guardian |
| `og/v1/stop/<scope>/<id>` | Orchestrator → hubs | 1 | Yes | Safe-stop service |
| `og/v1/scada/<bank>` | SCADA → orchestrator | 0 | No | Utility SCADA (simulated in MVP-S) |

A hub that loses its lease (30 s) holds its last signed setpoint, then falls back to local autonomy. A retained stop
message reaches hubs even if they reconnect later.

## Configuration template

Configuration is split in two:

- secrets in `/etc/opengrid/api_keys.env` (mode 640, never committed);
- non-secret settings in `config/orchestrator.toml`.

Replace every `<...>` placeholder. Settings refer to secrets by variable name only. **Always single-quote values
in the secrets file.** Passwords often contain shell-special characters that bash would alter; single quotes are
read literally by both bash and systemd.

**ERCOT token request:** POST form fields `username`, `password`, `grant_type=password`,
`client_id=fec253ea-0d06-4272-a5e6-b478baeecd70`, `scope=openid <client_id> offline_access` and
**`response_type=id_token`**. Use the returned **`id_token`** as the bearer token. `response_type=token` is rejected
with `400 invalid_grant`.

**`/etc/opengrid/api_keys.env`**

```
# --- ERCOT Public API (https://apiexplorer.ercot.com)
ERCOT_API_USER=<account email>
ERCOT_API_PASSWORD=<account password>
ERCOT_PUBLIC_API_KEY_PRIMARY=<Public API subscription key, primary>
ERCOT_PUBLIC_API_KEY_SECONDARY=<Public API subscription key, secondary>

# --- EIA Open Data v2 (https://www.eia.gov/opendata/register.php)
EIA_API_KEY=<EIA API key>

# --- Reserved for later releases (leave blank if not provisioned)
ERCOT_STORAGE_API_KEY_PRIMARY=
ERCOT_STORAGE_API_KEY_SECONDARY=
MISO_API_USER=
MISO_API_PASSWORD=
```

**`config/orchestrator.toml` (integration settings)**

```toml
[feeds.ercot]
username_env = "ERCOT_API_USER"
password_env = "ERCOT_API_PASSWORD"
subscription_key_env = "ERCOT_PUBLIC_API_KEY_PRIMARY"   # falls back to ..._SECONDARY
budget_requests_per_min = 24
products = ["np6-905-cd", "np6-345-cd", "np4-732-cd", "np4-737-cd", "np4-188-cd"]

[feeds.eia]
api_key_env = "EIA_API_KEY"
respondent = "ERCO"

[feeds.nws]
user_agent = "OpenGrid-Orchestrator (<contact email>)"
latitude = <lat>
longitude = <lon>

[feeds.staleness]            # seconds until a series is flagged STALE
ercot_price_fresh_s = 600
ercot_load_fresh_s = 1800
wind_solar_fresh_s = 10800
nws_fresh_s = 10800
eia_fresh_s = 10800

[mqtt]
host = "<broker host>"
port = 1883                   # 8883 when TLS is enabled
topic_root = "og/v1"
keepalive_s = 20

[retention]                  # days each record class is kept
"feed_obs".days = 30
"telemetry".days = 14
"trace.feed_change".days = 90
```

## Handling rules and reserved integrations

- Use a dedicated ERCOT account and subscription for the orchestrator. Never share keys with other tools or prototypes.
- Secrets live only in the env file on the server (owner `root:opengrid`, mode 640), never in the repository, tickets
  or chat.
- A source is flagged STALE when it passes its threshold. While any price or load feed is stale, the engine takes no
  new commitments but keeps delivering existing ones.
- Every feed-quality change and key rotation is recorded in the tamper-evident audit trace, kept for the configured
  retention period.

| Reserved integration | Purpose | Planned release |
| --- | --- | --- |
| ERCOT market submission (QSE) | Real DAM/RTM offers and COP instead of the simulated QSE | Later release |
| ERCOT storage API keys | Archive and storage data products | Later release |
| MISO API | Second market | Later release |
| Utility SCADA (DNP3/TLS first; ICCP, IEEE 2030.5, IEC 104, OPC UA after) | Real bank loading and utility instructions | Later release |
