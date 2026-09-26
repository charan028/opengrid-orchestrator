# OpenGrid Orchestrator — MVP-S Platform & Interfaces Specification

Invariants: see 00-invariants.md (canonical). Status: draft for owner approval (gate G1).

Status: build-ready · Written: 2026-09-25, for the Saturday 2026-09-26 18:00 delivery · Companion to
[`02a-mvp-s-spec-engine.md`](02a-mvp-s-spec-engine.md) (dispatch engine: `contracts`, `selector`, `ledger`,
`allocator`, `guardian`, `settle` internals). This document is the **platform and interfaces** half: repo layout,
`feeds`, the `fleet` twin and `sim` test harness, the MQTT wire contract, `health`, the `api`, the `ui`, deployment,
and non-functional budgets. Canonical module and table names are fixed by
[`01-saturday-delivery-plan.md`](01-saturday-delivery-plan.md) §2.1 and are used verbatim throughout.

**Reading order for implementers.** WS1 (platform) builds §1, §9 first — everything else in this document imports
those interfaces. WS2 (feeds) builds §2. WS3 (simulator) builds §4–§5. WS5 (health/guardian) builds §6. WS7 (UI)
builds §7–§8 against the read models this document defines. §10 and §11 are checked continuously, not built once.

**No orchestrator code exists yet.** Nothing here is a description of running code; it is the contract the fresh
Saturday build implements. The base-server simulators (`/opt/opengrid_sim`) and the local Streamlit pages in
`src/opengrid` are not a code base for this system — only the ERCOT/EIA HTTP call *patterns* in
`src/opengrid/clients/*.py` are reused, as thin reference, rewritten to the `feeds` interface below.

---

## 1. Repository layout, processes and configuration

### 1.1 Repository layout

One Python 3.13 package, `opengrid`, one Gitea repository `opengrid-orchestrator` on `calisto`
(`https://calisto.tocy-net.net/opengrid/opengrid-orchestrator`), tagged releases deployed to `basepower`.

```
opengrid-orchestrator/
  pyproject.toml                  # single package, one dependency set, HiGHS via `highspy`
  README.md
  Makefile                        # make lint / test / migrate / run-<process>
  config/
    orchestrator.toml             # non-secret config (schema: §1.4)
    logging.toml
  migrations/                     # SQL, forward-only within MVP-S (see §9.6)
    0001_init.sql
    0002_...
  src/opengrid/                   # "og.core" in these documents = the package opengrid.core
    core/                         # SHARED, single-owner functions (§12). No I/O, no process state
      models/                     # pydantic v2 contracts: wire (MQTT/API) + DB row shapes, all modules
      physics.py                  # SoC step, η, P/E/kVA capability, ramp — used by selector, allocator, guardian, sim, fleet
      limits.py                   # envelope checks (reserve, P, kVA, ramp, feeder ceiling) — allocator + guardian
      products.py                 # product-rule quantity rounding (min_qty / increment / block)
      crypto.py                   # Ed25519 sign/verify, command envelope (seq, epoch, precondition, lease)
      tracehash.py                # JCS canonicalisation + SHA-256 chain + verify
      timeutil.py                 # intervals, gates, clock quality
    platform/                     # cross-cutting I/O adapters: db.py (psycopg 3 pool), mqtt.py (aiomqtt),
                                   # config.py (TOML), metrics.py (prometheus_client), log.py
    trace/                        # trace store: append, checkpoints, retention pruning (uses core.tracehash)
    feeds/                        # §2 — process og-feeds: scheduler, ercot, eia, nws, breaker, normalize, store
    forecast/                     # §3 — library, run inside og-engine
    fleet/                        # §4 — digital twin (state + eligibility) inside og-engine; physics from core
    contracts/                    # 02a — contracts, product rules, opportunities, obligations, admission (og-engine)
    selector/  ledger/  allocator/ # 02a — og-engine
    guardian/                     # 02a §6 — process og-guardian (G-checks call core.limits on hub-reported inputs)
    safestop/                     # process og-safestop — stop-only key; imports only core + platform (K8)
    settle/                       # 02a §7 — process og-settle: M&V, billing, profitability
    health/                       # §6.4 — evaluator runs in og-settle; heartbeat emitter in platform, used by all
    sim/                          # §5 — process og-sim (test harness): hub/bank/SCADA/scenario; physics from core
    api/                          # §7 — process og-api: FastAPI app, auth, REST, SSE
    ui/                           # §8 — templates, static, routes (mounted by api)
  tests/
    unit/ property/ integration/ e2e/ perf/ chaos/        # per 04-mvp-s-test-plan.md
  deploy/
    systemd/                      # §9.3 unit files, one per process
    apache/og-orchestrator.conf   # §9.4
    postgres/                     # init SQL, pg_hba fragment
    mosquitto/                    # mosquitto.conf, ACL file, TLS/plain decision (§9.2)
    scripts/deploy.sh rollback.sh migrate.sh backup.sh
  scenarios/                      # scenario injector definitions (§5.5), YAML
```

### 1.2 Seven processes, one codebase

Each process is `python -m opengrid.<process>.main`, imports only the packages it needs, and opens its own DB pool
and MQTT connection. No process imports another process's package directly; cross-process contracts are the
pydantic models in `opengrid.contracts` (wire/DB shapes) plus Postgres tables and MQTT topics.

