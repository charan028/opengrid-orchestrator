# OpenGrid Orchestrator — Documentation Brief (shared by every author)

Status: v0.2 · 2026-09-25 · Owner: project lead · Audience: every spec author and reviewer.
Read this first. It pins the vocabulary, scope, defaults and conventions every document must use, so that
documents written in parallel by different experts fit together. Where this brief and
[`00-decision-register.md`](00-decision-register.md) disagree, the register wins; normative values are cited as
"register V-nn" (register §F) and never restated with a different number.

**Changes in this version (v0.2 — resolution pass after the four adversarial reviews; dispositions in
`06-reviews/resolution/A1-product-brief.md`).**
- §2: judged-demo date, team and build order stated with their register defaults (Q22, Q23, R21); every requirement
  carries a build tag (ARC-001, JDG-001).
- §3.1: customer types per R17 (ERCOT lanes with ALR/NCLR variants; ERCOT instructions for an on-line ADER are hard
  constraints), R18 (bank relief in kVA or per-phase current), R20 (`MOBILE_TEEEF` statute-shaped; grid-parallel support
  as the `MOBILE_DER` contract variant) and R27 (`PARTNER_CAPACITY` tolling and event variants; `DIST_DEFERRAL`
  co-op/municipal and TDU (SB 415) variants; `PJM_CAPACITY` self-serve); the allocation priority places grid-operator
  instructions (L2) above firm contracts and ring-fences awarded reserves (GRD-001, GRD-005, GRD-012, GRD-015, GRD-048,
  JDG-010).
- §3.3 (new): market roles per territory and the demo territory (R27, R27a, Q6, Q12, Q25; GRD-016, JDG-017).
- §3.2, §3.4, §3.5: reviewer proposals annotated with the resolutions that refine them (R18, R28, R29, R44, R47, V-28,
  V-33, V-34).
- §4: control cycle and telemetry cadence per V-03/V-32; 10:00 CT DAM offers vs 14:00 CT firm declarations (JDG-011);
  Valkey instead of Redis; load generator off the node (R35); no reboot of the shared host (R46, ARC-028); production
  cutover after the judged demo and QSE-dependent ICCP connectivity (Q4, Q6, Q22; ARC-031, GRD-050).
- §5: the independent `safe-stop` service (R16); `contracts-rt`/`contracts-batch` (R43); the fleet allocator and
  execution shards inside `dispatcher` (R30); `agent-sim` off the node (R35); `DeviceAdapter` in `device-gateway` (R23).
- §6: vocabulary adds IsoInstruction, CurrentOperatingPlan, DeviceAdapter, `SHADOW` mode, control-partition terms, hub
  connectivity vs eligibility, protective vs non-protective stops and the market-role terms.
- §7: build tags `MVP-J` / `MVP-B` / `R2` (R21); test, traceability and review file names (C-23).
- §9: failure scenarios add the EEA posture (R19), ICCP/QSE-link loss (R25), a frequency event (R26), a guardian
  timeout — not a veto (R31) — and a stop with the guardian down (R16).
- Regulatory facts aligned with the primary-source check `06-reviews/05-claims-verification.md` (claims 1, 3–8, 10, 12,
  15) and with the register's revision of the same day (Q7 answered, V-33): ALR/NCLR behaviour and NCLR disqualification,
  ADER pilot caps (500/100/100 MW, ≤ 90% per QSE), ECRS 1 h and Non-Spin 4 h, PURA §39.918 as amended by SB 231 and its
  reach, PURA §35.153 for SB 415, the EEA charging rule's scope, the DNP3 stack's bindings and TLS.
- Register revisions of the same day (10:15 and 10:34) applied: telemetered AS capability, not offers, is the ISO boundary
  and NCLR disqualification lasts ≥ 6 months (§3.1, §6; R17); node memory and the 10,000-hub rule — the `demo` profile fits
  at ≈ 92% of pod memory, the 10,000-hub runs use the node only if it can carry them and otherwise the replica VM, labelled
  (§4; R35, Q26); the assumed decommission date (Q4) and the ClamAV update windows (Q27).

---

## 1. What we are building

**The Orchestrator is the "brain" of Base Power's home-battery fleet**: a production-grade, cloud-native service that
runs thousands of home batteries as ONE portfolio and sells the same kilowatts to different customer types at different
hours, while every home keeps its backup reserve. It:

1. **Pulls real data** from the external APIs the simulators already use (ERCOT, EIA, …) plus new public sources where
   needed (e.g., NWS weather), validates it, and tolerates their failures.
2. **Talks to batteries** through a real device protocol behind a `DeviceAdapter` interface (R23). For the demo, a
   **mock battery agent** simulates many homes realistically (house load, EV charging, outages/islanding, faults, comms
   loss, firmware limits) from a host other than the node under test (R35). The orchestrator must treat these agents
   exactly as it would treat real devices: authenticate them, distrust their data, detect misbehaviour, and never assume a
   command was executed until telemetry proves it. A `SHADOW` mode runs the same brain on real telemetry and records
   commands without sending them — the path onto Base's real fleet.
3. **Decides**: forecasts, plans (day-ahead / intraday optimization), and runs a **real-time control loop** that
   allocates each interval's power and energy across customer commitments by priority, with feedback, substitution of
   failing batteries, and safe degraded modes.
4. **Dispatches committed services** to each customer type (below), verifies delivery with meter data (M&V), and
   produces settlement records.
5. **Is operable and secure**: HA design, self-recovery, retries with backoff, thresholds and alerts, audit trail,
   authenticated agents and dispatch requests, anomaly/abuse detection, protection against requests that would overload
   the grid or the fleet, and a stop path that works even when the command signer is down (R16).
6. **Has an operator UI** (control room console) that Base's operators could use tomorrow.

**Design principle — service-agnostic dispatch.** The orchestrator is an application that MUST dispatch energy as requested,
for every customer type: a request (kW or kWh profile, window, location/asset, ramp, duration) is executed within safety,
device, homeowner-reserve, grid-limit, authorization and contract-priority constraints, and its delivery is measured and
reported. ERCOT dispatch instructions and statutes are such limits (R17, R20), never a judgment of a service's value.
Whether a service achieves its intended impact (e.g., lower pipeline AC, a credited large-load offset, avoided upgrade) is
a separate question for the business case (the Projects Deck and simulators) — never grounds for refusing, down-ranking or
omitting a dispatch.

