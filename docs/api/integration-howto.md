# Integration how-to

Switching data sources is configuration only. No code path changes between live and simulated.

## Switch between live ERCOT/EIA/NWS and the market simulator

Edit the `[feeds.*]` base URLs in the config file the process reads (`OG_CONFIG`), then restart `og-feeds`.

| Source | Live | Market simulator |
|---|---|---|
| ERCOT | `https://api.ercot.com/api/public-reports` | `http://127.0.0.1:8090/ercot` |
| EIA | `https://api.eia.gov/v2` | `http://127.0.0.1:8090/eia` |
| NWS | `https://api.weather.gov` | `http://127.0.0.1:8090/nws` |

```toml
[feeds.ercot]
base_url = "http://127.0.0.1:8090/ercot"   # simulator; use the live URL above for production
```

- `orchestrator/config/orchestrator.toml` holds the live values; `orchestrator/config/test.toml` and
  `dev/config/dev.toml` point at the simulator.
- On the server: `systemctl restart og-feeds` (and `og-sim-market` if you changed its replay data).
- Locally: `make -C dev dev-up` starts Postgres, Mosquitto and the four simulators. See `dev/README.md`.
- Every value lands in `feed_obs` with a quality flag (`GOOD`, `ESTIMATED`, `STALE`) and its age, so a stale source is
  visible on the Markets and Health screens whichever source you use.
- To make the orchestrator use simulated devices instead of real ones, change `[mqtt] host` and `topic_root`. Keep
  test runs on their own root (`OG_MQTT_ROOT`) so they never touch production topics.
- The simulator's wire contract is `interfaces/http/market-api.md`. Anomalies such as `MARKET_401_KEY_REJECT`,
  price spikes and 5xx outages are injected through the control plane (`ogsim.control`).

**Token URL.** The ERCOT client signs in to a token endpoint that defaults to the real ERCOT B2C URL. To point it at a
stand-in, set `feeds.ercot.token_url`. No shipped config sets it, so a simulator setup that should not reach ERCOT must
set it explicitly.

## ERCOT token flow

Every ERCOT call needs a bearer token and a subscription key.

1. **Get a token.** `POST` a form to the B2C token endpoint
   (`https://ercotb2c.b2clogin.com/ercotb2c.onmicrosoft.com/B2C_1_PUBAPI-ROPC-FLOW/oauth2/v2.0/token`) with:

   | Field | Value |
   |---|---|
   | `grant_type` | `password` |
   | `username`, `password` | the ERCOT account, from `ERCOT_API_USER` and `ERCOT_API_PASSWORD` |
   | `client_id` | `fec253ea-0d06-4272-a5e6-b478baeecd70` (ERCOT's published public client id) |
   | `scope` | `openid fec253ea-0d06-4272-a5e6-b478baeecd70 offline_access` |
   | `response_type` | **`id_token`** |

2. **Use the `id_token` as the bearer**, not an access token: `Authorization: Bearer <id_token>`. `response_type=token`
   is rejected with `400 invalid_grant`.
3. **Add the subscription key**: `Ocp-Apim-Subscription-Key: <key>`.
4. **Lifetime.** A token lasts one hour and is renewed five minutes early.
5. **Key rotation.** The primary key (`ERCOT_PUBLIC_API_KEY_PRIMARY`) is used first. On `401`, `403` or quota
   exhaustion the client retries once with `ERCOT_PUBLIC_API_KEY_SECONDARY` and keeps it until restart. Each rotation
   is written to the audit trace.

Secrets stay in `/etc/opengrid/api_keys.env` (mode 640), single-quoted, and are referenced from config by variable
name only (`username_env`, `password_env`, `subscription_key_env`). See
`docs/orchestrator/07-delivery/integrations/api_keys.env.example` for the template.

**Rate and failure handling.** The client budgets 24 requests per minute (80% of ERCOT's 30), retries `GET`s at most
twice with backoff honouring `Retry-After`, and opens a circuit breaker after 5 consecutive failures while the last
good value keeps serving with its age. While any price or load feed is stale the engine takes no new commitments but
keeps delivering existing ones.

## Devices over MQTT

Hubs use topic root `og/v1/` with per-client users and ACLs, and obey only guardian-signed (Ed25519) commands with a
current sequence, epoch and lease. The topic table and message schemas are in `interfaces/mqtt/`
(`topics.md` and the `*.schema.json` files).
