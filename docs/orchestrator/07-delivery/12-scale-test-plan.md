# Scale test plan: 10,000-hub fleet (build phase, 2026-09-26)

Status: **plan only — no load test has been run yet.** This is the "load-test plan" deliverable for the
Austin Energy / scalable-fleet build (`08-market-model-two-markets.md`, `00-invariants.md`). It describes
how to run the fleet at 10,000 hubs in an isolated workspace, what to measure, and what counts as a pass.
It does not itself run anything against the shared broker or the production database.

## 1. Scope

Fleet-**size** scalability only (2,000 -> 10,000 hubs), independent of the Austin/CPS market-model work
(zone blocks, `ZoneBlockConfig`). The two can be combined later (a 10k-hub config that also enables
`zone_blocks`), but this plan's baseline is the plain scale-up: 200 banks x 50 homes, same 4 ERCOT
competitive zones as today, using the `fleet.10k.yaml` preset
(`integration-sims/config/fleet.10k.yaml`).

## 2. Isolation (mandatory — read `BUILD.md` S5/S6 first)

Every part of this test runs inside **one agent workspace**, never against the shared broker, the
production database, or with any orchestrator/ogsim process started outside `tools/remote.ps1`:

- Pick an unused workspace name from `BUILD.md` S5's list (or a new one your lead assigns for the load
  test specifically — do not reuse another agent's active workspace).
- `tools/remote.ps1 -Ws <ws> -Cmd "..."` gives that workspace its own Postgres database (`og_t_<ws>`) and
  MQTT topic root (`ogtest/<ws>`).
- **MQTT credentials:** the workspace's own broker user, `ogw_<ws>` (provisioned by
  `deploy/mosquitto/provision_ws_users.sh`, ACL-restricted to `ogtest/<ws>/#` only). It can never
  authenticate as a production MQTT user, and every workspace-aware client (`opengrid.platform.mqtt`,
  `ogsim.common.config.resolve_mqtt_credentials`) refuses to start without
  `OG_MQTT_WS_USER`/`OG_MQTT_WS_PASSWORD` set from it. Never export or fall back to `og_sim`/`og_engine`/
  etc.'s production passwords in this workspace.
