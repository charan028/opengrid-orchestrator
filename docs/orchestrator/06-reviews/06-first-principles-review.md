# OpenGrid Orchestrator — First-Principles Review of the Orchestration Engine

Date: 2026-09-25 · Status: review for the user · Method: six independent expert reviews (optimization/OR, power systems &
ERCOT, software architecture, product/requirements, prototype code & deck, safety/security), each applying the same
first-principles test, merged here. Scope: `00-brief.md`, `00-decision-register.md`, `01-product/*`,
`02-architecture/01–07`, `03-security/*`, `06-reviews/*`, `src/opengrid/*` (Streamlit prototype and simulators),
`deliverables/Base_Power_Grid_Services_Executive.pptx`, `Base_Power_Valuation_Analysis/*`.

**Stage.** No orchestrator code exists. The deliverable at this stage is the document set: spec, epics and stories, and
test plan. This review therefore recommends **document changes** (§7), not code changes. The only running code is the
set of **simulators** on the base server (192.168.5.35, `/opt/opengrid_sim`: `control_engine.py` rule allocator,
`scada_simulator.py`, `ercot_live.py`, `fleet_lp.js` MILP fleet-mix optimizer; `https://base.tocy-net.net/opengrid/`),
plus the local Streamlit simulators in `src/opengrid`.

**Scope limit.** The base-server simulator source is not in this repository. The site returns 401 and the source was not
reachable from this machine. The spec's "what the prototype taught" claims (§1.5 of `03-decision-engine.md`) and the
back-test baseline (§12.1) are therefore unverified here; copying `/opt/opengrid_sim` into the repo would close this.
Findings on "simulator code" below refer to the local Streamlit simulators only.

---

## 1. The first principles used

Every element of the design was tested against these irreducible truths: is it **necessary** (it follows from a truth), is
it **sufficient**, and is it **minimal** (not a rule layer duplicating what a constraint or price already expresses)?

| # | Principle | Consequence for the engine |
|---|---|---|
| P1 | **Physics.** $e_{t+1}=e_t+\eta_c p^c\Delta t-p^d\Delta t/\eta_d$; P, E, kVA, ramp and feeder limits; frequency response is local and sub-second | State, limits and fast response are properties of the device/hub, not of the optimizer |
| P2 | **Authority.** Exactly one entity controls a device at any instant; safety > homeowner reserve > grid operator/ISO > commerce | Authority is a hard constraint, never a price |
| P3 | **One buyer.** A kW/kWh of headroom is sold to one buyer per interval | A single reservation ledger with a single writer |
| P4 | **Commitment (user rule).** Once a delivery is committed it runs to completion; capacity is re-sold only when it is free | Selection happens at gates, delivery is frozen, and only uncommitted headroom is re-optimized |
| P5 | **Information.** The future is uncertain; a decision may use only what is known at its gate (non-anticipativity) | Uncertainty is handled once, consistently, at the layer that decides |
| P6 | **Time-scale separation.** Day-ahead / intraday / 5-min / seconds each have different information and authority | A lower layer tracks and repairs what the upper layer decided; it does not re-decide it |
| P7 | **Net value.** Value = revenue − energy cost − degradation − expected penalty − backup-comfort cost − cost to build and run | One objective, correct units, all cost terms; build effort proportionate to value at stake |
| P8 | **Measurement.** You can bill only what you can measure against a defensible baseline, and reproduce why | M&V and a replayable trace are core, not add-ons |

## 2. Verdict

**The core of the design is first-principles sound; the scope wrapped around it is not proportionate; and the user's
commitment-lock rule is only partly implemented.**

- **Sound (P1–P3, P5, P8).** Authority is modeled as law and physics, not commerce: ERCOT instructions to an on-line ADER
  are an *equality constraint* in the arbitration LP, not a priced call (§2.1, §8.4 A6). One-kWh-one-buyer is a
  single-writer reservation ledger (§2.4, R37). The guardian is the sole command signer, and the stop authority can
  stop but never release (R1, R16). TIMEOUT ≠ VETO ≠ STOP (R31). The AI agent can only read, simulate and propose (§11).
  Non-anticipativity, robust firm sizing and an independent plan validator are all present. All six experts rated this
  core as unusually rigorous for a pre-code spec.
