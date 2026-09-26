# OpenGrid Orchestrator — MVP-S Epics & Stories

Invariants: see 00-invariants.md (canonical). Status: draft for owner approval (gate G1).

**Code references.** A `file:line` marked R2 (or given in a "Status at main `6470cfa`" block) is at `main` `6470cfa`. Every other `file:line` was verified at `434d230` and may have moved since.

Release tag: **MVP-S**. Status: for G1 approval alongside `02a-mvp-s-spec-engine.md` and `02b-mvp-s-spec-platform.md`.
Derived from [`01-saturday-delivery-plan.md`](01-saturday-delivery-plan.md) (scope, modules, acceptance A1–A11,
schedule, cut lines, never-cut list) and [`../06-reviews/06-first-principles-review.md`](../06-reviews/06-first-principles-review.md)
(principles P1–P8, commitment lock §3, decisions §8a). Existing IDs are reused and cited, not renumbered, from
[`../01-product/03-epics-and-user-stories.md`](../01-product/03-epics-and-user-stories.md) (epics E01–E23, story IDs)
and [`../01-product/02-functional-requirements.md`](../01-product/02-functional-requirements.md) (FR-xxx). Personas are
the 16+1 roles of [`../01-product/01-vision-scope-personas.md`](../01-product/01-vision-scope-personas.md) §6.

**How to read the citations.** An existing story/FR ID (e.g. `ARB-S02`, `FR-ARB-004`) names a requirement or story
already specified for the full engine; MVP-S builds a first, thinner slice of it, scoped to the plan's modules and
the five services in A5. Where MVP-S narrows or corrects an existing item (most importantly `FR-ARB-004` and
`ARB-S05`, per review §3.1 finding #1), the story says so explicitly. `FR-ARB-014` is new (review §3.2); no other
new FR numbers are introduced — everything else cites what exists.

**Invariant legend (K1–K13).** Canonical definitions and enforcement points are in
[`00-invariants.md`](00-invariants.md); this document does not restate or renumber them. One-line summary: **K1**
homeowner reserve · **K2** one buyer (single-writer ledger) · **K3** sole signer (guardian Ed25519) · **K4** physical
envelope (P/kVA/ramp) · **K5** grid authority (L2 hard constraints) · **K6** command freshness (sequence/epoch/lease)
· **K7** degrade, don't trip (TIMEOUT≠VETO≠STOP, hold→schedule→local autonomy) · **K8** stop authority (independent
safe-stop) · **K9** one loop per quantity (allocator PI, guardian G-03) · **K10** trace before act · **K11**
verifiable track record (hash chain, retention, pruning) · **K12** time quality (guardian clock, G-20) · **K13**
commitment lock (review §3.2, `FR-ARB-014`, guardian check **G-19**) — the one invariant every workstream in this
document must not violate. Every story's "Invariants:" line below cites the canonical K-ID by meaning, not by the
number an earlier draft happened to use.

---

## 1. Overview

| Epic | Goal | Modules (spec) | Acceptance covered | Workstream | Never-cut |
|---|---|---|---|---|---|
| **ES01** Platform & data | One deployable codebase: schema, shared contracts, CI, compose/systemd, Apache route, backup | `feeds`(schema)/all modules (contracts lib), platform | A11 (survives process kill, nightly `pg_dump`) | WS1 | Partial — deploy path yes; nothing safety-critical |
| **ES02** Live feeds & forecast | ERCOT/EIA/NWS ingestion with staleness and a simple forecast | `feeds`, `forecast` | A1 | WS2 | No |
| **ES03** Fleet twin & test harness | Digital twin of 2,000–10,000 simulated hubs/banks; scenario injector | `fleet`, `sim` | A2, A11 (2k perf) | WS3 | No (twin feeds K13 substitution, but twin itself isn't on the never-cut list) |
| **ES04** Contracts, opportunities & commitments | Customer/contract/opportunity/obligation model; admission; partial take; re-nomination | `contracts` | A4, A9 | WS4 | Partial — obligation lifecycle yes (feeds K13); contract variety no |
| **ES05** Dispatch (selector, ledger, allocator) | LP selection at gates; **commitment lock**; single-writer ledger; 2 s real-time allocation with substitution | `selector`, `ledger`, `allocator` | A4, A5, A10 | WS4 | **Yes — commitment lock, reserve, one buyer (cut line 1 may fall back to the rule selector; the lock itself never falls back)** |
| **ES06** Guardian & safe stop | Sole command signer incl. **G-19**; scoped safe stop; TIMEOUT≠VETO≠STOP | `guardian`, `safestop` | A3, A10 | WS5 | **Yes — guardian signing** |
| **ES07** Health & degraded modes | Heartbeats, feed/hub health, cycle latency, alerts, degraded-mode switch | `health` | A6 | WS5 | No |
| **ES08** Settlement (M&V, billing, profitability) | Interval metering, baseline, insert-only invoicing, profitability incl. forgone upside | `settle` | A7, A8 | WS6 | Partial — cut line 3 may drop baseline/forgone-upside reporting |
| **ES09** Audit trace & retention | Hash-chained trace of every event; chain-verify; configurable retention with checkpointed pruning | `trace` | A9 | WS1 (lib) / WS8 (verify tests) | **Yes — the trace** |
| **ES10** Operator UI | 7 screens + scenario panel over SSE | `ui` | A1–A9 (presentation of all) | WS7 | No |
| **ES19** Two markets (regulated utility + ERCOT) | Regulated-utility capacity contracts (premium capacity, territory-bound energy) alongside the ERCOT free market; $/kW-in vs $/kW-out economics; the solar/off-peak charging mix | `selector`, `allocator`, `contracts`, `settle` (per `09-optimizer-dispatcher-update.md`, `08-market-model-two-markets.md`) | — (next-phase; not part of the original A1–A11 MVP-S demo scope) | WS4/WS6 | No |

Totals: **11 epics, 67 stories** (§2: 52 from the original MVP-S build, ES01–ES10 — that section's own "51"
subtotal-of-subtotals undercounts by one; a direct count of its story headers gives 52 — plus 15 added
post-MVP-S: ES19 §2.11 and the decision-log/flow-limit stories folded into ES01/ES03/ES04/ES05/ES06/ES07, per
`11-decision-log.md` D-4..D-27 and `09-optimizer-dispatcher-update.md`). Acceptance item A11 (performance/chaos)
is proven by stories across
ES03/ES05/ES06/ES07 plus WS8 test execution in the sibling test plan (`04-mvp-s-test-plan.md`), not by a UI story.
ES19 and the flow-limit stories are **next-phase** additions (owner decisions D-20..D-27, 2026-09-26): they extend
the schedule/dependency map of §4, which remains the record of the original Fri–Sat MVP-S build and is not
retro-fitted with these later stories' hours.

---

## 2. Epics and stories

Format per story: ID · title · role/goal · priority · Given/When/Then acceptance criteria · linked existing
FR/story IDs · invariants (K1–K13) · dependencies · estimate (hours).

### ES01 — Platform & data

Goal: one modular Python codebase, **7 processes** (`og-feeds`, `og-engine`, `og-guardian`, `og-safestop`, `og-sim`,
`og-settle`, `og-api` — `og-safestop` independent of the engine and the guardian, per K8), Postgres + Mosquitto,
deployed behind Apache TLS on the base server, backed up nightly. Modules: `feeds` (schema baseline), all modules'
shared contracts, platform-wide CI and deploy. Workstream: **WS1**.

**ES01-S01 — Postgres schema and migrations for every module's tables.**
As a *platform SRE*, I want one versioned migration set covering `feed_obs`, `hub`, `bank`, `telemetry`, `contract`,
`opportunity`, `obligation`, `plan`, `commitment`, `reservation`, `grant`, `verdict`, `stop_event`, `heartbeat`,
`alert`, `meter_interval`, `performance`, `invoice_line`, `pnl` and `trace`, so every module owns exactly the
tables the delivery plan §2.1 assigns it.
- Given a clean database, When migrations run, Then every table in plan §2.1 exists with the stated owner and no
  cross-module writer.
- Given `telemetry`, When partitioned, Then it is partitioned by day per plan §2.2.
- Given a second migration run, When applied, Then it is idempotent (no error, no duplicate objects).
Priority: Must · FR/story links: `FR-OPS-004` (externalized config), `TRACE-S01` (structural basis for `trace`'s
append-only table) · Invariants: — (foundational) · Dependencies: none · Estimate: 3 h

**ES01-S02 — Shared Pydantic contracts package.**
As a *system admin*, I want one Pydantic package defining every cross-module message and table row, imported by
all 7 processes, so WS2–WS7 can build against frozen interfaces from hour one (plan §7).
- Given the Phase 0 addendum's interfaces (`02a-mvp-s-spec-engine.md`, `02b-mvp-s-spec-platform.md`), When the
  package is generated, Then every module's public function signature matches the addendum exactly.
- Given a module imports a stale contract version, When CI runs, Then the build fails.
Priority: Must · FR/story links: `FR-DEV-017` (`DeviceAdapter`-style interface discipline), `FR-OPS-004` ·
Invariants: — · Dependencies: ES01-S01 · Estimate: 2 h

**ES01-S03 — Compose/native deploy behind Apache TLS + basic auth.**
As a *platform SRE*, I want the 7 processes (including `og-safestop` as its own unit), Postgres and Mosquitto
deployed on 192.168.5.35 as native systemd units (Compose files kept for portability), reachable at
`https://base.tocy-net.net/og/` behind the existing Apache, with two static roles (operator, viewer), so the demo
runs without touching MariaDB or `/opt/opengrid_sim`.
- Given `make deploy`, When run against a clean checkout, Then G0 ("hello" at `/og/`) passes in under 10 minutes.
- Given the deploy, When a live port scan runs, Then ports 80/443 are untouched and MariaDB/`/opt/opengrid_sim`
  are never written to.
- Given an unauthenticated request to `/og/`, When made, Then it is rejected by Apache basic auth before it
  reaches the app.
- Given the owner's 2026-09-26 confirmation that 192.168.5.35 is the **permanent** host (D-16, not only the
  MVP-S demo host), When later stories are planned, Then they target this same host/unit layout rather than a
  future migration.
