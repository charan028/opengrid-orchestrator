# Known limitations (hand-over)

Status: hand-over document, written against branch `wp/docs-lane` at `main` commit `434d230`
(`434d2301a99d6643dce6c9e9a6d29fd76f374d15`). What changed at R2 (`main` `6470cfa`) is in "Changes at R2" below
the method note, and the Summary marks those items. What R3 (`main` `451a2a2`, tag `r3`) fixed is marked in that
table, and the limitations the R3 review found are in "Open at R3". What r3.4.1 to r3.4.3 closed, and what is new
or still open at r3.4.3, is in "Status at r3.4.3" directly below. Audience: a technical, finance-literate owner and the next
engineering team.

Purpose: an honest inventory of everything that is **built but dark** (present in the code, gated by a config flag,
or with no production caller), everything **not built** at all, and the **operational limits** of what is running
today. Every "exists" claim below cites a `file:line` from a `Read`/`git grep` on this checkout. Every "not built" or
"no production caller" claim states what was searched. Anything not directly verifiable from the repository is
marked **uncertain** rather than assumed, and no vendor, price or date is invented anywhere below.

**Method note.** Absence-of-caller claims rest on static `grep`/`git grep` across `orchestrator/src/`,
`integration-sims/src/` and `tests-e2e/` on this one checkout, not on running the code (per this task's rules: no
tests were run, nothing was executed, the network was not touched). A call assembled dynamically (`getattr`,
string dispatch) would not show up in a literal-text search; nothing found while researching this document suggests
that pattern is used in the modules discussed, but this is a static-analysis exercise, not a runtime trace. Where a
finding depends on server-side state this checkout cannot see (for example whether something was hand-patched on
`192.168.5.35` outside git), it is called out as unverified.

---

## Status at r3.4.3 (2026-09-27)

Re-checked by static reads of the r3.4.3 tree (main `897965f` plus the r3.4.3 lanes); nothing was run. Line
numbers below are r3.4.3's. Decisions D-33 to D-38 are in [11-decision-log.md](11-decision-log.md).

### Closed or narrowed since R3

"By r3.4" means the fix was already in the tree before r3.4.1 and is confirmed at r3.4.3.

| # | Status | How |
|---|---|---|
| R3-1 | **Partly fixed (r3.4.1).** Still off. | The K4 retry keeps the original cycle id on its grant rows (only grant ids get `:r1`) and on its batch, so the guardian's per-cycle accumulators see one cycle (`orchestrator/src/opengrid/engine/__init__.py:1064-1066`). `[allocator.veto_retry] enabled = false` is unchanged, and its config comment still says the re-solve does not reuse the cycle id, which is now stale. |
| R3-3 | **Fixed (by r3.4; hardened r3.4.1).** | og-engine passes the topic and its trace store to `upsert_device_info` (`engine/__init__.py:1734-1736`); r3.4.1 bounds the free-text fields (128 chars, no control characters) and restricts `hub_id`. |
| R3-4 | **Fixed (by r3.4; r3.4.1).** | The stop row and its outbox entry are written in one transaction (`safestop/backend.py:91`); an entry that fails `max_attempts` times is dead-lettered and re-alerted on every drain (`:85`, `:111-113`); r3.4.1 drains in per-scope sequence order with a dead-letter sweep alert and a safe in-request drain. |
| R3-5 | **Fixed (r3.4.1).** | Health reads `[health].hub_stale_s` (`health/model.py:200`), and fleet search classifies hubs with the same `HealthThresholds.from_config` instead of a hand-built 6 s default. |
| R3-6 | **Fixed (by r3.4; r3.4.1).** | Migration 0041 lets `og.trace` accept `AUTHZ_DENY`. r3.4.1 quarantines any row Postgres refuses for its content (`<journal>.quarantine.jsonl`, `ALR-TRACE-QUARANTINED`), and replay continues past it (`trace/pg_backend.py:91-116`, `:437-440`). |
| R3-7 | **Fixed (by r3.4).** | A failed poll is retried after 5 min, doubling to 1 h, so a missed daily NP4-188-CD poll no longer holds the data stale for a day (`feeds/scheduler.py:45-67`). |
| R3-9 | **Fixed (by r3.4).** | The `ogsim` CLI and the smoke test send `X-OGSim-Request: 1` (`integration-sims/src/ogsim/control/cli.py:48`, `tests-e2e/smoke.py:77`). |
| R3-10 | **Partly fixed.** | `deploy/systemd/og-lifecycle.service` and `.timer` exist and `deploy.sh` installs them, but the timer is not enabled until an I/O check of the pgdata disk; the Kubernetes CronJob ships `suspend: true` (see [15-data-lifecycle.md](15-data-lifecycle.md)). |
| R3-11 | **Fixed (by r3.4).** | A bank proposed with no grants uses the ledger's current version (`engine/__init__.py:955-962`), not 0, so G-09 no longer vetoes it for a stale version. |
| NB-9 | **Narrowed (r3.4.1).** | The repo's og-settle unit may write the secondary anchor copy (`ReadWritePaths=... -/srv/ogbackup/anchors`, `deploy/systemd/og-settle.service:27`); r3.4.1 made the backup paths optional. The anchor is still on the same host, not off-node. |
| R2-4 | **Mostly fixed (r3.4.1, r3.4.2).** | Every deployment goes through `opengrid.calls` (D-33): the product cap, no overlap (checked under a lock), within the awarded kW (409 `R-CALL-OVER-COMMITTED`); the engine caps a called obligation at the call's requested kW. |
| R2-5 | **Fixed for data (r3.4.1, D-36).** | The topology seed maps every hub, bank and feeder (transformers, feeder limits, HOME_BANK assets, premise limits); `deploy/scripts/topology_backfill.sh` inserts the missing rows on existing databases. `fail_closed_missing_topology` is still `false` (see N-6). |
| BD-1 | **Changed (r3.4.2, D-37).** | The Austin substation set and the LZ_LCRA and LZ_RAYBN blocks are enabled in the simulator; LZ_LCRA and LZ_RAYBN are regulated and UNAVAILABLE (no contract). LZ_CPS stays off. |
| BD-3b | **Customer API enabled (r3.4.1, D-33)** for the utility role only (AUSTIN_ENERGY). | `[api.customer_api].enabled = true`, `[api.utility_api].enabled_utilities = ["AUSTIN_ENERGY"]`. Site ingest stays off. |
| NB-1 | **Partly built (r3.4.1, D-34).** | A DNP3 outstation over mutual TLS exists in og-engine but ships disabled (N-1). ICCP is not built. SCADA is still simulated. |
| NB-4 | **Installer built (r3.4.1).** | `deploy/k8s/install.sh` and a Helm chart install the whole stack on one cluster. It was validated offline (lint, kubeconform, podman smoke test) and not deployed to a real cluster. Production still runs on systemd, and it is not HA. |
| NB-5 | **Built as an advisory copilot.** | `[ai_agent]`: read-only, never in the control loop; r3.4.2 adds fleet queries through a read-only fleet tool and names the screening model. |
| OL-2 | **Harness built (r3.4.1).** | `tests-perf/` runs the scale and stress campaign up to 7,500 homes; no campaign results are in the repo yet. |
| OL-5 | **Narrowed (r3.4.2).** | Regulated zones (LZ_AEN, LZ_CPS, LZ_LCRA, LZ_RAYBN) carry no M1 by design (D-37). |

### New or still open at r3.4.3

| # | Limitation | Evidence |
|---|---|---|
| N-1 | **The utility grid link is disabled by default.** Nothing listens until `[grid_link].enabled` and a utility's own `enabled` are both true. Peer addresses (192.0.2.0/28, a documentation range), CNs and certificate paths are placeholders; only a loopback test PKI and enablement script exist (r3.4.3). When both switches are on, og-engine starts the link at startup (`start_grid_link` in `engine/__init__.py`). No real utility EMS has connected. | `orchestrator/config/orchestrator.toml:462-543` |
| N-2 | **AS-POLL is off in the repo and talks only to the simulator.** `[feeds.ercot_as_poll].enabled = false`; the MMS endpoint is the ogsim simulator with `signing = "none"`. A real ERCOT MMS endpoint and credentials are not configured (NB-2). | `orchestrator.toml:398-418` |
| N-3 | **LCRA and Rayburn have sample contracts only.** Their banks are UNAVAILABLE (`REGULATED_NO_CONTRACT`), their sample toll contracts are SUSPENDED, and their utility API and grid link identities are disabled; no Apache accounts exist for them. | `orchestrator.toml:338-346`, migration 0046 |
| N-4 | **Delivery verification has a partial console view (r3.4.3).** The Dispatch screen's AS/toll awards table has a Delivery column and a detail drawer (committed/commanded/delivered/meter chart) for deployed calls. Manual discharge targets and the per-contract summary are served only by the operator API (`/og/api/delivery/records`, `/records/{call_id}`, `/summary`). The call status keeps the deprecated `granted_kw`/`granted_kwh`/`granted_description` fields for one release (removed in r3.5). | `ui/templates/dispatch.html`, `ui/static/og-delivery.js`, `api/routers/delivery.py`, `calls/models.py` |
| N-5 | **No retention for `og.dispatch_call` or `og.delivery_record`.** Neither has an `og.data_retention` row, so both are kept forever and never archived. | migrations 0047, 0050; `lifecycle/retention.py:47` |
| N-6 | **Missing topology still does not fail closed.** `[guardian.flow] fail_closed_missing_topology = false`; with D-36's mapping a fresh install has no unmapped rows, so this matters only where data is missing. | `orchestrator.toml:185` |
| N-7 | **Meter corroboration covers few banks.** Only SUBSTATION asset banks are metered by default (`[delivery].meter_bank_ids = []`); home-bank calls report `NO_METER`. | `orchestrator.toml:283-286` |
| N-8 | **AS capacity settles at the opportunity price when no MCPC is stored.** Without an NP4-188-CD observation for the product and hour, settle falls back to the opportunity's stored value (flag `OPPORTUNITY_PRICE`, logged). | `orchestrator/src/opengrid/settle/pg_backend.py:156-160` |
| N-9 | **`[retention]` in `orchestrator.toml` is not read.** Trace pruning reads `og.retention_policy`, and table lifecycle reads `og.data_retention`. | Help maintenance page; `trace/pg_backend.py:307-309` |
| N-10 | **The utility simulator is on demand only.** `ogsim.utility_aen` ships with its `random.yaml` types disabled; LCRA and RAYBURN are disabled in it. | `integration-sims/config/random.yaml:22-23`, `:291` |
| N-11 | **Toll ramp re-anchoring is proven in unit tests and the loopback, not on a real 20 MW asset (r3.4.3).** og-engine steps a utility-scale hub from its last guardian-signed setpoint (`engine/ramp_anchor.py`) and the guardian's G-04 bounds one cycle from the same anchor (full step for the G-05/G-06/G-32 rate checks), reloading its signed anchors after a restart. A veto, a lapsed lease or a safe stop drops the anchor; after a release the hub is held at 0 kW until fresh telemetry. The r3.4.2 behaviour (anchor refreshed on unsigned proposals, stretched G-04 dt) is fixed. | `engine/ramp_anchor.py`; `guardian/checks.py` (`g04_anchor_kw`, `ramp_step_kw`); `tests/unit/guardian/test_toll_reanchor.py` |

