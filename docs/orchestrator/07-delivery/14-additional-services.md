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
priority bucket. Wiring line for the optimizer owner: `opengrid.selector.gate._CATEGORY_BY_SERVICE_TYPE`
needs `"PJM_CAPACITY": "MARKET"`.

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

**Tier and priority.** `T2` — committed for the deployment window, not the top firm tier. Wiring line for
the optimizer owner: `_CATEGORY_BY_SERVICE_TYPE["MOBILE_STORAGE"] = "FIRM"` (a deployment window is a
committed obligation for its duration, same as `DATA_CENTER`/`PARTNER_CAPACITY`).

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

**Tier and priority.** `T2`. Wiring line for the optimizer owner: `_CATEGORY_BY_SERVICE_TYPE["LARGE_LOAD"]
= "FIRM"` (a committed firming/ride-through capacity contract, same bucket as `DATA_CENTER`).

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
  `tests/unit/profiles/test_svc_scenarios.py`).
- `dev/seed/services_seed.sql`: one demo customer/contract per new service type, same shape as
  `dev/seed/customer_services_seed.sql`. Not run by CI; apply by hand after migration 0025.

**Wiring lines for other owners (not applied by this agent):**

| Owner | Path | Change needed |
|---|---|---|
| SETTLE | `opengrid/settle/baselines.py` | `METER_SOURCE_BY_SERVICE` is missing `PJM_CAPACITY`/`MOBILE_STORAGE`/`LARGE_LOAD` — merge in `services_extra.EXTRA_METER_SOURCE_BY_SERVICE` (or add the three keys directly). Without this, `settle()` raises `KeyError` for any obligation on these service types. `tests/unit/profiles/test_data_center_registered.py`'s `set(METER_SOURCE_BY_SERVICE) == set(get_args(ServiceType))` assertion already fails until this lands. |
| SETTLE | `opengrid/settle` orchestration (`settle()`) | Call `services_extra.pjm_non_performance_charge(...)` when `ctx.service_type == "PJM_CAPACITY"` and the interval falls inside a declared emergency performance hour, and append the result as an extra `LD_PENALTY`-shaped draft (a new `SettleBackend` accessor for the emergency-hour window/flag is settle's to add). |
| OPTIMIZER | `opengrid/selector/gate.py` | `_CATEGORY_BY_SERVICE_TYPE` needs `"PJM_CAPACITY": "MARKET"`, `"MOBILE_STORAGE": "FIRM"`, `"LARGE_LOAD": "FIRM"`. Until added, the `.get(row["service_type"], "MARKET")` fallback silently treats all three as `MARKET` priority, correct only for `PJM_CAPACITY`. |
| DISPATCH | `allocator/`, `engine/` | No special-casing expected to be required: `CAPACITY_HOLD` (`ERCOT_AS`) and `EVENT_SCHEDULE_TRACKING` (`PARTNER_CAPACITY` EVENT, `ERCOT_AS` NCLR) are both already-exercised control primitives: the three new profiles reuse them rather than introducing a new one, so no new control-loop shape is dispatched. Please confirm on review. |
| FLEET-SIM | `integration-sims/src/ogsim/control/catalogue.py` | New anomaly-type catalogue entries so `Injector`/`run_scenario` can execute the three `svc-*.yaml` scenarios: `pjm_emergency_performance_event` (owner `market`), `mobile_deployment_start` / `mobile_deployment_relocate` (owner `fleet`), and optionally a dedicated `large_load_curtailment_request` (owner `customer`) to replace the current stand-in reuse of `load_step_datacenter` in `svc-large-load.yaml`. |
| FLEET-SIM | `integration-sims/tests/test_scenarios.py` | `test_all_six_shipped_scenarios_load_without_error`'s hardcoded `len(scenarios) == 6` now undercounts — the `scenarios/` directory holds 9 files after this work package. |
| CUSTOMER | `ogsim/customer/` (directory does not exist yet) | Three new simulated customer operators, matching the pattern already used for `DATA_CENTER`/`PIPELINE_AC`: a simulated PJM RPM capacity customer (drives `svc-pjm-capacity.yaml`'s emergency declarations against `og-cust-pjm`), a mobile-deployment site customer (`og-cust-mobile`, tracks arrival/relocation against `dev/seed/services_seed.sql`'s seeded contract), and a large-load customer (`og-cust-largeld`) issuing `CUSTOMER_API` curtailment/ride-through signals. |

No migration, `ServiceType` enum value, or DDL CHECK was touched by this document or its accompanying
files — all three service types and the DB CHECK widening are migration 0025, owned by MARKET-MODEL.