- **Primitive decomposition holds (P1, P2).** The nine dispatch profiles compose from about six control primitives:
  open-loop schedule; closed-loop regulation on a measured quantity; price response; capacity hold/ring-fence;
  event/schedule tracking; island mode control. Together with one call→admission→reservation→dispatch→verify→bill
  pipeline, this is genuine composition, not nine special cases. Caveat: "new service type by configuration" is designed
  for nine types but proven for zero.
- **Commitment lock (P4): partial — the main correctness gap.** Seven specific ring-fence categories are protected, and
  arbitration is lexicographic so a price spike cannot outvote a firm tier. But a *served* event is re-arbitrated every
  2–10 s tick (`02-domain-model` §2.4 `SERVED → ARBITRATING`). The within-tier economics step (§8.4 S4) can move a
  delivering reservation to a newer, better-paying obligation. FR-ARB-004 lets profitability override priority
  per contract, with no restriction to pre-commitment. Only the dispatcher enforces commitments, and it is the component
  the architecture treats as untrusted. See §3.
- **Optimizer mostly derived, with four accretions (P5–P7).** Priority is enforced two different ways: true
  lexicographic stages at real time, but penalty-slope weights at day-ahead/intraday, never bounded against the
  $2,000–5,000/MWh price range. Uncertainty is buffered four times over (scenarios + over-enrollment z + robust P90 +
  dispatch/CVaR margins) with no combined budget. C19's hard net-peak charging ban duplicates the price term. The §7.4
  CVaR release is a side rule outside the LP. See §5.
- **Scope and cost disproportionate (P7).** There are 318 FRs, 280+ failure modes, 161 threats and about 30 Kubernetes
  deployables. The judged-demo scope (`MVP-J`) is about 292 person-days against at most 128 available. The single demo
  node does not schedule at 10,000 hubs (100.5% of memory). The spec has already diagnosed this (Q23, ARC-001) but has
  not cut.
- **Prototype, deck and spec disagree (P1, P3, P8).** The Streamlit simulators contain **no optimizer**, only threshold
  rules and a once-daily spread. Pages 4–5 let the same `total_kw`/`available_kw` be claimed simultaneously by several
  services (double-booking). Arbitrage omits round-trip efficiency and the power limit, overstating value by about 17% in
  a scratch test. The deck states the 80% dispatch-capture figure as fact; the spec itself labels it "unverified
  hypothesis" (§12.4).

## 3. Commitment lock — the user's dispatch rule

> The optimizer uses external feeds to **select** which customers or market opportunities to take on. Once committed,
> the delivery must be **fulfilled** before that capacity goes to a new opportunity. There is no switching mid-contract,
> or every cycle, because another market or zone now pays more.

### 3.1 What the spec already does

| Mechanism | Location | Status |
|---|---|---|
| Ring-fencing rules 1–7 (AS hold, later-window firm energy, declared capacity, ISO-visible capacity, tolled share, TDU calendar) | `03` §2.4 | Complies, for those seven categories only |
| Lexicographic arbitration: firm tier pinned before economics ("with weights a price spike can outvote a firm obligation; with stages it cannot") | `03` §8.4 | Complies *across* tiers |
| AS forward release as the *only* diversion path: default off, future intervals only, CVaR test, cap, human confirmation, traced buyback | `03` §7.4 | Complies. Keep it default-off; under this rule it is correct conservatism, not too narrow |
| Per-hub anti-hunting: 5-min dwell, $5/MWh hysteresis, +20% stickiness | `03` §8.12, §8.4 | Partial. Limits churn per hub, not per obligation |
| §8.8 substitution: triggered only by hub failure, house events or grid state, never price; swaps the hub, not the obligation | `03` §8.8 | Complies. This is the allowed case |
| §8.9 sudden price changes: a spike is captured up to the offered MW, never at T1/T2's expense | `03` §8.9 | Complies, but stated only for price-responsive mode on off-ADER premises, not as a general rule |
| §3.3 intraday re-plan triggers are information events (new obligation, topology, `AT_RISK`, notices), not price | `03` §3.3 | Partial. Nothing bars a re-plan from shrinking a window already in delivery |
| C16 non-anticipativity binds scenarios *within* one solve. §6.8 fixes first-stage commitments between solves "via `setSolution`", which is a warm-start hint, not an equality | `03` §6.5 C16, §6.8 L1203 | **Ambiguous.** A solver hint does not lock |
| `SERVED → ARBITRATING` every tick while the window is open | `02` §2.4 | **Gap.** Re-decides which obligation a reservation serves |
| Within-tier economics (§8.4 S4): "every stage re-optimizes **all** allocations" (L1623). The only protection is a +0.2 served-last-cycle weight and a tie-break | `03` §8.4 | **Violation path.** Obligation types with no §2.4 reservation entry (`LARGE_LOAD`, price-responsive `ERCOT_ENERGY`, `PJM_CAPACITY`) can lose hubs to a newer same-tier call every cycle |
| FR-ARB-004 "priority dominates profitability unless a per-contract override is configured"; T-order configurable per contract | `02-functional-requirements`; brief §3.1 | **Violation path.** Not limited to pre-commitment selection |
| Guardian checks G-01…G-18 | `02-security-architecture` §6.3 | **Gap.** No check that a committed allocation was not reduced or reassigned |
| Prototype simulators | `5_Control_Room_Simulator.py` L207–548 | No lock and no ledger. Tabs double-book the same kW |