Priority: Must · FR/story links: `FR-OPS-012` (no port conflicts on the shared host), `FR-OPS-004` · Invariants:
— (foundational; enables K8's independent `og-safestop` unit) · Dependencies: ES01-S01, ES01-S02 · Estimate: 3 h
Decision: D-16 · Status: **built** · Code: `deploy/apache/opengrid.conf:6` (`base.tocy-net.net`), `BUILD.md:17`
("deployed and working on 192.168.5.35") · Test: `tests-e2e/smoke.py`

**ES01-S04 — CI: unit and property tests on every push.**
As a *platform SRE*, I want `pytest` (unit + Hypothesis property tests) running in CI on every push, gating merge,
so a broken module never reaches Phase 2 integration.
- Given a pull request, When CI runs, Then unit tests for the touched module and the shared property-test suite
  (no double allocation, no commitment reduction without a reason code) both execute.
- Given any test fails, When CI completes, Then the merge is blocked.
Priority: Must · FR/story links: `FR-OPS-011` (build-blocking portability/config audit, same CI discipline) ·
Invariants: K13 (property test for commitment reduction is the first automated K13 guard) · Dependencies:
ES01-S02 · Estimate: 2 h

**ES01-S05 — Nightly backup and restore runbook.**
As a *platform SRE*, I want nightly `pg_dump` plus a written, tested restore procedure, so A11 ("nightly
`pg_dump`") is demonstrated, not asserted.
- Given the nightly job, When it runs, Then a dated dump lands outside the Postgres data volume.
- Given the restore runbook, When followed against a fresh instance, Then the restored database passes the
  chain-verify check of ES09-S04.
Priority: Should · FR/story links: `FR-OPS-006` (retry/dead-letter/self-recover discipline) · Invariants: K11
(audit trace survives restore, chain still verifies) · Dependencies: ES01-S03, ES09-S01 · Estimate: 1 h

**ES01-S06 — Per-workspace MQTT isolation for parallel dev workspaces.**
As a *platform SRE*, I want each developer workspace to get its own MQTT user (`ogw_<ws>`) scoped by ACL to its
own topic tree (`ogtest/<ws>/#` only), so parallel workspaces never receive production MQTT credentials or see
each other's simulated traffic (decision log D-13, delegated by the lead 2026-09-26).
- Given a new workspace is provisioned, When its MQTT user is generated, Then it can publish/subscribe only
  under `ogtest/<ws>/#`, never the production `og/#` tree.
- Given two workspaces run simultaneously, When either publishes telemetry, Then the other's `og-engine`/`og-sim`
  never receives it (topic isolation, not just an ACL policy on paper).
- Given a workspace is torn down, When its user is revoked, Then no other workspace's ACL is affected.
Priority: Should · FR/story links: `FR-OPS-004` (externalized config), BUILD.md §5 · Invariants: — (development
isolation; not a runtime safety invariant) · Dependencies: ES01-S02, ES01-S03 · Estimate: 2 h
Decision: D-13 · Status: **built** · Code: `dev/scripts/gen_mosquitto_acl.py`, `deploy/mosquitto/provision_ws_users.py`,
`deploy/mosquitto/provision_ws_users.sh`, `orchestrator/src/opengrid/platform/mqtt.py`, `tools/ws_env.sh` ·
Test: `orchestrator/tests/unit/tools/test_ws_env.py`, `integration-sims/tests/test_workspace_config.py`

**ES01 subtotal: 6 stories, 13 h.**

---

### ES02 — Live feeds & forecast

Goal: ERCOT/EIA/NWS ingestion on schedule, normalized, stale-flagged, rate-limited; simple quantile forecast.
Modules: `feeds`, `forecast`. Workstream: **WS2**.

**ES02-S01 — ERCOT prices, load, wind/solar and AS prices on schedule.**
As a *market/QSE trader*, I want ERCOT real-time price, load, wind/solar output and AS clearing prices polled and
normalized, so the selector always has live inputs for arbitrage and AS valuation.
- Given ERCOT's API is healthy, When a poll tick runs, Then a new observation for each configured series is
  available to consumers within 60 s.
- Given a poll returns an error, When it fails, Then the last known-good value is served, flagged `stale`, and no
  exception propagates to a consumer.
Priority: Must · FR/story links: `ING-S01` (ERCOT load-zone price), `FR-ING-001`, `FR-ING-006`, `FR-ING-007` ·
Invariants: — · Dependencies: ES01-S02 · Estimate: 3 h

**ES02-S02 — Shared 30 req/min ERCOT budget.**
As a *platform SRE*, I want every ERCOT poller bounded to one shared 30 req/min budget, so the demo never gets
ERCOT-blocked.
- Given all pollers active concurrently, When load-tested, Then the sustained rate never exceeds 30 req/min.
- Given the budget is momentarily exhausted, When a poller's turn comes, Then it waits rather than erroring.
Priority: Must · FR/story links: `ING-S04`, `FR-ING-004`, `FR-ING-005` · Invariants: — · Dependencies: ES02-S01 ·
Estimate: 1 h

**ES02-S03 — EIA fallback and NWS weather.**
As a *planning & forecasting analyst*, I want an EIA v2 fallback for price/load and NWS forecasts/watches/warnings
ingested for the fleet's service areas, so a single-source ERCOT outage doesn't blind the selector.
- Given ERCOT is unreachable for a series with an EIA fallback, When the poll runs, Then the EIA value is served,
  labelled by source.
- Given NWS issues a watch, When polled, Then it reaches the console within one poll cycle.
Priority: Must · FR/story links: `ING-S07`, `FR-ING-010`, `FR-ING-011`, `FR-ING-012` · Invariants: — ·
Dependencies: ES02-S01 · Estimate: 2 h

**ES02-S04 — Freshness badges and circuit breaker.**
As a *control-room operator*, I want every displayed series to carry an "as of" freshness badge, and a source
that keeps failing to trip a circuit breaker rather than retry-storm ERCOT, so A1's freshness requirement and A6's
feed-staleness trigger both have one shared source of truth.
- Given a source misses N consecutive polls (per `02b-mvp-s-spec-platform.md`'s threshold), When the Nth miss
  occurs, Then the circuit breaker opens and health (ES07-S02) is notified in the same cycle.
- Given any UI screen shows a feed value, When rendered, Then its freshness badge is present and accurate to the
  second.
Priority: Must · FR/story links: `ING-S05`, `FR-ING-012`, `FR-ING-013`, `FR-ING-015` · Invariants: — ·
Dependencies: ES02-S01, ES02-S03 · Estimate: 2 h

**ES02-S05 — Quantile-persistence forecast (P10/P50/P90).**
As a *planning & forecasting analyst*, I want the simplest defensible 24 h forecast — quantile persistence, not a
trained model — for price and load, so the selector has a scenario set without over-building forecasting for
Saturday.
- Given the forecast runs, When published, Then P10/P50/P90 exist for every hour of the next 24 h.
- Given a degraded/stale input, When the forecast runs, Then the band widens rather than staying unchanged.
- Given growing solar penetration (D-24: solar expansion widens the price swings), When the 24 h band is
  published, Then it reflects a solar-driven intraday shape (a midday dip, an evening ramp) rather than only a
  flat diurnal pattern — **gap**: today's quantile-persistence forecast has no solar/irradiance input at all.
Priority: Must · FR/story links: `FCST-S01`, `FCST-S04`, `FR-FCST-001`, `FR-FCST-005`, `FR-FCST-008` ·
Invariants: — · Dependencies: ES02-S01 · Estimate: 2 h
Decision: D-24 (partial) · Status: **not built** (the duck-curve/solar-shape clause only; the base P10/P50/P90
forecast itself is built) · Code: `orchestrator/src/opengrid/forecast/` has no solar/irradiance input (confirmed
by grep: no `solar`/`duck`/`PVGR` hit outside `forecast/README.md`) · Test: none found

**ES02 subtotal: 5 stories, 10 h.**

---

### ES03 — Fleet twin & test harness

Goal: an accurate hub/bank digital twin fed by a 2,000-hub (10,000 stretch) test harness that is the spec's
`agent-sim`/`grid-sim`, not the base-server concept simulators. Modules: `fleet`, `sim`. Workstream: **WS3**.

**ES03-S01 — Hub/bank digital twin: SoC, P, health, eligibility.**
As a *control-room operator*, I want a current, fresh state estimate (SoC, power, health, eligibility, quality
flags) for every hub and bank aggregate, so dispatch never plans against stale or fabricated capability. The twin
never simulates physics forward in time (`sim` does that, ES03-S03); it only stores reported state and derives
`capability(bank,t)` from `og.core.physics` (`02b` §12), so there is one SoC-step formula in the whole codebase.
- Given telemetry arrives on the 2 s cadence, When processed, Then the twin's estimate age never exceeds one
  interval plus processing latency.
- Given telemetry stops arriving, When more than 3 reports are missed, Then the hub is excluded from allocation
  totals but stays visible (not silently dropped).
- Given the hardware nameplate Base confirmed (D-5: 39.2 kWh/11 kW single-unit, 78.4 kWh/20 kW dual-unit, ~600
  kVA banks), When the twin seeds/reports hub params, Then every hub's `e_kwh`/`p_kw` matches one of these two
  nameplates, never an arbitrary value.
Priority: Must · FR/story links: `TWIN-S01`, `FR-TWIN-001`, `FR-TWIN-009` · Invariants: — · Dependencies:
ES01-S02 · Estimate: 3 h
Decision: D-5 · Status: **built** · Code: `orchestrator/src/opengrid/fleet/seed.py:41-43` (nameplate constants),
`integration-sims/src/ogsim/common/config.py` (`FleetConfig` defaults), `integration-sims/config/fleet.yaml` ·
Test: `orchestrator/tests/unit/fleet/test_seed.py`, `integration-sims/tests/test_fleet_dual_unit.py:478`
(`test_dual_unit_hub_power_and_reserve_match_config`)

**ES03-S02 — Bank/zone topology: "behind asset X" queries.**
As a *SCADA/protocol integration engineer*, I want to query exactly which homes sit behind a given bank, so
`DIST_DEFERRAL` and the allocator can restrict allocation to the right homes.
- Given a test bank's known enrolled homes, When queried, Then exactly those homes are returned, no others.
- Given the seed rule Base confirmed (D-9: dual-unit homes spread 10 per bank; every bank stays single-zone),
  When the topology is built, Then each of the 40 banks has exactly 10 dual-unit hubs and exactly one zone.
Priority: Must · FR/story links: `TWIN-S03`, `FR-TWIN-003` · Invariants: — · Dependencies: ES03-S01 · Estimate:
2 h
Decision: D-9 · Status: **built** · Code: `orchestrator/src/opengrid/fleet/seed.py:191-208` (`_is_dual_unit`),
`:218-234` (`build_topology`), `dev/seed/rebalance_dual_units.sql` · Test:
`integration-sims/tests/test_fleet_dual_unit.py:444` (`test_dual_unit_homes_spread_10_per_bank_not_clustered_in_8_banks`),
`:464` (`test_every_bank_has_exactly_one_zone`), `orchestrator/tests/unit/fleet/test_seed_zone_blocks.py`

**ES03-S03 — Sim harness: 2,000 hubs with real SoC physics, lease and signature verification.**
As a *platform SRE*, I want the test harness to run 2,000 simulated hubs (10,000 as a stretch) with real SoC
physics ($e_{t+1}=e_t+\eta_c p^c\Delta t-p^d\Delta t/\eta_d$), a 30 s lease, and Ed25519 signature verification on
every received command, so it stands in for real hubs and SCADA, not a shortcut around the guardian.
- Given 2,000 hubs streaming telemetry every 2 s, When measured over a 30-minute run, Then telemetry ingestion
  keeps pace with no dropped messages and the twin (ES03-S01) stays within one interval of freshness.
- Given a command signed by any key other than the guardian's, When received by a simulated hub, Then it is
  rejected and the rejection is observable.
- Given the guardian or engine process is stopped, When a hub's lease expires (30 s), Then the hub holds its last
  setpoint (local autonomy), never a trip.
Priority: Must · FR/story links: `SIM-S07` (drive N hubs off-node — MVP-S runs 2k on-node per plan §0a), `SIM-S10`
(safe-stop-only key enforcement), `FR-SIM-013`, `FR-SIM-020` · Invariants: K6 (lease/epoch freshness), K7 (lease
expiry → local autonomy, never a trip), K8 (safe-stop-only key enforcement) · Dependencies: ES01-S02, ES06-S01
(signature scheme) · Estimate: 5 h

**ES03-S04 — Scenario injector for the demo panel.**
As a *Base executive (sponsor)*, I want the harness to inject, on demand: a partner call, a price spike, a
better-paying call during an active delivery (the commitment-lock demo), a feeder overload, comms loss for a
zone, a stale feed, and a killed engine process, so the UI scenario panel (ES10-S06) has something real to show.
- Given each of the seven scenario types, When triggered from the API, Then it produces the identical downstream
  telemetry/event shape a real occurrence would.
- Given the same seed, When a scenario sequence re-runs, Then it reproduces exactly (timing and outcome).
Priority: Must · FR/story links: `SIM-S02`, `SIM-S06`, `FR-SIM-003`, `FR-SIM-009`, `FR-SIM-012` · Invariants: —
· Dependencies: ES03-S03 · Estimate: 3 h

**ES03-S05 — No over-reporting of available capacity.**
As an *auditor*, I want a guarantee that the twin's reported available kW never exceeds SoC minus the reserve
floor and any higher-priority reservation, so the selector never plans against fictional headroom.
- Given randomized fleet states, When property-tested, Then reported available kW plus every active reservation
  never exceeds SoC, across 100% of samples.
- Given a hub's SoC is exactly at the reserve floor, When queried for non-firm use, Then its available discharge
  kW is reported as zero.
Priority: Must · FR/story links: `TWIN-S05`, `FR-TWIN-007`, `FR-TWIN-008`, `FR-TWIN-011` · Invariants: **K1**
(homeowner reserve) · Dependencies: ES03-S01 · Estimate: 2 h
Decision: D-25 (confirmation only — Base confirmed the 20% floor already built here; no behaviour change) ·
Status: **built** · Code: `orchestrator/src/opengrid/core/limits.py:30-62` (`check_reserve_floor`,
`check_reserve_floor_over_lease` — G-01/G-01-ENERGY) · Test: `orchestrator/tests/property/test_k04_envelope.py`,
`orchestrator/tests/unit/core/test_limits.py`

**ES03-S06 — Customer-operator simulators (`ogsim/customer`), customer API, site ingest, closed-loop controllers.**
As a *fleet reliability engineer*, I want a customer-side simulator (mirroring a real DATA_CENTER/PIPELINE_AC
site) that ingests its own meter, publishes a measured-need signal through a customer API, and drives the
allocator's closed-loop (`MEASURED_FEEDBACK`) path, so need-basis obligations can be demonstrated end to end
without a real customer site (decision log D-11: "built dark").
- Given the customer simulator is running, When it publishes a site-ingest reading, Then the obligation's
  `setpoint_source=MEASURED_FEEDBACK` grant follows the customer's measured need, not the plan's schedule.
- Given no operator-facing UI exists for it yet, When the simulator runs, Then it is observable only through the
  API/trace (built dark), never exposed on the ES10 screens.
Priority: Should · FR/story links: none existing (new capability) · Invariants: K13 (need-basis grants,
`00-invariants.md`'s 2026-09-26 K13 additions) · Dependencies: ES03-S03, ES04-S06 · Estimate: 3 h
Decision: D-11 · Status: **not built** — verified against the codebase, this contradicts the decision log's
"(built dark)" label: there is no `ogsim/customer` package, no customer-facing API router, and no site-ingest
module anywhere in the repo (grep for `customer`/`site_ingest`/`CustomerSim`/`og-cust` across
`integration-sims/` and `orchestrator/src/` finds only the unrelated multi-customer **contract** field, e.g.
`orchestrator/tests/unit/contracts/test_multi_customer.py`). The only trace of the concept is the
`"CUSTOMER_API"` placeholder value in `SetpointSource` (`orchestrator/src/opengrid/core/models/pq.py:89`), and
`contracts/admission.py:169-183` explicitly gates DATA_CENTER/PIPELINE_AC admission off "until the closed-loop
controllers are confirmed live" — i.e. the code itself says this piece is outstanding · Test: none found

**ES03 subtotal: 6 stories, 18 h.**

---

### ES04 — Contracts, opportunities & commitments

Goal: the commercial substrate — customer/contract/opportunity/obligation — for the five A5 services, with
partial-take product rules and contractual re-nomination points. Module: `contracts`. Workstream: **WS4**.

**ES04-S01 — Obligation lifecycle: offered → selected → committed → delivering → fulfilled → settled.**
As a *system admin*, I want every opportunity to progress through the review's formal lifecycle (review §3.2),
owned by the ledger's single writer, so every later story has one authoritative state machine to build on.
- Given an opportunity is admitted, When it progresses, Then it visits exactly the six states in order, with no
  skip and no reverse transition except the documented exceptions (ES05-S03).
- Given two obligations reference the same underlying capacity, When queried, Then each has an independent
  lifecycle record.
Priority: Must · FR/story links: `CTR-S01`, `FR-CTR-001`, `FR-CTR-002` · Invariants: K13 (the lifecycle is the
scaffold the lock is enforced on) · Dependencies: ES01-S01 · Estimate: 2 h

**ES04-S02 — Opportunity admission and feasibility check.**
As a *Base executive*, I want a call admitted only if it is feasible and authorized against contract terms, one
buyer, and current derated capacity, before it competes for anything.
- Given a call violates its contract's terms, When submitted, Then it is rejected pre-allocation with a reason.
- Given a feasible call for one of the five A5 services (`HOME`, `ERCOT_ENERGY`, `ERCOT_AS`, `DIST_DEFERRAL`,
  `PARTNER_CAPACITY`), When admitted, Then it enters `offered` state.
- Given `DATA_CENTER` or the `PIPELINE_AC` variant (two service types added since MVP-S, D-7), When submitted,
  Then admission additionally requires the activation gate open (ES04-S06); with it closed, the call is rejected
  `R-ADMIT-REJECT` regardless of feasibility.
Priority: Must · FR/story links: `ARB-S01`, `FR-ARB-001` · Invariants: P3 one buyer · Dependencies: ES04-S01 ·
Estimate: 2 h

**ES04-S03 — Partial take per product rules: `min_qty`, `increment`, `block`.**
As a *market/QSE trader*, I want the selector's quantity variable for each opportunity to be continuous where the
market allows partial take, semi-continuous above `min_qty` in `increment` steps where required, and binary for
all-or-nothing blocks — per marketplace/ERCOT rules — so a five-MW AS award and a fixed-block tolling contract
are both represented correctly (review §8a decision 2).
- Given a `ERCOT_AS` opportunity with `min_qty=1 MW` and `increment=0.1 MW`, When the selector runs, Then the
  chosen quantity is either 0 or a value ≥ `min_qty` on the `increment` grid.
- Given a `PARTNER_CAPACITY` all-or-nothing block, When the selector runs, Then the admission quantity is exactly
  0 or the full block (binary), never a partial value.
- Given a `HOME`/`DIST_DEFERRAL` opportunity with no block restriction, When the selector runs, Then a continuous
  quantity in $[0,\bar Q_o]$ is allowed.
Priority: Must · FR/story links: `CTR-S02` (contract term validation, closest existing analogue), `FR-CTR-003`,
`FR-CTR-005`; new: `FR-ARB-014` (partial-take clause) · Invariants: K13 (partial-take rules feed the lock's
$Q_{o,t}$) · Dependencies: ES04-S02, ES05-S01 · Estimate: 3 h

**ES04-S04 — Re-nomination points for multi-day and tolling contracts.**
As a *partner-program manager*, I want a multi-day or tolling contract's lock to run until its next agreed
re-nomination point (an extra gate for that contract alone) rather than only at fulfilment, so a toll's owner can
re-nominate on schedule without breaking the lock for every other obligation (review §8a decision 1).
- Given a tolling contract with a daily re-nomination point, When that gate is reached, Then only that contract's
  commitment is opened for re-selection; every other committed obligation stays locked.
- Given a re-nomination point has not yet arrived, When a competing opportunity attempts to bid for the tolled
  capacity, Then it is rejected exactly as `FR-CTR-020` requires for the full toll term.
Priority: Must · FR/story links: `CTR-S10`, `FR-CTR-020` · Invariants: K13 · Dependencies: ES04-S01, ES05-S02 ·
Estimate: 2 h

**ES04-S05 — Reason-coded admission and shortfall recording.**
As an *auditor*, I want every admission rejection and every shortfall recorded with a reason code linked to the
trace, never silently dropped.
- Given a call is rejected at admission, When it happens, Then the reason code and inputs are traced (ES09-S01).
- Given a committed obligation cannot be fully served, When detected, Then it is recorded as a shortfall against
  that obligation, never masked by a different call's outcome.
- Given the obligation is best-effort SHORTFALL rather than a hard stop (D-17), When it is recorded, Then the
  obligation's row and lifecycle state are ES05-S03's — this story only covers the reason-coded *trace* of the
  event, not the continued-delivery behaviour (see ES05-S03 for that).
Priority: Must · FR/story links: `ARB-S04`, `FR-ARB-006`, `FR-ARB-007` · Invariants: K13 (shortfall, not
reallocation, is the only outcome of a lock exception) · Dependencies: ES04-S01, ES09-S02 · Estimate: 2 h
Decision: D-17 (cross-reference; see ES05-S03 for the full best-effort behaviour) · Status: **built** ·
Code: `orchestrator/src/opengrid/core/reasons.py` (`LOCK_REASON_BY_SHORTFALL`) · Test:
`orchestrator/tests/unit/engine/test_escalation.py`

**ES04-S06 — DATA_CENTER/PIPELINE_AC service profile: PQ envelope, activation gate, corrective-action ladder.**
As a *Base executive*, I want the DATA_CENTER (and PIPELINE_AC-variant) need-basis service profile to carry its
own `PowerQualityEnvelope` contract term, and its admission to stay behind an explicit activation gate until the
customer-side closed-loop controllers are confirmed live, so a demo/data-center pilot never goes live on an
unverified feedback path (K14, approved by the owner 2026-09-25; D-7 amendments).
- Given `[contracts.activation].data_center` is `false` (the default), When a `DATA_CENTER` or `PIPELINE_AC`
  opportunity is admitted, Then it is rejected `R-ADMIT-REJECT` regardless of its other terms.
- Given the flag is `true`, When the same call is admitted, Then it proceeds through the normal
  `02-architecture/03-decision-engine.md` §2.7 activation gate (schema validation, then static rules) like any
  other service type.
- Given a `DELIVERING` DATA_CENTER obligation's measured PQ breaches its envelope, When the allocator's PQ
  monitor evaluates it, Then the corrective-action ladder runs in order (rebalance → substitute within the
  obligation → remote recalibration where eligible → reactive/PF adjustment → exclude → `AT_RISK`) before any
  breach is allowed to stand.
Priority: Should · FR/story links: none existing (K14 is new since MVP-S) · Invariants: **K14** (power-quality
envelope) · Dependencies: ES04-S02, ES06-S08 · Estimate: 3 h
Decision: D-7 · Status: **in build** — the envelope, allocator ladder and activation gate are built and heavily
tested, but the gate is closed by default pending ES03-S06's closed-loop controllers, so the profile cannot go
live yet · Code: `orchestrator/src/opengrid/contracts/admission.py:58-63,67,124,169-183` (activation gate),
`orchestrator/config/service_profiles/data_center.toml`, `orchestrator/src/opengrid/allocator/pq_eligibility.py`,
`orchestrator/src/opengrid/allocator/pq_monitor.py`, `orchestrator/src/opengrid/core/pq/envelope.py` ·
Test: `orchestrator/tests/unit/contracts/test_admission.py:138` (`test_admit_data_center_is_rejected_when_activation_gate_closed`),
`:156`, `:172`, `:185`; `orchestrator/tests/unit/profiles/test_data_center_profile.py`;
`orchestrator/tests/unit/allocator/test_pq_eligibility.py`, `test_pq_monitor.py`, `test_pq_performance.py`

**ES04 subtotal: 6 stories, 14 h.**

---

### ES05 — Dispatch (selector, ledger, allocator)

Goal: LP selection at gates with commitments frozen as parameters; a single-writer reservation ledger; a 2 s
real-time allocator that substitutes homes but never obligations. This is the review's central correctness
finding (§3, §6 #1) and the plan's largest, never-cut epic. Modules: `selector`, `ledger`, `allocator`.
Workstream: **WS4**.

**ES05-S01 — LP selection at gates (15 min + admission event).**
As a *Base executive*, I want the selector to solve at every 15-minute gate and on every new-call admission,
choosing which of the five A5 opportunities to commit, using scenario-weighted value of the whole delivery window
minus energy cost, degradation and expected penalty.
- Given all five service types have candidate opportunities, When the gate solves, Then a commitment decision
  ($x_o$ or $q_o$) is produced for each feasible one, respecting SoC/reserve/P/kVA/one-buyer/non-anticipativity.
- Given the fleet's derated capacity is exceeded by demand, When solved, Then no plan assigns more than the
  derated, reserve-adjusted total.
Priority: Must · FR/story links: `PLAN-S01`, `FR-PLAN-001`, `FR-PLAN-015` · Invariants: P3, P5, P7 · Dependencies:
ES03-S05, ES04-S03, ES02-S05 · Estimate: 4 h

**ES05-S02 — Commitment lock: freeze committed $x_o$/$\hat y$ as parameters (K13, `FR-ARB-014`).**
As a *partner-program manager (counterparty)*, I want my committed delivery unaffected when a better-paying call
arrives during my delivery window, so the selector cannot be re-argued mid-contract by a newer, richer opportunity
— this is the mandatory commitment-lock story (review §3.2, §8a decision under commitment lock).
- Given my obligation is committed and delivering, and a new same-tier or higher-value call arrives mid-window,
  When the next gate or real-time cycle solves, Then my committed $\hat y_{o,b,t}$ is treated as an equality/lower-
  bound parameter, not re-optimized, and my granted kW never drops below $\hat y$ because of the new call.
- Given the new call is genuinely more valuable, When it is admitted, Then it competes only for uncommitted
  headroom $H_{b,t}=\text{cap}_{b,t}-\sum_{o\,\text{committed}} r_{o,b,t}$, never for my committed capacity.
- Given a replay fixture of two same-tier calls of rising relative value, When replayed, Then the earlier-
  committed call never drops below $\hat y$ unless an L0/L1/L2/infeasibility event is injected (ES05-S03).
- Given the current spec's `FR-ARB-004` override and the `SERVED → ARBITRATING` re-arbitration edge (review §3.1
  finding table, `ARB-S02`/`ARB-S05`), When MVP-S is built, Then their scope is limited to **pre-commitment**
  selection only — neither may reduce a committed allocation.
Priority: **Must (never-cut)** · FR/story links: new `FR-ARB-014`; narrows `FR-ARB-004`, `ARB-S02`; supersedes the
uncommitted-headroom-only competitive scope of `ARB-S05`/`FR-ARB-010`; `FR-DE-039`/C16 raised to Must (review §5.4
item 3) · Invariants: **K13**, P4 · Dependencies: ES05-S01, ES04-S01 · Estimate: 4 h
Decision: D-4 (commitment lock: once committed, delivery completes; no mid-contract switching on price) ·
Status: **built** · Code: `orchestrator/src/opengrid/core/limits.py:149-167` (`check_commitment_lock`) ·
Test: `orchestrator/tests/property/test_k13_commitment_lock.py`, `orchestrator/tests/unit/ledger/test_ts_04_01_stateful.py`

**ES05-S03 — Lock exceptions: L0 safety, L1 reserve, L2 instruction, infeasibility.**
As an *auditor*, I want the only four events that may ever reduce a committed allocation — device safety (L0),
homeowner reserve (L1), ERCOT/utility instruction (L2), or physical infeasibility with no substitute — each to
carry its own reason code and to produce a recorded shortfall, never a silent reallocation.
- Given an L0 device-safety trip on a committed hub, When it occurs, Then the reduction is traced with reason
  `R-COMMIT-LOCK-OVERRIDE-L0` and a substitute is sought first (ES05-S04).
- Given a homeowner reserve breach is imminent (L1), When detected, Then the reduction is traced with
  `R-COMMIT-LOCK-OVERRIDE-L1` and the obligation is flagged `AT_RISK`.
- Given an ERCOT/utility instruction (L2) conflicts with a committed delivery, When it arrives, Then the
  instruction wins as a hard constraint, traced `R-COMMIT-LOCK-OVERRIDE-L2`, and the firm call is served by
  substitution or reported `AT_RISK`.
- Given no substitute exists and the shortfall is physically unavoidable, When determined, Then it is traced
  `R-COMMIT-LOCK-INFEASIBLE` with the specific constraint that bound.
- Given any reduction with none of these four reason codes, When the guardian evaluates the batch, Then it is
  refused (ES06-S02).
- **(D-17, best effort after a mid-window SHORTFALL.)** Given none of L0/L1/L2/infeasibility clears and the
  obligation is instead marked `SHORTFALL` mid-window, When the next cycles run, Then it is never treated as a
  stop: the obligation keeps receiving its maximum feasible kW (substitution first, ES05-S04), is flagged
  `AT_RISK` while short, and settlement bills the actual delivered energy, never the planned amount.
- Given the constraint that caused the SHORTFALL clears, When the next cycle runs, Then the obligation's full
  committed kW is restored at the earliest feasible interval, with no separate re-admission or re-commitment step.
- Given a SHORTFALL persists, When any interval of the window elapses, Then dispatch never stops early for the
  remainder of that window solely because of the shortfall (it only stops via K8 safe stop or a fresh L0-L2
  event).
Priority: **Must (never-cut)** · FR/story links: new `FR-ARB-014`; `SAFE-S01` (guardian independent validation
pattern); `FR-DISP-024` (ERCOT instruction as hard L2 constraint) · Invariants: **K13**, P1, P2 · Dependencies:
ES05-S02 · Estimate: 3 h
Decision: D-17 · Status: **built** · Code: `orchestrator/src/opengrid/engine/escalation.py` (`ShortfallEscalator`,
`lock_reason_for_shortfall`, `merge_signals` — sustained-signal escalation to the `SHORTFALL` lifecycle edge),
`orchestrator/src/opengrid/allocator/cycle.py:187-256` (`best_effort_reason`) · Test:
`orchestrator/tests/unit/engine/test_escalation.py` (`test_a_shortfall_obligation_keeps_its_feasible_remainder_and_recovers_in_one_cycle:158`,
`test_a_short_obligation_is_flagged_at_risk_and_cleared_on_recovery:205`,
`test_a_shortfall_obligations_partial_grant_carries_the_code_g19_corroborates:224`);
`orchestrator/tests/unit/guardian/test_service.py:959` (`test_best_effort_shortfall_grant_is_signed_when_the_guardian_confirms_the_shortfall`),
`:973`, `:984`

**ES05-S04 — Substitution of homes within a committed obligation.**
As a *fleet reliability engineer*, I want the allocator to substitute which homes deliver a committed obligation
— never which obligation the capacity serves — within 3 ticks of a hub under-delivering, so the commitment lock's
one allowed exception (§8.8) works in practice.
- Given a hub under-delivers during an active committed obligation, When detected, Then a substitute hub covers
  the gap within 3 ticks, and the obligation's identity and committed profile are unchanged.
- Given no substitute exists, When checked, Then the true shortfall is reported against that obligation (never
  masked as a full delivery, and never routed to a different obligation).
Priority: Must · FR/story links: `DISP-S05`, `FR-DISP-009`, `FR-DISP-010`, `FR-DISP-014` · Invariants: K13 (this
is the *allowed* case the lock carves out) · Dependencies: ES05-S02, ES03-S01 · Estimate: 3 h

**ES05-S05 — Single-writer reservation ledger; one buyer, zero double-booking.**
As an *auditor*, I want a mathematical guarantee that no hub's grants across obligations ever exceed its available
energy, enforced by one single-writer ledger.
- Given randomized obligation mixes, When property-tested, Then grants never exceed available energy above the
  reserve floor, across 100% of samples.
- Given two writers attempt a reservation on the same hub-interval concurrently, When both attempt, Then only one
  succeeds and the other observes the updated headroom.
Priority: **Must (never-cut)** · FR/story links: `DISP-S08`, `FR-DISP-018`, `FR-DISP-019` · Invariants: P3, K13 ·
Dependencies: ES05-S01 · Estimate: 3 h

**ES05-S06 — 2 s real-time allocator with `DIST_DEFERRAL` PI loop.**
As a *SCADA/protocol integration engineer*, I want the 2 s real-time cycle to subtract every active committed
$\hat y$ from capability, allocate free headroom with 5-minute dwell and \$5/MWh hysteresis, and run the
`DIST_DEFERRAL` bank-kVA PI loop closed on simulated SCADA, so real time stays a pure LP/water-filling problem
under the frozen commitments.
- Given a bank sample, When the PI loop runs, Then `need = measured (net of fleet) + fleet's own output behind
  the bank at the sample's source time − (rating − margin)`, computed in the rating's unit.
- Given uncommitted headroom only, When allocated to the energy schedule, Then no home flips allocation more
  than once per 5-minute dwell window absent a $5/MWh price move.
- Given the RT cycle runs at 2,000 hubs, When measured, Then cycle p99 < 500 ms (ties to A11, verified further in
  ES07-S04).
Priority: Must · FR/story links: `DISP-S02`, `DISP-S04`, `FR-DISP-003`, `FR-DISP-004`, `FR-DISP-007`,
`FR-DISP-008` · Invariants: K13 (RT never selects, per review §5.2) · Dependencies: ES05-S02, ES05-S05, ES03-S03 ·
Estimate: 4 h

**ES05-S07 — Rule-based baseline allocator, run in shadow.**
As a *control-room operator*, I want a simple rule allocator (firm first, then AS, then market) running in
shadow on the same inputs as the LP, so the UI can show value added by the LP and forgone upside from the lock —
and so cut line 1 (LP not passing at G3) has a working fallback that goes live instead.
- Given the same tick's inputs, When both the LP and the rule allocator run, Then both produce a comparable
  allocation and the difference in net value is computable.
- Given the LP fails to converge or its unit tests fail at G3, When the cut line applies, Then the rule allocator
  becomes primary and the LP runs in shadow instead, with this state visible on the UI.
Priority: Must (cut-line fallback target) · FR/story links: `PLAN-S07` (rule-based fallback, `control_engine.py`
port — written fresh, not copied, per review §8a decision 4), `FR-PLAN-014` · Invariants: — · Dependencies:
ES05-S01, ES05-S06 · Estimate: 2 h

**ES05-S08 — Continuous per-obligation energy sufficiency (K1/K13, energy not just power).**
As an *auditor*, I want every COMMITTED/DELIVERING obligation checked, every allocator cycle, for whether its
eligible hubs hold enough kWh above reserve to sustain it for the rest of its window — not only whether kW
headroom passes this instant — so a hub that passes every power check can never quietly run dry before the
window ends (owner decision 2026-09-25, `00-invariants.md` "K1/K13 energy").
- Given an obligation's eligible hubs' live SoC, When the check runs, Then required kWh (remaining commitment ÷
  η_d) is compared against available kWh above reserve, net of energy already reserved for other obligations
  (K2).
- Given the margin goes negative even after substitution, When detected, Then the obligation is flagged
  `AT_RISK` and `ALR-ENERGY-SHORTFALL-RISK` fires exactly once on entry (not every cycle).
- Given a hub's SoC is missing or stale, When the check runs, Then that hub contributes zero kWh (fail closed,
  never an assumed value).
- Given the risk clears, When the next cycle runs, Then the `AT_RISK` flag and alert both clear automatically.
Priority: Must · FR/story links: none existing (new since MVP-S) · Invariants: **K1**, **K13** (energy form) ·
Dependencies: ES05-S06, ES03-S05 · Estimate: 2 h
Decision: D-6 · Status: **built** · Code: `orchestrator/src/opengrid/allocator/energy_sufficiency.py` · Test:
`orchestrator/tests/unit/engine/test_energy_sufficiency_gateway.py` (12 cases, e.g.
`test_missing_soc_flags_at_risk_and_raises_alert:263`, `test_at_risk_flag_is_set_on_entry_and_cleared_on_recovery:354`,
`test_two_obligations_sharing_a_bank_the_other_ones_energy_is_excluded_k2:287`),
`orchestrator/tests/unit/allocator/test_energy_sufficiency.py`

**ES05-S09 — Commitment basis: FIXED (schedule) vs NEED (measured), guarded need-basis release.**
As a *partner-program manager*, I want a commitment's delivery basis to be either FIXED (the schedule is the
grant) or NEED (the committed kW is a reserved maximum; delivered kW follows the customer's own measured signal,
`R-GRANT-CLOSED-LOOP`), with the guardian independently checking a need-basis release before it ever signs one,
so a closed-loop customer (DATA_CENTER, PIPELINE_AC) is never over- or under-delivered against its true need
while the reservation itself is still never resold (owner decision 2026-09-26, K13 additions).
- Given a FIXED-basis obligation, When a grant is proposed below its scheduled kW with no L0-L2/infeasibility
  reason, Then guardian G-19 refuses it exactly as any other commitment-lock violation.
- Given a NEED-basis obligation whose service profile's `setpoint_source` is `MEASURED_FEEDBACK`, When a grant
  below the reserved maximum is proposed with reason `R-GRANT-CLOSED-LOOP`, Then guardian G-19 signs it only
  after independently confirming (a) the profile really is `MEASURED_FEEDBACK` and (b) the unused reservation is
  not granted to any other obligation.
- Given the same reduction is proposed against a FIXED-basis profile, When guardian evaluates it, Then it is
  vetoed `NEED_BASIS_PROFILE_NOT_MEASURED_FEEDBACK` even carrying the same reason code.
- Given delivered kW is below the reserved maximum on a NEED basis, When the invariant checker runs, Then this
  is counted as compliant, never as a shortfall.
Priority: Must · FR/story links: new `FR-ARB-014`-adjacent (K13 additions, `00-invariants.md`) · Invariants:
**K13** (need basis) · Dependencies: ES05-S02, ES06-S02 · Estimate: 3 h
Decision: D-18 · Status: **built** · Code: `orchestrator/src/opengrid/guardian/checks.py:276,295-301` (need-basis
G-19 check), `orchestrator/src/opengrid/core/reasons.py:42` (`R_GRANT_CLOSED_LOOP`),
`orchestrator/src/opengrid/invariants/checks.py:341-343`, `orchestrator/src/opengrid/invariants/queries.py:192-205` ·
Test: `orchestrator/tests/unit/guardian/test_service.py:791` (`test_need_basis_grant_below_the_reserved_maximum_is_signed`),
`:800` (`test_the_same_grant_on_a_fixed_profile_is_vetoed`), `:811`, `:821`, `:835`

**ES05-S10 — Dispatcher models the maximum discharge-flow limit at every level (D-26).**
As a *fleet reliability engineer*, I want the selector and allocator to model, not just the built-in hub P/kVA/
ramp caps, the full discharge-flow envelope — SoC- and temperature-derated power ($P_{max}(SoC,T)$), the home
export limit net of the home's own load, and the service-transformer, feeder and substation loading limits
including reverse flow, plus the sustained-vs-peak distinction — so a plan is never infeasible in practice
against a physical limit the model doesn't know exists (owner requirement, `09-optimizer-dispatcher-update.md`
§1.9 F1-F7).
- Given a forecast cell temperature and SoC path, When the selector builds a bank's capability row, Then its
  discharge cap follows the piecewise-linear $f^{dis}_{SoC}(s)\cdot f^{dis}_T(T)$ curves, not a flat rating.
- Given a bank's net home load forecast, When the selector/allocator size the export/import rows, Then
  discharge first serves the home; only the excess is allowed to export, bounded by the interconnection limit.
- Given a service transformer, feeder or substation shared by several banks, When a cycle's batch is sized, Then
  the aggregate flow (including reverse/charging flow) stays within that asset's rating in both directions.
- Given a lease requests power above the continuous rating, When sized, Then it is allowed only for ≤ the peak
  duration and within the reported peak-power budget.
Priority: Should (next-phase; regulated-pilot flow limits) · FR/story links: none existing (new) · Invariants:
**K4** (physical envelope, extended) · Dependencies: ES05-S06, ES19-S02 · Estimate: 4 h
Decision: D-26 · Status: **partly built in R2** (`main` `6470cfa`), in the real-time allocator only:
- F1 derating: always on (`orchestrator/src/opengrid/allocator/flow_limits.py:49-68`, applied at
  `allocator/cycle.py:123-135`).
- F2 export cap (export side only, `flow_limits.py:78-88`) and F3 transformer, feeder and substation discharge
  budgets (`flow_limits.py:108-182`): behind `[allocator.flow_limits].enabled = true`, but no repo seed writes
  the limit rows (migration 0029), so nothing binds yet.
- Not built: the selector models none of these (the first and second criteria above are unmet at the planning
  level), there are no import/charging-direction caps, and above-continuous power is never planned (the fourth
  criterion is met only by never using the peak).

Test: `orchestrator/tests/unit/allocator/test_dispatch_extensions.py:295` (F1 uses the guardian's derating),
`:319` (export cap serves home load first), `:335` (the cycle applies derating, export and transformer caps),
`:351` (feeder budget split), `:363` (property: allocator cap never above the derated bound).

**ES05 subtotal: 10 stories, 32 h.**

---

### ES06 — Guardian & safe stop

Goal: an independent sole signer that never modifies a command, only PASS/VETO/HOLD, with **G-19** as the
commitment-lock check; a stop-only authority scoped to fleet/zone/bank. Modules: `guardian`, `safestop`.
Workstream: **WS5**.

**ES06-S01 — Guardian core checks: reserve, ramp, P/kVA limits, lease/epoch, Ed25519 signature.**
As an *auditor*, I want guardian to independently re-validate every proposed command against physical/contractual
limits and the homeowner reserve, sign only what passes, and hold (never veto-as-modify) anything it cannot
verify.
- Given a command exceeding a limit or breaching the reserve floor, When it reaches guardian, Then it is blocked
  even if the allocator would have allowed it — zero exceptions.
- Given a valid batch, When guardian signs it, Then every command carries a verifiable Ed25519 signature and a
  lease/epoch consistent with the current issuer.
- Given guardian gives no verdict within its budget or is unreachable, When the allocator runs, Then the batch
  stays unsigned, commands in force run to their lease, and the on-call is paged — a timeout is never a veto and
  never a stop.
Priority: **Must (never-cut)** · FR/story links: `SAFE-S01`, `SAFE-S02`, `FR-SAFE-001`, `FR-SAFE-002`,
`FR-SAFE-014` · Invariants: K1, K3 (sole signer), K4 (limits/ramp), K6 (lease/epoch) · Dependencies: ES03-S03,
ES05-S05 · Estimate: 4 h
Decision: D-25 (confirmation only — Base confirmed the 20% discharge floor already built here as G-01; no
behaviour change) · Status: **built** · Code: `orchestrator/src/opengrid/core/limits.py:30-62` · Test:
`orchestrator/tests/unit/guardian/test_service.py` (G-01 cases), `orchestrator/tests/property/test_k04_envelope.py`

**ES06-S02 — Guardian check G-19: refuse a batch that reduces a committed allocation without cause.**
As an *auditor*, I want guardian to independently refuse to sign any batch that reduces an active committed
allocation unless it carries a valid L0/L1/L2/infeasibility reason code, so the commitment lock is enforced twice
— once by construction in the allocator, once independently by the untrusted-by-design dispatcher's watchdog
(review §3.3 item 2, the new **G-19**).
- Given a batch that reduces a committed $\hat y$ with a valid `R-COMMIT-LOCK-OVERRIDE-L0/L1/L2` or
  `R-COMMIT-LOCK-INFEASIBLE` reason code, When guardian evaluates it, Then it signs.
- Given a batch that reduces a committed $\hat y$ with no such reason code, or an invalid one, When guardian
  evaluates it, Then it refuses to sign — the reduction never reaches a hub.
- Given a fixture of 100 batches, 10 of which illegally reduce a commitment, When run through guardian, Then
  100% of the 10 are refused and 100% of the 90 legitimate batches sign.
Priority: **Must (never-cut)** · FR/story links: new `FR-ARB-014` (G-19 is its enforcement point); extends the
`SAFE-S01`/`FR-SAFE-001` pattern to a new check class · Invariants: **K13** · Dependencies: ES06-S01, ES05-S02,
ES05-S03 · Estimate: 3 h

**ES06-S03 — Scoped safe stop: fleet, zone, bank — stop-only, never releases.**
As a *control-room operator*, I want to engage a scoped safe stop (fleet/zone/bank) on a two-step confirmation,
executed at once via a retained MQTT topic, using a key that can only stop and never release or command.
- Given I engage a bank-scope stop with a reason and confirm twice, When confirmed, Then dispatch to that bank's
  hubs stops within one cycle.
- Given a message signed under the safe-stop-only key that is not a stop, When a hub receives it, Then it is
  rejected; a stop under the same key is accepted.
- Given a stop is engaged, When release is attempted through the safe-stop path, Then it is structurally
  impossible — release requires the separate operator/guardian path, out of MVP-S's automated scope, gated by
  human confirmation only.
Priority: **Must (never-cut)** · FR/story links: `SAFE-S03`, `FR-SAFE-006`, `FR-SIM-020` (safe-stop-only key
enforcement in the harness) · Invariants: K8 (stop authority: independent process, `og-safestop`, works with the
engine and the guardian both down) · Dependencies: ES03-S03, ES06-S01 · Estimate: 3 h

**ES06-S04 — TIMEOUT ≠ VETO ≠ STOP; on-call paging.**
As a *platform SRE*, I want the three failure semantics kept strictly distinct — a guardian timeout holds and
pages, a veto blocks that batch only, a stop is the only thing that trips delivery — so no slow component ever
turns into an accidental fleet stop.
- Given a guardian verdict misses its time budget, When it happens, Then the batch holds, on-call pages, and no
  stop is triggered.
- Given an explicit invariant veto on > 5% of a tick's commands, When it happens, Then the scope goes
  CONSERVATIVE, still distinct from a stop.
- Given three consecutive CONSERVATIVE ticks from vetoes, When observed, Then a scoped safe stop is *requested*
  of a person — never engaged automatically.
Priority: Must · FR/story links: `SAFE-S02`, `FR-SAFE-014`, `FR-SAFE-016` · Invariants: K7 (degrade, don't trip:
TIMEOUT ≠ VETO ≠ STOP) · Dependencies: ES06-S01, ES07-S01 · Estimate: 2 h
Status: **built** · Code: `orchestrator/src/opengrid/guardian/escalation.py:29-32` (`Posture`
NORMAL/CONSERVATIVE, `TransitionKind` incl. `REQUEST_SAFE_STOP`), `:142-143` (`ALR-SCOPE-CONSERVATIVE`,
`ALR-SAFE-STOP-REQUESTED`) · Test: `orchestrator/tests/unit/guardian/test_escalation.py`
(`test_more_than_five_percent_vetoed_makes_bank_and_zone_conservative:49`,
`test_three_consecutive_conservative_ticks_request_a_stop_once:64`, `test_recovery_clears_and_the_count_restarts:76`,
`test_the_guardian_escalation_path_has_no_way_to_engage_a_stop:145`)

**ES06-S05 — Feeder/substation ramp ceiling for firm events.**
As a *platform SRE*, I want a per-feeder/substation ramp ceiling enforced by guardian, independent of the fleet-
wide 50 MW/min cap, so a firm event cannot be exempted into an unsafe local ramp (review §6 finding #4).
- Given a firm event that would exceed the configured feeder ramp ceiling, When guardian evaluates the batch,
  Then it is refused or reshaped to the ceiling, never signed as proposed.
- Given a batch within every ceiling, When evaluated, Then it signs normally.
Priority: Should (cut candidate only if G3 slips further than cut line 1; not on the plan's explicit cut list) ·
FR/story links: `DISP-S06`'s L2262 gap noted in review finding #4; nearest existing FR: `FR-DISP-011` (feedback
damping, same control family) · Invariants: K4 (physical envelope, guardian check **G-06**) · Dependencies:
ES06-S01 · Estimate: 2 h

**ES06-S06 — Guardian check G-20: refuse to sign on degraded clock quality (K12, new).**
As an *auditor*, I want guardian to refuse to sign any batch whenever its own clock's offset from NTP exceeds a
configured limit, because every lease, epoch and freshness check (K6) is arithmetic on that clock, so a silently
skewed guardian clock could make stale commands look fresh (00-invariants.md K12).
- Given the guardian's measured NTP offset is within `guardian.clock_offset_max_ms`, When a batch is evaluated,
  Then G-20 passes and the other checks proceed normally.
- Given the guardian's measured NTP offset exceeds the configured limit, When any batch is evaluated, Then
  guardian signs nothing for that cycle (a hold, not a veto and not a stop) and an alert fires.
- Given the clock recovers within limit, When the next cycle runs, Then signing resumes automatically, with the
  transition traced.
Priority: **Must (never-cut)** · FR/story links: new (review §6 finding #14, formalized here as guardian check
**G-20**); no existing FR predates this check · Invariants: **K12** (time quality) · Dependencies: ES06-S01 ·
Estimate: 2 h

**ES06-S07 — Two-person safe-stop RELEASE: `og-op-a`/`og-op-b` Tier-2 approval.**
As a *control-room operator*, I want a stopped scope's RELEASE to require two named operators' approval routed
through the guardian's own signing path (Tier-2), never the safe-stop-only key, so ES06-S03's "stop-only, never
releases" key stays true while a real release path still exists for a person to use (decision log D-12: named
dev accounts `og-op-a`/`og-op-b`; the release mechanism itself is `02b` §8 auth plus the guardian signing path).
- Given a stopped scope, When a release is requested, Then it is published only after a guardian-signed Tier-2
  approval is relayed — never by the safe-stop-only key, which the relay verifies never produced it.
- Given a release request signed by anything other than a valid guardian Tier-2 approval (forged, self-signed by
  `og-safestop`, or missing the public key), When received, Then the relay refuses it and republishes nothing.
- Given a valid release is relayed twice (retry), When the second copy arrives, Then it is idempotent by
  signature — no duplicate release event.
- Given the retention window for a released stop topic elapses, When the cleanup job runs, Then the retained
  MQTT topic is cleared with a zero-length payload.
Priority: **Must (never-cut, K8 companion)** · FR/story links: `SAFE-S03`-adjacent (release is the other half of
ES06-S03's stop) · Invariants: **K8** (stop-only key still never releases; release is a *separate*, guarded path) ·
Dependencies: ES06-S01, ES06-S03 · Estimate: 2 h
Decision: D-12 · Status: **built** · Code: `orchestrator/config/orchestrator.toml:95`
(`stop_release_authorised_operators = ["og-op-a", "og-op-b"]`), `:132` (`api.roles.operator`),
`orchestrator/src/opengrid/safestop/__init__.py:66`, `orchestrator/src/opengrid/safestop/service.py:36,216` ·
Test: `orchestrator/tests/unit/safestop/test_release_relay.py`
(`test_a_guardian_signed_release_is_published_on_the_engage_topic_and_recorded:122`, `test_relay_is_idempotent_by_signature:136`,
`test_anything_but_a_valid_guardian_tier2_release_is_refused:159`, `test_safestop_itself_still_never_releases:186`,
`test_k8_property_no_altered_or_foreign_release_is_ever_relayed:235`,
`test_released_stop_topics_are_cleared_after_the_retention_window:263`);
`tests-e2e/functional/safety/test_ts06_guardian_and_safe_stop.py:24-25` (`OPERATOR_A`/`OPERATOR_B` = `og-op-a`/`og-op-b`)

**ES06-S08 — Guardian PQ checks G-21..G-25: envelope, ride-through/asset-state, calibration safety (K14).**
As an *auditor*, I want guardian to independently re-check every dispatch's power-quality impact — per-phase
imbalance (G-21), THD (G-22), frequency/voltage deviation (G-23), ride-through/asset-state conformance (G-24) —
against the customer's own envelope, and to gate any remote inverter-recalibration command on its own bounds,
rate limit and fleet budget (G-25), so a K14 breach or an unsafe calibration command can never reach a hub even
if the allocator's own PQ eligibility check is wrong (K14, approved by the owner 2026-09-25).
- Given a proposed batch's modelled/measured PQ impact exceeds the tightest active envelope limit on any of
  imbalance, THD or frequency/voltage, When guardian evaluates it, Then the matching check (G-21/G-22/G-23)
  vetoes it — PARTLY_VETOED if only some hubs are affected.
- Given a hub is quarantined, excluded, or (for a PQ-sensitive profile) degraded, When any non-HOME command is
  proposed for it, Then G-24 vetoes it; HOME's own service is never gated by G-24.
- Given a `CalibrationCommand`, When proposed, Then G-25 signs it only if its bounds are within the firmware
  limit, its rate/lease are valid, no active PQ-sensitive grant conflicts, and the fleet-wide recalibration budget
  is not exceeded.
Priority: **Must (never-cut, K14)** · FR/story links: none existing (K14 is new since MVP-S) · Invariants:
**K14** · Dependencies: ES06-S01, ES04-S06 · Estimate: 3 h
Decision: D-7 · Status: **built** · Code: `orchestrator/src/opengrid/guardian/pq_checks.py:42-223`
(`check_g21_phase_imbalance`, `check_g22_thd`, `check_g23_freq_voltage_deviation`, `check_g24_asset_conformance`,
`check_g25_calibration_safety`, `check_g25_fleet_budget`, `check_g25_calibration_lease`) · Test:
`orchestrator/tests/unit/guardian/test_pq_checks.py` (27 cases, e.g. `test_g21_phase_imbalance_positive:42`,
`test_g24_positive_home_never_gated:150`, `test_g25_negative_active_sensitive_grant_ts18:246`);
`orchestrator/tests/property/test_k14_pq_envelope.py:228` (`test_k14_guardian_g21_to_g23_veto_exactly_when_their_own_dimension_breaches`),
`:250`, `:270`

**ES06-S09 — Guardian independently enforces every discharge-flow limit, fails closed on missing data (D-27).**
As an *auditor*, I want guardian to independently re-check, on its **own** reads (never the allocator's claimed
capability), every level of the discharge-flow envelope the dispatcher models in ES05-S10 — derated
$P_{max}(SoC,T)$, the per-home export/import limit net of load, service-transformer/feeder/substation loading in
both directions (including reverse flow), territory (K15), and sustained-vs-peak — with any missing or stale
input failing closed (relief always still passes), so a dispatcher bug or a stale read can never produce a
signed command that exceeds a physical or contractual limit (owner requirement, D-27; guardian checks G-02
(changed) and new G-26…G-33, final numbering in `00-invariants.md` "Flow limits and territory", matching
`09-optimizer-dispatcher-update.md` §2.5-2.6).
- Given a stale or missing SoC/temperature/BMS-limit reading, When a discharge command is evaluated, Then the
  derated-power check (G-02) fails closed (a conservative bound, e.g. `f_T := 0.5` or stricter per config), never
  an optimistic one.
- Given a stale or missing home meter reading, When an export/import command is evaluated, Then G-26 fails
  closed (assumes full-PV/no-load for the export side, full service rating for the import side) so discharge is
  never vetoed less than the conservative case requires.
- Given a stale service-transformer, feeder or substation SCADA reading, When a batch would *increase* the
  magnitude of flow on that asset, Then it is vetoed; a batch that only relieves an existing violation is never
  vetoed regardless of staleness.
- Given a hub or asset's territory is unknown, When a command is proposed for it, Then it is vetoed rather than
  assumed in-territory (K15 fail closed).
Priority: Should (next-phase; regulated-pilot flow limits, pairs with ES05-S10) · FR/story links: none existing
(new) · Invariants: **K4** (extended), **K15** (new) · Dependencies: ES06-S01, ES05-S10, ES19-S02 · Estimate: 4 h
Decision: D-27 · Status: **built in R2** (`main` `6470cfa`) — `orchestrator/src/opengrid/guardian/flow_checks.py`
implements G-26 (`:164`), G-27 (`:219`), G-28/G-29/G-30 on the aggregate flows (`:261`, plus `:296` for a substation
POI), G-31 (`:88`) and G-33 (`:317`, on `market/territory.py:131`), and G-02 changed to the derated bound
(`:133`); G-32 is `core/limits.py:360`. All are wired on the guardian's own reads in `guardian/service.py`
(`:328`, `:383-390`, `:427`, `:452-493`, `:539-541`, `:584`; `guardian/main.py:349` wires the topology port).
On a stale reading or an unknown limit, the aggregate check vetoes any increase and passes relief
(`flow_checks.py:261-293`). Caveats at R2:
- G-31's peak path is dead. The guardian reads the budget as `peak_budget_kws` (`guardian/mqtt_io.py:36`) while
  hubs send `peak_power_budget_kws`, so every above-continuous setpoint is vetoed. This is safe, and reported.
- G-29 and G-30 evaluate nothing until substation assets tied to a bank or regulated-zone banks exist.
- Aggregate flows sum unsigned SCADA apparent power as import (`guardian/flow_repo.py:40-48`), so a measured
  reverse flow is not seen.
- `[guardian.flow].telemetry_required` is false: a flow field a hub has never reported falls back to the static
  premise limits.

The ERCOT_AS energy hold is not a guardian check; the selector and engine enforce it and `CHECK_AS_HOLD` measures
it · Test:
`orchestrator/tests/unit/guardian/test_flow_checks.py` (29), `orchestrator/tests/unit/guardian/test_service_flow.py` (14)

**ES06 subtotal: 9 stories, 25 h.**

---

### ES07 — Health & degraded modes

Goal: heartbeats, feed/hub health, cycle latency, alerting, and the two named degraded modes (feed-stale → no new
commitments; engine down → hubs hold lease). Module: `health`. Workstream: **WS5**.

**ES07-S01 — Module heartbeats and `/status` + `/metrics`.**
As a *platform SRE*, I want every one of the 7 processes (including `og-safestop`) emitting a heartbeat and Prometheus-format `/metrics`,
so a hung or crashed process is visible within one heartbeat interval.
- Given a process stops heartbeating, When one interval elapses without one, Then health marks it down and an
  alert fires.
- Given `/metrics` is scraped, When read, Then cycle latency, feed freshness and hub counts by state are present.
Priority: Must · FR/story links: `OPS-S01`, `FR-OPS-001`, `FR-OPS-002` · Invariants: — · Dependencies: ES01-S03 ·
Estimate: 2 h

**ES07-S02 — Feed-stale degraded mode: no new commitments.**
As a *control-room operator*, I want the fleet to stop admitting new commitments — while continuing to deliver
every already-committed obligation — the moment a feed the selector depends on goes stale past its threshold,
so a bad feed can never corrupt a fresh commitment decision (mandatory degraded-mode story, plan A6).
- Given a price or load feed exceeds its staleness threshold, When detected, Then the selector refuses any new
  admission or gate solve using that feed, while ES05-S02's already-committed obligations continue delivering
  unaffected.
- Given the feed recovers within its freshness window, When detected, Then normal admission resumes automatically
  and the transition is traced.
- Given the feed stays stale past a second, longer threshold, When crossed, Then the console shows "degraded:
  feed stale" distinctly from "degraded: engine down" (ES07-S04).
Priority: **Must** · FR/story links: `ING-S09`, `FR-ING-015` (staleness alarm); nearest degraded-mode FR:
`FR-DISP-020` (scoped degraded mode) · Invariants: K13 (a stale-feed admission freeze protects the integrity of
future commitment decisions the lock will then freeze) · Dependencies: ES02-S04, ES05-S01 · Estimate: 3 h
Status: **partly built** · Code: `orchestrator/src/opengrid/health/model.py:22-23` (`DegradedMode` literal
`"NO_NEW_COMMITMENTS"`) raises the mode. At `434d230` nothing acted on it. At R2 (`main` `6470cfa`) it is
enforced:
- the intake gate skips intake (`orchestrator/src/opengrid/engine/gates.py:51-60`, `:123-132`);
- the selector gate withholds every new candidate, treating an unreadable mode as active
  (`orchestrator/src/opengrid/selector/gate.py:532-602`).

Contract admission and customer-API submission are not gated, and tracing of the transition was not verified.
Since R2 hotfix v3 (`afb26c2`) only feeds in `[health] firm_blocking_feeds` (default the ERCOT price series
`np6-905-cd`) set the mode; the load, NWS and EIA feeds alert only.
Test: `orchestrator/tests/unit/health/test_rules.py:186` (`test_feed_stale_yields_no_new_commitments`).

**ES07-S03 — Hub health: online / stale / fault classification.**
As a *fleet reliability engineer*, I want every hub classified online, stale (unresponsive) or fault (not
communicating / fault code), distinctly, so operators and the allocator both know what "healthy" means per hub.
- Given a hub misses its telemetry interval while its session is alive, When detected, Then it is classified
  `stale`.
- Given a hub's session drops entirely or it reports a fault code, When detected, Then it is classified `fault`,
  a distinct state from `stale`.
Priority: Must · FR/story links: `DEV-S03`, `DEV-S04`, `FR-DEV-007`, `FR-DEV-008`, `FR-DEV-009` · Invariants: —
· Dependencies: ES03-S01, ES07-S01 · Estimate: 2 h

**ES07-S04 — Cycle latency measurement, alerts, and engine-down degraded mode.**
As a *platform SRE*, I want RT cycle latency measured continuously against the p99 < 500 ms target at 2,000 hubs
(A11), alerted before breach, and an "engine down → hubs hold lease" degraded mode demonstrated by killing the
engine process.
- Given the RT cycle runs at 2,000 hubs, When measured over a 30-minute window, Then p99 < 500 ms, with the 10k
  attempt recorded separately as "measured, not met" if the cut line 4 applies.
- Given cycle latency approaches (not yet breaches) the 500 ms target, When it crosses a pre-limit threshold,
  Then an alert fires before the hard limit.
- Given the engine process is killed, When hubs' 30 s leases expire with no renewal, Then every hub holds its
  last setpoint (local autonomy), and health shows "degraded: engine down" within one heartbeat interval.
Priority: **Must** · FR/story links: `OPS-S05`, `FR-OPS-008`; performance basis `FR-RPT-013` (benchmark
provenance) · Invariants: K7 (lease expiry → local autonomy, engine-down degraded mode) · Dependencies: ES05-S06,
ES03-S03, ES07-S01 · Estimate: 3 h
Status: **built** · Code: `orchestrator/src/opengrid/health/model.py:24` (`DegradedMode` literal
`"HOLD_LOCAL_AUTONOMY"`) · Test: `orchestrator/tests/unit/health/test_rules.py:192` (`test_engine_down_yields_hold_local_autonomy`)

**ES07-S05 — Guardian-down (HOLD) and SCADA-silent (`DIST_DEFERRAL` open loop) degraded modes.**
As a *platform SRE*, I want the two remaining named degraded modes — guardian down/verdict-timeout (`HOLD`) and
a `DIST_DEFERRAL` bank's SCADA going silent (`DIST_DEFERRAL_OPEN_LOOP`) — surfaced on the health screen exactly
like the feed-stale and engine-down modes already are, so every degraded mode the system can enter is one an
operator can see and name, not just the two most-demoed ones (`02b` §6.5 rows 3 and 5).
- Given the guardian process is down or every verdict this cycle times out, When health evaluates the cycle,
  Then it reports `HOLD`, distinct from `HOLD_LOCAL_AUTONOMY` (engine down) and from a plain per-process
  heartbeat-down alert.
- Given a `DIST_DEFERRAL` bank's simulated SCADA stops publishing, When health evaluates the bank, Then it
  reports `DIST_DEFERRAL_OPEN_LOOP` for that bank (the PI loop falls back to an open-loop schedule, cut line 2's
  behaviour, but now as a live-detected degraded mode rather than only a planned cut).
- Given several degraded modes are active at once (e.g. a stale feed and a down guardian), When the health
  screen renders, Then all active modes are shown together, never only the first one found.
Priority: Must · FR/story links: none existing (fills a gap: `02b` §6.5 defines 5 degraded-mode rows, MVP-S's
ES07 only told the story of 2 of them) · Invariants: K7 (degrade, don't trip), K9 (one loop per quantity — the
PI's open-loop fallback is the "outer loop becomes feed-forward only" case) · Dependencies: ES06-S01, ES07-S01,
ES05-S06 · Estimate: 2 h
Status: **partly built**. `HOLD` is live. `DIST_DEFERRAL_OPEN_LOOP` could never be produced at `434d230` and
`6470cfa`. R2 hotfix v3 (`afb26c2`) raises it with `ALR-SCADA-SILENT` after 60 s without any SCADA reading
(`orchestrator/src/opengrid/health/rules.py:204-240` there). It is fleet-wide rather than per bank as the second
criterion asks, and the PI loop does not fall back to open loop: only the UI reads the mode · Code:
`orchestrator/src/opengrid/health/model.py:22-27` (all four `DegradedMode` values) · Test:
`orchestrator/tests/unit/health/test_rules.py:198` (`test_guardian_down_yields_hold`), `:204`
(`test_degraded_modes_can_combine`); at `afb26c2` also `test_rules.py:218` (SCADA silent) and
`orchestrator/tests/unit/health/test_init.py:389-431`

**ES07 subtotal: 5 stories, 12 h.**

---

### ES08 — Settlement (M&V, billing, profitability)

Goal: interval metering against a baseline, insert-only invoicing, and profitability reporting including the
lock's forgone upside. Module: `settle`. Workstream: **WS6**.

**ES08-S01 — Interval metering and baseline (M&V).**
As a *utility grid-ops engineer*, I want interval metering reconciled against a baseline for every obligation, so
delivery is measured against a defensible reference, not asserted.
- Given an active obligation window, When metered, Then interval resolution is ≤ 1 minute for 100% of sampled
  windows.
- Given a closed window, When reconciled, Then it completes within 24 hours and any out-of-tolerance variance is
  flagged.
Priority: Must · FR/story links: `MV-S01`, `MV-S02`, `FR-MV-001`, `FR-MV-002`, `FR-MV-003` · Invariants: P8 ·
Dependencies: ES03-S01, ES04-S01 · Estimate: 3 h

**ES08-S02 — Performance % and insert-only invoice lines.**
As a *billing admin*, I want a performance percentage applied automatically at settlement and every invoice line
insert-only and versioned, so no dispatched obligation goes unbilled and no line is ever silently rewritten.
- Given a full run with all five A5 services dispatching, When checked, Then every dispatched obligation has a
  settlement record.
- Given a settlement line exists, When an update is attempted, Then it is refused; a correction adds a
  superseding version instead.
Priority: Must · FR/story links: `BILL-S01`, `BILL-S05`, `FR-BILL-001`, `FR-BILL-006`, `FR-BILL-012` ·
Invariants: K13 adjacent — settlement never rewrites a delivered commitment's record · Dependencies: ES08-S01 ·
Estimate: 3 h

**ES08-S03 — Profitability per obligation, service and interval.**
As a *settlement/finance analyst*, I want revenue, energy cost, degradation, penalty and net margin computed per
obligation/service/interval, so profitability is decomposed, not a single opaque number.
- Given a settled obligation, When profitability is computed, Then it decomposes into value, energy cost,
  degradation (\$0.03/kWh floor), penalty and net margin, matching the LP's own objective terms.
Priority: Must · FR/story links: `ARB-S03`, `FR-ARB-003` · Invariants: — · Dependencies: ES08-S02 · Estimate: 2 h

**ES08-S04 — Value vs rule-baseline and forgone upside from the lock.**
As a *Base executive*, I want the LP's net value compared against the rule-baseline allocator (ES05-S07) run in
shadow, and the honest cost of the commitment lock reported as a separate "forgone spot upside" line, so the
lock's cost is measured, not assumed (review §3.4, KPI-22).
- Given the LP and the rule baseline both ran on the same tick's inputs, When compared, Then the value added by
  the LP is a computable, displayed number.
- Given a committed obligation held capacity that a genuinely new, higher-value opportunity could not use, When
  settled, Then the forgone value is reported as a distinct "forgone spot upside" line, never netted silently
  into net margin.
- Given cut line 3 applies (G4, if the team is behind), When it applies, Then this story is the one dropped —
  the UI still shows raw profitability without the baseline comparison.
Priority: Must (explicit cut-line candidate — cut line 3) · FR/story links: `RPT-S06` (value of orchestration,
KPI-22), `FR-RPT-011` · Invariants: — · Dependencies: ES08-S03, ES05-S07 · Estimate: 3 h

**ES08-S05 — CSV export of invoice lines and M&V records.**
As a *billing admin*, I want invoice lines and M&V performance records exportable as CSV, so a partner or judge
can inspect the numbers outside the console.
- Given a period's invoice lines, When exported, Then the CSV matches the underlying records exactly, including
  superseded-line links.
Priority: Should · FR/story links: `BILL-S06`, `FR-BILL-009` · Invariants: — · Dependencies: ES08-S02 ·
Estimate: 1 h

**ES08 subtotal: 5 stories, 12 h.**

---

### ES09 — Audit trace & retention

Goal: every event hash-chained, chain-verifiable, and retained per a configurable per-class policy with
checkpointed pruning that keeps verification passing. Module: `trace`. Workstream: **WS1** (library) / **WS8**
(verification).

**ES09-S01 — Append-only, hash-chained trace of every event.**
As an *auditor*, I want every selection, commitment, re-nomination, exception, shortfall, command, operator
action, feed change and alert recorded in one append-only, hash-chained store (`prev_hash`/`hash`), so nothing in
the system can be silently altered after the fact.
- Given any of the nine listed event classes occurs, When it occurs, Then a trace row is appended in the same
  transaction as the causing action (never after the fact).
- Given an attempted silent alteration of a past row, When checked, Then the chain break is detectable.
Priority: **Must (never-cut)** · FR/story links: `TRACE-S01`, `TRACE-S02`, `FR-TRACE-001`, `FR-TRACE-003` ·
Invariants: P8 · Dependencies: ES01-S01 · Estimate: 3 h

**ES09-S02 — Reason codes, including `R-COMMIT-LOCK-*`.**
As an *auditor*, I want every trace entry that reduces a committed allocation to carry one of the four named
reason codes (`R-COMMIT-LOCK-OVERRIDE-L0/L1/L2`, `R-COMMIT-LOCK-INFEASIBLE`), and a replay assertion that 100% of
such reductions carry one.
- Given a full test run including every lock-exception fixture (ES05-S03), When audited, Then no committed-
  allocation reduction lacks one of the four reason codes.
- Given a manufactured reduction with no reason code, When it occurs, Then an alarm fires immediately (it should
  be structurally impossible past ES06-S02, so this is the trace-layer's own independent check).
Priority: **Must (never-cut)** · FR/story links: `TRACE-S05`, `FR-TRACE-008`, `FR-TRACE-010`; new `FR-ARB-014`
(defines the reason-code set) · Invariants: **K13** · Dependencies: ES09-S01, ES05-S03 · Estimate: 2 h

**ES09-S03 — Configurable retention per event class, with checkpointed pruning.**
As a *system admin*, I want retention set per event class (`retention.<class>.days`) and pruning that removes
rows past their retention window while keeping periodic chain checkpoints, so old data can be pruned without
breaking chain verification (review §8a decision 3, mandatory retention story).
- Given a retention policy of, e.g., 30 days for `telemetry`-linked trace rows and 400 days for commitment/
  settlement rows, When the pruning job runs, Then only rows past their class's window are removed.
- Given pruning has removed rows between two checkpoints, When chain-verify runs (ES09-S04) across the pruned
  range, Then it still passes, using the checkpoint anchors in place of the pruned rows.
- Given a class's retention is reconfigured, When changed, Then the change itself is traced and takes effect on
  the next pruning cycle, never retroactively deleting rows that were compliant when written.
Priority: **Must (never-cut — retention/pruning is part of "the trace")** · FR/story links: nearest existing:
`TRACE-S06` (keep dispatching on a signed, anchored journal — same anchor-checkpoint mechanism); new
`FR-ARB-014`-adjacent retention clause (review §8a decision 3) · Invariants: **K11** (verifiable track record —
checkpointed pruning is how retention stays compatible with chain verification), **K13** (retained events are what
proves the lock held) · Dependencies: ES09-S01 · Estimate: 3 h

**ES09-S04 — Chain-verify button/endpoint.**
As an *auditor*, I want a one-click chain-verify that walks a trace range and confirms every hash links correctly,
including across a pruned-and-checkpointed range, so "the trace is intact" is a button, not an assertion.
- Given an intact, unpruned range, When verified, Then it reports pass with the range's start/end hashes.
- Given a pruned range with valid checkpoints, When verified, Then it still reports pass.
- Given a tampered or broken-chain fixture, When verified, Then it reports fail at the exact break point.
Priority: **Must (never-cut)** · FR/story links: `TRACE-S02`, `FR-TRACE-004` · Invariants: **K13** (this is how
K13 compliance is demonstrated live, per A9) · Dependencies: ES09-S01, ES09-S03 · Estimate: 2 h

**ES09 subtotal: 4 stories, 10 h.**

---

### ES10 — Operator UI

Goal: the 7 screens plus the scenario panel, over SSE, with no build step (FastAPI + HTMX + Alpine + ECharts +
Leaflet). Module: `ui`. Workstream: **WS7**.

**ES10-S01 — Control room (screen 1).**
As a *control-room operator*, I want fleet map, live ERCOT price/load/wind/solar, fleet MW/MWh, active
commitments, today's net margin, invariant counters and alerts all on one screen, refreshed over SSE.
- Given an obligation's state changes, When it changes, Then the board reflects it within one SSE push cycle
  with no manual refresh.
- Given 2,000 live hubs, When the map renders, Then it stays within the UI performance budget by clustering.
Priority: Must · FR/story links: `UI-S01`, `UI-S10`, `FR-UI-001`, `FR-UI-002`, `FR-UI-010` · Invariants: — ·
Dependencies: ES03-S01, ES02-S04, ES05-S05 · Estimate: 3 h

**ES10-S02 — Fleet monitoring & control (screen 2): manual command + scoped safe stop.**
As a *control-room operator*, I want a bank/hub table and drill-down (SoC, P, health, lease, last command), a
manual command path through the guardian, and a scoped safe stop with two-step confirmation.
- Given I engage a stop, When the dialog opens, Then it asks for scope, reason and one confirmation, shows the
  blast radius, and executes at once through ES06-S03's path.
- Given a manual command, When submitted, Then it is signed by guardian (ES06-S01) before any hub sees it.
Priority: Must · FR/story links: `UI-S03`, `FR-UI-006`, `FR-UI-021` · Invariants: K3 (sole signer — manual command
still goes through guardian), K8 (stop authority) · Dependencies: ES03-S02, ES06-S01, ES06-S03 · Estimate: 3 h

**ES10-S03 — Dispatch & commitments (screen 3).**
As a *market/QSE trader*, I want the opportunity pipeline (offered → committed → delivering → fulfilled), a
per-bank ledger timeline, the latest selector plan and why, and real-time grants/substitutions all visible —
including the commitment lock in action during the scenario-panel demo.
- Given a contested tick, When viewed, Then winner, losers and each loser's regret are all rendered.
- Given the commitment-lock scenario is triggered (ES03-S04), When it plays, Then the screen visibly shows the
  committed obligation's granted kW holding steady while the new call is shown competing only for headroom.
Priority: Must · FR/story links: `UI-S06`, `FR-UI-012`, `FR-UI-013` · Invariants: **K13** (this screen is the
demo evidence for the lock) · Dependencies: ES05-S02, ES04-S01 · Estimate: 3 h

**ES10-S04 — Markets & feeds and Health (screens 4–5, combined for MVP-S).**
As a *platform SRE*, I want series charts with freshness/source status and forecast quantiles, and module/feed/
hub health, cycle latency, alerts and current degraded mode, on adjoining panels of one screen.
- Given a feed goes stale, When it happens, Then the freshness badge and the health panel both reflect it within
  one poll/heartbeat cycle, consistently.
- Given the current degraded mode is "feed stale" or "engine down", When displayed, Then the mode and its cause
  are both named on screen.
Priority: Must · FR/story links: `UI-S07`-adjacent (nearest: SCADA log view pattern), `FR-UI-004`, `FR-OPS-001` ·
Invariants: — · Dependencies: ES02-S04, ES07-S02, ES07-S04 · Estimate: 3 h

**ES10-S05 — Profitability (screen 6).**
As a *Base executive*, I want per service/obligation/day revenue, cost and net margin, LP vs rule baseline, and
forgone upside all on one screen — dropped to raw profitability only if cut line 3 applies.
- Given underlying settlement data changes, When it changes, Then the screen updates and every figure is one
  click from its trace.
- Given cut line 3 has applied (ES08-S04 dropped), When the screen renders, Then it degrades gracefully to raw
  profitability with no baseline/forgone-upside panel, not a broken screen.
Priority: Must (degrades under cut line 3) · FR/story links: `UI-S04`, `FR-UI-007`, `FR-UI-008` · Invariants: —
· Dependencies: ES08-S03, ES08-S04 · Estimate: 2 h

**ES10-S06 — Billing & audit (screen 7) with trace explorer, chain-verify, and the scenario panel.**
As an *auditor*, I want invoice lines and CSV export, M&V performance, a trace explorer with the chain-verify
button, and the demo scenario panel (inject partner call / price spike / mid-delivery better-paying call / feeder
overload / comms loss / stale feed / killed engine process) on one screen.
- Given an invoice-line query, When run, Then the full trace resolves through call → command → telemetry → M&V →
  settlement in one traversal.
- Given the chain-verify button, When clicked, Then it reports pass/fail with the exact range checked.
- Given any of the seven scenario-panel triggers, When fired, Then the corresponding downstream effect appears on
  the relevant screen within one cycle.
Priority: Must · FR/story links: `UI-S06`, `TRACE-S02`, `FR-TRACE-002`, `FR-BILL-009` · Invariants: **K13**
(scenario panel's mid-delivery trigger is the live commitment-lock demo) · Dependencies: ES09-S04, ES08-S05,
ES03-S04 · Estimate: 3 h

**ES10 subtotal: 6 stories, 17 h.**

---

### ES19 — Two markets (regulated utility + ERCOT)

Goal: serve a regulated vertically-integrated utility (premium capacity, territory-bound energy) alongside the
existing ERCOT free market, on Base-owned assets including new substation-sited batteries, with $/kW-in vs
$/kW-out economics and a solar-plus-off-peak charging mix. Modules: `selector`, `allocator`, `contracts`,
`settle`. Workstream: **WS4/WS6**. This epic follows owner decisions D-20..D-24 (2026-09-26) and
[`08-market-model-two-markets.md`](08-market-model-two-markets.md); its flow-limit/territory-enforcement
counterparts (D-26/D-27) are added to ES05/ES06 (ES05-S10, ES06-S09) rather than duplicated here. It is a
**next-phase** epic (§4/§5's original Fri–Sat schedule and gates G1–G5 predate it; see the note in §1).

**ES19-S01 — Selector and real-time pricing use each bank's own ERCOT load zone.**
As a *market/QSE trader*, I want every bank's energy value and headroom threshold computed from its **own**
load zone's price path, so a bank in Oncor territory is never priced off a CenterPoint or "whichever zone's row
came last" price (Frank's review finding #5; decision log D-10, "the Houston Hub bug is fixed").
- Given banks in 4 different load zones, When the selector's scenario set is built, Then each bank's LP rows use
  its own zone's P10/P50/P90 path, and load-forecast (non-price) rows never leak into the price path.
- Given a bank has no zone-specific path, When priced, Then it falls back to the documented mean-of-zones fleet
  path, never an arbitrary single zone.
- Given the real-time allocator prices headroom, When it reads the latest zone prices, Then it looks up the
  bank's own zone, not one shared row for the whole fleet.
Priority: Must · FR/story links: none existing (Frank #5 fix) · Invariants: — (economic correctness, not a
safety invariant) · Dependencies: ES02-S01, ES05-S01 · Estimate: 2 h
Decision: D-10 · Status: **built** · Code: `orchestrator/src/opengrid/selector/gate.py:272-314`
(`load_scenarios`, `scenarios_from_points`), `orchestrator/src/opengrid/selector/types.py:53-69`
(`ScenarioPrice.price_at`), `orchestrator/src/opengrid/selector/model.py:314`,
`orchestrator/src/opengrid/selector/rule_fallback.py:119,141`, `orchestrator/src/opengrid/engine/gateways.py:142-209,336-360`
(RT per-zone price and M1 lookup) · Test: `orchestrator/tests/unit/selector/test_zone_pricing.py:28`
(`test_each_bank_gets_its_own_zone_and_load_rows_are_ignored`)

**ES19-S02 — Regulated-utility contracts: premium capacity, territory-bound energy, REG-first priority.**
As a *Base executive*, I want a regulated-utility contract to be admitted, planned and delivered only from
assets inside that utility's own service territory, priced at a capacity premium ($/kW-yr) ahead of any new
ERCOT free-market opportunity at the same gate, so the two markets never blend into one undifferentiated pool
(D-20: two markets, one regulated with premium capacity and territory-bound energy, one free/ERCOT; D-21: first
utility is Austin Energy or CPS Energy, home batteries are in scope for regulated contracts).
- Given a REG(utility) obligation, When the selector reserves capacity for it, Then only assets whose
  `territory` matches that utility are ever assigned, never an ERCOT-competitive-area or other-utility asset.
- Given an asset sits inside a regulated utility's territory, When a FREE (ERCOT) opportunity is evaluated for
  it, Then it is excluded unless that utility's contract explicitly grants wholesale access (default: no access).
- Given a new REG capacity candidate and a new FREE arbitrage candidate compete at the same gate, When the
  selector solves, Then the REG candidate's value is settled first (commitments, then new regulated capacity),
  and FREE competes only for what's left — never the reverse.
- Given a home battery is enrolled under a regulated contract, When eligibility is computed, Then home batteries
  are treated as eligible regulated-capacity assets, not excluded as "substation-only".
Priority: Should (next-phase) · FR/story links: none existing (new since MVP-S) · Invariants: **K15** (new;
territory) · Dependencies: ES04-S01, ES05-S01 · Estimate: 4 h
Decision: D-20, D-21 · Status: **partly built in R2** (`main` `6470cfa`); dark in production because no
regulated-zone bank is seeded (the Austin Energy and CPS zone blocks ship `enabled: false`,
`integration-sims/config/fleet.yaml:56-72`):
- Built: `og.utility`, `og.contract.market`/`utility_id` and `REGULATED_CAPACITY`
  (`orchestrator/migrations/0025_market_model.sql`); `opengrid.market` (`orchestrator/src/opengrid/market/territory.py:131`
  `check_territory`, the one predicate); selector C25 as bank eligibility (`orchestrator/src/opengrid/selector/gate.py:489-529`)
  and the regulated-first stage R (`selector/solve.py:140-144`); allocator territory enforcement
  (`allocator/cycle.py:221-232`); guardian G-33 and G-30; settle regulated charging and capacity payment
  (`orchestrator/src/opengrid/settle/__init__.py:242-269`); the `K15_TERRITORY` checker.
- Not built: the contract-admission check (`contracts/` is unchanged; `R-TERRITORY-INELIGIBLE` is raised only by
  the guardian).
- The utility rows and a demo contract come only from the hand-applied `dev/seed/market_model_seed.sql`.

Test: `orchestrator/tests/unit/market/test_territory.py:123` (property: regulated obligations never leave their
territory), `orchestrator/tests/unit/market/test_model.py:63` (eligibility is territory-bound), `:72` (free-access
flag), `orchestrator/tests/unit/selector/test_market_economics.py:182` (territory and regulated capacity in
`prepare_obligations`), `:212` (regulated capacity selected first even when a free offer pays more).

**ES19-S03 — Substation-sited battery assets (~20 MW, Base-owned) serving regulated capacity.**
As a *Base executive*, I want a new asset class for substation-sited batteries (their own rating, SoC window,
POI and transformer limits, ramp and wear — distinct from a home bank), sited nearest a regulated data-center or
distribution-deferral customer, so Base's substation batteries can serve regulated capacity contracts alongside
home batteries (D-21: "substation battery sets of about 20 MW... Base owns all batteries").
- Given a substation asset is registered, When the selector builds its capability row, Then it uses the asset's
  own P/E/SoC-window/POI/transformer parameters, never a home-bank default.
- Given a DATA_CENTER contract sited at a substation, When eligible assets are chosen, Then the nearest
  substation asset is preferred over home banks on the same feeders.
Priority: Should (next-phase) · FR/story links: none existing (new asset class) · Invariants: **K4** (extended
envelope, per-asset), **K14** (the substation PCS is a single inverter, no √N diversity) · Dependencies:
ES19-S02 · Estimate: 3 h
Decision: D-21 · Status: **partly built in R2** (`main` `6470cfa`): data, guardian and simulator only.
- Built: `og.asset` with `asset_class` `SUBSTATION` and POI import/export limits
  (`orchestrator/migrations/0025_market_model.sql`; `orchestrator/src/opengrid/core/models/market.py`); the guardian's
  substation limit and POI check (G-29, `orchestrator/src/opengrid/guardian/flow_checks.py:296`); a simulator
  substation asset (`integration-sims/config/fleet.yaml:85-90`, `enabled: false`).
- Not built: dispatching a substation asset. The selector and engine build the market model from banks only, no
  capability row exists for an asset, and there is no nearest-substation preference.
- The dev seed's `sub-aen-01` (`dev/seed/market_model_seed.sql:82-94`) is PLANNED with no `bank_id`, so G-29's POI
  check does not apply to it; the simulator's asset id (`sub-LZ_AEN-00`) differs from the seed's.

Test: none found for substation dispatch.

**ES19-S04 — $/kW-in vs $/kW-out economics, per contract/market/fleet, with payback and the 3-year flag.**
As a *Base executive*, I want profitability reported as $/kW-in (charging cost, delivery charge, demand charges)
against $/kW-out (capacity, energy and AS revenue), net $/kW-yr, and payback (simple, effective net-of-incentive,
and discounted) against a 3-year target flag — not just the hardware capex view — so Base's "<$500/kW effective,
~3-year payback" framing can be checked against real settled data (D-20: economics are $/kW in vs $/kW out; D-23:
ROI planning assumptions, "<$500/kW" is a net effective investment, target payback ~3 years, §3c stack confirmed
reasonable by the owner).
- Given a settlement period closes for a contract, market or the whole fleet, When the profitability rollup
  runs, Then it reports $/kW-in and $/kW-out broken into their components (energy, delivery/M1, demand, solar;
  capacity, energy, AS, availability), not only a single net-margin number.
- Given the same period, When payback is computed, Then it reports the simple payback, the effective payback
  (net of incentives, e.g. an AE rebate), and the discounted multi-year payback, alongside the $7,000/11 kW
  hardware-view figure for comparison.
- Given payback exceeds 3 years, When shown, Then it is flagged (amber), never presented as meeting the target
  silently.
Priority: Should (next-phase) · FR/story links: `RPT-S06`/`FR-RPT-011` (nearest existing: ES08-S04's forgone-
upside line is a narrower, already-built precursor at the obligation level, not this contract/market/fleet $/kW
basis) · Invariants: — · Dependencies: ES08-S03, ES19-S01 · Estimate: 3 h
Decision: D-20, D-23 · Status: **built in R2** (`main` `6470cfa`), with planning capex:
- `orchestrator/src/opengrid/market/economics.py` computes $/kW-in, $/kW-out, net, and simple, effective and
  discounted payback, NPV and the 3-year flag.
- `market/view.py` rolls these up per contract, market and fleet.
- `GET /og/api/profitability/per-kw` serves them (`orchestrator/src/opengrid/api/routers/profitability_kw.py:37`),
  and the Profitability screen shows them.
- Capex and O&M are planning attributions; there is no `og.asset_finance` table.

Test: `orchestrator/tests/unit/market/test_economics.py:115` (no payback when net ≤ 0), `:151` (discounted payback
and NPV), `orchestrator/tests/unit/market/test_view.py:26` (rollup by market and fleet).

**ES19-S05 — Charging mix: ≥30% solar plus utility off-peak, M1 only in the competitive area.**
As a *Base executive*, I want charging cost split by source and territory — at least 30% solar (soft floor,
priced slack when short) with the rest at the regulated utility's off-peak/night rate inside its territory, and
the full TDSP delivery charge (M1) only on grid-drawn kWh in the ERCOT competitive area — so the charging-cost
half of $/kW-in matches Base's actual guidance (D-19: M1 is the full TDSP charge on ERCOT-competitive grid
charging, PUCT 2026-09-01 rates in `config/tdsp_tariffs.toml`; D-22: at least 30% solar, the rest at the
utility's night/off-peak rate; D-24: solar expansion widens the price swings, so charging should also run at the
midday solar dip, not only overnight).
- Given a bank in the ERCOT competitive area draws grid energy to charge, When costed, Then it carries the full
  per-TDSP M1 charge from `tdsp_tariffs.toml`; behind-the-meter solar never carries it.
- Given a bank inside a regulated utility's territory draws grid energy to charge, When costed, Then it uses
  that utility's own contracted rate, never the TDSP M1 charge (M1 is ERCOT-competitive-area only).
- Given a regulated contract's accounting period, When its solar share is checked, Then it must be ≥ 30% or the
  shortfall is priced as slack and reported, never silently absorbed.
- Given growing solar availability at midday, When the charging schedule is built, Then it prefers midday solar
  charging as well as overnight, per D-24 (see ES02-S05's forecast gap note — this depends on that forecast
  input existing).
Priority: Should (next-phase) · FR/story links: none existing (new) · Invariants: — · Dependencies: ES19-S01,
ES02-S05 · Estimate: 3 h
Decision: D-19, D-22, D-24 · Status: **partly built in R2** (`main` `6470cfa`); the selector part is built:
- the C27 soft solar floor per territory, with priced slack (`orchestrator/src/opengrid/selector/model.py:265-307`);
- regulated grid charging only in the utility's off-peak/night periods at its rate, with no M1, and M1 on
  grid-drawn kWh in the competitive area (`orchestrator/src/opengrid/market/charging.py`);
- the measured solar share per interval (D-28) stored with its source (`og.plan_energy_value.solar_share`,
  migration 0030);
- settle's M1 (`orchestrator/src/opengrid/settle/tariffs.py`), now priced on the zone's trailing grid share of
  charging (OL-5).

Gaps: no month-to-date carry-in for the floor, and whether a floor shortfall is reported was not verified. The
midday-charging preference depends on the forecast input of ES02-S05.

Test: `orchestrator/tests/unit/selector/test_plan_hardening.py:177` (a regulated bank meets the 30% solar floor and
charges from the grid only at night), `:111` (solar-share source priority),
`orchestrator/tests/unit/market/test_charging.py:46` (Austin Energy night charging), `:69` (M1 on grid kWh only),
`orchestrator/tests/unit/settle/test_tariffs.py` (M1).

**ES19 subtotal: 5 stories, 15 h.**

---

**Grand total: 11 epics, 67 stories, ≈ 178 estimated build hours** (135 h across 7 parallel workstreams from the
original MVP-S build — plan §0 targets ~24 wall-clock hours for Phase 1–2 with ~7 concurrent workstreams;
135 person-hours ÷ ~6 effective parallel streams ≈ 22.5 h wall-clock, consistent with the plan's Fri
15:00–Sat 06:00 foundation-and-modules window before Phase 3 integration — plus ≈ 43 h added post-MVP-S across
ES01/ES03/ES04/ES05/ES06/ES07/ES19 for the decisions in `11-decision-log.md` D-4..D-27 and
`09-optimizer-dispatcher-update.md`, which are **not** part of the original Fri–Sat schedule or gates G1–G5 in
§4 below and carry no wall-clock claim of their own).

---

## 3. Mandatory stories cross-check

Every story the brief requires by name:

| Requirement | Story |
|---|---|
| Better-paying call during delivery; committed call unaffected | **ES05-S02** |
| Each L0/L1/L2 exception, individually | **ES05-S03** |
| Substitution allowed (homes, not obligations) | **ES05-S04** |
| Re-nomination point allows re-selection | **ES04-S04** |
| Partial take by product rules (`min_qty`/`increment`/`block`) | **ES04-S03** |
| Retention config and pruning with chain-verify | **ES09-S03** (config+pruning), **ES09-S04** (verify) |
| Feed-stale degraded mode | **ES07-S02** |
| Guardian G-19 | **ES06-S02** |
| Guardian G-20 (time quality) | **ES06-S06** |
| Scoped safe stop, independent of engine and guardian (K8) | **ES06-S03** |
| Profitability forgone-upside | **ES08-S04** |
| 2k-hub performance target | **ES03-S03** (harness sustains 2k), **ES07-S04** (p99 < 500 ms measured) |

---

## 4. Release/dependency map and schedule

```mermaid
flowchart TB
  subgraph P0[Phase 0 — documents, Fri 12:00-15:00]
    SPEC[spec addendum 02a/02b]
    STORIES[this document]
    TESTPLAN[04-mvp-s-test-plan.md]
  end
  G1{{G1: user approves spec pack}}
  SPEC --> G1
  STORIES --> G1
  TESTPLAN --> G1

  subgraph P1[Phase 1 — foundation, Fri 15:00-19:00]
    ES01
    ES02S1[ES02-S01..03 feeds]
    ES03S1[ES03-S01..03 twin+sim]
  end
  G1 --> ES01
  ES01 --> ES02S1
  ES01 --> ES03S1
  G2{{G2: telemetry flowing, feeds stored, deploy works}}
  ES02S1 --> G2
  ES03S1 --> G2

  subgraph P2[Phase 2 — modules in parallel, Fri 19:00-Sat 06:00]
    ES04
    ES05
    ES06
    ES07
    ES08
    ES09
    ES10
  end
  G2 --> ES04
  G2 --> ES06
  G2 --> ES09
  ES04 --> ES05
  ES06 --> ES05
  ES09 --> ES05
  ES05 --> ES08
  ES06 --> ES07
  ES02S1 --> ES07
  G3{{G3: each module passes unit + property tests}}
  ES05 --> G3
  ES06 --> G3
  ES07 --> G3
  ES08 --> G3
  ES09 --> G3

  subgraph P3[Phase 3 — integration, Sat 06:00-12:00]
    E2E[End-to-end loop; scenario panel; baseline shadow]
  end
  G3 --> E2E
  ES10 --> E2E
  G4{{G4: A1-A10 pass on the server}}
  E2E --> G4

  subgraph P4[Phase 4 — verification, Sat 12:00-16:00]
    PERF[Performance 2k/10k; chaos]
  end
  G4 --> PERF
  G5{{G5: A11 pass, 0 invariant violations}}
  PERF --> G5

  subgraph P5[Phase 5 — hand-over, Sat 16:00-18:00]
    DONE[Demo script, runbook, as-built deltas]
  end
  G5 --> DONE

  classDef cut fill:#fee,stroke:#c33
  class ES05S1cut,ES05S6cut,ES08S4cut,ES03S3cut cut
```

### Schedule table (stories → phase → gate)

| Phase | Window | Stories | Gate |
|---|---|---|---|
| 0 | Fri 12:00–15:00 | (this document + 02a/02b + test plan) | **G1** |
| 1 | Fri 15:00–19:00 | ES01-S01..05; ES02-S01..03; ES03-S01..03 | **G2**: telemetry flowing, feeds stored, deploy works |
| 2 | Fri 19:00–Sat 06:00 | ES02-S04..05; ES03-S04..05; ES04-S01..05; ES05-S01..07; ES06-S01..05; ES07-S01..04; ES08-S01..05; ES09-S01..04; ES10-S01..06 | **G3**: each module passes unit + property tests |
| 3 | Sat 06:00–12:00 | Integration of all of the above; scenario panel wired; baseline shadow live | **G4**: A1–A10 pass on the server |
| 4 | Sat 12:00–16:00 | Performance run (ES03-S03, ES07-S04) at 2k then a 10k attempt; chaos drills against ES06/ES07 | **G5**: A11 pass, 0 invariant violations |
| 5 | Sat 16:00–18:00 | Hand-over: demo script, runbook, as-built deltas to `02a`/`02b` | **Done** |

### Cut-line stories (marked, per plan §6)

| Cut line | Gate | Story affected | Behavior if cut |
|---|---|---|---|
| 1 | G3 | **ES05-S01** (LP selector) | **ES05-S07** (rule baseline) goes live as primary; LP runs in shadow instead |
| 2 | G4 | **ES05-S06** (`DIST_DEFERRAL` PI loop) | Closed loop becomes an open-loop schedule |
| 3 | G4 | **ES08-S04** (baseline comparison, forgone upside) | Dropped; **ES10-S05** degrades to raw profitability only |
| 4 | G5 | **ES03-S03** (2k/10k harness) | Keep 2,000 hubs; record the 10k result as "measured, not met" |

**Never cut, regardless of gate slippage:** ES05-S02/S03/S05 (commitment lock, exceptions, one-buyer ledger),
ES06-S01/S02/S03/S06 (guardian signing, G-19, safe stop, G-20 time quality), ES09-S01/S02/S03/S04 (the trace, its
reason codes, its retention, its verify).

---

## 5. Traceability table (story → A1–A11 → existing FR/story IDs → covered by)

"Covered by" cites the `04-mvp-s-test-plan.md` test ID(s) that exercise this story, closing task 4's "every story
covered by ≥ 1 test" check. Every row below has at least one entry; none is empty.

| Story | A# | Existing FR/story IDs cited | Covered by (`04-mvp-s-test-plan.md`) |
|---|---|---|---|
| ES01-S01 | A11 | `FR-OPS-004`, `TRACE-S01` | TS-01-01 |
| ES01-S02 | A11 | `FR-DEV-017`, `FR-OPS-004` | TS-01-02 |
| ES01-S03 | A11 | `FR-OPS-012` | TS-01-06 |
| ES01-S04 | A10, A11 | `FR-OPS-011` | TS-01-07 (no duplicated functions is the first CI-blocking check this story stands up) |
| ES01-S05 | A11 | `FR-OPS-006` | TS-01-05 |
| ES02-S01 | A1 | `ING-S01`, `FR-ING-001`, `FR-ING-006`, `FR-ING-007` | TS-02-01 |
| ES02-S02 | A1 | `ING-S04`, `FR-ING-004`, `FR-ING-005` | TS-02-02 |
| ES02-S03 | A1 | `ING-S07`, `FR-ING-010`, `FR-ING-011`, `FR-ING-012` | TS-02-03, TS-02-04 |
| ES02-S04 | A1, A6 | `ING-S05`, `FR-ING-012`, `FR-ING-013`, `FR-ING-015` | TS-02-05, TS-02-07 |
| ES02-S05 | A1, A4 | `FCST-S01`, `FCST-S04`, `FR-FCST-001`, `FR-FCST-005`, `FR-FCST-008` | TS-02-06 |
| ES03-S01 | A2 | `TWIN-S01`, `FR-TWIN-001`, `FR-TWIN-009` | TS-03-04, TS-03-05 |
| ES03-S02 | A2, A5 | `TWIN-S03`, `FR-TWIN-003` | TS-03-05 |
| ES03-S03 | A2, A11 | `SIM-S07`, `SIM-S10`, `FR-SIM-013`, `FR-SIM-020` | TS-03-01 (property), TS-03-03, TS-03-07 |
| ES03-S04 | A2, A3 | `SIM-S02`, `SIM-S06`, `FR-SIM-003`, `FR-SIM-009`, `FR-SIM-012` | TS-03-06 |
| ES03-S05 | A10 | `TWIN-S05`, `FR-TWIN-007`, `FR-TWIN-008`, `FR-TWIN-011` | TS-05-01 (property) |
| ES04-S01 | A4, A9 | `CTR-S01`, `FR-CTR-001`, `FR-CTR-002` | TS-04-05, TS-04-07 |
| ES04-S02 | A4 | `ARB-S01`, `FR-ARB-001` | TS-01-02, TS-04-05 |
| ES04-S03 | A4, A5 | `CTR-S02`, `FR-CTR-003`, `FR-CTR-005`, `FR-ARB-014` | TS-04-15, TS-04-16 |
| ES04-S04 | A4 | `CTR-S10`, `FR-CTR-020` | TS-04-14 |
| ES04-S05 | A9, A10 | `ARB-S04`, `FR-ARB-006`, `FR-ARB-007` | TS-09-05, TS-04-12 |
| ES05-S01 | A4 | `PLAN-S01`, `FR-PLAN-001`, `FR-PLAN-015` | TS-05-04 |
| ES05-S02 | A4, A10 | `FR-ARB-014` (new), `FR-ARB-004` (narrowed), `ARB-S02`/`ARB-S05` (narrowed) | TS-04-01, TS-04-02 (property), TS-04-05…07 |
| ES05-S03 | A4, A9, A10 | `FR-ARB-014`, `SAFE-S01`, `FR-DISP-024` | TS-04-08…12 |
| ES05-S04 | A4, A5 | `DISP-S05`, `FR-DISP-009`, `FR-DISP-010`, `FR-DISP-014` | TS-04-03 (property), TS-04-13 |
| ES05-S05 | A4, A10 | `DISP-S08`, `FR-DISP-018`, `FR-DISP-019` | TS-05-02 (property), TS-05-07 |
| ES05-S06 | A4, A5, A11 | `DISP-S02`, `DISP-S04`, `FR-DISP-003`, `FR-DISP-004`, `FR-DISP-007`, `FR-DISP-008` | TS-05-07, TS-05-08, TS-05-09, TS-05-10 |
| ES05-S07 | A4, A7 | `PLAN-S07`, `FR-PLAN-014` | TS-05-05, TS-05-06 |
| ES06-S01 | A3, A10 | `SAFE-S01`, `SAFE-S02`, `FR-SAFE-001`, `FR-SAFE-002`, `FR-SAFE-014` | TS-06-06, TS-06-07a, TS-06-09, TS-06-10, TS-06-17 |
| ES06-S02 | A3, A9, A10 | `FR-ARB-014` (G-19 enforcement) | TS-06-14, TS-04-01 (property) |
| ES06-S03 | A3 | `SAFE-S03`, `FR-SAFE-006`, `FR-SIM-020` | TS-06-03 (property), TS-06-15, TS-06-16, TS-06-23 |
| ES06-S04 | A3, A6 | `SAFE-S02`, `FR-SAFE-014`, `FR-SAFE-016` | TS-06-04 (property), TS-06-17 |
| ES06-S05 | A3, A10, A11 | `FR-DISP-011` (nearest) | TS-06-19 |
| ES06-S06 | A3, A10 | new (guardian check G-20) | TS-06-18 (property) |
| ES07-S01 | A6 | `OPS-S01`, `FR-OPS-001`, `FR-OPS-002` | TS-07-01 |
| ES07-S02 | A6, A10 | `ING-S09`, `FR-ING-015`, `FR-DISP-020` | TS-02-05, TS-02-07, TS-07-02 |
| ES07-S03 | A2, A6 | `DEV-S03`, `DEV-S04`, `FR-DEV-007`, `FR-DEV-008`, `FR-DEV-009` | TS-07-04 |
| ES07-S04 | A6, A11 | `OPS-S05`, `FR-OPS-008`, `FR-RPT-013` | TS-N-01, TS-07-03, TS-07-05 |
| ES08-S01 | A7, A8 | `MV-S01`, `MV-S02`, `FR-MV-001`, `FR-MV-002`, `FR-MV-003` | TS-08-02, TS-08-03 |
| ES08-S02 | A7, A8 | `BILL-S01`, `BILL-S05`, `FR-BILL-001`, `FR-BILL-006`, `FR-BILL-012` | TS-08-04 |
| ES08-S03 | A7 | `ARB-S03`, `FR-ARB-003` | TS-08-06 |
| ES08-S04 | A7 | `RPT-S06`, `FR-RPT-011` | TS-08-07, TS-08-08 |
| ES08-S05 | A8 | `BILL-S06`, `FR-BILL-009` | TS-08-05 |
| ES09-S01 | A9 | `TRACE-S01`, `TRACE-S02`, `FR-TRACE-001`, `FR-TRACE-003` | TS-09-02 (property), TS-09-03, TS-09-09 (property) |
| ES09-S02 | A9, A10 | `TRACE-S05`, `FR-TRACE-008`, `FR-TRACE-010`, `FR-ARB-014` | TS-09-05 |
| ES09-S03 | A9 | `TRACE-S06`, `FR-ARB-014` (retention clause) | TS-09-01 (property), TS-09-06, TS-09-07 |
| ES09-S04 | A9, A10 | `TRACE-S02`, `FR-TRACE-004` | TS-09-04 |
| ES10-S01 | A1, A2, A4 | `UI-S01`, `UI-S10`, `FR-UI-001`, `FR-UI-002`, `FR-UI-010` | TS-10-01 |
| ES10-S02 | A3 | `UI-S03`, `FR-UI-006`, `FR-UI-021` | TS-10-02, TS-10-03 |
| ES10-S03 | A4, A9, A10 | `UI-S06`, `FR-UI-012`, `FR-UI-013` | TS-10-04 |
| ES10-S04 | A1, A6 | `FR-UI-004`, `FR-OPS-001` | TS-10-05 |
| ES10-S05 | A7 | `UI-S04`, `FR-UI-007`, `FR-UI-008` | TS-10-07 |
| ES10-S06 | A8, A9 | `UI-S06`, `TRACE-S02`, `FR-TRACE-002`, `FR-BILL-009` | TS-10-06 |

**Stories added post-MVP-S** (decision log `11-decision-log.md` D-4..D-27 and `09-optimizer-dispatcher-update.md`;
see each story's own "Decision"/"Status"/"Code"/"Test" lines in §2 for the full evidence). None of these has a
`04-mvp-s-test-plan.md` TS-ID yet — `04` is not edited by this pass — so "Covered by" here cites the real pytest
file(s) that exist today, or the `TS-19-nn` id `09-optimizer-dispatcher-update.md` §7 already proposes for `04`
to adopt, or "none found" where no test exists.

| Story | A# | Existing FR/story IDs cited | Covered by (today) |
|---|---|---|---|
| ES01-S06 | A11 | new (D-13) | `orchestrator/tests/unit/tools/test_ws_env.py`, `integration-sims/tests/test_workspace_config.py` |
| ES03-S06 | A2, A5 | new (D-11) | none found |
| ES04-S06 | A4 | new (D-7, K14) | `orchestrator/tests/unit/contracts/test_admission.py`, `orchestrator/tests/unit/profiles/test_data_center_profile.py` |
| ES05-S08 | A2, A4, A10 | new (D-6) | `orchestrator/tests/unit/engine/test_energy_sufficiency_gateway.py`, `orchestrator/tests/unit/allocator/test_energy_sufficiency.py` |
| ES05-S09 | A4, A10 | new (D-18) | `orchestrator/tests/unit/guardian/test_service.py` (need-basis cases, lines 791-835) |
| ES05-S10 | A4, A5, A10 | new (D-26) | `orchestrator/tests/unit/allocator/test_dispatch_extensions.py` (F1-F3, R2) |
| ES06-S07 | A3, A10 | new (D-12) | `orchestrator/tests/unit/safestop/test_release_relay.py`, `tests-e2e/functional/safety/test_ts06_guardian_and_safe_stop.py` |
| ES06-S08 | A3, A10 | new (D-7, K14) | `orchestrator/tests/unit/guardian/test_pq_checks.py`, `orchestrator/tests/property/test_k14_pq_envelope.py` |
| ES06-S09 | A3, A10 | new (D-27) | `orchestrator/tests/unit/guardian/test_flow_checks.py`, `test_service_flow.py` (R2) |
| ES07-S05 | A6 | new | `orchestrator/tests/unit/health/test_rules.py` (`HOLD` case confirmed; `DIST_DEFERRAL_OPEN_LOOP` not individually named — see report) |
| ES19-S01 | A4, A7 | new (D-10) | `orchestrator/tests/unit/selector/test_zone_pricing.py` (`TS-19-01` proposed) |
| ES19-S02 | A4, A5 | new (D-20, D-21) | `orchestrator/tests/unit/market/test_territory.py`, `test_model.py`, `orchestrator/tests/unit/selector/test_market_economics.py` (R2) |
| ES19-S03 | A5 | new (D-21) | none found |
| ES19-S04 | A7 | new (D-20, D-23) | `orchestrator/tests/unit/market/test_economics.py`, `test_view.py` (R2) |
| ES19-S05 | A5 | new (D-19, D-22, D-24) | `orchestrator/tests/unit/selector/test_plan_hardening.py`, `orchestrator/tests/unit/market/test_charging.py`, `orchestrator/tests/unit/settle/test_tariffs.py` (R2) |
