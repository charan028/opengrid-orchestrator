# 14 — Additional services: PJM_CAPACITY, MOBILE_STORAGE, LARGE_LOAD

Status: **Owner decision 2026-09-26 13:30 CT.** Build PJM_CAPACITY, MOBILE_STORAGE and LARGE_LOAD as
service profiles with simulator support and optimizer/settlement mappings. Extends `06-service-profiles-
and-power-quality.md` (the `ServiceProfile`/`PowerQualityEnvelope` model, §1-2, and the priority-profile
pattern of §4) exactly the way `06` itself extends `03-decision-engine.md` — additive only, no existing
field, enum value or DDL column changed. `ServiceType` gains all three literals via migration 0025
(MARKET-MODEL owned); this document, the three profile templates and their settlement/simulation wiring
are the SERVICES agent's delivery against that decision.

**PJM stays simulated.** There is no real PJM membership, capacity-market registration, eRPM/eDART feed
or settlement connection in this build, and none is scheduled. Every PJM-shaped signal in this system —
the emergency performance-hour declaration, the RPM capacity clearing price, the non-performance charge
rate — comes from a scenario file (`integration-sims/scenarios/svc-pjm-capacity.yaml`) or a configured
constant, never a live PJM interface. §4 below states exactly what would have to change for that to stop
being true.

## 1. PJM_CAPACITY

**What it is.** A simulated PJM Reliability Pricing Model (RPM) capacity-performance commitment: the
resource is paid for a capacity reservation, must ramp to the committed kW within a short deadline when a
(simulated) emergency performance hour is declared, and is charged a non-performance charge for any
shortfall during that declared hour. `03-decision-engine.md` §2.6's front-matter note already anticipated
the shape: *"`PJM_CAPACITY` dispatches toward meter net load and is non-firm by default."*