### 3.2 Formal rule (add as a named invariant — K13 / FR-ARB-014)

Each obligation $o$ has a lifecycle **offered → selected → committed → delivering → fulfilled → settled**, owned by the
fleet allocator's reservation ledger (single writer).

- **Selection** happens only at decision gates (day-ahead, intraday, or an admission event), with a commitment decision
  $x_o\in\{0,1\}$ (or a quantity $q_o\in[0,\bar Q_o]$ where contracts are divisible).
- **Lock.** For every $t\in[s_o,f_o]$, once $x_o=1$ at gate $g$, every later solve treats $x_o$ and the committed
  profile $Q_{o,t}$ as **parameters**, not variables:
  $\sum_b y_{o,b,t} + z_{o,t} \ge Q_{o,t}$, where the slack $z$ is used only when an exception below applies.
- **Re-optimized:** only uncommitted headroom $H_{b,t}=\text{cap}_{b,t}-\sum_{o\,\text{committed}} r_{o,b,t}$. New
  opportunities compete **only** for $H$.
- **Allowed:** *substitution* — changing *which homes* deliver the same committed obligation (§8.8).
- **Forbidden:** *switching* — reassigning committed capacity to a *different* obligation.
- **Exceptions (authority and physics only):** L0 device safety, L1 homeowner reserve, L2 utility/ERCOT instruction,
  physical infeasibility with no substitute, or the §7.4 audited release (default off, future intervals only, priced,
  confirmed). Each carries a reason code; any resulting shortfall is recorded as `AT_RISK`, notified, and settled at
  contract penalty.
- **Uncommitted headroom** (e.g., spot energy) keeps the §8.12 dwell/hysteresis so it does not flap either.

### 3.3 Where to enforce it (once, by construction, then verified independently)

1. **Allocator/ledger (the enforcement point).** At S4/S6, committed reservations are hard inputs. Economics runs "inside
   tiers, over unreserved capacity only." The seven ring-fence rules become special cases of this one rule.
2. **Guardian (the independent check, new G-19).** Refuse to sign a batch that reduces an active committed allocation
   unless it carries an L0/L1/L2/infeasible reason code or a valid §7.4 approval.
3. **Trace.** Reason codes `R-COMMIT-LOCK-OVERRIDE-L0/L1/L2`, `R-COMMIT-LOCK-INFEASIBLE`. Replay asserts that 100% of
   reductions carry one.
4. **Detection.** Committed-allocation churn rate per obligation, and cross-zone/cross-service reassignment frequency,
   feeding the existing withholding detector (CTL-154).
5. **Optimizer.** Add a new constraint family **C24 — commitment lock**: $y_{o,b,t,\omega}\ge\hat y_{o,b,t}$ and $x_o$ fixed
   for committed $o$, $t\in[s_o,f_o]$. Amend §6.8 so that intraday fixes first-stage commitments by **equality
   constraint**, not by `setSolution` alone (freeze and shift). At real time, S2 subtracts every active $\hat y$ from
   capability before arbitration, the same way tolled shares are removed in C21. The RT LP then sees only free capacity:
   no new binaries, and a smaller problem.
6. **Scope of the rule.** Amend §2.4 so that ring-fencing applies uniformly to *every* admitted obligation type, including
   `LARGE_LOAD`, price-responsive `ERCOT_ENERGY` and `PJM_CAPACITY`.
