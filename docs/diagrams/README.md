# OpenGrid Orchestrator — Architecture Diagrams (DOC3)

Self-contained SVG/HTML written for this repository, not produced by a diagramming tool: plain SVG
elements laid out by a small throwaway script (see "How these were verified", point 4), with no external
resources (no CDN, web fonts, remote images or externally loaded scripts; inline CSS only; system font
stack). Verified against `main @ f3b3365` (this working copy, branch `wp/doc3-diagrams`, rebased onto
`f3b3365` — the fourth snapshot in this diagram set's history: `7669a7c` → `fcaa2ca` → `f3b3365`).

**These diagrams are a snapshot of `main @ f3b3365` (2026-09-26, deployed).** This round's headline: PQ
wave 2's ingestion and guardian checks are now LIVE (`9153dc2`), and the obligation lifecycle in diagram
03 is essentially fully driven — `R-ADMIT-REJECT`, the `R-RENOM-GATE` self-loop, and
`FULFILLED`/`SHORTFALL → SETTLED` all now have real production callers, leaving only the `L0`/`L1`
mid-window overrides undriven. Every one of the lead's reported commits was independently re-verified
against this checkout's code — not the commit messages — before being drawn; see "Resolved since
`fcaa2ca`" and "Still open" below for exactly what changed and what didn't. (The `7669a7c` → `fcaa2ca`
delta is kept further down, under "Resolved since `7669a7c`", as history.)

## Resolved since `fcaa2ca` (verified in code, `f3b3365`)

- **PQ wave 2 ingestion + guardian checks are LIVE** (`9153dc2`). New `opengrid.pq_ingest` package:
  `og-engine`'s MQTT ingest loop subscribes `scada/wave/+/+/+/{summary,raw}`; summaries buffer in memory
  and batch-flush (`flush_summaries()`, one `executemany`, default every 2 s) instead of one insert per
  message (fixed a stall at ~1,000 msg/s, live 2026-09-26); raw captures validate + blob-write in a
  background worker (`engine.background.BackgroundIngest`, `aa8a2d3`/`d2c57b7`) off the event loop (~34%
  of og-engine CPU moved, live 2026-09-26). ogsim's own summary gate defaults to 10 s
  (`WaveConfig.summary_interval_s`). New `guardian/pq_checks.py` (G-21 phase imbalance, G-22 THD, G-23
  freq/voltage deviation, G-24 asset-state/ride-through, G-25 calibration safety) and `guardian/pq_repo.py`
  (real Postgres ports) — confirmed called from `service.py`'s `_check_power_quality()`, itself already
  in the same violations list every other G-check feeds, and wired via `repo.build_pg_ports()`. Diagrams
  01, 02, 05, 06 updated.
- **Correction: waveform sample generation ("WP-H") is on main, since `9153dc2`.** An earlier pass of
  this diagram set marked it absent. `ogsim.fleet.wave` (a module separate from `pq.py`; its brief was
  "call ogsim.fleet.pq's access function and don't edit it") builds real sample arrays:
  `synthesize_raw_capture()` (`wave.py:282`) synthesizes v(t)/i(t) from S7.4's
  sinusoid-plus-dominant-harmonics model, quantized to int16, and `build_summary_message()`
  (`wave.py:184`) builds the periodic summary. `runtime.py`'s `FleetEngine` wires both in: a 1 %/min
  `RotatingAuditSampler` (`wave_rotating_audit_captures()`, `runtime.py:170-186`), published every tick
  (`runtime.py:454-455`); `__main__.py:113-123` answers an inbound `.../request` through
  `handle_wave_capture_request()`. The earlier pass read only `ogsim.fleet.pq.inverter_state()`'s
  docstring ("never generates samples itself") and did not grep for `wave.py`. Diagrams 01 and 06
  corrected.