**Control law.** `control_primitive = CAPACITY_HOLD` (§1.2 of `06`) — reserve power against a call that may
or may not arrive, the same primitive `ERCOT_AS`'s ring-fence uses, dispatching toward `target_scope =
SITE_METER` net load rather than a hub or bank. `setpoint_source = ISO_INSTRUCTION`: in this build, the
"ISO" issuing that instruction is always the scenario runner (`ogsim.control.scenarios`), never a real PJM
feed — `[control].emergency_call_source = "SIMULATED_ISO_INSTRUCTION"` in the profile template says this
explicitly so nothing downstream mistakes it for a live market signal. `response_time_s = 600.0` mirrors
PJM's real Capacity Performance requirement (full output within 10 minutes of a declared event) even
though the *declaration* itself is simulated — this keeps the ramp-law realistic for a future real-market
swap-in.

**PQ.** Grid-code minimum only, the same §4.c pattern `ERCOT_ENERGY` uses: `pq_aware_selection = false`,
only `phase_config` is set in the profile's `[pq_envelope]` (every other field takes the fleet-default
value), and `DEGRADED` inverters remain eligible provided they still meet the grid-code-minimum envelope
(§5.5.3's arbitrage row, reused here for the same reason — PJM_CAPACITY should not compete with
`DATA_CENTER`/`PIPELINE_AC` for the fleet's highest-quality units).

**Tier and priority.** `T3`, non-firm by default (03 §2.6) — it sits below the firm services in the F2
priority bucket. Wired: `opengrid.selector.gate._CATEGORY_BY_SERVICE_TYPE["PJM_CAPACITY"] = "MARKET"`
(optimizer owner, confirmed in place 2026-09-26).

**M&V and settlement.** `mv_method = DIRECT_HUB_METER`; `settlement_metric = capacity_payment_x_pf` (the
routine, non-emergency-hour line, computed exactly like every other capacity profile's generic
`CAPACITY_PAYMENT x performance_factor` — `opengrid.settle.billing.draft_invoice_lines`'s existing `else`
branch needs no change for this). The non-performance charge is a SEPARATE, PJM-specific computation
(`opengrid.settle.services_extra.pjm_non_performance_charge`): shortfall kWh × a configured
`non_performance_rate_per_kwh`, charged only when `is_emergency_performance_hour` is true, never doubled
against the ordinary capacity payment. Real PJM prices non-performance against Net CONE and the Balancing
Ratio over a rolling 40-hour window; a single configured rate stands in for that calculation here.

**What real-market onboarding would need.** PJM membership and a Load-Serving-Entity or Capacity Market
Seller registration; an RPM auction participation agreement; a real-time eRPM/eDART integration for
emergency performance-hour declarations (replacing the scenario-file trigger entirely); a metering
agreement meeting PJM's meter-data-submission requirements (replacing `DIRECT_HUB_METER` simulation with a
PJM-accepted meter); the real Net-CONE/Balancing-Ratio non-performance charge formula (replacing the
configured flat rate); and a settlement feed from PJM's billing system to reconcile against
`og.invoice_line` rather than treating this system's own computation as authoritative.

## 2. MOBILE_STORAGE

**What it is.** A trailer or mobile battery deployed to a customer site for a scheduled window, then
relocated to a different site for its next deployment. This is the one profile in `config/
service_profiles/` whose delivery location is not fixed for the contract's life: each deployment carries
its own `site_id`, and the instantiated `og.service_profile.feedback_signal_ref` (`site_meter:{site_id}:
p_kw`) is re-filled at every deployment, not once at contract signing. State of charge on arrival is the
deployment's starting condition for M&V — it is not a `ServiceProfile`/`PowerQualityEnvelope` schema field
(neither schema has a SoC concept; both are per-contract-instance PQ/control declarations), so it is
carried as deployment metadata alongside the M&V record, the same way `og.hub_inverter_pq.last_estimated_at`
distinguishes "characterized" from "default" without being a control-law parameter itself.

**Control law.** `control_primitive = EVENT_SCHEDULE_TRACKING` — track a declared deployment profile to a
compliance band, the same primitive `PARTNER_CAPACITY`'s `EVENT` variant and `ERCOT_AS`'s
`NCLR_DEPLOYMENT_BAND` use (03 §2.6). `setpoint_source = PLAN` (the deployment's day-ahead/intraday
delivery schedule); `target_scope = SITE_METER`, `mv_method = AMI_INTERVAL` — the same source class
`DATA_CENTER` uses for its site meter.

**PQ.** Grid-code minimum at the delivery site (only `phase_config` set); `required_ride_through_class =
CATEGORY_II` (looser than `DATA_CENTER`'s Category III — a relocatable trailer's own inverter
characterization, not a fixed hub bank, is what is being screened here) and `pq_aware_selection = false`.

**Home-station model and charging (OWNER DECISION D-31, 2026-09-26,
`docs/orchestrator/07-delivery/11-decision-log.md`).** A mobile unit is **never charged from the fleet** —
not from home batteries, not from any other fleet asset, not from a customer deployment site's hubs. It
charges only while parked at its registered **home station**: a depot near a distribution or power
substation, from that station's own grid connection, priced at that station's own zone/tariff (never the
deployment site's). While charging, the unit is at its home station and unavailable for service; it
becomes available again only after it leaves for a deployment with whatever state of charge it left with.

There is no `og.mobile_home_station` DB table yet, so this is modeled config-first (per the lead's
2026-09-26 R3.1 instruction) in `config/service_profiles/mobile_storage_home_stations.toml`:
- `[[home_station]]`: one row per depot — `home_station_id`, `zone` (the ERCOT load zone or regulated
  utility zone billing that depot), `utility_id` (empty = free-market), `lat`, `lon`, `charger_kw`.
- `[[assignment]]`: binds a mobile unit's `bank_id` (`og.bank.bank_id`/`og.hub.hub_id` — a mobile unit is a
  single-hub bank) to its `home_station_id`. A unit's home station changes only by an explicit
  re-assignment; a deployment relocates the unit's *customer site*, never its home station.
- `mobile_storage.toml` itself carries the profile-level statement: `[home_station].
  chargeable_from_fleet = false`, `[home_station].registry` pointing at the file above, `[home_station].
  charging_priced_at = "HOME_STATION_TARIFF"`, and `[control].charge_source = "HOME_STATION_ONLY"`.

**Migration requested (lead to assign a number):** `og.mobile_home_station` (home_station_id PK, zone,
utility_id nullable, lat, lon, charger_kw) + `og.hub.is_mobile boolean NOT NULL DEFAULT false` +
`og.hub.home_station_id text NULL REFERENCES og.mobile_home_station` (mirrors the two config tables above
1:1, additive only) + a new `og.mobile_deployment` (deployment_id PK, bank_id, home_station_id, site_id
NULL, starts_at, ends_at NULL) recording each charge-at-station/deploy-to-site leg — the real source for
"which intervals is this unit at its home station" once it exists.

**Simulator (`integration-sims/scenarios/svc-mobile-storage.yaml`, updated for D-31).** The timeline is
deliberately HOME STATION CHARGE → DEPLOY → RELOCATE BACK TO HOME STATION → HOME STATION CHARGE → DEPLOY:
never site-to-site relocation, never a charge step anywhere but at the home station. `mobile_home_station_
charge` is the only step type that may raise SoC; `mobile_deployment_start`/`mobile_deployment_relocate`
never carry a `target_soc_pct`. `tests/unit/profiles/test_svc_scenarios.py` enforces this shape.

**Tier and priority.** `T2` — committed for the deployment window, not the top firm tier. Wired:
`opengrid.selector.gate._CATEGORY_BY_SERVICE_TYPE["MOBILE_STORAGE"] = "FIRM"` (optimizer owner, confirmed
in place 2026-09-26).

**Wiring needed for D-31 enforcement (OPTIMIZER/DISPATCH/SAFETY):**

| Owner | Status | Detail |
|---|---|---|
| OPTIMIZER | **Done** (confirmed 2026-09-26). | `opengrid.selector.types.BankSnapshot.is_mobile`/`.home_station_intervals`/`.charging_allowed(t)` gate `max_charge_kw` to 0 outside home-station intervals (`model.py`'s `charge_cap_kw = ... if bank.charging_allowed(t) else 0.0`); `model.py` also refuses to let a non-`is_mobile` bank serve a MOBILE_STORAGE obligation. The exact source/join for `is_mobile`/`zone`/`home_station_intervals` (this file's config, above) was sent to OPTIMIZER directly 2026-09-26 R3.1: `is_mobile`/`zone` from the config now; `home_station_intervals = None` (fail closed — never charge) until the `og.mobile_deployment` migration above lands. |
| DISPATCH | **Wiring line, not applied here** (`allocator/`, `engine/` are DISPATCH-owned). | Any real-time water-fill/substitution/rebalance path in `allocator/`/`engine/` that can independently propose a CHARGE setpoint (i.e. not just replaying the optimizer's plan) must consult the same predicate before ever proposing to charge a hub: `is_mobile` and (if mobile) "is this hub's bank inside its `home_station_intervals` this cycle" — mirroring `BankSnapshot.charging_allowed(t)` exactly, not re-derived. Since `Hub`/`HubState` (`opengrid.core.models.platform`, platform/LIVE-PATH owned) carry no `is_mobile` field today, DISPATCH's real-time path has no live signal to check against until the `og.hub.is_mobile`/`home_station_id` migration above lands — flagging this as a gap alongside the migration ask, not a silent risk. |
| SAFETY | **Wiring line, not applied here** (`guardian/` is SAFETY-owned). | Add guardian check **G-35** (next free number after G-34, `opengrid.guardian.flow_checks`/`service.py`'s numbering): item-level, added to `service.py`'s `_ITEM_LEVEL_RULES`, VETOing any batch item where the hub is mobile and the proposed setpoint is a charge (negative real power, matching this codebase's sign convention) unless the hub's home-station-arrival status (from telemetry once wired, or the same config source in the interim) says it is currently at its assigned home station. Missing/unknown status fails closed (VETO), the same stance every other guardian check in this family takes (K1/G-01's stale-data pattern, G-33's territory block). Reason code suggestion: `R-MOBILE-CHARGE-AWAY-FROM-HOME-STATION`. This is the independent, production-side enforcement of the same rule DISPATCH enforces primarily and OPTIMIZER enforces at planning time (the K2 primary-check/independent-check pattern already used throughout `guardian/`). |

**M&V and settlement.** `settlement_metric = deployment_capacity_payment`, plus a second line
`deployment_availability_pct` — the MOBILE_STORAGE analogue of `DATA_CENTER`'s `pq_compliance_pct`, but
measuring on-site energization and compliance-band tracking rather than PQ envelope compliance (this
profile has no PQ compliance line — grid-code minimum only).
`opengrid.settle.services_extra.mobile_deployment_availability_pct(energized_minutes, window_minutes)`
computes it, clipped to `[0, 1]`.

**What real-market onboarding would need.** Nothing PJM-shaped — MOBILE_STORAGE is not a wholesale-market
product, it is a bilateral deployment contract. The main real-world gap is operational, not
regulatory/market: a dispatch/logistics integration for scheduling the physical relocation (truck routing,
site access, interconnection permission at the new site) that this build has no analogue for — the
simulator (`integration-sims/scenarios/svc-mobile-storage.yaml`) models only the electrical/M&V side of a
deployment and relocation, not the trucking.

## 3. LARGE_LOAD

**What it is.** Firming or ride-through support for a large flexible load — a crypto or data load
curtailing with battery support, or an ERCOT large-load interconnection — that follows a load-following
schedule driven by the customer's own signal. `06-service-profiles-and-power-quality.md` §4.b already
states the defining contrast: *"`LARGE_LOAD` ... is a load-offset event profile with no PQ obligation
beyond grid-code minimum"* — this is the explicit reason `DATA_CENTER` had to become its own profile
rather than a `LARGE_LOAD` variant, and this document is where `LARGE_LOAD` itself finally gets built out
to the same level of detail.

**Control law.** `control_primitive = EVENT_SCHEDULE_TRACKING`; `setpoint_source = CUSTOMER_API` — "`LARGE_
LOAD` contracted stress events from the customer's signal" (03 §2.6, §8.6.3) drives the target directly,
unlike `DATA_CENTER`'s `MEASURED_FEEDBACK` closed loop. `target_scope = SITE_METER`; `response_time_s =
5.0` (battery-support ride-through is fast and event-triggered, not a 10-minute capacity-market deadline
like `PJM_CAPACITY`).

**PQ.** Grid-code minimum only, per §4.b's explicit statement — only `phase_config` is set,
`pq_aware_selection = false`. It still requires `CATEGORY_III` ride-through: grid-code minimum does not
waive the requirement that the load's own battery support must not trip during the grid event that likely
triggered the curtailment/ride-through call in the first place (the same logic `DATA_CENTER` uses for its
own Category III requirement, §4.b).

**Tier and priority.** `T2`. Wired: `_CATEGORY_BY_SERVICE_TYPE["LARGE_LOAD"] = "FIRM"` (optimizer owner,
confirmed in place 2026-09-26; a committed firming/ride-through capacity contract, same bucket as
`DATA_CENTER`).

**M&V and settlement.** `settlement_metric = capacity_payment_x_performance`, a single line — no PQ
compliance line (§4.b's contrast means there is nothing PQ-specific to bill beyond the grid-code-minimum
floor every profile already meets). `opengrid.settle.services_extra.large_load_curtailment_compliance_pct
(delivered_kw, scheduled_kw)` computes the interval's kW-only compliance share against whatever was
scheduled for that event, `None` when no event was in progress.

**What real-market onboarding would need.** For the ERCOT large-load-interconnection variant specifically:
ERCOT's Large Load Interconnection process (currently under active ERCOT/PUCT rulemaking as of this
writing) and its associated telemetry/curtailment-compliance requirements, which are stricter and more
codified than this profile's simulated `CUSTOMER_API` signal; for the crypto/data-load-curtailment variant,
a direct telemetry integration with the customer's own load-management system (replacing the simulated
`load_step_datacenter`-shaped trigger in `integration-sims/scenarios/svc-large-load.yaml` with the real
signal), and a bilateral curtailment-performance settlement agreement with the customer.

## 4. Simulation and wiring summary

Everything below is either delivered in this work package or an explicit wiring line for another agent's
owned path (BUILD.md ownership map) — the SERVICES agent cannot edit those paths itself.

**Delivered here:**

- `config/service_profiles/{pjm_capacity,mobile_storage,large_load}.toml` (+ `README.md` update), each
  validated against `interfaces/contracts/{service_profile,pq_envelope}.schema.json` the same way
  `data_center.toml` is (`tests/unit/profiles/test_{pjm_capacity,mobile_storage,large_load}_profile.py`).
- `orchestrator/src/opengrid/settle/services_extra.py` (new module): `pjm_non_performance_charge`,
  `mobile_deployment_availability_pct`, `large_load_curtailment_compliance_pct`, plus
  `EXTRA_METER_SOURCE_BY_SERVICE` for the settle owner to merge in (`tests/unit/settle/
  test_services_extra.py`).
- `integration-sims/scenarios/svc-{pjm-capacity,mobile-storage,large-load}.yaml` (structural tests:
  `tests/unit/profiles/test_svc_scenarios.py`; `svc-mobile-storage.yaml` updated 2026-09-26 for D-31's
  home-station-only charging).
- `config/service_profiles/mobile_storage_home_stations.toml` (new, D-31): the config-first home-station
  registry and unit assignments (tests: `tests/unit/profiles/test_mobile_storage_profile.py`'s `test_d31_*`).
- `dev/seed/services_seed.sql`: one demo customer/contract per new service type, same shape as
  `dev/seed/customer_services_seed.sql`. Not run by CI; apply by hand after migration 0025.

**Wiring lines for other owners (not applied by this agent):**

| Owner | Path | Status / change needed |
|---|---|---|
| SETTLE | `opengrid/settle/baselines.py` | **Done** (confirmed 2026-09-26): `METER_SOURCE_BY_SERVICE` now spreads in `services_extra.EXTRA_METER_SOURCE_BY_SERVICE`. |
| SETTLE | `opengrid/settle` orchestration (`settle()`) | Still open: call `services_extra.pjm_non_performance_charge(...)` when `ctx.service_type == "PJM_CAPACITY"` and the interval falls inside a declared emergency performance hour, and append the result as an extra `LD_PENALTY`-shaped draft (a new `SettleBackend` accessor for the emergency-hour window/flag is settle's to add). |
| OPTIMIZER | `opengrid/selector/gate.py`, `opengrid/selector/types.py`, `opengrid/selector/model.py` | **Done** (confirmed 2026-09-26): `_CATEGORY_BY_SERVICE_TYPE` has all three entries; D-31's `BankSnapshot.is_mobile`/`.home_station_intervals`/`.charging_allowed(t)` are wired into `model.py`'s charge-cap and mobile-obligation-eligibility logic. The exact `is_mobile`/`zone`/`home_station_intervals` source (`mobile_storage_home_stations.toml`, §2 above) was sent to OPTIMIZER directly 2026-09-26 R3.1. |
| DISPATCH | `allocator/`, `engine/` | Still open (D-31, §2 above): any real-time water-fill/substitution/rebalance path that can independently propose a CHARGE setpoint must consult the same `is_mobile` + "at home station this cycle" predicate before proposing to charge a hub — never re-derived, mirroring `BankSnapshot.charging_allowed(t)`. Blocked on the same `og.hub.is_mobile`/`home_station_id` migration requested in §2 (no live per-hub signal exists yet outside the optimizer's own config read). For PJM_CAPACITY/LARGE_LOAD: no special-casing expected — `CAPACITY_HOLD` (`ERCOT_AS`) and `EVENT_SCHEDULE_TRACKING` (`PARTNER_CAPACITY` EVENT, `ERCOT_AS` NCLR) are both already-exercised control primitives reused here, not a new control-loop shape. Please confirm on review. |
| SAFETY | `guardian/` | Still open (D-31, §2 above): new item-level check **G-35** (next free number after G-34) VETOing any batch item that charges a mobile hub away from its home station — added to `service.py`'s `_ITEM_LEVEL_RULES`, fail-closed on missing/unknown home-station status (mirrors G-01/G-33's stale-data stance). Suggested reason code `R-MOBILE-CHARGE-AWAY-FROM-HOME-STATION`. |
| FLEET-SIM | `integration-sims/src/ogsim/control/catalogue.py` | New anomaly-type catalogue entries so `Injector`/`run_scenario` can execute the three `svc-*.yaml` scenarios: `pjm_emergency_performance_event` (owner `market`), `mobile_deployment_start` / `mobile_deployment_relocate` / `mobile_home_station_charge` (owner `fleet`; the D-31 charging step — the fleet/hub charge model must never route power into an `is_mobile` hub at any other step), and optionally a dedicated `large_load_curtailment_request` (owner `customer`) to replace the current stand-in reuse of `load_step_datacenter` in `svc-large-load.yaml`. |
| FLEET-SIM | `integration-sims/tests/test_scenarios.py` | `test_all_six_shipped_scenarios_load_without_error`'s hardcoded `len(scenarios) == 6` now undercounts — the `scenarios/` directory holds 9 files after this work package. |
| CUSTOMER | `ogsim/customer/` (directory does not exist yet) | Three new simulated customer operators, matching the pattern already used for `DATA_CENTER`/`PIPELINE_AC`: a simulated PJM RPM capacity customer (drives `svc-pjm-capacity.yaml`'s emergency declarations against `og-cust-pjm`), a mobile-deployment site customer (`og-cust-mobile`, tracks arrival/relocation/home-station-charging against `dev/seed/services_seed.sql`'s seeded contract), and a large-load customer (`og-cust-largeld`) issuing `CUSTOMER_API` curtailment/ride-through signals. |
| LEAD | migration number | Requested (§2 above): `og.mobile_home_station` + `og.hub.is_mobile`/`home_station_id` + `og.mobile_deployment`, additive only, to replace the config-first D-31 model with a real one DISPATCH/SAFETY can read live. |

No migration, `ServiceType` enum value, or DDL CHECK was touched by this document or its accompanying
files — all three service types and the DB CHECK widening are migration 0025, owned by MARKET-MODEL. The
D-31 home-station tables above are a SEPARATE, not-yet-numbered migration request.