7. **Requirements and tests.** Add FR-ARB-014 / FR-DE (Must): §8.4 never reduces a committed obligation's granted kW
   below $\hat y$ for any tier except on L0/L1/L2 or verified infeasibility; every such reduction is traced and reported as
   a shortfall against that obligation, never as a reallocation. Limit FR-ARB-004's override to *pre-commitment*
   selection. Add a replay fixture: two same-tier calls of rising relative value; the earlier-committed call never drops
   below $\hat y$ unless an L0–L2 or infeasibility event is injected.

### 3.4 Why this is optimal, not just conservative

The commitment decision must price the **whole delivery window**: expected net value minus the opportunity cost of
locking capacity, taken over the scenario set rather than at the instantaneous price. Once that is done at the gate,
re-deciding mid-window buys little and costs a lot. Each reversal re-pays round-trip loss and degradation on a partial
cycle, and adds penalty exposure, counterparty-trust loss, fleet-wide ramp stress and market-conduct risk. In a scratch
test (synthetic, load-derived prices, 10 MWh/2.5 MW, η 0.90), plan-then-lock dispatch earned **~9.7×** a myopic
flip-on-every-price-cross rule. The number is illustrative only; the direction is robust. The honest cost of the lock is
forgone upside from a *genuinely new* opportunity inside a committed window. KPI-22 should report it as a separate term
("forgone spot upside") so it is measured, not assumed.

## 4. First-principles decomposition of the orchestration engine

Components are split by responsibility, time scale, owned state and failure domain. The spec's containers map onto
them as shown.

| # | Component | Time scale | Owns | Invariants owned | Fails to | Spec container → verdict |
|---|---|---|---|---|---|---|
| 1 | **Hub firmware / edge** | ms–1 s | Protection, droop, local reserve, local fallback schedule | P1 locally, L0, L1 | Local autonomy on lease expiry | Hub (not built by us). Keep, never centrally overridden |
| 2 | **State estimator (digital twin)** | 1–10 s | Per-hub state, eligibility, quality flags | Truthful capability | Stale → exclude, flagged | `fleet-state` + `ingest-writer`. Keep |
| 3 | **Forecaster** | 15 min–day | Scenarios, quantiles | — | Naive/persistence | `forecaster`. Defer sophistication (Line B) |
| 4 | **Opportunity intake & admission** | event | Calls, contracts, eligibility, feasibility | Admits only what is feasible and authorized | Reject with reason | `contracts-rt` (admission). Merge with #9 for the MVP |
| 5 | **Selector / commitment planner** (DA + ID) | gates | Commitment decisions $x_o$, offers, holds, COP | P4 selection, P5 non-anticipativity | Rule-based F2 plan | `planner`. Keep; MILP can follow a rule MVP |
| 6 | **Reservation ledger + RT allocator** | 2–10 s | The ledger (single writer) | P3 one buyer, **P4 lock**, tier order | Hold last grants | `dispatcher` allocator. Essential. One process with the shards for the MVP |
| 7 | **Controllers** (bank kVA PI, ADER net-power, ramp) | 2–4 s | Integrator states | One integrating loop per quantity (R28) | Hold/feed-forward | `dispatcher` executor. Keep |
| 8 | **Guardian** (sole signer) + **Safe-Stop Authority** | ms | Signing key, verdicts / stop-only key | K1–K13 incl. new G-19 | Hold (timeout), veto, scoped stop | `guardian`, `safe-stop`. Essential. Guardian can be a distinct call boundary, not a pod, for the MVP |
| 9 | **M&V + settlement** | interval / batch | Baselines, performance, invoice lines | P8 | Estimate flagged | `contracts-batch`. Merge with #4 until real settlement volume exists |
| 10 | **Decision trace** | async | Hash-chained streams | Replayability, attribution | Local journal | Trace streams. Essential — the differentiator |
| 11 | **Protocol gateways** (device, SCADA, market) | ms–s | Sessions, point maps | Authenticated I/O | Stateless reconnect | `device-gateway`, `scada-gateway`, `integrations`. One DNP3/TLS path for the demo; defer ICCP/2030.5/IEC 104/OPC UA |
| 12 | **Advisor (AI)** | min | Proposals | Outside the loop | No effect | `ai-agent`. Correct; defer |

The irreducible loop is **2 → 3 → 5 → 6 → 7 → 8 → 1**, verified by **9 → 10**. Everything else supports it.

## 5. The linear optimizer for dispatch — recommended stack

### 5.1 Canonical core model (one model family for all layers)

