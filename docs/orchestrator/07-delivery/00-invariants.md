# MVP-S Canonical Invariants (K1–K15)

This is the single source of truth for invariant IDs. Every MVP-S document (02a, 02b, 03, 04) and every test uses
these IDs and meanings. Principles P1–P8 are defined in `../06-reviews/06-first-principles-review.md` §1.

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
  - **Built today:** (e) is unchanged and enforced (`orchestrator/src/opengrid/core/limits.py:94-138`
    `check_hub_ramp`/`check_fleet_ramp_cap`/`check_feeder_ramp_ceiling`, guardian G-04/G-05/G-06); (a)'s flat rated
    power and energy-over-lease caps are enforced (`orchestrator/src/opengrid/core/limits.py:65-72`
    `check_hub_power`, `orchestrator/src/opengrid/core/physics.py:63-104`
    `hub_sustainable_discharge_kw`/`hub_sustainable_charge_kw`), but the SoC/temperature derating curve itself is
    not; aggregate bank kVA (part of (c)) is enforced (`core/limits.py:75-91` `check_bank_kva`, guardian G-03).
  - **Specified, not built:** the SoC/temperature derating curve; per-home meter export/import (b); per-service-
    transformer and feeder/substation thermal loading and reverse-flow limits (c, beyond the existing ramp
    ceiling); substation-asset POI/transformer limits (d) — there is no substation asset class in code (no
    `SUBSTATION_BESS`, no substation `og.asset` table). The data-model groundwork for (a)-(d) exists as inert
    fields only: `HubSnapshot.cell_temp_c`/`p_dis_max_kw`/`meter_kw`/`export_limit_kw`/`xfmr_id`
    (`orchestrator/src/opengrid/allocator/models.py:53-57`) and `BankSnapshot.feeder_id`/`substation_id`
    (`orchestrator/src/opengrid/allocator/models.py:75-76`) are carried but read by no check function yet. See
    `09-optimizer-dispatcher-update.md` §1.9 (families F1-F7) and §2.6 (guardian checks G-02 (changed) and
    G-26…G-33; final numbering in "Flow limits and territory" below) for the full target design (not edited here).
- **K15 — territory (new) (D-20, D-21, D-27).** Three parts (`09-optimizer-dispatcher-update.md` §6, quoted here):
  - A REG(u) obligation is reserved, granted and delivered only by assets inside utility $u$'s service territory.
  - An asset inside a regulated territory takes FREE (ERCOT) opportunities only if $u$'s contract grants wholesale
    access.
  - Base's net injection at every boundary substation of $u$ stays ≤ 0 (energy is consumed inside the territory).
    Charging is priced and settled under the asset's own territory tariff.
  - Principle P2. Enforced at (target): contracts admission → selector C25 → allocator eligibility → guardian
    G-33 (market segregation) + G-30 (territory export) → settle tariff attribution. Fail-safe (target): VETO the
    item; a shortfall is recorded against the same obligation (K13 best effort, above).
  - **Specified, not built.** No stage of the chain exists in code: `orchestrator/src/opengrid/contracts/admission.py`
    has no market/territory check; `orchestrator/src/opengrid/selector/model.py` and `selector/gate.py` have no C25
    rows; the guardian's built check set stops at G-20 (`orchestrator/src/opengrid/guardian/checks.py:1-2` lists
    G-01…G-20; there is no G-30/G-33 anywhere in `guardian/checks.py` or `guardian/service.py`). The data-model
    groundwork is in place but inert: `BankSnapshot.territory`/`free_access`
    (`orchestrator/src/opengrid/allocator/models.py:74-81`) are carried fields nothing yet populates or reads (the
    comment there names `opengrid.market.territory_of_zone`, a module that does not exist in this repo); the config
    surface `orchestrator/config/tdsp_tariffs.toml:65-103` (`[zone_territory]`) documents AE/CPS/LCRA/RAYBN
    territory by ERCOT settlement zone but says of itself "no selector/allocator/guardian code reads this table
    yet." Proven by: none yet (target: a property test over random fleet/contract mixes, plus the `K15_*` checker
    counters described in `09-optimizer-dispatcher-update.md` §6).

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

| Check | Limit |
|---|---|
| G-26 | Home meter: export and import at the meter, net of home load |
| G-27 | Service transformer |
| G-28 | Feeder thermal and reverse flow |
| G-29 | Substation POI / transformer |
| G-30 | Territory export (K15c) |
| G-31 | Sustained vs peak |
| G-32 | Feeder ramp for non-firm steps |
| G-33 | K15 market segregation |

- **Status at main `434d230`:** none of G-26…G-33 is in the guardian yet. `guardian/checks.py` and
  `guardian/pq_checks.py` stop at G-25. The ids are fixed so the implementations land on them and nothing else
  reuses them.
- **The ERCOT_AS energy hold is not a guardian check.** Two built pieces enforce it:
  - the selector's C3′ floor (`orchestrator/src/opengrid/selector/model.py:296`);
  - the engine's S6 hold floor (`orchestrator/src/opengrid/engine/gateways.py:124-127`).

  The invariants check `CHECK_AS_HOLD` is to measure it; it is not on main at `434d230`.