- **Hub acks persisted; `obligation.at_risk` now set AND cleared; opportunity decisions recorded**
  (`01fbd38`). `fleet.flush()` writes `og.command_ack` (new migration `0012`) from the buffered MQTT
  `ack/#` stream. `EnergySufficiencyGateway` now calls the new `contracts.set_obligation_at_risk()` on
  both edges (`True` on entry, `False` on recovery) instead of only alerting. `selector.commit`'s
  `_record_opportunity_decision()` mirrors every `SELECTED`/`REJECTED` obligation outcome onto its
  opportunity. Diagrams 01, 03, 05 updated.
- **`DATA_CENTER` service type shipped** (`ffbef06` the profile template, `be4bb34` the follow-up):
  migration `0013` widens `og.contract.service_type`'s CHECK (additive); `core.models.engine.ServiceType`,
  `api.schemas` and `allocator.models` all reuse the one core literal; the selector treats it as FIRM
  (firm bridging capacity); settle meters it at the site meter (`AMI_INTERVAL`). Diagram 05 updated.
- **`FULFILLED`/`SHORTFALL → SETTLED` is now driven** (`289d68f`). New `settle.close_settled_obligations()`
  calls `contracts.transition_obligation(..., "SETTLED", reason_code="R-SETTLED")` for every
  `fetch_settleable_obligations()` result, wired into `og-settle`'s own job list. Diagram 03's residual gap
  from the prior round is closed.
- **`OFFERED → REJECTED` (`R-ADMIT-REJECT`) is now driven** (`289d68f`), for an offer above fleet capacity.
  New `selector.commit.reject_structurally_infeasible()` (`structurally_infeasible()`: requested/min-qty
  kW exceeds the sum of rated kW across every eligible bank) is called from `gate.py`'s admission path.
  Diagram 03 updated.
- **Re-nomination points now run after the gate; `RESELECTED` only for a `DELIVERING` obligation**
  (`14c4a01`). New `engine.exercise_due_renomination_points()` is invoked as a callback from
  `run_due_gates()` after a `RENOMINATION`-scoped gate; `contracts.renomination.exercise_renomination_point()`
  now explicitly checks the obligation is `DELIVERING` before driving the `R-RENOM-GATE` self-loop (a
  `COMMITTED` obligation taking `COMMITTED → DELIVERING` early was a live bug, 2026-09-26) — otherwise
  falls back to `CONFIRMED`. Diagram 03's `R-RENOM-GATE` self-loop flips to LIVE.
- **Mid-window `SHORTFALL` escalation coded and wired** (`289d68f`/`14c4a01`). New
  `engine.escalation.ShortfallEscalator`: a signal (allocator L2-instruction shortfall → `R-COMMIT-LOCK-
  OVERRIDE-L2`; no-substitute/bank-capacity shortfall or post-substitution energy infeasibility →
  `R-COMMIT-LOCK-INFEASIBLE`) must persist `sustain_cycles` (default 30 = 60 s at 2 s) before
  `escalate_sustained_shortfalls()` drives `DELIVERING → SHORTFALL` mid-window. Only `L0`/`L1` (device-
  safety/homeowner-reserve) remain undriven. **Per the lead, kept "still open": code-complete, not yet
  observed firing on a real obligation.** Diagram 03 updated.
- **LP plans solve again** (`927cc77`). The bank charge envelope is now passed into `build_mode_o_model`
  (previously omitted, so the model could never recharge a bank and every plan fell back to F2, live
  2026-09-26); `fleet.capability()` is read once per bank rather than once per bank/interval (was ~96×
  redundant reads per gate); soft C15 (a penalized terminal-energy target, not a hard floor) avoids the
  infeasibility a hard floor caused. Applies to both `L-DA` and `L-ID` plan modes. Diagram 02 updated.