Sets $t\in\mathcal T$, partitions/banks $b\in\mathcal B$ (the right aggregation level, not homes), obligations
$o\in\mathcal O=\mathcal O^{c}\cup\mathcal O^{new}$ (committed ∪ candidate), scenarios $\omega\in\Omega$.

$$\max\; \sum_{o\in\mathcal O^{new}} V_o x_o + \sum_\omega \pi_\omega\Big[\sum_{b,t}\Delta_t\big(\lambda_{t\omega}(d^{s}_{bt\omega}-g_{bt\omega}) - c_{deg}(g+d)_{bt\omega}\big) - \sum_{o,t}\text{Pen}_o z_{ot\omega}\Big] + V^{end}(e_{T})$$

subject to:

- **SoC (P1):** $e_{bt\omega}=e_{b,t-1,\omega}+\eta_c g_{bt\omega}\Delta_t-\tfrac{\Delta_t}{\eta_d}\big(d^{s}_{bt\omega}+\textstyle\sum_o y_{obt\omega}\big)-\ell_b\Delta_t$.
- **Reserve (L1, hard, never priced):** $0\le e_{bt\omega}\le E_b-R_b$.
- **Power, network and ISO:** $g+d^s+\sum_o y\le P_b$; kVA/feeder caps; ADER UDSP equality (L2).
- **One buyer (P3):** $y_{obt\omega}\le r_{obt}$, and $\sum_o r_{obt}\le \text{cap}_{bt}$.
- **Commitment and lock (P4):** $\sum_b y_{obt\omega}+z_{ot\omega}\ge Q_{ot}\,x_o$ for $t\in[s_o,f_o]$, with $x_o\equiv1$ fixed
  for $o\in\mathcal O^c$.
- **Non-anticipativity (P5):** $x_o, r_{obt}$ identical across $\omega$.
- **Charge/discharge exclusivity:** binary only where the price can go negative (C14).

C3 (additive floor), C4 (pipeline headroom) and C21 (tolled share) are all instances of "$r$ reserved for $o$". Merge
them into one reservation family.

### 5.2 The stack

| Layer | Solves | Decision type | Cadence / size | Commitment role |
|---|---|---|---|---|
| **L-DA** stochastic MILP | Select obligations, offers, AS holds, firm energy, charge windows, COP | $x_o$ binary (block contracts), rest LP | 3×/day; ~260k continuous / ~2k binary at demo scale; HiGHS with MIP start | **Main selection gate** |
| **L-ID** rolling MILP | Re-select *only uncommitted headroom*; new calls; embedded CVaR release decision | Committed $x_o$ frozen | 15 min + event triggers; warm start | **Secondary selection gate** |
| **L-SCED** LP | Re-price energy and water values; targets for free headroom | Continuous; no new commitments | 5 min; basis warm start | Tracks commitments, prices headroom |
| **L-RT** lexicographic LP + water-filling | Allocate homes to committed obligations (substitution) and headroom to spot | Continuous; $x$ is a parameter | 2–10 s; per partition | **Never selects.** Substitution only |
| **Controllers** PI | Bank kVA, ADER net power, ramp | Feedback | 2–4 s | Deliver the grant |
| **Hub** | Protection, droop | Local | ms | — |

With commitments fixed as parameters, every layer below L-ID is a pure LP. This keeps real time fast and deterministic.

### 5.3 How priority enters the model

| Kind | Examples | Enters the model as |
|---|---|---|
| Authority | L0 safety, L1 reserve, L2 ISO/utility instruction | Hard constraints |
| Existing commitments | Anything already committed/delivering | Hard fulfilment constraints; slack only via an exception reason code |
| Choice among *new* opportunities | Tier T1–T4, profitability | Value $V_o$ and penalty terms, **at the selection gate only** |

### 5.4 Optimizer changes recommended

1. **Priority must mean the same thing at every layer.** Make day-ahead/intraday lexicographic as well: solve firm
   first, fix it within ε, then optimize economics. Alternatively, prove that the penalty slopes dominate
   RTSWCAP/VOLL. (`03` §6.6 vs §8.4)
2. **Add the commitment state $x_o$ and lock constraints** (§3.2). Real time becomes substitution plus headroom only.
3. **Raise non-anticipativity (C16, FR-DE-039) from Should to Must.** Every other guarantee depends on it.
4. **Budget conservatism once.** Report compounded capacity withheld by the scenarios, over-enrollment z, robust P90,
   dispatch margin and CVaR margin against perfect foresight.