**Core job — call arbitration with full auditability.** When signals arrive to serve customer A and customer B (of the same
or different types, at the same or overlapping times), the orchestrator must: (1) accept and validate each call against
its contract; (2) arbitrate between them with explicit logic for **priority** (contract priority classes, firmness),
**commitments** (what is already promised: awards, firm windows, declared capacity, energy reserved) and **profitability**
(value of each call vs its cost: energy, degradation, delivery charges, penalties/liquidated damages, buyback exposure,
opportunity cost); (3) dispatch the chosen allocation; (4) measure delivery and produce **billing/settlement** records per
customer, contract and interval; and (5) record a **decision trace** for every decision — inputs, options considered,
constraints applied, the winning allocation and **why**, losers and their cost — in a tamper-evident audit log that links
call → decision → commands → telemetry → M&V → invoice, so any charge or any refusal can be explained and replayed.

**AI agent (where it adds value).** Where arbitration or operations need reasoning beyond encoded rules, the orchestrator
may use an **AI agent built on LLM APIs — cloud (e.g., Anthropic Claude models such as `claude-opus-5-5` for complex
reasoning and `claude-haiku-4-5-20251001` for fast summaries) or local (an OpenAI-compatible local server running an
open-weights model; on the CPU-only base node this is slow, so cloud is the default and local is the fallback/air-gapped
option)**. Model IDs are configuration, recorded in every decision trace (R12). The `ai-agent` service uses tool calls
against the orchestrator's own APIs. Uses: arbitration advisor for novel or multi-way conflicts (proposes an allocation with
rationale; an approved proposal becomes a time-boxed, versioned constraint set, and human confirmation is always required,
R49); natural-language explanation of decision traces ("why"); operator copilot (questions over live state, obligations at
risk, what-ifs); anomaly and incident triage and summaries; intake of unstructured customer requests into structured calls
(always confirmed). Guardrails: never in the hard real-time loop; every AI proposal passes the same contract validation, OPA
policy, guardian limits and human confirmation as any other request; deterministic rules keep running when the LLM is
unavailable, slow or wrong; prompts, tool calls, responses and model/version are recorded in the decision trace;
prompt-injection and data-leakage controls; no personal data to a cloud LLM (D5; aggregation floor register V-18); cost and
rate budgets (V-22).

The documentation set is written FIRST and reviewed by the user before any code is written.

## 2. How the result will be judged (the user's evaluation criteria)

Judging is done on the real, working system and its codebase — not slides, not a thin API wrapper.

| Criterion | Points | What the docs must make possible |
|---|---|---|
| Completeness | 15 | The core workflow runs end-to-end without crashing (data in → decision → dispatch → verified delivery → settlement), including under injected failures |
| Technical depth | 15 | Real engineering: control loop with feedback, optimization, state estimation, resilience patterns, security controls — not a wrapper |
| The problem | 15 | Directly attacks the track's problem: monetizing a distributed home-battery fleet across grid customers while keeping homes safe |
| The "why" | 15 | Clear articulation of why this approach (one fleet, many buyers, firm-first allocation) solves a meaningful problem |
| Insight quality | 10 | Non-obvious, useful outputs (e.g., which hours each customer owns, delivered-vs-committed kW, risk of breaching a firm contract before it happens) |
| Usability | 10 | Base (or another operator) could use it tomorrow: installable, documented, sane defaults, real data |
| Creativity | 10 | Novel combination of tools/data; looks and feels good |
| Performance | 10 | Optimized for speed and scale; measured, not asserted |

Every document should state which criteria it serves. Scope choices must favour a **working, demonstrable core** over
breadth, while the spec itself stays comprehensive (full failure catalogue, full test plan).

**Date, team and build order.** The judged demo runs on the node before the production cutover — default 2026-10-21
(register Q22). Default team: built by Claude with parallel agents, reviewed by the user (Q23). Build order (R21) is
**sequencing only — nothing is dropped**: Line A (`MVP-J`) starts with a walking skeleton — one hub to one invoice line by
day 5, all nine customer types dispatchable through profiles on a minimal stack — and grows it into the judged core;
Line B (`MVP-B`) adds the planner, Insights, value of orchestration, performance evidence, the OpenADR VEN, AI explanations,
`SHADOW` mode and the demo profile; everything else is `R2` with its design unchanged. Every requirement and story carries
its build tag; the release map is in `01-product/03-epics-and-user-stories.md`, and the judged-demo gate is G3-J
(`05-testing/01-test-strategy.md`).

## 3. The service (from the existing simulators — the source of truth for the use cases)

Live today at https://base.tocy-net.net/opengrid/ (control room `index.php`; simulators `service.php?type=grid_arbitrage`,
`transformer_deferral`, `datacenter_bridging`, `pipeline_smoothing`; the fleet-mix optimizer `optimizer.php`; the business
case deck `presentation.php` — the **Projects Deck**). Backend on the base server (192.168.5.35) in `/opt/opengrid_sim`:
`ercot_live.py` (real ERCOT data every 60 s; token reuse: register §D, Q18), `scada_simulator.py` (simulated SCADA
signals), `control_engine.py` (today's rule-based allocator: firm contracts first, then hypothesis services, then market;
fail-safe on bad signals; energy reserved for firm windows), MariaDB `opengrid_sim` (assets, scada_signals,
dispatch_events, billing_records, fleet_allocation). The optimizer is a MILP (HiGHS) over representative days
(`fleet_lp.js`). The local prototype is in `G:\OpenGrid\src\opengrid` (clients: `ercot_client.py`, `eia_client.py`,
`geo_client.py`, `solar_client.py`; `analytics.py`; `transforms.py`).

### 3.1 Customer types (services) the orchestrator must dispatch

**Every business case from the simulators and the Projects Deck is in scope. Nothing is dropped.** The independent
reviews challenged each case; their challenges become conditions the orchestrator must help validate (evidence, M&V,
pilots), never reasons to leave a service out. Where a review showed that a service must be dispatched differently
(an ERCOT rule, a statute, a contract form), the service keeps its code and gains a profile variant (§3.5).

