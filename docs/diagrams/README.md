# OpenGrid Orchestrator — Architecture Diagrams (DOC3)

Self-contained SVG/HTML written for this repository, not produced by a diagramming tool: plain SVG
elements laid out by a small throwaway script (see "How these were verified", point 4), with no external
resources (no CDN, web fonts, remote images or externally loaded scripts; inline CSS only; system font
stack). Verified against `main @ 434d230` (this working copy, branch `wp/doc3-diagrams`, rebased onto
`434d230` — the fifth snapshot in this diagram set's history: `7669a7c` → `fcaa2ca` → `f3b3365` →
`daef460` → `434d230`).

**These diagrams are a snapshot of `main @ 434d230` (2026-09-26).** This round's headline: the obligation
lifecycle (diagram 03) is now **fully driven, zero remaining gaps** — `R-COMMIT-LOCK-OVERRIDE-L0`/`-L1`
(the last undriven reason codes) are now computed every cycle by `allocator.cycle.classify_hub_loss()`.
The calibration loop (`cmd/cal`/`ack/cal`) and allocator PQ eligibility are now LIVE; the asset-health
drift sweep has a real caller but is switched off by config; a two-person guardian-signed safe-stop
RELEASE, an ERCOT_AS capacity-hold/deployment model, and K7 scope-posture escalation all shipped. Every
claim was re-verified in this checkout's code, not the commit messages; see "Resolved since `f3b3365`"
and "Still open" below. (Earlier rounds' deltas are kept further down as history; the `7669a7c` pass's
WP-H mistake — this diagram set once wrongly marked waveform sample generation absent — was corrected in
the `f3b3365` round and is not repeated here.)

## R2 update (`main @ 6470cfa`)

Four diagrams were updated for release R2; everything else in this set is still the `434d230` snapshot.

- **05 data model:** migrations `0024`–`0032` were read in full. It now shows 60 tables in 15 groups, 13 of them new:
  - `utility` and `asset` (two markets, 0025);
  - four customer-services tables (0026);
  - `trace_anchor` (0028);
  - `service_transformer`, `feeder_limit` and `substation_limit` (0029);
  - `plan_energy_value`, `plan_value` and `plan_shadow_obligation` (0030).

  It also shows the new columns on `contract` (0025), `hub_state`/`telemetry` (0027), `hub` (0029, 0032) and
  `alert` (0031), and the `invoice_line` insert-only trigger (0024). Each new table names its writer, and says
  when that writer ships switched off.
- **02 dispatch cycle:** the guardian check grid adds G-26…G-33 (`guardian/flow_checks.py`; G-32 in
  `core/limits.py`) and the changed G-02/G-05/G-19. The other stages are still `434d230`.
- **01 system architecture:** the guardian box lists G-26…G-33, and the Postgres box names the R2 tables.
- **06 power quality:** stages 3–4 are updated. Continuous monitoring (`allocator/pq_monitor.py`) and the S5.4
  corrective ladder (`engine/pq_ladder.py`) are wired at R2 (`engine/wiring.py:84-106`), but they are built only
  when `[site_ingest].enabled` or `[allocator.closed_loop].enabled` is true, and both ship false. Statements
  below that `pq_monitor` "has 0 callers" are true at `434d230` only.

The R2 build status per invariant and story is in `docs/orchestrator/07-delivery/00-invariants.md` and
`10-traceability-matrix.md`.

## Resolved since `f3b3365` (verified at `434d230`)