5. **Turn C19** (hard net-peak charging ban) into a price adder. Keep only the reliability recharges hard.
6. **Embed the §7.4 CVaR release** in the intraday LP (Rockafellar–Uryasev), keeping it default-off.
7. **Degradation.** State that the $0.03/kWh cost is a floor and that the C18 cycle-budget dual carries the marginal wear
   signal. Validate against a real curve.
8. **Resolve A-DE-15** (AS energy hold measured at cells vs terminals, a ~5% factor), and add a nightly check comparing
   bucketed vs per-hub RT results.

## 6. Consolidated findings (ranked)

| # | Sev | Area | Finding | Location | Recommendation |
|---|---|---|---|---|---|
| 1 | **High** | P4 commitment | Delivering reservations re-arbitrated every tick; within-tier economics can switch obligations; FR-ARB-004 override not limited to pre-commitment; no guardian check | `02` §2.4; `03` §8.4 S4; FR-ARB-004; `02-sec` §6.3 | §3: K13, FR-ARB-014, G-19, reason codes, fixture |
| 2 | **Critical** | P7 scope | `MVP-J` ≈292 person-days vs ≤128 capacity; ~30 deployables; node over budget at 10k hubs | `03-epics` capacity; `06` §1.8; Q23 | Cut to the thin slice in §7 now; the judge's ≈59+23 person-day cut is the target |
| 3 | **High** | P5/P6 optimizer | Priority lexicographic at RT but penalty-weighted at DA/ID; slopes unbounded vs price caps | `03` §6.6 vs §8.4 | Lexicographic DA/ID or a dominance proof |
| 4 | **High** | P1 safety | Firm events exempt from the 50 MW/min fleet ramp cap; no feeder/substation-scope ceiling | `03` §8.12 L2262; G-05 | Guardian-enforced per-feeder/substation ramp ceiling |
| 5 | **High** | P3 prototype | Simulators double-book the same kW across services; no ledger | `5_Control_Room_Simulator.py` L207–548; page 4 | Shared ledger; don't present summed values |
| 6 | **High** | Business evidence | ECRS at its 100 MW system cap; 0 MW ADER approved in the chosen NOIE zones (Austin Energy, CPS Energy) | Claims-verification #15; R27a | Mark as unavailable/simulated inputs; don't size KPIs on them |
| 7 | High | Safety | Common-mode firmware/vendor-cloud risk is detect-only (bypasses the guardian) | RR-03 | Attestation/measured boot as a go-live gate |
| 8 | Medium | P7 economics | Prototype arbitrage omits η and the kW limit (~17% overstatement); page 2 uses hour-of-day averages despite its own fix | `analytics.py` L230, L409; page 2 L191 | Use P/E/η-constrained LP values and `real_daily_spread_stats` |
| 9 | Medium | Consistency | Deck states 80% capture and derived IRRs as fact; spec says unverified | Deck slides 1/3/7; `03` §12.4 | Carry the spec's caveat into the deck |
| 10 | Medium | Traceability | Prototype lessons and back-test baseline point to out-of-repo code | `03` §1.5, §12.1 | Import `/opt/opengrid_sim` into the repo |
| 11 | Medium | P5 | Four stacked conservatism layers with no combined accounting | `03` §5.3, C3/C12, §7.4 | Conservatism budget in KPI-22 |
| 12 | Medium | Invariant ambiguity | Guardian "never modifies" (`03`) vs "clips" (security doc) | X-2 in A2 resolution | Decide PASS/VETO only before any guardian code |
| 13 | Medium | Premature generality | "Any service type by configuration" for 9 types, proven for 0 | Brief §3.5 | Concrete profiles first; extract the schema after 2 real onboardings |
| 14 | Medium | Safety | Guardian's own clock integrity isn't a first-class check | G-table; `05` CONSERVATIVE triggers | Add a G-row for time quality |
| 15 | Medium | Catalogue | 280 FMs / 161 threats derived by hand; completeness argued in prose | `05` §1.6, §3.13 | Generate from invariants × components × failure types; CI coverage gate |
| 16 | Medium | P7 platform | Keycloak/OPA/cert-manager, split contracts-rt/batch, and the allocator/shard split are sized for production | `06` §1.8; R30, R43 | Static roles + mTLS; one process; keep the interfaces |
| 17 | Low–Med | Optimizer | C19 hard charging ban duplicates the price term; CVaR release outside the LP | `03` §6.5, §7.4 | §5.4 items 5–6 |
| 18 | Low–Med | Cost | Full SCADA protocol stack regardless of contract size | `07` §7 | Light tier gated by contracted kW |
| 19 | Low | Doc hygiene | Resolution log summary shows 0/172 addressed; per-finding tables show many fixed | `06-reviews/00-resolution-log.md` | Fix the generator |

