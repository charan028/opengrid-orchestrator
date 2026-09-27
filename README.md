# OpenGrid Orchestrator

**One fleet, many buyers, homes first.** The orchestrator runs thousands of home batteries as one
portfolio and sells the same kilowatt to different buyers in different hours, while every home keeps
its backup reserve. It sells each kilowatt-hour once, to the buyer the contracts say should have it,
proves it was delivered, and measures what that is worth against the rule-based allocation it replaces.

Open the console and the first screen, **Story** (`/og/story`), says all of this with the live
system's own numbers. Read that before anything else. `SUBMISSION.md` carries the hackathon
write-up, the video cut and the team.

## Quick start

Docker Engine with the compose plugin (2.20 or newer), Python 3.12 or newer, and `make`.

```bash
git clone <this repo> && cd opengrid-orchestrator
make -C dev dev-up-full            # Postgres, Mosquitto, migrations, seed (8 banks, 200 hubs), 4 simulators, 6 orchestrator services
python dev/scripts/dev_proxy.py    # 127.0.0.1:8088 -> og-api, adding the identity headers Apache adds in production
open http://127.0.0.1:8088/og/story
```

`dev-up-full` copies `dev/.env.example` and `dev/secrets.example` into place on first run; the
placeholder values are dev-only and fine for a fully local stack. Never open og-api's own port
(8080) in the browser: it trusts no identity without the proxy and every read degrades to 401.

Tests need no stack:

```bash
cd orchestrator && python -m pytest tests/unit -q     # unit and property tests
make check                                            # lint, types, duplication, unit tests, coverage
```

## Tech stack and architecture

| Layer | What |
|---|---|
| Language | Python 3.12, fully typed, `ruff` and `mypy` gated in CI |
| Optimizer | HiGHS (`highspy`), a two-stage stochastic mixed-integer program with an independent validator and a greedy fallback |
| Services | FastAPI and uvicorn; six long-running processes under systemd in production, compose locally |
| State | PostgreSQL 16 (`psycopg` async, 47 migrations); MQTT (Mosquitto, `aiomqtt`) for telemetry and commands |
| Safety | Ed25519 command signing with RFC 8785 canonicalisation; per-stream SHA-256 hash chain |
| Console | Server-rendered Jinja2 with htmx 1.9, Alpine 3.14, ECharts 5.5 and Leaflet 1.9; server-sent events for live values |
| Copilot | Advisory only: deterministic answers first, Claude for open explanation, TypeSafe System One as the screening fallback |
| Simulators | `integration-sims/`: hubs, SCADA, market and data APIs, a scenario control plane |

Five independent processes, in the order a kilowatt travels. No single one can both decide and act.

```
 ERCOT / EIA / NWS                                   homes (2,000 hubs, 40 banks)
        |                                                    ^        |
        v                                                    |  telemetry (MQTT)
   +-----------+   prices,   +-----------+  batch   +-------------+   |
   | og-feeds  |----load---->| og-engine |--------->| og-guardian |---+  signed commands (MQTT)
   +-----------+   forecast  +-----------+          +-------------+
        |                    plan (MILP)             check + sign     +-------------+
        |                    allocate (2 s)          veto = no publish| og-safestop |  stop-only key,
        v                          |                                  +-------------+  works with the
   +----------------------- PostgreSQL (og.*) ------------------------+               signer down
        |                          |
        v                          v
   +-----------+           +-----------+          +--------+  htmx / SSE  +-----------+
   | og-settle |           |  og-api   |<---------| Apache |<-------------| operators |
   +-----------+           +-----------+          +--------+              +-----------+
   meter, price,           console, API,           identity
   invoice, trace          copilot, streams        (X-Remote-User + secret)
```

The full diagram set is in `docs/diagrams/` (system, dispatch cycle, commitment lifecycle,
deployment, data model, power quality), and the specs behind it in `docs/orchestrator/`.

## How to reproduce the demo

The judged cut is `docs/demo/FIVE-MINUTES.md`; the full 24-step script is `docs/demo/README.md`.
Everything runs on the local stack above with the market feeds reading the simulator, which is the
default in `dev/config/dev.toml`, so the price spike is injectable:

```bash
SIM=http://127.0.0.1:8091          # the simulator control plane; user `tester`, password in dev/secrets
curl -u tester:$TESTER_PASSWORD -H 'X-OGSim-Request: 1' -X POST \
     $SIM/api/scenarios/demo-01-price-spike-lock/run -H 'Content-Type: application/json' -d '{"speed": 1}'
```

Then watch `/og/story`: prices jump on the ticker, the committed obligations on `/og/dispatch` do not
move, the three promise counters stay at zero, and the "upside declined" tile on the Story and
Profitability screens turns non-zero.