| # | systemd unit | Process | Entry point | Packages run | Port(s) |
|---|---|---|---|---|---|
| 1 | `og-feeds` | `feeds` | `python -m opengrid.feeds.main` | `feeds`, `forecast` (forecast runs inside feeds' cycle — no separate process, MVP-S) | `/metrics` :9101 |
| 2 | `og-engine` | `engine` | `python -m opengrid.engine.main` | `fleet` (twin, read side), `contracts_mod`, `selector`, `ledger`, `allocator`, `trace` (writer) | `/metrics` :9102 |
| 3 | `og-guardian` | `guardian` | `python -m opengrid.guardian.main` | `guardian` only (checks, Ed25519 signing, verdicts) | `/metrics` :9103 |
| 4 | `og-safestop` | `safestop` | `python -m opengrid.safestop.main` | `safestop` only — **independent process, stop-only key, no import of and no runtime dependency on `engine` or `guardian` (K8)** | `/metrics` :9106 |
| 5 | `og-sim` | `sim` | `python -m opengrid.sim.main` | `sim` (test harness: hubs, banks, simulated SCADA, scenario injector) | `/metrics` :9104 |
| 6 | `og-settle` | `settle` | `python -m opengrid.settle.main` | `settle`, `health` (heartbeats, alert rules, degraded-mode switch), `trace.retention` (pruning) | `/metrics` :9105 |
| 7 | `og-api` | `api` | `python -m opengrid.api.main` (uvicorn) | `api`, `ui` | :8080 (loopback only) |

`health` (heartbeats, feed/hub freshness, alert rules, degraded-mode switch, `/metrics` aggregation for the UI) runs
inside the `og-settle` process for MVP-S — health is a batch-cadence read-and-alert job like settlement, and neither
the engine nor the guardian may depend on it, so colocating it with `settle` (rather than with `guardian`, as an
earlier draft had it) keeps the guardian process minimal and keeps health from ever being a reason the guardian is
slow to verdict. Its logic is still an isolated package (`opengrid.health`) so it can be split into its own unit in
`MVP-J` without a redesign. Trace pruning/checkpointing (§8.3 of `02a`) also runs on `og-settle`'s cadence, for the
same reason: neither is on the real-time or safety path.

### 1.3 Why 7 processes, not fewer or more

Split by **time scale and failure domain** (delivery plan §2.1): `feeds` (seconds–hours, external I/O bound),
`engine` (2 s real-time loop, CPU/LP bound), `guardian` (must independently verify every batch the engine proposes —
the independent check, K3/K13), **`safestop` (must work even if `engine` and `guardian` are both down — the
independent stop authority, K8; this is why it is its own unit and not folded into `guardian`)**, `sim` (stands in
for hardware, must be killable without taking down the real system), `settle` (batch — M&V, billing, profitability,
trace pruning, health evaluation; can fall behind without safety impact), `api` (stateless, restartable, serves
UI/SSE). Postgres is the single source of truth; MQTT is the device boundary only (never used between `engine`,
`guardian`, `safestop`, `settle`, `api`).

**Design-error fix (as-reviewed).** An earlier draft of this document colocated `safestop` inside the `guardian`
process to save a systemd unit. That is a K8 violation: K8 requires the scoped safe stop to work "when the engine is
down," and a stop that shares a process with the guardian also goes down if the guardian process crashes, hangs, or
is itself the thing being stopped-around. `safestop` is therefore its own unit, `og-safestop`, with its own
stop-only Ed25519 key (distinct from the guardian's signing key, §6.5 of `02a`), its own `/metrics` port, and no
import of `opengrid.guardian` or `opengrid.engine`. It talks to the rest of the system only through Postgres
(`stop_event`) and MQTT (the retained `og/v1/stop/*` topic it publishes to directly) — the same two channels every
other process uses, so independence is structural, not just a naming convention.

### 1.4 Configuration: `config/orchestrator.toml`

Non-secret configuration, one file, read by every process at startup (hot-reload not required for MVP-S; `SIGHUP`
re-reads `retention.*` and `health.thresholds.*` only). Secrets are never in this file (§1.5).

```toml
[general]
env = "prod"                       # "prod" | "dev" — dev points at a local Postgres/Mosquitto for offline work
node_id = "basepower"
timezone_market = "America/Chicago"

[postgres]
host = "127.0.0.1"
port = 5432
database = "og"                    # "og_test" for the CI/integration test pool, same owner (opengrid)
pool_min = 2
pool_max = 8                       # per process; see §11.5 for the 7-process total vs Postgres max_connections

[mqtt]
host = "127.0.0.1"
port = 1883
topic_root = "og/v1"               # every topic in §6.2 is under this root; ACL is scoped to it
client_id_prefix = "og"
keepalive_s = 20

[feeds.ercot]
subscription_key_env = "ERCOT_PUBLIC_API_KEY_PRIMARY"   # falls back to ERCOT_PUBLIC_API_KEY_SECONDARY, §2.6
username_env = "ERCOT_API_USER"
password_env = "ERCOT_API_PASSWORD"
budget_requests_per_min = 24        # 80% of the published 30/min (04-external-data-integration.md §4.2)
products = ["np6-905-cd", "np6-345-cd", "np4-732-cd", "np4-737-cd", "np4-188-cd"]  # §2.2

[feeds.eia]
api_key_env = "EIA_API_KEY"
respondent = "ERCO"

[feeds.nws]
user_agent = "OpenGrid-Orchestrator (ops@tocy-net.net)"
grid_point = "EWX/156,91"           # resolved once at startup from lat/lon (§2.4)

[feeds.staleness]
ercot_price_fresh_s = 600
ercot_price_lgv_s = 900
ercot_load_fresh_s = 1800
wind_solar_fresh_s = 10800
nws_fresh_s = 10800
eia_fresh_s = 10800

[forecast]
horizon_hours = 24
resolution_min = 15
quantiles = [0.10, 0.50, 0.90]

[fleet]
hub_count = 2000
banks = 40
zones = ["LZ_NORTH", "LZ_SOUTH", "LZ_HOUSTON", "LZ_WEST"]
telemetry_interval_s = 2
lease_ttl_s = 30

[allocator]
cycle_interval_s = 2
dwell_s = 300
hysteresis_usd_per_mwh = 5

[guardian]
key_path = "/etc/opengrid/guardian_ed25519.key"     # private key file, mode 0600, see §1.5
verdict_timeout_ms = 300

[health]
heartbeat_interval_s = 5
heartbeat_miss_threshold = 3
hub_stale_s = 6                     # > telemetry_interval_s * 3
hub_offline_s = 30

[retention]
"trace.selection".days = 90
"trace.commitment".days = 400
"trace.renomination".days = 400
"trace.exception".days = 400
"trace.shortfall".days = 400
"trace.command".days = 30
"trace.operator_action".days = 400
"trace.feed_change".days = 90
"trace.alert".days = 90
"telemetry".days = 14
"feed_obs".days = 30

[api]
bind_host = "127.0.0.1"
bind_port = 8080
sse_heartbeat_s = 15

[ui]
base_path = "/og"

[metrics]
bind_host = "127.0.0.1"
```

Every `retention.<class>.days` key is read by `trace.retention` (§1.6) at prune time; unknown classes default to 400
days rather than being silently dropped, so a class added later degrades safely.

### 1.5 Secrets: `/etc/opengrid/api_keys.env` and `/etc/opengrid/secrets.env`

Two files, both outside the repo, never derived from `/opt/opengrid_sim/config.ini` (new credentials only, per the
delivery plan). Loaded by each systemd unit via `EnvironmentFile=` (§9.3). There is no `sudo` on `basepower`; file
ownership/mode is set once by an administrator with `runuser`/direct root login during provisioning, not by any
orchestrator process at runtime.

**`/etc/opengrid/api_keys.env`** (external-data credentials; mode `640`, owner `root:opengrid` — every process runs
as the `opengrid` group member, so group-read is sufficient and no process needs to run as root):

```
EIA_API_KEY=<EIA registration>
ERCOT_API_USER=<ROPC account email>
ERCOT_API_PASSWORD=<that account's password>
ERCOT_PUBLIC_API_KEY_PRIMARY=<Ocp-Apim-Subscription-Key, primary>
ERCOT_PUBLIC_API_KEY_SECONDARY=<Ocp-Apim-Subscription-Key, secondary — §2.6 rotates to this on 401/403 or quota exhaustion>
ERCOT_STORAGE_API_KEY_PRIMARY=<reserved, not used in MVP-S>
ERCOT_STORAGE_API_KEY_SECONDARY=<reserved, not used in MVP-S>
MISO_API_USER=<reserved, not used in MVP-S>
MISO_API_PASSWORD=<reserved, not used in MVP-S>
```

`feeds.ercot` uses `ERCOT_PUBLIC_API_KEY_PRIMARY` as the `Ocp-Apim-Subscription-Key` on every call; on a `401`/`403`
response or the circuit breaker recording quota exhaustion (§2.6), it swaps to `ERCOT_PUBLIC_API_KEY_SECONDARY` for
the remainder of the process lifetime and logs the rotation as a `feed_change` trace event. `ERCOT_STORAGE_API_*` and
`MISO_*` are provisioned but unused — no MVP-S code path reads them; they exist for the `R2` archive/PJM-adjacent
work and are listed here only so `feeds` never treats an unrecognized env var as a misconfiguration.

**`/etc/opengrid/secrets.env`** (platform credentials; mode `640`, owner `root:opengrid`):

```
OG_DB_PASSWORD=<opengrid Postgres role password>
OG_MQTT_ENGINE_PASSWORD=<for MQTT user og_engine>
OG_MQTT_GUARDIAN_PASSWORD=<for MQTT user og_guardian>
OG_MQTT_SAFESTOP_PASSWORD=<for MQTT user og_safestop>
OG_MQTT_SIM_PASSWORD=<for MQTT user og_sim>
OG_MQTT_API_PASSWORD=<for MQTT user og_api>
GUARDIAN_SIGNING_SEED=<Ed25519 seed, generated once, backed up offline>
OG_BASIC_AUTH_OPERATOR=<htpasswd-style hash, provisioned into Apache, §9.4>
OG_BASIC_AUTH_VIEWER=<htpasswd-style hash>
```

`ERCOT_API_USER`/`PASSWORD` and both `ERCOT_PUBLIC_API_KEY_*` are a **new** account and subscription, never the
legacy prototype pollers' key (`04-external-data-integration.md` §4.2 "not on the orchestrator's key").

### 1.6 The `trace` library

`opengrid.trace` is a small library, not a process: every process that produces an auditable event (`feeds` on a
feed-quality change, `engine` on selection/commitment/re-nomination/shortfall, `guardian` on every verdict and
operator action, `settle` on billing runs) calls `trace.append(event_class, payload)`. It writes one row to the
`trace` table (owned by 02a; schema shared) with `prev_hash`/`hash` chained per-process-stream, and periodically
(`trace.retention`) prunes rows older than `retention.<event_class>.days` while keeping **checkpoint** rows
(`trace_checkpoint`, one per 1,000 pruned rows, storing the running hash) so `verify(range)` still passes across a
pruned gap — this is the "checkpointed pruning" the delivery-plan decisions require (§8a.3).

---

## 2. `feeds`

Owns tables `feed_obs`, `feed_status`. Interface: `latest(series) -> FeedObs`, `window(series, t0, t1) -> list[FeedObs]`.
Reference only for HTTP call shape: `src/opengrid/clients/ercot_client.py`, `eia_client.py` — rewritten against this
interface, not imported as-is (the auth flow and endpoint paths are correct and reused; the polling, staleness,
breaker and normalization code is new).

### 2.1 Scope cut from the full spec

The full external-data-integration spec (11 sources, 38 products) is `MVP-J`/`R2`. MVP-S ingests exactly **three**
sources: ERCOT (5 products), EIA (fallback for load), NWS (weather for load/PV forecast inputs). No S3/S5–S11
reference-data sources, no PJM, no reconciliation rules R-1…R-6, no backfill/replay corpus. `REPLAY` mode (§7 of
`04-external-data-integration.md`) is kept as a single flag because it directly serves A1 ("real data on screen even
if the live API is unavailable") at negligible cost: `feeds` can be pointed at a recorded JSON fixture directory
instead of `api.ercot.com` and republishes it with original timestamps plus `replay=true`.

### 2.2 Exact ERCOT products

| Product | Endpoint | What | Cadence polled | Consumer |
|---|---|---|---|---|
| Settlement point prices (RT) | `GET /np6-905-cd/spp_node_zone_hub` (`settlementPointType=HU` for all hubs, or `settlementPoint=HB_HUBAVG` for the system average) | 15-min real-time settlement point price, $/MWh, per hub | Every 5 min | `forecast` (price quantiles), `engine` selector value term, `ui` Markets screen |
| System load by weather zone | `GET /np6-345-cd/act_sys_load_by_wzn` | Actual load, MW, per ERCOT weather zone | Hourly (this is a daily-posted product; poll hourly, expect no new data until the next posting) | `forecast` (load quantiles), `ui` |
| Wind actual vs forecast | `GET /np4-732-cd/wpp_hrly_avrg_actl_fcast` (filter `postedDatetimeFrom`, take the latest posting) | System-wide wind output, actual + forecast, MW | Every 30 min | `forecast`, `ui`, `fleet` PV/renewable context |
| Solar actual vs forecast | `GET /np4-737-cd/spp_hrly_avrg_actl_fcast` | System-wide solar output, actual + forecast, MW | Every 30 min | Same as wind |
| AS clearing prices (DAM MCPC) | `GET /np4-188-cd/dam_clear_price_for_cap` | Day-ahead ancillary-service clearing price, $/MW-h, per AS product (RegUp, RegDown, RRS, NonSpin, ECRS) | Once daily at 14:00 CT (after DAM results post ~13:55) | `ui` Markets screen, `engine` `ERCOT_AS` capacity-hold value term |

Auth (unchanged from `ercot_client.py`): Azure AD B2C ROPC token (`POST` to the B2C token URL, `grant_type=password`,
1-hour lifetime, re-authenticate — never refresh) **and** `Ocp-Apim-Subscription-Key` header on every data call. Token
is cached in-process and renewed 5 minutes before expiry; a 401 triggers one forced re-authentication and retry.

### 2.3 EIA fallback

If the ERCOT circuit breaker (§2.6) is open for load data, `feeds` calls `EIAClient.hourly_demand(respondent="ERCO")`
(`GET /electricity/rto/region-data/data/`, `facets[type][]=D`) as a same-shape substitute for system load, published
into `feed_obs` with `source_id="EIA"` and `quality="ESTIMATED"` so downstream consumers can tell it apart. EIA is
never polled as primary in MVP-S (ERCOT is more granular and zone-level); it is a standby only.

### 2.4 NWS

`GET https://api.weather.gov/points/{lat},{lon}` once at startup to resolve the forecast gridpoint (config
`feeds.nws.grid_point` can pin it and skip the lookup), then `GET /gridpoints/{office}/{x},{y}/forecast/hourly` every
hour with `If-Modified-Since`. `User-Agent` header is mandatory (NWS rejects requests without one). Fields used:
temperature, dew point, sky cover — inputs to the load/PV persistence forecast (§3).

### 2.5 Schedules and the 30 req/min budget

One scheduler loop per source (`feeds.scheduler`), each with its own asyncio task and its own token bucket. The
ERCOT bucket is capacity 5, refill 0.4/s (24/min = 80% of the published 30/min), matching
`04-external-data-integration.md` §4.2. MVP-S's own steady-state ERCOT usage is far under budget:

| Product | Polls/hour | ERCOT calls/hour |
|---|---|---|
| SPP (5 min) | 12 | 12 |
| Load by weather zone (hourly) | 1 | 1 |
| Wind (30 min) | 2 | 2 |
| Solar (30 min) | 2 | 2 |
| AS MCPC (daily, one window) | ~1/24 | ~0.04 |
| Token refresh (1/50 min) | 1.2 | 1.2 |
| **Total** | | **≈ 18/hour ≈ 0.3/min** |

This leaves large headroom under the 24/min bucket even with retries; no P1/P2/P3 priority split is needed at this
volume (full spec §4.2's three-class system is `MVP-J`).

### 2.6 Caching, staleness thresholds, circuit breaker

- **Last-good value (LGV) cache.** Every `feed_obs` write updates an in-memory LGV per `(source_id, series_key)`
  keyed row and the `feed_status` table (`series_key`, `last_value_at`, `quality`, `age_s` computed on read, not
  stored). `latest(series)` always returns a value plus its age and quality — never an error — per
  `04-external-data-integration.md` FR-ING-127.
- **Staleness thresholds** (from `[feeds.staleness]`, §1.4): price fresh ≤ 600 s, LGV usable ≤ 900 s; load fresh ≤
  1800 s; wind/solar fresh ≤ 10800 s; NWS fresh ≤ 10800 s. Crossing a threshold flips `feed_status.quality` from
  `GOOD` to `STALE` and appends a `trace` event of class `feed_change` (drives health §6.5's "feed stale → no new
  commitments" degraded mode).
- **Circuit breaker** per source (ERCOT, EIA, NWS), simplified from the full 4.6 table: opens after 5 consecutive
  failures or ≥ 50% failures in the last 10 calls; open duration 60 s, doubling to 15 min; 1 half-open probe. While
  open, `latest()` keeps serving the LGV with its growing age. Breaker state is exported as
  `og_feed_breaker_open{source=...}` (0/1).
- **ERCOT key rotation.** A `401`/`403` on `ERCOT_PUBLIC_API_KEY_PRIMARY`, or the breaker opening specifically on a
  `429`/quota signal, makes `feeds` retry once immediately with `ERCOT_PUBLIC_API_KEY_SECONDARY` before counting the
  call as a failure; if the secondary also fails, the normal breaker/backoff path applies. Once rotated, `feeds` keeps
  using the secondary key for the rest of the process lifetime (no automatic rotation back — a restart or an operator
  config change is required), and the rotation itself is written to `trace` as a `feed_change` event.
- **Retries**: idempotent GETs only, capped exponential backoff with full jitter (base 1 s, cap 8 s, max 2 retries),
  honoring `Retry-After` on 429. Never retried: 400, 403, 404, 422, and 401 after one re-authentication — same
  taxonomy as `04-external-data-integration.md` §4.4/§4.8, trimmed to the classes MVP-S's 5 products can actually hit.

### 2.7 Normalization into `feed_obs`

```sql
CREATE TABLE feed_obs (
  id            BIGSERIAL PRIMARY KEY,
  source_id     TEXT NOT NULL,           -- 'ERCOT' | 'EIA' | 'NWS'
  product       TEXT NOT NULL,           -- e.g. 'np6-905-cd'
  series_key    TEXT NOT NULL,           -- e.g. 'HB_HUBAVG', 'LZ_SOUTH', 'wind_system', 'ECRS'
  interval_start_utc TIMESTAMPTZ NOT NULL,
  interval_end_utc   TIMESTAMPTZ NOT NULL,
  value         DOUBLE PRECISION NOT NULL,
  unit          TEXT NOT NULL,           -- 'usd_per_mwh' | 'mw' | 'degc' | 'pct'
  quality       TEXT NOT NULL DEFAULT 'GOOD',  -- GOOD | ESTIMATED | STALE | QUARANTINED
  posted_at     TIMESTAMPTZ,
  retrieved_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  batch_id      UUID NOT NULL,
  UNIQUE (source_id, product, series_key, interval_start_utc)
);
CREATE INDEX ON feed_obs (product, series_key, interval_start_utc DESC);

CREATE TABLE feed_status (
  source_id     TEXT NOT NULL,
  product       TEXT NOT NULL,
  series_key    TEXT NOT NULL,
  last_value_at TIMESTAMPTZ,
  quality       TEXT NOT NULL DEFAULT 'GOOD',
  breaker_open  BOOLEAN NOT NULL DEFAULT false,
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (source_id, product, series_key)
);
```

Idempotent upsert on the natural key `(source_id, product, series_key, interval_start_utc)`; a re-posting overwrites
in place for MVP-S (no `correction_version` — that refinement is `R2`).

---

## 3. `forecast`: quantile persistence

24 h horizon, 15-min resolution (96 steps), for **price** (settlement point, $/MWh) and **load** (weather-zone MW).
This is deliberately the simplest defensible method (first-principles review §4 row 3: "defer sophistication"),
producing P10/P50/P90 so the selector has a 3-scenario input (`03-decision-engine.md` §6.2 scenario cardinality) without
building a real forecaster.

**Method.** For each of the last 14 days, take the value at the same time-of-day and day-type (weekday/weekend) as
the target 15-min slot, from `feed_obs`. P50 is the sample median, P10/P90 are the sample 10th/90th percentiles of
that pool (linear interpolation, `numpy.percentile`). If a slot's live series (§2.2) is `STALE` beyond its window
(§2.6), the same computation runs over the LGV-extended history and the resulting quantile band is widened by a
fixed multiplier (1.3×) to signal reduced confidence, and `forecast.firm_fitness` is set to `NOT_FOR_FIRM`.

```sql
CREATE TABLE forecast (
  id              BIGSERIAL PRIMARY KEY,
  series_key      TEXT NOT NULL,          -- price hub or weather zone
  kind            TEXT NOT NULL,          -- 'price' | 'load'
  interval_start_utc TIMESTAMPTZ NOT NULL,
  horizon_step    SMALLINT NOT NULL,      -- 0..95
  p10 DOUBLE PRECISION NOT NULL,
  p50 DOUBLE PRECISION NOT NULL,
  p90 DOUBLE PRECISION NOT NULL,
  firm_fitness    TEXT NOT NULL DEFAULT 'FIRM_OK',
  computed_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (series_key, kind, interval_start_utc)
);
```

Recomputed every 15 minutes (aligned with the selector's gate cadence, `03-decision-engine.md` §3.1) and on demand
when `feeds` reports a fresh SPP/load batch. `forecast.scenarios(horizon)` returns the three quantile columns as the
selector's 3-scenario set (P10 = low, P50 = mid, P90 = high), matching the `L-DA`/`L-ID` "3 scenarios" sizing the
delivery plan commits to (§2.1 "3 scenarios").

---

## 4. `fleet` twin and the hub/bank model

Owns `hub`, `bank`, `hub_state` (latest, one row per hub, upserted), `telemetry` (append-only, partitioned by day).
Interface: `capability(bank, t) -> AvailableCapability` used by `selector`/`allocator` (owned by 02a).

### 4.1 Scale

2,000 hubs over **40 banks** (~50 hubs/bank average) across the four ERCOT weather zones in `[fleet.zones]`; a 10,000
stretch target uses the same schema and code path (`--fleet-scale=10000` on `sim`), just more rows and more MQTT
connections — no redesign, per the delivery plan's "path to k3s design without code change" principle applied down
to "path to 10k without code change."

### 4.2 Hub/bank physics model

Per hub $i$, state of charge evolves each 2 s tick in `sim` (§5) exactly as the canonical SoC equation in
`06-first-principles-review.md` §5.1:

$$e_{i,t+1} = e_{i,t} + \eta_c\, p^c_{i,t}\,\Delta t - \frac{\Delta t}{\eta_d}\, p^d_{i,t} - \ell_i \Delta t$$

with per-hub parameters: usable energy $E_i$ (kWh, default 39.2, one Base Power battery unit, confirmed by Base),
reserve floor $R_i$ (kWh, default 20% of $E_i$, homeowner-configurable), power limit $P_i$ (kW, default 11, one Base
Power inverter unit, confirmed by Base), round-trip split $\eta_c=\eta_d=\sqrt{0.90}$ (≈0.949 each way, giving 0.90
round-trip), self-discharge $\ell_i$ negligible (0.0005 kWh/h) but modeled for completeness.

**Dual-unit homes.** A configurable share of homes (`fleet.dual_unit_share`, default 20%) have **two** battery units
instead of one: $E_i = 78.4$ kWh, $P_i = 20$ kW (confirmed by Base). Which hubs get the dual-unit parameters is
deterministic and identical between `opengrid.fleet.seed` and `ogsim.fleet.state` (so hub ids line up): hub index $i$
(hub-{i:05d}) is dual-unit iff floor((i + 1) * dual_unit_share) > floor(i * dual_unit_share), which selects exactly
floor(hub_count * dual_unit_share) hubs, deterministically and evenly spread across i in range(hub_count). At the
MVP-S scale (2,000 hubs, 20% share) this selects exactly 400 dual-unit hubs.

A bank $b$ aggregates its member hubs' $P$ and has its own **kVA bank rating** (default 600 kVA per ~50-home feeder
segment, not a single distribution transformer, config per bank) and **reserve**, both enforced as hard constraints
by `capability(bank, t)` before the LP ever sees the bank — the additive one-buyer floor of the first-principles
review §5.1's "reserve, L1, hard, never priced."

```sql
CREATE TABLE hub (
  hub_id      TEXT PRIMARY KEY,
  bank_id     TEXT NOT NULL,
  zone        TEXT NOT NULL,
  e_kwh       DOUBLE PRECISION NOT NULL,     -- usable energy capacity
  r_kwh       DOUBLE PRECISION NOT NULL,     -- reserve floor
  p_kw        DOUBLE PRECISION NOT NULL,     -- power limit
  eta_c       DOUBLE PRECISION NOT NULL DEFAULT 0.9487,
  eta_d       DOUBLE PRECISION NOT NULL DEFAULT 0.9487,
  lat DOUBLE PRECISION, lon DOUBLE PRECISION,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE bank (
  bank_id     TEXT PRIMARY KEY,
  zone        TEXT NOT NULL,
  kva_rating  DOUBLE PRECISION NOT NULL,
  reserve_kva DOUBLE PRECISION NOT NULL DEFAULT 0,
  feeder_id   TEXT
);
CREATE TABLE hub_state (            -- latest snapshot, one row per hub, UPSERTed every telemetry tick
  hub_id      TEXT PRIMARY KEY REFERENCES hub(hub_id),
  soc_kwh     DOUBLE PRECISION NOT NULL,
  p_kw        DOUBLE PRECISION NOT NULL,     -- +charge / -discharge
  health      TEXT NOT NULL DEFAULT 'online', -- online | stale | fault
  lease_epoch BIGINT NOT NULL DEFAULT 0,
  lease_expires_at TIMESTAMPTZ,
  last_command_id UUID,
  last_seen_at TIMESTAMPTZ NOT NULL,
  fault_code  TEXT
);
CREATE TABLE telemetry (             -- partitioned by day; see §9.6
  hub_id TEXT NOT NULL, ts TIMESTAMPTZ NOT NULL, soc_kwh DOUBLE PRECISION, p_kw DOUBLE PRECISION,
  seq BIGINT NOT NULL, epoch BIGINT NOT NULL, health TEXT, PRIMARY KEY (hub_id, ts)
) PARTITION BY RANGE (ts);
```

`telemetry` is bulk-inserted every 2 s via `COPY` (batches of ~2,000 rows at full fleet scale) rather than
row-by-row `INSERT`, to hold RT cycle p99 (§10). `hub_state` is the only table the 2-s allocator reads for current
fleet capability — it never scans `telemetry`.

### 4.3 Lease, epoch, sequence, signature

Every command batch carries `(hub_id, epoch, seq)`; a hub only accepts a command whose `epoch` is ≥ its last accepted
epoch and whose `seq` is strictly greater within that epoch (replay/out-of-order rejection, mirroring
`03-decision-engine.md`'s command-integrity rules at MVP-S scale). Each hub holds a **lease** of `fleet.lease_ttl_s`
(30 s, config); the engine (via guardian) renews it on every accepted cycle. If the lease expires without renewal,
the hub enters **local autonomy** (§4.4). Every command batch is Ed25519-signed by the guardian's private key
(`guardian.key_path`); the hub (real, or `sim`'s simulated hub) verifies the signature against the guardian's public
key before acting — a rejected signature is logged and the command dropped, never partially applied.

### 4.4 Local autonomy on lease expiry

On lease expiry, a simulated hub in `sim` (and any real hub) falls back to a fixed, pre-loaded local schedule (home
reserve only, no market participation) until a fresh, validly signed, correctly epoched command batch arrives. This
is the fleet-level expression of guardian's `TIMEOUT → hold` rule; `sim` implements it identically to how a real hub
must, per the security architecture's "implemented in `agent-sim` exactly as a real hub must" (§7.4 of
`02-security-architecture.md`).

---

## 5. `sim`: the fleet test harness

`sim` is the spec's test harness (`agent-sim`/`grid-sim`, `03-decision-engine.md` §12.3), **not** a concept
simulator and not a copy of `/opt/opengrid_sim`. It is new code, written against this MQTT contract, that stands in
for real hubs and SCADA until hardware exists.

### 5.1 What `sim` runs

One process, `python -m opengrid.sim.main --fleet-scale=2000`, spawning:

- **2,000 hub simulators** (asyncio tasks, not OS processes — 2,000 threads would not fit in 16 GB), each publishing
  telemetry every 2 s (§4.2 physics), holding its own lease/epoch/sequence state, verifying every inbound command's
  Ed25519 signature, and falling to local autonomy on lease expiry (§4.4).
- **~40 bank aggregators**, each computing bank-level P/kVA from its member hubs and publishing a simulated SCADA
  bank-load reading (adds realistic noise + a configurable "true" feeder background load) that `allocator`'s
  `DIST_DEFERRAL` PI loop (02a) closes on.
- **The scenario injector** (§5.5), driven by the UI's scenario panel (§8) via a small control topic
  (`og/v1/scenario/cmd`, §5.4) or a CLI flag for scripted test runs.

### 5.2 Process boundary and why `sim` is killable

`sim` is a distinct systemd unit specifically so the scenario "kill the engine process" / chaos tests can also kill
`sim` itself without special-casing it, and so the real fleet (when it exists) is a drop-in replacement: nothing in
`engine`, `guardian`, or `api` imports `opengrid.sim`; they only see MQTT topics and Postgres rows.

### 5.3 10,000-hub stretch

`--fleet-scale=10000` runs the same code; the acceptance bar (§10) explicitly allows recording 10k as "measured, not
met" per the delivery plan's cut line 4 — `sim` must not silently degrade accuracy to hit the number, it must report
actual p99 at 10k honestly.

### 5.4 Command/ack loop `sim` implements

1. `engine` → `guardian`: proposed batch (internal, not MQTT — same process boundary is Postgres `LISTEN/NOTIFY` or a
   direct call within the `engine`↔`guardian` trust boundary defined in 02a).
2. `guardian` → MQTT `og/v1/cmd/<bank_id>/batch` (signed, QoS 1): the command batch.
3. `sim` hub tasks subscribed to their bank's command topic verify signature + lease/epoch/seq, apply the setpoint
   physics-limited by $P_i$/SoC, and publish an ack to `og/v1/ack/<hub_id>`.
4. `sim` publishes telemetry to `og/v1/tel/<zone>/<bank_id>/<hub_id>` every 2 s regardless of command activity.
5. On a `safestop` scoped stop, `sim` hubs subscribed to the retained `og/v1/stop/<scope>/<id>` topic immediately zero
   commanded power (never below L1 reserve floor for *discharge into* the fleet — a stop halts new dispatch, it does
   not force-discharge) and stay stopped until the retained message is cleared.

### 5.5 Scenario injector

Implements exactly the delivery plan's scenario-panel list (§4) plus the review's pilot-protocol subset that fits
MVP-S's 30-hour budget:

| Scenario | Mechanism | Proves |
|---|---|---|
| Partner call | Publishes a synthetic `PARTNER_CAPACITY` opportunity via the engine's admission API (02a), triggered over `og/v1/scenario/cmd` | A5, admission → selection → commitment path |
| Price spike | `sim` overrides the next `feed_obs` price read (test hook, not a real ERCOT call) to a configured $/MWh value | Selector reacts; commitment lock holds existing deliveries (A4, A10) |
| Better-paying call during delivery | Admits a second, higher-value opportunity for an already-committed bank/window | **The commitment-lock demonstration**: guardian G-19 refuses any batch that would reduce the committed allocation without a reason code |
| Feeder overload | Bumps the simulated SCADA bank-load reading above the bank's kVA rating | `DIST_DEFERRAL` PI loop responds; guardian's ramp/feeder ceiling holds |
| Zone comms loss | Stops publishing telemetry for all hubs in one zone for a configured duration | Health marks those hubs `stale`→`offline`; engine excludes them; hubs (simulated) fall to local autonomy on lease expiry |
| Stale feed | Pauses `feeds`' ERCOT poll for a configured duration (test hook) | Degraded mode: feed stale → no new commitments (§6.5) |
| Process kill | `systemctl kill -s SIGKILL og-engine` (or any of the 7 units, including `og-safestop`), run manually over SSH as root during the demo — there is no `sudo` on `basepower` (§9.1), so the scenario panel's "kill process" button only *displays instructions and a countdown*, it does not execute the kill itself | A11 availability: hubs hold lease then go local; `systemctl` `Restart=always` brings the process back; state rebuilds from Postgres |

Scenario state and every injected event are written to `trace` (class `exception` or `feed_change` as appropriate)
so the demo narrative is itself auditable.

---

## 6. MQTT topic tree, message schemas, and `health`

### 6.1 Broker

Mosquitto on `127.0.0.1:1883`, plain (not TLS — loopback-only, matching the single-node hardening posture of §9.2;
this is a deliberate MVP-S simplification from the production mTLS device boundary, recorded in Open points). Four
MQTT users, one per process that touches the broker — `og_engine`, `og_guardian`, `og_safestop`, `og_sim`, `og_api`
(`feeds` and `settle` never touch MQTT) — each with its own password (`OG_MQTT_*_PASSWORD`, §1.5), ACL-restricted
(§6.2) to the `og/v1/` topic root already configured on the broker. No anonymous access.

### 6.2 Topic tree

Every topic is under the configured root `og/v1/` (`[mqtt].topic_root`, §1.4), and every second-level prefix below
(`tel`, `ack`, `cmd`, `stop`, `lease`, `scada`, `scenario`) matches the broker's existing ACL grants exactly — this
document does not introduce any new top-level prefix.

| Topic | Direction | QoS | Retain | Publisher | Subscriber(s) |
|---|---|---|---|---|---|
| `og/v1/tel/<zone>/<bank_id>/<hub_id>` | hub → engine | 0 | No | `og_sim` (or a real hub) | `og_engine` (fleet twin ingest), `og_api` (SSE fan-out, sampled) |
| `og/v1/cmd/<bank_id>/batch` | guardian → hubs | 1 | No | `og_guardian` | `og_sim` hub tasks for that bank |
| `og/v1/ack/<hub_id>` | hub → engine | 1 | No | `og_sim` (or a real hub) | `og_engine`, `og_guardian` (for verdict/exec latency) |
| `og/v1/stop/<scope>/<id>` | safestop → hubs | 1 | **Yes** | `og_safestop` | `og_sim` (all hubs in scope), `og_api` |
| `og/v1/lease/<hub_id>` | guardian → hub | 1 | **Yes** | `og_guardian` | `og_sim` (that hub) |
| `og/v1/scenario/cmd` | api/operator → sim | 1 | No | `og_api` (scenario panel) | `og_sim` |
| `og/v1/scada/<bank_id>` | sim → engine | 0 | No | `og_sim` (simulated SCADA) | `og_engine` (`DIST_DEFERRAL` PI loop) |

`<scope>` for `og/v1/stop/*` is `fleet`, `zone/<zone>`, or `bank/<bank_id>`; `<id>` is a stop-event UUID. A retained
empty payload clears the stop (release).

### 6.3 Message schemas (pydantic v2, `opengrid.contracts.mqtt`)

```python
class Telemetry(BaseModel):
    hub_id: str
    bank_id: str
    zone: str
    ts: datetime                # UTC
    soc_kwh: float
    p_kw: float                 # +charge / -discharge
    health: Literal["online", "stale", "fault"]
    seq: int
    epoch: int
    fault_code: str | None = None

class CommandItem(BaseModel):
    hub_id: str
    p_kw_setpoint: float
    reason_code: str            # e.g. "SELECTOR", "ALLOCATOR_HEADROOM", "R-COMMIT-LOCK-L1", ...

class CommandBatch(BaseModel):
    batch_id: UUID
    bank_id: str
    epoch: int
    seq: int
    issued_at: datetime
    expires_at: datetime        # commands older than this are void even if delivered late
    items: list[CommandItem]
    signature: str              # base64 Ed25519 signature over the canonical JSON of the fields above

class Ack(BaseModel):
    hub_id: str
    batch_id: UUID
    accepted: bool
    applied_p_kw: float | None = None
    reject_reason: str | None = None  # "BAD_SIGNATURE" | "STALE_EPOCH" | "STALE_SEQ" | "EXPIRED" | None
    ts: datetime

class StopEvent(BaseModel):
    stop_id: UUID
    scope: Literal["fleet", "zone", "bank"]
    scope_id: str | None = None       # zone name or bank_id; None for fleet scope
    reason: str
    issued_by: str                    # operator id or "SAFESTOP_AUTO"
    issued_at: datetime

class Lease(BaseModel):
    hub_id: str
    epoch: int
    expires_at: datetime
    issued_at: datetime
```

QoS choices: telemetry is QoS 0 (high frequency, loss-tolerant — a missed sample is covered by the next one 2 s
later and drives the freshness/health model, not a hard failure). Commands, acks and leases are QoS 1
(at-least-once, hub-side dedup by `batch_id`/`seq`). Stop is QoS 1 **and retained**, so a hub connecting mid-stop (or
reconnecting after comms loss) immediately receives the active stop state — this is the mechanism that makes stop
"never lost," matching the security architecture's stop-authority design intent at MVP-S scale.

### 6.4 `health`

Runs inside the `og-settle` process (§1.2) — deliberately not inside `og-guardian`, so the guardian's signing path
never depends on the health evaluator, and not inside `og-safestop`, so `og-safestop`'s independence (K8) does not
depend on `health` either. Responsibilities:

- **Heartbeats.** Every process (all 7: `feeds`, `engine`, `guardian`, `safestop`, `sim`, `settle`, `api`) publishes a
  heartbeat row (`heartbeat` table, upsert) every `health.heartbeat_interval_s` (5 s): `(process, pid, ts, status)`.
  `health` reads all 7 rows every cycle; a process is `DOWN` after `heartbeat_miss_threshold` (3) missed intervals
  (15 s).
- **Feed freshness.** Reads `feed_status` (§2.7); a product past its staleness threshold flips the corresponding
  degraded mode (§6.5).
- **Hub health.** Derived from `hub_state.last_seen_at`: `online` (≤ `fleet.telemetry_interval_s` × 2 = 4 s),
  `stale` (≤ `health.hub_stale_s` = 6 s... up to `health.hub_offline_s` = 30 s), `offline` (> 30 s). Health writes
  this classification back onto `hub_state.health` so `fleet.capability()` can exclude stale/offline hubs.
  `fault` is set directly by a hub-reported `fault_code`, independent of the timing classification.
- **Cycle latency.** Reads `og_control_tick_duration_seconds` (below) each cycle; alerts if p99 over the last 5
  minutes exceeds the budget (§10).
- **Alert rules** (a trimmed `ALR-*` set for MVP-S): feed stale (warning) / feed LGV exhausted (critical); any
  process down (critical); hub offline ratio > 5% in a zone (warning) / > 20% (critical); cycle p99 > 500 ms for 3
  consecutive cycles (warning); guardian verdict timeout rate > 1% (critical); reserve-breach counter > 0
  (critical, page-equivalent — logged prominently, MVP-S has no real pager). Alerts insert into `alert` and appear on
  the Health screen and Control room (§8).

```sql
CREATE TABLE heartbeat (
  process TEXT NOT NULL, pid INT NOT NULL, ts TIMESTAMPTZ NOT NULL, status TEXT NOT NULL DEFAULT 'ok',
  PRIMARY KEY (process)
);
CREATE TABLE alert (
  id BIGSERIAL PRIMARY KEY, rule TEXT NOT NULL, severity TEXT NOT NULL, -- warning|critical
  summary TEXT NOT NULL, detail JSONB, opened_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  cleared_at TIMESTAMPTZ, acked_by TEXT
);
```

### 6.5 Degraded modes and their effect on the engine

| Trigger | Degraded mode | Effect |
|---|---|---|
| A feed (ERCOT price/load) crosses `STALE` | **No new commitments** | `selector` gate refuses to admit or re-select using that series; existing commitments continue to deliver (the lock is unaffected by feed staleness — only *new* selection is frozen); UI banner "prices stale, Ns" |
| `engine` process down (heartbeat miss) | **Hold, then local autonomy** | `guardian` stops issuing new batches (nothing to sign); hubs' leases expire after `fleet.lease_ttl_s` (30 s) and each falls to its local schedule (§4.4); no reserve or one-buyer violation is possible in this mode because no new commands are issued at all |
| `guardian` process down / verdict timeout | **Hold** | `engine` still proposes batches but none are ever signed; same lease-expiry → local-autonomy path as above, on the same 30 s timer |
| A hub goes `stale`/`offline` | Excluded from `fleet.capability()` | Selector/allocator simply do not plan on that hub's capacity; on return, no probation logic in MVP-S (kept simple; `R2` adds trust scoring) |
| `sim`'s simulated SCADA for a bank goes silent | `DIST_DEFERRAL` open-loop fallback | The PI loop (02a) holds its last output and switches to the day-ahead schedule rather than integrating on stale feedback (mirrors the A3 "hold, then schedule" rule) |

### 6.6 Prometheus `/metrics`

Each process exposes `/metrics` on its own port (§1.2 table) via `prometheus_client`, scraped by an optional local
Prometheus (not required for the demo itself, but wired for the performance-evidence requirement, §10). No
`hub_id` label anywhere (cardinality discipline carried down from the production spec even at MVP-S scale, since it
costs nothing to do correctly from day one).

| Metric | Type | Labels | Meaning |
|---|---|---|---|
| `og_control_tick_duration_seconds` | histogram | `phase` (select/reserve/allocate/guard) | Per-phase and total RT cycle latency |
| `og_control_ticks_total` | counter | `outcome` (on_time/late) | Cycle count |
| `og_command_ack_latency_seconds` | histogram | — | Time from batch issue to ack received |
| `og_commands_total` | counter | `result` (acked/rejected/expired) | |
| `og_hubs` | gauge | `health` (online/stale/offline/fault) | Current hub count per state |
| `og_telemetry_fresh_ratio` | gauge | `zone` | Fraction of hubs with telemetry ≤ 4 s old |
| `og_feed_age_seconds` | gauge | `source`, `product` | Age of the latest accepted value |
| `og_feed_breaker_open` | gauge (0/1) | `source` | |
| `og_guardian_verdicts_total` | counter | `outcome` (signed/vetoed/timeout) | |
| `og_reserve_breaches_total` | counter | — | Must stay 0 (A10) |
| `og_double_sold_kwh_total` | counter | — | Must stay 0 (A10) |
| `og_process_up` | gauge (0/1) | `process` | From `health`'s heartbeat table |
| `og_eventloop_lag_seconds` | histogram | `process` | asyncio loop lag, same convention as the production spec |

---

## 7. API

FastAPI app (`opengrid.api.app`), bound to `127.0.0.1:8080`, reached only through the Apache reverse proxy (§9.4).
Two roles, `operator` and `viewer`, carried as an HTTP Basic Auth identity mapped to a role by Apache
(`AuthUserFile` groups, §9.4) and re-asserted in FastAPI via a dependency that reads the `Authorization` header
Apache forwards unchanged (loopback trust boundary — Apache is the only thing that can reach `api`). `viewer` can
read every GET/SSE endpoint; only `operator` can call a mutating endpoint, and safe-stop / manual command require a
second explicit confirmation step (§7.3).

### 7.1 REST endpoints

| Method & path | Role | Purpose |
|---|---|---|
| `GET /og/api/health` | viewer | Aggregated health: processes, feeds, hub summary, current degraded modes |
| `GET /og/api/fleet/hubs?zone=&bank=&health=` | viewer | Paged hub table for Fleet screen |
| `GET /og/api/fleet/hubs/{hub_id}` | viewer | Hub drill-down: state, lease, last command, telemetry sparkline |
| `GET /og/api/fleet/banks/{bank_id}` | viewer | Bank aggregate: kVA load vs rating, member hubs |
| `GET /og/api/markets/series?product=&series_key=&from=&to=` | viewer | Feed observation window (for charts) |
| `GET /og/api/forecast?series_key=&kind=` | viewer | P10/P50/P90 array, 96 steps |
| `GET /og/api/dispatch/opportunities` | viewer | Opportunity pipeline (offered/committed/delivering/fulfilled) — owned by 02a, exposed here |
| `GET /og/api/dispatch/plan/latest` | viewer | Latest selector plan and its rationale (value terms) |
| `GET /og/api/ledger/{bank_id}/timeline` | viewer | Reservation timeline for a bank |
| `GET /og/api/profitability/summary?service=&day=` | viewer | Revenue/cost/margin, LP-vs-baseline, forgone upside |
| `GET /og/api/billing/invoice-lines?from=&to=&format=csv` | viewer | Insert-only invoice lines; `format=csv` streams a download |
| `GET /og/api/trace/events?class=&from=&to=` | viewer | Trace explorer query |
| `POST /og/api/trace/verify` | viewer | Runs `trace.verify(range)`, returns pass/fail + first broken link if any |
| `POST /og/api/fleet/command` | operator | Manual command proposal: `{bank_id or hub_id, p_kw_setpoint, reason}` → goes to `guardian` for signing, **step 1 of 2** |
| `POST /og/api/fleet/command/{proposal_id}/confirm` | operator | **Step 2**: explicit confirm, only then does `guardian` sign and publish |
| `POST /og/api/safestop` | operator | Scoped stop proposal: `{scope, scope_id, reason}` — **step 1 of 2** |
| `POST /og/api/safestop/{proposal_id}/confirm` | operator | **Step 2**: confirm → `guardian`/`safestop` publishes the retained stop |
| `POST /og/api/safestop/{scope}/{scope_id}/release` | operator | Clears the retained stop (single step; release is inherently reversible, engage is not) |
| `POST /og/api/scenario/{name}` | operator | Triggers a scenario-injector event on `sim` (§5.5) |

All mutating endpoints go through `guardian` for anything touching a hub/bank (per the delivery plan: "operator
commands and safe stop go through the guardian with two-step confirmation" — this doc's contribution is the API
shape; guardian's verification logic is owned by 02a). The two-step pattern is: **POST proposal → server returns a
`proposal_id` and a human-readable summary → UI shows a confirm dialog → POST confirm with that `proposal_id`**;
a proposal not confirmed within 60 s expires.

### 7.2 SSE streams

| Path | Role | Payload | Cadence |
|---|---|---|---|
| `GET /og/api/stream/fleet` | viewer | Sampled hub state deltas (health, SoC bucket, P) for the map/table | On change, coalesced to ≤ 1/s |
| `GET /og/api/stream/control-room` | viewer | Price/load ticker, fleet MW/MWh, active commitment count, net margin, invariant counters, open alerts | 2 s |
| `GET /og/api/stream/dispatch` | viewer | Opportunity pipeline transitions, latest plan diff, real-time grants/substitutions | 2 s |
| `GET /og/api/stream/health` | viewer | Process up/down, feed freshness, hub health histogram, cycle latency, degraded mode | 2 s |
| `GET /og/api/stream/alerts` | viewer | New/cleared alerts | On change |

Every SSE endpoint sends a comment heartbeat (`: keepalive`) every `api.sse_heartbeat_s` (15 s) so Apache's proxy
timeout and browser reconnect logic behave predictably (§7.2 of the UI spec's "throttling and reconnect" pattern,
adopted at MVP-S scale).

### 7.3 Two-step confirmation detail

Step 1 validates shape and role only (cheap); step 2 is where `guardian` actually runs its checks (reserve, ramp,
lease/epoch, G-19 commitment lock for anything that would reduce a committed allocation) and either signs or
vetoes. A veto returns `409` with the guardian's reason code, shown verbatim in the UI — never silently retried.

---

## 8. UI: 7 screens + scenario panel

Server-rendered HTMX + Alpine.js + ECharts + Leaflet, all from CDN, no build step, mounted under `/og/`. Every
screen's live regions are `hx-ext="sse"` divs pointed at the SSE paths in §7.2; static shell + first paint is a
normal HTMX `GET`. This is a deliberately lean subset of the 20-screen production UI spec
(`04-ui/01-ui-ux-specification.md`) — MVP-S ships the delivery plan's 7 screens only; UI-CUS, UI-SVC, UI-SCD,
UI-ADM, UI-SIM (as a full lab), UI-INS, UI-GOP, UI-OOB and the AI-related panels are `MVP-J`/`R2`.

| # | Screen | Route | Widgets | Data source | Refresh | Actions |
|---|---|---|---|---|---|---|
| 1 | Control room | `/og/` | Leaflet fleet map (colored by SoC/health); live price/load/wind/solar ticker; fleet MW/MWh gauge; active commitment count; today's net margin; invariant counters (reserve breaches, double-sold kWh — both must read 0); open alerts strip | `GET /og/api/stream/control-room`, `GET /og/api/fleet/hubs` (initial map paint) | 2 s (SSE) | Ack an alert |
| 2 | Fleet monitoring & control | `/og/fleet` | Bank/hub table (sortable, filterable by zone/bank/health); hub drill-down panel (SoC, P, health, lease, last command, 15-min telemetry sparkline via ECharts); manual command form; scoped safe-stop control | `GET /og/api/fleet/hubs`, `/hubs/{id}`, `/banks/{id}`, `stream/fleet` | 2 s (SSE), on-demand for drill-down | `POST fleet/command` (+confirm); `POST safestop` (+confirm); `POST safestop/.../release` |
| 3 | Dispatch & commitments | `/og/dispatch` | Opportunity pipeline (Kanban: offered → committed → delivering → fulfilled); per-bank ledger timeline (ECharts Gantt-style); latest selector plan panel with its value terms; real-time grants/substitutions feed | `GET /og/api/dispatch/opportunities`, `/plan/latest`, `/ledger/{bank}/timeline`, `stream/dispatch` | 2 s (SSE) | None (read-only in MVP-S; admission is via `sim`'s scenario injector for the demo) |
| 4 | Markets & feeds | `/og/markets` | Price/load/wind/solar time series (ECharts); freshness/source-status table (from `feed_status`); forecast quantile band chart (P10/P50/P90) | `GET /og/api/markets/series`, `/forecast` | 30 s poll (not SSE — market data does not need 2 s) | None |
| 5 | Health | `/og/health` | Process status grid (7 processes); feed freshness table; hub health histogram (online/stale/offline/fault) by zone; cycle latency chart (p50/p99); alert list; current degraded mode banner | `GET /og/api/health`, `stream/health`, `stream/alerts` | 2 s (SSE) | Ack alert |
| 6 | Profitability | `/og/profitability` | Per-service/obligation/day table: revenue, energy cost, degradation, penalty, net margin; LP-vs-rule-baseline comparison chart; forgone-upside-from-lock line item | `GET /og/api/profitability/summary` | 30 s poll | Filter by service/day |
| 7 | Billing & audit | `/og/billing` | Invoice line table with CSV export; M&V performance summary; trace explorer (filter by class/time); chain-verify button with pass/fail + broken-link locator | `GET /og/api/billing/invoice-lines`, `/trace/events`, `POST /trace/verify` | On demand | CSV export; run chain verify |
| — | **Scenario panel** (overlay, all screens) | `/og/scenario` (Alpine.js modal) | Buttons for each §5.5 scenario | `POST /og/api/scenario/{name}` | — | Trigger scenario; shows the resulting trace events inline |

**Design system, kept minimal for MVP-S**: dark control-room theme, one categorical palette for the 5 services
(`HOME`, `ERCOT_ENERGY`, `ERCOT_AS`, `DIST_DEFERRAL`, `PARTNER_CAPACITY`), colour-blind-safe status colours for
online/stale/offline/fault (green/amber/grey/red with an icon, not colour alone) — the one accessibility rule kept
from the full UI spec's §8.3, because it is nearly free and directly affects operator safety.

---

## 9. Deployment

### 9.1 Target, existing provisioning and packages

Debian 13, `basepower` (192.168.5.35). The host is already provisioned for this project: venv at
`/opt/opengrid/venv` (Python 3.13), application directory `/opt/opengrid`, runtime/data directory
`/var/lib/opengrid`, logs at `/var/log/opengrid`, secrets at `/etc/opengrid` (§1.5). **There is no `sudo` on this
host**; anything that needs elevated rights (installing packages, writing `/etc/systemd/system/*`, changing file
ownership) is done once by an administrator directly as root or via `runuser -u opengrid -- <cmd>` to run as the
service account — no orchestrator script or API path ever invokes `sudo`. Package install (root, one-time, existing
Apache/MariaDB/mail stack untouched):

```
apt-get install -y postgresql postgresql-contrib mosquitto mosquitto-clients python3.13-venv
```

Python deps already installed into `/opt/opengrid/venv`: `fastapi`, `uvicorn[standard]`, `pydantic>=2`, `asyncpg`,
`paho-mqtt`, `highspy`, `numpy`, `pandas`, `httpx`, `prometheus-client`, `PyNaCl` (Ed25519), `jinja2` (HTMX server
rendering).

### 9.2 Postgres and Mosquitto

- **Postgres 17.11**, listening on `127.0.0.1:5432`. Two databases already exist, both owned by role `opengrid`:
  **`og`** (the running system, used by every process per `[postgres].database`, §1.4) and **`og_test`** (CI and the
  integration/e2e test suites — `tests/` never touches `og`). A single application role `opengrid` is used by all 7
  processes for MVP-S (a single-role model is acceptable at this scale; per-process roles are `R2`).
  `listen_addresses = 'localhost'`; `max_connections = 100` (7 processes × pool_max 8 = 56, plus `og_test` and
  interactive headroom, §11.5). `shared_buffers` sized to ~1.5 GB given the 16 GB shared-host budget (§10 memory
  table).
- **Mosquitto**, `listener 1883 localhost`, `allow_anonymous false`, password file (`mosquitto_passwd`) with five
  entries — `og_engine`, `og_guardian`, `og_safestop`, `og_sim`, `og_api` — and an ACL file already scoped to the
  `og/v1/` root's `tel/#`, `ack/#`, `cmd/#`, `stop/#`, `lease/#`, `scada/#`, `scenario/#` prefixes (§6.1–§6.2). This
  spec's topic tree is designed to fit that existing ACL without requesting any change to it: e.g. `og_sim` publishes
  `og/v1/tel/#` and `og/v1/scada/#`, subscribes `og/v1/cmd/#`, `og/v1/stop/#`, `og/v1/lease/#` and
  `og/v1/scenario/#`; it never publishes `og/v1/cmd/#` or `og/v1/stop/#`. `og_safestop` publishes only `og/v1/stop/#`
  and subscribes to nothing — it never needs `og/v1/cmd/#` or telemetry to do its job, keeping its blast radius on
  the broker as small as its process boundary.

### 9.3 systemd units (one per process, 7 total)

```ini
# /etc/systemd/system/og-engine.service (pattern repeated for og-feeds, og-guardian, og-safestop, og-sim, og-settle, og-api)
[Unit]
Description=OpenGrid Orchestrator - engine
After=network.target postgresql.service mosquitto.service
Requires=postgresql.service mosquitto.service

[Service]
Type=simple
User=opengrid
Group=opengrid
WorkingDirectory=/opt/opengrid
EnvironmentFile=/etc/opengrid/secrets.env
EnvironmentFile=-/etc/opengrid/api_keys.env    # '-' prefix: optional per unit (only feeds needs it; see note below)
ExecStart=/opt/opengrid/venv/bin/python -m opengrid.engine.main --config /opt/opengrid/config/orchestrator.toml
Restart=always
RestartSec=2
MemoryMax=2G
MemoryHigh=1600M
NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths=/var/log/opengrid /var/lib/opengrid
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

Per-process `MemoryMax` (sums to comfortably under the 16 GB shared budget, §10): `feeds` 512M, `engine` 2G,
`guardian` 512M, `safestop` 256M (a small, dependency-free process by design — the smallest footprint of the seven,
per K8), `sim` 4G (2,000 asyncio hub tasks + MQTT client), `settle` 512M, `api` 1G. `Restart=always` on every unit is
the availability mechanism the chaos scenario (§5.5 "process kill") exercises. Only `og-feeds.service`
loads `/etc/opengrid/api_keys.env` (the ERCOT/EIA/NWS credentials); the other six units load only
`/etc/opengrid/secrets.env` (DB, MQTT, guardian signing, basic-auth hashes) — least privilege at the
`EnvironmentFile` level, at no extra deployment cost since both files are already provisioned. Since there is no
`sudo` on the host, unit files themselves are installed once by an administrator (`cp` to
`/etc/systemd/system/`, `systemctl daemon-reload`) as root; day-to-day restarts by the `opengrid` account use
`runuser -u opengrid -- systemctl --user ...` only if user units are adopted later — for MVP-S, restarts are issued
by whoever holds root on the box (the same administrator who deploys), not by the API or by `opengrid` itself.

### 9.4 Apache reverse-proxy snippet

```apache
# /etc/apache2/conf-available/og-orchestrator.conf
<Location /og/>
    ProxyPreserveHost On
    ProxyPass        http://127.0.0.1:8080/og/
    ProxyPassReverse http://127.0.0.1:8080/og/
    RequestHeader unset Authorization
    RequestHeader unset X-Forwarded-User

    AuthType Basic
    AuthName "OpenGrid Orchestrator"
    AuthUserFile /etc/apache2/og-orchestrator.htpasswd
    AuthGroupFile /etc/apache2/og-orchestrator.groups
    Require group operator viewer

    RequestHeader set X-OG-Role expr=%{env:REMOTE_USER_GROUP}
</Location>
<Location /og/api/fleet/command>
    Require group operator
</Location>
<Location /og/api/safestop>
    Require group operator
</Location>
```

New `htpasswd` and group files only; existing Apache vhosts, TLS certs and every other `<Location>` are untouched.

### 9.5 Backups

Nightly `pg_dump --format=custom og > /var/lib/opengrid/backups/og-$(date +%F).dump`, run as the `opengrid` account
(`/var/lib/opengrid` is already writable by it; no root needed for the dump itself) via a systemd timer
(`og-backup.timer`, 03:00 local), 7 daily copies retained, rotated by a small script (`deploy/scripts/backup.sh`).
`og_test` is never backed up (disposable). Mosquitto and hub state are not backed up separately — Postgres is
authoritative; `sim`'s in-memory hub state rebuilds from `hub`/`hub_state` on restart.

### 9.6 Deploy and rollback

- **Source control access.** The repository is on Gitea at `calisto` (192.168.5.22), reached over SSH port 2222
  through the host alias `calisto` already defined in `/opt/opengrid/.ssh/config` — every git command on
  `basepower` uses `git@calisto:opengrid-orchestrator.git` (the alias resolves host, port and identity file), never
  a raw IP or the default port 22.
- **Deploy**: tag a release in Gitea (`v0.1.0`), `deploy/scripts/deploy.sh <tag>` on `basepower`, run as `opengrid`
  (no root needed — `/opt/opengrid` is owned by that account), does `git fetch --tags && git checkout <tag>`,
  `venv/bin/pip install -e .`, `deploy/scripts/migrate.sh` (applies any new `migrations/*.sql` against the `og`
  database, not yet in a `schema_migrations` tracking table, forward-only for MVP-S — no down-migrations are
  written; a bad migration is fixed forward), then a **root-run** `systemctl restart og-feeds og-engine og-guardian
  og-safestop og-sim og-settle og-api` in that order (feeds/sim first so engine has data to read on its first cycle;
  `og-safestop` restarts independently of `og-guardian` and never blocks on it) — the restart step is the one part
  of deploy that needs the administrator's root session, consistent with §9.1's no-`sudo` constraint.
- **Rollback**: `deploy/scripts/rollback.sh <previous-tag>` does the same `git checkout` to the previous tag and
  service restart; **no automatic down-migration** — because MVP-S retention/pruning is append-mostly and the
  demo window is 30 hours, the accepted rollback risk is "restart on old code against a slightly ahead-of-it
  schema," which every MVP-S migration is written to tolerate (additive columns/tables only, never a rename or drop
  in a single migration).
- `telemetry` partitions: a daily cron (`deploy/scripts/migrate.sh` also creates tomorrow's partition idempotently)
  creates `telemetry_YYYY_MM_DD` ahead of midnight so the 2-s `COPY` writer never blocks on partition creation.

---

## 10. Non-functional budgets and how each is measured

| Budget | Target | How measured |
|---|---|---|
| RT cycle p99 | < 500 ms at 2,000 hubs (measure, don't assume, at 10,000) | `og_control_tick_duration_seconds` histogram; `og-settle`'s `health` package aggregates p99 over a rolling 5 min window; exported on the Health screen; perf test suite (`tests/perf/`) runs a scripted 30-min soak at 2k and reports the 10k number labelled "measured, not met" if it misses |
| UI refresh | ≤ 2 s from event to screen | SSE cadence is fixed at 2 s (§7.2) for every live screen; a synthetic click-to-photon test (`tests/e2e/ui_latency.py`) measures wall-clock from a `sim` telemetry publish to the corresponding SSE frame reaching a test client |
| Ingest freshness | Per §2.6 thresholds (price ≤ 600 s, load ≤ 1800 s, wind/solar ≤ 10800 s) | `og_feed_age_seconds` gauge; Health screen "feed freshness" table; alert on breach |
| Availability | Survive killing any one of the 7 processes, including `og-safestop` alone | Chaos test (`tests/chaos/kill_each_process.py`) kills each unit in turn during a live run, asserts: (a) `systemd` restarts it within `RestartSec`, (b) hubs never exceed lease TTL without falling to local autonomy, (c) zero reserve breaches or double-sold kWh across the whole run, (d) the killed process's own state (e.g., engine's in-flight plan) rebuilds from Postgres without operator intervention, (e) killing `og-guardian` or `og-engine` alone does not impair `og-safestop`'s ability to engage a scoped stop (K8) |
| Memory per process | Within 16 GB shared with mail/web (existing services use ~2 GB observed; budget the remaining ~12–14 GB) | `MemoryMax` per unit (§9.3) sums to ≤ 9 GB, leaving > 5 GB headroom over the existing footprint; `node_exporter`-equivalent (`/proc/meminfo` scrape or manual `free -h` check during the perf soak) confirms no OOM kill of Apache/Postfix/MariaDB during the 10k stretch test |

Every budget row has a concrete artifact (a metric, a test file, or both) — none of them is asserted without a
measurement path, per the delivery plan's "measured, not asserted" observability principle.

---

## 11. Acceptance mapping (A1–A11 → section → source spec)

| # | Acceptance (delivery plan §1) | Where in this document | Source spec section |
|---|---|---|---|
| A1 | Live feeds, freshness badges | §2 (`feeds`), §6.5 (degraded modes), §8 screen 4 | `04-external-data-integration.md` §2, §4, §6 |
| A2 | Fleet monitoring, 2,000 hubs, map/bank/hub drill-down | §4 (`fleet`), §5 (`sim`), §8 screen 2 | `03-decision-engine.md` §12.3; first-principles review §5.1 (P1 physics) |
| A3 | Fleet control, signed commands, verified, acked | §6.3 (schemas), §7.1/§7.3 (two-step + guardian), §4.3 (lease/epoch/sig) | `02-security-architecture.md` §7 (command integrity), §6 (guardian) |
| A4 | Dispatch engine, LP at gates, commitment lock, 2 s allocation | §6.5 (degraded modes preserve the lock); engine internals owned by 02a | `03-decision-engine.md` §6, §8; first-principles review §3 (K13/FR-ARB-014), §5 |
| A5 | Five services | Owned by 02a (`contracts`/`selector` profiles); this doc's `feed_obs`/`forecast` and MQTT contract are the shared substrate every profile reads/writes | `03-decision-engine.md` §2.5–§2.6 |
| A6 | Health, heartbeats, staleness, hub health, cycle latency, alerts, degraded modes | §6 (`health`) entire | `06-platform-and-operations.md` §5 (observability), §3.1 (SLOs) |
| A7 | Profitability | §7.1 endpoint, §8 screen 6 (read models); computation owned by 02a `settle` | `03-decision-engine.md` §10 |
| A8 | Billing & M&V, CSV export | §7.1 endpoint, §8 screen 7 | `03-decision-engine.md` §10.6 |
| A9 | Audit & track record, hash-chained trace, retention per class, checkpointed pruning | §1.6 (`trace` library), §7.1 (`/trace/verify`), §8 screen 7 | first-principles review §3.3 (trace enforcement); `02-security-architecture.md` §12 |
| A10 | Invariants: 0 reserve breaches, 0 double-sold kWh, 0 commitment switches without a reason | §6.6 metrics (`og_reserve_breaches_total`, `og_double_sold_kwh_total`), §8 screen 1 counters; enforcement owned by 02a (ledger, guardian G-19) | first-principles review §3.2 (K13), §5.1 (C24) |
| A11 | Non-functional: RT p99, UI refresh, availability, nightly `pg_dump` | §10 entire, §9.5 (backup) | `06-platform-and-operations.md` §3.1 (SLOs), §1.8 (resource budget) |

---

## 12. Function ownership (no duplication)

**Rule.** Every pure function used by more than one MVP-S module has exactly one implementation, in
`src/opengrid/core/` (`og.core`), and every other module imports it rather than re-deriving it. A module may keep a
thin wrapper that adapts `og.core`'s signature to its own types, but the arithmetic/logic itself lives in one place.
This is enforced mechanically by `TS-01-07` (`04-mvp-s-test-plan.md` §3.1), a static/import-graph check that fails
the build if a formula `og.core` owns is found reimplemented elsewhere.

| Function | Single owner (`og.core.*`) | Consumers (import, never reimplement) |
|---|---|---|
| Hub/bank physics: SoC step ($e_{t+1}=e_t+\eta_c p^c\Delta t-p^d\Delta t/\eta_d$), P/E/kVA capability, ramp-limit application | `og.core.physics` | `sim` (forward-simulates real hub behavior with it), `fleet` (derives `capability(bank,t)` from the same step function applied to reported state — see below), `allocator` (reads `fleet.capability`, never recomputes physics itself) |
| Bank/feeder capability and headroom (`capability(bank,t)`, the recharge-headroom formula) | `og.core.capability` | `fleet.capability(bank,t)` (the module-level entry point 02a's `selector`/`allocator` call) **and** `guardian` G-03's recharge-headroom check call the *same* `og.core.capability.recharge_headroom()` function on their own respective inputs. An earlier draft of §6.1 in `02a` described G-03's formula as one that "matches the allocator's" — implying two hand-kept-in-sync implementations. That wording is retired: G-03 calls `og.core.capability.recharge_headroom()` directly, so it cannot drift from the allocator's number because it is not a second number, it is the same function call on independently-read inputs (see the independence note below) |
| Limit/envelope checks (P/kVA/ramp bound tests, reserve-floor test) | `og.core.limits` | `allocator` (S1–S7 planning-time checks), `guardian` (G-01…G-06 signing-time checks). **Independence rule:** the guardian's check is independent not because it re-derives the limit formula differently, but because it calls the same `og.core.limits` functions on its **own, independently-read inputs** — hub-reported telemetry and the ledger version it reads itself, not the allocator's in-memory estimate. A second, differently-written implementation of the same physical limit would be a second thing that can be wrong in a different way; calling the same tested function on independently-sourced data is what "independent check" means for MVP-S (review §3.3's "enforce once, verify independently," read as *one formula, two data paths*, not *two formulas*) |
| Product-rule quantity rounding (`min_qty`/`increment`/`block` → the selector's variable-kind mapping, §3.6 of `02a`) | `og.core.product_rules` | `selector` (builds the LP/MILP variable), `contracts` module (admission-time feasibility pre-check uses the same rounding to reject an obviously-infeasible call before it reaches the solver) |
| Ed25519 sign/verify and the command envelope (canonical JSON serialization, signature check) | `og.core.crypto` | `guardian` (signs), `safestop` (signs its distinct stop-only key the same way), `sim` hub tasks (verify), any future real hub |
| Trace hashing (JCS canonicalization + SHA-256) and chain verify | `og.core.trace_hash` | `trace` (the only module that *writes* `trace` rows, via `append()`/`verify()` in `02a` §8.2–8.4, both built on `og.core.trace_hash`); `guardian`'s G-14 check calls `og.core.trace_hash` only indirectly — it calls `trace.exists_preimage(command_batch_id)` (a `trace`-module function, itself built on `og.core.trace_hash`) to confirm a pre-image row exists before signing. Guardian never recomputes or re-verifies a hash itself; it asks `trace`, the single owner, whether the write already happened |
| Time/interval utilities (15-min slot alignment, UTC↔America/Chicago, monotonic epoch/seq comparison) | `og.core.time` | Every module that touches `interval_start`/`interval_end` or epoch/seq (`selector`, `ledger`, `allocator`, `guardian`, `sim`, `settle`, `trace`) |
| Pydantic contracts (wire + row shapes) | `og.core.contracts` (re-exported as `opengrid.contracts` for backward compatibility with §1.1's repo layout) | All 7 processes |

**Staleness/freshness comparison** (the one place two modules independently decide "is this too old?"): `feeds`
(feed staleness, §2.6) and `health` (hub staleness, §6.4) compare *different* data against *different* thresholds
for *different* purposes, so they are not the same function — but the comparison itself
(`age = now - last_seen; stale = age > threshold`) is one function, `og.core.time.is_stale(last_seen, threshold)`,
called by both with their own threshold. Neither module hand-rolls its own age arithmetic.

**M&V baseline vs. profitability.** There is no separate "profitability module" in MVP-S — profitability (§7.4 of
`02a`) is a computation inside `settle` that reads the M&V baseline `settle` itself already computed and posted to
`meter_interval`/`performance` (§7.1–7.2 of `02a`); it does not recompute a baseline. `settle` is therefore the sole
owner of both M&V and profitability math; the UI's Profitability screen (§8 here) only reads `pnl` rows, it does not
compute anything.

**`sim` and the fleet twin, restated.** `sim` (test harness) is the only module that *simulates* hub physics forward
in time (`og.core.physics`, applied to a fictional hub for the demo). `fleet` (twin) never simulates — it only
stores and aggregates whatever state a hub (real or `sim`) reports, and calls `og.core.physics`'s capability
functions (not its SoC-step function) to derive `capability(bank,t)` from that reported state. The two never
disagree on the SoC-step formula because only one of them (`sim`) ever runs it forward; `fleet` only ever reads a
result.

---

## Open points

1. **MQTT transport security.** §6.1 runs Mosquitto plain on loopback for MVP-S, deferring the production mTLS
   device boundary (`02-security-architecture.md` §7) entirely. Acceptable only because `sim` and every subscriber
   are on the same host; must be revisited before any real hub or off-node `agent-sim` host connects (the security
   architecture's §19.5 "monitored scope gate" already flags this class of risk).
2. **Single Postgres role for all 7 processes** (§9.2) trades least-privilege for build speed; per-process roles and
   row-level security are `R2`.
3. **`forecast`'s quantile-persistence method** has no back-tested error bound in MVP-S; `og_forecast_mape_ratio`-style
   tracking (present in the production metrics catalogue) is deferred until there is a real history to score against.
4. **10,000-hub target** is a stretch measured honestly, not a commitment; §5.3 and §10 both say so explicitly. If
   `sim`'s asyncio model can't reach 10k within the 4 GB `MemoryMax`, the fallback is to report the largest scale
   actually reached rather than raise the memory cap into the shared host's budget.
5. **No AS or SCED-cycle products** beyond the 5 in §2.2 — MVP-S's `ERCOT_AS` service (A5) uses only the daily DAM
   MCPC for capacity-hold value; real-time AS deployment tracking is `MVP-J`.
6. **Scenario-panel process-kill action** cannot self-execute: the host has no `sudo` (§9.1) and `opengrid` cannot
   `systemctl kill` its own units, so §5.5's kill scenario is operator-narrated (the UI shows which unit to kill and
   the expected recovery sequence; a human runs `systemctl kill` over SSH as root). Automating this within the
   demo's constraints would need either a narrow root-owned helper the API can invoke, or a systemd user-unit
   delegation — neither exists today and both are out of scope to build by Saturday.
7. **UI role separation is Apache-enforced only** (§7, §9.4); `api` trusts the `X-OG-Role` header from loopback
   Apache. This is safe only as long as nothing but Apache can reach `127.0.0.1:8080`, which the systemd
   `ReadWritePaths`/bind-address configuration (§9.3, `api.bind_host = "127.0.0.1"`) enforces but does not
   independently verify at runtime; a startup self-check that refuses to bind to a non-loopback address would close
   this gap cheaply and is recommended for the Phase 1 foundation work.
8. **`settle`'s read models** (§7.1 profitability/billing endpoints) assume 02a's table shapes (`meter_interval`,
   `performance`, `invoice_line`, `pnl`) are frozen before WS7 (UI) starts on screens 6–7; any late change to those
   shapes is a cross-document risk this spec cannot resolve unilaterally.