## 7. Document change plan (spec → epics/stories → test plan, before any code)

No code exists, so every recommendation lands first as a document change. Listed in the order to make them.

### 7.1 Spec — `00-brief.md`, `00-decision-register.md`

- Add **P4 commitment lock** to the design principles, and a register decision (D-new) recording the user's rule and its
  exceptions (L0/L1/L2, infeasibility, §7.4 audited release).
- Record the **scope cut**: adopt the thin slice in §7b as the literal `MVP-J` and retag the rest to `MVP-B`/`R2`
  (answers Q23). Mark ECRS and NOIE-zone ERCOT lanes as unavailable/simulated inputs.
- Resolve X-2 (guardian PASS/VETO only vs clip).

### 7.2 Spec — `02-architecture/03-decision-engine.md`

- §2.4: the commitment lock as the general ring-fence rule; rules 1–7 become special cases; applies to every obligation
  type.
- §2.7 / `02-domain-model` §2.4: obligation lifecycle offered → selected → committed → delivering → fulfilled → settled.
  Annotate the `SERVED → ARBITRATING` edge ("may change the hub, never the obligation").
- §6: add $x_o$ and C24. Make C16 a Must. Lexicographic DA/ID, or a proof that the penalty slopes dominate. C19 becomes a
  price adder. CVaR release embedded in intraday. §6.8 equality freeze. Add a conservatism budget.
- §8.1 S2/S4/S6 and §8.4: "economics inside tiers, over unreserved capacity only"; committed $\hat y$ subtracted at S2.
- §8.12: feeder/substation ramp ceiling for firm events.
- §9.2: `R-COMMIT-LOCK-*` reason codes. §12.4: KPI-22 reports forgone spot upside and the conservatism budget.

### 7.3 Spec — security, failure modes, SCADA

- `03-security/02-security-architecture.md` §6.3: add **G-19** (commitment lock), a guardian clock-quality check, and the
  feeder ramp ceiling. §10: add a commitment-churn detector.
- `02-architecture/05-failure-modes-and-recovery.md`: add K13 to the P-table. Define the catalogue as generated from
  invariants × components × failure types.
- `02-architecture/06-platform-and-operations.md`: add a demo profile (one process or docker-compose, static roles +
  mTLS). The Kubernetes topology remains the production design.
- `02-architecture/07-scada-integration.md`: a light protocol tier gated by contracted kW.

### 7.4 Product — `01-product/02-functional-requirements.md`, `03-epics-and-user-stories.md`

- Add FR-ARB-014 (commitment lock). Limit FR-ARB-004 and the per-contract T-order to pre-commitment selection. Raise
  FR-DE-039 to Must.
- A new story under the arbitration epic: "As a counterparty, my committed delivery is not reallocated to a
  better-paying call", with acceptance criteria that mirror §3.3 item 7.
- Retag stories to the thin slice (§7b). Trim personas with no `MVP-J` workflow.

### 7.5 Test plan — `05-testing/*`

- `02-test-cases-functional.md`: commitment-lock fixtures (same-tier rising value; cross-tier; a new call during
  delivery; each L0/L1/L2 exception; infeasibility with and without a substitute; substitution allowed).
- `03-test-cases-nonfunctional.md`: guardian G-19 negative tests (an unsigned reduction is refused); replay assertion
  that 100% of reductions carry a reason code; RT LP timing with commitments subtracted.
- `04-traceability-matrix.md`: generate it now, since it is the actual completeness proof. Include P1–P8 and K1–K13.
- Rerun `build_traceability.py` and `build_resolution_log.py` (also fixes the 0/172 summary).

### 7.6 Simulators and deck (not orchestrator code)

- Streamlit simulators: add a shared capacity ledger so tabs cannot double-book. Put η and the kW limit into the
  arbitrage math.
- Deck: carry the spec's "unverified" caveat on the 80% capture figure and the IRRs that depend on it.

## 7b. Implementation sequence (once the documents are frozen)

1. **Prove the loop.** One simulated hub, `HOME` + one dispatchable service (`DIST_DEFERRAL`: it exercises the ledger and
   a real feedback loop). Guardian-signed command → ack → trace → one invoice line. Deliver it as one process with
   Postgres, MQTT and docker-compose (not Kubernetes). *Proves:* trace completeness, zero reserve violations.