| Code | Customer / service | What is committed | Reviewers' main challenge (to be answered with evidence) |
|---|---|---|---|
| `HOME` | The homeowner | Backup reserve (default 20% SOC, homeowner-adjustable), home load served first, opt-outs; reserves pre-positioned on forecast risk, never raised by grid charging during an ERCOT EEA (R19) | Always the first constraint — never violated for revenue |
| `ERCOT_ENERGY` | ERCOT wholesale market via an ADER, registered as an ALR (Aggregate Load Resource) or an NCLR (Non-Controllable Load Resource) variant (R17) | ALR on line: the aggregate net load of the member premises follows ERCOT's set point (NPC regulation) — a hard constraint, never squeezed by another customer; price-responsive energy dispatch only for premises whose ADER is off line or unregistered | Merchant and compressing; delivery charge on exports; market roles per territory (§3.3) |
| `ERCOT_AS` | ERCOT ancillary services via an ADER (ALR or NCLR variant, R17) | Non-Spin and ECRS energy holds per register V-33 — ECRS 1 h and Non-Spin 4 h, a profile field that falls to 2 h when NPRR1309 is implemented (NPRR1282; Q7 answered by evidence); ADER pilot caps of 100 MW Non-Spin and 100 MW ECRS system-wide (500 MW registered in all), no QSE above 90%, plus each ADER's qualified MW; held capacity ring-fenced inside awarded intervals | ADER caps — ECRS is at its system cap, so plan with no ECRS headroom (`06-reviews/05` claim 15); RTC+B buyback exposure on a forward release or a capability loss |
| `PARTNER_CAPACITY` | Co-op / muni capacity programs (Austin Energy 40 MW battery tolling agreement, CoServ 100 MW, GVEC 50 MW) | `TOLLING` variant: continuous reservation of the tolled kW and kWh, utility-scheduled charge and discharge, cycle budget, availability settlement; `EVENT` variant: utility-dispatched events, ~1.5 h, ~10 kW per hub at system peaks (4CP) (R27) | Measured P10 kW per hub; export limits; 4CP rule changes; dual participation with an ADER per partner mode (Q6) |
| `DIST_DEFERRAL` | Substation / feeder deferral: co-op and municipal variant; TDU variant under SB 415 (PURA §35.153; the implementing rule 16 TAC §25.58 is still proposed) with a reservation calendar ring-fenced from ERCOT, discharge for the contract only on the TDU's direction, and the TDU's load-ratio share of the 100 MW statewide cap (R27) | Firm relief of a constrained bank measured on the quantity its rating protects — apparent power (kVA) or maximum per-phase current (R18) — from homes electrically behind it, in need windows, sized on year-10 capacity (~3.7 kW/home) | Measured sites (SCADA, N-1, growth); price vs the utility's alternative; strict M&V (outcome- or share-based, Q9) |
| `LARGE_LOAD` | Data-center / large-load grid-side offset | Discharge in the zone during the load's stress events; the capacity is withheld from ERCOT's view before the event, not during it (R17) | Whether an off-site offset is credited (SB6); deliverability |
| `PIPELINE_AC` | Pipeline AC interference: battery-based smoothing (H1/H2, pilot) and corridor monitoring (H3) | Smoothing band on co-located corridors, with the line-current sensitivity (shift factor) as a profile parameter and an open-loop kW schedule when it is unknown (R28); alerts on corridors whose line configuration changed | Standards score time-weighted AC; needs a validated corridor model and field test |
| `MOBILE_TEEEF` | Mobile restoration batteries leased to utilities (separate assets), statute-shaped under PURA §39.918 as amended by SB 231 (R20): island-forming only, under the lessee TDU's operational control, admitted only with a lessee-declared qualifying outage, no ERCOT telemetry or market participation, units mobile and of 5 MW or less; Base reports readiness and never initiates energization. §39.918 does not reach co-ops or municipal utilities, which lease under their own recorded legal basis. Grid-parallel planned support is the separate `MOBILE_DER` contract variant with its own interconnection agreement | Availability and deployment of 1 MW / 2 MWh units; island load planned at the measured cold-load factor within the unit's short-time rating | Eligibility ruling; grounding/island protection; cold-load pickup; separate capex; field-safety sign-off (Q20) |
| `PJM_CAPACITY` | PJM peak-load (PLC) reduction, Illinois/ComEd | Discharge toward meter net load ≈ 0 at the 5 coincident peaks unless export is paid; non-firm by default; premises registered with a PJM curtailment service provider excluded unless the contract handles it (R27) | Residential PLC method; one-year lag; VPP stacking |

**Allocation priority (default; precedence levels of `02-architecture/03-decision-engine.md` §2.3):** L0 safety
(guardian and device protection) > L1 homeowner reserve, opt-out and storm hold > L2 grid-operator instructions —
authorized utility stops, blocks and limits, and **ERCOT instructions for an on-line ADER, which are hard constraints,
never squeezable calls (R17)** > T1 firm contracts in their window (`DIST_DEFERRAL`, `PARTNER_CAPACITY` tolled capacity and
events, `LARGE_LOAD` contracted events) > T2 awarded ancillary services (`ERCOT_AS`) > T3 energy (`ERCOT_ENERGY`
price-responsive dispatch outside an on-line ADER) > T4 pilot contracts (e.g., `PIPELINE_AC` smoothing). The T-order is
configurable per contract. Arbitration between customers happens **before the fact**, in what ERCOT can see: MPC/LPC,
ramp rates, AS capability, offers and the Current Operating Plan are computed from ledger-free, guardian-permitted capacity
(R17), and a guardian invariant keeps telemetered AS capability ≤ ledger-free capacity for each product's duration (ERCOT
builds proxy AS offers from telemetry, so telemetered capability, not offers, is the boundary); a firm commitment is
therefore never met by deviating from an ERCOT instruction. An awarded reserve is never diverted to a
firm event inside its awarded interval; RTC+B buyback applies only to a forward release or a capability loss (utility
block, safe stop, hub loss). One kWh must never back two buyers.

### 3.2 Reviewer-proposed requirements — arguments to be proven, not facts