---

## Open at R3 (`main` `451a2a2`, tag `r3`)

Found by the R3 adversarial review. Each item was read at `451a2a2` and is still in the code at `main` `fdb0cdd`
(tag `r3.3`). The rules are the same as for the rest of this document (static reads, nothing run), except R3-11,
which was also observed on the local dev stack. Line numbers are `451a2a2`'s. **At r3.4.3, R3-1 and R3-10 are
partly fixed and R3-3 to R3-7, R3-9 and R3-11 are fixed; see "Status at r3.4.3" above. R3-2 and R3-8 were not
re-verified.**

| # | Limitation | Evidence |
|---|---|---|
| R3-1 (new) | **The K4 same-cycle retry gets fresh guardian budgets and replaces allocator state.**<br>• The retry is proposed as cycle `<cycle>-r1`, and the guardian keys its G-05, G-06/G-32 and G-28/G-29/G-30 sums on the cycle id. A retried batch can therefore take the fleet ramp, a feeder's ramp or its reverse-flow margin past the limit within one physical cycle.<br>• The retry re-runs the allocator for the retried banks only. That replaces the hold-last-grants set, so a gateway timeout in the next cycle holds only those banks, and it steps their DIST_DEFERRAL PI a second time.<br>• `[allocator.veto_retry] enabled = false` turns the retry off. | `orchestrator/src/opengrid/engine/__init__.py:942`, `:965`; `guardian/service.py:405`, `:416`, `:426`, `:529`; `allocator/__init__.py:161`, `:192-205`; `allocator/cycle.py:197-207`; `engine/settings.py:61`, `:93` |
| R3-2 (new) | **Without `REAL_POWER_KW`, rooftop-PV export is not seen.**<br>• A kVA-only bank's export floor is Σ hub p − Σ `pv_rated_kw`.<br>• Nothing writes `og.hub.pv_rated_kw`, so the default 0 applies. With idle batteries the floor is ≥ 0, so G-28/G-30 treat the bank as not exporting. | `orchestrator/src/opengrid/guardian/flow_repo.py:223-228`, `guardian/config.py:108` |
| R3-3 (new) | **A hub's device-info message re-rates it without a trace.**<br>• og-engine calls `upsert_device_info` without a trace store and takes the hub id from the payload, not the topic.<br>• The module writes the reported units, kW, kWh, reserve floor and location whenever they differ from the record, and traces only when it has a trace store.<br>• A report of `reserve_floor_pct` 0, or of another hub's id, changes the ratings the guardian loads at its next start (G-01 reserve). | `orchestrator/src/opengrid/engine/__init__.py:1561`; `fleet/device_info.py:120`, `:131-144`; `guardian/repo.py:130-151` |
| R3-4 (new) | **A safe stop can be recorded but never published.**<br>• og-safestop writes the `og.stop_event` row and the outbox entry in separate transactions. A failure between them leaves a recorded stop with nothing queued, and a redelivered L2 ESTOP then reads as already acted on.<br>• The outbox drain stops at the first failing entry, with no attempt cap or dead-letter. | `orchestrator/src/opengrid/safestop/service.py:114-124`, `:191-194`, `:196-217`; `safestop/pg_backend.py:62-67`, `:107` |
| R3-5 (new) | **`[health] hub_stale_s` is not read.**<br>• A hub is stale after 2 × the telemetry interval (20 s at 10 s) and offline after `hub_offline_s` (60 s).<br>• The console's age badges use 10 s (Fleet table) and 6 s (hub drill-down), so hubs reporting every 10 s flicker stale.<br>• The guardian's own K1 freshness is 60 s. | `orchestrator/src/opengrid/health/model.py:182-184`, `:195`; `health/rules.py:74-78`; `ui/routes/fleet.py:88`; `ui/templates/_partials/hub_drilldown.html:43`, `:129`; `guardian/config.py:34` |
| R3-6 (new) | **One refused trace row can stop the K11 journal replay.**<br>• The `og.trace` decision-type check (migration 0033) does not allow `AUTHZ_DENY`, which og-api writes for every audited deny (for example a viewer's 403 on a bulk command).<br>• The refused insert is journaled as if the database were down. Replay stops at the first failing entry, so that entry stays at the head of the journal, and rows journaled in a later real outage never reach `og.trace`. | `orchestrator/migrations/0033_data_lifecycle.sql:291-296`; `orchestrator/src/opengrid/authz/enforce.py:60-63`; `orchestrator/config/authz.toml:28-29`; `api/routers/fleet_bulk.py:44`, `:118`; `trace/pg_backend.py:477-481` |
| R3-7 (new) | **One missed AS price poll can block new commitments for up to about a day.** `np4-188-cd` is a blocking feed since R3. It is polled every 24 h, and a failed or skipped poll is not retried before the next one. | `orchestrator/src/opengrid/health/model.py:72-74`; `feeds/scheduler.py:32`, `:86-88` |
| R3-8 (new) | **A contract-scoped intake skip under `NO_NEW_COMMITMENTS` is never traced.** The `INTAKE_SKIPPED` payload carries the trigger's `contract_scope` as a UUID. The canonical-JSON hash rejects it, and the error is logged and swallowed. | `orchestrator/src/opengrid/engine/gates.py:120`, `:128-132`; `engine/__init__.py:152`; `core/tracehash.py:26-27`; `core/crypto.py:148` |
| R3-9 (new) | **The simulator CLI and the smoke test get 403.** Since R3 every POST/DELETE on the simulator control plane needs `X-OGSim-Request: 1`. The `ogsim` CLI, `tests-e2e/smoke.py` and the e2e harness's `control()` do not send it. | `integration-sims/src/ogsim/control/app.py:68-73`; `control/cli.py:122`, `:145`, `:179-185`; `tests-e2e/smoke.py:77`; `tests-e2e/functional/e2e_stack.py:139-140` |
| R3-10 (new) | **The data-lifecycle job is not deployed.** Migration 0033 leaves telemetry partitions and index builds to `opengrid.lifecycle`, but `deploy/` has no unit, timer or cron entry that runs it. | `orchestrator/migrations/0033_data_lifecycle.sql:5-13`; `deploy/systemd/`, `deploy/cron/` |
| R3-11 (new) | **A manual target on a bank with no grant is always vetoed, and escalates.**<br>• The engine adds a bank with a live manual target and no grant with an empty grant list, and proposes it with ledger version `max(..., default=0)` = 0.<br>• G-09 vetoes any version other than the guardian's current one (`STALE_LEDGER_VERSION`).<br>• **Observed on the local dev stack at `r3`** (not a static read): a 0.1 kW, 2-minute target on an idle hub was vetoed G-09 every cycle and the hub stayed at 0 kW. `ALR-SAFE-STOP-REQUESTED` opened for its bank and zone 6 s after the confirm, and cleared about 60 s after the target was cancelled. | `orchestrator/src/opengrid/engine/__init__.py:1052-1053`, `:1065` (the K4 retry, `:968`, has the same default); `guardian/checks.py:160-166` |
| NB-9 | **Anchoring at R3.** Anchors use absolute paths (`/var/lib/opengrid/anchors`, secondary `/srv/ogbackup/anchors`). The repo's og-settle unit allows writes to `/var/lib/opengrid` and `/var/log/opengrid` only, so from the repo's unit the secondary copy cannot be written. The server's own unit is not checked here. | `orchestrator/config/orchestrator.toml:186-188`; `deploy/systemd/og-settle.service:22-23` |

---

## Changes at R2 (`main` `6470cfa`)

The sections below this one describe `434d230`. This table records what changed for each item at R2, on the same
rules (static reads of the `6470cfa` checkout; nothing was run). Items not listed here were not re-verified.

| # | Status at R2 | Evidence (R2 line numbers) |
|---|---|---|
| BD-1 | **Still dark.** The zone blocks stay `enabled: false`, and `[fleet].zones` still lists the four ERCOT zones. The territory code now exists (selector C25, allocator, guardian G-33/G-30), so enabling a block would apply the K15 rules. | `integration-sims/config/fleet.yaml:56-72`, `integration-sims/config/scada.yaml:17-25`, `orchestrator/config/orchestrator.toml:105` |
| BD-2 | **Changed.** The allocator's PQ-eligibility filter runs in the real-time cycle. The S5.4 ladder and continuous monitor are wired, but are built only when `[site_ingest].enabled` or `[allocator.closed_loop].enabled` is true, and both ship false. | `orchestrator/src/opengrid/allocator/cycle.py:461-470`, `orchestrator/src/opengrid/engine/wiring.py:84-106`, `orchestrator.toml:128-130`, `:206-208` |
| BD-3b | **Built, dark.** The customer API is mounted only when `[api.customer_api].enabled`, which ships false. Site ingest sits behind `[site_ingest].enabled = false`. The `og-sim-customer` unit is not in `ogsim.target`. Migration 0026 creates the four customer-services tables. | `orchestrator/src/opengrid/api/app.py:177-190`, `orchestrator.toml:202-204`, `orchestrator/src/opengrid/engine/__init__.py:976-980`, `deploy/systemd/ogsim.target`, `orchestrator/migrations/0026_customer_services.sql` |
| NB-9 | **Partly built.** Every 900 s the invariants runner writes the chain head to two directories (`[trace].anchor_dir`, `anchor_secondary_dir`; default relative `var/anchors*`), logs each publish in `og.trace_anchor` (migration 0028), and checks anchor freshness. The shipped config sets neither directory nor a signing key, so anchors are unsigned and on the same host, not off-node. | `orchestrator/src/opengrid/invariants/__init__.py:121-122`, `:171-175`, `orchestrator/src/opengrid/trace/anchoring.py:34-35`, `:44-59` (`load_anchor_key`: no key, unsigned), `:100-160`, `orchestrator.toml:145-147` |
| NB-10 | **Partly built.** The guardian vetoes a gross synchronized step (G-05). There is still no signed per-hub start jitter. | `orchestrator/src/opengrid/core/limits.py:370-378`, `orchestrator/src/opengrid/guardian/service.py:360-368` |
| NB-12 | **Mostly built.** Built: the market model (migration 0025), territory (selector C25, allocator, G-33/G-30), the regulated-first stage R, C3′/C27, the allocator flow limits and guardian G-26…G-33, and the $/kW economics. Not built: substation-asset dispatch, flow rows in the selector, the contract-admission territory check, and part of the K15 checker. Per-item status: `00-invariants.md` K4/K15 and `10-traceability-matrix.md`. | `00-invariants.md` (K4 extended, K15) |
| OL-2 | **Partly run on the local dev stack** (PR #35, code at `434d230`). 2,000 hubs passed (allocator cycle p99 82-96 ms). The 10,000-hub run did not produce a result on that host: the fleet simulator saturated one core, and og-engine's MQTT ingest stopped after a keepalive timeout and never reconnected. That defect was routed for R3 and is still present at `6470cfa`. | `orchestrator/src/opengrid/engine/__init__.py:1195` (`_mqtt_ingest_loop`, no reconnect) |
| OL-5 | **Fixed with a proxy.** M1 = delivered kWh / (η_c·η_d) × the zone's trailing 24 h grid share of charging × the TDSP volumetric rate. | `orchestrator/src/opengrid/settle/__init__.py:270-292`, `orchestrator/src/opengrid/settle/tariffs.py:101-127` |
| DM-1 | **Unchanged.** The R2 change to `stop.py` is the ramp tracker only. | `integration-sims/src/ogsim/fleet/stop.py:113-114` |
| DM-2 | **Structurally resolved.** Migration 0026 (not 0015) creates both tables, and `opengrid.site_ingest` exists, so the import succeeds. Readings arrive only when `[site_ingest].enabled`, which ships false. | `orchestrator/migrations/0026_customer_services.sql`, `orchestrator/src/opengrid/invariants/queries.py:359-365` |
| Degraded modes | **`NO_NEW_COMMITMENTS` is enforced** at the intake gate and the selector gate (an unreadable mode counts as active). It is not checked at contract admission or at customer-API submission. At `6470cfa` `DIST_DEFERRAL_OPEN_LOOP` was never set (the only caller never passed the SCADA-silent argument). **R2 hotfix v3 (`afb26c2`):** only feeds in `[health] firm_blocking_feeds` (default `ERCOT:np6-905-cd`) set NO_NEW_COMMITMENTS, and the load, NWS, EIA and solar feeds alert only. `DIST_DEFERRAL_OPEN_LOOP` is now set, with `ALR-SCADA-SILENT`, after 60 s without any SCADA reading, but only the UI reads it. | `orchestrator/src/opengrid/engine/gates.py:51-60`, `:123-132`, `orchestrator/src/opengrid/selector/gate.py:532-602`; at `afb26c2`: `orchestrator/src/opengrid/health/rules.py:139` (`is_firm_blocking_feed`), `:204-240` (SCADA silent), `orchestrator/src/opengrid/health/__init__.py:369`, `:377` |
| R2-1 (new) | **Fixed in R3 (`451a2a2`):** the guardian maps its `peak_budget_kws` input to the wire field `peak_power_budget_kws` (`orchestrator/src/opengrid/guardian/mqtt_io.py:46`, read at `:87`; test `orchestrator/tests/unit/guardian/test_service_flow.py:398`). At `6470cfa`: **G-31's peak path is dead.** The guardian reads the peak budget as `peak_budget_kws`; hubs send `peak_power_budget_kws`. Every above-continuous setpoint is vetoed. This is safe, and reported to the lead. | `orchestrator/src/opengrid/guardian/mqtt_io.py:36`, `orchestrator/src/opengrid/core/models/mqtt.py:44`, `core/limits.py:283-284` |
| R2-2 (new) | **Fixed in R3 (`451a2a2`):** the guardian takes each bank's signed `REAL_POWER_KW` (+ = import); without it an unsigned kVA reading is an interval and an unknown direction counts as export (`orchestrator/src/opengrid/guardian/flow_repo.py:8-17`, `:67`, `:73`; tests `orchestrator/tests/unit/guardian/test_flow_review_r3.py:190-305`). At `6470cfa`: **Reverse flow is not measured.** The guardian's aggregate flows sum unsigned SCADA apparent power as import, so a bank that is already exporting reads as importing. | `orchestrator/src/opengrid/guardian/flow_repo.py:40-48`, `integration-sims/src/ogsim/scada/aggregation.py:37-41` |
| R2-3 (new) | **Fixed in R3 (`451a2a2`):** `CHECK_AS_HOLD` measures every committed AS award. A held award needs kW × its product duration; a deployed one needs only the rest of its deployment window. It counts healthy hubs with a live SoC, net of what every other active reservation on the bank owes (`orchestrator/src/opengrid/invariants/checks.py:537` `_energy_owed_kwh`, `:551` `find_as_hold_violations`, `:578`; `invariants/queries.py:478` `fetch_as_hold_inputs`; tests `orchestrator/tests/unit/invariants/test_checks.py:193-226`). At `6470cfa`: **`CHECK_AS_HOLD` has a narrower scope than the spec.** It measures only active deployment windows, never a held, undeployed award. It requires the full product duration mid-deployment and does not net out other obligations on the same banks. Reported to the lead. | `orchestrator/src/opengrid/invariants/queries.py:460`, `:471`, `orchestrator/src/opengrid/invariants/checks.py:547` |
| R2-4 (new) | **Partly fixed in R3 (`451a2a2`):** og-api caps a deployment by the award's own product rule and refuses a second one while one is active (`orchestrator/src/opengrid/api/routers/dispatch.py:135-147`, `api/store.py:899-903`). Still open: the engine sizes AS energy holds by the longest product duration on the contract (`orchestrator/src/opengrid/engine/gateways.py:123`, `:309`), and the active-deployment check and the insert are not atomic. At `6470cfa`: **The AS deployment cap is per contract.** A deployment is capped by the longest product duration on the award's contract, not the award's own product. | `orchestrator/src/opengrid/api/store.py:729-740` |
| R2-5 (new) | **Still open at R3.** New at R3: `[guardian.flow] fail_closed_missing_topology`, false at `r3`, makes a feeder without an `og.feeder_limit` row veto any G-28 increase instead of taking the defaults. It covers feeders only (`orchestrator/src/opengrid/guardian/flow_repo.py:274-278`, `guardian/config.py:120`). At `6470cfa`: **Flow limits bind only where data exists.** No repo seed writes `og.service_transformer`, `og.feeder_limit`, `og.substation_limit`, the `og.hub` premise columns (migration 0029), or a HOME_BANK `og.asset` row. The allocator's F2/F3 caps have nothing to apply, and the guardian uses its static defaults. Server database contents were not checked. | `orchestrator/src/opengrid/guardian/config.py:106-116` |
| R2-6 (new) | **Fixed in R3 (`451a2a2`):** vetoed hubs are excluded for 3 cycles (`R-HUB-VETO-EXCLUDED`) and the bank is re-proposed without them (`orchestrator/src/opengrid/engine/veto.py`, `engine/__init__.py:907-928`; tests `orchestrator/tests/unit/engine/test_dispatch_wiring.py:823-890`). At `6470cfa`: **One item-level veto holds the whole bank.** Only a PASS verdict is signed, and neither the engine nor the allocator handles PARTLY_VETOED; K4's "re-solve without the vetoed hubs" is not implemented. So a single G-26, G-27, G-31 or G-33 item veto leaves the bank unsigned until its leases lapse. Reported to the lead. | `orchestrator/src/opengrid/guardian/service.py:1226-1234` |
| R2-7 (new) | **Fixed in R3 (`451a2a2`):** an instruction past its `expires_at` no longer binds (`orchestrator/src/opengrid/engine/gateways.py:646-663`; tests `test_dispatch_wiring.py:715-737`). At `6470cfa`: **og-engine keeps applying an expired utility (L2) instruction.** A lifted BLOCK or LIMIT keeps the bank cut, which is why a best-effort delivery does not return to its full commitment (D-17). The guardian honours expiry. Reported to the lead. | `orchestrator/src/opengrid/engine/gateways.py:518-527`, `orchestrator/src/opengrid/fleet/__init__.py:491-503` |
| R2-8 (new) | **Fixed in R3 (`451a2a2`):** G-33 passes 0 kW items (`orchestrator/src/opengrid/guardian/flow_checks.py:313-314`; tests `test_flow_review_r3.py:138-170`). At `6470cfa`: **G-33 vetoes the engine's own territory-block items.** The 0 kW explanation items for a territory-blocked obligation are vetoed, so with R2-6 the whole bank goes unsigned. Reported to the lead. | `orchestrator/src/opengrid/guardian/service.py:568-588` |
| R2-9 (new) | **Fixed in R3 (`451a2a2`):** the router is mounted (`orchestrator/src/opengrid/api/app.py:150`, `:175`). At `r3`, Profitability then returned 500 once a gate was recorded, because the breakdown's `plan_mode` text went through `float()`; `r3.1` fixed it (`62eab91`). At `6470cfa`: **The LP value-added API is not mounted.** `og.plan_value` (migration 0030) is written but nothing serves it. | `orchestrator/src/opengrid/api/routers/lp_value.py:91`, `orchestrator/src/opengrid/api/app.py:130-171` |

---

## Summary

| # | Item | Category | Reason | External dependency | Owner decision needed |
|---|---|---|---|---|---|
| BD-1 | Zone blocks for Austin Energy / CPS Energy (`LZ_AEN`, `LZ_CPS`) | dark | `enabled: false` in two sim config files | Utility territory polygons, tariffs, substation asset data | Yes |
| BD-2 | Allocator-level PQ hub-eligibility filter + continuous PQ monitor (WP-D) | dark (R2: filter wired; ladder/monitor gated by config) | not called anywhere in the allocator's real-time cycle | None (internal wiring) | No |
| BD-3a | DATA_CENTER / PIPELINE_AC closed-loop admission gate | dark | `[contracts.activation].data_center = false` | Closed-loop controller validation against real/simulated hardware | Yes |
| BD-3b | Customer-operator simulators, customer API, site ingest (decision D-11) | mostly **not built**, despite the decision log's label (see body); **R2: built, dark** | no module/package exists in this checkout; only an auth-role scaffold and ops placeholders do | Product scope decision | Yes |
| BD-4 | Ledger K13 `release()` / `reduce()` / `substitute_hub()` call path | dark | no production caller; the one gateway method it would use is independently documented as broken | None (internal wiring) | No |
| BD-5 | AS forward release (`R-AS-RELEASE` / `as_release_enabled`) | dark | feature flag, default off, and only half-wired to config even if flipped | An audited sign-off process (spec S7.4) | Yes |
| BD-6 | Asset-health drift sweep | dark | `assets.drift_enabled = false`, switched off after a false-positive incident | Base warranty/BMS drift data to retune thresholds | Yes |
| NB-1 | Real DNP3/ICCP SCADA integration | not built | — | Utility SCADA access (NDA, DNP3/ICCP/IEEE 2030.5 points) | Yes |
| NB-2 | ERCOT market submission (QSE registration, bids/offers) | not built | — | ERCOT QSE registration and credentials | Yes |
| NB-3 | Keycloak or another SSO identity provider | not built | — | IT/security decision, identity-provider selection | Yes |
| NB-4 | Kubernetes high availability | not built | the orchestrator runs on systemd; its single-node k3s design is a spec, not applied | Second host/hardware, ops investment | Yes |
| NB-5 | The AI agent | not built | — | Product scope definition | Yes |
| NB-6 | PJM | not built | UI has a reserved chart colour only | PJM market membership/agreement | Yes |
| NB-7 | Mobile app | not built | — | Product scope decision | Yes |
| NB-8 | Large loads (`LARGE_LOAD`) | not built | UI has a reserved chart colour only | Customer segment / site engineering data | Yes |
| NB-9 | K11 external anchoring | not built (R2: partly built, on-host and unsigned) | the local hash-chain journal is built; the off-node anchor is not | An anchoring service (time-stamp authority / write-once store) | Yes |
| NB-10 | K4 stagger (signed per-hub start jitter) | not built (R2: guardian vetoes synchronized steps; no jitter) | the fleet ramp cap is built; the jitter/desync half is not | None (engineering only) | Yes (safety-relevant) |
| NB-11 | Automated chaos tests (CI-scheduled, passing) | partially built | the runner/framework exists; most rows fail against a real system today | None (engineering + tracked product gaps) | No |
| NB-12 | Two-market direction pieces (territory, flow limits, substation assets, lexicographic solver) | not built (R2: mostly built) | design/scoping spec plus a standalone prototype only; no production code changed | Utility contract terms, GIS data, Base asset data (18 open questions) | Yes |
| OL-1 | Single host (`192.168.5.35`) | operational | decided permanent (D-16) | — | No — already decided |
| OL-2 | Scale test (10,000 hubs) | operational | plan written; R2: 2k passed on the local dev stack, 10k not yet run on the server | — | No |
| OL-3 | Simulator-only data sources (fleet telemetry, SCADA) | operational | ERCOT/EIA/NWS feeds are real; fleet and SCADA are simulated | Real hardware fleet, utility SCADA access | Yes |
| OL-4 | API identity trust (`X-Remote-User` / proxy secret, D-12) | operational | verified built and fail-closed | — | No |
| OL-5 | M1 delivery charge (D-19) settles at $0 | operational (R2: fixed with a grid-share proxy) | grid-charged kWh attribution not built; a constant 0 is used | — | No |
| DM-1 | Hub stop-release `issued_at` backstop is scope-wide | dormant | unreachable while the guardian releases a whole scope at once | — | No |
| DM-2 | Need-basis K13 check reads migration-0015 tables | dormant (R2: tables exist via 0026; data gated by config) | gated behind the absent `opengrid.site_ingest` package | Customer-services package + migration 0015 | No |

---

## 1. Built but dark

### BD-1. Zone blocks for Austin Energy / CPS Energy (`LZ_AEN`, `LZ_CPS`)

**What exists.** Commit `f5058af` ("R1.6: dual-unit homes 10 per bank (owner-approved), zone-block code (disabled),
dispatch 500 fix", 2026-09-26 13:12, confirmed an ancestor of `HEAD`) declared, but did not enable, an extra
load-zone block per regulated utility:

- `integration-sims/src/ogsim/common/config.py:45-65` — `ZoneBlockConfig` dataclass (`zone`, `banks`,
  `homes_per_bank`, `enabled: bool = False`), docstring: "Disabled by default... a disabled block reserves no
  hub/bank ids at all."
- `orchestrator/src/opengrid/fleet/seed.py:56-68` — the orchestrator's own mirror of the same dataclass (the two
  can't share code per BUILD.md's "share no code" rule); `:107` `zone_blocks: tuple[ZoneBlockConfig, ...] = ()` on
  the topology config; `:273-274` `for block in config.zone_blocks: if not block.enabled: continue`.
- `integration-sims/config/fleet.yaml:47-55` and `integration-sims/config/scada.yaml:17-25` — both declare the same
  two blocks (`LZ_AEN`/10 banks, `LZ_CPS`/10 banks, 50 homes/bank) with `enabled: false`, and both files' comments
  say a block must be turned on in **both** places together.
- `orchestrator/config/orchestrator.toml:76-80` — a comment noting that `[fleet].zones` and
  `[zone_territory]` in `tdsp_tariffs.toml` must also be updated once a block is enabled.

**Why dark.** Pure config: `enabled: false` in both sim config files. No code path removes or ignores a disabled
block; seeding a disabled block is a byte-for-byte no-op (verified by `seed.py:273-274` and the fleet/scada config
comments).

**What's needed to turn it on.** (1) Set `enabled: true` for the wanted block in **both**
`integration-sims/config/fleet.yaml` and `integration-sims/config/scada.yaml`; (2) add the zone to
`[fleet].zones` in `orchestrator/config/orchestrator.toml`; (3) add a `[zone_territory]` entry in
`orchestrator/config/tdsp_tariffs.toml`; (4) supply real territory polygon and tariff data (see NB-12 and OQ-5/OQ-17
in `09-optimizer-dispatcher-update.md`) — the two-market model that would actually price and constrain these banks
correctly is itself not built yet (NB-12), so enabling a zone block today would seed banks that the selector/allocator
still treat as an ordinary ERCOT-zone bank with no territory restriction.

### BD-2. Allocator-level PQ hub-eligibility filter and continuous PQ monitor (WP-D)

**What exists.** Two pure-logic modules built for wave 2 of the power-quality spec (`docs/orchestrator/07-delivery/
06-service-profiles-and-power-quality.md`, "Agent D — allocator," line 1267):

- `orchestrator/src/opengrid/allocator/pq_eligibility.py` — `evaluate_hub_eligibility()` (line 142) and
  `apply_eligibility()` (line 338): a hard filter (asset-state, ride-through class, k-sigma THD/voltage outlier,
  kVA-circle feasibility) plus S5.2-step-2 diversity/phase-balance weighting, meant to narrow the hub pool a
  PQ-sensitive obligation's **real-time water-fill** may draw from.
- `orchestrator/src/opengrid/allocator/pq_monitor.py` — `evaluate_obligation_pq()` (line 126) and
  `rank_hubs_by_deviation()` (line 228): the six-step corrective-action ladder (REBALANCE_PHASES, SUBSTITUTE_HUBS,
  RECALIBRATE hand-off, ADJUST_PF, EXCLUDE_HUB, ESCALATE_AT_RISK) for a **committed** obligation whose delivered
  power quality drifts.

Both modules' own docstrings say explicitly: "This file does not edit `allocator/cycle.py`, `allocator/
substitution.py` or `allocator/models.py` — see this package's README for the exact call-site wiring the live-path
agent applies" (`pq_eligibility.py:11-14`, `pq_monitor.py:34-36`).

**Why dark — no production caller.** `orchestrator/src/opengrid/allocator/README.md` (the package's own
call-site map) does not mention PQ at all. `git grep` for `apply_eligibility|evaluate_obligation_pq|
rank_hubs_by_deviation` across the whole repository returns only the two files above and their three unit-test
files (`tests/unit/allocator/test_pq_eligibility.py`, `test_pq_monitor.py`, `test_pq_performance.py`); a further
case-insensitive search for `pq` in `allocator/cycle.py` (the actual real-time orchestration function) returns zero
hits. Wiring is genuinely absent, not merely untested.

This is distinct from a **different**, already-live module with a similar name: `orchestrator/src/opengrid/engine/
pq_eligibility.py` computes a **selection-time eligible-kW cap** per service profile (a simpler, separate mechanism)
and *is* wired into production — `engine/__init__.py:44` imports it, `:885` calls `pq_eligibility.configure(...)` at
start-up, `:956` runs `pq_eligibility.refresh(pool)` on a periodic task, and `engine/gateways.py:52` /
`selector/gate.py` wire its `eligible_kw` into `gate.configure_pq_capacity(...)` so the selector never commits a
DATA_CENTER obligation beyond its PQ-eligible capacity (`tests/unit/engine/test_pq_eligibility_wiring.py` exercises
this live path). Only the allocator-level filter and the continuous monitor (this item) are unwired.

**What's needed to turn it on.** Call `pq_eligibility.apply_eligibility()` on the hub set before `water_fill`/
`realize_obligation` run inside `allocator/cycle.py`, and call `pq_monitor.evaluate_obligation_pq()` once per cycle
per committed PQ-sensitive obligation, feeding its `CorrectionAction`s to the trace store (K10, "traced before act")
and to `opengrid.assets.service.AssetHealthService`/guardian G-25 for the `RECALIBRATE` hand-off. Both modules are
already unit- and property-tested in isolation; this is a wiring task inside `allocator/cycle.py`, not new logic.

### BD-3. Decision D-11: "customer-operator simulators, customer API, site ingest and closed-loop controllers"

`docs/orchestrator/07-delivery/11-decision-log.md:24` records D-11 as: "Customer-operator simulators (`ogsim/
customer`) plus a customer API, site ingest and closed-loop controllers (**built dark** 2026-09-26)." Checking each
of the four pieces separately gives two different answers:

#### BD-3a. Closed-loop controllers — real, and dark

`[contracts.activation].data_center` gates DATA_CENTER/PIPELINE_AC contract admission until "the closed-loop
controllers are confirmed live":

- `orchestrator/config/orchestrator.toml:57-60` — `[contracts.activation]\ndata_center = false`, comment: "DATA_CENTER/
  PIPELINE_AC contract admission stays closed (default) until the closed-loop controllers are confirmed live for a
  deployment."
- `orchestrator/src/opengrid/contracts/admission.py:55-63` — `_ACTIVATION_GATED_VARIANTS = frozenset({"PIPELINE_AC"})`,
  with a comment that PIPELINE_AC "has no `og.contract.service_type` value of its own yet (unlike DATA_CENTER...)" —
  i.e. PIPELINE_AC is even less built out than DATA_CENTER, riding as an admission-gated *variant* of it.
  `:66-90` `_is_activation_gated()`; `:116-133` the `data_center_activation_enabled` parameter (default `False`);
  `:169-183` the gate check itself, raising `AdmissionError("R-ADMIT-REJECT")` with the message "DATA_CENTER/
  PIPELINE_AC admission is disabled until the closed-loop controllers are confirmed live (...; set
  `[contracts.activation].data_center = true`)".
- `orchestrator/src/opengrid/contracts/__init__.py:72` and `orchestrator/src/opengrid/engine/__init__.py:858` both
  carry the same flag/comment at the point the engine wires contracts admission.

**What's needed to turn it on.** Confirm the closed-loop control path for a real (or fully simulated end-to-end)
DATA_CENTER/PIPELINE_AC deployment, then set `[contracts.activation].data_center = true`. This is a real, working
feature-flag off-switch, unlike BD-3b below.

#### BD-3b. Customer-operator simulators, customer API, site ingest — not found in this checkout

Searched for (all case-insensitive, whole repository unless noted): `customer_api`, `ogsim/customer` (as a
directory), `customer_operator`, `site_ingest`, `SiteIngest`, `closed_loop`/`ClosedLoop` (outside the admission gate
above), and a `[api.customer_api]` config section.

**Found instead — scaffolding and ops placeholders only, no simulator/API/ingest code:**

- `orchestrator/src/opengrid/api/auth.py:42-47` — `Role` enum includes `CUSTOMER = "customer"`; `:60-78`
  `role_for_identity()` maps a customer only via an explicit `[api.roles.customer]` config entry, never a name
  fallback; `:119-124` `require_viewer()` explicitly refuses a `CUSTOMER` identity ("a customer can never read the
  operator console's fleet-wide data"). This is real, tested code (`tests/unit/api/test_auth.py`) — but it is
  authorization scaffolding for a future customer role, not a customer API.
- `deploy/apache/opengrid.conf:39-42` — a `<Location /og/api/customer/>` block requiring five `og-cust-*` htpasswd
  accounts, commented "customer API (ships dark; `[api.customer_api].enabled`)". No FastAPI router named `customer`
  exists under `orchestrator/src/opengrid/api/routers/` (that directory holds only `admin.py`, `billing.py`,
  `contracts.py`, `dispatch.py`, `fleet.py`, `health.py`, `markets.py`, `profitability.py`, `retention.py`,
  `safestop.py`, `scenario.py`), and `orchestrator/config/orchestrator.toml` (read in full) has no
  `[api.customer_api]` section at all — the flag named in the Apache comment does not exist in the config schema.
- `deploy/RUNBOOK.md:14` — "`og-sim-customer` is installed but not enabled until the customer services go live." No
  `og-sim-customer.service` unit exists under `deploy/systemd/` (only `og-sim-fleet.service`,
  `og-sim-scada.service`, `og-sim-market.service`, `og-sim-control.service` do); the claim of an installed-but-disabled
  unit describes something this repository does not contain (it may exist only as a manual, uncommitted change on
  `192.168.5.35` — **unverified**, this checkout cannot see server state).
- `interfaces/contracts/service_profile.schema.json:27`, `orchestrator/src/opengrid/core/models/pq.py:89`, and
  migration `orchestrator/migrations/0010_service_profile.sql:56` all list `"CUSTOMER_API"` as one enum value of
  `setpoint_source` — a schema slot a service profile's setpoint could someday be sourced from, not an
  implementation.
- No `integration-sims/src/ogsim/customer/` package, or any "customer" simulator code, exists under
  `integration-sims/src` (confirmed by `Glob`/`Grep`); the only two "customer" hits under `integration-sims/` are a
  PQ test docstring and a demo scenario's prose ("several customers are COMMITTED/DELIVERING"), both unrelated to a
  customer-facing simulator.

**Conclusion.** For three of D-11's four items (customer-operator simulators, a customer API, site ingest), this
checkout contains no implementation — only an auth-role placeholder and dangling references in ops docs/config
comments to a flag and a systemd unit that do not exist in the repository. The decision log's "(built dark
2026-09-26)" label does not match the code at commit `434d230` for these three items; it does match for the fourth
item (closed-loop controllers, BD-3a). This discrepancy is worth raising with the owner directly, since the decision
log is otherwise treated as authoritative for what has and hasn't shipped.

**What's needed to turn these on** (i.e. to build them for the first time): a customer-facing FastAPI router under
`orchestrator/src/opengrid/api/routers/` wired to the existing `Role.CUSTOMER` scaffolding; an `[api.customer_api]`
config section actually read by that router; a site-ingest endpoint/module (contract intake already has
`contracts/intake/` for operator-side contract types — a customer-facing equivalent does not exist); and, if a
customer-operator simulator is still wanted, a new `integration-sims/src/ogsim/customer/` package plus an
`og-sim-customer.service` unit under `deploy/systemd/` and its addition to `ogsim.target`.

### BD-4. Ledger K13 `release()` / `reduce()` / `substitute_hub()` call path

**What exists.** The commitment-lock check itself, and the ledger methods that enforce it, are fully built and
heavily tested:

- `orchestrator/src/opengrid/core/limits.py:149-167` — `check_commitment_lock()`, the single K13/G-19 formula
  (floor = `min(frozen_kw, prior_kw)`; a reduction below it needs an allowed reason code, or `R-AS-RELEASE` with
  `as_release_enabled`).
- `orchestrator/src/opengrid/ledger/__init__.py:33-44` — `ALLOWED_RELEASE_REASONS`; `:386-421` `release()`/
  `reduce()`/`_reduce_or_release()` (the write path that calls `check_commitment_lock`); `:438-492` `substitute()`
  (moves a committed reservation to a different bank under the `R-SUBSTITUTION` exemption); `:546-550` the
  module-level `release()` facade.
- The guardian keeps its **own, independent** copy of the same check: `guardian/checks.py:203-249` and
  `guardian/service.py:756` (`as_release_enabled=self.config.as_release_enabled`) — this is the K2/K13
  primary-plus-independent-check pattern working as designed, and it is exercised live (a batch that violates K13
  would still be vetoed by the guardian regardless of the gap below).
- Extensive tests exist for the ledger side alone: `tests/unit/ledger/test_ledger.py`, `test_ledger_properties.py`,
  `test_ts_04_01_stateful.py`, and the property test `tests/property/test_k13_commitment_lock.py`.

**Why dark — no production caller for the write path.** `git grep` across `orchestrator/src` for
`ledger\.release\(|ledger\.reduce\(|ledger_mod\.release\(|ledger_mod\.reduce\(|ledger\.substitute\(` finds **zero**
matches outside the ledger module's own facade definitions. The only ledger-lifecycle call `og-engine` actually
makes at start-up is `release_uncommitted()` (`engine/__init__.py:872`), a different, simpler cleanup path unrelated
to K13's release/reduce/substitute methods. Separately:

- `orchestrator/src/opengrid/allocator/cycle.py:261-268` documents the K13 exception reason codes
  (`R-COMMIT-LOCK-OVERRIDE-L0`/`L1`) for a cycle's shortfall, but only as metadata **attached to a shortfall
  record** — it never calls `ledger.reduce()`/`.release()` to actually shrink the underlying reservation.
- `orchestrator/src/opengrid/allocator/__init__.py:160-189` — the public `substitute_hub()` interface function
  (per `INTERFACES.md`) itself has no caller anywhere in `orchestrator/src` either (`git grep 'substitute_hub\('`
  finds only its own definition/internal delegate and the package README). Its body calls
  `ledger.record_substitution(...)` — a `LedgerGateway` protocol method, *not* `ReservationLedger.substitute()`
  above — and `docs/demo/NEEDS_FROM_OTHER_OWNERS.md` item 4 independently documents that this exact method
  (`EngineLedgerGateway.record_substitution`, `engine/gateways.py`) currently **raises**, because `og.grant` has no
  `reason_code` column.

**Net effect.** Nothing can violate K13 (the guardian's independent check still blocks it), but the "legitimate
exception" paths the spec describes — a mid-cycle reduction for cause, a hub substitution, a release on
fulfilment/settlement — are not reachable from the live engine/allocator loop in this codebase. Obligations
presumably reach `R-FULFILLED`/`R-SETTLED` through some other mechanism this search did not find; if none exists,
committed reservations may simply never be released once the ledger's own explicit lifecycle is bypassed. This
needs the owning engineer's confirmation — it is flagged here as a gap, not asserted as a live bug, since a
different, not-yet-found call site is possible (see this document's method note on static analysis).

**What's needed to turn it on.** Wire `allocator/cycle.py`'s L0/L1/L2 shortfall path to actually call
`opengrid.ledger.reduce()`; wire `contracts` lifecycle transitions to call `ledger.release()` with
`R-FULFILLED`/`R-SETTLED` on obligation completion; add the missing `og.grant.reason_code` column and fix
`EngineLedgerGateway.record_substitution`; then call `allocator.substitute_hub()` from wherever the live path
currently only detects a hub health loss.

### BD-5. AS forward release (`R-AS-RELEASE` / `as_release_enabled`)

**What exists.** A fifth, audited exception to the K13 commitment lock, fully specified and coded, off by default:

- `orchestrator/src/opengrid/core/reasons.py:18` — `R_AS_RELEASE = "R-AS-RELEASE"`.
- `orchestrator/src/opengrid/guardian/config.py:25` — `DEFAULT_AS_RELEASE_ENABLED = False`; `:67`
  `as_release_enabled: bool = DEFAULT_AS_RELEASE_ENABLED`; `:128` reads `guardian.as_release_enabled` from
  `orchestrator.toml` (absent from the checked-in `[guardian]` section, so it resolves to the code default,
  `False`).
- `orchestrator/src/opengrid/ledger/__init__.py:273,278` — `ReservationLedger.__init__`'s own
  `as_release_enabled: bool = False` keyword; `:388-389,411-412` `release()`/`_reduce_or_release()` pass it into
  `check_commitment_lock`.
- `orchestrator/src/opengrid/guardian/checks.py:203,208,249` and `guardian/service.py:756` — the guardian's
  independent copy, wired to the same config key.
- `orchestrator/src/opengrid/contracts/state_machine.py:9,20-21` — explicitly excludes `R-AS-RELEASE` from ever
  authorizing a `DELIVERING -> SHORTFALL` transition on its own.
- Both states are tested: `tests/unit/core/test_limits.py:112-186`, `tests/unit/guardian/test_service.py:392-399`,
  `tests/unit/guardian/test_config.py:15-35`, `tests/unit/ledger/test_ledger.py:107-122`.

**Why dark.** `as_release_enabled` defaults to `False` in both the guardian config and the ledger constructor, and
`orchestrator/config/orchestrator.toml`'s checked-in `[guardian]` section does not set it — so both copies of the
check refuse `R-AS-RELEASE` today. `02a-mvp-s-spec-engine.md:567-568` frames this as deliberate: "disabled by a
feature flag ... for all of MVP-S (never exercised in the demo; kept in the schema...)."

**A second, independent reason it would still be inert even if the config flag were flipped:** `og-engine`'s actual
construction of the ledger — `orchestrator/src/opengrid/engine/__init__.py:826-832`
(`ReservationLedger(PgLedgerBackend(pool), FleetCapabilityProvider(), grant_backend=PgGrantBackend(pool))`) — never
passes `as_release_enabled` at all, so it silently keeps the constructor default of `False` regardless of what
`orchestrator.toml`'s `[guardian].as_release_enabled` says. Only the **guardian's** independent copy reads that
config key (`guardian/config.py:128`); the **ledger's** copy, which is what would actually have to execute a
release, is not wired to any config today. This is on top of BD-4's finding that `ledger.release()` has no
production caller at all yet.

**What's needed to turn it on.** (1) Set `guardian.as_release_enabled = true` in `orchestrator.toml`'s `[guardian]`
section; (2) change `engine/__init__.py:827-831`'s `ReservationLedger(...)` construction to also read and pass
`as_release_enabled` from config; (3) build the caller that would invoke `ledger.release(reservation_id,
"R-AS-RELEASE")` in the first place (BD-4); (4) put in place the "audited" sign-off process the spec (S7.4)
requires around actually using this exception, since it lets an AS capacity hold be released early.

### BD-6. Asset-health drift sweep

**What exists.** A background job that watches fleet-wide inverter/asset drift and can move a hub to `WATCH` (a
guardian-signed recalibration) or `DEGRADED` (an open work order):

- `orchestrator/config/orchestrator.toml:143-148` — `[assets]\ndrift_enabled = false`, comment: "OFF 2026-09-26
  10:00: enabling it fleet-wide put 188 hubs in DEGRADED, 4 in QUARANTINED and opened 192 work orders within 10
  minutes (drift thresholds vs. the simulator's natural drift); re-enable only after tuning."
- `orchestrator/src/opengrid/settle/main.py:264` — `if bool(cfg.get("assets.drift_enabled", False)):` gates
  registration of the job.
- `orchestrator/src/opengrid/assets/README.md:88` — `jobs.append(("asset_drift", Cadence(...drift_interval_s...),
  asset_drift_job))`, the job that would run every `assets.drift_interval_s` (default 60 s) if enabled.

**Why dark.** A same-day incident: the drift thresholds were tuned against real BMS warranty behaviour, not the
simulator's own (different) drift model, so turning it on fleet-wide produced a mass false-positive event.

**What's needed to turn it on.** Retune the drift thresholds against either real Base warranty/BMS drift data or the
simulator's actual drift model (whichever the fleet is running against at the time), then re-enable
`assets.drift_enabled` — ideally after a bank-scoped canary rather than fleet-wide, given the incident.

---

## 2. Not built

Each item: what's specified (if anything), the external dependency, and what the orchestrator does today instead.

### NB-1. Real DNP3/ICCP SCADA integration

Not built. `05-integrations-guide.md`'s reserved-integrations table lists "Utility SCADA (DNP3/TLS first; ICCP,
IEEE 2030.5, IEC 104, OPC UA after) — Real bank loading and utility instructions — Later release."
`09-optimizer-dispatcher-update.md`'s data table (§3) marks feeder-head/substation SCADA source as "utility
DNP3/ICCP ... | Fallback: none — stale is a veto of increases," i.e. even the design assumes no real utility SCADA
yet. **External dependency:** utility SCADA access under NDA (transformer-to-meter mapping, feeder/substation
points). **Today instead:** `ogsim.scada` simulates bank-level apparent power (kVA) over the same MQTT topic
(`og/v1/scada/<bank>`) the real utility SCADA would use, per `05-integrations-guide.md`'s topic table row "SCADA →
orchestrator ... Publisher: Utility SCADA (simulated in MVP-S)."

### NB-2. ERCOT market submission (QSE registration, bids and offers)

Not built. `05-integrations-guide.md`'s reserved table: "ERCOT market submission (QSE) — Real DAM/RTM offers and COP
instead of the simulated QSE — Later release." `01-saturday-delivery-plan.md`'s explicit "not built by Saturday"
list names it too. Searched `orchestrator/src` for `QSE`: the only hit is a comment in `feeds/ercot.py` (about
reading ERCOT public data, not submitting anything as a QSE). **External dependency:** ERCOT QSE registration,
credentials and COP submission rights. **Today instead:** `ogsim.market` simulates the ERCOT side entirely (prices,
AS awards) and the orchestrator only ever reads ERCOT's public data feeds (NP6-905-CD etc.) — it never submits a
bid, offer or Current Operating Plan anywhere in this codebase.

### NB-3. Keycloak or another SSO identity provider

Not built. `01-saturday-delivery-plan.md`'s "not built by Saturday" list names "Keycloak/OPA" explicitly. Searched
`orchestrator/src` for `Keycloak|OIDC|OAuth2|OAuth |SSO\b`: zero matches. **External dependency:** an org-level
identity-provider and security-posture decision. **Today instead:** Apache terminates HTTP Basic Auth
(`AuthUserFile`/`htpasswd`) and forwards the authenticated username via `X-Remote-User`, trusted by `og-api` only
when a shared proxy secret (`X-OG-Proxy-Auth`) is also present (`orchestrator/src/opengrid/api/auth.py` — see OL-4).
Two static roles (`operator`, `viewer`) plus the not-yet-used `customer` role (BD-3b) are the entire authorization
model; there is no login flow, token issuance, MFA or federation of any kind.

### NB-4. Kubernetes high availability

Not built, and what's specified is not high availability either.
- `docs/orchestrator/02-architecture/06-platform-and-operations.md` contains an extensive **single-node** k3s design
  for the orchestrator (`## 1. Single-node k3s on 192.168.5.35`, line 96).
- Its install-parameters block is headed "`# /etc/rancher/k3s/config.yaml — SPEC for review, not applied`"
  (line 117). The orchestrator is not deployed on Kubernetes: it runs as the systemd units in `deploy/systemd/`.
- The owner reports that Kubernetes is present on the base server for other purposes. The orchestrator does not use
  it, and any change there needs care not to disturb it.
- Even if applied, the design is one node (`ADR-500`: "k3s single node with the SQLite (kine) datastore," line 77).
  That is a packaging and operations improvement over systemd, not multi-node failover. True
multi-node HA is not designed at all; `01-saturday-delivery-plan.md` lists "Kubernetes/multi-node HA" as explicitly
out of scope. **External dependency:** a second host (or more) and the ops investment to run and test a cluster.
**Today instead:** seven systemd units under `opengrid.target`/`ogsim.target` on one host, `Restart=always`, no
automatic failover to a second node (see OL-1).

### NB-5. The AI agent

Not built. `01-saturday-delivery-plan.md`'s "not built by Saturday" list names "the AI agent." The only related hits
in the docs are aspirational/threat-model framing — `docs/orchestrator/06-reviews/03-red-team-report.md:16` lists
"propose-only AI" among the system's control ideas, and `02-architecture/05-failure-modes-and-recovery.md:36`
mentions an "AI budget" value among failure-mode variables — neither backed by any code. Searched `orchestrator/src`
and `integration-sims/src`: no agent/LLM/ML module of any kind; the selector and allocator are conventional LP/MILP
(HiGHS) and rule-based logic, not learned or generative. **External dependency:** a product-scope decision on what
"the AI agent" would actually do (the spec never gets more concrete than "propose-only"). **Today instead:** nothing
proposes anything autonomously; every commitment is either the LP selector's output or an operator's manual command,
both guardian-signed.

### NB-6. PJM

Not built. `01-saturday-delivery-plan.md` lists `PJM_CAPACITY` among the service types explicitly out of scope.
Searched the whole repository for `PJM`: the only hits are a reserved ECharts colour token
(`orchestrator/src/opengrid/ui/static/og.css:37` `--series-pjm-capacity: #008300;`, referenced by `og.js:89` and
`ui/DESIGN.md`). No PJM feed, contract service type, settlement logic or admission path exists anywhere.
**External dependency:** PJM market membership/registration, a second-ISO business decision (Base currently
operates in ERCOT only). **Today instead:** nothing — nothing outside ERCOT (plus the not-yet-enabled Austin/CPS
regulated zones, BD-1/NB-12) is even planned for near-term delivery.

### NB-7. A mobile app

Not built. `01-saturday-delivery-plan.md` lists `MOBILE_*` service types as out of scope. Searched the entire
repository (docs and code) for `MOBILE_`: **zero** matches anywhere, not even a UI placeholder (contrast with
NB-6/NB-8, which at least got a reserved chart colour) — this is the least-started item on the whole "not built"
list. **External dependency:** a product-scope decision on what a mobile app would even do (operator app? customer
self-service? field-technician tool?). **Today instead:** the operator/viewer UI is the FastAPI + HTMX web control
room described in `01-saturday-delivery-plan.md` §4, browser-only, no responsive/mobile-specific design system beyond
ordinary CSS.

### NB-8. Large loads (`LARGE_LOAD`)

Not built as a service type. `01-saturday-delivery-plan.md` lists `LARGE_LOAD` among the out-of-scope service types.
Searched `orchestrator/src` for `LARGE_LOAD`: the only hits are a reserved ECharts colour token
(`ui/static/og.css:35` `--series-large-load: #d55181;`, referenced by `og.js:88` and `ui/DESIGN.md:29,242`) — no
contract `service_type` value, admission rule, settlement logic or asset model exists. **External dependency:**
customer-segment definition and site engineering data for a large industrial/commercial load class (distinct from
DATA_CENTER, which does exist as a real profile). **Today instead:** nothing — the five MVP-S service types
(`HOME`, `ERCOT_ENERGY`, `ERCOT_AS`, `DIST_DEFERRAL`, `PARTNER_CAPACITY`) plus the MVP-S+ `DATA_CENTER`/`PIPELINE_AC`
pair (BD-3a) are the entire built set.

### NB-9. The K11 trace journal and external anchoring

**Partially built — the distinction matters.** `00-invariants.md:18` states K11's own bar plainly: "Every event is
traced in a per-stream SHA-256 hash chain... Mitigation: **Local journal; alert if unanchored**." The **local**
half is real: `orchestrator/src/opengrid/trace/store.py:131-135` implements `async def verify(self, stream_id, ...)`
over `prev_hash`-chained records, and this is exercised by property tests ("verify after random prune"). The
**external anchoring** half — writing the journal's head to something outside this Postgres instance (an RFC 3161
timestamp authority, a write-once/object-locked bucket, etc., as extensively specified in
`02-architecture/06-platform-and-operations.md`'s `ALR-547`/`RP-63`/`DASH-512` and `02a-mvp-s-spec-engine.md:490`'s
`anchor_ref` column comment "external anchor id (file hash / RFC3161 token), **MVP-S: local WORM file**") — is not
built. Searched `orchestrator/src` for `anchor` (case-insensitive): the only two hits are unrelated uses of the
English word ("anchor to a drive" in `fleet/seed.py:135`); nothing posts a hash anywhere off-host. **External
dependency:** selection and cost of an anchoring service (a timestamp authority, or a cloud object-lock bucket, or
similar) — none is named or priced anywhere in the docs, so none is assumed here. **Today instead:** the hash chain
lives only in this Postgres instance; a full compromise or loss of that database would remove the only copy of the
audit trail (the spec's own "accepted residual risk" framing, capped nominally at a design target it does not appear
this codebase currently measures or alerts on — the `og_audit_anchor_age_seconds`/`ALR-547` metrics in
`06-platform-and-operations.md` were not found under `orchestrator/src/opengrid/health/` or `metrics.py` in this
search).

### NB-10. The K4 stagger

Not built, though extensively specified. `00-invariants.md:11` folds it directly into K4: "There are no synchronized
fleet steps (**stagger**, fleet ramp cap)..." The **fleet ramp cap** half of that sentence is real —
`orchestrator/src/opengrid/guardian/checks.py:121` implements "K4: fleet-wide ramp cap for synchronized steps
(discretionary vs. non-firm)." The **stagger** half (a signed per-hub start-time jitter, `nbf`, so a fleet-wide event
doesn't switch every hub in the same instant — `02a-mvp-s-spec-engine.md:962` "G-05 ... synchronized step ≤
discretionary cap ÷ 30 per 2-s tick; stagger jitter `U_i T_s`, `T_s=30 s`"; heavily cross-referenced in the security
architecture and threat-model docs as `CTL-031`, e.g. `03-security/01-threat-model.md:879` "Synchronized swing from
aligned schedules ... Signed staggering and step limits (CTL-031)") has no matching code anywhere. Searched
`orchestrator/src` and `integration-sims/src` for `stagger|nbf|jitter|randomiz` (case-insensitive): the only hits
outside documentation are an unrelated UI CSS animation stagger (`ui/DESIGN.md:356`, `.impeccable/design.json:34`,
purely a front-end fade-in effect) and an unrelated docstring in `ogsim/control/random_config.py:76` about a
scenario-parameter range. **External dependency:** none — this is pure engineering completion of an
already-specified control. **Today instead:** the fleet ramp cap still bounds how fast the *aggregate* fleet can
move, but nothing today desynchronizes individual hub start times within that cap, which is the specific grid risk
(`TH-006`, "synchronized swing from aligned schedules") the stagger exists to cut.

### NB-11. Automated chaos tests

**Partially built — the framework is real; passing runs against a live system are not.** A genuine, runnable chaos
harness exists at `tests-e2e/chaos/`: `runner.py` (CLI: snapshot → kill → poll health → restart → poll → evaluate →
Markdown report), `backends.py` (`SystemdController`/`DockerComposeController`/`DryRunController`),
`expectations.py` (the expected-behaviour table as data), and it runs today in dry-run mode
(`tests-e2e/chaos/README.md:13-18`: "It proves the tooling; it proves nothing about the product.") against a
scripted stub. Running it for real (against the dev Docker stack or the server's systemd units) is documented,
in the harness's own `NEEDS_FROM_OTHER_OWNERS.md`, as failing most rows today for product reasons, not test bugs —
as of its last recorded status ("Status after main @ c5eda88"), still-open gaps include `degraded_modes` missing
from `GET /og/api/health`, `processes[x].status` never flipping to `down`, `og-api`/the simulators writing no
heartbeat, and `og-engine` serving no `/metrics`. There is no CI job or schedule found anywhere in this repository
that runs the chaos suite automatically; `tests-e2e/chaos/README.md`'s three invocation modes (dry-run, dev Docker,
production systemd) are all manual, and the systemd mode is explicitly "the one e2e script that runs as root... over
the deploy role's SSH key," run by the lead. **External dependency:** none — the remaining gaps are the same health/
metrics engineering work items already tracked in that file. **Today instead:** the lead manually invokes
`runner.py --backend systemd` against the single production host when a chaos check is wanted; there is no
scheduled or CI-gated chaos run, and most rows against the real system do not yet pass end-to-end.

### NB-12. Two-market direction pieces not yet in code

Not built — this is the biggest single item. `docs/orchestrator/07-delivery/09-optimizer-dispatcher-update.md`
states its own status plainly: "**design and scoping spec for owner review**. No production code is changed by this
document. A standalone prototype backs the formulation: `prototypes/two_market_lp.py`." It documents eleven
already-found gaps in the code as built (`G1`–`G11`, e.g. "every bank is priced at the LZ_WEST price"
regardless of its real zone, `selector/gate.py:257-273`; "every bank is eligible for every obligation, so there is
no locality or territory," `selector/gate.py:299,337`) and defines eleven work packages (`WP-2M-01`…`WP-2M-11`,
§7) — territory/`K15`, substation battery assets, the AS energy hold (`C3′`; not a guardian check), the lexicographic
regulated-then-free objective, seven families of maximum-discharge-flow limits (`F1`–`F7`, most rows of its own
"not enforced anywhere" table, §1.9), and the `$/kW-in vs $/kW-out` economic reporting — none of which exist in
`orchestrator/src` yet (the spec's own §0.2 findings table is the authoritative "what's built vs not" breakdown
inside this design; this document does not re-derive it). It also lists 18 open questions for Base/the utilities
(`OQ-1`…`OQ-18`) that block finishing the design itself, e.g. `OQ-5` — "May REG-territory assets sell into ERCOT?" —
whose answer swings the prototype's payback estimate for an Austin Energy home-bank between roughly 6 and 3 years.
**External dependency:** utility contract terms (Austin Energy and/or CPS Energy), service-territory GIS data,
substation asset specifications from Base, and the answers to `OQ-1`–`OQ-18`. **Today instead:** the system runs a
single ERCOT-only model with no territory concept, no substation-sited assets, and per-bank zone pricing that is
itself bugged (`G1`/`G2` above) — the regulated-utility business case is not represented in the running system at
all yet, only in this design document and its offline prototype.

---

## 3. Known operational limits

### OL-1. Single host

The base server (`192.168.5.35`, `basepower`) is the **permanent** production host by owner decision: `11-decision-
log.md:19`, D-16, "The base server (192.168.5.35) is the permanent host." All seven `og-*` processes plus Postgres
and Mosquitto run as systemd units on this one machine (`deploy/RUNBOOK.md`'s unit table); there is no second node,
standby, or automatic failover (see NB-4). A host-level outage takes down the whole system; `deploy/RUNBOOK.md`'s
restart guidance ("Prefer the minutes right after a Postgres `checkpoint complete` line... disk is quietest") is the
closest thing to a maintenance window this system has.

### OL-2. Scale-test status

`docs/orchestrator/07-delivery/12-scale-test-plan.md:3` states its own status in the first line: "**plan only — no
load test has been run yet.**" It is a fully worked-out plan (isolation rules, an isolated workspace's own Postgres
database and MQTT topic root, what to measure — allocator cycle p99, guardian verdict time, telemetry write rate,
disk I/O, memory — and explicit pass criteria, e.g. "Allocator cycle p99 < 500 ms at 10k"), but as of this checkout
it has never been executed: no 10k-hub run, no captured metrics, no pass/fail result exists anywhere in this
repository. The plan's own scope note (§1) also flags that it is independent of, and has not been combined with, the
Austin/CPS zone-block work (BD-1).

### OL-3. Simulator-only data sources

Not every "live" data source is equally real. `05-integrations-guide.md:12` states "Four integrations are live in
MVP-S": ERCOT, EIA, NWS and the fleet's MQTT device interface are genuinely real protocol integrations (real HTTPS
calls to ERCOT's Public API and NWS, a real API key against EIA) — but the *fleet* and *SCADA* endpoints of that
MQTT interface are simulated, not real hardware or a real utility: `05-integrations-guide.md`'s topic table lists
`og/v1/scada/<bank>`'s publisher as "Utility SCADA (**simulated in MVP-S**)," and every hub publishing telemetry on
`og/v1/tel/...` is one of `ogsim.fleet`'s 2,000 (design target 10,000, OL-2) simulated hubs, not a Base Power
battery in the field. `docs/team/NOTICES.md`'s 2026-09-25 status table shows this plainly in practice too — feed
checks (A1) pass against real ERCOT data while fleet checks (A2) were failing at check time because simulated
telemetry was reconnecting after a restart. **External dependency:** a real deployed hardware fleet and a real
utility SCADA integration (NB-1) before any of the numbers this system produces reflect actual batteries or actual
grid conditions.

### OL-4. API identity trust (`X-Remote-User` / proxy secret, D-12)

`11-decision-log.md:23`, D-12, records the requirement: "the API trusts identity only with the Apache proxy secret."
Checked `orchestrator/src/opengrid/api/auth.py` directly: this is **built**, not merely planned. `bind_host` is
loopback-only, but the module's own docstring (lines 1-9) is explicit that loopback is not a trust boundary on a
shared host ("the simulators, workspace test runs and any other local process can reach the port and set
`X-Remote-User` themselves"). `proxy_authenticated()` (`:81-91`) accepts the identity header only when the request
also carries `X-OG-Proxy-Auth` equal to the `OG_API_PROXY_SECRET` environment value, compared with
`secrets.compare_digest` (constant-time); an **unset or empty secret authenticates nothing** and is logged as an
error (`:84-89`) — fail-closed by construction, not merely by convention. `verified_remote_user()` (`:94-100`) and
`current_identity()` (`:103-116`) are the single choke point every non-health endpoint depends on: no header is
401, an unverified header is 401, and an unmapped identity is 403. The one carve-out, `/og/api/health`
(`require_loopback_health_probe`, `:134-141`), never reads `X-Remote-User` at all and instead gates purely on the
TCP connection being loopback — used only by `deploy/scripts/deploy.sh`'s own liveness probe, which never goes
through Apache. **Operational limit, not a gap:** this mechanism's safety depends entirely on
`OG_API_PROXY_SECRET` being set correctly on both the Apache config (`deploy/apache/opengrid.conf:26-27`) and the
`og-api` process's environment, and on nothing else on the host ever being handed that secret — there is no code
issue here, only an ordinary deployment-hygiene dependency.

### OL-5. The M1 delivery charge settles at $0

- D-19 says "assume the FULL TDSP charge on grid charging". The tariff side is built:
  `orchestrator/src/opengrid/settle/tariffs.py` and `orchestrator/config/tdsp_tariffs.toml`.
- The grid-charged kWh the charge multiplies is a constant 0 until per-obligation charging attribution exists
  (`orchestrator/src/opengrid/settle/pg_backend.py:296`, `:669-679`).
- So every settled interval carries a $0 M1 charge, and margins in ERCOT competitive zones are overstated by the
  full delivery charge.
- The code discloses this and logs it on every settlement. The lead reports a fix in progress for R3 (2026-09-26).

## 4. Dormant edge cases

These code paths are correct today only because another path never exercises them. None is reachable in production
at `434d230`; each becomes live when the named change lands.

### DM-1. The hub's stop-release backstop compares scope-wide, not per stop

- `StopRegistry.release_stop` (`integration-sims/src/ogsim/fleet/stop.py:126-136`) ignores a RELEASE whose
  `issued_at` is older than the newest ENGAGE seen for the whole scope (`newest_engage_at`, `:114`, `:123`). It does
  not compare against the ENGAGE of the specific `stop_id` being released.
- So a legitimate release of an old, still-outstanding stop would be refused if an unrelated ENGAGE landed on the
  same scope after the release was approved but before it was delivered.
- **Unreachable today:** the guardian always releases every outstanding stop on a scope at once, stamped at approval
  time (`orchestrator/src/opengrid/guardian/stop_release.py:98-121`). A release is never older than any engage it
  covers.
- **Becomes live if** releases are ever issued per stop. The fix then: compare against the target stop's own ENGAGE
  `issued_at`.

### DM-2. The need-basis K13 check reads tables that migration 0015 creates

- `invariants.queries.fetch_measured_need_sample` queries `og.customer_site_meter_reading` and
  `og.corridor_current_reading` (`orchestrator/src/opengrid/invariants/queries.py:357-368`).
- Those tables come from migration 0015, which is reserved for the customer-services work and is not on main
  (`orchestrator/migrations/0017_invariant_violation_dedupe_and_trace_watermark.sql:2`).
- **Unreachable today:** the query runs only after `opengrid.site_ingest` imports successfully (`:344-350`), and that
  package isn't on main either.
- **Becomes live** when customer services land. Migration 0015 must land with `opengrid.site_ingest` or before it.
  The lead routed this to REVIEW-FIX on 2026-09-26.

---

*Verified against main `434d230` on 2026-09-26. Every file:line reference in this document was checked against the
tracked files at that commit.*