- **Engine cycle-latency metrics** (`927cc77`). New `engine.latency.CycleLatencyWindow` records each 2 s
  tick's wall time and publishes p50/p99/max (over a ~5 min/150-tick window) as a `CYCLE_LATENCY` trace
  event. Target is A11 "p99 < 500 ms at 2k hubs" (from the module's own docstring). **Kept "still open" per
  the lead: the metric exists and is traced, but nothing in the code/config proves the target is actually
  met at that scale.** Diagram 02 updated.
- **Selector gates run off the 2 s tick** (`f3b3365`). New `engine.start_gates_in_background()` runs the
  whole due-gate batch as one background `asyncio` task, never awaited by the tick — a 24 h gate
  (capability load, intake, LP) took 10–20 s and held dispatch for committed obligations that long (A11
  p99, live 2026-09-26); triggers arriving mid-run queue in a de-duplicated backlog. Diagram 02 updated.
- **Health/feeds/SCADA hardening** (`558d80f`/`c00a236`/`4f2588d`): per-product feed-staleness thresholds
  (parametrized `staleness_threshold_s`), `og-feeds` folded onto one `run_forever` loop (same SIGTERM
  pattern as `og-settle`'s earlier fix), an NWS updated-field fallback, batched hub-health writes, and
  `ogsim.scada`'s own per-bank background load generator (`ogsim/scada/background.py`).
- **`deploy/` unchanged.** `git diff fcaa2ca f3b3365 -- deploy/` is empty — every capability above folds
  into the existing `og-engine`/`og-guardian`/`og-settle` units; no new process, port, or env var. Diagram
  04 updated (snapshot reference only).

## Resolved since `7669a7c` (verified in code, `fcaa2ca`)

- **Commitment path is now driven.** `selector/commit.py`'s new `commit_candidate()` drives
  `OFFERED → SELECTED → COMMITTED` (`R-GATE-SELECT`, `R-COMMIT-LOCK-ENTER`) or `→ REJECTED`
  (`R-COMMIT-LOCK-INFEASIBLE`); `ledger.reserve()` now writes `og.commitment` atomically with
  `og.reservation` (`build_commitments()`). `engine/lifecycle.py`'s new `advance_obligations()` drives
  `COMMITTED → DELIVERING` (`window_start <= now`) and, at window end, `DELIVERING → FULFILLED`
  (`R-FULFILLED`) or `→ SHORTFALL` (`R-SHORTFALL-THRESHOLD`) from settle's per-interval performance.
  Diagram 03 redrawn: 7 of 12 reason-code edges are now LIVE (was 1 — `OFFERED → EXPIRED` only).
- **A failing selector gate no longer costs the dispatch cycle.** `engine/gates.py`'s new
  `run_due_gates()` catches each trigger's own exception, traces + raises `ALR-SELECTOR-GATE-FAILED`,
  and the tick continues for every other trigger (K7). Diagram 02 stage 1 updated.
- **Hub-level substitution is traced.** `allocator.run_cycle()` now reads `CycleResult.substitutions` and
  calls `ledger.record_substitution_events()` — one `SUBSTITUTION` trace event per event, and a recording
  failure never costs the cycle. Diagrams 02/03 updated.
- **Three guardian scoping/read bugs fixed**, each previously vetoing or crashing on a live fleet
  (`docs/team/NOTICES.md`, 2026-09-25/26): G-19's commitment-lock query now scopes to reservations
  covering *now* on that bank (was unscoped — every future commitment counted, so G-19 vetoed every
  batch); G-03's bank-kVA check now scopes to the load a batch *adds* (was net-of-reserve — vetoed relief
  on an already-overloaded bank instead of allowing it); the guardian's ledger-version read is now the
  durable `og.commitment` version via `PgLedgerVersionPort`/`current_version()` (was the engine process's
  unconfigured in-memory facade — raised `RuntimeError` on every guardian evaluation). Diagram 02's
  guardian-check grid updated (G-19, G-03, G-09).
- **G-01 now vetoes discharge on a stale/faulted SoC**, not only a low one: `hub.health != "online"` with
  a negative setpoint is refused (`HUB_SOC_NOT_LIVE`). Diagram 02 updated.
- **Guardian signs the actual command-batch envelope**, not the verdict. New `sign_command_batch()` /
  `build_signed_batch()` sign the `CommandBatch`'s own `{batch_id, bank_id, epoch, seq, issued_at,
  expires_at, items}` — a signature separate from the verdict's own audit-trail signature in
  `og.verdict`; publishing `verdict.signature` instead would fail every hub's `BAD_SIGNATURE` check.
  Diagram 02 stage 5 updated.
- **The engine ramps hub setpoints** before handing them to the guardian: `RAMP_SAFETY_FACTOR = 0.9` via
  `apply_ramp_limit`, using the same `core.physics.hub_ramp_kw_per_s()` guardian's G-04 checks. Diagram 02
  stage 4 / guardian grid (G-04) updated.
- **K6 epoch is now durable and per-engine-start**: `og-engine`'s `main()` reads `next_epoch()` (strictly
  greater than any epoch the guardian has accepted) instead of a hardcoded `epoch = 1`. Diagram 01
  updated.
- **SCADA persistence moved off the MQTT ingest path.** `fleet.ingest_scada_signal()` is now memory-only;
  persistence happens in `flush()` (`backend.record_scada_observations()`), alongside telemetry — a
  per-message commit on the ingest path fell behind under load (live 2026-09-25). Diagram 01 updated.
- **`og-settle` is one `run_forever` loop, not three.** A second concurrent `run_forever` had been
  replacing the first one's SIGTERM handler, so systemd had to `SIGKILL` `og-settle` after 90 s on every
  deploy; each due job (heartbeat, health, settle, trace-prune) now runs as its own task under a single
  loop. `og-api` now writes its own process heartbeat (`heartbeat_loop()`), fixing intermittent false
  "api down" reports. Diagrams 01 and 04 updated.
- **`deploy/README.md`'s stale status note is gone.** It now reads "all ten units are enabled and running
  from `/opt/opengrid/current`..." — the diagram 04 dashed "not enabled/started" note is removed. No
  other `deploy/*` file changed between `7669a7c` and `fcaa2ca`.
- **`interfaces/mqtt/topics.md`'s `og_api` claim is fixed** — it now correctly says `og_api` does not
  subscribe to any MQTT topic (its SSE streams poll Postgres directly). Diagram 01's note updated; this
  item is dropped from "Still open" below.
- **Property-based tests for every invariant landed** (PR #1): `orchestrator/tests/property/` now has one
  file per invariant K1–K13 plus `test_energy_sufficiency.py` (17 files total).

## Still open (re-verified at `f3b3365`)

- **The engine cycle-latency p99 target is not proven met.** `engine.latency.CycleLatencyWindow` computes
  and traces p50/p99/max every ~5 min; the target (A11: "p99 < 500 ms at 2k hubs") is documented in the
  module's own docstring, but nothing in the code or config asserts or demonstrates it is currently met at
  that scale — the metric exists, the target does not yet have live/test proof. Diagram 02.
- **The mid-window `SHORTFALL` escalation is not yet proven live.** `engine.escalation.ShortfallEscalator`
  is fully coded and wired (60 s sustain, `L2`/`INFEASIBLE` reason codes) — but per the lead this has not
  yet been observed firing on a real obligation in production. Diagram 03 (edge drawn LIVE, with this
  caveat called out explicitly).
- **`R-COMMIT-LOCK-OVERRIDE-L0`/`-L1` (mid-window device-safety / homeowner-reserve overrides) have 0
  production callers.** Only the `L2`/`INFEASIBLE` sustained-signal paths and the window-end
  `R-SHORTFALL-THRESHOLD` path reach `DELIVERING → SHORTFALL`. Diagram 03.
- **Bank-level substitution (`allocator.substitute_hub()`) is wired but still never triggered** — unchanged
  this round: `engine.main()` calls `allocator.configure(ledger)` at startup so it no longer raises
  unconditionally, but grep still finds 0 production call sites for `substitute_hub()` itself. Diagram 03.
- **The asset-health/calibration ladder is fully coded but has 0 production callers.** `opengrid.assets`
  (new, `9153dc2`) has a complete `state_machine.py` (`OK→WATCH→DEGRADED→QUARANTINED→AWAITING_REPLACEMENT
  →RECOMMISSIONING→OK`, fail-closed), a complete `AssetHealthService` (drift evaluation, calibration
  request/result, quarantine, work orders, replacement, recommissioning — every transition traced), and
  real Postgres repos — but grep finds 0 callers of `AssetHealthService` anywhere in `engine`/`guardian`/
  `settle`/`api`. Guardian's own half (`G-25` calibration-command signing) IS wired (see "Resolved"
  above); the gap is the missing scheduler/caller that would connect drift detection to it, not the
  guardian. Consequently `hub_inverter_pq.asset_state`, `calibration_attempt`, `maintenance_work_order` and
  `asset_event` all still never get written on this checkout. Diagrams 05, 06.
- **The calibration-command MQTT wire path (`cmd/cal`/`ack/cal`) has 0 wiring, either side** — unchanged:
  neither `opengrid` nor `ogsim` publishes/subscribes it. Diagrams 01, 06.
- **Allocator PQ-aware selection/continuous monitoring is entirely unbuilt** — unchanged: grep for
  `pq`/`PQ` across `opengrid.allocator` is still 0 files. This was never part of the lead's wave-2 report
  and is called out separately so it isn't mistaken for part of it. Diagram 06.

## Index

| File | Shows | Main sources |
|---|---|---|
| [`01-system-architecture.svg`](01-system-architecture.svg) | The 6 orchestrator processes + 4 simulators + Postgres + Mosquitto + Apache; which process publishes/subscribes which MQTT topic family and owns which table groups; external live ERCOT/EIA/NWS vs `og-sim-market`, and the exact config key that switches between them. | `BUILD.md`, `orchestrator/config/orchestrator.toml`, `interfaces/mqtt/topics.md`, every process's `main.py`/`__init__.py`, `deploy/apache/opengrid.conf`, `dev/docker-compose.yml` |
| [`02-dispatch-cycle.svg`](02-dispatch-cycle.svg) | The full dispatch stack as implemented: selector gate (HiGHS MILP/LP + F2 rule fallback) → real-time allocator 2 s cycle (tiers, PI loop, water-filling/substitution, price response) → ledger → engine→guardian handoff → guardian verdict (every G-check it actually runs, listed) → signed MQTT command batch → hub → ack/telemetry → settle. | `orchestrator/src/opengrid/{selector,allocator,ledger,engine,guardian,settle}/*.py`, `interfaces/mqtt/*.schema.json`, `interfaces/crypto.md`, `00-invariants.md` |
| [`03-commitment-lifecycle.svg`](03-commitment-lifecycle.svg) | The obligation state machine exactly as coded in `state_machine.py`'s `_TRANSITIONS` table, the K13 commitment lock, substitution (hub-level vs. bank-level), and the `at_risk` flag — **each edge marked LIVE or NOT DRIVEN based on a full-repo grep for its reason code / trigger function.** | `orchestrator/src/opengrid/contracts/*.py`, `orchestrator/src/opengrid/ledger/__init__.py`, `orchestrator/src/opengrid/allocator/__init__.py`, `orchestrator/src/opengrid/engine/gateways.py` |
| [`04-deployment.svg`](04-deployment.svg) | The base-server layout: every `systemd` unit with its venv/working directory/memory budget/ports, the Apache reverse-proxy rules, Postgres/Mosquitto, the deploy/rollback/backup flow, env-variable *names*; plus the local `docker compose` dev stack. | `deploy/README.md`, `deploy/systemd/*`, `deploy/apache/opengrid.conf`, `deploy/{cron,logrotate}/opengrid`, `dev/README.md`, `dev/docker-compose.yml`, `dev/.env.example` |
| [`05-data-model.html`](05-data-model.html) | Every one of the 40 tables created or altered by migrations `0001`…`0013`, grouped into the 10 areas the work order asked for, with primary keys, foreign keys, one-line purpose, and which migration touched each (`0012` adds `og.command_ack`; `0013` only widens a CHECK constraint for `DATA_CENTER`, no new table). | `orchestrator/migrations/0001_init.sql` … `0013_service_type_data_center.sql` (read in full) |
| [`06-power-quality-flow.svg`](06-power-quality-flow.svg) | The PQ pipeline the spec describes — waveform → transport → ingestion/storage → envelope checks (K14) → corrective ladder → asset health → work order → physical swap — **with every stage marked LIVE, BUILT-BUT-0-CALLERS, or PLANNED**, cross-checked against `docs/team/NOTICES.md`'s own wave 1/2/3 status. | `orchestrator/src/opengrid/pq_ingest/*.py`, `guardian/{pq_checks,pq_repo}.py`, `assets/*.py`, `core/pq/*.py`, `core/models/pq.py`, `integration-sims/src/ogsim/fleet/{pq,calibration,wave,runtime}.py`, `orchestrator/migrations/0010_service_profile.sql`, `0011_asset_health.sql`, `interfaces/mqtt/{pq_waveform_*,calibration_*,waveform_capture_request}.schema.json` |

## How these were verified

1. **Read first, drew second.** Every process's `main.py`/`__init__.py`, the modules it imports for its
   core logic, `orchestrator/migrations/*.sql` in full, every `interfaces/mqtt/*.schema.json` and
   `interfaces/contracts/*.schema.json`, `deploy/**`, `dev/**`, and `integration-sims/src/ogsim/**` were
   read directly from this checkout before any box was drawn — not summarized from the specs in
   `docs/orchestrator/07-delivery/`, which were read for names/intent only, per the work order.
2. **Every label is grep-checked.** Every process name, MQTT topic segment, table/column name, port
   number, reason code, and guardian check id drawn was confirmed present verbatim in the code or
   config with a targeted `grep`/`Select-String` — not typed from memory. Where a spec-described thing
   (a G-check, a state transition, a topic) had **zero** matching call sites outside tests, it is drawn
   and explicitly marked as such (diagrams 02, 03, 06) rather than omitted or silently assumed done.
3. **Cross-checked against the team's own status reports.** `docs/team/NOTICES.md` and `WORKBOARD.md`
   (live A1–A11 results, the wave 1/2/3 PQ build status) were used to corroborate — several findings in
   the original (`7669a7c`) pass independently reproduced symptoms the team had already logged (e.g. the
   then-open "commitments FAIL (0)", since fixed — see "Resolved since `7669a7c`" above), and this round's
   docstrings/comments cite the same live incidents (e.g. "~34% of og-engine CPU," "A11 p99, live
   2026-09-26") rather than being taken at face value from the commit messages.
4. **Generated, not freehand.** Each SVG was built by a small Python script (this session's scratch
   working area, not part of the repo) over a shared layout helper, so every box's height is computed
   from its actual text rather than eyeballed, and an automated pass flags any line that would render
   under 11 px or wider than its box *before* the file is written. Every SVG was then re-parsed with
   `xml.dom.minidom` to confirm well-formedness.
5. **Visually proof-read.** Each diagram was rendered in a browser (headless Edge, no network access) and
   inspected at full scale to catch label/box overlaps and non-orthogonal arrows the automated text-fit
   check can't see — this caught and fixed a legend overflowing the canvas (01), several edge labels
   sitting on top of their target box's own text (03), and a couple of diagonal connectors (03) in the
   original pass, and a fresh round of edge-label collisions in diagram 03 (introduced by this round's
   longer "what changed" notes, then shortened until they cleared) before this hand-off.
6. **No external references.** `grep -rn "http://|https://"` across `docs/diagrams/` matches only the
   mandatory `xmlns="http://www.w3.org/2000/svg"` namespace declaration and plain-text descriptions of
   loopback URLs (`127.0.0.1:8080` etc., quoting `deploy/apache/opengrid.conf`) — never an `href`/`src`.
7. **Re-verified claims, not commit messages, every round.** Each round's reported fixes were traced to
   their own current-code evidence (function names, docstrings, live-incident comments, and — for "not
   driven" claims — an explicit `grep` for every production call site of the relevant function/reason
   code) before being drawn. This round (`fcaa2ca → f3b3365`): every claim in the lead's list was
   confirmed true in code, down to the exact caller chain (e.g. `_engine_tick` → `start_gates_in_
   background` → `run_due_gates` → the `on_renomination` callback → `exercise_due_renomination_points` →
   `contracts.exercise_renomination_point` for the `R-RENOM-GATE` self-loop) — none failed verification,
   and the two items the lead asked to keep "still open" (cycle p99, mid-window `SHORTFALL` liveness) are
   exactly the two called out that way below, no more and no fewer.

## Planned vs. built (consolidated across all six diagrams)

**Built and live** (normal operation drives it every cycle, confirmed by tracing the caller chain):

- All 6 orchestrator processes (`og-feeds/engine/guardian/safestop/settle/api`) and all 4 simulators
  (`og-sim-fleet/scada/market/control`); the config-only live-vs-simulator switch for ERCOT/EIA/NWS.
- Selector — LP plans solve again (charge envelope + soft C15 fix, one capability read/bank; `L-DA`/`L-ID`
  both), with F2 rule fallback — → real-time allocator (tiers, `DIST_DEFERRAL` PI, water-filling, price
  response, hub ramp limiting) → ledger `reserve()` (K2, `og.commitment`) → the **full obligation
  lifecycle**: `OFFERED → SELECTED → COMMITTED → DELIVERING → FULFILLED`/`SHORTFALL` (window-end
  threshold, or mid-window after 60 s sustained `L2`/`INFEASIBLE`) `→ SETTLED`, plus `→ EXPIRED`,
  `→ REJECTED` (both admission-time capacity and selection-time infeasible), and the `DELIVERING →
  DELIVERING` re-nomination self-loop — → engine→guardian handoff (gates run off-tick in a background
  task; isolated per-trigger failure) → guardian signing (every check G-01, G-01-ENERGY, G-02 – G-06,
  G-09, G-13 – G-15, G-19, G-20, **G-21 – G-25 (PQ, NEW)**) → a command-batch envelope signature separate
  from the verdict's → signed command batch → hub → ack (now persisted as `og.command_ack`)/telemetry.
- The K13 commitment lock's *exception* machinery (`check_commitment_lock`, guardian G-19, the allowed
  release-reason set); hub-level substitution (unhealthy hub excluded, healthy one water-filled in) inside
  the automatic 2 s cycle, traced; the `REPLACE_INVERTER` scenario-control action end to end.
- **PQ wave 2 ingestion + guardian checks (NEW, `9153dc2`):** `ogsim.fleet.wave.py` **synthesizes real
  waveform samples** (`synthesize_raw_capture()`, S7.4's sinusoid-plus-harmonics model) and periodic
  summaries, published every tick plus a 1 %/min rotating audit and on-demand request replies (corrected
  in this document — see "Resolved" above); `og-engine` subscribes and batch-flushes `scada/wave/.../
  summary`, and validates+stores `.../raw` in a background worker; ogsim's own summary gate defaults to
  10 s. Guardian's G-21–G-25 read real Postgres state (envelope limits, measured/modelled measurement,
  asset-state, calibration history) and feed the same violations list every other check does.
- `obligation.at_risk` now set **and cleared** every cycle (not just alerted); `og-engine`'s durable
  per-start epoch (K6), buffered/async SCADA + telemetry persistence, per-hub ramp-limited setpoints, and
  cycle-latency (p50/p99) tracing; `og-settle`'s single `run_forever` loop; `og-api`'s process heartbeat;
  per-product feed-staleness thresholds; `og-feeds`' single `run_forever` loop.
- All 40 data-model tables (schema; `DATA_CENTER` service type added, additive); the full
  deploy/systemd/Apache/Postgres/Mosquitto topology (unchanged since `fcaa2ca`) and the local dev
  `docker compose` stack.

**Built but 0 production callers** (the logic/schema exists and is unit-tested; nothing in a live process
calls it):

- Bank-level substitution (`allocator.substitute_hub()`) — wired (`engine.main()` calls
  `allocator.configure(ledger)` at startup) but still 0 production call sites (diagram 03).
- **The asset-health/calibration ladder** (`opengrid.assets`, NEW `9153dc2`) — a complete, tested state
  machine + `AssetHealthService` (drift evaluation, calibration request/result, quarantine, work orders,
  replacement, recommissioning, every transition traced) with real Postgres repos, but 0 callers anywhere
  in `engine`/`guardian`/`settle`/`api`. Guardian's own `G-25` signing half IS wired; the gap is the
  missing scheduler that would connect drift detection to a build-and-sign call. `hub_inverter_pq.
  asset_state`, `calibration_attempt`, `maintenance_work_order`, `asset_event` all still never get written
  (diagrams 05, 06).
- `R-COMMIT-LOCK-OVERRIDE-L0`/`-L1` (mid-window device-safety/homeowner-reserve overrides) — legal in
  `state_machine.py`'s transition table, 0 production callers; only the `L2`/`INFEASIBLE` sustained-signal
  paths and the window-end threshold path are driven (diagram 03).
- The calibration-command MQTT wire path (`cmd/cal`/`ack/cal`) — 0 wiring, either side, on either
  deployable (diagrams 01, 06). Allocator PQ-aware selection/monitoring — 0 files reference `pq`/`PQ` in
  `opengrid.allocator` at all; never part of any wave-2 report (diagram 06).

**Planned, no code at all:** the corrective ladder's rebalance / reactive-PF / exclusion steps; a
stand-alone "wave-ingestion service" process (ingestion is real, but folded into `og-engine`, not a
separate process). (Waveform sample generation, "WP-H," was wrongly listed here in an earlier pass — it
is in fact LIVE; see "Built and live" above and the correction note at the top.)

**Not a gap (by design):** the spec's "SCED-interval LP" is explicitly folded into the allocator's
per-cycle price response (`02a-mvp-s-spec-engine.md` §3.1) rather than run as its own 5-minute solve —
drawn in diagram 02 as a dashed cross-reference, not as "planned."

## Uncertain

Per the work order, listed here rather than guessed at:

- **`og_sim` grant `topic read og/v1/scada/ctl/#`** in `dev/mosquitto/opengrid.acl` (generated by
  `dev/scripts/gen_mosquitto_acl.py`). This topic pattern does not appear in `interfaces/mqtt/topics.md`
  and no publisher or subscriber for `scada/ctl/#` was found anywhere in `orchestrator/src` or
  `integration-sims/src`. Likely reserved/vestigial; not drawn as a live topic in diagram 01.
- **Prometheus `/metrics` endpoints for processes other than `og-guardian`.** `platform/metrics.py`
  defines metric objects that read as if `og-engine`/`og-feeds` should also export them, and
  `orchestrator.toml` has a shared `[metrics].bind_host`, but `start_http_server` is only ever called
  from `guardian/main.py` (port 9103). Diagram 04 draws only the guardian's port as confirmed.
- **Exact per-environment hub/bank counts in `dev/config/fleet.dev.yaml` and `scada.dev.yaml`** — diagram
  04's "200 hubs / 8 banks (dev default)" is sourced from `dev/config/dev.toml`'s `[fleet]` section and
  `dev/docker-compose.yml`'s comments (which say these YAML files "must both change together" with it);
  the YAML files themselves were not opened.

## Terminology (used consistently across all six diagrams)

- **Substitution** — a software move of delivery between hubs (live, traced), or, for the wired-but-never-
  triggered ledger primitive, between banks. Never changes an obligation's committed total.
- **Inverter swap / replacement** — a physical hardware action (`ogsim.fleet.pq.replace_inverter`,
  `og.asset_event.event_type = INVERTER_REPLACED`). Remote **recalibration** is always tried first.