- Point both the fleet simulator and the seed loader at the 10k preset:
  `OGSIM_FLEET_CONFIG=integration-sims/config/fleet.10k.yaml` (sim) and
  `OG_FLEET_SIM_CONFIG=integration-sims/config/fleet.10k.yaml` (orchestrator's `opengrid.fleet.seed`,
  overriding its normal sibling-file resolution — see that module's docstring). This is the "matching
  seed option": no code change is needed, only pointing both processes' config env var at the preset.
- Test ports >= 18000 (never the production 8080/8090/8091).
- **Never** run this against the shared broker, in production, or as a background job outside your
  workspace's lifetime. **Never** `pkill`/`killall` by pattern to stop it — start it with `cmd & pid=$!`
  and stop only that PID.

## 3. What to run

1. Seed the 10k topology into the workspace database:
   `OG_FLEET_SIM_CONFIG=integration-sims/config/fleet.10k.yaml python -m opengrid.fleet.seed`
   (expect: "seeded 200 banks, 10000 hubs").
2. Start the fleet simulator against the workspace broker/topic root with the 10k preset (foreground,
   `cmd & pid=$!`, stopped by PID only):
   `OGSIM_FLEET_CONFIG=integration-sims/config/fleet.10k.yaml python -m ogsim.fleet`.
3. Start `og-engine`/`og-guardian`/`og-api` (and any other process under test) the same way, all bound to
   the workspace's `OG_DB`/`OG_MQTT_ROOT`/ports.
4. Drive load for a sustained window (target: >= 30 minutes steady state, per Q3's perf-tooling lane) —
   reuse fancyviper007's perf capture tooling on branch `wp/U1-console-redesign`
   (`tests-e2e/perf/`, work package Q3 in `docs/team/lane-ux-perf-demo.md`: "drive the fleet sim at 2k
   (local) and 10k (the lead runs it on the server)... capture the allocator cycle p99... from
   `/metrics`") rather than writing new capture code — that branch is not yet merged to `main`, so check
   it out separately or ask the lead to run it if the load test happens before it lands.
5. Enable `sysstat` (`sar`) for disk I/O sampling for the duration of the run (already enabled on the base
   server per this plan's requirement; confirm with `sar -V` before starting).

## 4. What to measure

| Metric | Source | Why |
|---|---|---|
| Allocator cycle p99 | `og-engine`'s `/metrics` (Prometheus; see `[health].engine_metrics_url`) | 02a's 2 s cycle budget; the number that most directly answers "does 5x the hubs blow the cycle budget" |
| Guardian verdict time (p50/p99) | `og-guardian`'s own metrics / trace timestamps (decision pre-image to signed command) | K10/K12 — the guardian must still sign within its lease/clock-quality budget at scale |
| Telemetry write rate | `og.telemetry` insert rate (Postgres) or the `og-feeds`/ingest side's own counters | 2,000 hubs at the default 2 s cadence is ~1,000 msg/s already (fleet.yaml's wave-2 bandwidth note); 10,000 hubs is ~5,000 msg/s — confirm the ingest path doesn't fall behind (queue growth, dropped messages) |
| Disk I/O | `sar -d` (sysstat), for the duration of the run | Postgres write volume scales with hub count (telemetry, hub_state); confirms no I/O-bound cliff |
| Memory | `og-engine`/`og-guardian`/`ogsim.fleet` RSS over time (`ps`/`/proc`, or a metrics exporter if one exists) | `ogsim.fleet.state.FleetState` is struct-of-arrays (numpy), so memory should scale ~linearly with hub count, not superlinearly — confirms no accidental O(n^2) structure (e.g. per-hub Python objects, unbounded per-cycle allocation) |

Capture all five for both the 2k baseline (today's default `fleet.yaml`) and the 10k preset, in the same
workspace shape, so the comparison isolates the hub-count effect from environment noise.

## 5. Pass criteria

- **Allocator cycle p99 < 500 ms** at 10k (Q3's existing target is "< 500 ms at 2k"; this plan does not
  relax it for 10k — a 5x hub count increase must not blow the 2 s cycle budget's headroom).
- **Guardian verdict time p99 < its configured `verdict_timeout_ms`** (`[guardian].verdict_timeout_ms`,
  300 ms default) — a guardian that can't keep up at scale degrades to hold/schedule (K7), which is safe
  but must be reported, not silently tolerated as "the new normal."
- **Telemetry write rate keeps pace with the fleet's publish rate** — no sustained ingest queue growth or
  dropped-message rate above what the 2k baseline already shows (a "the sim publishes 5x, the DB accepts
  5x" check, not an absolute number).
- **Disk I/O has no sustained saturation** (`sar -d` `%util` not pinned near 100% for the run's steady
  state) — the base server's disk must not become the bottleneck before the CPU/logic does.
- **Memory scales sub-quadratically**: 10k RSS should be roughly proportional to 2k RSS x 5 (allowing
  normal per-process fixed overhead), not dramatically higher — a much larger multiple flags an
  accidental O(n^2) data structure.
- **Zero K1–K14 invariant violations** during the run (the same bar as every other MVP-S test — a scale
  test is not exempt from the safety invariants; see `00-invariants.md`).

A run that fails any of the above is reported (which metric, by how much, and the workspace/run
parameters) rather than quietly re-run with looser settings until it passes.

## 6. Out of scope for this plan

- Combining the 10k scale-up with the Austin/CPS `zone_blocks` (build item 1) — a follow-up config
  (`hub_count`/`bank_count` scaled AND `zone_blocks` enabled) can reuse this same plan's isolation and
  measurement sections once both land.
- Selector/allocator/engine/settle code changes to actually improve any metric that fails — this plan
  only measures; the live-path agent (or whichever owner's code the bottleneck falls under, per BUILD.md
  S4 ownership) makes the fix.
- Running this against the shared broker or in production, ever.