- **The obligation lifecycle is now fully driven — 0 remaining gaps.** `allocator/cycle.py:260`'s
  `classify_hub_loss()` (NEW, `4913a95`) runs every cycle and attributes each bank's lost capacity to
  `L0` (FAULT hubs, device safety), `L1` (healthy hubs held below `rated_kw` by the reserve floor), or
  `INFEASIBLE` (stale/offline) — the largest wins, and an active `L2` allocator instruction still binds
  first (`engine/escalation.py`'s `_PRECEDENCE`, `L0>L1>L2>INFEASIBLE`, `947c9f5`). This drives the
  previously-undriven `R-COMMIT-LOCK-OVERRIDE-L0`/`-L1` reason codes. Diagram 03 now shows 12 of 12
  state-machine edges LIVE; diagram 02's S2 side panel and K13 addendum updated. **Narrower caveat kept
  below:** the code path is proven live every cycle; an `L0`/`L1` mid-window transition firing on a real
  obligation has not separately been observed.
- **ERCOT_AS is now a capacity hold, not a grant** (`d43da06`). `allocator/cycle.py:147-162`'s `is_as_hold`
  path grants **0 kW** (`R-GRANT-AS-HOLD`) for an AS-committed bank until an operator deploys it; new
  `POST`/`GET`/`DELETE /og/api/dispatch/as-deployments` (`api/routers/dispatch.py:74`) opens a
  time-bounded deployment window that then discharges the bank up to its committed kW for that one
  window. Diagrams 02 and 03 updated.
- **K7 posture escalation and safe-stop request are live** (`guardian/escalation.py`). A new
  `EscalationTracker` watches each tick's veto ratio per bank/zone; over 5% vetoed in a tick flips that
  scope to `CONSERVATIVE` (written to the new `og.scope_posture` table, migration `0019`, alert
  `ALR-SCOPE-CONSERVATIVE`) — `engine/gateways.py` reads the posture table and feeds `CONSERVATIVE`
  scopes to the allocator as `LIMIT`/`BLOCK` instructions (K5), the same shape as a real L2 order. Three
  consecutive `CONSERVATIVE` ticks raise `ALR-SAFE-STOP-REQUESTED` — this only *proposes* a stop; nothing
  in the code engages one without an operator's own confirmation (K8). Diagrams 01 and 02 updated.
- **Degraded modes are health/UI display, not an engine gate.** `NO_NEW_COMMITMENTS`,
  `HOLD_LOCAL_AUTONOMY`, `HOLD` and `DIST_DEFERRAL_OPEN_LOOP` (new `og.degraded_mode_state` table,
  migration `0021`) are computed and shown by `health`/`ui`/`api`; grep finds 0 references to any of the
  four in `contracts` — nothing in the admission/dispatch path actually checks or enforces them. Drawn as
  monitoring-only in diagrams 01 and 02, not as an admission gate.
- **Two-person safe-stop RELEASE is built and signed — overturning this document's prior "not built"
  claim** (`f27e4e3`). `guardian/stop_release.py`: operator A requests, operator B approves (`api/routers/
  safestop.py` returns 403 if B==A); the release event is traced as a Tier-2 `og.operator_action`
  (`SAFE_STOP_RELEASE`), then guardian-signed — every `StopEvent` this module returns is signed before
  return (`stop_release.py:107-120`; the `unsigned` object always gets `.model_copy(update=
  {"signature": ...})`, closing what had been an unsigned-bypass risk). Signing only fires for operators
  named in the `[guardian].stop_release_authorised_operators` allow-list, empty by default. Test operators
  `og-op-a`/`og-op-b` are env-overridable, not production ids. Diagrams 01, 02 and 03 updated; diagram 04
  and this README's earlier "not built" wording removed.
- **Per-bank load-zone pricing (D-10) exists but ships disabled.** `fleet/seed.py`'s `ZoneBlockConfig`
  (Austin Energy `LZ_AEN` / CPS Energy `LZ_CPS` as optional extra load zones) defaults `enabled=False`
  (`f5058af`); the base fleet topology and every table are unaffected either way. Diagrams 02 and 05
  reference this as disabled-by-default, not built-and-off.
- **Allocator PQ-aware selection is split: eligibility filtering is LIVE, continuous monitoring is not**
  (`0bfac90`). `engine.pq_eligibility` (wired into `engine/__init__.py`'s periodic tasks) refreshes each
  PQ-sensitive profile's eligible-hub set from `og.hub_inverter_pq` + live health on a
  `PQ_ELIGIBILITY_REFRESH_S` timer; `selector/gate.py:526`'s `exceeds_pq_eligible_capacity()` refuses to
  commit an obligation beyond that eligible capacity — a real, live admission-time control. Separately,
  `allocator/pq_monitor.py` (continuous post-commit monitoring) still has 0 callers anywhere. Diagram 06
  redrawn to show this split rather than one uniform "unwired" status.
- **The calibration command loop is now fully wired end to end** (migration `0016`). `guardian/
  mqtt_io.py:158` publishes `cmd/cal/<hub_id>`; `engine/__init__.py:1008` routes the matching
  `ack/cal/<hub_id>` to `assets.calibration_ack.handle_calibration_ack()`; new `og.calibration_command`
  table (migration `0016`) persists the round trip. This overturns the prior round's "0 wiring either
  side" claim. Diagrams 01 and 06 updated.
- **The asset-health drift sweep now has a real caller — but ships switched off.** `og-settle`'s
  `JobRunner` (`settle/main.py:125`) does call `make_asset_drift_job()` → `assets.runner.run_once()` →
  `AssetHealthService`, a real production call path where none existed before. It is gated by
  `[assets].drift_enabled`, which is `false` in this checkout (`orchestrator/config/orchestrator.toml:
  147`) — briefly `true` in an intermediate commit (`daef460`), then reverted (`b5f17a9`, "fleet-wide
  false positives"). Correct status is **"wired, disabled by config,"** not "0 production callers" —
  a materially different claim from the prior round's, called out separately in "Still open" below.
  Diagram 06 updated.
- **Solver runs in its own long-lived process** (`6503187`). `highs_solve` now runs in a
  `ProcessPoolExecutor` (1 worker, warm-start hints passed in) instead of on `og-engine`'s own event
  loop — model build/validate/price-of-firmness held the GIL for seconds per gate before, queuing every
  2 s tick's `await` behind it (A11). If the solver process dies, the gate falls back to solving in a
  thread and the pool is replaced (K7). Diagram 02's S1 updated.
- **Banks propose to the guardian concurrently, bounded** (`9902d61`). Up to 4 banks propose at once
  instead of serially — a full ERCOT_AS event at 07:00 spread over 24 banks meant up to 24×3 synchronous
  K10 pre-image/batch-row/`NOTIFY` commits serialized on one tick; one bank's failed proposal is logged
  and never blocks the rest (K7). Diagram 02's S4 updated.
- **Heartbeat and fleet/PQ persistence moved off the 2 s tick** (`0e9969f`/`c5d8374`). Both now run as
  their own periodic tasks — a host disk stall had made these commits take up to 10 s while heartbeat was
  the tick's first `await` (observed 8–17 s ticks). Heartbeat is only written while the tick keeps
  completing within 3 cycles, so a hung tick still reads as engine-down to health. New `CYCLE_LATENCY`
  trace event (`86488e8`) times each tick broken out by phase (heartbeat, fleet_flush, pq_flush,
  gate_schedule, lifecycle, allocator, energy_check, escalation, guardian_check, propose) plus event-loop
  lag, over a 300-tick window. Diagram 02's S4 updated.
- **Safety hardening** (`f726fd1`): K12's clock now reads a kernel `adjtimex` port and fails closed on
  error rather than assuming synced; G-19 re-verifies an override claim against the guardian's *own* read
  (an `L2` claim is checked against the guardian's own instruction view, `L0`/`L1`/`INFEASIBLE` against
  its own capability read) instead of trusting the claimed reason alone — stale telemetry now vetoes the
  claim instead of passing it through; G-03 bounds bank kVA loading in both directions; G-06's feeder ids
  are read from `fleet.seed.feeder_id_for()` rather than guessed; calibration-command replay
  (`(epoch, seq)` reuse) is rejected; K8 no longer accepts an empty payload as a valid stop event. Diagram
  02's guardian grid and side panel updated.
- **MQTT identity hardening.** `platform/mqtt.py:109`'s `compose_client_id()` composes every client id as
  `<[mqtt].client_id_prefix>[-<OG_WS>]-<process>` and refuses (`MqttIdentityError`) a production-looking
  id when a workspace is set or `general.env != "prod"`; a workspace without its own MQTT credentials is
  refused rather than falling back to production users (`mqtt.py:139-149`). On the simulator side,
  `ogsim/common/config.py:132`'s `resolve_topic_root()` gives `OG_MQTT_ROOT` precedence over the YAML
  `topic_root` (mirroring the orchestrator's own config precedence), and `fleet.yaml:59` documents the
  same precedence for the simulator's signing-key override. `orchestrator/config/test.toml:116` now sets
  its own `blob_store_dir = "var/pq_waveform"` so test runs no longer share the production blob
  directory. Diagram 01's MQTT panel and sources updated.
- **Measured K1/K2 proof counters are real, not just traced.** New `og.invariant_check` /
  `invariant_violation` / `invariant_trace_watermark` tables (migration `0014`, dedupe + resume-cursor
  follow-up in `0017`) are written solely by `opengrid.invariants`, one row per named measured check
  (last run, duration, violations this run, running total); `api/routers/{health,dispatch}.py` read them
  for the measured counters the UI/API surface. Diagram 05's new invariants group; diagram 02's K-check
  references updated.
- **Base server reconfirmed the permanent host (D-16); deploy hardened further.** No decommission
  wording exists anywhere in the source read for this diagram set. Postgres's data directory moved to
  `/srv/pgdata/17/main`; backups moved to `/srv/ogbackup` (override `OG_BACKUP_DIR`); new
  `deploy/RUNBOOK.md` and `deploy/mosquitto/provision_ws_users.{py,sh}`; `og-api` gained `api_proxy.env`;
  `og-sim-fleet`/`og-sim-control` gained `OGSIM_ENV=prod`. Diagram 04 updated throughout.
- **Migration renumbering note.** `0015` is genuinely absent on this branch (allocated elsewhere to an
  in-flight `customer_services` migration by another work package that never landed here) — migrations on
  disk run `0001`–`0014`, `0016`–`0023` (22 files, confirmed by directory listing). 7 new tables since
  `f3b3365`: `invariant_check`, `invariant_violation`, `invariant_trace_watermark`, `scope_posture`,
  `as_deployment`, `degraded_mode_state`, `calibration_command`; plus a new `og.pnl.delivery_charge`
  column (`0023`). Table count 40 → 47. Diagram 05 and the Index below updated.

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
- **Correction: waveform sample generation ("WP-H") is on main since `9153dc2`** — an earlier pass of
  this diagram set wrongly marked it absent; `ogsim.fleet.wave.synthesize_raw_capture()` builds real
  sample arrays and has stayed LIVE through every round since, including this one.
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

## Still open (re-verified at `434d230`)

- **The engine cycle-latency p99 target is not proven met.** `engine.latency.CycleLatencyWindow` computes
  and traces p50/p99/max every ~5 min; the target (A11: "p99 < 500 ms at 2k hubs") is documented in the
  module's own docstring, but nothing in the code or config asserts or demonstrates it is currently met at
  that scale — the metric exists, the target does not yet have live/test proof. Diagram 02.
- **Mid-window `SHORTFALL` escalation: the code path is now proven live every cycle; firing on a real
  obligation is not separately confirmed.** `allocator.cycle.classify_hub_loss()` runs unconditionally each
  cycle and would attribute any lost capacity to `L0`/`L1`/`INFEASIBLE` today — this closes last round's
  "0 production callers" gap for `R-COMMIT-LOCK-OVERRIDE-L0`/`-L1` and moves the mechanism itself to
  "Resolved" above. What remains open is narrower: no log/trace evidence in this checkout shows an `L0` or
  `L1` mid-window transition having actually fired against a real obligation, as opposed to the `L2`/
  `INFEASIBLE` paths and the window-end `R-SHORTFALL-THRESHOLD` path, which are asserted separately.
  Diagram 03 (all 12 edges drawn LIVE; this caveat is called out in the diagram's own text, not hidden).
- **Bank-level substitution (`allocator.substitute_hub()`) is wired but still never triggered** — re-checked
  this round, unchanged: `allocator/__init__.py:160` defines it and `engine.main()` still calls
  `allocator.configure(ledger)` at startup, but grep finds 0 call sites for `substitute_hub(` anywhere
  outside its own definition and the module README. Diagram 03.
- **The asset-health drift sweep is wired but switched off by config — not "0 callers."** `og-settle`'s
  `JobRunner` (`settle/main.py:125`) does call `make_asset_drift_job()` → `assets.runner.run_once()` →
  `AssetHealthService` — a real caller now exists (see "Resolved" above) — but `[assets].drift_enabled =
  false` in `orchestrator/config/orchestrator.toml:147` means the job never actually runs on this
  checkout. It was briefly enabled in an intermediate commit (`daef460`) and reverted (`b5f17a9`,
  "fleet-wide false positives") before `434d230`. Consequently `hub_inverter_pq.asset_state`,
  `calibration_attempt`, `maintenance_work_order` and `asset_event` still never get written here — the
  same observable outcome as last round, but for a different reason (a config flag, not a missing caller).
  Diagrams 05, 06.
- **Allocator continuous PQ monitoring is unbuilt; eligibility filtering (a related but separate
  mechanism) is not — don't conflate the two.** `allocator/pq_monitor.py` still has 0 callers anywhere.
  Separately, `engine.pq_eligibility` + `selector/gate.py:526`'s `exceeds_pq_eligible_capacity()` are LIVE
  admission-time controls (see "Resolved" above) — that half moved out of "still open" this round. Diagram
  06 draws the two halves distinctly rather than one "unbuilt" block.
- **The customer-operator simulators (D-11) are in progress, not started.** `integration-sims/src/ogsim/
  customer/` has 0 files on this checkout; `deploy/RUNBOOK.md` documents its systemd unit as "installed but
  not enabled until the customer services go live." Diagrams 01 and 04 mark this in-progress, not built.
- **The two-market direction (docs 08/09) is a prototype, not integrated code.** `docs/orchestrator/
  07-delivery/prototypes/two_market_lp.py` exists alongside `09-optimizer-dispatcher-update.md`, but
  nothing under `orchestrator/src` or `integration-sims/src` references it — it is not drawn as built in
  any diagram, only noted as "in build" where the lead asked for a legend/box reference.

## Index

| File | Shows | Main sources |
|---|---|---|
| [`01-system-architecture.svg`](01-system-architecture.svg) | The 6 orchestrator processes + 4 simulators (customer sim, D-11, marked in progress) + Postgres + Mosquitto + Apache; which process publishes/subscribes which MQTT topic family (incl. `cmd/cal`/`ack/cal`, NEW) and owns which table groups; external live ERCOT/EIA/NWS vs `og-sim-market`, and the exact config key that switches between them; hardened MQTT client-id/topic-root precedence. | `BUILD.md`, `orchestrator/config/orchestrator.toml`, `interfaces/mqtt/topics.md`, every process's `main.py`/`__init__.py`, `platform/mqtt.py`, `guardian/{stop_release,escalation,mqtt_io}.py`, `deploy/apache/opengrid.conf`, `dev/docker-compose.yml` |
| [`02-dispatch-cycle.svg`](02-dispatch-cycle.svg) | The full dispatch stack as implemented: selector gate (HiGHS solver process + F2 rule fallback) → real-time allocator 2 s cycle (tiers, PI loop, water-filling/substitution, price response, K13 L0/L1/L2/INFEASIBLE attribution, ERCOT_AS capacity hold) → ledger → engine→guardian handoff (bounded-concurrent bank proposals, off-tick heartbeat/persistence, per-phase latency) → guardian verdict (every G-check it actually runs, K7 posture escalation, two-person safe-stop RELEASE) → signed MQTT command batch → hub → ack/telemetry → settle. | `orchestrator/src/opengrid/{selector,allocator,ledger,engine,guardian,settle}/*.py`, `interfaces/mqtt/*.schema.json`, `interfaces/crypto.md`, `00-invariants.md` |
| [`03-commitment-lifecycle.svg`](03-commitment-lifecycle.svg) | The obligation state machine exactly as coded in `state_machine.py`'s `_TRANSITIONS` table, the K13 commitment lock (all of `L0`/`L1`/`L2`/`INFEASIBLE` now driven), substitution (hub-level vs. bank-level), the `at_risk` flag, and the ERCOT_AS capacity hold — **each edge marked LIVE or NOT DRIVEN based on a full-repo grep for its reason code / trigger function; 12 of 12 edges are LIVE at `434d230`.** | `orchestrator/src/opengrid/contracts/*.py`, `orchestrator/src/opengrid/ledger/__init__.py`, `orchestrator/src/opengrid/allocator/cycle.py`, `orchestrator/src/opengrid/engine/gateways.py`, `api/routers/dispatch.py` |
| [`04-deployment.svg`](04-deployment.svg) | The base-server layout (D-16, the confirmed permanent host — no decommission wording): every `systemd` unit with its venv/working directory/memory budget/ports/env-vars, the Apache reverse-proxy rules, Postgres (`/srv/pgdata`)/Mosquitto, the deploy/rollback/backup (`/srv/ogbackup`) flow per `deploy/RUNBOOK.md`; plus the local `docker compose` dev stack. | `deploy/README.md`, `deploy/RUNBOOK.md`, `deploy/systemd/*`, `deploy/apache/opengrid.conf`, `deploy/{cron,logrotate}/opengrid`, `deploy/mosquitto/provision_ws_users.{py,sh}`, `dev/README.md`, `dev/docker-compose.yml`, `dev/.env.example` |
| [`05-data-model.html`](05-data-model.html) | Every one of the 60 tables created or altered by migrations `0001`…`0032` (`0015` skipped; customer services landed as `0026`), grouped into 15 areas, with primary keys, foreign keys, one-line purpose, which migration touched each, and (for the R2 tables) the writer and whether it ships switched off. Verified at `6470cfa` (R2). | `orchestrator/migrations/0001_init.sql` … `0032_hub_units.sql` (0024–0032 read in full for R2) |
| [`06-power-quality-flow.svg`](06-power-quality-flow.svg) | The PQ pipeline the spec describes — waveform → transport → ingestion/storage → envelope checks (K14) → corrective ladder (calibration loop now fully wired, gated off by config) → asset health → work order → physical swap, plus the allocator PQ-eligibility/monitoring split — **with every stage marked LIVE, WIRED-BUT-CONFIG-DISABLED, BUILT-BUT-0-CALLERS, or PLANNED**, cross-checked against `docs/team/NOTICES.md`'s own wave 1/2/3 status. | `orchestrator/src/opengrid/pq_ingest/*.py`, `guardian/{pq_checks,pq_repo,mqtt_io}.py`, `assets/*.py`, `allocator/{pq_eligibility,pq_monitor}.py`, `engine/pq_eligibility.py`, `settle/main.py`, `core/models/pq.py`, `integration-sims/src/ogsim/fleet/{pq,calibration,wave,runtime}.py`, `orchestrator/migrations/0010_service_profile.sql`, `0011_asset_health.sql`, `0016_calibration_command.sql`, `interfaces/mqtt/{pq_waveform_*,calibration_*,waveform_capture_request}.schema.json` |

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
   This round (`f3b3365 → 434d230`, ~30 commits): every topic the lead flagged was re-derived from code,
   not from the commit subject lines — most consequentially, two claims this document itself carried
   forward from the prior round turned out to be stale on a fresh read: the calibration-command MQTT path
   ("0 wiring either side") is now fully wired (`guardian/mqtt_io.py:158` → `engine/__init__.py:1008`),
   and the asset-health drift sweep ("0 production callers") now has a real caller
   (`settle/main.py:125`) that a config flag happens to keep switched off. Both are called out explicitly
   as corrections above and in "Still open," not silently folded in, per the same "reviewer statements are
   claims to verify, not facts to assume" standard applied every round.

## Planned vs. built (consolidated across all six diagrams)

**Built and live** (normal operation drives it every cycle, confirmed by tracing the caller chain):

- All 6 orchestrator processes (`og-feeds/engine/guardian/safestop/settle/api`) and all 4 fleet-side
  simulators (`og-sim-fleet/scada/market/control`); the config-only live-vs-simulator switch for
  ERCOT/EIA/NWS; hardened MQTT client-id/topic-root/blob-dir separation between prod and test workspaces.
- Selector — `highs_solve` now in its own long-lived solver process, LP plans solving again (charge
  envelope + soft C15 fix, one capability read/bank; `L-DA`/`L-ID` both), with F2 rule fallback — →
  real-time allocator (tiers, `DIST_DEFERRAL` PI, water-filling, price response, hub ramp limiting, K13's
  `classify_hub_loss()` attributing `L0`/`L1`/`INFEASIBLE` every cycle, the ERCOT_AS capacity hold/
  deployment window) → ledger `reserve()` (K2, `og.commitment`) → the **full obligation lifecycle, 0
  remaining gaps**: `OFFERED → SELECTED → COMMITTED → DELIVERING → FULFILLED`/`SHORTFALL` (window-end
  threshold, or mid-window after sustained `L0`/`L1`/`L2`/`INFEASIBLE`) `→ SETTLED`, plus `→ EXPIRED`,
  `→ REJECTED` (both admission-time capacity and selection-time infeasible), and the `DELIVERING →
  DELIVERING` re-nomination self-loop — → engine→guardian handoff (bounded-concurrent bank proposals,
  heartbeat/persistence off-tick, per-phase `CYCLE_LATENCY`; isolated per-trigger gate failure) → guardian
  signing (every check G-01, G-01-ENERGY, G-02 – G-06, G-09, G-13 – G-15, G-19, G-20, G-21 – G-25 (PQ),
  plus K7 posture escalation to `CONSERVATIVE`/`ALR-SAFE-STOP-REQUESTED`) → a command-batch envelope
  signature separate from the verdict's → signed command batch → hub → ack (persisted as
  `og.command_ack`)/telemetry.
- The K13 commitment lock's *exception* machinery (`check_commitment_lock`, guardian G-19 re-verifying the
  claim against its own read, the allowed release-reason set); hub-level substitution (unhealthy hub
  excluded, healthy one water-filled in) inside the automatic 2 s cycle, traced; the `REPLACE_INVERTER`
  scenario-control action end to end.
- **Two-person guardian-signed safe-stop RELEASE (NEW, `f27e4e3`):** operator-A-requests /
  operator-B-approves, `og.operator_action` traced, signed only for allow-listed operators — see
  "Resolved since `f3b3365`" above. This overturns every earlier round's "not built" status for release.
- **K7 scope-posture escalation (NEW):** `guardian/escalation.py`'s `EscalationTracker` → `og.scope_posture`
  → `engine/gateways.py` feeding `CONSERVATIVE` scopes to the allocator as real `LIMIT`/`BLOCK`
  instructions. Degraded modes (`NO_NEW_COMMITMENTS`/`HOLD_LOCAL_AUTONOMY`/`HOLD`/
  `DIST_DEFERRAL_OPEN_LOOP`) are also live, but as `health`/`ui`/`api` display only — 0 references in
  `contracts`, so they do not gate dispatch.
- **The calibration command loop (NEW, migration `0016`):** `guardian/mqtt_io.py:158` publishes
  `cmd/cal/<hub_id>`; `engine/__init__.py:1008` routes `ack/cal/<hub_id>` to
  `assets.calibration_ack.handle_calibration_ack()`. **Allocator PQ-eligibility filtering (NEW,
  `0bfac90`)** is a separate, also-live control: `engine.pq_eligibility` + `selector/gate.py:526`'s
  `exceeds_pq_eligible_capacity()` refuse to commit beyond a profile's live-health-derived eligible
  capacity.
- **PQ wave 2 ingestion + guardian checks (`9153dc2`):** `ogsim.fleet.wave.py` **synthesizes real
  waveform samples** (`synthesize_raw_capture()`, S7.4's sinusoid-plus-harmonics model) and periodic
  summaries, published every tick plus a 1 %/min rotating audit and on-demand request replies; `og-engine`
  subscribes and batch-flushes `scada/wave/.../summary`, and validates+stores `.../raw` in a background
  worker. Guardian's G-21–G-25 read real Postgres state and feed the same violations list every other
  check does.
- `obligation.at_risk` now set **and cleared** every cycle (not just alerted); measured K1/K2 proof
  counters (`og.invariant_check`/`invariant_violation`, written solely by `opengrid.invariants`, read by
  `api/routers/{health,dispatch}.py`); `og-engine`'s durable per-start epoch (K6), buffered/async SCADA +
  telemetry persistence, per-hub ramp-limited setpoints, and cycle-latency (p50/p99) tracing; `og-settle`'s
  single `run_forever` loop; `og-api`'s process heartbeat; per-product feed-staleness thresholds;
  `og-feeds`' single `run_forever` loop; `og-engine`'s own `/metrics` (NEW, loopback-only, confirming half
  of the prior round's "Uncertain" item).
- All 47 data-model tables across 22 migration files (`DATA_CENTER` service type, additive; 7 brand-new
  tables for invariants/posture/AS-hold/calibration — see "Resolved" above); the full
  deploy/systemd/Apache/Postgres/Mosquitto topology, hardened this round (`/srv/pgdata`, `/srv/ogbackup`,
  `deploy/RUNBOOK.md`, base confirmed the permanent host, D-16) and the local dev `docker compose` stack.

**Wired but disabled by config** (a real production caller now exists; a config flag keeps it from
running — distinct from "0 callers"):

- **The asset-health drift sweep.** `og-settle`'s `JobRunner` (`settle/main.py:125`) calls
  `make_asset_drift_job()` → `assets.runner.run_once()` → `AssetHealthService`, but
  `[assets].drift_enabled = false` (`orchestrator/config/orchestrator.toml:147`) means it never actually
  runs on this checkout (briefly `true` in `daef460`, reverted in `b5f17a9` for fleet-wide false
  positives). `hub_inverter_pq.asset_state`, `calibration_attempt`, `maintenance_work_order`, `asset_event`
  still never get written as a result (diagrams 05, 06).

**Built but 0 production callers** (the logic/schema exists and is unit-tested; nothing in a live process
calls it):

- Bank-level substitution (`allocator.substitute_hub()`, defined at `allocator/__init__.py:160`) — wired
  (`engine.main()` calls `allocator.configure(ledger)` at startup) but still 0 production call sites
  outside its own definition and the module README (diagram 03).
- Allocator continuous PQ monitoring (`allocator/pq_monitor.py`) — 0 callers anywhere; not to be confused
  with PQ-eligibility filtering, which is live (see "Built and live" above). Never part of any wave-2
  report (diagram 06).

**Planned, no code at all:** the corrective ladder's rebalance / reactive-PF / exclusion steps; a
stand-alone "wave-ingestion service" process (ingestion is real, but folded into `og-engine`, not a
separate process); the customer-operator simulators (D-11 — `integration-sims/src/ogsim/customer/` has 0
files; its systemd unit is documented as installed but not enabled). (Waveform sample generation, "WP-H,"
was wrongly listed here in an earlier pass — it is in fact LIVE; see "Built and live" above.)

**Prototype only, not integrated:** the two-market direction (docs 08/09) — `docs/orchestrator/
07-delivery/prototypes/two_market_lp.py` exists, but nothing under `orchestrator/src` or
`integration-sims/src` references it. Drawn only as an "in build" legend note where asked for, never as
built.

**Not a gap (by design):** the spec's "SCED-interval LP" is explicitly folded into the allocator's
per-cycle price response (`02a-mvp-s-spec-engine.md` §3.1) rather than run as its own 5-minute solve —
drawn in diagram 02 as a dashed cross-reference, not as "planned."

## Uncertain

Per the work order, listed here rather than guessed at:

- **`og_sim` grant `topic read og/v1/scada/ctl/#`** in `dev/mosquitto/opengrid.acl` (generated by
  `dev/scripts/gen_mosquitto_acl.py`). This topic pattern does not appear in `interfaces/mqtt/topics.md`
  and no publisher or subscriber for `scada/ctl/#` was found anywhere in `orchestrator/src` or
  `integration-sims/src`. Likely reserved/vestigial; not drawn as a live topic in diagram 01.
- **Prometheus `/metrics` for `og-feeds` (partially resolved this round).** `og-engine` is now confirmed:
  `engine/metrics.py:56`'s `start_metrics_server()` (called from `engine/__init__.py:915`) serves
  `/metrics` on `[metrics].engine_port`, loopback-only. `og-feeds` is still unconfirmed — grep finds 0
  calls to `start_http_server`/`start_metrics_server` anywhere under `feeds/`, though `platform/
  metrics.py`'s shared metric objects still read as if it should also export them. Diagram 04 now draws
  the guardian's and engine's ports as confirmed, and still does not draw one for `og-feeds`.
- **Exact per-environment hub/bank counts in `dev/config/fleet.dev.yaml` and `scada.dev.yaml`** — diagram
  04's "200 hubs / 8 banks (dev default)" is sourced from `dev/config/dev.toml`'s `[fleet]` section and
  `dev/docker-compose.yml`'s comments (which say these YAML files "must both change together" with it);
  the YAML files themselves were not opened.

## Terminology (used consistently across all six diagrams)

- **Substitution** — a software move of delivery between hubs (live, traced), or, for the wired-but-never-
  triggered ledger primitive, between banks. Never changes an obligation's committed total.
- **Inverter swap / replacement** — a physical hardware action (`ogsim.fleet.pq.replace_inverter`,
  `og.asset_event.event_type = INVERTER_REPLACED`). Remote **recalibration** is always tried first.
- **Capacity hold (AS)** — an ERCOT_AS-committed bank's grant is 0 kW (`R-GRANT-AS-HOLD`) until an
  operator opens a time-bounded deployment window, which then discharges it up to its committed kW for
  that one window only (`og.as_deployment`). Distinct from a normal energy grant, which is never held back
  this way.
- **Posture / degraded mode** — two different, easily-confused concepts. *Posture* (`og.scope_posture`,
  `NORMAL`/`CONSERVATIVE`) is guardian-computed per bank/zone from veto ratio and actively **feeds the
  allocator** real dispatch instructions (K7). A *degraded mode* (`og.degraded_mode_state`) is
  health/UI-computed and **display-only** — it is not read anywhere in `contracts` and does not gate
  dispatch.
- **Safe-stop RELEASE** — the two-person, guardian-signed act of lifting an engaged stop (`guardian/
  stop_release.py`), distinct from *requesting* a stop (any operator, K8) or `ALR-SAFE-STOP-REQUESTED`
  (the guardian merely proposing one after sustained `CONSERVATIVE` posture — never self-engaging it).
