# MVP-S Canonical Invariants (K1–K15)

This is the single source of truth for invariant IDs. Every MVP-S document (02a, 02b, 03, 04) and every test uses
these IDs and meanings. Principles P1–P8 are defined in `../06-reviews/06-first-principles-review.md` §1.

**Code references.** A `file:line` marked R2 (or given in a "Status at main `6470cfa`" block) is at `main` `6470cfa`. Every other `file:line` was verified at `434d230` and may have moved since.

| ID | Invariant | Principle | Enforced at (primary → independent check) | Fail-safe | Proven by |
|---|---|---|---|---|---|
| **K1** | **Homeowner reserve.** A hub's SoC is never driven below its backup reserve by any command (L1, hard, never priced) | P2 | selector/allocator constraint → guardian G-01 → hub firmware (sim) | Hold; the hub refuses discharge below reserve | Property test; live counter "reserve breaches = 0" |
| **K2** | **One buyer.** Per hub, bank and interval, $\sum$ reservations ≤ capability; each kW/kWh backs at most one obligation. The ledger has a single writer | P3 | ledger (single writer: engine) → guardian G-09 (reads ledger version) | Reject the reservation | Property test; "kWh sold twice = 0" |
| **K3** | **Sole signer.** No command moves MW unless the guardian has signed it (Ed25519). The hub verifies signature and key | P2 | guardian → hub signature check | Drop the command; the hub holds its lease | Negative test: an unsigned or forged command is rejected |
| **K4** | **Physical envelope.** Per hub, bank and feeder, P, kVA and ramp limits hold. There are no synchronized fleet steps (stagger, fleet ramp cap), and firm events obey a feeder/bank ramp ceiling | P1 | allocator → guardian G-02..G-06 | VETO the batch; re-solve without the vetoed hubs | Property test; guardian negative tests |
| **K5** | **Grid authority.** L2 utility/ISO instructions are hard constraints and are never traded for commercial value | P2 | allocator (equality/limit) → guardian G-15 | Substitute other hubs, else `AT_RISK` + notice | Scenario test |
| **K6** | **Command freshness.** Every command carries sequence, epoch, precondition and lease. Stale, duplicate, out-of-order or wrong-epoch commands are rejected; epochs increase strictly | P2 | guardian → hub | Reject with reason; resync | Property test on sequence/epoch |
| **K7** | **Degrade, don't trip.** TIMEOUT ≠ VETO ≠ STOP. On loss of an input or module, fall back hold → schedule → local autonomy; never an unplanned step to 0 for firm obligations | P6 | guardian timeout handling; allocator; hub lease expiry | Hold the last signed setpoint until the lease expires | Chaos tests (kill each process) |
| **K8** | **Stop authority.** A scoped safe stop works when the engine is down. The stop key can only stop, never release; release needs the guardian plus an operator | P2 | safestop (independent process, stop-only key) → hub | Ramp to zero within the scope's time | Chaos + negative test |
| **K9** | **One loop per quantity.** Exactly one integrating controller regulates a given physical quantity (e.g., bank kVA); others treat it as feed-forward | P1/P6 | allocator (DIST_DEFERRAL PI) → guardian G-03 | Outer loop in feed-forward only | Unit + scenario test |
| **K10** | **Trace before act.** No command is signed unless its decision pre-image is durably written to the trace | P8 | guardian (checks the trace write) | Refuse to sign → hold | Property test: #commands ≤ #traced decisions |
| **K11** | **Verifiable track record.** Every event is traced in a per-stream SHA-256 hash chain. Retention per event class is configurable, and pruning keeps checkpoints so `verify()` still passes | P8 | trace module | Local journal; alert if unanchored | Property test: verify after random prune |
| **K12** | **Time quality.** The guardian refuses to sign when its own clock quality (offset from NTP) exceeds its limit, because leases, epochs and jitter depend on time | P1/P6 | guardian G-20 (time quality) | Hold | Negative test with a skewed clock |
| **K13** | **Commitment lock.** A committed obligation's allocation $\hat y_{o,b,t}$ is never reduced or reassigned to a different obligation before fulfilment or its next agreed re-nomination point. The only exceptions are L0, L1, L2 or physical infeasibility (reason codes `R-COMMIT-LOCK-OVERRIDE-L0/L1/L2`, `R-COMMIT-LOCK-INFEASIBLE`) or the audited §7.4 release (`R-AS-RELEASE`, default off). Substituting hubs within the same obligation is allowed (`R-SUBSTITUTION`) | P4 | selector (C24 freeze) + allocator (subtract ŷ at S2) + ledger release check → guardian G-19 | Shortfall recorded against the same obligation, never a reallocation | Property test: random arrivals and prices; FR-ARB-014 fixtures |

