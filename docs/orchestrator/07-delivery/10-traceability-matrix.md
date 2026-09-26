# OpenGrid Orchestrator — MVP-S+ Traceability Matrix

As of: branch `wp/docs-lane` on main `6470cfa` (R2), 2026-09-26. Re-verified at R2, with R2 line numbers: ES03-S06,
ES05-S10, ES06-S09, ES07-S02, ES19-S02…S05, K4 (and K4 extended), K11, K15, G-26…G-33 and `CHECK_AS_HOLD`; ES07-S02
and ES07-S05 also at R2 hotfix v3 (`afb26c2`, refs marked as such). Every
other row, and every `file:line` in it, is as verified at `434d230` (the first version of this matrix); lines may
have moved since.

Sources: `03-mvp-s-epics-stories.md` (67 stories, ES01–ES19), `00-invariants.md` (K1–K15, incl. the final G-26…G-33
guardian numbering in "Flow limits and territory"), `11-decision-log.md` (D-4…D-27), `04-mvp-s-test-plan.md`
(TS-xx-yy), `13-known-limitations.md`, the code (`orchestrator/src/opengrid/`, `integration-sims/src/ogsim/`) and
the tests (`orchestrator/tests/{unit,property,integration}`, `integration-sims/tests`,
`tests-e2e/{functional,ui,chaos,perf}`).

**Path prefixes** (used throughout): `o/` = `orchestrator/src/opengrid/` · `t/` = `orchestrator/tests/` · `sim/` =
`integration-sims/src/ogsim/` · `simt/` = `integration-sims/tests/` · `e2e/` = `tests-e2e/`.

**Method note.** Every `file:line` and test name below was confirmed against this checkout by `Read`/`Grep` (or
`git grep` via PowerShell) — either directly, or by parallel research passes whose file:line/test-name claims were
then spot-checked directly against the repository before being written here. Nothing is guessed. Rows marked
**pending verification** are cases where neither pass produced a directly-confirmed citation before this file was
written; they are flagged rather than filled with a guess.

---

## 1. Summary

**Stories (67 total, ES01–ES19):**

| Status | Count |
|---|---|
| built | 53 |
| in build (partial) | 12 |
| built dark (code exists, no live production path) | 2 |
| not built | 0 |
| **Total** | **67** |

By epic: ES01 5 built/1 in-build · ES02 3 built/2 in-build · ES03 5 built/1 built-dark · ES04 5 built/1 in-build ·
ES05 9 built/1 in-build · ES06 9 built · ES07 2 built/3 in-build · ES08 4 built/1 built-dark · ES09 4 built · ES10 5
built/1 in-build · ES19 2 built/3 in-build. At `434d230` the counts were 51 built, 10 in build, 1 built dark and 5
not built (ES03-S06, ES05-S10, ES06-S09, ES19-S03, ES19-S04); R2 moved all five.

**Invariants (K1–K15):**

| Status | Count | Which |
|---|---|---|
| built (primary + independent guardian check + property test) | 13 | K1, K2, K3, K5, K6, K7, K8, K9, K10, K11 (local chain; external anchoring partly built), K12, K13, K14 |
| built at base, extended families partial | 1 | K4 (R2: guardian G-02 changed and G-26…G-33 built; dispatcher derating built, other families built without data or not built; selector none) |
| partly built | 1 | K15 (R2: selector, allocator, guardian and settle built; contract admission and most checker counters not built) |
| **Total** | **15** | |

**Decisions (D-4…D-27, 24 total):** 19 fully reflected in every doc that names them, with no contradiction found;
**5 had an open conflict or gap at `434d230`** (D-6, D-7, D-11, D-17, D-18 — see §4.2). D-6 is fixed with this matrix.
The test-plan (`04`) halves of D-7, D-11, D-17 and D-18 are fixed on `wp/TP-UPDATE-test-plan` (`30d7f78`, and
`778370a` for D-7), queued for R3.

**Verification status:** all 67 story rows and all 15 invariant rows are independently confirmed against the
repository. All 24 decisions are checked against the 7 named docs (00, 01, 02a, 02b, 03, 04, 06). No row in this
file is marked "pending verification" — everywhere the evidence was incomplete, the row instead states exactly
what was and wasn't found (see the many "gap/note" cells below, e.g. ES07-S02, ES08-S04, ES09-S01).

---

## 2. Stories → code → test → status

One row per story in `03-mvp-s-epics-stories.md`, all 67. "Decision" repeats a story's own `Decision: D-x` line
where it has one (cross-reference to §4).

### ES01 — Platform & data

| Story | Title | Status | Code | Test | Gap / note |
|---|---|---|---|---|---|
| ES01-S01 | PG schema & migrations | built | `o/platform/db.py:75` (`migrate_sync`); `orchestrator/migrations/0001…0023*.sql` | `t/integration/platform/test_es01_s01_migrate_idempotency.py::test_migrate_sync_is_idempotent_on_a_fresh_workspace_db` | Table ownership is asserted in module READMEs/spec, not DB-level `GRANT`s — no automated cross-module-writer check. This integration test is DB-only (skips without live Postgres) and is **not** run by `.gitea/workflows/check.yml` (CI runs `tests/unit` + `tests/property` only). |
| ES01-S02 | Shared Pydantic contracts package | in build | `o/core/models/__init__.py:7-9` (re-exports `engine,mqtt,platform,pq`, imported by 66+ files) | `t/unit/core/test_ts_01_02_pydantic_fuzz.py::test_extra_field_raises_validation_error` | Package itself is real and universally used, but no "stale contract version fails CI" mechanism exists anywhere (no version constant, no CI step) — one of the story's own acceptance bullets is unmet. |
| ES01-S03 | Compose/native deploy behind Apache TLS | built | `deploy/apache/opengrid.conf:6,19-22`; `BUILD.md:17` | `e2e/smoke.py::check_ui` | Citation (pre-existing, Decision D-16) confirmed exact. |
| ES01-S04 | CI: unit + property tests on every push | built | `.gitea/workflows/check.yml:46-55` (lint/type/dupcheck/unit), `:67-72` (property, gated on `tests/property` existing) | `t/property/test_k13_commitment_lock.py` (Hypothesis `@given`, the K13 guard the story names) | CI does not run `tests/integration`. |
| ES01-S05 | Nightly backup and restore runbook | built | `deploy/scripts/backup.sh:27` (`pg_dump`); `deploy/cron/opengrid:7` (03:00 daily) | `t/integration/platform/test_es01_s05_restore_and_verify.py::test_restored_database_still_verifies_the_trace_chain` | Same CI-gating caveat as ES01-S01 (DB-only, not run in `.gitea`). `deploy/RUNBOOK.md:76-101` documents the restore procedure and cites this test directly. |
| ES01-S06 | Per-workspace MQTT isolation | built | Decision D-13. `dev/scripts/gen_mosquitto_acl.py`; `deploy/mosquitto/provision_ws_users.{py,sh}`; `o/platform/mqtt.py:112-149`; `tools/ws_env.sh:18` | `t/unit/tools/test_ws_env.py::test_workspace_gets_its_own_user_and_no_production_mqtt_credential`; `simt/test_workspace_config.py::test_a_workspace_without_its_own_mqtt_user_is_refused` | Citation confirmed exact. |

### ES02 — Live feeds & forecast

