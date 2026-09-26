# OpenGrid Orchestrator — Build Brief (MVP-S)

Read this first, then `docs/team/NOTICES.md` (requirement changes). The design is fixed by the approved documents in
`docs/orchestrator/07-delivery/` in this repo. The lead's workspace path `D:\Projects\OpenGrid\docs\orchestrator\` is
the same set; the repo copy is what contributors use.

- `00-invariants.md` — canonical K1–K13 and guardian checks (G-01…G-20);
- `01-saturday-delivery-plan.md` — scope, acceptance A1–A11, rules in §7a;
- `02a-mvp-s-spec-engine.md` — engine: DDL, lifecycle, selector, ledger, allocator, guardian, safestop, settle, trace;
- `02b-mvp-s-spec-platform.md` — platform: feeds, forecast, fleet, MQTT, health, API, UI, deploy; §12 function
  ownership;
- `03-mvp-s-epics-stories.md` — stories and acceptance criteria;
- `04-mvp-s-test-plan.md` — tests (TS-nn-nn) to implement;
- `05-integrations-guide.md` — external interfaces.

Deadline: Saturday 2026-09-26 18:00 CT, deployed and working on 192.168.5.35.

## 1. Two independent products in one repository

| Product | Path | Package | Runtime venv (server) | Purpose |
|---|---|---|---|---|
| **Orchestrator (the live app)** | `orchestrator/` | `opengrid` | `/opt/opengrid/venv` | Runs against LIVE ERCOT/EIA/NWS and LIVE devices/SCADA over MQTT by default |
| **Integration simulators (external elements)** | `integration-sims/` | `ogsim` | `/opt/ogsim/venv` | Stand-ins for hubs, utility SCADA and market/data APIs. Used for testing and demos. They generate normal **and abnormal** events on demand |

- **They share no code.** `ogsim` must never import `opengrid`, and `opengrid` must never import `ogsim`. They meet only
  at the wire, defined language-neutrally in `interfaces/` (JSON Schema for MQTT messages, OpenAPI for HTTP). Both
  sides validate against those files.
- Switching the orchestrator between live and simulated sources is **configuration only**: feed base URLs, MQTT broker
  and topic root. It never involves a code path.
- **No duplicated functions inside the orchestrator.** Shared logic lives only in `opengrid.core` (see 02b §12). The
  guardian calls the same core functions on its own inputs. The CI check `orchestrator/tools/dupcheck.py` fails the
  build on violations. The simulators have their own code (they model external systems).

## 2. Multiple customers at once (first-class requirement)

At any time the orchestrator serves **several customers and obligations concurrently** across services (HOME,
ERCOT_ENERGY, ERCOT_AS, DIST_DEFERRAL, PARTNER_CAPACITY). Capacity is split per bank and interval by
agreements (commitments, product rules, priority/tiers) and market conditions (prices, forecasts). The commitment
lock (K13) protects each committed customer. New opportunities compete only for uncommitted headroom. Every module,
test and UI screen must handle N concurrent obligations, not one at a time.

## 2a. Requirement changes, 2026-09-25 evening (read before new work)

1. **Energy, continuously.** Capacity (kW) is not enough: remaining energy above reserve vs each committed delivery is
   checked every cycle (K1/K13). A missing or stale SoC means zero discharge. The guardian projects SoC over the
   command lease (G-01-ENERGY).
2. **Base hardware (confirmed):** 39.2 kWh / 11 kW per unit; 20% of homes have 2 units (78.4 kWh / 20 kW); reserve
   20%; banks are ~600 kVA feeder segments. Use these in all configs, fixtures and docs.
3. **Service tailoring (MVP-S+):** each customer/service has a ServiceProfile (control primitive, target quantity,
   response, tolerance, M&V, settlement). Pipeline AC mitigation, data center and arbitrage are distinct services.
4. **Power quality (MVP-S+):** a per-customer PowerQualityEnvelope (phase, V, I, f, PF, THD), an inverter
   imperfection model, PQ-aware dispatch, guardian PQ checks (proposed K14), per-phase telemetry, and simulator PQ
   anomalies.
   Spec (draft, pending owner approval): `docs/orchestrator/07-delivery/06-service-profiles-and-power-quality.md`. Do
   not implement 3–4 before approval; design your work so it will accept a ServiceProfile later (don't hard-code
   service behaviour).

## 3. Abnormal events (integration sims)

Every simulator exposes anomaly injection through one control plane (`ogsim.control`: REST + small web UI + CLI, and
scenario YAML files). An anomaly has
`{id, target (sim/asset/zone/bank/hub), type, params, start, duration}`, and every injection is logged with its id,
so QA can correlate it with the orchestrator's response (trace, alerts, UI).

**The simulators are autonomous.** Once started, they run continuously on their own, like the real external
systems: hubs stream telemetry, SCADA reports bank load, and the market APIs serve prices. On top of that there are
three ways to inject anomalies, which can be combined:

1. **Autonomous random mode** (on by default, switchable per simulator from the control plane):
   - each sim draws random anomalies from its catalogue as a Poisson process;
   - it has a configurable rate per anomaly type (e.g., `rate_per_hour`), severity ranges, duration ranges, and a
     max-concurrent cap;
   - there is a quiet-hours/pause switch and a fixed random seed for reproducible test runs;
   - intensity profiles: `calm`, `normal`, `stressed`, `chaos`.
2. **Manual injection** through the control plane: REST API, web UI at `/ogsim/`, and CLI.
3. **Scenario files** (YAML timed sequences), for repeatable QA runs.

Every anomaly, whatever its source (random, manual or scenario), is logged the same way with its id and source.

Minimum anomaly catalogue:

- **SCADA:** bank overload (kVA over rating), load spike/step, frozen value, bad quality flag, stale/no update,
  out-of-range value, oscillation, phase imbalance, breaker open/topology change, comms loss, utility instruction
  (limit / block / ESTOP), time skew.
- **Fleet (hubs):** hub offline, zone mass disconnect, not following commands (partial/none), inverter trip, SoC sensor
  drift, telemetry delay/burst, lease loss, clock skew, tampered/unsigned command (must be rejected), reserve-floor
  pressure (high home load).
- **Market/data APIs:** price spike (e.g., $5,000/MWh), negative price, AS price jump, HTTP 5xx outage, 429
  throttling, 401 key rejection (tests key rotation), stale posting (no new data), malformed payload, slow response,
  NWS extreme weather.

## 4. Directory ownership (one owner per path; do not edit others' paths — ask the lead instead)

| Agent | Owns |
|---|---|
| architect | `interfaces/`, `orchestrator/pyproject.toml`, `orchestrator/src/opengrid/{core,platform,trace}/`, `orchestrator/migrations/`, `orchestrator/config/`, `orchestrator/tests/conftest.py`, `orchestrator/tests/unit/{core,platform,trace}/`, `orchestrator/tools/`, `Makefile`, `README.md` |
| feeds | `orchestrator/src/opengrid/feeds/` + tests; history importer `feeds/history_import.py` |
| forecast | `orchestrator/src/opengrid/forecast/` + tests |
| contracts | `orchestrator/src/opengrid/contracts/` + tests (admission, lifecycle, product rules, re-nomination) |
| selector | `orchestrator/src/opengrid/selector/` + tests (LP/MILP + rule-selector fallback) |
| ledger | `orchestrator/src/opengrid/ledger/` + tests (K2 one-buyer, K13 commitment lock) |
| allocator | `orchestrator/src/opengrid/allocator/` + tests (2 s cycle, water-filling, substitution, PI) |
| engine | `orchestrator/src/opengrid/{fleet,engine}/` + tests (fleet twin; og-engine process wiring, gate scheduler) |
| guardian | `orchestrator/src/opengrid/guardian/` + tests |
| safestop | `orchestrator/src/opengrid/safestop/` + tests |
| settle | `orchestrator/src/opengrid/settle/` + tests |
| health | `orchestrator/src/opengrid/health/` + tests, and `orchestrator/src/opengrid/trace/pg_backend.py` + its tests (the Postgres TraceBackend and the retention pruning job) |
| api | `orchestrator/src/opengrid/api/` + tests |
| ui-a | `orchestrator/src/opengrid/ui/` base: `templates/base.html`, `templates/_partials/`, `static/`, `routes/__init__.py`; screens Control room, Fleet monitoring & control, Health |
| ui-b | `orchestrator/src/opengrid/ui/templates/{dispatch,markets,profitability,billing_audit}*.html` + `ui/routes/{dispatch,markets,profitability,billing_audit}.py` |
| sims | `integration-sims/src/ogsim/{fleet,scada,common}/` + tests |
| market | `integration-sims/pyproject.toml`, `integration-sims/src/ogsim/{market,control}/`, `integration-sims/scenarios/` + tests |
| deploy | `deploy/` (systemd units, Apache config, scripts) |
| merge | integration fixes across paths (after the builders finish), git on the server |
| qa / ui / security | `tests-e2e/`, `qa/` reports; no product code |

Process entry points (fixed): `python -m opengrid.feeds`, `opengrid.engine`, `opengrid.guardian`,
`opengrid.safestop`, `opengrid.settle`, `opengrid.api` → units `og-feeds`, `og-engine`, `og-guardian`,
`og-safestop`, `og-settle`, `og-api` (plus `og-sim-*` for `python -m ogsim.fleet`, `ogsim.scada`, `ogsim.market`,
`ogsim.control`). Each process reads `OG_CONFIG` (TOML path) and the env files.

## 5. How to run code

- **Local (unit and property tests, no DB/MQTT):**
  `D:\Projects\OpenGrid\opengrid-orchestrator\.venv\Scripts\python.exe -m pytest orchestrator\tests\unit -q`
  (the local venv has the orchestrator and sim dependencies). Local git is not installed.
