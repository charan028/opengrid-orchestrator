# ogsim.customer

Customer-operator simulators: one simulated operator per configured customer/service
(PIPELINE_AC, DATA_CENTER, ERCOT_ENERGY/AS arbitrage, DIST_DEFERRAL, PARTNER_CAPACITY),
running autonomously against the orchestrator's real customer API and MQTT broker, exactly
as a real customer or partner integration would. Shares no code with `opengrid`; the only
shared surface is `interfaces/` (JSON Schemas for the two new MQTT messages below).

## What it does

1. **Service requests** (`ogsim.customer.requests`): submits opportunities/service requests
   through the orchestrator's customer API on a per-operator cadence (default ~1/min,
   BUILD.md's negligible-load target).
2. **Site measurements** (`ogsim.customer.signals`): publishes the DATA_CENTER site meter
   and PIPELINE_AC corridor current every 2s over MQTT, validated against
   `interfaces/mqtt/customer_site_meter.schema.json` /
   `interfaces/mqtt/pipeline_corridor_current.schema.json`.
3. **Delivery and billing**: polls obligation/invoice state and reacts (cancel, renominate,
   dispute) via `ogsim.customer.api_client.CustomerApiClient`.
4. **Random + manual anomaly injection** (`ogsim.customer.anomalies`): the customer anomaly
   catalogue (`load_step_datacenter`, `pipeline_current_surge`, `request_burst`,
   `malformed_request`, `late_cancellation`, `invoice_dispute`, `site_meter_stale`), wired
   into `ogsim.control`'s shared catalogue/random-engine/REST+UI/scenario-YAML control
   plane exactly like `ogsim.scada`/`ogsim.fleet`.

## Interface

- Config: `integration-sims/config/customer.yaml` (`OGSIM_CUSTOMER_CONFIG` to override),
  loaded by `ogsim.customer.config.load_customer_config`. Env wins over YAML; the
  production MQTT topic root is refused without `OGSIM_ENV=prod`
  (`ogsim.common.config.resolve_topic_root`, reused unchanged).
- MQTT identity: `og_sim_customer` (`OG_MQTT_CUSTOMER_PASSWORD`), publishing only `site/#`
  and `corridor/#` (ACL must not grant it anything else).
- HTTP: calls the orchestrator's customer API **through Apache** at
  `OGSIM_CUSTOMER_API_BASE` (e.g. `https://base.tocy-net.net` -- never the
  orchestrator's loopback port, which 401s without the Apache proxy secret), authenticating
  with HTTP Basic using one of the owner's per-service-group Apache accounts
  (`og-cust-dc`/`-pipe`/`-ercot`/`-dist`/`-partner`); credentials come from
  `OGSIM_CUSTOMER_<GROUP>_USER`/`_PASSWORD` (`/etc/opengrid/customer_sim.env`) and are
  never logged. It never sets `X-Remote-User` itself. **If `OGSIM_CUSTOMER_API_BASE` is
  unset, the sim refuses to start the API part and logs a warning; the MQTT
  site-measurement signals still run.** `api_mode: operator_fallback` targets today's
  operator-only `POST /og/api/opportunities` for use before the customer role/routes
  exist; every other action (obligations/invoices/dispute/cancel/renominate) requires
  `api_mode: customer`.
- Customer identity: `customer_id` is the fixed dev/test UUID from
  `dev/seed/customer_services_seed.sql`/`[api.roles.customer]` (e.g.
  `00000000-0000-7000-8000-0000000000c6` for the DATA_CENTER customer); MQTT topics and API
  payloads use that UUID directly. `contract_id` is read from the orchestrator's own
  `GET obligations`/`GET contracts` at startup (`ogsim.customer.runtime.discover_contract_ids`)
  rather than hard-coded where the API can supply it; a YAML `contract_id` (set for
  DATA_CENTER/PIPELINE_AC from the seed file) is only the fallback if discovery finds
  nothing or the API is disabled.
- Entry point: `python -m ogsim.customer` -> unit `og-sim-customer.service` (disabled by
  default; the live-path agent installs and enables it).

## How to test

```
.venv\Scripts\python.exe -m pytest integration-sims\tests -k customer -q
```

All tests use a fake MQTT transport (`ogsim.common.mqtt_client.MqttTransport`) and a fake
HTTP transport (`ogsim.customer.api_client.HttpTransport`); no live broker or orchestrator
is required. `CustomerEngine` is deterministic under its configured `seed`.