**Additions (2026-09-26, owner decisions):**

- **K13, best effort after a shortfall (D-17).** A mid-window SHORTFALL never stops dispatch for the rest of the window.
  - The obligation stays served at the maximum feasible kW, as fast and at as high quality as possible: substitution
    first, then restoring the full commitment as soon as the constraint clears.
  - It is marked AT_RISK while short, and settlement uses the actual delivered energy.
  - The shortfall is recorded against the same obligation; capacity is never reallocated.
  - **Built:** `orchestrator/src/opengrid/engine/escalation.py:1-91` (sustain-cycle counter that turns a persistent
    L0/L1/L2/infeasible signal into the `DELIVERING → SHORTFALL` edge, never a stop); best-effort continuation at
    `orchestrator/src/opengrid/allocator/cycle.py:185-192,248-257` (`best_effort_reason`); reason codes at
    `orchestrator/src/opengrid/core/reasons.py:49-60`; AT_RISK flagging at
    `orchestrator/src/opengrid/contracts/__init__.py:220-239` (`set_obligation_at_risk`, traces `AT_RISK`/
    `AT_RISK_CLEARED`) and `orchestrator/src/opengrid/engine/gateways.py:654-664`; the mid-window continuation
    query at `orchestrator/src/opengrid/engine/gateways.py:111-117,213-231` (`o.state IN ('COMMITTED','DELIVERING',
    'SHORTFALL')`); settlement on actual delivered energy is inherent to `meter_interval.delivered_kwh`
    (`orchestrator/src/opengrid/settle/__init__.py:125-132`).
- **K13, commitments are over a period (D-18).** A commitment is fulfilled over its window either on a **fixed** basis
  (scheduled kW) or on a **need** basis. Need basis applies to measured closed-loop profiles such as DATA_CENTER and
  PIPELINE_AC, where the committed kW is a **reserved maximum**:
  - the reserved capacity stays locked to that obligation and is never reassigned (K2/K13);
  - delivered kW follows the customer's measured need (`R-GRANT-CLOSED-LOOP`).
  - The guardian (G-19) accepts a need-basis grant below the reserved maximum only after its own check that the
    obligation's profile is `MEASURED_FEEDBACK` and that the unused reservation isn't granted to any other obligation.
  - The invariant checker counts need-basis delivery below the maximum as compliant.
  - **Built:** `og.service_profile.setpoint_source` incl. `MEASURED_FEEDBACK` at
    `orchestrator/migrations/0010_service_profile.sql:55-56,71-72`; `R-GRANT-CLOSED-LOOP` at
    `orchestrator/src/opengrid/core/reasons.py:42`; the guardian's own check at
    `orchestrator/src/opengrid/guardian/checks.py:275-305` (`NEED_BASIS_SETPOINT_SOURCE`, `check_g19_need_basis`),
    wired into signing at `orchestrator/src/opengrid/guardian/service.py:731-746,777-779`; need-basis settlement at
    `orchestrator/src/opengrid/settle/__init__.py:134-146` and `orchestrator/src/opengrid/settle/performance.py:43-46`
    (`is_need_basis_compliant`); the checker's compliant classification at
    `orchestrator/src/opengrid/invariants/checks.py:341-343` and
    `orchestrator/src/opengrid/invariants/queries.py:191-205`.