- **Server (integration, DB, MQTT, live APIs):**
  `powershell -File tools\remote.ps1 -Ws <your-ws> -Cmd "<bash command>"`. The script:
  - syncs the whole repo to `/opt/opengrid/work/<ws>` and runs the command as user `opengrid`;
  - sets `OG_DB=og_t_<ws>` (your own Postgres database), `OG_MQTT_ROOT=ogtest/<ws>` (your own topic root; the
    ACL allows it for all og users) and the env files (`secrets.env`, `api_keys.env`).

  Workspaces: arch, feeds, fcst, ctr, sel, ledg, alloc, eng, guard, stop, settle, hlth, api, uia, uib, sims, mkt,
  merge, qa, ui, sec, rev1, rev2, prop. Use only yours.
- DB connection: host 127.0.0.1, port 5432, user `opengrid`, password `$OG_DB_PASSWORD`, database `$OG_DB`.
  MQTT: 127.0.0.1:1883, users `og_engine|og_guardian|og_safestop|og_sim|og_simctl|og_api`, passwords
  `$OG_MQTT_<USER>_PASSWORD`.
- Historical data (2.5 days, UTC): `/var/lib/opengrid/import/mariadb_history_signals.tsv` (asset, signal_type, unit,
  reading_time_utc, value, quality; includes `wholesale_price_mwh`, `grid_stress_price_mwh`, `substation_load_kw`,
  line flows) and `mariadb_history_assets.tsv`. Use them to seed the forecast and the market simulator's replay mode.
  Never connect to MariaDB.

## 5a. Code quality standard (hard gate: nothing merges without it)

Professional, clean, maintainable code. The reviewer agents reject anything below this bar.

- **Style and lint:** `ruff check` and `ruff format` clean (line length 110; rules E, F, W, I, B, UP, SIM, N, RUF, ASYNC,
  S). No unused code, no commented-out code, no `print` (use the JSON logger), no bare `except`.
- **Types:** full type hints on every public function and class; `mypy --strict` clean for `opengrid.core`, and
  `mypy` (default) clean for everything else. Pydantic v2 models for all data crossing a module or process boundary.
- **Structure:**
  - Small, single-purpose functions (aim for ≤ 40 lines) and modules (≤ 500 lines).
  - Clear names from the domain (obligation, commitment, reservation, grant, verdict…).
  - Pure logic separated from I/O.
  - Dependency direction: `core` ← `platform` ← modules ← process entry points; never the reverse.
  - No duplicated logic (see §1, dupcheck).
- **Docs:** a module docstring saying what the module owns and which spec section it implements (e.g., "02a §5
  allocator"). Docstrings on public functions state behaviour, units and invariants (K-ids), not implementation
  chatter. Comments explain *why*, never *what*.
- **Errors and robustness:**
  - Explicit exception types.
  - Every external call has a timeout.
  - Retries only where the spec says so.
  - No silent fallbacks: a degraded path is logged, traced and visible in health.
- **Config:** no hard-coded hosts, ports, credentials, thresholds or magic numbers. Everything comes from config or
  named constants with units in the name (`lease_ttl_s`, `price_usd_per_mwh`).
- **Tests:**
  - pytest with descriptive test names that map to test-plan IDs (`test_ts_05_03_commitment_lock_same_tier`).
  - Hypothesis property tests for the K1–K13 invariants.
  - ≥ 85% line coverage on `core`, ledger, allocator, guardian and selector; ≥ 70% elsewhere.
  - No flaky sleeps: use injected clocks.
- **Security:** no secrets in code or logs; parameterised SQL only; validate all inbound messages against
  `interfaces/` schemas; least-privilege MQTT users.
- **Definition of done for a module:** lint + types + tests green locally and on the server, dupcheck clean, spec
  section referenced, public interface unchanged or approved by the architect, and a short `README.md` in the package
  (purpose, interface, how to test).

## 6. Safety rules for agents (mandatory)

- Never print, log or commit secret values. Refer to env-variable names only.
- Run on the server only through `tools/remote.ps1` in your own workspace, as user `opengrid`. Only the deploy agent
  may run root scripts. It may touch only `og-*` systemd units, `/etc/apache2/conf-available/opengrid*.conf` (always
  `apache2ctl configtest` before reload), `/etc/cron.d/opengrid`, `/etc/logrotate.d/opengrid`, and `/opt/opengrid`,
  `/opt/ogsim`, `/var/lib/opengrid`.
- Never touch Apache vhosts, MariaDB, mail services, `/opt/opengrid_sim`, `/var/www/html/opengrid` or
  `/etc/mosquitto` (ask the lead).
- Remote commands: never inline double-quoted strings through ssh. `remote.ps1` sends a script file; use it.
- **Never stop processes by pattern** (`pkill -f`, `killall`). Test workspaces run as the same `opengrid` user as the
  production services, so a pattern kill can stop production. Start background test processes with `cmd & pid=$!`
  and stop only that PID (`kill "$pid"`). Use test ports ≥ 18000, never the production ports 8080/8090/8091.
- Tests first from `04-mvp-s-test-plan.md`. Any K1–K13 failure blocks.
- When done, report: files created, tests run with results (paste the pytest summary line), open issues.
