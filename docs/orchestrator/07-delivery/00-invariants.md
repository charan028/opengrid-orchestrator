# MVP-S Canonical Invariants (K1–K14)

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
§5.3/§6.7. Other G-numbers (G-07/08/10-12/16-18) are deferred to later releases.