2. **Prove arbitration and commitment lock.** Add a second, conflicting service. Add the single-writer ledger with
   **K13 enforced at the allocator and the guardian**. *Proves:* KPI-24 (0 kWh billed twice), commitment-lock fixture,
   arbitration regret.
3. **Prove safety under load.** Independent safe stop, feeder ramp ceiling, comms-loss degraded mode. *Proves:* KPI-20,
   KPI-09.
4. **Real data plus a rule allocator.** Naive forecast, rule-based selection at gates. This is also the KPI-22 baseline.
5. **LP/MILP stack (§5.2).** Replace rule selection with L-DA/L-ID, and measure value added and the forgone-upside cost
   of the lock against the rule baseline.
6. **Breadth.** Remaining profiles (tests the generic abstraction), one DNP3/TLS SCADA association, AI advisor toggle,
   insight views.
7. **R2.** ICCP/2030.5/IEC 104/OPC UA, NCLR/SB 415/`MOBILE_DER`, full IAM, shard split, multi-zone HA.

## 8a. Decisions by the user (2026-09-25)

| # | Question | Decision | Design consequence |
|---|---|---|---|
| 1 | Commitment granularity | **Multi-day and tolling contracts may have agreed re-nomination points** | The lock runs until the next contractual re-nomination point or fulfilment, whichever comes first. Each re-nomination point is an extra selection gate for **that contract only**. Between points, P4 applies in full |
| 2 | Divisible commitments | **Partial take is allowed, subject to ERCOT and marketplace rules** | Every product/marketplace carries `min_qty`, `increment`, `block`/all-or-nothing, and duration rules. The selector uses a continuous $q_o$ where the rules allow and a semi-continuous or binary variable only where they require it |
| 3 | Audited exceptions (§7.4) and events | **Every event must have a track record, retained for a configurable retention period** | The §7.4 release stays as the single audited exception (default off). *All* events are traced in the hash chain: selections, commitments, re-nominations, exceptions, shortfalls, operator actions, feed changes and alerts. Retention is configurable per event class, with pruning that keeps the chain verifiable (checkpoint anchors) |
| 4 | Simulator source | **The simulators are not the solution; they demonstrate the concept** | The orchestration engine is built fresh from the spec. Simulator code is not ported, copied or used as a baseline. Findings about simulator behaviour (§2, §6 #5, #8, #10) are concept-level observations only, not defects in the solution. The §12.1 back-test baseline is specified independently in the spec |
| 5 | "Scope cut" | **Build the orchestration engine**; this is not a reduced demo | The Saturday release is the first working increment of the full engine. The modular design means the remaining spec scope adds modules, with no rewrite. The document set remains the full engine spec |

## 8. Questions for the user (original)

1. **Commitment granularity.** Is a lock always the *whole* contracted delivery window? Or may a multi-day or tolling
   contract have internal gates (e.g., daily re-nomination) where re-selection is contractually allowed?
2. **Divisible commitments.** Can an opportunity be taken partially (continuous $q_o$, keeps the model LP), or is it
   all-or-nothing (binary $x_o$)?
3. **§7.4 AS forward release.** Keep it as the single audited exception (default off), or remove it entirely under the
   commitment rule?
4. **Simulator source.** Can the base-server simulators (`/opt/opengrid_sim`: control_engine.py, fleet_lp.js) be copied
   into this repo so the spec's §1.5 lessons and the §12.1 baseline can be verified?
5. **Scope cut.** Adopt the thin slice in §7b as the literal `MVP-J`?

## 9. What the spec gets right

Authority rooted in law and physics (ERCOT instruction as an equality constraint, never a priced call). A single-writer
reservation ledger with the additive one-floor (fixing a real double-count). The guardian as sole signer, a stop-only
Safe-Stop Authority, and an independent epoch authority. TIMEOUT ≠ VETO ≠ STOP. Lexicographic real-time arbitration.
kVA/per-phase bank control with feed-forward add-back and anti-windup. Robust firm sizing, an independent plan validator,
and price-of-firmness duals. The AI agent kept out of the control loop. Provenance labels on every unverified number,
plus a real adversarial-review process. The spec's diagnosis of its own problems is largely right; what remains is to
act on it: cut the scope and make the commitment lock a named, independently enforced invariant.