| Story | Title | Status | Code | Test | Gap / note |
|---|---|---|---|---|---|
| ES02-S01 | ERCOT prices/load/wind-solar/AS on schedule | built | `o/feeds/scheduler.py:90` (`_poll_ercot_product`) | `t/unit/feeds/test_scheduler.py::test_successful_poll_writes_obs_and_status`; `t/unit/feeds/test_ercot.py::test_both_keys_failing_raises` | Errors caught (`FeedHttpError`/`FeedDataError`/`ErcotAuthError`) and logged, never propagated. |
| ES02-S02 | Shared 30 req/min ERCOT budget | built | `o/feeds/token_bucket.py:35` (`acquire`, waits not errors); `o/feeds/__init__.py:108-109` (one shared bucket for all 5 products) | `t/unit/feeds/test_token_bucket.py::test_acquire_blocks_until_refill` | Configured default is 24 req/min (`o/feeds/__init__.py:108`, `orchestrator/config/orchestrator.toml:30`), under the story's 30 req/min ceiling, so the acceptance bullet ("never exceeds 30 req/min") holds with 20% headroom. Only the title's number differs; not a defect. |
| ES02-S03 | EIA fallback + NWS weather | in build | `o/feeds/scheduler.py:145` (`_poll_eia_fallback`), `:176` (`_poll_nws`); `o/feeds/eia.py:28`; `o/feeds/nws.py:57` | `t/unit/feeds/test_eia.py::test_hourly_demand_success`; `t/unit/feeds/test_nws.py::test_hourly_forecast_success_sets_if_modified_since_next_time`; `t/unit/feeds/test_scheduler.py::test_ercot_breaker_opens_and_engages_eia_fallback_for_load` | EIA fallback (source-labelled) and NWS hourly forecast are built and tested. NWS **watches/warnings** ingestion (the story's other half) has zero hits anywhere in the repo. |
| ES02-S04 | Freshness badges + circuit breaker | built | `o/feeds/breaker.py:25` (`CircuitBreaker`, 60s→900s backoff, half-open probe); `o/ui/routes/markets.py:205`; `o/health/rules.py:112` (`evaluate_feed_alert`) | `t/unit/feeds/test_breaker.py::test_opens_after_consecutive_failure_threshold`; `t/unit/ui/test_static_staleness.py::test_screen_includes_a_staleness_indicator` | A static test enforces every UI screen includes the staleness badge. |
| ES02-S05 | Quantile-persistence forecast (P10/P50/P90) | in build | Decision D-24 (partial). `o/forecast/quantiles.py:137` (`compute_slot_quantiles`) | `t/unit/forecast/test_quantiles.py::test_ts_02_06_sample_quantiles_monotonic`; `t/unit/forecast/test_service.py::test_compute_and_persist_widens_band_when_series_stale` | Base P10/P50/P90 + stale-widening is built and tested (overrides the doc's blanket "not built" — 2 of 3 acceptance bullets pass). D-24's solar-driven intraday shape is genuinely not built: no `solar`/`duck`/`PVGR`/`irradiance` hit anywhere in `o/forecast/` outside its own README's "gap" notes. |

### ES03 — Fleet twin & test harness

| Story | Title | Status | Code | Test | Gap / note |
|---|---|---|---|---|---|
| ES03-S01 | Hub/bank digital twin | built | Decision D-5. `o/fleet/__init__.py:469` (`capability`), `:256-264` (`hub_health`), `:65-93` (`HubCapabilitySnapshot`); `o/fleet/seed.py:41-43` (nameplate constants) | `t/unit/fleet/test_twin.py`; `simt/test_fleet_dual_unit.py::test_dual_unit_hub_power_and_reserve_match_config:478` | — |
| ES03-S02 | Bank/zone topology ("behind asset X") | built | Decision D-9. `o/fleet/seed.py:191-208` (`_is_dual_unit`), `:211-284` (`build_topology`); `dev/seed/rebalance_dual_units.sql` | `simt/test_fleet_dual_unit.py::test_dual_unit_homes_spread_10_per_bank_not_clustered_in_8_banks:444`, `::test_every_bank_has_exactly_one_zone:464`; `t/unit/fleet/test_es03_s02_hub_capabilities_isolation.py` (self-tagged "ES03-S02" in its own docstring) | — |
| ES03-S03 | Sim harness: 2,000 hubs, physics/lease/sig-verify | built | `sim/fleet/physics.py:25-38,77-98`; `sim/fleet/lease.py:17-52` (`LeaseState`, `HoldTracker`); `sim/fleet/commands.py:63-98` (`evaluate_batch`, K6); `sim/common/crypto.py:56-74` (Ed25519) | `simt/test_fleet_benchmark.py::test_2000_hub_tick_is_well_within_the_2s_cadence:13`; `simt/test_fleet_physics.py` (12 tests); `simt/test_fleet_lease.py` (6); `simt/test_common_crypto.py` (8); `simt/test_signed_batch_fixture.py::test_hub_rejects_the_batch_if_a_setpoint_is_altered:29` | K6/K7/K8 all confirmed referenced in code comments; `sim/fleet/runtime.py:382-413` has a built-in self-test of unsigned-command rejection. |
| ES03-S04 | Scenario injector (7 types) | built | `sim/control/injector.py:99-181`; `sim/control/scenarios.py:56-108`; `sim/control/catalogue.py` (37 entries) | `simt/test_injector.py` (14 tests); `simt/test_scenarios.py`; `simt/test_catalogue.py` (12); `simt/test_catalogue_pq.py` (13) | 5 of 7 story-named types solidly covered. **"Partner call"** (`PARTNER_CALL` wire enum) has no catalogue entry and zero test hits anywhere. **"Killed engine process"** is real but lives in `e2e/chaos/runner.py`, a separate subsystem, not this injector. `04-mvp-s-test-plan.md`'s own TS-03-06 text says "6 scenario-panel events" but names only 5 — a pre-existing 7-vs-6-vs-5 count mismatch across docs. |
| ES03-S05 | No over-reporting of available capacity | built | Decision D-25 (confirmation). `o/core/physics.py:141-152` (`hub_capability`, self-tagged "ES03-S05 (K1)" in its own docstring); `o/core/limits.py:30-62` | `t/property/test_k04_envelope.py`; `t/unit/core/test_limits.py`; `t/unit/fleet/test_es03_s05_energy_sustainability_property.py::test_reported_capability_never_exceeds_soc_derived_sustainable_power:97` | Fixed the same day (2026-09-26): a real over-reporting bug (0.1 kWh above reserve reported as full rated power) — the test's own docstring says "the strict `xfail` is removed." |
| ES03-S06 | Customer-operator simulators (`ogsim/customer`) | built dark (R2) | Decision D-11. At R2: `sim/customer/` (runtime, signals, requests, API client); unit `deploy/systemd/og-sim-customer.service`, not in `deploy/systemd/ogsim.target`. The orchestrator side is dark too: the customer API is mounted only when `[api.customer_api].enabled` (`o/api/app.py:177-190`, ships false), and site ingest runs only when `[site_ingest].enabled` (`o/engine/__init__.py:976-980`, ships false) | none found in this pass | At `434d230` no `sim/customer` package existed (the decision log's "(built dark)" label was ahead of the code). At R2 the label matches: `13-known-limitations.md` "Changes at R2", BD-3b. |

### ES04 — Contracts, opportunities & commitments

| Story | Title | Status | Code | Test | Gap / note |
|---|---|---|---|---|---|
| ES04-S01 | Obligation lifecycle (6 states) | built | `o/contracts/state_machine.py:38-65` (`_TRANSITIONS`), `:82-97` (`validate_transition`); `o/contracts/lifecycle.py:36-76` (`transition_obligation`, single writer) | `t/unit/contracts/test_state_machine.py::test_validate_transition_matches_reference_table:63`, `::test_random_event_sequences_never_reach_an_illegal_state:82` (property); `t/unit/contracts/test_lifecycle.py::test_delivering_shortfall_requires_an_allowed_reason_code:80` | `lifecycle.py`'s own docstring: "the only place that writes `og.obligation.state`." |
| ES04-S02 | Opportunity admission and feasibility | built | `o/contracts/admission.py:108-236` (`admit`/`admit_priced`), `:66-74` (`_is_activation_gated`) | `t/unit/contracts/test_admission.py::test_admit_creates_paired_opportunity_and_obligation:24`, `::test_admit_inactive_contract_is_rejected:49` | Module docstring self-tags "ES04-S02/S03/S05." |
| ES04-S03 | Partial take: `min_qty`/`increment`/`block` | built | `o/core/products.py:25-64` (`derive_variable_kind`/`round_quantity`/`is_feasible`); `o/selector/types.py:94-124` | `t/unit/core/test_products.py::test_block_all_or_nothing:31`, `::test_semi_continuous_ercot_as_min_0_1_increment_0_1:24`; `t/unit/selector/test_properties.py::test_lp_path_never_violates_k13_k2_or_product_rules:84` | — |
| ES04-S04 | Re-nomination points | built | `o/contracts/renomination.py:29-89` (`exercise_renomination_point`, self-tagged "ES04-S04") | `t/unit/contracts/test_renomination.py::test_reselection_of_a_committed_not_yet_delivering_obligation_is_refused:138`; `t/unit/selector/test_ts_04_04_renomination_property.py::test_ts_04_04_no_reselection_or_lock_reduction_between_renomination_points:153` | — |
| ES04-S05 | Reason-coded admission and shortfall | built | `o/core/reasons.py:56-60` (`LOCK_REASON_BY_SHORTFALL`); `o/contracts/admission.py:77-105` (`_reject`) | `t/unit/engine/test_escalation.py::test_the_real_allocators_shortfall_reasons_escalate:38` | — |
| ES04-S06 | DATA_CENTER/PIPELINE_AC profile: PQ envelope, activation gate | in build | Decision D-7. `o/contracts/admission.py:58-63,67,124,169-183`; `orchestrator/config/service_profiles/data_center.toml`; `o/allocator/pq_eligibility.py`; `o/allocator/pq_monitor.py`; `o/core/pq/envelope.py` | `t/unit/contracts/test_admission.py::test_admit_data_center_is_rejected_when_activation_gate_closed:138`,`:156`,`:172`,`:185`; `t/unit/profiles/test_data_center_profile.py`; `t/unit/allocator/test_pq_eligibility.py`,`test_pq_monitor.py`,`test_pq_performance.py` | Envelope/ladder/gate built and heavily tested; gate closed by default (`orchestrator/config/orchestrator.toml:57-60`) pending ES03-S06's closed-loop controllers. |

### ES05 — Dispatch (selector, ledger, allocator)

| Story | Title | Status | Code | Test | Gap / note |
|---|---|---|---|---|---|
| ES05-S01 | LP selection at gates (15 min + admission) | built | `o/selector/gate.py:473` (`run_gate`); `o/selector/model.py:93` (`build_mode_o_model`); `o/engine/__init__.py:123-139` (`GateScheduler.due_triggers`) | `t/unit/selector/test_gate.py::test_solve_gate_returns_optimal_for_a_feasible_instance`; `t/unit/selector/test_model_solve_extract.py::test_c16_non_anticipativity_is_structural` | `model.py`'s own docstring lists constraints C3/C7-C9/C11/C14/C17/C18 as still deferred. |
| ES05-S02 | Commitment lock (K13): freeze committed ŷ | built | Decision D-4. `o/core/limits.py:149-167` (`check_commitment_lock`) | `t/property/test_k13_commitment_lock.py` (7 tests); `t/unit/ledger/test_ts_04_01_stateful.py` (Hypothesis `RuleBasedStateMachine`) | Citation confirmed exact: floor = `min(frozen_kw, prior_kw)`. |
| ES05-S03 | Lock exceptions: L0/L1/L2/infeasibility | built | Decision D-17. `o/engine/escalation.py` (`ShortfallEscalator`, `lock_reason_for_shortfall`, `merge_signals`); `o/allocator/cycle.py:248-257` (`best_effort_reason`) | `t/unit/engine/test_escalation.py::test_a_shortfall_obligation_keeps_its_feasible_remainder_and_recovers_in_one_cycle:158`, `::test_a_short_obligation_is_flagged_at_risk_and_cleared_on_recovery:205`, `::test_a_shortfall_obligations_partial_grant_carries_the_code_g19_corroborates:224`; `t/unit/guardian/test_service.py:959,973,984` | All citations confirmed exact. |
| ES05-S04 | Substitution within a committed obligation | built | `o/allocator/substitution.py:34` (`realize_obligation`); `o/ledger/__init__.py:438` (`substitute`, rejects cross-obligation moves) | `t/unit/allocator/test_substitution.py::test_ts_05_20_substitutes_unhealthy_hub_keeps_full_delivery`, `::test_ts_05_21_no_substitute_reports_shortfall_against_same_obligation`; `t/unit/ledger/test_ledger.py::test_substitution_across_obligations_is_rejected` | Runs every 2s cycle, trivially meeting the "≤3 ticks" criterion; no test times recovery across multiple ticks specifically. |
| ES05-S05 | Single-writer ledger; one buyer | built | `o/ledger/__init__.py:260` (`ReservationLedger`), `:312` (`reserve`, calls `check_one_buyer` at `:339`), `:281` (`_write_lock`) | `t/unit/ledger/test_ledger.py::test_ts_05_02_reserve_refuses_to_exceed_capability`; `t/property/test_k02_one_buyer.py` (3 tests) | Cross-process exclusivity depends on `PgLedgerBackend.write_guard()`, exercised only in `t/integration/ledger/test_pg_ledger.py`, not the in-memory unit suite. |
| ES05-S06 | 2s real-time allocator + `DIST_DEFERRAL` PI | built | `o/allocator/cycle.py:53` (`cycle`) calls `allocate_tiers` (`o/allocator/lexicographic.py:40`) at `:109`, `DistDeferralPI.step` (`o/allocator/dist_deferral_pi.py:67`) at `:125`, `price_responsive_schedule` (`o/allocator/price_response.py:19`, 5-min dwell + $5/MWh hysteresis) at `:216` | `t/unit/allocator/test_cycle.py::test_ts_05_55_dist_deferral_pi_folds_into_the_committed_grant`; `t/unit/allocator/test_dist_deferral_pi.py::test_ts_05_30_output_never_exceeds_bank_kva_capability`; `t/unit/allocator/test_performance.py::test_ts_05_70_cycle_under_200ms_for_2000_hubs_40_banks` | The A11 "p99 < 500ms @ 2,000 hubs" target itself is measured manually via `e2e/perf/capture.py` against a live stack, not gated by the unit perf test (which checks a stricter but different best-of-5 < 200ms). See §5. `test_lexicographic.py`'s 6 tests are internally named `test_ts_05_10`…`test_ts_05_15` but test tier-priority allocation — content that no longer matches what `04-mvp-s-test-plan.md` defines under those same IDs today (doc/test-ID drift, see §5). |
| ES05-S07 | Rule-based baseline allocator, "shadow" | built | `o/selector/rule_fallback.py:28` (`rule_fallback_f2`), invoked at `o/selector/gate.py:465,469` on LP infeasible/timeout/validation failure | `t/unit/selector/test_rule_fallback.py` (4 tests); `t/unit/selector/test_properties.py:109-133` | "Shadow" in the story is the G3 delivery-milestone cut-line swap (rule allocator becomes primary, LP runs in shadow) — **not a live dual-run comparator**. No code path runs both concurrently against a succeeding LP solve. |
| ES05-S08 | Continuous per-obligation energy sufficiency (K1/K13) | built | Decision D-6. `o/allocator/energy_sufficiency.py:62` (`evaluate_energy_sufficiency`), `:98` (`evaluate_with_substitution`); wired at `o/engine/gateways.py:713` | `t/unit/engine/test_energy_sufficiency_gateway.py::test_missing_soc_flags_at_risk_and_raises_alert:263`, `::test_at_risk_flag_is_set_on_entry_and_cleared_on_recovery:354`, `::test_two_obligations_sharing_a_bank_the_other_ones_energy_is_excluded_k2:287`; `t/unit/allocator/test_energy_sufficiency.py` (9 tests) | `t/property/test_energy_sufficiency.py` tests a **different** K1 mechanism (the guardian's own `check_reserve_floor_over_lease`) — do not cite it for this story. |
| ES05-S09 | Commitment basis: FIXED vs NEED, guarded release | built | Decision D-18. `o/guardian/checks.py:276,292-305` (`check_g19_need_basis`); `o/core/reasons.py:42` (`R_GRANT_CLOSED_LOOP`); `o/invariants/checks.py:341-343`; `o/invariants/queries.py:187-213` | `t/unit/guardian/test_service.py::test_need_basis_grant_below_the_reserved_maximum_is_signed:791`, `::test_the_same_grant_on_a_fixed_profile_is_vetoed:800`,`:811`,`:821`,`:835` | Every line and test verified exact — cleanest citation of the epic. |
| ES05-S10 | Max discharge-flow limit at every level | in build (R2: allocator only) | Decision D-26. F1 derating, always on: `o/allocator/flow_limits.py:49-68`, applied at `o/allocator/cycle.py:123-135`. F2 export cap (`o/allocator/flow_limits.py:78-88`) and F3 transformer/feeder/substation budgets (`:108-182`), behind `[allocator.flow_limits].enabled = true` | `t/unit/allocator/test_dispatch_extensions.py:295,319,335,351,363` | Not in the selector. F2/F3 have no limit rows in any repo seed (migration 0029), so they bind nothing yet. No import-direction caps; the peak is never planned. |

### ES06 — Guardian & safe stop

| Story | Title | Status | Code | Test | Gap / note |
|---|---|---|---|---|---|
| ES06-S01 | Guardian core checks: reserve/ramp/P-kVA/lease-epoch/Ed25519 | built | Decision D-25 (confirmation). `o/core/limits.py:30-105`; `o/guardian/checks.py:151-171` (G-13 lease/epoch); `o/core/crypto.py:16-18,161-172` (Ed25519) | `t/unit/guardian/test_checks.py` (6 G-01 cases); `t/property/test_k04_envelope.py`; `t/property/test_k03_sole_signer.py:74,88`; `t/property/test_k06_freshness.py:32-58` | Pre-existing citation only covered the reserve half (`o/core/limits.py:30-62`); ramp/P/kVA are `:65-105`. |
| ES06-S02 | G-19: refuse a batch reducing a commitment without cause | built | `o/guardian/checks.py:196-327` (`check_g19_commitment_lock` + 5 helpers, docstring self-cites "ES06-S02"); wired at `o/guardian/service.py:718,737,750,765` | `t/unit/guardian/test_checks.py::test_g19_commitment_lock_negative_no_reason:177`; `t/unit/guardian/test_service.py` (18 `g19` tests); `t/property/test_k13_commitment_lock.py` (7 tests) | — |
| ES06-S03 | Scoped safe stop: fleet/zone/bank, stop-only | built | `o/safestop/confirmation.py:51-83` (two-step propose/confirm); `o/safestop/main.py:62,73,77`; `o/safestop/__init__.py:21,53-56` (Scope); `o/safestop/service.py:208-217` (`release()` unconditionally raises) | `t/unit/safestop/test_confirmation.py::test_a_single_propose_never_engages_anything:58`; `t/unit/safestop/test_property_ts_06_03_stop_only.py::test_no_sequence_ever_produces_a_release_row:69`; `t/integration/safestop/test_og_t_stop.py::test_k8_topology_stop_works_with_no_engine_or_guardian_process:102` | — |
| ES06-S04 | TIMEOUT ≠ VETO ≠ STOP; on-call paging | built | `o/guardian/escalation.py:29-32,142-143` | `t/unit/guardian/test_escalation.py:49,64,76,145` | Pre-existing citation confirmed exact. |
| ES06-S05 | Feeder/substation ramp ceiling (G-06) | built | `o/core/limits.py:125-138` (`check_feeder_ramp_ceiling`); `o/guardian/checks.py:132-139` (`check_g06_feeder_ramp`); `o/guardian/service.py:322` | `t/unit/core/test_limits.py::test_feeder_ramp_ceiling_only_applies_to_firm_events:80`; `t/unit/guardian/test_checks.py::test_g06_feeder_ramp_negative_firm_event:108`; `t/property/test_k04_envelope.py::test_k04_the_guardian_never_signs_a_firm_step_beyond_its_feeder_ceiling:164` | The story text's own "TS-06-19" tag appears in zero test files by that literal name (doc/test-ID label mismatch; the functionality itself is covered). |
| ES06-S06 | G-20: refuse to sign on degraded clock quality | built | `o/guardian/checks.py:330-336` (`check_g20_clock_quality`); `o/guardian/config.py:23,59,114` | `t/property/test_k12_time_quality.py` (5 tests); `t/unit/guardian/test_checks.py:189,194`; `t/unit/guardian/test_service.py::test_clock_skew_holds_as_timeout_not_veto:157` | Hold-not-veto semantics explicitly tested. |
| ES06-S07 | Two-person safe-stop RELEASE (`og-op-a`/`og-op-b`) | built | Decision D-12. `orchestrator/config/orchestrator.toml:95,132`; `o/safestop/__init__.py:65-69`; `o/safestop/service.py:33-36,214-217` | `t/unit/safestop/test_release_relay.py` (6 tests); `e2e/functional/safety/test_ts06_guardian_and_safe_stop.py:24-25` | Pre-existing citation confirmed exact. |
| ES06-S08 | Guardian PQ checks G-21..G-25 (K14) | built | Decision D-7. `o/guardian/pq_checks.py:42-223` (7 functions) | `t/unit/guardian/test_pq_checks.py` (27 tests); `t/property/test_k14_pq_envelope.py:228,250,270` | Best-verified story in the epic — every citation exact. |
| ES06-S09 | Guardian flow-limit checks G-26..G-33 | built (R2) | Decision D-27. `o/guardian/flow_checks.py:164` G-26, `:219` G-27, `:261` G-28/G-29/G-30 (aggregate flows), `:296` G-29 POI, `:88` G-31, `:317` G-33 (on `o/market/territory.py:131`); G-32 `o/core/limits.py:360`; wired `o/guardian/service.py:328,383-390,427,452-493,539-541,584` | `t/unit/guardian/test_flow_checks.py` (29), `t/unit/guardian/test_service_flow.py` (14) | Lines at R2 `6470cfa`. Adversarial review of R2 in progress (fail-open paths). |

### ES07 — Health & degraded modes

| Story | Title | Status | Code | Test | Gap / note |
|---|---|---|---|---|---|
| ES07-S01 | Module heartbeats + `/status` + `/metrics` | in build | `o/platform/heartbeat.py:26` (`write_heartbeat`, called by all 6 orchestrator processes); `o/engine/metrics.py:56` | `t/unit/platform/test_heartbeat.py::test_write_heartbeat_upserts_expected_row`; `t/unit/engine/test_metrics.py::test_endpoint_uses_the_configured_port_on_loopback` | Heartbeat is built and tested for all 6 processes. A Prometheus `/metrics` HTTP server is only started by **engine** and **guardian** (`start_http_server` calls found only there) — feeds/safestop/settle/api never expose one. No literal `/status` route exists anywhere. |
| ES07-S02 | Feed-stale degraded mode: no new commitments | in build (R2: enforced at two gates) | `o/health/model.py:22-23` (`DegradedMode` literal); `o/health/rules.py:100-101`. At R2 the mode is enforced: the intake gate skips intake (`o/engine/gates.py:51-60`, `:123-132`) and the selector gate withholds every candidate, treating an unreadable mode as active (`o/selector/gate.py:532-602`) | `t/unit/health/test_rules.py::test_feed_stale_yields_no_new_commitments:186` | Not checked at contract admission (operator CRUD/admit) or at customer-API submission; that the transition is traced was not verified. At `434d230` nothing read the flag. Since R2 hotfix v3 (`afb26c2`) only `[health] firm_blocking_feeds` (default the ERCOT price, `o/health/rules.py:139` there) sets the mode. |
| ES07-S03 | Hub health: online/stale/fault | built | `o/health/rules.py:66-77` (`classify_hub_health`) | `t/unit/health/test_rules.py:91-117` (4 tests); `t/unit/fleet/test_twin.py::test_hub_health_classifies_fault_independent_of_timing` | Cross-checked directly against the fleet twin's own classification. |
| ES07-S04 | Cycle latency + engine-down degraded mode | built | `o/health/model.py:24`; `o/engine/latency.py:74`; `o/health/rules.py:225-265` | `t/unit/health/test_rules.py::test_engine_down_yields_hold_local_autonomy:192`; `t/unit/engine/test_latency.py` (10 tests); `e2e/perf/test_metrics.py` | The 2k-hub/30-min p99 < 500ms claim itself is measured by a manual perf-harness script (`e2e/perf/capture.py`), not a CI-asserted test — see §5. |
| ES07-S05 | Guardian-down (HOLD) + SCADA-silent (`DIST_DEFERRAL_OPEN_LOOP`) | in build (`afb26c2`: raised, display-only) | `o/health/model.py:22-27` (4 `DegradedMode` values); `o/health/rules.py:92-109` (`derive_degraded_modes`); at `afb26c2`: `o/health/rules.py:204-240` (`is_scada_silent`, `ALR-SCADA-SILENT`), wired `o/health/__init__.py:377` | `t/unit/health/test_rules.py::test_guardian_down_yields_hold:198`, `::test_degraded_modes_can_combine:204`; `t/unit/ui/test_degraded_and_escalation.py::test_banner_text_joins_labels_and_keeps_unknown_codes:57` (exercises the `DIST_DEFERRAL_OPEN_LOOP` label) | `HOLD` (guardian-down) is fully live. At `434d230` and `6470cfa` `DIST_DEFERRAL_OPEN_LOOP` could never be produced (its sole caller never passed `dist_deferral_scada_silent`). Since R2 hotfix v3 (`afb26c2`) it is set after 60 s without any SCADA reading, fleet-wide rather than per bank, never before the first reading. Tests at `afb26c2`: `t/unit/health/test_rules.py:218`, `t/unit/health/test_init.py:389,402,416,431`. Only the UI reads the mode; the PI loop does not fall back to open loop. |

### ES08 — Settlement (M&V, billing, profitability)

| Story | Title | Status | Code | Test | Gap / note |
|---|---|---|---|---|---|
| ES08-S01 | Interval metering and baseline (M&V) | built | `o/settle/metering.py:23` (`meter_interval`); `o/settle/baselines.py:41` | `t/unit/settle/test_metering.py` (5 tests); `t/unit/settle/test_baselines.py` (3 tests) | No dedicated code or test found for the "reconciled within 24h" / variance-flagging acceptance bullet specifically. |
| ES08-S02 | Performance % + insert-only invoice lines | built | `o/settle/billing.py:29` (`draft_invoice_lines`), `:115` (`next_version`) | `t/unit/settle/test_billing.py:107-152` (4 tests); `t/unit/settle/test_idempotency_property.py::test_repeated_settle_calls_never_duplicate_rows` | — |
| ES08-S03 | Profitability decomposition | built | `o/settle/profitability.py:71` (`compute_pnl`); `o/core/economics.py:17` (`wear_cost`) | `t/unit/settle/test_profitability.py:38,72,131` | Degradation cost is folded into `compute_pnl` via `wear_cost()`, not a standalone function. |
| ES08-S04 | Value vs rule-baseline + forgone upside | built dark | `o/settle/profitability.py:116` (`compute_value_added_by_lp`), `:134` (`compute_forgone_upside`) — real-data feed stubbed at `o/settle/pg_backend.py:661-667,681-686` | `t/unit/settle/test_profitability.py:155,164` | `fetch_rule_baseline_delivered_kwh`/`fetch_best_competing_value_per_kwh` both explicitly return `None` ("the rule-baseline shadow allocator (ES05-S07) is not wired up yet"). Formulas are built and tested against fixtures; in production `rule_baseline_value` is always `None` and `forgone_upside` always `0` — matches the documented cut-line-3 fallback. |
| ES08-S05 | CSV export of invoice lines / M&V | built | `o/settle/csv_export.py:20,35,42` | `t/unit/settle/test_csv_export.py::test_invoice_line_csv_includes_superseded_line_link` (+5 more) | Superseded-line-link bullet directly covered. |

### ES09 — Audit trace & retention

| Story | Title | Status | Code | Test | Gap / note |
|---|---|---|---|---|---|
| ES09-S01 | Append-only, hash-chained trace | built | `o/trace/store.py:90` (`TraceStore.append`), `:131` (`verify`) | `t/unit/trace/test_store.py:97,102,109`; `t/property/test_k11_verifiable_trace.py::test_k11_every_stream_verifies_after_arbitrary_interleaved_appends:32`; `t/property/test_k10_trace_before_act.py:40` | "Same transaction as the causing action" is not literally traced call-site-by-call-site; safety instead comes from a UNIQUE-constraint conflict on double-insert, not an explicit shared transaction. |
| ES09-S02 | Reason codes incl. `R-COMMIT-LOCK-*` | built | `o/core/reasons.py:14-17,21-28` (`R_COMMIT_LOCK_OVERRIDE_L0/L1/L2`, `R_COMMIT_LOCK_INFEASIBLE`); `o/invariants/queries.py:19-26,245-264` | `t/property/test_k13_commitment_lock.py:26,98`; `t/unit/invariants/test_checks.py:321,338` | — |
| ES09-S03 | Configurable retention + checkpointed pruning | built | `o/trace/pg_backend.py:239` (`run_retention_prune_job`); `o/settle/__init__.py:384` (`run_trace_pruning_cycle`) | `t/unit/trace/test_pg_backend.py:397,409,443`; `t/property/test_k11_verifiable_trace.py::test_k11_the_stores_own_retention_prune_keeps_the_chain_verifiable:48` | `store.py`'s own `prune()` (line 153) is only a fixed 1,000-record backstop; the real per-class-days retention logic lives in `pg_backend.py`, not `store.py` — cite the latter. |
| ES09-S04 | Chain-verify button/endpoint | built | `o/api/routers/billing.py:107-121` (`verify_trace`, `POST /og/api/trace/verify`) | `t/unit/api/test_endpoints.py:64,70`; `t/unit/ui/test_billing_audit.py:61,71`; `t/unit/trace/test_store.py:141` | Endpoint lives in `billing.py`, not `admin.py`/`retention.py`. |

### ES10 — Operator UI

| Story | Title | Status | Code | Test | Gap / note |
|---|---|---|---|---|---|
| ES10-S01 | Control room (screen 1) | built | `o/ui/routes/control_room.py:30` (`control_room`), `:84` (`story_view`) | `t/unit/ui/test_markets_live_mapping.py::test_control_room_seeds_the_ticker_from_the_price_series:77`; `t/unit/ui/test_degraded_and_escalation.py::test_banner_shows_the_active_modes:73` | The fleet "map" is 4 fixed load-zone bubbles sized by √(hub count), not real per-hub clustering — hub lat/lon are always `NULL` (`docs/demo/NEEDS_FROM_OTHER_OWNERS.md` item 13). No test exercises the 2,000-hub map/perf acceptance criterion directly. |
| ES10-S02 | Fleet monitoring & control (screen 2) | built | `o/ui/routes/fleet.py:108` (`fleet_screen`), `:162`/`:283` (propose safestop/command) | `t/unit/ui/test_fleet_confirm_flows.py` (18 tests); `t/unit/ui/test_fleet_release.py` (6 tests) | Guardian veto (409), spoofed-role rejection, and the two-operator release path all covered. |
| ES10-S03 | Dispatch & commitments (screen 3) | built | `o/ui/routes/dispatch.py:321` (`dispatch_page`), `:106` (`pipeline_view`), `:300` (`commitment_lock_events_view`) | `t/unit/ui/test_dispatch.py` (8 tests); `e2e/functional/dispatch/test_ts04_commitment_lock.py:40,72` | `decision_line()` renders a qualitative sentence, not a computed "regret" number — AC's "each loser's regret rendered" is only partly literal. |
| ES10-S04 | Markets & feeds + Health (screens 4–5) | built | `o/ui/routes/markets.py:213` (`markets_page`); `o/ui/routes/health.py:176` (`health_screen`) | `t/unit/ui/test_markets.py` (4); `test_markets_live_mapping.py` (6); `test_degraded_and_escalation.py` (10) | The cycle-latency panel (`o/ui/templates/health.html:41`) has no live metrics endpoint wired yet — a data gap (`NEEDS_FROM_OTHER_OWNERS.md` item 11), not a code gap. |
| ES10-S05 | Profitability (screen 6) | built | `o/ui/routes/profitability.py:108` (`profitability_page`), `:70`, `:99` | `t/unit/ui/test_profitability.py:26,38,50` | The graceful-degrade path under cut line 3 (no baseline/forgone panel) is unverified — no test covers it. |
| ES10-S06 | Billing & audit (screen 7) + scenario panel | in build | Audit half built: `o/ui/routes/billing_audit.py:143`. Scenario half is API-only: `o/api/routers/scenario.py:34` (`trigger_scenario`) | Audit: `t/unit/ui/test_billing_audit.py` (6); `test_billing_audit_routes.py` (3). Scenario: `t/unit/api/test_scenario.py:34,52` | Trace explorer, chain-verify, invoice+CSV, M&V are all built and tested. **The "demo scenario panel" (7 triggers) does not exist as a UI feature** — no template or route under `o/ui/` mentions scenario/panel/inject/trigger (only a docstring in `o/ui/__init__.py:2` names it as intended scope); only a generic single-name POST relay exists via the API, backed by the 37-entry `sim/control/catalogue.py` (which doesn't map 1:1 to the story's 7 named triggers either — no id for "killed engine process" or "partner call"). |

### ES19 — Two markets (regulated utility + ERCOT)

| Story | Title | Status | Code | Test | Gap / note |
|---|---|---|---|---|---|
| ES19-S01 | Per-bank ERCOT zone pricing | built | Decision D-10. `o/selector/gate.py:272-314`; `o/selector/types.py:52-69`; `o/selector/model.py:314`; `o/selector/rule_fallback.py:119,141`; `o/engine/gateways.py:142-209,336-360` | `t/unit/selector/test_zone_pricing.py::test_each_bank_gets_its_own_zone_and_load_rows_are_ignored:28` | Pre-existing citation confirmed exact. |
| ES19-S02 | Regulated-utility contracts, territory-bound energy | in build (R2: all but admission) | Decision D-20, D-21. At R2: migration 0025 (`og.utility`, `og.contract.market`/`utility_id`); `o/market/territory.py:131` (`check_territory`); selector C25 as bank eligibility `o/selector/gate.py:489-529` and stage R `o/selector/solve.py:140-144`; allocator `o/allocator/cycle.py:221-232`; guardian G-33/G-30; settle `o/settle/__init__.py:242-269` | `t/unit/market/test_territory.py:123` (property), `t/unit/market/test_model.py:63,72`, `t/unit/selector/test_market_economics.py:182,212` | Contract admission has no territory check. Dark in production: no regulated-zone bank is seeded (`integration-sims/config/fleet.yaml:56-72`). |
| ES19-S03 | Substation-sited battery assets (~20 MW) | in build (R2: data, guardian, sim) | Decision D-21. At R2: `og.asset` with `asset_class` `SUBSTATION` and POI limits (migration 0025); guardian POI check `o/guardian/flow_checks.py:296`; sim asset `integration-sims/config/fleet.yaml:85-90` (`enabled: false`) | none found for dispatch | No code dispatches a substation asset (no capability row, no nearest-substation preference). The dev seed's `sub-aen-01` has no `bank_id`, so the POI check does not apply to it. |
| ES19-S04 | $/kW-in vs $/kW-out economics, payback | built (R2) | Decision D-20, D-23. `o/market/economics.py` (per-kW, simple/effective/discounted payback, NPV, 3-year flag); `o/market/view.py`; `GET /og/api/profitability/per-kw` (`o/api/routers/profitability_kw.py:37`) | `t/unit/market/test_economics.py:115,151`; `t/unit/market/test_view.py:26` | Capex and O&M are planning attributions; no `og.asset_finance` table. |
| ES19-S05 | Charging mix: ≥30% solar + off-peak, M1 in competitive area | in build (R2: selector built) | Decision D-19, D-22, D-24. C27 soft solar floor `o/selector/model.py:265-307`; regulated off-peak/night charging and M1 in the competitive area (`o/market/charging.py`); measured solar share per interval (`og.plan_energy_value.solar_share`, migration 0030); settle M1 (`o/settle/tariffs.py:101`) | `t/unit/selector/test_plan_hardening.py:111,177`; `t/unit/market/test_charging.py:46,69`; `t/unit/settle/test_tariffs.py` | No month-to-date carry-in for the floor; whether a floor shortfall is reported was not verified; the midday preference depends on ES02-S05's forecast input. |

---

## 3. Invariants K1–K15 → enforcement → test → status

Canonical definitions: `00-invariants.md`. Enforcement is listed primary → independent (guardian) check, per that
document's own convention.

| K | Summary | Primary enforcement | Independent check (guardian) | Test | Status |
|---|---|---|---|---|---|
| K1 | Homeowner reserve never breached | `o/core/limits.py:30-39` (`check_reserve_floor`, `check_reserve_floor_over_lease`) | `o/guardian/checks.py:38-59` (`check_g01_reserve`, `check_g01_energy_lease`, G-01/G-01-ENERGY) | `t/property/test_k01_reserve.py` (5 tests, e.g. `test_k01_guardian_never_signs_a_discharge_below_the_reserve_floor:64`) | built |
| K2 | One buyer: single-writer ledger | `o/core/limits.py:141` (`check_one_buyer`); `o/ledger/__init__.py:260-339` (`ReservationLedger`, `_write_lock`) | `o/guardian/checks.py:142` (`check_g09_ledger_version`, G-09) | `t/property/test_k02_one_buyer.py` (3 tests, e.g. `test_k02_ledger_admits_obligations_first_fit_and_never_oversells:33`) | built |
| K3 | Sole signer: Ed25519 | `o/core/crypto.py:16-18,161-172` | guardian is the signer itself; hub verifies (`sim/fleet/commands.py:63-98`) | `t/property/test_k03_sole_signer.py` (2 tests, e.g. `test_k03_a_pass_signature_verifies_only_for_its_own_payload_and_key:88`) | built |
| K4 | Physical envelope: P/kVA/ramp | `o/core/limits.py:65-138` at `434d230` (`check_hub_power`, `check_bank_kva`, `check_hub_ramp`, `check_fleet_ramp_cap`, `check_feeder_ramp_ceiling`; R2: `:93`, `:295`, `:314-367`) | `o/guardian/checks.py:60-139` (G-02 hub P · G-03 bank kVA · G-04 hub ramp · G-05 fleet ramp cap/stagger · G-06 feeder ramp ceiling) | `t/property/test_k04_envelope.py` (9 tests) | **built; extended families built in the guardian, partly in the dispatcher (R2) — see K4 extended below** |
| K5 | Grid authority: L2 hard constraint | allocator equality/limit | `o/guardian/checks.py:181` (`check_g15_l2_boundary`, G-15) | `t/property/test_k05_grid_authority.py` (2 tests) | built |
| K6 | Command freshness: sequence/epoch/lease | guardian issues; hub checks (`sim/fleet/commands.py`) | `o/guardian/checks.py:151` (`check_g13_freshness`, G-13, wraps `o/core/timeutil.py`) | `t/property/test_k06_freshness.py` (4 tests) | built |
| K7 | Degrade, don't trip: TIMEOUT≠VETO≠STOP | `o/guardian/escalation.py` (`Posture`, `TransitionKind`); hub lease expiry (`sim/fleet/lease.py`) | guardian timeout handling itself | `t/property/test_k07_degrade.py` (4 tests) | built |
| K8 | Stop authority: independent safe stop | `o/safestop/` (own process, stop-only key; `o/safestop/service.py:208-217` `release()` always raises) | n/a by design (independent of guardian/engine) | `t/property/test_k08_stop_authority.py` (2 tests); `t/integration/safestop/test_og_t_stop.py::test_k8_topology_stop_works_with_no_engine_or_guardian_process:102` | built |
| K9 | One loop per quantity | `o/allocator/dist_deferral_pi.py` (`DistDeferralPI.step`) | `o/guardian/checks.py:67-91` (`check_g03_bank_kva*`, G-03, reads the same `core.limits`/`core.physics` formula, no second integrator) | `t/property/test_k09_single_integrator.py` (3 tests) | built |
| K10 | Trace before act | `o/trace/store.py:90` (`append`, pre-image write) | `o/guardian/checks.py:174` (`check_g14_trace_preimage`, G-14) | `t/property/test_k10_trace_before_act.py` (3 tests, e.g. `test_k10_a_missing_pre_image_vetoes_on_g14_and_signs_nothing:40`) | built |
| K11 | Verifiable track record: hash chain, retention | `o/trace/store.py:131` (`verify`), `:153` (`prune`); `o/trace/pg_backend.py:239` (real per-class retention) | trace module itself (no separate guardian check) | `t/property/test_k11_verifiable_trace.py` (4 tests, e.g. `test_k11_a_stream_still_verifies_after_pruning_any_prefix:39`) | built (local chain); **external anchoring partly built at R2** (NB-9: every 900 s to two local directories and `og.trace_anchor`, `o/trace/anchoring.py:100-160`; unsigned in the shipped config, same host) |
| K12 | Time quality: guardian clock offset | `o/core/timeutil.py` (`clock_offset_ok`) | `o/guardian/checks.py:330` (`check_g20_clock_quality`, G-20) | `t/property/test_k12_time_quality.py` (5 tests) | built |
| K13 | Commitment lock | `o/core/limits.py:149-167` (`check_commitment_lock`) | `o/guardian/checks.py:196-327` (`check_g19_commitment_lock`, `check_g19_override_evidence`, `check_g19_need_basis`, `check_g19_as_hold`, G-19) | `t/property/test_k13_commitment_lock.py` (7 tests, e.g. `test_k13_guardian_vetoes_a_batch_that_omits_any_committed_obligation:50`) | built — incl. the two 2026-09-26 additions below |
| K13 (D-17 addition) | Best effort after a mid-window SHORTFALL — never a stop for the rest of the window | `o/engine/escalation.py` (sustain-cycle counter → `SHORTFALL`); `o/allocator/cycle.py:248-257` (`best_effort_reason`) | `o/guardian/checks.py:196-327` (same G-19 path signs the best-effort grant) | `t/unit/engine/test_escalation.py:158,205,224`; `t/unit/guardian/test_service.py:959,973,984` | built |
| K13 (D-18 addition) | Commitments FIXED vs NEED basis; need-basis reservation never resold | `og.service_profile.setpoint_source` (`MEASURED_FEEDBACK`); `o/core/reasons.py:42` (`R_GRANT_CLOSED_LOOP`) | `o/guardian/checks.py:292-305` (`check_g19_need_basis`) | `t/unit/guardian/test_service.py:791,800,811,821,835` | built |
| K14 | Power-quality envelope | `o/core/pq/envelope.py`; `o/allocator/pq_eligibility.py`, `pq_monitor.py` | `o/guardian/pq_checks.py:42-223` (G-21 imbalance · G-22 THD · G-23 freq/voltage · G-24 asset conformance · G-25 calibration safety) | `t/property/test_k14_pq_envelope.py` (16 tests); `t/unit/guardian/test_pq_checks.py` (27 tests) | built |
| K15 | Territory (REG obligations stay in-territory; net injection ≤ 0 at boundary substations) | R2: selector C25 as bank eligibility (`o/selector/gate.py:489-529`); allocator `enforce_territory` on (`o/allocator/cycle.py:221-232`, `:412-416`); settle regulated tariff (`o/settle/__init__.py:242-269`). Contract admission: not built | G-33 (`o/guardian/flow_checks.py:317` → `o/market/territory.py:131`), G-30 (`o/guardian/service.py:464-469`, idle: no regulated-zone bank seeded) | `t/unit/market/test_territory.py:122-130`; `t/unit/allocator/test_dispatch_extensions.py:440-452`; `t/unit/guardian/test_flow_checks.py:389`; checker `K15_TERRITORY` (`o/invariants/queries.py:409-441`) | **partly built (R2)** — admission, part (b) in the checker and the `K15_TERRITORY_MARKET`/`_EXPORT`/`K15_CHARGING_TARIFF` counters are not built |

**K4 extended: flow-limit hierarchy (D-26, D-27).** Final guardian numbering per `00-invariants.md` "Flow limits
and territory":

| Check | Limit | Status at R2 (`6470cfa`) |
|---|---|---|
| G-26 | Home meter: export/import net of home load | built — `o/guardian/flow_checks.py:164`; wired `o/guardian/service.py:328` |
| G-27 | Service transformer | built — `flow_checks.py:219`; wired `service.py:539-541` |
| G-28 | Feeder thermal and reverse flow | built — `flow_checks.py:261`; wired `service.py:452-457` |
| G-29 | Substation POI / transformer | built — `flow_checks.py:261`, `:296`; wired `service.py:458-463,489-493` |
| G-30 | Territory export (K15c) | built — `flow_checks.py:261`; wired `service.py:464-469` |
| G-31 | Sustained vs peak | built — `flow_checks.py:88`; wired `service.py:427` |
| G-32 | Feeder ramp for non-firm steps | built — `o/core/limits.py:360`; wired `service.py:383-390` |
| G-33 | K15 market segregation | built — `flow_checks.py:317` → `o/market/territory.py:131`; wired `service.py:584` |

All eight are built at R2 (`main` `6470cfa`, the lines above), on the guardian's own reads (`o/guardian/main.py:349`
wires the topology port). Tests: `t/unit/guardian/test_flow_checks.py` (29), `t/unit/guardian/test_service_flow.py`
(14). No feature flag gates them. The (e) ramp family is built and covered above under K4. The (a) derating curve
is built in the allocator (`o/allocator/flow_limits.py:49-68`) and the guardian (G-02 changed,
`o/guardian/flow_checks.py:133`). Caveats, detailed under K4 in `00-invariants.md`:
- G-31's peak path is dead because of a field-name mismatch (`o/guardian/mqtt_io.py:36`);
- G-29 and G-30 are idle without substation or regulated-zone data;
- reverse flow is not measured (`o/guardian/flow_repo.py:40-48`);
- the allocator's F2/F3 caps have no limit rows to apply.

**The ERCOT_AS energy hold is not a guardian check.** Two built pieces enforce it:

- the selector's C3′ floor — `o/selector/model.py:345` at R2 (`:294-298` at `434d230`);
- the engine's S6 hold floor — `o/engine/gateways.py:139` at R2 (`AS_HOLD_FLOOR_FRACTION`; `:120-127` at
  `434d230`).

`CHECK_AS_HOLD` measures it, built in R2: `o/invariants/__init__.py:489-508` (registered at `:192`),
`o/invariants/checks.py:527-561` (`find_as_hold_violations`), `o/invariants/queries.py:444-477`. Its scope is
narrower than the spec:
- it runs only while an `og.as_deployment` window is active (`queries.py:471`), so a held, undeployed award is
  never measured;
- it requires the full product duration even mid-deployment (`checks.py:547`);
- it does not net out other obligations on the same banks (`queries.py:460`).

This has been reported to the lead.

---

## 4. Decisions D-4…D-27 → affected docs → status (CONFLICTS)

Docs checked (per the task): `00` = `00-invariants.md` · `01` = `01-saturday-delivery-plan.md` · `02a` =
`02a-mvp-s-spec-engine.md` · `02b` = `02b-mvp-s-spec-platform.md` · `03` = `03-mvp-s-epics-stories.md` · `04` =
`04-mvp-s-test-plan.md` · `06` = `06-service-profiles-and-power-quality.md`. A cell reads **n/a** when that
decision's own "Affects" column in `11-decision-log.md` doesn't name that doc (checked anyway where there was
independent reason to; otherwise skipped). `GAP` = the doc is named in "Affects" but says nothing about the
decision. `CONTRADICTS` = the doc states something the decision has superseded.

### 4.1 Decision → doc reflection

| D-# | Decision (one line) | 00 | 01 | 02a | 02b | 03 | 04 | 06 | Open? |
|---|---|---|---|---|---|---|---|---|---|
| D-4 | Commitment lock: no mid-contract switching | yes (K13 def) | yes (never-cut list) | yes (§3.3 C24, §6.1 G-19) | yes (§7 demo script) | yes (ES05-S02) | yes (K13 row, TS-04-02/03) | n/a | No |
| D-5 | Hardware: 39.2kWh/11kW, 20% dual-unit, ~600kVA | n/a | yes (§0b RC-2) | yes (G-01/G-02 table) | yes (§4.2) | yes (ES03-S01) | yes (`FX-FLEET-MVPS`) | n/a | No |
| D-6 | Energy sufficiency checked continuously | yes (K1/K13 energy addition) | yes (§0b RC-1; its stale "in progress" was fixed with this matrix) | yes (§2.3 cross-ref) | n/a | yes (ES05-S08) | n/a | n/a | No (fixed) |
| D-7 | PQ spec approved w/ amendments (K14, G-21..25) | yes (K14 def, guardian numbering) | yes (§0b RC-3/4, A12/A13) | n/a (covered via 06) | n/a | yes (ES04-S06, ES06-S08) | **CONTRADICTS** (TS-06-07b/12/13 still call G-21/22/23 "supplemental, non-canonical"; fixed on `wp/TP-UPDATE-test-plan` `778370a`: TS-06-07b → G-27, TS-06-12 → G-24, TS-06-13 → G-13) | yes (owner-decisions items 1–6) | Fixed on TP-UPDATE (R3) |
| D-8 | Inverter terms: substitution/swap/calibration | n/a | n/a | n/a | n/a | n/a | n/a | yes (items 7–8) | No |
| D-9 | Dual-unit homes 10/bank; banks single-zone | n/a | n/a | n/a | yes (§4.2, old rule kept "for the record" as superseded) | yes (ES03-S02) | n/a | n/a | No |
| D-10 | Per-bank load-zone pricing (Houston Hub fix) | n/a | n/a | yes (§6.1) | n/a | yes (ES19-S01) | n/a | n/a | No |
| D-11 | Customer sims/API/site-ingest (log says "built dark") | n/a | n/a | n/a | **partial** (open point 9, self-flags 3 of 4 pieces as inert scaffolding, not dark) | **partial** (ES03-S06 self-flags the same discrepancy) | **GAP** at `434d230` (zero coverage); `wp/TP-UPDATE-test-plan` `30d7f78` adds TS-10-08 | **GAP** (defines `MEASURED_FEEDBACK`/`CUSTOMER_API` schema fields, never narrates D-11 itself) | **Yes** |
| D-12 | Test accounts `og-op-a`/`og-op-b`/`og-cust-*`, proxy-secret trust | n/a | yes (bonus, Keycloak/OPA row) | n/a | yes (§7 — log cites "§8", a stale section-label only) | yes (ES06-S07) | n/a | n/a | No |
| D-13 | Per-workspace MQTT users | n/a (infra, not a K-invariant) | n/a | n/a | yes (§6.1) | yes (ES01-S06) | n/a | n/a | No |
| D-14 | ftbrown is a team member | n/a | n/a | n/a | n/a | n/a | n/a | n/a | No — affects `WORKBOARD.md` only, outside the 7 |
| D-15 | Keep the EIA key, no rotation | n/a | n/a | n/a | n/a | n/a | n/a | n/a | No — "Affects: none" |
| D-16 | 192.168.5.35 is the permanent host | n/a | yes (§0a) | n/a | yes (§9.1) | n/a | n/a | n/a | No |
| D-17 | Mid-window SHORTFALL is best effort, never stops | yes (addition, built) | n/a | yes (§2.3, §5.6) | n/a | yes (ES04-S05 xref, ES05-S03) | **partial** at `434d230` (TS-04-08/09/10/12 consistent but no row states the added "keeps delivering/restores/never stops" nuance); `30d7f78` adds TS-04-17, TS-19-13 | n/a | **Yes (minor)** |
| D-18 | Commitments FIXED/NEED basis; need-basis never resold | yes (addition, built) | n/a | yes (§5.6, §6.6) | n/a | yes (ES05-S09) | **GAP** at `434d230` (no need-basis test row); `30d7f78` adds TS-04-18, TS-19-12 | **silent** (defines the field, never narrates the policy) | **Yes** |
| D-19 | M1 = full TDSP charge, PUCT 2026-09-01 rates | n/a | n/a | yes (§3.4) | n/a | yes (ES19-S05) | n/a (decision log's "04-external-data-integration" is `docs/orchestrator/02-architecture/...`, a different doc — out of scope here) | n/a | No |
| D-20 | Two markets: regulated + free, $/kW-in vs out | n/a | yes (§0a, "in build") | yes (§3.2) | n/a | yes (ES19 epic intro, S02, S04) | n/a (predates D-20, silent not wrong) | n/a | No |
| D-21 | First utility AE/CPS; ~20MW substation batteries | n/a | n/a | n/a | yes (§4.5) | yes (ES19-S02, S03) | n/a | n/a | No |
| D-22 | Charging mix ≥30% solar + utility off-peak | n/a | n/a | n/a | n/a | yes (ES19-S05, "not built") | n/a | n/a (affects 08/09 only) | No |
| D-23 | ROI: <$500/kW net effective, ~3yr payback | n/a | n/a | n/a | n/a | yes (ES19-S04) | n/a | n/a (affects 08 §3c only) | No |
| D-24 | Solar expansion widens price swings | n/a | n/a | yes (§3.2, "specified, not built") | n/a | yes (ES02-S05 note, ES19-S05) | n/a | n/a | No |
| D-25 | 20% SoC floor confirmed (already built) | n/a (covered via K1 base text) | n/a | n/a | yes (§4.2, "confirmation, 2026-09-26") | yes (ES03-S05, ES06-S01) | n/a | n/a | No |
| D-26 | Dispatcher models max discharge flow at every level | yes (K4-extended text) | n/a | yes (§5.7) | n/a | yes (ES05-S10) | n/a | n/a | No |
| D-27 | Guardian independently enforces every flow limit | yes (K4-ext + K15, G-26..33 table) | n/a | yes (§6.7) | n/a | yes (ES06-S09) | n/a | n/a | No |

### 4.2 CONFLICTS (open)

The test plan `04` is being updated on `wp/TP-UPDATE-test-plan` (`30d7f78`, merged into `integ/docs-team` for R3).
That branch adds TS-10-08 (D-11), TS-04-17 and TS-19-13 (D-17), and TS-04-18 and TS-19-12 (D-18), which closes the
`04` halves of items 2–4 below, and its `778370a` closes item 1 (TS-06-07b → G-27, TS-06-12 → G-24, TS-06-13 →
G-13). Item 5 is fixed with this matrix. The items are kept as found at `434d230`, for the record.

1. **D-7 vs `04-mvp-s-test-plan.md`.** TS-06-07b, TS-06-12 and TS-06-13 (§3.6) still label G-21/G-22/G-23
   "supplemental, non-canonical" checks for service-transformer loading, asset-state exclusion and command-rate/
   duplicate detection, and the section's own summary states "the full canonical set" stops at G-01–06/09/13–15/
   19/20. This collides with the now-canonical K14 meanings of G-21 (phase imbalance), G-22 (THD) and G-23
   (frequency/voltage deviation) fixed in `00-invariants.md` and `06`. `04` has had no commits since before either
   post-`434d230` docs commit — it is the stale outlier. **Fix:** renumber/retitle those three test rows off
   G-21/22/23 (they test real, different things) and add explicit K14 property/negative rows for the real G-21..23.
   Under the final numbering, TS-06-07b's service-transformer loading is G-27 (not built at `434d230`; built at R2).

2. **D-11 vs `04-mvp-s-test-plan.md` and `06-service-profiles-and-power-quality.md`.** Neither doc mentions D-11's
   subject matter at all — `04` has zero test coverage for the customer simulator/API/site-ingest/closed-loop path
   (only unrelated `DIST_DEFERRAL` "closed loop" hits), and `06` defines the supporting `MEASURED_FEEDBACK`/
   `CUSTOMER_API` schema fields without ever narrating D-11's activation-gate dependency. Compounding this: the
   decision log's own "(built dark 2026-09-26)" label for D-11 does not match the code — `02b` and `03`
   (ES03-S06) already self-flag this same discrepancy, and it is independently confirmed in §2 above and in
   `13-known-limitations.md` BD-3b. **Fix:** either correct the decision log's label to "mostly not built," or
   build the missing three pieces; either way `04` needs at least a placeholder test-plan section. **At R2** the
   three pieces exist and are dark (ES03-S06 above), so the label now matches the code; the `04` half waits for
   `wp/TP-UPDATE-test-plan` (R3).

3. **D-17 vs `04-mvp-s-test-plan.md` (minor/partial).** TS-04-08/09/10/12 are consistent with D-17 (shortfall
   recorded, `AT_RISK`, never reallocated) but were written before D-17 existed (`04` header: "Written: Friday
   2026-09-25," D-17 is dated 2026-09-26) and none of them literally exercises "keeps delivering at maximum
   feasible," "restores the full commitment at the earliest feasible interval," or "never stops for the rest of the
   window." Silence, not contradiction — but worth a new test row.

4. **D-18 vs `04-mvp-s-test-plan.md` (gap).** No test row anywhere in `04` for the FIXED-vs-NEED basis distinction
   or `R-GRANT-CLOSED-LOOP`, even though the code and its unit tests (ES05-S09, §2) are solid. Needs a new
   TS-xx row.

5. **D-6 vs `01-saturday-delivery-plan.md` (minor).** §0b's RC-1 still reads "energy-sufficiency work **in
   progress**," while `00-invariants.md` and the code (ES05-S08) show it long since built. Unlike other superseded
   passages in `02b`/`02a` (which are explicitly marked "kept for the record"), this one carries no such marker —
   looks like a simple missed edit rather than a deliberate historical snapshot. **Fixed with this matrix:** RC-1
   now reads "energy sufficiency built (D-6, ES05-S08)".

No other decision produced an active contradiction in any of the 7 docs checked. Two harmless labelling slips were
also found and are not counted as conflicts: D-12's decision-log citation says "02b §8 (auth)" where the current
heading is §7 (content is correct); D-19's decision-log "Affects: 04-external-data-integration" refers to
`docs/orchestrator/02-architecture/04-external-data-integration.md`, a different document from this file's `04`
(`07-delivery/04-mvp-s-test-plan.md`) — a numbering trap for future readers, not a doc defect.

---

## 5. Test coverage gaps

### 5.1 Built (or partly built) with no test, weak test, or an unreachable path

- **ES07-S02** — the selector-side enforcement of "no new commitments while a feed is stale" has no code and no
  test at all; only the health/alerting half is built (see §2).
- **ES07-S05** — at `434d230`/`6470cfa`, `DIST_DEFERRAL_OPEN_LOOP` was unreachable in production (its sole caller
  never passed the flag). R2 hotfix v3 (`afb26c2`) sets it after 60 s without SCADA readings and tests that, but
  nothing but the UI reads it.
- **ES08-S04** — `compute_value_added_by_lp`/`compute_forgone_upside` are tested only against fixtures; the real
  data feed (`o/settle/pg_backend.py:661-667,681-686`) is stubbed to `None`/0 in production (built dark).
- **A11 / ES05-S06 / ES07-S04** — the single most-cited non-functional target ("RT cycle p99 < 500ms at 2,000
  hubs") has no automated, CI-gated test. `e2e/perf/capture.py` and `test_capture.py` only unit-test the capture
  tool's own logic plus a dry run; a real, recorded 2,000-hub measurement has never been executed per
  `13-known-limitations.md` OL-2 ("plan only — no load test has been run yet").
- **TS-01-07** (no-duplicated-functions check) is implemented as a CI-gating script, `orchestrator/tools/
  dupcheck.py` (self-cites "test TS-01-07" in its own docstring, runs at `.gitea/workflows/check.yml:54`) — but
  has no dedicated pytest test exercising the checker's own detection logic (e.g. a fixture with a deliberately
  duplicated function to confirm it's caught).
- **ES01-S02** — no mechanism anywhere fails CI on a stale/mismatched shared-contract version, despite being one
  of the story's own acceptance bullets.
- **ES01-S01 / ES01-S05** — both stories' acceptance-critical integration tests
  (`test_es01_s01_migrate_idempotency.py`, `test_es01_s05_restore_and_verify.py`) are DB-only and are **not** run
  by `.gitea/workflows/check.yml` (CI runs `tests/unit` + `tests/property` only, never `tests/integration`).
- **ES02-S03** — NWS watches/warnings ingestion: no code, hence no test, anywhere.
- **ES03-S04** — the "partner call" scenario type has no catalogue entry and no test anywhere in the repo.
- **ES09-S01** — "trace write happens in the same transaction as the causing action" is asserted by the story but
  not literally verified; the actual safety net is a DB unique-constraint conflict on double-insert.
- **ES10-S01** — the 2,000-hub map/clustering performance acceptance criterion has no test; per-hub markers are
  inert (hub lat/lon always `NULL`).
- **ES10-S05** — the cut-line-3 graceful-degrade UI path (no baseline/forgone panel) is untested.
- **ES10-S06** — the demo scenario panel has no UI implementation to test in the first place (see §2).

### 5.2 Tests that exist without a clear story

- **`o/assets/`** (`AssetHealthService` at `o/assets/service.py:84`, plus `calibration.py`, `calibration_ack.py`,
  `state_machine.py`, `runner.py`, `repo.py`) — a substantial drift-detection/WATCH/DEGRADED/calibration workflow
  with dozens of tests (`t/unit/assets/test_service.py` alone has 26; plus `test_calibration.py`,
  `test_calibration_ack.py`, `test_repo_drift_observation.py`, `test_runner.py`, `test_state_machine.py`, and
  `t/integration/assets/test_calibration_e2e.py`) but **no dedicated ES0X-SYY story**. It implements D-8's
  terminology and is exactly the "asset-health drift sweep" `13-known-limitations.md` BD-6 documents as dark
  (`assets.drift_enabled = false`, disabled after a same-day false-positive incident). ES06-S08 only touches its
  edge (G-25 gates a calibration *command*; it doesn't own the health service itself).
- **`t/unit/api/test_csrf.py`, `test_bind_host.py`** — CSRF protection and loopback-bind hardening, both real and
  tested, with no story naming them (closest is ES01's foundational, un-invariant-tagged scope).
- **`t/unit/allocator/test_lexicographic.py`** — its 6 tests are internally named `test_ts_05_10`…`test_ts_05_15`
  and do test live production code (tier-priority allocation inside ES05-S06's real-time cycle), but those same
  TS-IDs mean different things in `04-mvp-s-test-plan.md` today (`DIST_DEFERRAL` PI rating ceiling, `ERCOT_AS` hold
  maintenance, AS-release-default-off, and the K5/K9 property tests) — a test-ID collision, not a missing-test
  problem.
- **`t/unit/selector/test_gate_run_gate.py`** — several tests are labeled `test_ts_05_03_*` and cover gate-commit/
  infeasibility-retry behaviour (ES05-S01 territory), while `04`'s TS-05-03 is actually the non-anticipativity
  property test (itself correctly covered by `test_model_solve_extract.py::test_c16_non_anticipativity_is_structural`).
  Same drift pattern as above.
- **`t/property/test_energy_sufficiency.py`** — tests the guardian's own lease-projection reserve check
  (`check_reserve_floor_over_lease`, a K1 mechanism), not ES05-S08's allocator-level `energy_sufficiency.py` — easy
  to mis-cite as the same test; flagged here so this matrix doesn't repeat that error.

### 5.3 Doc-internal inconsistency (bonus finding, not a code gap)

`04-mvp-s-test-plan.md`'s TS-03-06 text says "trigger each of **the 6** scenario-panel events" but names only 5
(partner call, price spike, feeder overload, comms loss, killed engine), while the story it traces to (ES03-S04)
specifies **7** (adding the mid-delivery commitment-lock demo call and a stale-feed trigger). Three different counts
across two documents for the same feature — worth reconciling next time either doc is touched.

---

*Every file:line and test name above was confirmed against branch `wp/docs-lane` @ `d8faa0b` by direct `Read`/
`Grep`. No test in this file was executed; "test exists" means the function is defined and named as cited, not
that a live run currently passes.*