- **K4, extended: flow-limit hierarchy (D-26, D-27).** The dispatcher models, and the guardian independently
  re-checks, discharge/charge flow limits at every level, both directions (reverse flow included); missing data
  fails closed:
  - (a) hub/asset power at the derated $P_{max}(SoC,T)$: continuous rating, with the peak rating usable only within
    its duration budget and a lease no longer than the peak duration;
  - (b) per-home meter export/import, net of the home's own load, against the interconnection export limit and the
    service rating;
  - (c) service-transformer and feeder loading, both directions; no reverse flow at a feeder head or substation
    unless the utility has confirmed bidirectional settings;
  - (d) substation-asset POI import/export and substation-transformer limits;
  - (e) hub, asset, fleet and feeder ramps, with no synchronized fleet steps (unchanged from the original K4 above).
  - **Status at main `6470cfa` (R2), per family:**
    - (a) **Built in the real-time dispatcher and the guardian; not in the selector.** The allocator derates
      every cycle, always on (`orchestrator/src/opengrid/allocator/flow_limits.py:49-68` `derated_discharge_kw`,
      `cap_hub` `:94-105`, applied at `orchestrator/src/opengrid/allocator/cycle.py:123-135`), on the shared curve
      `orchestrator/src/opengrid/core/limits.py:175-207`; an unknown cell temperature derates to 0.5
      (`core/limits.py:126`). The per-unit cap reads `og.hub.units` (migration 0032, `core/limits.py:77-90`). The
      guardian re-checks with G-02 (changed, `orchestrator/src/opengrid/guardian/flow_checks.py:133`) and G-31
      (`flow_checks.py:88`). The allocator never plans above continuous power. **Known defect:** the guardian reads
      the peak budget under the wrong field name (`guardian/mqtt_io.py:36` `peak_budget_kws`; the wire field is
      `peak_power_budget_kws`, `orchestrator/src/opengrid/core/models/mqtt.py:44`), so G-31 vetoes every
      above-continuous setpoint (`core/limits.py:283-284`): safe, but the peak allowance cannot be used.
    - (b) **Guardian built; allocator partial.** G-26 checks both sides, net of home load
      (`flow_checks.py:164`), with static defaults of 20 kW export and 48 kW service when a premise has no row
      (`orchestrator/src/opengrid/guardian/config.py:106-107`). The allocator caps export only
      (`allocator/flow_limits.py:78-88`), behind `[allocator.flow_limits].enabled = true`
      (`orchestrator/config/orchestrator.toml:124-126`), and no repo seed sets `og.hub.export_limit_kw`, so it has
      nothing to apply yet.
    - (c) **Guardian built; allocator built, without data.** G-27 checks each service transformer over all its
      members (`flow_checks.py:219`); a hub with no transformer is a group of one at
      `[guardian.flow].unmapped_xfmr_kva_per_home = 25` (`orchestrator.toml:143`). G-28 checks feeder thermal
      and reverse flow (`flow_checks.py:261`) with defaults of 10 MW × 0.95 thermal and 3 MW reverse
      (`guardian/config.py:113-116`). The allocator's transformer and feeder budgets (`allocator/flow_limits.py:108-182`)
      need `og.service_transformer`/`og.feeder_limit` rows (migration 0029), which no repo seed writes. Bank kVA
      (G-03) is unchanged.
    - (d) **Guardian built, idle.** G-29 (substation limit, and a substation asset's POI, `flow_checks.py:296`)
      evaluates nothing in the shipped topology: no `og.asset` row has a `bank_id`. No code dispatches a
      substation asset (ES19-S03).
    - (e) **Guardian built; dispatcher per hub only.** G-04, G-05 (fleet cap, plus a new gross synchronized-step
      check, `core/limits.py:370-378`, `guardian/service.py:360-368`), G-06 and the new G-32 (non-firm feeder
      ramp, `core/limits.py:360`). The engine ramps each hub (`orchestrator/src/opengrid/engine/__init__.py:195-207`);
      there is no fleet or feeder ramp shaping and no signed start jitter.
    - **Reverse flow was not measured at R2; fixed in R3 (`451a2a2`, `02a` §6.8):** the guardian now takes the signed
      `REAL_POWER_KW`, and an unsigned kVA reading becomes an interval with unknown direction treated as export.
      At `6470cfa`: the guardian's aggregate flow is the sum of each bank's latest SCADA
      `APPARENT_POWER_KVA`, taken as import-positive (`orchestrator/src/opengrid/guardian/flow_repo.py:40-48`).
      Apparent power has no sign (the simulator publishes |kW|/pf,
      `integration-sims/src/ogsim/scada/aggregation.py:37-41`), so a bank that is already exporting reads as
      importing, and G-28/G-29/G-30 see reverse flow only through the batch's own change.
    - Checker: `FLOW_LIMIT` measures home meter, derate, transformer, feeder and substation after the fact
      (`orchestrator/src/opengrid/invariants/queries.py:497-565`, `invariants/checks.py:564-624`); the spec's
      `K4_*` counters are not built.
    - Target design: `09-optimizer-dispatcher-update.md` §1.9 (families F1-F7) and §2.6.
- **K15 — territory (new) (D-20, D-21, D-27).** Three parts (`09-optimizer-dispatcher-update.md` §6, quoted here):
  - A REG(u) obligation is reserved, granted and delivered only by assets inside utility $u$'s service territory.
  - An asset inside a regulated territory takes FREE (ERCOT) opportunities only if $u$'s contract grants wholesale
    access.
  - Base's net injection at every boundary substation of $u$ stays ≤ 0 (energy is consumed inside the territory).
    Charging is priced and settled under the asset's own territory tariff.
  - Principle P2. Enforced at (target): contracts admission → selector C25 → allocator eligibility → guardian
    G-33 (market segregation) + G-30 (territory export) → settle tariff attribution. Fail-safe (target): VETO the
    item; a shortfall is recorded against the same obligation (K13 best effort, above).
  - **Status at main `6470cfa` (R2): partly built.**
    - Contract admission: **not built** (`orchestrator/src/opengrid/contracts/` is unchanged since `434d230`;
      `R_TERRITORY_INELIGIBLE`, `orchestrator/src/opengrid/core/reasons.py:88`, is raised only by the guardian).
    - Selector C25: **built** as bank-eligibility narrowing, not as LP rows
      (`orchestrator/src/opengrid/selector/gate.py:489-529`, called in `run_gate` at `:951-966`); FREE headroom in
      a territory at `selector/model.py:226-227`; the regulated-first stage R at `selector/solve.py:140-144`.
    - Allocator: **built, on** (`allocator/cycle.py:221-232`, `:412-416`; `[allocator].enforce_territory = true`,
      `orchestrator.toml:119-120`). A blocked obligation gets a 0 kW grant and a shortfall with the reason.
    - Guardian: G-33 **built, always wired** (`flow_checks.py:317` on `orchestrator/src/opengrid/market/territory.py:131`,
      the one predicate; `guardian/service.py:584`). The guardian refuses to start without `[zone_territory]`
      (`guardian/flow_repo.py:270-274`). G-19 accepts a territory reduction only when its own check agrees
      (`guardian/service.py:1011`, `:1065`). G-30 is built but idle: no Austin Energy or CPS bank is seeded (and
      see "Reverse flow is not measured" under K4).
    - Settle: **built** for a REGULATED contract, TOU charging and the capacity payment
      (`orchestrator/src/opengrid/settle/__init__.py:242-269`), keyed on the contract's market, not the asset's
      territory.
    - Checker: **partly built.** One `K15_TERRITORY` check over regulated, non-headroom grants
      (`invariants/queries.py:409-441`, `invariants/checks.py:656-688`), counter `og_territory_violations_total`;
      part (b) and the `K15_TERRITORY_MARKET`/`K15_TERRITORY_EXPORT`/`K15_CHARGING_TARIFF` counters are not built.
    - Proven by: property tests `orchestrator/tests/unit/market/test_territory.py:122-130`,
      `orchestrator/tests/unit/allocator/test_dispatch_extensions.py:440-452` and
      `orchestrator/tests/unit/guardian/test_flow_checks.py:389`.

**Additions (2026-09-25):**

- **K1 / K13 energy:** "never below reserve" and the commitment lock are enforced on ENERGY, not only power. Every
  cycle, each committed obligation's remaining delivery is compared with the energy available above reserve on its
  eligible hubs (net of energy reserved for other obligations). A shortfall risk marks the obligation AT_RISK and
  raises ALR-ENERGY-SHORTFALL-RISK. A missing or stale SoC means zero discharge. The guardian projects each hub's SoC
  over the command's lease (G-01-ENERGY).
- **K14 (canonical, MVP-S+, approved by owner 2026-09-25): power-quality envelope.** Dispatch serving a customer must
  keep the aggregated per-phase imbalance, voltage/frequency deviation and THD within that customer's
  PowerQualityEnvelope — both at selection (for uncommitted headroom) and continuously for already-committed,
  `DELIVERING` obligations, with a corrective-action ladder (rebalance → substitute within the obligation → remote
  recalibration where eligible → reactive/PF adjustment → exclude → `AT_RISK`) before any envelope breach is allowed
  to stand. THD, imbalance and frequency/voltage deviation are measured from per-inverter waveform telemetry
  wherever available; the modelled aggregation formulas are a pre-delivery planning/forecast cross-check only, not
  the delivery-time source of truth. The guardian checks this on independent inputs (G-21…G-24), and a related
  calibration-safety check (G-25) gates any remote inverter-recalibration command. Fully defined in
  `06-service-profiles-and-power-quality.md`, including the asset-health/calibration workflow for inverters that
  cannot be corrected remotely.

Guardian check numbering in MVP-S+: G-01 reserve · G-02 hub P · G-03 bank kVA · G-04 hub ramp · G-05 fleet ramp cap
and stagger · G-06 feeder/bank ramp ceiling for firm events · G-09 ledger version · G-13 sequence/epoch/lease ·
G-14 trace pre-image present · G-15 L2 instruction · G-19 commitment lock · G-20 time quality · **G-21 per-phase
imbalance (K14) · G-22 THD estimate/measurement (K14) · G-23 frequency/voltage deviation (K14) · G-24 ride-through
and asset-state conformance (K14) · G-25 calibration-command safety (K14; bounds, rate limit, no active
non-default-envelope grant on the target hub)** — G-21..G-25 defined in `06-service-profiles-and-power-quality.md`
§5.3/§6.7. Other G-numbers (G-07/08/10-12/16-18) are deferred to later releases. G-26…G-33 are assigned below.

**Flow limits and territory (D-26, D-27; K4 extended, K15).** The final guardian numbering (lead, 2026-09-26)
matches `09-optimizer-dispatcher-update.md` §2.5/§2.6 as renumbered:

| Check | Limit | Built in R2 (`main` `6470cfa`): check | Wired in `guardian/service.py` |
|---|---|---|---|
| G-26 | Home meter: export and import at the meter, net of home load | `orchestrator/src/opengrid/guardian/flow_checks.py:164` `check_g26_home_meter` | `:328`, per item |
| G-27 | Service transformer | `flow_checks.py:219` `check_g27_transformer` | `:539-541`, over all members of each transformer |
| G-28 | Feeder thermal and reverse flow | `flow_checks.py:261` `check_aggregate_flow` on the feeder's flow | `:452-457`, `:477` |
| G-29 | Substation POI / transformer | `flow_checks.py:261` on the substation's flow; `flow_checks.py:296` `check_g29_poi` for a substation asset | `:458-463`, `:489-493` |
| G-30 | Territory export (K15c) | `flow_checks.py:261` on the territory boundary flow | `:464-469` |
| G-31 | Sustained vs peak | `flow_checks.py:88` `check_g31_peak` | `:427`, per item |
| G-32 | Feeder ramp for non-firm steps | `orchestrator/src/opengrid/core/limits.py:360` `check_feeder_ramp` | `:383-390` |
| G-33 | K15 market segregation | `flow_checks.py:317` `check_g33_territory`, on `market/territory.py:131` `check_territory` (the one predicate) | `:584`, per item |
| G-34 | Hub on the proposal's bank (new in R3; refs at `451a2a2`) | `flow_checks.py:317` `check_hub_in_bank` | `:627`, batch |

- **Status at main `6470cfa` (R2): built.** All eight run on the guardian's own reads (its topology, territory and
  telemetry ports; `guardian/main.py:349` wires `PgGridTopologyPort`), next to G-01…G-25. Item-level rules are
  listed at `guardian/service.py:66`. Tests: `orchestrator/tests/unit/guardian/test_flow_checks.py` (29) and
  `test_service_flow.py` (14). No feature flag gates them. Caveats, all under K4 above: G-31's peak path is
  dead (the field-name defect), G-29/G-30 are idle without substation or regulated-zone data, reverse flow is
  seen only through the batch's own change (fixed in R3, `02a` §6.8), and `[guardian.flow].telemetry_required` is false, so a flow field
  a hub has never reported falls back to the static premise limits (`guardian/config.py:101`).
- **The ERCOT_AS energy hold is not a guardian check.** Two built pieces enforce it:
  - the selector's C3′ floor (`orchestrator/src/opengrid/selector/model.py:345`);
  - the engine's S6 hold floor (`orchestrator/src/opengrid/engine/gateways.py:139`).

  The invariants check `CHECK_AS_HOLD` measures it (built in R2, `orchestrator/src/opengrid/invariants/__init__.py:489-508`,
  `invariants/checks.py:527-561`, `invariants/queries.py:444-477`), with a narrower scope than the spec:
  - it runs only while an `og.as_deployment` window is active (`queries.py:471`), so an award that is held and
    not deployed is never measured;
  - it compares the energy held with the product's full duration (`checks.py:547`), even part-way through a
    deployment;
  - it sums the deliverable energy of every hub on the award's reserved banks (`queries.py:460`, `:469`) without
    netting out other obligations on those banks.

  The mismatch has been reported to the lead for routing.