The independent reviewers' statements are **claims to be proven or disproven with evidence** (a fact-check against primary
sources is recorded in `G:\OpenGrid\docs\business-case\01-reviewer-claims-verification.md`; the reviews of the
specification itself are checked in `06-reviews/05-claims-verification.md`). Their proposed numbers are adopted as
**candidate acceptance criteria**, labelled "reviewer proposal — unverified", to be confirmed with real contracts, data and
pilots; each is a configurable profile field (R11). The orchestrator's own role is to dispatch, measure and bill what it
delivers (M&V).

Proposed by the grid-engineering reviewer (see the deck's "what would satisfy the reviewers" slide); *italic notes* give
the register resolutions that refine a proposal:
- Firm kW sized on end-of-term capacity; contract kW ≤ homes × year-10 AC kWh ÷ design-day need hours × 0.95, + ≥ 20% over-enrollment.
- Control law on **measured load + fleet output − (rating − margin)**, with deadband and ramp limits (no hunting); on a bad
  signal hold the prior setpoint, then run the day-ahead schedule; reserve the energy firm windows need. *R18/R28: regulate
  apparent power or the maximum per-phase current against unit-typed ratings, adding back the fleet's own active and
  reactive power at the SCADA sample's source time; exactly one integrating loop per bank; return from HOLD per V-38.*
- Allocate a site's obligation only from homes **electrically behind** that asset (service point → transformer → feeder → bank).
- No charging behind a constrained bank during its need window; ramp recharge; no rebound above 95% of the bank rating.
  *R28: reserve recovery inside a need window only within bank headroom.*
- Performance: every 15-min interval ≥ 95% of contract kW (season ≥ 98%); availability ≥ 97% of need hours; full output
  ≤ 5 min after dispatch. *V-34: design target p99 ≤ 240 s from event receipt.*
- Telemetry: per-bank kW, kWh and hubs online at ≤ 1-minute resolution to the utility (IEEE 2030.5 / DNP3), ≥ 99%
  availability, ≤ 60 s latency, day-ahead declaration by 14:00, utility override. *14:00 CT is the firm-declaration
  deadline; ERCOT day-ahead market offers close at 10:00 CT (§4).*
- M&V: revenue-grade 1-minute meter data per hub, reconciled to 15-minute smart-meter data; data within 24 h.
- Partner programs: measure P10 delivered kW per hub; respect export limits; 4CP rule changes (PUCT Project 58484).
- Market: RTC+B real-time buyback when awards are diverted; ADER per-product caps; Non-Spin 4 h / ECRS 1 h energy holds.
  *R17/JDG-010: an awarded hold is ring-fenced and never diverted inside its awarded interval; buyback applies to a forward
  release or a capability loss; the ECRS duration is a profile field set by Q7 (V-33) — 1 h under NPRR1282
  (`06-reviews/05` claim 6).*

### 3.3 Market roles and territories (R27, R27a)

- **Per-territory role model** in `contracts`: for every territory and ADER the Load Serving Entity (LSE), Qualified
  Scheduling Entity (QSE), Resource Entity and Distribution Service Provider (DSP) are recorded; per-utility tariff tables
  (NOIE bundled rates, buyback terms) replace a TDSP-only tariff. An ALR-type ADER has one load zone, one LSE and one DSP;
  an NCLR-type ADER needs a signed acknowledgment from every LSE; premises of 100 kW or less belong to the submitting LSE in
  both models (ADER governing document 3.3; `06-reviews/05` claim 4).
- **NOIE territories** (municipal utilities and co-ops): the NOIE's consent (as DSP, per premise) is a condition of every
  ERCOT lane, and ERCOT value flows through the NOIE's contract with Base. ERCOT's tracking sheet shows 0 MW of ADER
  approved in `LZ_AEN` and `LZ_CPS`, so that consent gates any ERCOT lane there (claim 15).
- **Dual participation** is set per partner (Q6): partner-as-QSE (the partner's own QSE decisions arbitrate its two
  services and Base executes them) or Base-as-QSE (allowed only when the partner's calls reach ERCOT beforehand through
  telemetry and offers). The ERS exclusion is a hard admission rule. The verified co-op case shows one fleet serving both
  lanes; continued concurrent use after ADER qualification is an assumption to validate with each utility (claim 8).
- **Demo territory (R27a, Q25):** the fleet model has both kinds of partition. The ERCOT lanes (`ERCOT_ENERGY`, `ERCOT_AS`)
  are demonstrated on a competitive-area partition; the NOIE partitions (Austin Energy, CPS Energy) carry `PARTNER_CAPACITY`
  (tolling) and `DIST_DEFERRAL` and show the NOIE-consent condition.
- **Per-premise data (Q12, D5):** ERCOT's ADER rules require premise- or device-level data on ERCOT's request. Until the user
  answers Q12, the ERCOT lanes stay simulated and no real per-home data leaves the system; every other counterparty receives
  aggregates only (V-18).

### 3.4 SCADA integration of the fleet (first-class requirement)

The fleet must be integrated with utility and ISO SCADA/EMS/DERMS systems **with all the necessary details and hooks** —
specified in `02-architecture/07-scada-integration.md` and reflected in every other document:
- **Northbound (the fleet as a SCADA-visible resource):** aggregated points per bank, feeder, substation, load zone,
  program and resource (kW, kVAr, available kW/kWh by duration, SOC, hubs online/offline/islanded, mode, alarms), control
  points (setpoints, enable/block, curtailment, emergency stop, utility override), exposed via DNP3 (IEEE 1815, secure
  authentication and TLS per IEC 62351), IEC 60870-5-104 where required, IEEE 2030.5 for DERMS, and ICCP/TASE.2
  (IEC 60870-6) for QSE-to-ERCOT telemetry; point maps, deadbands, report-by-exception, event classes, quality flags,
  timestamps and time synchronization, select-before-operate, redundancy. ERCOT-facing telemetry (MPC, LPC, ramp rates,
  AS capability) is computed from ledger-free, guardian-permitted capacity (R17).
- **Southbound/ingest (grid measurements the orchestrator uses):** substation bank/feeder SCADA (DNP3 master polling or
  ICCP/utility historian feeds, OPC UA), AMI/meter data, with quality handling, failover and latency budgets; bank ratings
  and limits are unit-typed (kVA, kW, A) and carry phase (R18).
- **Hooks:** protocol gateway service (`scada-gateway`), point-mapping registry, alarm and sequence-of-events mapping,
  interlocks between SCADA commands and the orchestrator's own dispatch (who wins, when), commissioning/point-to-point
  tests, simulators for DNP3/ICCP counterparties, and conformance testing.
- **Command-order protection (D4a, R29):** select-before-operate or direct operate per point as the point map's per-point
  column says (`07` §3.1), DNP3 application-layer sequencing, Secure Authentication anti-replay and state-machine
  preconditions; the extra `COMMAND_SEQ` point is required only on associations without Secure Authentication.
- **Build order and licences (R21, R44, Q11):** `MVP-J` carries DNP3 over TLS — one outstation association and one master
  poll of the `grid-sim` RTU — on a stack chosen by a week-1 spike: the commercial stack has no Python binding and a
  non-production public licence, and the open-source Python binding has no TLS in its default build, so TLS may be
  terminated outside the DNP3 process (`06-reviews/05` claim 12); without an ICCP licence the ICCP path is a labelled
  protocol-level stub (`SIM`) in the judged evidence. IEEE 2030.5 server, ICCP/TASE.2, IEC 60870-5-104 (Could, V-28),
  OPC UA, DNP3 Secure Authentication and redundancy are `R2` with their design unchanged.

### 3.5 Service-type dispatch handling (what the orchestrator must know)

Research and evaluation of the business cases (with academic partners) belongs to the **Projects Deck and the
simulators** as a use case — **it is not an orchestrator function**. The orchestrator must receive signals and manage
actions **regardless of client and service type**, and it must know how to handle each service type's dispatch. Every
service type is described by a **dispatch profile** the orchestrator executes generically:

| Profile element | Meaning |
|---|---|
| Signal sources and protocols | Where requests come from (SCADA/ICCP/DNP3, OpenADR 3.0, IEEE 2030.5, ERCOT QSE instructions, market prices, customer APIs/webhooks, operator console, schedules) |
| Request schema | kW/kWh profile or setpoint, window, asset scope (fleet, zone, substation, bank, feeder, corridor, unit), ramp limits, duration, notice time, firmness |
| Validation and admission | Contract check, authorization, feasibility (energy, power, topology, homeowner reserve), territory role and consent conditions (§3.3), guardian limits, confirmation for critical impact |
| Allocation and control mode | Open-loop profile, closed-loop regulation on a measured signal (e.g., bank kVA or phase current, line current, an ADER's net load), price-responsive, event-based; which hubs may serve it |
| Priority class and arbitration rules | Firmness, precedence, displacement cost, profitability inputs |
| Completion and performance rules | What "delivered" means (per interval), tolerance, response time, sustain time |
| M&V and billing rules | Baseline method, metering source, settlement interval, price/penalty terms, invoice lines |
| Failure behaviour | Substitution, degraded modes, customer notification, hold-then-schedule rules |

Profiles exist for every customer type in §3.1 (and new types can be added by configuration, not code). A **profile
variant** (ALR/NCLR, `TOLLING`/`EVENT`, co-op/TDU, `MOBILE_TEEEF`/`MOBILE_DER`) is a versioned profile of the same schema,
selected per contract. Profiles are versioned, signed and effective-dated; the activation gate is tiered by risk (R10,
R47): tighten-only or safety changes pass the golden week plus a guardian-envelope check, and loosening priority or limits
passes the replay of the real ERCOT year; a running event keeps its profile version (V-27).

## 4. Deployment target and defaults (assumptions — the user may override any of them in review)

**Deployment (decided by the user):** Kubernetes on the **base server, 192.168.5.35** (base.tocy-net.net), for deployment
and testing. Node facts (read-only checks, 2026-09-25): Debian 13, KVM guest, 8 vCPU, 15 GiB RAM (≈13 GiB free; Q26
asks whether the guest can be raised to ≥ 23 GiB), no container runtime yet. **Disks were expanded on 2026-09-25**: 300 GB disk; `/var` 157 GB (147 GB free), `/` 67 GB (62 GB
free), `/home` 31 GB (30 GB free), `/tmp` 2.7 GB — k3s can use its default data directory under `/var/lib/rancher`, and
disk is no longer the binding constraint on the node (memory is; the pod budget and its re-baselining are R2 and R35, and
`06-platform-and-operations.md` §1.8 is the only resource table, R14).
Apache already serves 80/443 (simulators, Roundcube) and must remain the edge reverse proxy; k3s's bundled Traefik is
disabled. The node also runs mail (Postfix/Dovecot, spamd, ClamAV), MariaDB and an unrelated `fdmp` project — **never
touch them**, and **never reboot the shared host**: node loss is exercised on a replica VM; on the shared node it is
simulated only by stopping k3s, with the host owner's written consent (R46); demo and test windows avoid the host's
ClamAV signature-update times (Q27). The load generator (`agent-sim` and the fault
proxy) runs **off the node** on a LAN host (R35, Q24), connecting to the node's LAN-only MQTT port (Q21), so the node's
performance evidence measures the system, not its own simulator. The server is scheduled for decommissioning around late
October 2026 (2026-10-30 assumed, Q4), so everything must be portable (Helm charts, no host-specific state). The **production cutover runs after
the judged demo** (Q4, Q22); plan B for the decommission lifts the single-node profile to one cloud VM. The production
target is a managed, multi-zone Kubernetes cluster; its ICCP connectivity depends on the QSE model (Q6): with Base as its
own QSE, ERCOT WAN routers terminate at physical control-centre sites that need a private interconnect to the cluster;
with a third-party QSE, that QSE's ICCP/DNP3 interface and its service level carry the ERCOT link (GRD-050).

**Default technology choices (pinned for consistency; justify or challenge them in the architecture ADRs):**

| Concern | Default |
|---|---|
| Services | Python 3.12, FastAPI + asyncio, Pydantic v2; optimization with HiGHS (`highspy`) |
| UI | TypeScript + React (Vite) console, served as static files; live updates via WebSocket |
| Device protocol (southbound) | MQTT 5 over mutual TLS (X.509 per device), EMQX broker; versioned JSON schema; signed commands (JWS ES256, V-36); one device contract owned by `02-domain-model-and-interfaces.md` (R33), behind the `DeviceAdapter` interface (R23) |
| Utility / market (northbound) | OpenADR 3.0 (partner utility events and schedules), IEEE 2030.5-style telemetry for utility DERMS, simulated ERCOT QSE interface (no real market access; ISO instructions and the Current Operating Plan, R17), webhooks/REST for other customers |
| Internal messaging | NATS JetStream (streams for telemetry, commands, events; at-least-once with idempotent consumers; one normative stream table in `02`, R34) |
| Data | PostgreSQL 16 + TimescaleDB (telemetry hypertables, continuous aggregates); Valkey (Redis protocol, BSD licence) for rebuildable hot state — locks, rate limits, caches, acknowledgement correlation (R43); leases and epochs live in NATS KV, never in Valkey |
| Identity | Keycloak (OIDC) for people; internal CA (cert-manager / step-ca) for device and service certificates; a separate safe-stop key hierarchy (R16, V-11) |
| Policy | OPA (Rego) for dispatch authorization and admission rules |
| Observability | OpenTelemetry → Prometheus + Alertmanager, Grafana, Loki, Tempo; the demo values profile keeps Prometheus + Grafana (R35) and pages through ≤ 25 rules (V-25) |
| Delivery | Container images with SBOM, vulnerability scan and signature; Helm; GitOps-ready; a demo values profile (`PROFILE=demo`, R35) with the MVP-J infrastructure set |

**Scale targets (defaults):** designed for 100,000 hubs in production (a labelled model until measured); tested with 10,000
simulated hubs, with the judged demo showing 2,000 live hubs and the 10,000-hub runs as recorded evidence (assumption,
JDG-006). The `demo` profile (2,000 hubs) fits the node at ≈ 92% of pod memory; the 10,000-hub profile with every
component does not (≈ 100.5%), so its runs use the node only if the micro-benchmarks show the savings or the guest's RAM is
raised (Q26), and otherwise run on the replica VM with the same chart and profile, labelled as such (R35; `06` §1.9.1).
Telemetry every 10 s per hub, and every 2 s during events and for members of an on-line
ADER (register V-32); control cycle 2 s during an active event and for members of an on-line ADER, 10 s otherwise (V-03);
an on-line ALR ADER's net-load regulator runs at a cycle ≤ 4 s (R17); SCED-aligned re-evaluation every 5 min; intraday
re-plan every 15 min; day-ahead: **ERCOT day-ahead market offers before the 10:00 America/Chicago close and firm
declarations by 14:00 America/Chicago** (JDG-011); the Current Operating Plan is resubmitted on changes ≥ 1 MW or ≥ 10%
and always within 60 min (R17). All times are stored in UTC; ERCOT intervals are handled in America/Chicago.

## 5. Service map (component names every document must use)

| Service | Responsibility |
|---|---|
| `market-data` | Pulls and validates external data (ERCOT prices and operating notices, EIA, NWS weather and alerts, PJM later); caches; record/replay with as-of provenance; publishes prices/load/forecast inputs |
| `device-gateway` | Terminates MQTT (EMQX), authenticates hubs, ingests telemetry, routes commands and acknowledgements; hosts the `DeviceAdapter` interface — the MQTT agent contract is one implementation (R23); stateless except acknowledgement correlation (R43) |
| `fleet-state` | Digital twin and state estimation per hub, site, transformer, feeder, bank, zone: SOC, available kW/kWh, health, trust score, connectivity and eligibility (V-29), phase and unit-typed ratings (R18) |
| `forecaster` | Home load, solar, prices, overloads, availability (with uncertainty) |
| `planner` | Day-ahead and intraday optimization (MILP): commitments, offers, reserve holds, firm-window energy, the Current Operating Plan (R17), cycle budgets, pre-positioning on forecast risk (R19) |
| `dispatcher` | Real-time control: one fenced **fleet allocator** solves the bucket-level lexicographic arbitration for every conflict component each tick and is the single writer of the reservation ledger; **execution shards** keyed by hash(hub_id), each with a fenced leader, water-fill its grants, assign per-hub sequence numbers and submit batches (R30); feedback control, ADER net-load regulation (R17), substitution, safe degraded modes, `SHADOW` mode (R23) |
| `contracts` | Customers, contracts, programs, obligations, events, territory roles, enrollments, baselines, M&V, settlement — deployed as `contracts-rt` (registry, admission, SCADA entitlement; cached) and `contracts-batch` (M&V, settlement, reports), one schema owner, separate database pools (R43) |
| `integrations` | Northbound adapters: OpenADR 3.0 VEN, IEEE 2030.5 server/client (application layer, R6), ERCOT QSE interface (simulated; ISO instructions in, offers and the Current Operating Plan out), customer webhooks |
| `scada-gateway` | SCADA/EMS protocol front end: DNP3 outstation and master, IEC 60870-5-104, ICCP/TASE.2 (QSE telemetry), OPC UA; point mapping, quality, time sync, select-before-operate, redundancy (IEEE 2030.5 and OpenADR application layers live in `integrations` — decision register R6) |
| `guardian` | Independent safety and security supervisor and the **only signer of anything that moves MW** (R1): command validation, grid-stress and overload protection, anomaly detection, stops; a TIMEOUT is not a veto (R31) |
| `safe-stop` | **Safe-Stop Authority** (R16): independent service in namespace `og-safestop` (≥ 2 replicas; no dependency on `guardian`, `dispatcher`, `contracts` or `api`) with its own key hierarchy (`safe-stop-only` EKU, V-11); signs only scoped `SAFE_STOP`/`CEASE`; triggered by the guardian or the out-of-band hardware-token path; **can never release** |
| `api` | Public/partner API and the console's backend (REST + WebSocket), authentication and authorization |
| `console` | Operator UI |
| `agent-sim` | Mock battery agents (many homes per process) with realistic behaviour and fault injection; runs off the node on a LAN host, never on the system under test (R35) |
| `grid-sim` | Simulated counterparties: substation SCADA, utility OpenADR VTN, large-load stress signal, ERCOT market and QSE interface (awards, set points, ISO instructions, EEA levels, frequency events, link loss) |
| `notifier` | Alert routing (Alertmanager receivers: email, chat, on-call) |
| `ai-agent` | LLM-based agent (cloud Claude API or local OpenAI-compatible model) with tool access to orchestrator APIs: arbitration advice, decision explanations, operator copilot, incident triage, request intake — advisory, policy-checked, fully audited |

Services may be co-located in fewer pods on the single node; the logical boundaries stay. The **dispatch-key epoch
authority** (R16) is a two-person custody function (security + SRE) that can invalidate a compromised guardian's
outstanding commands; it is not a service.

## 6. Domain vocabulary

Grid hierarchy: **ISO** (ERCOT, PJM) → **load zone** → **substation** → **bank** (transformer bank) → **feeder** →
**service transformer** → **service point** (ESI ID / meter) → **site** (home) → **hub** (Base battery system: 39.2 kWh,
11 kW inverter; usable 31.36 kWh DC after 20% reserve; 29.75 kWh AC per full discharge at 90% round trip). Service
transformers and service points carry their **phase**; bank ratings and limits are unit-typed (kVA, kW, A) (R18).

Commercial: **customer** → **contract** → **program** → **obligation** (a commitment of kW/kWh over a window, with a
performance rule) → **call** (any incoming request, before validation) → **event** (a validated call bound to an
obligation) → **dispatch** (the orchestrator's allocation) →
**command** (a setpoint to one hub) → **telemetry** (what actually happened) → **M&V record** → **settlement**.
An **enrollment** binds a hub to a program for effective dates, with an exclusivity group; a **reservation** is a ledger
entry holding a hub's kW/kWh for one obligation and interval (single writer: the fleet allocator) (R37).

ISO and market (R7, R17, R27):
- **IsoInstruction** — an ERCOT instruction for an on-line ADER: verbal dispatch instruction (VDI), manual deployment or
  recall, status change, emergency action. A hard constraint at L2 precedence, acknowledged, executed and traced — never a
  squeezable call.
- **CurrentOperatingPlan (COP)** — per ADER and hour for the next 168 h: status (ONL/OUTL), MPC, LPC and AS capability per
  product, computed from ledger-free capacity; resubmitted on changes ≥ 1 MW or ≥ 10% and always within 60 min.
- **ALR / NCLR** — the two ADER registration models: an Aggregate Load Resource is a Controllable Load Resource dispatched
  by SCED, whose online Non-Spin and ECRS arrive inside its set point with no separate deployment message; a
  Non-Controllable Load Resource is not dispatched by SCED but still awarded AS by it, deployed by XML instruction and held
  until recall, and two performance failures in a rolling 365 days disqualify it for at least 6 months (register R17;
  `06-reviews/05` claims 1, 3);
  **NPC** — the net power consumption ERCOT sees for an ADER; **UDSP** — ERCOT's updated desired set point for it, sent
  every 4 s; **EEA** — Energy Emergency Alert level.
- **LSE, QSE, Resource Entity, DSP** — the market roles recorded per territory; **NOIE** — a non-opt-in entity (municipal
  utility or co-op) whose consent conditions every ERCOT lane in its territory.

Control partitioning (R30, R32): the **fleet allocator** (one fenced leader) arbitrates each **conflict component** — the
set of calls and hubs whose constraints interact — at bucket level; an **execution shard** (keyed by hash(hub_id), stable
and independent of topology) water-fills the allocator's grants and submits command batches; **epoch** is the fencing
generation of a leader. A stop is a **scope broadcast**: one signed message per scope on a retained scope topic.

Devices and modes:
- **DeviceAdapter** — the interface between the orchestrator and a fleet: telemetry in, commands out, acknowledgement
  semantics (R23). The MQTT agent contract is one implementation; an adapter to Base's real fleet interface is another.
- **`SHADOW` mode** — plans, arbitration, traces and M&V are computed on real telemetry; commands are recorded and never
  sent; a shadow-vs-actual report compares them with what the fleet actually did (R23).
- **Hub connectivity vs eligibility** — connectivity is ONLINE, SILENT, OFFLINE or LOST; eligibility is eligible, probation
  or excluded; thresholds V-29 (R40).
- **Protective vs non-protective stop** — protective stops (safety, security, utility stop, guardian-triggered, Safe-Stop
  Authority) ramp per scope at once; non-protective stops are ramp-capped and held while frequency < 59.95 Hz or during an
  EEA (V-16).

## 7. Conventions (IDs, files, style)

- IDs (unique within their document; the traceability matrix links them):
  `FR-<AREA>-NNN` functional requirements · `NFR-NNN` non-functional (product `NFR-201…`, architecture `NFR-001…034`,
  platform `NFR-500…`; K1) · `ADR-NNN` architecture decisions ·
  `FM-<CAT>-NNN` failure modes, CAT ∈ {DEV device, HOME house events, COM communications, EXT external APIs, SCADA SCADA
  links and controls, DSP dispatch & grid, ARB dispatch profiles/arbitration/billing/audit, MKT market, PLT platform,
  DAT data quality, SEC security-triggered, AI ai-agent} · `TH-NNN` threats · `CTL-NNN` security
  controls · `E<nn>` epics, `E<nn>-S<nn>` stories · `TC-<TYPE>-NNN` tests, TYPE ∈ {FUN, INT, NFR, PERF, SEC, UI, UX, CHAOS, DR}
  · `UI-<SCREEN>-NN` UI requirements · `RB-NNN` runbooks · `ALR-NNN` alert rules. IDs stay stable: new IDs are appended at
  the end of their family, never renumbered.
- Every requirement: statement, rationale, acceptance criterion (measurable), priority (MoSCoW), **build tag**, source
  (user / reviewer / regulation / derived).
- **Build tags (R21):** `MVP-J` — Line A, the judged core (walking skeleton first); `MVP-B` — Line B (planner, Insights,
  value of orchestration, performance evidence, OpenADR VEN, AI explanations, `SHADOW` mode, demo profile); `R2` — built
  after the judged demo with the design unchanged. A tag is sequencing, never a scope cut (D0a). Where a requirement is
  delivered in two steps, the tag names the first and a note names the rest ("`MVP-J` (R2: …)").
- Every number is either sourced (public) or labelled an assumption. Normative values are cited as "register V-nn".
  Units: kW, kWh, kVA, A, kWh/kW for durations, $/kW-yr, $/MWh.
- Markdown, GitHub-flavoured; diagrams in Mermaid; tables for catalogues. Plain, precise English; no marketing.
- Files live in `G:\OpenGrid\docs\orchestrator\`:

```
00-brief.md                                (this file)
00-decision-register.md                    (decisions, resolutions, open questions, normative values; wins on conflict)
01-product/   01-vision-scope-personas.md · 02-functional-requirements.md · 03-epics-and-user-stories.md
02-architecture/ 01-system-architecture.md · 02-domain-model-and-interfaces.md · 03-decision-engine.md ·
                 04-external-data-integration.md · 05-failure-modes-and-recovery.md · 06-platform-and-operations.md ·
                 07-scada-integration.md
03-security/  01-threat-model.md · 02-security-architecture.md
04-ui/        01-ui-ux-specification.md
05-testing/   01-test-strategy.md · 02-test-cases-functional.md · 03-test-cases-nonfunctional.md ·
              04-traceability-matrix.md (generated by build_traceability.py from the "Covers:" field of every test case)
06-reviews/   01-review-architecture-sre.md · 02-review-grid-market-scada.md · 03-red-team-report.md ·
              04-review-judging-and-product.md · 05-claims-verification.md · 00-resolution-log.md ·
              resolution/ (per-owner dispositions of every finding)
```

## 8. Decisions from the user (binding; 2026-09-25)

The register's §A holds the full list (D0a–D0g and D1–D5); D1–D5 are repeated here for convenience. How each is applied
(approval tiers, ramps, the independent stop path) is in the register's resolutions (R3, R4, R16).

| # | Topic | Decision |
|---|---|---|
| D1 | Roles | The role catalogue must include **fleet operator**, **billing admin** and **system admin** (in addition to the control-room operator, reliability engineer, trader, program manager, settlement analyst, security analyst, SRE, auditor and executive roles). |
| D2 | Kill switch / safe stop | Scoped at three levels: **per bank**, **per zone**, or **the entire fleet** (all with reason capture, approval rules and audited release). |
| D3 | Mobile units in the UI | `MOBILE_TEEEF` keeps its own icon and colour outside the seven-colour customer-type palette (confirmed). |
| D4 | Command safety (SCADA and all control paths) | (a) **Protection from wrong command order**: every control path enforces sequence/ordering and state interlocks (monotonic sequence numbers, expected-state preconditions, rejection of out-of-order, stale or conflicting commands, select-before-operate where the protocol supports it). (b) **Confirmation of critical-impact commands**: commands above impact thresholds (magnitude, scope, fleet-wide mode changes, kill-switch engage/release, SCADA controls on banks/zones) require explicit confirmation and, above a higher threshold, a second approver. (c) **SCADA communication must be secure**: authenticated and encrypted (DNP3 Secure Authentication + TLS per IEC 62351, ICCP per IEC 62351-4, IEC 104 per IEC 62351-3/-5), segmented and monitored. |
| D5 | Privacy of personal data | Privacy rules for the personal data the orchestrator processes (homeowner identity and address, ESI ID/meter data, household routines inferable from load, location) must be aligned with **GDPR** and **CCPA/CPRA** and similar state and federal regulations — e.g., the Texas Data Privacy and Security Act, PUCT and Illinois Commerce Commission rules on customer/meter data, the FTC Act, and the U.S. DOE DataGuard energy-data privacy code: privacy by design and default, lawful basis, purpose limitation, data minimization, data-subject rights (access, correction, deletion, opt-out), retention limits, access logging and breach notification. **There is no data sharing with third parties at this time** — not with academic partners and not with a cloud LLM (the `ai-agent` may receive only non-personal or aggregated data; anything personal stays on the platform or uses the local model). |

## 9. Failure scenarios the user named (non-exhaustive; the catalogue must go far beyond)

- **Batteries:** unresponsive; not communicating; generating faults; not delivering the requested capacity; reporting an
  interrupted dispatch because of house events (an EV starts charging; the house loses power and islands on the battery;
  a grid outage); homeowner opt-out or reserve change.
- **External APIs:** rate limits, error responses, abnormal data, internal errors, timeouts, schema changes, stale data.
- **Dispatch / grid:** no transmission capacity; no available capacity; interruption; feedback-loop instability; sudden
  market price changes; changes in the optimal transmission path; need to switch to other battery sources; competing load;
  no capacity to deliver.
- **Grid emergencies and ISO operations:** an ERCOT **EEA** — reserves were pre-positioned on forecast risk; during the EEA
  no grid charging except recovery to the contractual minimum reserve at a capped rate or an explicit ERCOT instruction,
  awarded or deployed AS never withdrawn without a hotline call, a storm hold met by discharging less (R19; an operator
  policy — ERCOT's EEA charging rule binds registered storage resources, not this ADER fleet, `06-reviews/05` claim 7); a
  **frequency event** — the hubs' autonomous droop, volt-watt and volt-var responses act, while integrators, substitution
  and trust penalties freeze and setpoints hold (R26); **ICCP or QSE-link loss** — the ADER holds its last set point flat
  (never steps to zero) while the QSE desk calls ERCOT, agrees status and substitute telemetry, updates the COP and then
  acts on ERCOT's instruction (R25); lease expiry at hubs follows the local autonomy of V-07.
- **Platform:** capacity, scalability, high availability, operability, error handling, self-recovery, retries and delayed
  retries, thresholds, alerts; a **guardian timeout** — no verdict within the budget (V-35) leaves the batch unsigned, lets
  commands in force run to their lease and pages the on-call: a timeout is never a veto and never a stop (R31); the
  **guardian down** — a stop still reaches hubs through the Safe-Stop Authority, which can stop but never release (R16);
  audit store down — firm delivery continues on a signed, anchored local journal (R22).
- **Security:** authenticate agents and dispatch requests; detect abnormal requests; stress conditions; requests that could
  overload the fleet or create grid-stress conditions.