**Environment and keys.** `.env.example` at the repo root lists every variable in one place.

| File | Purpose | Needed for the demo |
|---|---|---|
| `dev/.env.example` | compose ports, fleet size, market data mode | yes, copied automatically |
| `dev/secrets.example` | dev-only Postgres and MQTT passwords, the proxy secret | yes, copied automatically |
| `docs/orchestrator/07-delivery/integrations/api_keys.env.example` | ERCOT Public API and EIA keys | only for live feeds |
| `ANTHROPIC_API_KEY`, `TYPESAFE_API_KEY` | the copilot's explanation and screening tiers | no; without them the copilot answers from console data only and says so |

No key is committed. The Markets screen labels every feed `LIVE`, `SIM` or `HIST`, so what you are
looking at is never ambiguous.

## Data: what is real, what is synthetic, and where it came from

| Data | Kind | Provenance |
|---|---|---|
| ERCOT settlement point prices per load zone, load by weather zone, wind, solar, day-ahead ancillary-service prices | real, live on the server | ERCOT Public API (`api.ercot.com/api/public-reports`), products np6-905-cd, np6-345-cd, np4-732-cd, np4-737-cd, np4-188-cd, np4-745-cd; `orchestrator/src/opengrid/feeds/ercot.py` |
| Demand (fallback) | real, live | EIA Open Data v2, respondent ERCO; `feeds/eia.py` |
| Weather | real, live | NWS `api.weather.gov` hourly; `feeds/nws.py` |
| Transmission lines (7,089 polylines), weather-zone centroids, utility-scale storage sites | real, one-time snapshot | Esri Living Atlas "US Electric Power Transmission Lines" (derived from HIFLD), HIFLD and EIA plant data, LBNL Tracking the Sun; exported 2026-09-23, SHA-256 pinned in `orchestrator/config/grid/README.md`, attribution required |
| TDSP delivery tariffs | real, hand-curated | PUCT filings; `orchestrator/config/tdsp_tariffs.toml` |
| Market data on the dev stack | synthetic | seeded diurnal price, load, wind and solar curves with noise; `integration-sims/src/ogsim/market/synthetic.py` (`replay` mode can use a real history export) |
| Battery telemetry, SCADA bank load, utility instructions, meter data | synthetic | `ogsim.fleet` and `ogsim.scada` physics over MQTT; `integration-sims/config/fleet.yaml` |
| Contracts, banks, hubs, home reserve floors | seeded | `orchestrator/migrations/0002_seed_demo.sql` and `dev/seed/` |
| Scenarios and anomalies (price spike, overload, comms loss, forged command, energy runs low) | synthetic, injected on demand | `integration-sims/scenarios/*.yaml` |

No bid has been submitted to ERCOT and no ERCOT settlement statement has been reconciled; the market
counterparty is simulated end to end.

## Known limitations and next steps

The blunt, per-item register is `docs/orchestrator/07-delivery/13-known-limitations.md`. The ones that
matter most to a reader of this repo:

- `GET /og/api/health` reports a dead process as healthy: the snapshot query ignores its staleness
  threshold. The chaos runner kills each service correctly; the API cannot yet show the result.
- A manual target on a bank with no grant is vetoed every cycle and opens a spurious safe-stop alert.
- A safe stop can be recorded and never published (two transactions, no retry cap on the outbox).
- The homeowner is a constraint, not yet a counterparty: no member-facing surface, no revenue share.
- Scale is designed, not evidenced: budgets and a 7,500-home stress harness exist, but nothing above
  1,009 hubs has completed a run, and the one production datapoint misses the cycle budget.
- CI gates unit, simulator and property tests only; integration, end-to-end and performance suites
  run by hand.
- Real DNP3/ICCP SCADA, ERCOT QSE registration, SSO and Kubernetes HA are not built.

Next, in order: fix the health snapshot and commit one real chaos report; fix the permanent veto;
one honest scale number with a chart; a member's view of their own battery; a demo director that
turns a sentence into a simulator scenario.

## Layout

Two independent products that share no code and meet only at the wire (`interfaces/`):

- `orchestrator/` is package `opengrid`, the live app (engine, guardian, safestop, settle, feeds, api, ui).
- `integration-sims/` is package `ogsim`, the simulators (hubs, SCADA, market and data APIs).

`BUILD.md` fixes scope, ownership and the code-quality gate. `orchestrator/INTERFACES.md` indexes the
module interfaces, `docs/orchestrator/` holds the approved specs, `docs/api/` the API reference,
`docs/operator/README.md` explains every screen, and `deploy/` holds the systemd, Apache and
Kubernetes material with its runbook.
