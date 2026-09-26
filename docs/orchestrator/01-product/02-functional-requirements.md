# OpenGrid Orchestrator — Functional & Non-Functional Requirements

Status: v3.3 · 2026-09-25 · Owner: Product Manager (this document).

**Changes in this version (v3.3 — resolution pass after the four adversarial reviews; dispositions in
`06-reviews/resolution/A1-product-brief.md`).** IDs stay stable; new requirements are appended at the end of their area.
- **Register revision of 2026-09-25 10:15 applied:** the ISO-boundary invariant is on telemetered AS capability, not
  offers (`FR-SAFE-029`, `FR-ARB-013`); NCLR disqualification lasts ≥ 6 months (`FR-CTR-026`); the Q1 default approvers
  exclude the system admin (`FR-SAFE` area note, `-006`, `-008`); Q7 answered and V-33 filled (`FR-CTR-006`). **Revision
  of 10:34 applied:** the 10,000-hub runs use the node only if it can carry them — after the micro-benchmarks, or with the
  guest's RAM raised (Q26) — and otherwise the replica VM, labelled (`FR-SIM-013`, `NFR-210`; R35). Role codes `QSD` and
  `FSE` follow `03-security` §5.1 v0.2 (`FR-SEC-015`, `FR-CTR-024`; V-37).
- **Build tags on every requirement (R21; JDG-001, JDG-008, ARC-001):** the Priority column now reads
  "MoSCoW · build tag" (`MVP-J`, `MVP-B`, `R2`), following the judge's build order (all nine customer types dispatchable in
  `MVP-J`); §1 states the rule. `FR-ING-011` and `FR-CTR-014` rise to Must (NWS feeds R19; the PJM shape is needed for the
  replayed 5CP day); `FR-SCAD-004` is Could · R2 (V-28).
- **Rewritten rows:** `FR-SAFE-006/007/008/009/020/022` and the area note (R3 amended: single-person stop engage at every
  scope with a 15-min co-sign, Tier 2 release, automatic downward re-declarations; V-15…V-17; GRD-010, GRD-041);
  `FR-SAFE-014` (a guardian TIMEOUT is not a veto, R31); `FR-DISP-012` (price-responsive only for premises whose ADER is off
  line or unregistered, R17; GRD-001); `FR-DISP-003` (fleet output at the SCADA sample's source time, kVA/per-phase,
  GRD-060, R18); `FR-DISP-002`/`FR-DEV-003` (V-03/V-32); `FR-DISP-005/007` (V-38, R28, GRD-047); `FR-SCAD-011` (per-point
  SBO/DO, R29; GRD-053); `FR-SCAD-004` (V-28); `FR-PLAN-008` and `FR-BILL-004` (buyback only for a forward release or a
  capability loss; AS ring-fenced inside awarded intervals; JDG-010); `FR-INT-004`, `FR-PLAN-010` (10:00 CT DAM offers vs
  14:00 CT firm declarations, JDG-011); `FR-PLAN-012`/`FR-SAFE-015` (V-41, JDG-012); `FR-UI-007` (8-KPI headline
  scorecard, JDG-013); `FR-UI-008`/`FR-RPT-005` (orchestrator-measured facts linked to the Projects Deck, JDG-026);
  `FR-AI-013` (JDG-027); `FR-AI-004`/`FR-ARB-009` (R49); `FR-ING-001` (R27, R27a; GRD-016, JDG-017); `FR-CTR-003/004`
  (R27, R18; GRD-015, GRD-048); `FR-MV-001/002` (GRD-036); `FR-BILL-006` (GRD-037); `FR-FCST-003` (GRD-038);
  `FR-DEV-013/014/015` (GRD-020, JDG-028, C-14); `FR-SIM-013` (R35); `FR-SVC-004/005/015` (R47, V-27); `FR-TRACE-008` (R22);
  `FR-OPS-001` (R41, V-25); `FR-SEC-004/015` (V-37); `FR-PRIV-004…008/011` (V-18, V-19).
- **New requirements:** R16 safe-stop path (`FR-SAFE-025`, `-032`); R17 ISO instructions, ADER net-load regulation,
  ERCOT-visible capability and the COP (`FR-DISP-024`, `-025`, `FR-ARB-013`, `FR-SAFE-029`, `FR-PLAN-017`, `FR-INT-012`,
  `-013`, `FR-CTR-026`); R18 kVA/per-phase regulation and unit-typed ratings (`FR-DISP-028`, `-030`, `-031`, `FR-TWIN-013`,
  `FR-CTR-027`); R19 EEA posture and pre-positioning (`FR-SAFE-027`, `FR-PLAN-016`, `FR-ING-017`); R20 statute-shaped
  `MOBILE_TEEEF` and `MOBILE_DER` (`FR-CTR-024`, `-025`); R23 `DeviceAdapter` and `SHADOW` mode (`FR-DEV-017`,
  `FR-DISP-032`); R24 Insights, value of orchestration, performance strip and benchmark report (`FR-UI-023`, `-024`,
  `FR-RPT-011`, `-012`, `-013`); R25 QSE desk and "what ERCOT sees", ICCP-loss behaviour, independent utility stop path
  (`FR-UI-026`, `-027`, `FR-DISP-026`, `FR-SAFE-033`); R26 settings read-back and freeze on autonomous response (`FR-DEV-019`, `-020`,
  `FR-DISP-027`, `FR-TWIN-015`, `FR-SAFE-028`); R27 territory roles, tolling, dual participation, SB 415 variant, PJM
  self-serve, cycle budget (`FR-CTR-019…023`, `FR-PLAN-018`); plus the V-30 ramp table (`FR-SAFE-030`), R48
  clip-or-defer (`FR-DISP-029`), the R22 journal (`FR-TRACE-011`), R38 crypto-shredding (`FR-PRIV-014`), Q12 premise data
  (`FR-PRIV-013`), the JDG-028/029 device contract and demo profile (`FR-DEV-018`, `FR-OPS-014`) and the GRD-035…038 M&V
  and forecast rules. 66 FRs were added in all (§5 counts them); the complete list of new IDs is in
  `06-reviews/resolution/A1-product-brief.md`.
- **NFRs:** `NFR-207` scoped to production with a node test target (ARC-036); `NFR-206` keeps 1-s control-room channels
  under shedding (ARC-062, R48); `NFR-217/218/219/221` state their verification method; new `NFR-233…236`; a Build column.
- **Regulatory facts aligned with `06-reviews/05-claims-verification.md`:** ADER pilot caps and the 90% per-QSE share
  (`FR-PLAN-007`, claim 15); ECRS 1 h and Non-Spin 4 h → 2 h at NPRR1309 (`FR-CTR-006`, claim 6); one load zone/LSE/DSP
  per ALR and LSE acknowledgment for NCLR (`FR-CTR-019`, claim 4); PURA §35.153 terms (`FR-CTR-022`, claim 10); SB 231
  and the reach of §39.918 (`FR-CTR-024`, claim 5); NCLR awards, baseline and disqualification (`FR-CTR-026`, claim 3);
  proxy offers (`FR-ARB-013`, claim 2); the EEA charging rule as operator policy (`FR-SAFE-027`, claim 7); the 4-s UDSP and
  the configurable base ramp (`FR-DISP-025`, claim 14); DNP3 stack TLS (`FR-SCAD-003`, claim 12).

**Revision note (v3.2):** consistency pass against `00-decision-register.md` (the register wins on conflict;
IDs kept stable — this pass only appends new FRs at the end of their area and edits existing rows in place).
(1) **R3/R4 — unified kill-switch and command-safety tiers.** `FR-SAFE-006/007/008/009/020` rewritten to the
register's single two-tier table (Tier 1 = explicit confirmation; Tier 2 = a second, distinct approver) with
concrete thresholds and the 30 s/60 s/120 s bank/zone/fleet ramp-down; release is now explicitly Tier 2 at
every scope, with a staged ramp-up. New `FR-SAFE-022` (guardian/critical-alarm exception: one confirmation,
second approver co-signs within 15 min), `FR-SAFE-023` (pre-agreed in-limit utility SCADA controls execute
without confirmation; out-of-limit ones are rejected, not queued), `FR-SAFE-024` (an authorized utility's
stop/block command always executes). (2) **R9 — retention split.** `NFR-226` now distinguishes this node's
retention (7 days raw; 1-minute M&V, commands, audit/trace and settlement for the node's operational life)
from the production target (raw tiered to object storage for ≥ 13 months; 7 years for billing/settlement/
audit on write-once storage, pending Q5). (3) **R10 — dispatch-profile governance.** `FR-SVC-004/005` now
require a replay/simulation gate against the real ERCOT year before activation, signed and effective-dated
versions, and Tier 2 approval for a priority/limit change; new `FR-SVC-015` pins a running event to the
profile version it started with. (4) **R11 — reviewer numbers are configurable design targets.** New
`FR-SVC-014` makes every reviewer-proposed performance number a per-profile configurable field, not a
hard-coded constant. (5) **`FR-SAFE-005` rewritten** per the coordinator's explicit correction: anomalous
requests are flagged and handled within safety limits, never refused because of doubts about a service's
business value. (6) **Resolved open questions** (register Q1, Q16, Q17, Q19) are folded into `FR-SAFE-006..009`
(Q1: invoker ≠ approver, always), `FR-PRIV-004..007` (Q16: fulfilled via Base's existing support channel,
calling this system's fulfilment API), `FR-AI-013` (Q17: decline personal-data-dependent queries on this node;
route to a local model only in a production deployment), and `FR-SIM-016`/epics (Q19: three simulated
`MOBILE_TEEEF` units) — see §6 for what remains genuinely open.

Builds on [`00-brief.md`](../00-brief.md), [`01-vision-scope-personas.md`](01-vision-scope-personas.md) and
[`00-decision-register.md`](../00-decision-register.md), which is the single source of truth where any
document disagrees with it. Serves **Completeness** and **Technical depth** primarily; tags below note other
criteria served.

## 1. Conventions
`FR-<AREA>-NNN` IDs; sources `user` / `reviewer proposal — unverified` / `regulation` / `derived`; every acceptance
criterion measurable; no FR conditions a dispatch on a business case's proven value. **Per decision register R11**,
every reviewer-proposed number is a *design target*, expressed as a configurable field of the relevant dispatch profile
(`FR-SVC-001/014`) — the FR rows below state the reviewer's figure as the shipped default, not an immovable constant.
Normative values are cited as "register V-nn" and never restated with a different number.

**Priority and build tag (register R21).** The Priority column reads "MoSCoW · build tag". MoSCoW is the requirement's
importance to the product; the build tag is when it is built — sequencing only, never a scope cut (D0a):
- `MVP-J` — Line A, the judged core: walking skeleton (one hub → one invoice line by day 5, all nine customer types
  dispatchable through profiles on a minimal stack) → real data → `agent-sim` → gateway and twin → profiles and contracts
  → privacy baseline → fleet allocator and execution shards → guardian and Safe-Stop Authority → trace → M&V and
  settlement → `grid-sim` → DNP3 over TLS → console v1 → scenario runner. A register correction to one of these components
  (R16–R20, R25–R29) is built with the component.
- `MVP-B` — Line B: planner, Insights, value of orchestration, performance evidence, OpenADR VEN, AI explanations,
  `SHADOW` mode and the demo profile.
- `R2` — everything else, built after the judged demo with its design unchanged.
Where a requirement is delivered in two steps, the tag names the first and a note names the rest, e.g.
"Must · MVP-J (R2: signing)". The tag of every story is in `03-epics-and-user-stories.md`; the judged gate is G3-J
(`05-testing/01-test-strategy.md`).

**Numbering ranges across documents.** This document's `FR-<AREA>-NNN` IDs (e.g., `FR-ING-001…016`) are the
product-level requirement set referenced by `03-epics-and-user-stories.md` and the traceability matrix. Where
an architecture document decomposes an area into a more granular engineering-level set, it uses a distinct
numeric range in the same area code to avoid collision — e.g., `02-architecture/04-external-data-integration.md`
§15.2 numbers its detailed ingestion requirements `FR-ING-101…173` and maps each back to one of the sixteen
`FR-ING-001…016` requirements below. The two ranges are deliberately non-overlapping; this document's IDs
remain the ones every other product document cites.

## 2. Area index

| Area | Owning service(s) | Scope |
|---|---|---|
| `ING` | `market-data` | External data ingestion |
| `DEV` | `device-gateway` | Hub auth, telemetry, command/ack |
| `TWIN` | `fleet-state` | Digital twin, topology, house events |
| `FCST` | `forecaster` | Forecasts with uncertainty |
| `PLAN` | `planner` | Day-ahead/intraday MILP |
| `DISP` | `dispatcher` (fleet allocator and execution shards, R30) | Real-time control loop, ADER net-load regulation, `SHADOW` mode |
| `ARB` | `dispatcher` (arbitration) | Priority/commitment/profitability resolution; ERCOT-visible capability before the fact |
| `SVC` | `contracts`/`dispatcher` (profile catalogue) | Generic, config-driven service-type dispatch profiles and their variants |
| `CTR` | `contracts` (`contracts-rt`, R43) | Customer/contract/obligation/event model, all 9 types, territory roles |
| `MV` | `contracts` (`contracts-batch`, R43) | Meter reconciliation, capture ratio |
| `BILL` | `contracts` (`contracts-batch`) | Settlement/invoice records |
| `TRACE` | `dispatcher`/`contracts`/`guardian` | Tamper-evident decision audit |
| `INT` | `integrations` | OpenADR, simulated ERCOT QSE (ISO instructions, COP), webhooks |
| `SCAD` | `scada-gateway` | DNP3/60870-5-104/ICCP/2030.5/OPC UA, interlocks, commissioning, secure comms |
| `SAFE` | `guardian`, `safe-stop` (R16) | Command validation, stops at 3 scopes with the independent Safe-Stop Authority, command safety, emergency posture |
| `SEC` | all | Authn/z, roles, audit |
| `OPS` | platform-wide | Alerts, runbooks, flags, HA |
| `UI` | `console`, `api` | Console functions incl. new-role views |
| `SIM` | `agent-sim`, `grid-sim` | Mock hubs/counterparties, fault injection |
| `AI` | `ai-agent` | Advisor/explainer/copilot; non-personal data only |
| `PRIV` | `contracts`/platform-wide | Privacy of personal data (D5) |
| `RPT` | `contracts`, `fleet-state` | Insights and reporting |

## 3. Functional requirements

### 3.1 `ING` — External data ingestion

*Cross-reference: `02-architecture/04-external-data-integration.md` §15.2 decomposes this area into
`FR-ING-101…173` (protocol-level detail, retry/backoff parameters, per-source schemas) and maps each back to
one row below. Treat the two ID ranges as complementary, not duplicative.*

| ID | Statement | Rationale | Acceptance criterion | Priority | Source |
|---|---|---|---|---|---|
| FR-ING-001 | Poll ERCOT real-time settlement point prices for every load zone the fleet model uses — the competitive-area partition that carries the ERCOT lanes (e.g., `LZ_HOUSTON`, `LZ_NORTH`) and the NOIE partitions (`LZ_CPS`, `LZ_AEN`) (register R27a); the zone list is configuration; each configured zone records whose settlement its price drives (the ADER's LSE and QSE per territory, R27); retain a reference trading-hub price (e.g., `HB_NORTH`) for display/comparison only | The fleet settles at its load zone, not a trading hub; in a NOIE territory ERCOT value flows through the NOIE's contract with Base, so a load-zone price must never be booked as Base's own revenue there (GRD-016, JDG-017) | New load-zone price reaches consumers ≤ 60 s after ERCOT posts it, ≥ 99% of polls/24 h, for every configured zone; every price consumer reads the zone's settlement role; the reference hub price is labelled "reference only" wherever shown and never feeds a settlement/M&V/revenue calculation | Must · MVP-J | derived |
| FR-ING-002 | Poll ERCOT zone load with a 2-day lookback window | Substation-load proxy | Latest row returned regardless of day boundary, ≥ 99% | Must · MVP-J | derived |
| FR-ING-003 | Poll ERCOT actual wind generation (actual rows only) | `PIPELINE_AC` current proxy | Forecast rows never mistaken for actuals | Must · R2 | derived |
| FR-ING-004 | Authenticate (OAuth ROPC); refresh token pre-expiry | Avoid auth failures | Zero token-expiry failures, 24 h soak | Must · MVP-J | derived |
| FR-ING-005 | Enforce ≤ 30 req/min fleet-wide rate limit | Compliance | Load test confirms ≤ 30 req/min | Must · MVP-J | derived |
| FR-ING-006 | On API error, log `comm_fail`, fall back to last-good value | Fault tolerance | Chaos test: tick completes, no crash | Must · MVP-J | derived |
| FR-ING-007 | Flag reading quality `good`/`stale`/`out_of_range`/`missing` | Downstream confidence | All 4 outcomes tested; consumers use the flag | Must · MVP-J | derived |
| FR-ING-008 | Ingest EIA data on its cadence | Sizing context | Latest dataset reflected within one cycle | Should · R2 | derived |
| FR-ING-009 | Ingest geospatial reference data | Topology/corridor lookups | Known co-located pair returned correctly | Should · R2 | derived |
| FR-ING-010 | Ingest solar reference data | Realistic sim defaults | Defaults derived from dataset | Should · MVP-J | derived |
| FR-ING-011 | Pluggable NWS adapter: forecasts, and watches and warnings for the service areas | Forecasting input; forecast risk for reserve pre-positioning (R19) | Same interface as ERCOT/EIA clients; a watch/warning fixture reaches `planner` and the console within one poll cycle | Must · MVP-J | user |
| FR-ING-012 | Cache last-good value with "as of" timestamp | Graceful degradation | Query during outage returns cached + age | Must · MVP-J | derived |
| FR-ING-013 | Publish signals idempotently, at-least-once | No double count | Replayed duplicate is a no-op | Must · MVP-J | user |
| FR-ING-014 | Design-only PJM adapter interface | Future-proofing | Type-checks with no PJM connection | Could · R2 | user |
| FR-ING-015 | Alert on prolonged stale/unavailable signal | Operability | Alert within one poll cycle | Must · MVP-J | derived |
| FR-ING-016 | Version/quarantine on schema change | Robustness | Injected change is quarantined | Should · R2 | derived |
| FR-ING-017 | Ingest ERCOT operating notices — operating condition notices, advisories, watches and the current EEA level — with their effective times, and publish them to `planner`, `guardian` and the console | Pre-positioning on forecast risk and the EEA posture need the grid's declared state (R19; GRD-004) | A notice fixture of each kind reaches the three consumers within one poll cycle with its effective time; the EEA level is also accepted from the (simulated) QSE interface (`FR-SIM-019`) | Must · MVP-B | regulation |
| FR-ING-018 | Record every external payload with its as-of time and source, and replay a recorded day through the same ingestion path; the console labels live and replayed data distinctly | Real data must stay visible and reproducible even when ERCOT's API is unavailable; the demo's fallback replays a real day (JDG-016) | A recorded real ERCOT day replays through the live code path with identical downstream values; every price on screen shows its product, as-of time and "live" or "replayed" | Must · MVP-J | derived |

### 3.2 `DEV` — Device management & telemetry

| ID | Statement | Rationale | Acceptance criterion | Priority | Source |
|---|---|---|---|---|---|
| FR-DEV-001 | MQTT 5 over mTLS; reject invalid/revoked certs | Zero-trust | 100% rejection in test | Must · MVP-J | user |
| FR-DEV-002 | Enroll before act (cert + `fleet-state` registration) | No unregistered action | Unenrolled device cannot act | Must · MVP-J | derived |
| FR-DEV-003 | Telemetry at the cadence of register V-32: 10 s per hub, 2 s during events and for every member of an on-line ADER | Scale targets; ADER net-load tracking (R17) | Cadence within ± one tick in both modes; a hub joining an on-line ADER switches to 2 s within one cycle | Must · MVP-J | user |
| FR-DEV-004 | Validate/quarantine malformed telemetry | Robustness | Malformed fixture quarantined, connection stays up | Must · MVP-J | derived |
| FR-DEV-005 | Never mark a command executed without confirming telemetry | Brief §1.2 | Unconfirmed shown as unconfirmed | Must · MVP-J | user |
| FR-DEV-006 | Sign outbound commands; reject unsigned | Integrity | Unsigned fixture rejected | Must · MVP-J | user |
| FR-DEV-007 | Detect unresponsive hub after missed intervals | Brief §9 | Marked unavailable within one cycle | Must · MVP-J | user |
| FR-DEV-008 | Detect "not communicating" distinctly from unresponsive | Fault triage | Session-only drop classified correctly | Must · MVP-J | user |
| FR-DEV-009 | Detect fault code; halt commands until cleared | Brief §9 | No new setpoint while faulted | Must · MVP-J | user |
| FR-DEV-010 | Detect sustained under-delivery; flag for substitution | Brief §9 | Flagged within N+1 ticks | Must · MVP-J | user |
| FR-DEV-011 | Backoff+jitter retry, bounded | Brief §9 | Matches policy in scripted test | Must · MVP-J | user |
| FR-DEV-012 | Rate-limit/backpressure; queue on reconnect burst | Overload prevention | 10,000-hub burst: zero dropped | Must · MVP-B (the admission limits of V-21 are configured from MVP-J; the 10,000-hub burst is measured in the MVP-B performance runs) | derived |
| FR-DEV-013 | Per-hub firmware/protocol capability negotiation (protection and grid-support settings are verified separately, `FR-DEV-019`) | Brief §9 | Constrained hub never exceeds its limit | Should · R2 | user |
| FR-DEV-014 | `agent-sim` hubs use the identical real-device path, speak only the published device contract (`FR-DEV-018`) and receive faults only through the simulator's own fault API | Brief §1.2; the system must not test itself (JDG-028) | No simulator-only branch; the conformance suite of `FR-DEV-018` passes against `agent-sim` | Must · MVP-J | user |
| FR-DEV-015 | Quarantine a hub (register C-14 — the kill switch has no hub scope, D2): exclude it from every pool and stop commanding it within one cycle; revoke its certificate and session so it cannot reconnect until re-enrolled | Safety | A quarantined hub receives no command after one cycle; a revoked hub cannot reconnect | Must · MVP-J (R2: certificate and session revocation) | user |
| FR-DEV-016 | Expose per-hub/site/bank health counts at cadence | Observability | Console matches DB count | Must · MVP-J | derived |
| FR-DEV-017 | Put every fleet behind a `DeviceAdapter` interface — telemetry in, commands out, acknowledgement semantics — with the MQTT agent contract as one implementation; a second adapter can be added without changing any other service | A path onto Base's real fleet without re-architecture (R23; JDG-009) | A stub second adapter passes the adapter contract test with no change outside `device-gateway`; the MQTT implementation passes the same test | Must · MVP-B | derived |
| FR-DEV-018 | Publish the device contract (owned by `02-domain-model-and-interfaces.md`, R33) as a separate package: JSON Schema with a version field for every message, an AsyncAPI document and a conformance suite; support N and N-1 versions per firmware cohort | Firmware-facing interfaces are the costliest to change; conformance must be testable by anyone (JDG-028, R33) | The package builds on its own; the conformance suite passes against `agent-sim` and fails on a fixture that violates the schema | Must · MVP-J | derived |
| FR-DEV-019 | Read back each hub's IEEE 1547 settings (ride-through category, trip thresholds and times, enter-service, droop, volt-var and volt-watt curves) at enrolment, at every boot and after every rollout ring; compare them with a signed, accepted settings profile; quarantine a drifting hub from ADER and firm pools; report the fleet MW exposed to a common-mode trip | A firmware defect that narrows ride-through across the fleet is a common-mode trip risk (R26; GRD-020) | A drift fixture quarantines the hub from ADER and firm pools within one cycle of the read-back; the exposed-MW figure updates | Should · R2 | regulation |
| FR-DEV-020 | Carry the hub's autonomous-response reason codes (frequency-watt, volt-watt, volt-var priority) and the autonomous change in active power in telemetry | Autonomous grid support must be told apart from under-delivery (R26; GRD-009, GRD-031) | A frequency-event fixture yields reason-coded telemetry with the autonomous ΔP on every responding hub | Must · MVP-J | regulation |

### 3.3 `TWIN` — Fleet digital twin & state estimation

| ID | Statement | Rationale | Acceptance criterion | Priority | Source |
|---|---|---|---|---|---|
| FR-TWIN-001 | Maintain SOC/available kW/health per hub | Core twin | Age ≤ one interval + latency | Must · MVP-J | user |
| FR-TWIN-002 | Compute a per-hub trust score | Brief §5 | Faulted hub scores lower | Must · MVP-J | user |
| FR-TWIN-003 | Maintain full topology; "behind asset X" queries | Control scoping | Test bank returns exactly its homes | Must · MVP-J | derived |
| FR-TWIN-004 | Reduce available kW for competing local load (EV) | Brief §9 | Drops within one tick of session start | Must · MVP-J | user |
| FR-TWIN-005 | Exclude an islanded hub from all obligations | Brief §9 | Excluded within one detection cycle | Must · MVP-J | user |
| FR-TWIN-006 | Apply opt-out/reserve-change within a bounded latency | Brief §9 | Latency under documented bound | Must · MVP-J | user |
| FR-TWIN-007 | Never report available kW above SOC minus reserve/reservations | Principle 4 | Property test: 100% compliance | Must · MVP-J | derived |
| FR-TWIN-008 | Roll up state to every hierarchy level | Console/planner need | Aggregate reconciles with sum | Must · MVP-J | derived |
| FR-TWIN-009 | Track each hub's connectivity (ONLINE, SILENT, OFFLINE, LOST) separately from its eligibility (eligible, probation, excluded) with the thresholds of register V-29; exclude from totals and allocation from SILENT onward, keep visible | Brief §9; one hub-state table for every document (R40) | A hub silent for more than 3 reports is excluded (6 s at 2-s cadence, 30 s at 10-s) and shown with a badge; it returns through probation (3 fresh reports and one verified command) | Must · MVP-J | derived |
| FR-TWIN-010 | Recompute feasibility on topology change | Correctness | Recheck within one planning cycle | Should · R2 | derived |
| FR-TWIN-011 | Retain historical per-hub state timeline | M&V/backtest | Any past query returns recorded state | Must · MVP-J | derived |
| FR-TWIN-012 | Label a hub `degraded` from delivered-vs-commanded history | Real, not nameplate, capacity | Manufactured fade history labels the hub | Should · R2 | reviewer proposal — unverified |
| FR-TWIN-013 | Carry `phase` on every service transformer and service point, and unit-typed ratings and limits (kVA, kW, A) on every bank and feeder; reject a mixed-unit comparison at map validation; a hub with unknown phase counts toward three-phase totals only, never toward a phase-limited need | Transformer limits are thermal (current, apparent power) and residential hubs sit on single-phase laterals (R18; GRD-003, GRD-006) | A mixed-unit point map is rejected at validation; a per-phase need is served only by hubs on that phase | Must · MVP-J | derived |
| FR-TWIN-014 | Define topology freshness as the GIS version plus every switching order applied since, from the utility's OMS/ADMS feed; while a switching order is open or step-response inference disagrees, apply the conservative mode for the affected bank | GIS extracts arrive weekly or monthly and many switches have no SCADA status (R28; GRD-027) | A switching-order fixture updates the as-operated topology before the next planning cycle; an open order puts only its bank in conservative mode | Should · R2 | derived |
| FR-TWIN-015 | Compute each hub's available active power from its apparent-power rating, the reactive power its volt-var curve demands at the measured voltage and its P-or-Q priority setting | Volt-var with reactive priority lowers available P at full export (R26; GRD-031) | At a high-voltage fixture a reactive-priority hub reports reduced available kW that matches the curve | Must · MVP-J | derived |

### 3.4 `FCST` — Forecasting

| ID | Statement | Rationale | Acceptance criterion | Priority | Source |
|---|---|---|---|---|---|
| FR-FCST-001 | Day-ahead hourly ERCOT price forecast with uncertainty | Feeds `PLAN` | Published before 10:00, non-zero band | Must · MVP-B | derived |
| FR-FCST-002 | Day-ahead hourly load/solar forecast with uncertainty | Brief §5 | Published daily; MAPE recorded | Must · MVP-B | user |
| FR-FCST-003 | Day-ahead per-bank overload forecast, labelled a proxy while it rests on a `SYNTHETIC` zone-shaped bank proxy; no firm declaration or firm sizing uses a `SYNTHETIC` forecast without a documented conservative multiplier the utility accepted (`FR-FCST-011`) | Model-fidelity caveat; a label alone does not stop the planner committing on a biased input (GRD-038) | Proxy label visible until real data replaces it; a firm declaration fixture on a `SYNTHETIC` forecast without an accepted multiplier is refused with the reason | Must · MVP-B | reviewer proposal — unverified |
| FR-FCST-004 | Availability forecast from schedule/dropout history | Brief §5 | Changes with a scheduled event | Should · MVP-B | user |
| FR-FCST-005 | Attach uncertainty; `planner` sizes reserve on it | Correctness | Reserve size responds to band width | Must · MVP-B | user |
| FR-FCST-006 | Re-run intraday forecast ≥ every 15 min | Brief §4 | Timestamp updates ≥ every 15 min | Must · MVP-B | user |
| FR-FCST-007 | Backtest each forecast type; publish error | Insight | Error metric updated ≥ daily | Should · R2 | derived |
| FR-FCST-008 | Widen band on degraded input quality | Consistency | Band widens on a `stale` input | Must · MVP-B | derived |
| FR-FCST-009 | Report day-ahead overload prediction accuracy | Proposed ≥ 90% bar | Report generated daily | Should · R2 | reviewer proposal — unverified |
| FR-FCST-010 | Pluggable forecast models per signal | Extensibility | Second model passes the same contract test | Could · R2 | derived |
| FR-FCST-011 | Base firm declarations and firm sizing on inputs fit for them: home-load P10 for 16:00–21:00 from ≥ 28 days of site telemetry or a peak-shaped residential prior; a `SYNTHETIC` bank forecast only with a documented conservative multiplier the utility accepted; publish declared vs measured P10 per program monthly | A zone-shaped bank proxy and a home-load model that understates evening load overstate evening export P10 (GRD-038) | A declaration fixture records which inputs it used; the monthly declared-vs-measured P10 report exists per program | Must · MVP-B | reviewer proposal — unverified |

### 3.5 `PLAN` — Planning & optimization

| ID | Statement | Rationale | Acceptance criterion | Priority | Source |
|---|---|---|---|---|---|
| FR-PLAN-001 | Jointly solve day-ahead commitments/bids/reserves within real derated capacity | Brief §5 | Never exceeds derated totals | Must · MVP-B | user |
| FR-PLAN-002 | Size `DIST_DEFERRAL` on end-of-term usable capacity | Proposed sizing correction | Uses term-end fade factor | Must · MVP-B | reviewer proposal — unverified |
| FR-PLAN-003 | One shared SOC floor per hub/group | Proposed double-count correction | No solution over-reserves | Must · MVP-B | reviewer proposal — unverified |
| FR-PLAN-004 | Restrict firm allocation to homes behind the asset | Proposed scoping correction | Uses only "behind" homes | Must · MVP-B | reviewer proposal — unverified |
| FR-PLAN-005 | No charging behind a constrained bank in its need window | Proposed correction | Zero charging kW in-window | Must · MVP-B | reviewer proposal — unverified |
| FR-PLAN-006 | Worst-day stress-test mode | Stress-test firm commitments | Differs from expected-value plan | Should · R2 | reviewer proposal — unverified |
| FR-PLAN-007 | Respect the ADER pilot caps of the governing document — 500 MW registered, 100 MW Non-Spin and 100 MW ECRS system-wide, no QSE above 90% of each (as configuration; `06-reviews/05` claim 15) — and each ADER's qualified-MW cap per product; never exceed the fleet's allotment; plan with no ECRS headroom while ECRS is at its system cap | Brief §3.1; per-ADER caps (R17; GRD-057) | No offer exceeds the system-wide allotment, the 90% per-QSE share or the ADER's qualified MW for the product | Must · MVP-J | regulation |
| FR-PLAN-008 | Ring-fence an awarded AS hold inside its awarded interval — it is never diverted to a firm event; model RTC+B buyback cost only for a forward release of AS capacity (`03-decision-engine.md` §7.4, default off) or a capability loss (utility block, safe stop, hub loss), linked to the causing trace | A diversion inside the awarded interval is forbidden (brief §3.1); buyback is the price of a release or a loss, never of a normal allocation (JDG-010) | A firm event competing with an awarded hold leaves the hold intact and is served by substitution or reported `AT_RISK`, with no buyback line; a capability-loss fixture records buyback exposure linked to its causing trace | Should · MVP-J (R2: forward-release decision) | regulation / reviewer proposal — unverified |
| FR-PLAN-009 | Recompute plan ≥ every 15 min; preserve firm declarations | Brief §4 | Preserved across ≥ 95% of cycles | Must · MVP-B | user |
| FR-PLAN-010 | Two day-ahead deadlines, both America/Chicago: ERCOT day-ahead market offers before the 10:00 close, and firm declarations to utilities and partners by 14:00 | Brief §4; the two deadlines are different and must never be conflated (JDG-011) | Offers are timestamped before 10:00 CT and declarations before 14:00 CT on every day of a 30-day soak, including a DST change | Must · MVP-B | user |
| FR-PLAN-011 | Joint value from solver's own feasibility, never summed maxima | Proposed correction | Joint ≤ sum; matches solver objective | Must · MVP-B | reviewer proposal — unverified |
| FR-PLAN-012 | Pre-emptive breach-risk flag (`AT_RISK`) with probability, expected shortfall and first-breach time, before a firm window opens | Vision KPI-13 | Replays meet the lead-time target of register V-41 (median ≥ 60 min, P10 ≥ 15 min) with a calibration plot (`FR-RPT-012`) | Must · MVP-B | derived |
| FR-PLAN-013 | Operator review/approve/reject before commitment | Vision persona 6.1 | Un-approved plan not committed | Must · MVP-B | derived |
| FR-PLAN-014 | Auto-commit fallback plan if not approved by cutover | Avoid empty plan | Fallback committed, never empty | Must · MVP-J | derived |
| FR-PLAN-015 | Include every customer type's candidates in one optimization, none pre-excluded | Service-agnostic dispatch | All nine types considered when active | Must · MVP-B | user |
| FR-PLAN-016 | Pre-position homeowner reserves on forecast risk (NWS watches and warnings; ERCOT operating notices, advisories and watches) in low net-load hours, updating ADER telemetry and the COP first; never plan a reserve raise by grid charging during an EEA | The fleet must not become a charger when the grid is short (R19; GRD-004) | A watch fixture 24 h ahead raises reserves before the risk window; an EEA fixture produces no planned grid charging beyond `FR-SAFE-027`'s exceptions | Must · MVP-B | regulation |
| FR-PLAN-017 | Produce a Current Operating Plan per ADER and hour for the next 168 h — status (ONL/OUTL), MPC, LPC and AS capability per product — from ledger-free, guardian-permitted capacity (every non-ERCOT reservation excluded); resubmit it on a change ≥ 1 MW or ≥ 10% and always within 60 min (register R17) | ERCOT must see only capacity no other buyer holds, and a QSE keeps a COP for every resource (GRD-023) | A storm-hold fixture and a zone-stop fixture each trigger a COP resubmission within 60 min; no COP hour shows capacity that the ledger reserves for another buyer | Must · MVP-J (MVP-B: produced by the MILP planner) | regulation |
| FR-PLAN-018 | Enforce a per-hub and per-cohort cycle budget (cycles per year, calendar-aware) as a planner constraint with a shadow price; report cycles per service per month | Tolling partners cycling daily, arbitrage and AS deployments stack cycles beyond warranty throughput (R27; GRD-043) | No plan exceeds a hub's remaining budget; the monthly report attributes cycles to services | Must · MVP-B | derived |

### 3.6 `DISP` — Real-time dispatch & control

| ID | Statement | Rationale | Acceptance criterion | Priority | Source |
|---|---|---|---|---|---|
| FR-DISP-001 | Allocate each tick by priority order (configurable per contract) from one shared pool | Brief §3.1 | Lower priority never starves a higher one that could use the kW | Must · MVP-J | user |
| FR-DISP-002 | Run the control cycle of register V-03 — 2 s during an active event and for members of an on-line ADER, 10 s otherwise — and re-evaluate on every SCED interval (≥ every 5 min) | Brief §4; ADER members track ERCOT's set point continuously (R17; GRD-022) | Matches V-03 ± one tick in both modes; SCED re-evaluation ≥ every 5 min | Must · MVP-J | user |
| FR-DISP-003 | Need = measured bank loading (net of fleet) + the fleet's own output behind the bank at the SCADA sample's source time (alignment ≤ 1 s) − (rating − margin), computed on the quantity the rating protects — apparent power (kVA, with the fleet's own reactive power added back like its active power) or the maximum per-phase current (`FR-DISP-028`) | Proposed control-law correction; a "prior" fleet output reintroduces the time misalignment the decision engine removes (GRD-060, R18) | Unit test matches the corrected formula with the fleet output time-aligned to the sample; a PF 0.9 fixture with fleet volt-var absorption holds the kVA limit | Must · MVP-J | reviewer proposal — unverified |
| FR-DISP-004 | Deadband + ramp limit on firm control laws | Proposed anti-hunting | Change/tick ≤ ramp limit | Must · MVP-J | reviewer proposal — unverified |
| FR-DISP-005 | Hold-then-schedule on bad signal; never drop to 0 kW first; inside a need window HOLD = max(held setpoint, scheduled setpoint); return from HOLD per register V-38 | Proposed fail-safe correction; more relief is the safe error inside a need window (R28) | No first-response drop to 0 kW; the held setpoint never falls below the schedule inside a need window; return only after V-38's criterion | Must · MVP-J | reviewer proposal — unverified |
| FR-DISP-006 | Reserve firm energy ahead; block lower-priority draws | Proposed reservation correction | Reserved kWh never drained early | Must · MVP-J | reviewer proposal — unverified |
| FR-DISP-007 | Block charging in a constrained bank's need window, at dispatch time; the one exception is homeowner-reserve recovery, allowed only within bank headroom (never above 95% of the rating), at a capped per-hub rate, lowest SOC first, and counted as excused per contract | Proposed correction; the order between a homeowner's reserve (L1) and the bank limit must be explicit (R28; GRD-047) | A charge command is rejected even if planning allowed it; after an outage inside a need window, reserve recovery stays within headroom and the bank never exceeds 95% | Must · MVP-J | reviewer proposal — unverified |
| FR-DISP-008 | Cap recharge ramp below 95% of the bank rating, in the rating's own unit, with the headroom adding back the fleet's own charging (`FR-DISP-030`) | Proposed rebound bar | Never exceeds 95% | Must · MVP-J | reviewer proposal — unverified |
| FR-DISP-009 | Substitute an under-delivering hub within KPI-12 bound | Vision KPI-12 | ≤ 3 ticks | Must · MVP-J | user |
| FR-DISP-010 | Report true shortfall when no substitute exists | Principle | Delivered ≠ commanded when short | Must · MVP-J | user |
| FR-DISP-011 | Damp oscillation; alert | Brief §9 | Damped mode within 3 ticks | Must · MVP-J (R2: frequency-domain oscillation detector; MVP-J uses a direction-reversal counter) | user |
| FR-DISP-012 | React to a price spike within one tick — price-responsive dispatch applies only to premises whose ADER is off line (OUTL) or not registered, settled at retail; members of an on-line ADER follow ERCOT's set point (`FR-DISP-024/025`) and never react to price on their own | Brief §9; for an on-line ALR, ERCOT's set point already prices the spike, and a self-directed response is a failure to follow dispatch (R17; GRD-001) | Decision changes within one tick for off-line or unregistered premises; an on-line ADER fixture shows no price-driven deviation from the set point | Must · MVP-J | user |
| FR-DISP-013 | Re-route on topology path change | Brief §9 | Updates within one planning cycle | Should · R2 | user |
| FR-DISP-014 | Switch battery source when preferred one fails, logging why | Brief §9 | Substitution logged | Must · MVP-J | user |
| FR-DISP-015 | Net available kW against competing local load | Brief §9 | Drops during EV-peak window | Must · MVP-J | user |
| FR-DISP-016 | Cap at a saturated export limit; tag shortfall structural | Brief §9 | Structural tag applied | Must · MVP-J | user |
| FR-DISP-017 | Clean stop on interruption/override; return capacity | Brief §9 | Stop + capacity reappear within one tick | Must · MVP-J | user |
| FR-DISP-018 | Log every tick's requested/granted/priority/rule per obligation | Principle 5/`TRACE` | Every obligation logged every tick | Must · MVP-J | derived |
| FR-DISP-019 | Per-hub grants never exceed available energy above floor | Principle 4 | 100% compliance, property test | Must · MVP-J | derived |
| FR-DISP-020 | Operator/guardian-invoked safe degraded mode, scoped | Brief §1.5 | Suspends targeted obligations within one tick | Must · MVP-J | user |
| FR-DISP-021 | Dispatch a `PIPELINE_AC` H1/H2 smoothing call within its contracted band whenever it arrives, exactly like any other obligation type | Service-agnostic dispatch | Executes within the contracted band on every call; never suppressed, delayed or down-ranked pending an impact judgment | Must · MVP-J | user |
| FR-DISP-022 | Dispatch a `LARGE_LOAD` event exactly as requested, arbitrated like any call | Service-agnostic dispatch | Full dispatch subject only to safety/priority/capacity | Must · MVP-J | user |
| FR-DISP-023 | Attach a monotonic sequence number and expected prior state to every internally generated command; reject execution if either check fails downstream | D4a | Out-of-sequence/stale internal command rejected in a test | Must · MVP-J | user |
| FR-DISP-024 | Treat every ERCOT instruction for an on-line ADER (`IsoInstruction`: set point, manual deployment or recall, status change, emergency action, verbal dispatch instruction) as a hard constraint at L2 precedence, never a squeezable call; never meet a firm commitment by deviating from it; resolve a residual conflict by substitution from non-ADER hubs, then `AT_RISK` with notice to the counterparty, then a QSE status or telemetry change going forward | ERCOT dispatch instructions are binding on the resource; customers are arbitrated before the fact, not by deviation (R17; GRD-001, GRD-021) | A firm call that wants the same hubs as an instructed ADER is served by substitution or flagged `AT_RISK`; the ADER's net load stays on the instruction in 100% of fixture cycles | Must · MVP-J | regulation |
| FR-DISP-025 | For an ALR-type ADER on line, regulate the aggregate net load of all member premises (home load − PV − hub output, plus the offset) on ERCOT's updated set-point trajectory (UDSP, received every 4 s; the base-ramp shape toward a new base point is configurable, default a 4-min linear ramp — an ERCOT training value, not a protocol value) with a cycle ≤ 4 s and member telemetry every 2 s, absorbing home-load noise and every other service's action on member hubs; no step of the ADER's net load without an ERCOT instruction | For an ALR, ERCOT's set point binds the whole resource's net power consumption (R17; GRD-001, GRD-022) | A 30-day replay with house-load noise, EV starts, partner and deferral actions on member hubs and a frequency event keeps the set-point deviation within the profile's tolerance; no net-load step occurs without an instruction | Must · MVP-J | regulation |
| FR-DISP-026 | On loss of ICCP or of the QSE link, hold the ADER's last set point flat — never step to zero — until the QSE desk has agreed status and substitute telemetry with ERCOT and ERCOT's instruction arrives | ERCOT sees neither a step nor its cause while the link is down (R25; GRD-014) | A link-loss fixture holds the net-load set point flat for its duration; the QSE-desk task appears at once; no net-load step without an instruction | Must · MVP-J | regulation |
| FR-DISP-027 | While the frequency error exceeds the hubs' droop deadband or hubs report autonomous-response reason codes (`FR-DEV-020`), freeze every integrator, substitution and trust penalty and hold setpoints; exclude the autonomous change from "not following" and, per contract, from M&V shortfall | Integral trims must not cancel the fleet's primary frequency response or mark healthy hubs untrustworthy (R26; GRD-009) | A 59.85 Hz, 60-s fixture: integrators and substitution freeze, no trust decay, no shortfall charged where the contract excuses it; normal control resumes after the event | Must · MVP-J | regulation |
| FR-DISP-028 | Regulate each deferral bank on the quantity its rating protects — apparent power (P_target = √(S_limit² − Q²), with the fleet's own reactive power added back) or the maximum per-phase current against the per-phase rating — and allocate relief per phase; per contract, deferral performance is outcome-based (bank loading ≤ limit in the need window) or share-based (the integrator corrects only the deferral's own delivered-vs-requested error) — never a PI that unwinds when another service relieves the bank | A kW law under-relieves a bank at low power factor, and a phase can overload while the three-phase total looks fine (R18; GRD-003, GRD-006, GRD-008) | PF 0.9 and phase-imbalance fixtures hold the kVA and per-phase limits; Example B re-run in closed loop keeps the deferral's own request when another service relieves the bank | Must · MVP-J | reviewer proposal — unverified |
| FR-DISP-029 | Under platform saturation, clip or defer calls with the shortfall reported — never reject a call for load reasons | Rejection is allowed only for authorization or contract validity (R48; D0b; ARC-061) | A saturation fixture yields clipped or deferred calls with reported shortfalls and zero load-based rejections | Must · MVP-J | derived |
| FR-DISP-030 | Compute recharge headroom as the recharge limit minus (measured load − the fleet's own charging behind the bank at the sample time) minus margin, with the same formula in the guardian; no discretionary fleet recharge while ERCOT net load is near its daily peak (HE20–21) or the real-time price exceeds the profile threshold (register V-30) | Measured load already contains the fleet's charging, so a law without the add-back chatters and recharges at half rate; fleet-wide recharge into ERCOT's net-load peak adds hundreds of MW when ERCOT is shortest (R18; GRD-007, GRD-045) | A closed-loop test with 2–10 s SCADA delay converges monotonically to the headroom; no discretionary recharge is scheduled in HE20–21 on a replayed peak day | Must · MVP-J | derived |
| FR-DISP-031 | Keep exactly one integrating loop per bank (every other loop, inside or outside the orchestrator, is feedforward-only or a fixed target); low-pass the feedforward (30–60 s) with a fast path for steps beyond 3σ; set per-bank deadbands from the measured σ of the bank signal | Two integrating loops on one quantity fight; raw 2-s feedforward churns setpoints across hundreds of hubs (R28; GRD-028, GRD-030) | A two-loop fixture (utility battery PI plus the fleet) has one integrator; command churn per bank is reported and falls with the filter on | Must · MVP-J | derived |
| FR-DISP-032 | Offer a `SHADOW` operating mode: plans, arbitration, decision traces and M&V are computed on real telemetry; commands are recorded and never sent; a shadow-vs-actual report compares them with what the fleet actually did; every screen shows the mode unmissably | A path onto Base's real fleet with no command sent until Base decides (R23; JDG-009); UI-GLB-03, UI-INS-08 | In `SHADOW`, zero commands leave `device-gateway` in a 24 h run while traces and M&V are produced; the report lists per-interval differences | Must · MVP-B | derived |

### 3.7 `ARB` — Call arbitration

| ID | Statement | Rationale | Acceptance criterion | Priority | Source |
|---|---|---|---|---|---|
| FR-ARB-001 | Validate every call against its contract before allocation | Brief §1 | Invalid-contract fixture rejected pre-allocation | Must · MVP-J | user |
| FR-ARB-002 | Resolve overlaps via priority + commitments + profitability | Brief §1 | Two-call fixture resolves reproducibly | Must · MVP-J | user |
| FR-ARB-003 | Compute profitability from value minus energy/degradation/delivery-charge/penalty/buyback/opportunity-cost | Brief §1 | Figure decomposes into named components | Must · MVP-J | user |
| FR-ARB-004 | Priority class dominates profitability unless a per-contract override is configured | Brief §3.1 | Higher priority wins absent override | Must · MVP-J | user |
| FR-ARB-005 | Deterministic tie-break rule | Avoid arbitrary outcomes | Same inputs → same winner | Must · MVP-J | derived |
| FR-ARB-006 | Record full option set, constraints, winner, every loser's regret | Brief §1 | Trace contains all contenders + regret | Must · MVP-J | user |
| FR-ARB-007 | Record a true shortfall against the specific call when none can be served | Principle | Correct attribution in a fixture | Must · MVP-J | derived |
| FR-ARB-008 | Per-contract configurable priority order, audited change | Brief §3.1 | Change isolated + audited | Should · MVP-J | user |
| FR-ARB-009 | `ai-agent` may propose for novel conflicts; deterministic + guardian must independently validate first; an approved proposal becomes a time-boxed, versioned constraint set (pins, priorities, holds) that arbitration consumes until it expires, and human confirmation is always required (R49) | Brief §1; a proposal applied as one decision would vanish on the next tick (ARC-049) | AI proposal never dispatches unchecked; an approved proposal holds for its time box and expires on schedule; no proposal applies without a recorded human confirmation | Must · R2 | user |
| FR-ARB-010 | Re-evaluate within one tick of a material input change | Correctness | New higher-priority call changes outcome within one tick | Must · MVP-J | derived |
| FR-ARB-011 | Chosen allocation always feasible against the shared ledger | Principle 4 | Never exceeds `FR-DISP-019` bound | Must · MVP-J | derived |
| FR-ARB-012 | Expose decision to console/`TRACE`/`BILL` within one tick | Operability | Reflected within one tick | Must · MVP-J | derived |
| FR-ARB-013 | Arbitrate ERCOT against other customers before the fact: compute every ERCOT-visible quantity — MPC, LPC, ramp rates (min(physical, guardian-permitted share), register V-30), AS capability per product, offers and the COP — from ledger-free, guardian-permitted capacity, and update it within 2 s of any reservation change | ERCOT dispatches and awards inside what it sees, and creates a proxy AS offer for any telemetered capability not covered by an offer; capacity sold to another buyer must never be visible to SCED (R17; GRD-002, GRD-013; `06-reviews/05` claims 2, 14) | A partner event in its window plus a proxy-offer fixture produces no SCED award on reserved kW; telemetered AS capability never exceeds what the ledger can hold for the product's duration (the boundary, `FR-SAFE-029`), and real-time AS offers and an energy bid cover the telemetered range; telemetry updates within 2 s of a reservation change; telemetered ramp × 5 min ≤ guardian-permitted change | Must · MVP-J | regulation |

### 3.8 `SVC` — Service-type dispatch profile catalogue

| ID | Statement | Rationale | Acceptance criterion | Priority | Source |
|---|---|---|---|---|---|
| FR-SVC-001 | Define a versioned profile schema: signal sources/protocols, request schema, admission rules, control mode, priority/arbitration inputs, completion rules, M&V/billing rules, failure behaviour | Brief §3.5 | Schema captures all eight elements; every reviewer-proposed performance number is expressed as a configurable field within the completion/performance or M&V/billing elements, not a hard-coded constant | Must · MVP-J | user |
| FR-SVC-002 | Provide a complete, validated profile for each of the nine in-scope customer types | Brief §3.1/§3.5 | All nine exist and pass validation | Must · MVP-J | user |
| FR-SVC-003 | Add a new service type by registering a new profile, zero application-code change | Brief §3.5 | New profile activates with no code deploy | Must · MVP-J | user |
| FR-SVC-004 | Validate a profile's completeness, referential integrity and guardian-limit compatibility, and gate its activation by risk: a tighten-only or safety change passes the golden-week replay plus a guardian-envelope check; a change that loosens priority or limits passes the replay of the real ERCOT year | Decision register R10, R47 — the full-year replay costs hours of CPU, so an emergency tightening must not wait for it (ARC-054) | Incomplete/incompatible profile rejected; a tighten-only fixture activates after the golden week and envelope check; a loosening fixture cannot activate before the full-year replay passes | Must · MVP-J (R2: the full-year replay as an automated nightly gate; until then a loosening change stays inactive) | derived |
| FR-SVC-005 | Version, sign and effective-date every profile; retain history; audit every change; a change that alters priority or limits requires Tier 2 approval (`FR-SAFE-020`) before activation | Decision register R10 | Rollback to a prior version works; a priority/limit change fixture is blocked without a recorded second approver | Must · MVP-J (R2: profile signing) | derived |
| FR-SVC-006 | `ARB`/`DISP` consult a profile generically for admission/control-mode/priority/failure-behaviour — no per-type code branch | Brief §3.5 | No per-type conditional found in a code-structure review | Must · MVP-J | user |
| FR-SVC-007 | `CTR`/`MV`/`BILL` consult a profile generically for completion and M&V/billing rules | Brief §3.5 | No per-type conditional found | Must · MVP-J | user |
| FR-SVC-008 | Profile declares asset scope (fleet/zone/substation/bank/feeder/corridor/unit) consumed by `TWIN` | Brief §3.5 | Scope resolves correctly against topology | Must · MVP-J | user |
| FR-SVC-009 | Profile declares firmness/priority class and displacement cost consumed by `ARB` | Brief §3.5 | Consumed correctly in an arbitration test | Must · MVP-J | user |
| FR-SVC-010 | Profile declares failure behaviour (substitution, degraded mode, notification, hold-then-schedule) consumed by `DISP`/`SAFE` | Brief §3.5 | Consumed correctly in a fault-injection test | Must · MVP-J | user |
| FR-SVC-011 | Dry-run a new/modified profile against `grid-sim`/`agent-sim` before live activation | Safety | Dry-run fixture exercises the profile without a live effect | Should · R2 | derived |
| FR-SVC-012 | Expose the profile catalogue (list, version, validation status) to the system-admin persona | Vision persona 6.11 | Console view lists all profiles with status | Must · MVP-B | derived |
| FR-SVC-013 | Reject activation of a profile missing a required element or an unvalidatable control mode/scope | Defense in depth | Incomplete profile fixture rejected | Must · MVP-J | derived |
| FR-SVC-014 | Express every reviewer-proposed performance number (interval-compliance %, availability %, telemetry latency, ramp-to-full time, P10 kW, declaration time, etc.) as a configurable, per-profile field, defaulting to the reviewer's figure, never a value hard-coded into application logic | Decision register R11 | Changing a profile's configured target (e.g., raising `DIST_DEFERRAL`'s interval-compliance target) changes the enforced/measured target with no code change | Must · MVP-J | derived |
| FR-SVC-015 | Pin a running event to the dispatch-profile version active when it started, for evaluation and settlement; a tightened safety limit is enforced by the guardian within one cycle, and a loosened one waits for the next event (register V-27) | Decision register R10, V-27 | An in-flight event keeps its version for evaluation and settlement; a tightened safety limit binds within one cycle; a loosened limit applies only to an event that starts after the change | Must · MVP-J | derived |
| FR-SVC-016 | Express each contract form of a customer type as a profile variant of the one schema, selected per contract with no code branch: ALR and NCLR for `ERCOT_ENERGY`/`ERCOT_AS` (R17); `TOLLING` and `EVENT` for `PARTNER_CAPACITY` (R27); co-op/municipal and TDU (SB 415) for `DIST_DEFERRAL` (R27); statute-shaped `MOBILE_TEEEF` and the grid-parallel `MOBILE_DER` contract variant (R20) | Reviews showed that one service type can be dispatched under different rules; each form keeps its code and gains a variant (D0a) | Every variant validates against the schema; a code-structure review finds no per-variant branch; each variant's build tag is that of its own FR (`FR-CTR-020…026`) | Must · MVP-J | derived |

### 3.9 `CTR` — Contracts, obligations & events

| ID | Statement | Rationale | Acceptance criterion | Priority | Source |
|---|---|---|---|---|---|
| FR-CTR-001 | Model customer → contract → program → obligation → event as first-class entities | Brief §6 | Matches brief vocabulary exactly | Must · MVP-J | user |
| FR-CTR-002 | Distinct obligation shape for all nine customer types, none omitted | Brief §3.1 | Every code has a tested shape | Must · MVP-J | user |
| FR-CTR-003 | `PARTNER_CAPACITY` terms: variant (`TOLLING` or `EVENT`, `FR-CTR-020`), rate, kW/hub, export limit, duration cap, term, 4CP re-opener, dual-participation mode (`FR-CTR-021`) | Proposed contract-terms bar; the only public partner contract is a tolling agreement (R27; GRD-015) | Fields validated on creation for both variants | Must · MVP-J | reviewer proposal — unverified |
| FR-CTR-004 | `DIST_DEFERRAL` terms: variant (co-op/municipal or TDU, `FR-CTR-022`), rating quantity and unit (kVA or per-phase A, R18), performance definition (outcome- or share-based, Q9), performance factor, liquidated damages, derate, term, firm priority | Proposed contract-terms bar; SB 415 terms apply to TDUs only (R27; GRD-048) | Settlement applies terms against a scenario; a contract without a rating unit or performance definition is rejected | Must · MVP-J | reviewer proposal — unverified |
| FR-CTR-005 | `DIST_DEFERRAL` sizing check before activation | Proposed sizing bar | Creation rejected if check fails | Must · MVP-J | reviewer proposal — unverified |
| FR-CTR-006 | `ERCOT_AS` stored-energy hold per product, per register V-33 — ECRS 1 h (NPRR1282, the claims check's value for Q7) and Non-Spin 4 h, falling to 2 h when NPRR1309 is implemented — as a profile field | Brief §3.1; `06-reviews/05` claim 6 | Shorter hold rejected; changing the configured duration changes the enforced hold with no code change | Must · MVP-J | regulation |
| FR-CTR-007 | `PARTNER_CAPACITY` event record supports P10/P50 | Proposed measurement need | P10/P50 computable from the record | Must · MVP-J | reviewer proposal — unverified |
| FR-CTR-008 | Opt-out/reserve-change counted as "unavailable" | Proposed contract-terms bar | Included in availability report | Must · MVP-J | reviewer proposal — unverified |
| FR-CTR-009 | Rolling failed-event count; auto-derate after threshold | Proposed contract-terms bar | Kw reduces after 2nd scripted failure | Should · R2 | reviewer proposal — unverified |
| FR-CTR-010 | "Storm hold" designation, distinct from a fault | Proposed contract-terms bar | Excluded from fault-rate, included in exceptions | Should · R2 | reviewer proposal — unverified |
| FR-CTR-011 | Record governing document/version per program; diff rule changes | Track 4CP review risk | Rule-version change visible against obligations | Should · R2 | regulation / reviewer proposal — unverified |
| FR-CTR-012 | Model `PIPELINE_AC`'s two dispatch profiles on equal footing: a smoothing obligation (H1/H2) with a contracted current/power band, ramp limit and cycling allowance — ordinary request-schema parameters, not a special restriction — dispatched via closed-loop control on the corridor's measured current; and a monitoring function (H3) whose profile simply has no dispatch/control-point request type, only an alert output | Service-agnostic dispatch — brief §1 | The smoothing profile dispatches real commands through the standard `ARB`/`DISP` path with no approval beyond ordinary contract/guardian validation, the same as any other obligation; the monitoring profile has no dispatch request type in its schema at all, so there is structurally nothing for it to gate | Must · MVP-J | user |
| FR-CTR-013 | Model `LARGE_LOAD`'s contract with its event-trigger definition (price/emergency threshold or utility instruction), zone/asset scope and discharge profile; dispatch it through the same event-based control mode and priority/arbitration path used by every other firm obligation | Service-agnostic dispatch — brief §1 | A `LARGE_LOAD` event dispatches through the identical `ARB`/`DISP` path as `PARTNER_CAPACITY`/`DIST_DEFERRAL`, evaluated only against contract terms, guardian limits and the arbitration outcome; a code/config review finds no `LARGE_LOAD`-specific branch anywhere in `ARB`/`DISP`/`SAFE` | Must · MVP-J | user |
| FR-CTR-014 | `PJM_CAPACITY` shape testable without a live connection, dispatched on a replayed historical 5CP day | Brief §3.1; all nine customer types are dispatchable in the judged build (R21) | Passes unit tests with no PJM dependency; the replayed day dispatches, settles and traces like any obligation | Must · MVP-J | user |
| FR-CTR-015 | Complete obligation/event state history, queryable | Vision §8 | Every transition retrievable in order | Must · MVP-J | derived |
| FR-CTR-016 | Reject/require precedence for two equal-priority firm obligations on one asset/window | Principle 4 | Conflicting pair rejected at setup | Should · MVP-J | derived |
| FR-CTR-017 | Model `MOBILE_TEEEF` as a distinct leased asset with availability calendar and precondition checklist, under the statute-shaped profile of `FR-CTR-024` | Brief §3.1 | Cannot activate until checklist complete | Must · MVP-J | user |
| FR-CTR-018 | Attach an optional "business-value recognition" status to an obligation (e.g., a credit not yet confirmed), never affecting whether it dispatches or settles | Service-agnostic dispatch | Dispatch/settlement succeed regardless of this status | Must · MVP-J | derived |
| FR-CTR-019 | Keep a per-territory market-role model: for each territory and ADER the LSE, QSE, Resource Entity and DSP; enforce one load zone, one LSE and one DSP per ALR-type ADER, a signed acknowledgment from every LSE of an NCLR-type ADER, and the submitting LSE for premises of 100 kW or less (`06-reviews/05` claim 4); in a NOIE territory, the NOIE's consent (as DSP, per premise) is an admission condition of every ERCOT lane and ERCOT value flows through the NOIE's contract with Base; per-utility tariff tables (NOIE bundled rates, buyback terms) drive delivery charges | An ALR needs every premise under one LSE and the NOIE is the LSE in its territory; a TDSP-only tariff misprices NOIE areas (R27, R27a, Q25; GRD-016, JDG-017) | An ERCOT-lane enrollment in a NOIE partition without recorded consent is refused with the reason; settlement in a NOIE partition books ERCOT value to the NOIE contract, not to Base's market revenue | Must · MVP-J | regulation |
| FR-CTR-020 | Offer a `TOLLING` variant of `PARTNER_CAPACITY`: continuous reservation of the tolled kW and kWh for the term (not only on likely event days), charge and discharge scheduled by the utility (setpoints or schedules over OpenADR, DNP3 or signed REST), SOC ownership rules, a cycle budget (`FR-PLAN-018`) and settlement on availability of the reserved capacity; the `EVENT` variant stays for utilities that buy events | Tolled capacity reserved only on likely days is sold twice (R27; GRD-015) | No other buyer — `ERCOT_ENERGY` included — is ever granted tolled kW on any day of the term; a utility schedule fixture is executed and its availability settled | Must · MVP-J | reviewer proposal — unverified |
| FR-CTR-021 | Set dual participation per partner: partner-as-QSE (the partner's own QSE decisions arbitrate its two services and Base executes them) or Base-as-QSE (allowed only when the partner's calls reach ERCOT beforehand through ADER telemetry and offers, `FR-ARB-013`); premises in ERCOT's ERS are refused for ADER lanes by a hard admission rule | The verified partner model runs both services on one fleet; the QSE decides between them (R27, Q6; GRD-024) | Each mode's fixture arbitrates as specified; an ERS premise is refused an ADER enrollment with the reason | Must · MVP-J | regulation |
| FR-CTR-022 | Offer a TDU variant of `DIST_DEFERRAL` for wires utilities under SB 415 (PURA §35.153): a reservation calendar (kW, hours) that is a hard ring-fence against ERCOT offers and telemetry (`FR-ARB-013`); discharge for the contract only on the TDU's direction; ERCOT sales from the reserved capacity only while the reservation is honoured; contract metadata (competitive-bid ID, the TDU's load-ratio share of the 100 MW statewide cap, prior PUCT authorization, power-generation-company registration) and reporting for the utility's cost comparison with traditional facilities; 16 TAC §25.58 is labelled a proposed rule until adopted; the co-op/municipal variant is unchanged | TDU storage contracts carry statutory terms that co-op and municipal contracts do not (R27; GRD-048; `06-reviews/05` claim 10) | A calendar fixture keeps its hours out of every ERCOT-visible quantity; a contract discharge without a TDU direction is refused; a TDU contract without its metadata is rejected | Must · R2 | regulation |
| FR-CTR-023 | Dispatch `PJM_CAPACITY` toward meter net load ≈ 0 (self-serve) at predicted coincident peaks unless the contract pays for export; non-firm by default; refuse premises registered with a PJM curtailment service provider unless the contract handles them | Export beyond the home's own load earns nothing toward the PLC; PJM demand-response load reductions are added back to the PLC, so only non-DR metered reduction lowers it and CSP enrollment double counts (R27; GRD-049; `06-reviews/05` claim 11); each customer's PLC method is verified with ComEd before value is counted (assumption) | A replayed 5CP-day fixture never exports beyond meter net load under a self-serve contract; a CSP-registered premise is refused with the reason | Must · MVP-J | regulation |
| FR-CTR-024 | Make the `MOBILE_TEEEF` profile statute-shaped (PURA §39.918 as amended by SB 231): island-forming only; under the lessee TDU's operational control; admitted only with a lessee-declared qualifying outage; no ERCOT telemetry or market participation and no energy or AS sales; for leases from 2025-06-20 the unit is mobile, movable from its staging site in under 12 h and of 5 MW or less, and the lessee's competitive bidding and prior commission authorization are recorded; Base reports readiness (all interlocks satisfied) and never initiates energization — the lessee's operator closes under a switching-order ID; island load planned at the measured cold-load factor within the unit's short-time rating; §39.918 does not reach co-ops or municipal utilities, which record their own legal basis; a deployment leaves `PENDING_SAFETY_REVIEW` only with the field-safety sign-off of Q20 by a licensed field engineer (`FSE`, `03-security` §5.1), never one who requested or operates that deployment (SoD-13); the lease's energy line is reviewed against the statute before billing | The statute limits leased units to restoring customers in isolation during a qualifying outage, and a remote party must not energize a utility circuit (R20; GRD-005, GRD-018, GRD-019; `06-reviews/05` claim 5) | A deployment without a declared qualifying outage is refused; a Base-initiated close is impossible; an island-plan fixture respects the cold-load factor and short-time rating; no TEEEF quantity appears in any ERCOT-visible value | Must · MVP-J | regulation |
| FR-CTR-025 | Offer `MOBILE_DER` as a separate contract variant for grid-parallel planned support by a mobile unit, requiring its own interconnection agreement and dispatched as a DER, never under the TEEEF profile | Grid-parallel support stays in scope outside the statute's authority (R20) | A `MOBILE_DER` contract without an interconnection agreement is rejected; its dispatch never uses the TEEEF profile | Should · R2 | regulation |
| FR-CTR-026 | Offer the NCLR variant of the ERCOT lanes: ingest the AS awards SCED still makes every interval; deploy only on an XML instruction at the instructed MW (overshoot ≤ 10%), hold until recall with energy sized per product (V-33), protect the pre-deployment baseline and score against the governing document's meter-before/meter-after baseline (the full 15-min interval before the instruction), tracking the 5-min telemetry baseline as well; make an L2 block during a deployment trigger an immediate QSE call and substitution inside the ADER; keep a failure counter (a counted failure is below 95%; 150% is a ceiling) with an alarm at the first failure, because two failures in a rolling 365 days disqualify the resource for at least 6 months (register R17) | NCLR ADERs are deployed by instruction and measured against a baseline, not dispatched by SCED (R17; GRD-017; `06-reviews/05` claim 3) | A deployment fixture holds until recall inside the band; the first failure raises the alarm and a second within 365 days is flagged as disqualifying | Must · R2 | regulation |
| FR-CTR-027 | Record, at deferral-contract intake, every other closed loop acting on the bank (utility DERMS, AC cycling, VVO/CVR) and assign exactly one integrating loop; record the rating quantity and unit, the performance definition and — for contracts on live banks — the utility's OMS/ADMS switching-order feed as a precondition (`FR-TWIN-014`) | Loops that fight on one bank, and topology that silently changes, defeat deferral control (R18, R28; GRD-027, GRD-028) | A contract without these fields cannot activate; the dispatcher reads the assigned loop role | Must · MVP-J | derived |
| FR-CTR-028 | For each contract pair that shares hubs, record each counterparty's own measurement method, predict at admission whether the same kWh would be counted by both, and require a non-stacking or co-counting clause keyed to those methods | "One kWh, one buyer" is a contract property as well as a ledger property: a counterparty's own M&V can count kWh attributed elsewhere (GRD-037) | An overlapping pair without a clause is refused at admission with the predicted overlap; the clause is referenced on both invoices | Must · MVP-J | derived |

### 3.10 `MV` — Measurement & verification

| ID | Statement | Rationale | Acceptance criterion | Priority | Source |
|---|---|---|---|---|---|
| FR-MV-001 | Ingest ≥ 1-min meter data per hub for active windows, from device-signed meter blocks (R33); settle on hub meters only for a counterparty that accepted the meter's certification (`FR-MV-011`), otherwise on AMI 15-min data with hub data as supporting evidence | Proposed "revenue-grade" bar; "revenue-grade" needs a certification basis each utility accepts (GRD-036) | Resolution ≤ 1 min, 100% of windows; a counterparty without an accepted certification settles on AMI in a fixture | Must · MVP-J | reviewer proposal — unverified |
| FR-MV-002 | Reconcile 1-min to 15-min data (hub meter blocks to utility AMI); flag out-of-tolerance variance | Proposed step-check bar | Correct flagging in/out of tolerance | Must · MVP-J | reviewer proposal — unverified |
| FR-MV-003 | Reconcile within 24 h of window end | Vision KPI-05 | 100% within 24 h | Must · MVP-J | reviewer proposal — unverified |
| FR-MV-004 | Continuous delivered-vs-committed per 15-min interval | Vision KPI-01 | Recomputed within one interval | Must · MVP-J | reviewer proposal — unverified |
| FR-MV-005 | Obligation availability (KPI-02) per period | Proposed availability bar | Matches manual recount on a sample | Must · MVP-J | reviewer proposal — unverified |
| FR-MV-006 | P10/P50 delivered kW per hub for `PARTNER_CAPACITY`/`LARGE_LOAD`; retain distribution | Proposed P10 need | Per-hub values retrievable, not just mean | Must · MVP-J | reviewer proposal — unverified |
| FR-MV-007 | `MOBILE_TEEEF` availability/deployment M&V, distinct from home-hub M&V | Brief §3.1 | Distinct, correctly attributed record | Must · MVP-J | user |
| FR-MV-008 | Live capture-ratio (KPI-07) with the fixed-schedule, today's-rule-allocator and perfect-foresight references shown, numerator and denominator visible | Measures the software on real prices (R24) | All four values shown together | Must · MVP-B | reviewer proposal — unverified |
| FR-MV-009 | Provide a backtest/replay mode, applicable identically to every obligation type without exception (including `LARGE_LOAD` and `PIPELINE_AC`), running historical external data through the same decision code path used live | Avoid live/replay drift, for any customer type; the replay harness also powers KPI-22 | Replay reproduces the original run's decisions for every obligation type sampled, `LARGE_LOAD`/`PIPELINE_AC` included | Must · MVP-B (R2: replay of every obligation type beyond the demo storyline's set) | derived |
| FR-MV-010 | For battery premises measured against a customer baseline, compute the baseline on net load minus the metered battery exchange (hub meter), or forbid pre-event charging in the adjustment window by contract; keep `DIRECT_HUB_METER` as the default method and the baseline as the counterparty's check; document event-day exclusions | Battery charging inside the adjustment window inflates a baseline, and excluded event days bias it (GRD-035) | A pre-event-charging fixture leaves the adjusted baseline unchanged; the exclusions are listed on the M&V record | Should · R2 | reviewer proposal — unverified |
| FR-MV-011 | Record, per counterparty, the hub meter's accuracy class and certification (e.g., ANSI C12.20 class 0.5), calibration and sealing regime as a contract precondition for hub-meter settlement | Settlement standing depends on a certification basis the counterparty accepts (GRD-036) | A contract selecting hub-meter settlement without these fields is refused; the M&V record names the metering basis used | Must · MVP-J | reviewer proposal — unverified |

### 3.11 `BILL` — Billing & settlement

| ID | Statement | Rationale | Acceptance criterion | Priority | Source |
|---|---|---|---|---|---|
| FR-BILL-001 | Settlement record per customer/contract/interval for every dispatched obligation, no exception | Brief §1 | No dispatched obligation lacks a record | Must · MVP-J | user |
| FR-BILL-002 | Quantity/rate/revenue/cost/margin traceable to M&V and the arbitration decision | Brief §1 | Every record links to its sources | Must · MVP-J | derived |
| FR-BILL-003 | Apply performance factor / liquidated-damages / derate automatically | Proposed contract-terms bar | Correct reduced amount in a scenario | Should · MVP-J (R2: liquidated-damages and derate automation) | reviewer proposal — unverified |
| FR-BILL-004 | Record RTC+B buyback cost only where ERCOT's settlement creates it — a forward release of AS capacity or a capability loss (utility block, safe stop, hub loss) inside an awarded interval — as a shadow-settlement line linked to its causing trace; an awarded hold is never diverted to a firm event, so a normal allocation never produces a buyback line | Regulation / proposed accounting; showing a diversion as a priced outcome misstates ADER rules (JDG-010) | A capability-loss fixture yields a buyback line linked to its trace; a firm-vs-AS contention fixture yields none | Should · MVP-J | regulation / reviewer proposal — unverified |
| FR-BILL-005 | Bill `MOBILE_TEEEF` per its lease terms, distinctly; the energy pass-through line is enabled only after its legal review against PURA §39.918 is recorded on the contract (R20) | Brief §3.1 | Invoice matches lease configuration; without the recorded review the energy line is absent | Must · MVP-J | user |
| FR-BILL-006 | Never double-bill the same delivered kWh across obligations, and disclose on the invoice any kWh a counterparty's own measurement method also counts (`FR-CTR-028`, `FR-BILL-011`) | Principle 4; internal attribution cannot stop two counterparties' own M&V counting the same kWh (GRD-037) | Zero double-billed kWh, property test | Must · MVP-J | derived |
| FR-BILL-007 | Support a linked "value not yet recognized" record that never blocks the delivery settlement | Service-agnostic dispatch | Settlement succeeds regardless of that status | Must · MVP-J | derived |
| FR-BILL-008 | Audited settlement correction/adjustment workflow | Real-world corrections | Correction produces a linked, audited record | Should · R2 | derived |
| FR-BILL-009 | Roll up to a per-customer/contract/period statement, exportable | Finance need | Export matches rolled-up figures | Should · R2 | derived |
| FR-BILL-010 | Complete settlement within the M&V SLA | Timeliness | 100% within SLA | Must · MVP-J | derived |
| FR-BILL-011 | Feed the M&V-overlap report into invoicing: where another counterparty's method would also count a kWh, the invoice line discloses it at each contract's rate as `CO_BENEFIT` or `CO_COUNTED` per the clause of `FR-CTR-028` | A priced, disclosed overlap is a negotiation lever without double counting (GRD-037, R24) | An overlapping-methods fixture shows the disclosure on both invoices with the clause reference | Should · MVP-B | derived |
| FR-BILL-012 | Keep settlement lines insert-only, keyed (contract, obligation, interval, line type, version) with supersede links; money and kWh in `numeric(18,6)` with half-even rounding at the invoice line (register V-39) | A late correction must never rewrite money, and float money breaks identical re-runs (R37; ARC-023) | An update attempt on a settlement line is refused; a correction adds a superseding version; re-running settlement yields identical line hashes | Must · MVP-J | derived |

### 3.12 `TRACE` — Decision audit & explainability

| ID | Statement | Rationale | Acceptance criterion | Priority | Source |
|---|---|---|---|---|---|
| FR-TRACE-001 | Structured trace per decision: inputs, options, constraints, winner, reason, losers' regret | Brief §1 | All six elements captured | Must · MVP-J | user |
| FR-TRACE-002 | Link trace to calls, commands, telemetry, M&V, settlement line | Brief §1 | Invoice line resolves through the full chain | Must · MVP-J | user |
| FR-TRACE-003 | Tamper-evident log (hash-chained/append-only) | Brief §1 | Alteration attempt is detectable | Must · MVP-J | user |
| FR-TRACE-004 | Replay a decision from recorded inputs | Brief §1 | Replay matches original outcome | Must · MVP-J | user |
| FR-TRACE-005 | Query path invoice line → full trace, bounded time | Vision KPI-15 | Returns within the KPI-15 bound | Must · MVP-J | derived |
| FR-TRACE-006 | Guardian blocks and AI proposals (accepted/rejected) recorded in the same structure | Consistency | Both appear in the same store | Must · MVP-J | derived |
| FR-TRACE-007 | Retain ≥ settlement retention period; export within access boundary | `NFR-227` | Export respects both constraints | Must · MVP-J | derived |
| FR-TRACE-008 | No command executes without a durable authorizing entry: a compact pre-image (decision id, version vector, batch hash) is persisted before the batch is signed and enriched asynchronously (R22) | Principle 5 | No command lacks a preceding pre-image; a crash between signing and enrichment leaves a verifiable pre-image | Must · MVP-J | derived |
| FR-TRACE-009 | Plain-language rendering, independent of `ai-agent` availability | Principle 5 | Legible with `ai-agent` disabled | Must · MVP-J | derived |
| FR-TRACE-010 | Measure/report trace completeness (KPI-14); alarm below 100% | Vision KPI-14 | Alarm fires on an incomplete-trace fixture | Must · MVP-J | derived |
| FR-TRACE-011 | With the audit database down, keep firm delivery running on producer-signed records written to a local journal whose head is anchored off-node every 10 s; journal integrity failure or no anchor for 5 min → CONSERVATIVE; both stores unavailable → no new commands; anchoring per register V-23 | No command without a durable trace, and no dispatch stop merely because the audit store stalls (R22, V-26; K3) | A database-outage fixture keeps firm delivery with every command journalled and anchored; a tampered journal entry forces CONSERVATIVE; with both stores down no new command is signed | Must · MVP-J | derived |

### 3.13 `INT` — Northbound integrations

| ID | Statement | Rationale | Acceptance criterion | Priority | Source |
|---|---|---|---|---|---|
| FR-INT-001 | OpenADR 3.0 VEN receiving VTN events into `contracts` | Brief §4 | One event → one `PARTNER_CAPACITY` record | Must · MVP-B | user |
| FR-INT-002 | OpenADR-event-level override/cancellation, authenticated and audited | Reviewer-proposed override concept | Mid-event cancellation stops dispatch within one tick | Must · MVP-B (MVP-J: cancellation of partner events received over signed REST) | reviewer proposal — unverified |
| FR-INT-003 | Simulated ERCOT QSE interface for offers, awards and set points, with schemas that follow ERCOT's: day-ahead AS awards as hourly MW per product at MCPC ($/MW-h), real-time awards per SCED run, ALR set points, NCLR deployments as instructions with ID, MW, time and recall | Brief §5; a simulator built on a wrong schema validates the wrong behaviour (R17; GRD-039) | Round-trip exercised in CI against `grid-sim`, including a partial award and a set-point trajectory | Must · MVP-J | user |
| FR-INT-004 | Deliver firm declarations to utilities and partners by 14:00 America/Chicago and ERCOT day-ahead offers before the 10:00 America/Chicago close — two distinct deadlines, each retried and alerted if unacknowledged | Vision KPI-06; the DAM offer close is not the firm-declaration deadline (JDG-011) | 100% of declarations delivered and acknowledged by 14:00 CT and 100% of offers submitted before 10:00 CT, or alarmed | Must · MVP-B | user |
| FR-INT-005 | Outbound signed, idempotent webhooks | Brief §4 | Retried delivery processed once | Should · MVP-J | user |
| FR-INT-006 | Version/quarantine on schema mismatch | Brief §9 | Unknown version quarantined | Must · MVP-J | derived |
| FR-INT-007 | Circuit breaker on a failing endpoint | Brief §9 | Breaker opens/alerts on threshold | Must · MVP-J (R2: breaker tuning) | derived |
| FR-INT-008 | Log every inbound/outbound message | Principle 5 | History reconstructable from logs | Must · MVP-J | derived |
| FR-INT-009 | Design-only PJM adapter matching the OpenADR contract | Brief §3.1 | Passes contract test, no live connection | Could · R2 | user |
| FR-INT-010 | One round-trip test per integration type in CI | Test coverage | Passing test per type | Must · MVP-J | derived |
| FR-INT-011 | De-duplicate inbound counterparty events | Principle 4 | Duplicate ID → one dispatch action | Must · MVP-J | derived |
| FR-INT-012 | Receive ERCOT instructions from the QSE interface as `IsoInstruction` records (set-point trajectory, manual deployment or recall, status change, emergency action), and record a verbal dispatch instruction (VDI) entered by the QSE-desk operator on a minimal entry form as the same record; acknowledge each within its timer, track execution to completion and link each to its decision trace; keep the instruction log with the settlement records (the full QSE-desk console is `FR-UI-026`) | ISO instructions are hard constraints that must be acknowledged, executed and evidenced (R17; GRD-021) | Every instruction fixture — a VDI entered on the form included — is acknowledged within its timer and shows its execution state and trace link; the log exports with settlement | Must · MVP-J | regulation |
| FR-INT-013 | Submit the Current Operating Plan (`FR-PLAN-017`) and every ERCOT-visible quantity (`FR-ARB-013`) through the QSE interface, with an acknowledgement check and an alarm on failure | ERCOT must see the plan and capability the ledger allows, in time (R17; GRD-023) | A COP resubmission fixture is acknowledged or alarmed within 60 min of the change | Must · MVP-J | regulation |

### 3.14 `SCAD` — SCADA/EMS/DERMS integration

| ID | Statement | Rationale | Acceptance criterion | Priority | Source |
|---|---|---|---|---|---|
| FR-SCAD-001 | Expose aggregated northbound points per bank/feeder/substation/zone/program/resource | Brief §3.4 | Matches the brief's named point set | Must · MVP-J (R2: the full point set; MVP-J: the virtual-resource point subset of one DNP3 association) | user |
| FR-SCAD-002 | Expose northbound control points (setpoint, enable/block, curtailment, e-stop, override) | Brief §3.4 | Each independently addressable | Must · MVP-J | user |
| FR-SCAD-003 | DNP3 (IEEE 1815) outstation with Secure Authentication + TLS | D4c / brief §3.4 | Unauthenticated/non-TLS poll rejected | Must · MVP-J (R2: DNP3 Secure Authentication; the judged build runs DNP3 over TLS under the Q11 exception, documented as a residual risk, with TLS in-process or in a terminator per the week-1 stack spike, `06-reviews/05` claim 12) | user / regulation |
| FR-SCAD-004 | IEC 60870-5-104 where required (register V-28) | Brief §3.4 | Round-trips against a simulated 104 peer | Could · R2 | user |
| FR-SCAD-005 | IEEE 2030.5 server/client sharing the DNP3 point registry; the IEEE 2030.5 application layer lives in `integrations` (R6) | Brief §3.4 | Cross-protocol point update reflected both ways | Must · R2 | user |
| FR-SCAD-006 | ICCP/TASE.2 for QSE telemetry (simulated), on a licensed TASE.2 stack (Q11); until then the labelled `SIM` stub of `FR-SCAD-020` | Brief §3.4; the stack is commercial (R44; ARC-034) | Round-trips against a simulated ICCP peer | Must · R2 | user |
| FR-SCAD-007 | Southbound ingest (DNP3 master/ICCP-historian/OPC UA) with quality/failover/latency budget | Brief §3.4 | Reading carries quality flag, meets latency budget | Must · MVP-J (R2: ICCP/historian and OPC UA ingest; MVP-J: DNP3 master poll) | user |
| FR-SCAD-008 | Versioned, diffable point-mapping registry | Brief §3.4 | Diffable change without breaking existing mapping | Must · MVP-J | user |
| FR-SCAD-009 | Deadbands, report-by-exception, event classes per config | Brief §3.4 | Sub-deadband change not streamed | Should · MVP-J | user |
| FR-SCAD-010 | Synchronized timestamping; time-sync status as its own point | Brief §3.4 | Drift fixture reflected in status point | Must · MVP-J | user |
| FR-SCAD-011 | Protect command order on every control point per register R29: select-before-operate or direct operate as the point map's per-point SBO/DO column says (`07-scada-integration.md` §3.1), DNP3 application-layer sequencing, Secure Authentication anti-replay and state-machine preconditions (a release requires an engaged stop; a setpoint requires enable); the extra `COMMAND_SEQ` point only on associations without Secure Authentication | Brief §3.4; IEEE 1815; utilities commonly send restrictive controls as direct operate (GRD-053, GRD-054) | Direct operate without select is rejected on every point whose column requires SBO and accepted on every point whose column allows DO; a release without an engaged stop and a setpoint without enable are rejected | Must · MVP-J | user / regulation |
| FR-SCAD-012 | Documented interlock rule for a same-tick SCADA/dispatch conflict, recorded in `TRACE` | Brief §3.4 | Conflict resolves per rule, traced | Must · MVP-J | user |
| FR-SCAD-013 | Redundant sessions, failover within the latency budget | Brief §3.4 | Failover within the KPI-04 bound | Should · R2 | user |
| FR-SCAD-014 | Commissioning/point-to-point test mode before live enable | Brief §3.4 | Untested point cannot go live | Must · R2 | user |
| FR-SCAD-015 | DNP3/ICCP/2030.5 conformance suites against `grid-sim` | Brief §3.4 | CI reports pass/fail per behaviour | Must · R2 (MVP-J: a DNP3 packet capture in the evidence pack) | user |
| FR-SCAD-016 | Same quality-flagging and guardian validation as any other input | Defense in depth | No bypass path | Must · MVP-J | derived |
| FR-SCAD-017 | Secure ICCP/TASE.2 per IEC 62351-4 | D4c | Unauthenticated ICCP session rejected | Must · R2 | user / regulation |
| FR-SCAD-018 | Secure IEC 60870-5-104 per IEC 62351-3 (TLS) / 62351-5 (application auth) | D4c | Unauthenticated/unencrypted 104 session rejected | Could · R2 | user / regulation |
| FR-SCAD-019 | Segment the SCADA/DERMS network path and monitor it for anomalous traffic, distinct from application-layer anomaly detection | D4c "segmented and monitored" | Segmentation verified; monitoring fires on an injected anomalous-traffic fixture | Must · R2 (MVP-J: namespace NetworkPolicy isolation of `scada-gateway`, `FR-SEC-004`) | user |
| FR-SCAD-020 | Until an ICCP/TASE.2 licence is decided (Q11), carry the QSE-to-ERCOT telemetry path as a protocol-level stub labelled `SIM` in the console, the traces and the judged evidence | A path without a licensed stack must never be presented as real (R44; ARC-034) | Every ICCP-path value and screen carries the `SIM` label; the evidence pack says so | Must · MVP-J | derived |

### 3.15 `SAFE` — Guardian safety & command safety

*Per decision register R3 (as amended in register v0.2; proposed until Q1 is confirmed), R4 and R16, every
command-safety and kill-switch FR below follows one policy with four classes. **Stop engage** — a stop or block at
bank, zone or fleet scope: one qualified operator, explicit confirmation (typed scope, reason, blast-radius preview), the
stop executes at once, and a second approver co-signs within 15 minutes (V-15), with escalation if the co-sign is
missing; an ERCOT verbal dispatch instruction or a utility instruction logged by the operator is a qualifying trigger.
**Tier 1 (explicit confirmation)** — ≥ 1 MW, or ≥ 25% of the target resource, or a discretionary increase of a customer's
declared capacity, or releasing capacity to another buyer. **Tier 2 (a second, distinct approver)** — ≥ 5 MW, fleet-wide
mode changes, a kill-switch release at any scope, and dispatch-profile changes that alter priority or limits.
**Automatic (pre-authorized)** — downward re-declarations of available capacity, ERCOT telemetry and COP updates.
Confirmation and approval windows: V-12 (Tier 1 expires in 2 min), V-13 (Tier 2 in 10 min; single-use tokens), V-14
(rolling 15-min cumulative windows per invoker and per scope, and across principals per bank and zone). The invoker and
the approver are never the same person; the Q1 default co-signer or approver is a shift supervisor for bank and zone
scope and the executive on call (or a second shift supervisor) for fleet scope — never the system admin, who is barred
from approving dispatch (`03-security` SoD-03). Stop ramps and release follow V-16 and V-17; the ramp values are unsigned
until Q13.*

| ID | Statement | Rationale | Acceptance criterion | Priority | Source |
|---|---|---|---|---|---|
| FR-SAFE-001 | Independently validate every command against physical/contractual limits | Brief §5 | Blocked even if `dispatcher` would allow it | Must · MVP-J | user |
| FR-SAFE-002 | Independently validate against the reserve floor, zero exceptions | Principle 2 | 100% of breaching commands blocked | Must · MVP-J | user |
| FR-SAFE-003 | Independently re-check per-hub grants never exceed available energy | Defense in depth | Fault-injected over-allocation caught | Must · MVP-J | derived |
| FR-SAFE-004 | Block a grid-stress-creating command regardless of origin | Brief §9 | Bypass of `FR-DISP-007` still blocked | Must · MVP-J | user |
| FR-SAFE-005 | Detect anomalous requests (magnitude, identity, bursts) and handle them within safety limits (flag, rate-limit, or require the Tier 1/2 confirmation the request's own characteristics warrant) — never refuse a request solely because of doubts about a service's business value; only a genuine safety, contract or authorization failure is grounds for refusal | Decision register (coordinator correction); D0b service-agnostic dispatch extended to anomaly handling | Three anomaly types (magnitude/identity/burst) are detected and handled per policy; a fixture representing a legitimate but unusual first-of-its-kind service call within contract and safety limits is flagged for review and still dispatched, distinguished from a fixture that actually breaches a safety/authorization limit, which is refused | Must · MVP-J | user |
| FR-SAFE-006 | Bank-scope stop — **stop engage**: one qualified operator engages a stop of every hub behind one bank with a mandatory reason (qualifying triggers include "ERCOT verbal dispatch instruction" and "utility instruction") and explicit confirmation (typed scope, blast-radius preview); it executes at once, ramps down per V-16 (protective: 30 s), and a second approver co-signs within 15 minutes (V-15; Q1 default: a shift supervisor) | D2; decision register R3 (amended), R4; control-room practice lets any qualified operator curtail at once (GRD-010) | The stop reaches hubs within one control cycle and completes ramp-down within 30 s; invocation without a reason or confirmation is rejected; a missing co-sign after 15 min escalates and never reverts the stop | Must · MVP-J | user |
| FR-SAFE-007 | Zone-scope stop — **stop engage** at zone scope, with the same single-person engage, mandatory reason and confirmation as `FR-SAFE-006`; protective ramp-down 60 s (V-16); co-sign within 15 minutes by a second approver distinct from the invoker (Q1 default: a shift supervisor) | D2; decision register R3 (amended), R4; a stop that waits for a supervisor adds minutes when a utility or ERCOT calls for it (GRD-010) | The stop takes effect at once on one confirmation and completes ramp-down within 60 s; the co-sign is recorded or escalated at 15 min; the invoker cannot co-sign | Must · MVP-J | user |
| FR-SAFE-008 | Fleet-scope stop — **stop engage** at fleet scope, with the same single-person engage, mandatory reason and confirmation as `FR-SAFE-006`; protective ramp-down 120 s (V-16); co-sign within 15 minutes by a senior second approver distinct from the invoker (Q1 default: the executive on call or a second shift supervisor — never the system admin, `03-security` SoD-03); ADER telemetry and the COP updated in the same cycle and an ERCOT hotline notice when more than 20 MW is affected | D2; decision register R3 (amended), R4, V-16 (GRD-010, GRD-025) | The stop takes effect at once on one confirmation and completes ramp-down within 120 s; telemetry/COP update and the hotline task appear in the same cycle; the co-sign is recorded or escalated at 15 min | Must · MVP-J | user |
| FR-SAFE-009 | Audited release: releasing any stop scope (bank, zone or fleet) is always **Tier 2** — a distinct, explicitly confirmed second-approver action — followed by the reverse of the stop sequence (ADER telemetry and COP first, hotline notice when more than 20 MW) and a staged ramp-up over ≥ 15 minutes within the fleet ramp table (V-17, V-30); never an instantaneous return to full output and never a timer-based auto-release; a stop engaged by a utility is released only by that utility (Q10 default); the Safe-Stop Authority can never release (R16) | D2; decision register R3, R4, R16 | A release without a second approver is refused at every scope; the released scope ramps up in stages over ≥ 15 min; no scope self-releases on a timer; a Safe-Stop-Authority release attempt is impossible | Must · MVP-J | user |
| FR-SAFE-010 | Single, traversable invoke → approve → release record per kill-switch action, retrievable by the auditor role | D2, vision persona 6.12 | Full record retrievable for a sampled action | Must · MVP-J | derived |
| FR-SAFE-011 | Auto-invoke scoped safe degraded mode on an unresolvable condition | Brief §1.5 | Persistent oscillation triggers it automatically | Must · MVP-J | derived |
| FR-SAFE-012 | Reject a command to a non-authenticated/not-in-good-standing hub | Defense in depth | Command to a revoked or quarantined hub rejected | Must · MVP-J | derived |
| FR-SAFE-013 | Log every guardian block/override into `TRACE` with the rule triggered | Principle 5 | Complete, rule-attributed entry | Must · MVP-J | derived |
| FR-SAFE-014 | If the guardian gives no verdict within its time budget (register V-35) or is unreachable, the batch stays unsigned, commands in force run to their lease (V-06) and the on-call is paged — a TIMEOUT is never a VETO and never a stop; `dispatcher` never bypasses the guardian (R1, R31) | Principle 5; a performance hiccup must not become a fleet stop (ARC-004) | A guardian-latency fixture of 1–5 s: no command signed by any other path, no automatic stop, hubs hold their leased setpoints and then follow V-07, a page is raised | Must · MVP-J | derived |
| FR-SAFE-015 | Pre-emptive breach-risk alert before a firm window opens, with the counterparty notified per contract | Vision KPI-13 | Meets the lead-time target of register V-41 in replays (median ≥ 60 min, P10 ≥ 15 min) | Must · MVP-B | derived |
| FR-SAFE-016 | Verify authenticated identity before honoring a utility/SCADA override | `SEC`/`SAFE` overlap | Unauthenticated override rejected | Must · MVP-J | derived |
| FR-SAFE-017 | Route adversarial-tagged fault injections through the identical guardian path | Vision §5.4 | Same block/audit behaviour as a real-attack test | Must · MVP-J | derived |
| FR-SAFE-018 | Enforce `MOBILE_TEEEF`/`PIPELINE_AC` preconditions (commissioning checklist, contracted band/cycling limit) as safety gates, never a business-case gate | Service-agnostic dispatch | Only a precondition failure blocks dispatch | Must · MVP-J | user |
| FR-SAFE-019 | Enforce a monotonic sequence number and expected-state precondition on every control path; reject out-of-order, stale or conflicting-precondition commands | D4a | Out-of-order/stale fixture rejected | Must · MVP-J | user |
| FR-SAFE-020 | Enforce the command-safety policy of the area note — stop engage, Tier 1, Tier 2 and automatic classes — for every control path, internal and SCADA alike | D4b; decision register R3 (amended; GRD-041) | Threshold-crossing fixtures at each named condition require exactly the class the policy specifies (0.99/1.00 MW and 4.99/5.00 MW boundaries included); a fixture just below every Tier 1 condition requires no confirmation; a downward re-declaration needs none | Must · MVP-J | user |
| FR-SAFE-021 | Reject a command conflicting with another already-accepted command for the same asset/window in the same tick; resolve via the documented interlock/precedence rule | D4a; generalizes `FR-SCAD-012` | Conflicting pair resolves per rule, never both execute | Must · MVP-J | user |
| FR-SAFE-022 | A stop triggered by the guardian's own risk-reducing rules executes without waiting for a person; the on-call is paged at once and a second approver co-signs within 15 minutes (V-15), as for any stop engage; only an explicit invariant veto — never a timeout — can lead to such a stop (R31) | Decision register R3 (amended), R31 | A guardian-initiated risk-reducing stop executes immediately; it is never reverted for lack of a co-sign, and a missing co-sign after 15 minutes escalates; a timeout fixture triggers no stop | Must · MVP-J | user |
| FR-SAFE-023 | Execute a pre-agreed utility SCADA control within its contracted limits without requiring human confirmation; reject (not queue) any such control that falls outside its contracted limits | Decision register R3 | An in-limit fixture executes with no confirmation prompt; an out-of-limit fixture is rejected immediately, never held pending a confirmation that will never come | Must · MVP-J | user |
| FR-SAFE-024 | Always execute a stop/block command from an authenticated, authorized utility, regardless of the orchestrator's own Tier 1/Tier 2 confirmation state | Decision register R3/R4; register Q10 | An authorized utility's stop/block fixture executes even where an equivalent Base-originated command would need a pending confirmation/approval | Must · MVP-J | user |
| FR-SAFE-025 | Give operators a stop path that never waits on the component that signs dispatch: the console's stop goes through the guardian in normal operation, and the out-of-band hardware-token trigger goes to the independent Safe-Stop Authority (`safe-stop`), which signs only a scoped `SAFE_STOP`/`CEASE` (setpoint 0, V-16 ramp) under its `safe-stop-only` key (V-11); hubs accept a stop signed by either; the console and the stop record show which authority signed each stop; the Safe-Stop Authority can never release | A stop must work with `api`, console, `dispatcher` and `guardian` down, without adding a second signer of anything that moves MW (R16; RT-001, RT-002, ARC-024); UI-SAF-08, UI-OOB-01…04 | With the guardian pod stopped and both guardian replicas isolated, an out-of-band bank, zone and fleet stop each reach reachable hubs within one control cycle; a release attempt through the Safe-Stop Authority is impossible; each stop record names its signer | Must · MVP-J | user |
| FR-SAFE-026 | Sum calls across principals per bank and per zone in the rolling windows of register V-14, so that several principals below a tier threshold cannot together exceed it unnoticed | Cumulation per principal alone lets distinct principals coordinate under the radar (RT-012) | Three principals each below Tier 1 but together ≥ 1 MW behind one bank in 15 min trigger the Tier 1 class | Must · MVP-J | derived |
| FR-SAFE-027 | Apply the emergency posture during an ERCOT EEA (register V-31, R19): no grid charging except recovery to the contractual minimum reserve at a capped rate or an explicit ERCOT instruction; awarded or deployed AS never withdrawn without a hotline call; a storm hold declared before the EEA is met by discharging less, never by charging; no reserve raise by grid charging | Raising homeowner reserves by grid charging at EEA2 withdraws awarded AS and turns the fleet into a charger when ERCOT is short (GRD-004; Q14); an operator policy — ERCOT's EEA charging rule binds registered storage resources, not this ADER fleet (`06-reviews/05` claim 7); an on-line ALR ADER keeps following its SCED set point | An "EEA2 at 18:00 with 40% of hubs below 50%" fixture produces no grid charging beyond the two exceptions and no AS withdrawal; the storm hold is met by discharging less | Must · MVP-J | regulation |
| FR-SAFE-028 | Sequence and gate stops per register V-16: protective stops (safety, security, utility stop, guardian-triggered, Safe-Stop Authority) ramp per scope at once, with ADER telemetry and the COP updated in the same cycle and an ERCOT hotline notice when more than 20 MW is affected; non-protective stops ramp no faster than the discretionary cap of V-30, are held while frequency is below 59.95 Hz or during an EEA, and run telemetry/COP update → hotline notice → ramp; every stop is one signed broadcast per scope on a retained scope topic, and the affected counterparty is notified at once | A stop removes injection SCED is counting on and can deepen an under-frequency event (R4, R26; GRD-025) | A non-protective stop fixture at 59.94 Hz is held and released when frequency recovers; a protective one proceeds; a > 20 MW stop raises the hotline task before its ramp; a reconnecting hub reads the retained stop state on subscribe | Must · MVP-J | regulation |
| FR-SAFE-029 | Enforce the ISO-boundary invariant in the guardian (register R17): every ERCOT-visible range ≤ ledger-free capacity, and telemetered AS capability per product ≤ ledger-free capacity for the product's duration — telemetered capability, not offers, is the boundary | ERCOT creates a proxy AS offer for every qualified resource at every SCED run (MW = MPC for a load resource) and caps awards by telemetered AS capability (Protocols §6.5.7.3(5); GRD-002; `06-reviews/05` claim 2) | A fixture that would telemeter reserved kW, or AS capability the ledger cannot hold for the product's duration, is vetoed with the rule ID | Must · MVP-J | regulation |
| FR-SAFE-030 | Apply the fleet ramp table of register V-30 in the guardian: firm and ISO-instructed changes follow their contracted ramps and are pre-staged; coincident firm starts above 50 MW are announced to ERCOT through ADER telemetry and the COP; discretionary actions ≤ 50 MW/min fleet and ≤ 10 MW/min for non-firm services; telemetered ADER ramp rates = min(physical, guardian-permitted share); values unsigned until Q13 | One normative ramp table for firm response, fleet caps and SCED ramps; coincident partner programs at 4CP need pre-staging (GRD-012, GRD-013) | A coincident 4CP fixture pre-stages the firm starts and announces them; a discretionary fixture never exceeds its cap; telemetered ramps never exceed the guardian-permitted change | Must · MVP-J | derived |
| FR-SAFE-031 | Execute pre-authorized, risk-reducing updates automatically: downward re-declarations of available capacity, ERCOT telemetry and COP updates need no human confirmation; a discretionary increase of declared capacity and releasing capacity to another buyer stay Tier 1 (R3 amended) | An honest downward re-declaration must never wait in a confirmation queue (GRD-041) | A downward re-declaration fixture is sent within the contract's re-declaration time with no prompt; an upward fixture requires Tier 1 | Must · MVP-J | derived |
| FR-SAFE-032 | Provide a dispatch-key epoch authority independent of the guardian, under two-person custody (security + SRE), that can advance the key epoch so every outstanding command of a compromised guardian is invalidated without the guardian's cooperation (R16, V-10) | Stopping a rogue guardian is not enough; its outstanding commands must also be invalidated (RT-002, RT-010) | After an epoch advance by the two custodians, hubs reject every command signed under the old epoch within one lease period; a single custodian cannot advance it | Must · MVP-J | derived |
| FR-SAFE-033 | Give each distribution counterparty a stop path that does not traverse the orchestrator — a CSIP control to the hub or the IEEE 1547 permit-service function — and record which path each counterparty holds | During a command-path outage a utility block must still reach hubs (R25; GRD-011) | A counterparty stop through its own path ceases export at the hub with the orchestrator's command path down | Must · R2 | regulation |

### 3.16 `SEC` — Security functions

| ID | Statement | Rationale | Acceptance criterion | Priority | Source |
|---|---|---|---|---|---|
| FR-SEC-001 | OIDC auth; MFA for plan-approval/override/kill-switch roles | Brief §4 | MFA-less login rejected for those roles | Must · MVP-J | user |
| FR-SEC-002 | Centrally evaluated authorization policy (OPA) | Brief §4 | One rule change affects all consumers consistently | Must · MVP-J | user |
| FR-SEC-003 | Unique per-device X.509 certs | Brief §4 | No shared cert in a fleet audit | Must · MVP-J | user |
| FR-SEC-004 | mTLS service-to-service with distinct identities; namespaces isolated by NetworkPolicy | Zero trust | Impersonation without key rejected; cross-namespace traffic outside the allowed flows is dropped | Must · MVP-J (R2: service-to-service mTLS; MVP-J: NetworkPolicy isolation between namespaces and TLS on every external edge) | derived |
| FR-SEC-005 | Every dispatch-affecting request (incl. AI tool calls, SCADA commands) carries a verifiable, attributable signature | Brief §1.5 | 100% resolve to a signed identity | Must · MVP-J | user |
| FR-SEC-006 | Immutable audit entry for every auth/authz/dispatch/guardian-block/config-change/kill-switch/AI-tool-call event | Brief §1.5 | Append-only; covers all seven types | Must · MVP-J | user |
| FR-SEC-007 | Detect/rate-limit abnormal request patterns per identity | Brief §9 | Burst vs. abuse classified correctly | Must · MVP-J | user |
| FR-SEC-008 | Detect/reject overload-risk requests, attributed to identity | Brief §9 | Rejection logged with identity | Must · MVP-J | user |
| FR-SEC-009 | Auto-rotate certs pre-expiry; revoke on compromise or quarantine (register C-14) | Standard practice | No unrotated near-expiry cert; revocation within one tick | Must · R2 | derived |
| FR-SEC-010 | Encrypt in transit and PII-adjacent data at rest | Standard practice | TLS fleet-wide; at-rest encryption verified | Must · MVP-J | derived |
| FR-SEC-011 | Security-event feed to SOC, distinct from ops alarms | Vision persona 6.9 | Separate, filterable views | Must · MVP-J | derived |
| FR-SEC-012 | Least-privilege scoping of every role | Data-boundary correctness | Cross-role access attempt denied | Must · MVP-J | derived |
| FR-SEC-013 | Never log a secret anywhere | Standard practice | Secret-scan finds zero matches | Must · MVP-J | derived |
| FR-SEC-014 | End-to-end incident path: SOC detect → scoped stop (guardian or Safe-Stop Authority) → audit export | Vision §5.4 | Full path executes, exportable record | Must · MVP-J | derived |
| FR-SEC-015 | Maintain the role catalogue of `03-security/02-security-architecture.md` §5.1, which is authoritative (register V-37): it covers every persona of the vision (control-room operator, fleet operator, reliability engineer, trader, utility grid-ops engineer, partner-program manager, settlement analyst, billing admin, SOC analyst, SRE, system admin, auditor, executive), the QSE-desk operator (R25; code `QSD`, console alias `QSE`) and the licensed field engineer who signs off `MOBILE_TEEEF` field safety (Q20; code `FSE`), each least-privilege-scoped and reviewed on a documented cadence | D1; V-37 | Every catalogue role exists in the identity realm and OPA with a distinct permission set; the console uses the aliases of `05-testing/01` §4.2; review cadence documented and met | Must · MVP-J (R2: the per-role review cadence) | user |

### 3.17 `OPS` — Operability

| ID | Statement | Rationale | Acceptance criterion | Priority | Source |
|---|---|---|---|---|---|
| FR-OPS-001 | Alert rule for every §3 KPI threshold, routed as a ticket or dashboard; only the paging budget of register V-25 (≤ 25 rules: SLO burn, safety invariants, loss of control, security) pages a person (R41) | Vision §3; one person on call cannot answer ~100 paging rules (ARC-033) | Every KPI has a mapped, tested route; the paging list has ≤ 25 rules, each with an owner and hours | Must · R2 (MVP-J: ten alerts with five runbooks for the demo) | derived |
| FR-OPS-002 | Linked runbook per alert rule | Brief §7 | No orphan alerts | Must · R2 (MVP-J: runbooks for the five paging scenarios reachable in the demo) | user |
| FR-OPS-003 | Feature flags, audited, role-restricted | Operability | Toggle logged, denied to unauthorized role | Must · R2 | derived |
| FR-OPS-004 | Externalized config from images | Portability | Same image behaves per environment, zero rebuild | Must · MVP-J | user |
| FR-OPS-005 | Documented, escalating kill-switch approval workflow matching the two-tier policy and the bank/zone/fleet ramp timings in `FR-SAFE-006/007/008/009` | Governance | Workflow doc matches enforced tiers and ramp timings exactly | Must · MVP-J | derived |
| FR-OPS-006 | Backoff retry, bounded, then dead-letter with alert | Brief §9 | Persistent failure → dead-letter + alert, no silent drop | Must · MVP-J | user |
| FR-OPS-007 | Self-recover; alert after N failed attempts | Brief §9 | Auto-restart; alert only after Nth failure | Must · MVP-J | user |
| FR-OPS-008 | Alert on capacity thresholds pre-limit | Brief §9 | Fires before the hard limit | Must · R2 | user |
| FR-OPS-009 | Horizontal scaling of stateless services to scale targets | Brief §4 | Scale-out increases capacity without downtime | Must · R2 | user |
| FR-OPS-010 | Documented HA posture, deferred properties labelled | Brief §4 | HA test matches documented posture | Must · MVP-J | user |
| FR-OPS-011 | Automatable portability audit | Brief §4 | Fixture with host-specific state fails the audit | Should · R2 | user |
| FR-OPS-012 | Never bind 80/443 or interfere with co-located services | Brief §4 | Manifest review + live port-check confirm no conflict | Must · MVP-J | user |
| FR-OPS-013 | Config/flag change-history view | Operability | Viewable by SRE role without DB/git access | Should · R2 | derived |
| FR-OPS-014 | Ship an evaluator's path: a demo values profile (`PROFILE=demo`) with seeded contracts, profiles, topology and users; `make demo` on a laptop k3d cluster; a two-page README quick start; an operator quick reference for the top five tasks; an integrator guide for the `DeviceAdapter`; a runbook index linked from alarms | "Installable, documented, sane defaults" is the usability bar; the node deploy exists, the evaluator's path does not (JDG-029, R23, R35) | A person who has not seen the system installs the demo profile on a laptop and completes the five quick-reference tasks from the documents alone | Must · MVP-B | derived |

### 3.18 `UI` — Console functions

| ID | Statement | Rationale | Acceptance criterion | Priority | Source |
|---|---|---|---|---|---|
| FR-UI-001 | Fleet-map view, filterable by type/geography | Vision persona 6.1 | Filter shows only relevant assets | Must · MVP-B | derived |
| FR-UI-002 | Per-obligation status board | Vision §8 | Reflects state change within one refresh | Must · MVP-J | derived |
| FR-UI-003 | Searchable, plain-language arbitration/decision log | Principle 5 | Correct search results | Must · MVP-J | derived |
| FR-UI-004 | Alarm feed distinguishing safe-hold from action-needed | Vision persona 6.1 | Visibly different treatment | Must · MVP-J | derived |
| FR-UI-005 | Day-ahead plan review/approval screen, audited | `FR-PLAN-013` | Decision recorded with operator identity | Must · MVP-B | derived |
| FR-UI-006 | Override/kill-switch controls, role-restricted, with the tier-appropriate confirmation | `FR-OPS-005`, `FR-SEC-002/012` | Unauthorized role cannot see/invoke | Must · MVP-J | derived |
| FR-UI-007 | Live 8-KPI headline scorecard on the operations screen — KPI-22, KPI-01, KPI-23, KPI-24, KPI-13, KPI-25, KPI-14, KPI-20 (vision §3.1) — each with measured value, target and provenance label, and drill-down to every other KPI; a KPI without data yet reads "not yet computed" | Vision §5.4; a 21-row scorecard is unreadable in a 7-minute demo (JDG-013, R24); UI-OPS-10 | Changes with underlying telemetry; each tile shows value, target and provenance; every drill-down KPI is one click away | Must · MVP-J | derived |
| FR-UI-008 | Show orchestrator-measured facts next to the business-case claims they bear on (e.g., measured per-hub P10 kW next to the 9.5 kW claim), each labelled with its provenance and linked to the Projects Deck, which keeps the business-case condition board (D0f) | Vision persona 6.13; a met/partly/unknown condition board in the dispatch console blurs the line between dispatch and business-case evaluation (JDG-026) | Every shown fact is computed by the orchestrator and links to the Projects Deck; no business-case verdict is computed in the console | Should · MVP-J (R2: a facts panel for every business-case condition) | derived |
| FR-UI-009 | Screen data scoped to logged-in role | `FR-SEC-012` | Cross-role leak attempt fails | Must · MVP-J | derived |
| FR-UI-010 | Live WebSocket updates on every board | Brief §4 | Visible without manual refresh | Must · MVP-J | user |
| FR-UI-011 | SOC security/audit view, distinct from ops alarms | `FR-SEC-011` | Demonstrably separate screens | Must · R2 | derived |
| FR-UI-012 | Finance settlement/M&V view with a link to the full trace per line | `FR-BILL-002`, `FR-TRACE-005` | Trace retrievable in one flow | Must · MVP-J | derived |
| FR-UI-013 | Arbitration view: winner, losers, regret per contested tick | Vision §8.10 | All three rendered for a sampled tick | Must · MVP-J | derived |
| FR-UI-014 | AI-copilot chat surface, every exchange logged | `FR-AI-005/006/009` | Every exchange appears in the audit log | Must · MVP-B | derived |
| FR-UI-015 | SCADA point/commissioning view, including the command log (select, operate, rejections with expected vs received sequence) | Vision persona 6.16 | Shows commissioning pass/fail per point and every control with its outcome | Must · MVP-J (R2: commissioning pass/fail per point) | derived |
| FR-UI-016 | Dispatch-profile catalogue view (list, version, validation status) for the system-admin persona | `FR-SVC-012` | Console lists all profiles with status | Must · MVP-B | derived |
| FR-UI-017 | Fleet-operator asset view: enrollment, decommission, topology assignment, `MOBILE_TEEEF` calendar | Vision persona 6.2 | Enroll/decommission actions available and audited | Must · R2 | derived |
| FR-UI-018 | Billing-admin view: contract rate configuration, correction workflow, unbilled-obligation alerts | Vision persona 6.8 | All three functions present and role-scoped | Must · R2 | derived |
| FR-UI-019 | Auditor/compliance view: decision-trace sampling, kill-switch invoke/approve/release records, access logs | Vision persona 6.12 | All three record types retrievable | Must · R2 (MVP-J: audit-chain verification on the stops-and-approvals screen) | derived |
| FR-UI-020 | `MOBILE_TEEEF` rendered with a distinct icon/colour outside the seven-colour customer-type palette | D3 | Visually distinct in a UI review | Must · MVP-J | user |
| FR-UI-021 | Console enforces the tier-appropriate confirmation / second-approver prompts matching `FR-SAFE-020` for critical-impact commands | D4b | Prompt sequence matches the enforced thresholds | Must · MVP-J | derived |
| FR-UI-022 | System-admin view for user/role provisioning and least-privilege review | Vision persona 6.11 | Role grants reviewable and change-audited | Must · R2 | derived |
| FR-UI-023 | Insights view: ownership heatmap for the next 36 h and the past (customer type × hour, planned vs realized, kW and $ per cell, click → traces); price of firmness per hour against the contract payment (firmness premium); breach radar for every firm obligation with the calibration plot of `FR-RPT-012`; displacement ledger by customer pair ($, kWh); M&V-overlap table per contract pair; capture ratio with numerator and denominator | The engine computes these; unsurfaced, they earn nothing (R24; JDG-004; `FR-DE-050`); UI-INS-01…09 | Every panel renders from plan duals, traces and M&V records for a sampled day; values reconcile with their sources | Must · MVP-B | derived |
| FR-UI-024 | Performance strip on the operations screen: live tick p99, telemetry → twin p99 and commands/s, each with hardware, profile and hub count, linked to the benchmark report (`FR-RPT-013`) | Performance must be measured and visible, not asserted (R24; JDG-020); UI-OPS-11 | The strip updates live during a 2,000-hub run; its values match the Prometheus histograms | Must · MVP-B | derived |
| FR-UI-025 | Show both day-ahead deadlines as distinct markers on the planning and obligation screens: 10:00 CT ERCOT offers and 14:00 CT firm declarations, each with its countdown and status | The two deadlines differ and the UI must not bind "declaration" to the DAM close (JDG-011); `04-ui` §2.2 status bar, UI-OBL-04, UI-PLN-01 | Both markers render with correct countdowns across a DST change; status flips independently | Must · MVP-B | derived |
| FR-UI-026 | QSE-desk console: ISO-instruction log with acknowledgement timer and execution status (`FR-INT-012`), verbal dispatch instruction and hotline log, EEA board with the fleet's posture, a "what ERCOT sees" history view (the live panel is `FR-UI-027`), shift log and handover, and a utility/ERCOT contact directory | A QSE runs a staffed desk; the operator needs ERCOT's view beside the orchestrator's (R25; GRD-021, GRD-046); UI-GOP-01…07 | Each panel renders from its source; a verbal instruction entered on the desk is acknowledged, executed and traced | Must · R2 | regulation |
| FR-UI-027 | Show "what ERCOT sees" beside the internal state on the dispatch view: per ADER, net load against the set point, MPC, LPC, status, AS capability per product and the current COP hour, with the margin of the ISO-boundary invariant (`FR-SAFE-029`) | The operator must see ERCOT's view where arbitration happens, before the full QSE desk exists (R17, R25; GRD-046); UI-DSP-17 | Each value equals its source for the same interval; a margin below zero is highlighted with the rule ID | Must · MVP-B | regulation |

### 3.19 `SIM` — Agent-sim & grid-sim behaviours and fault injection

| ID | Statement | Rationale | Acceptance criterion | Priority | Source |
|---|---|---|---|---|---|
| FR-SIM-001 | Realistic per-hub load/solar shape from real reference data | Brief §1.2 | Matches source dataset's profile within tolerance | Must · MVP-J | user |
| FR-SIM-002 | EV charging sessions per documented share/timing | Derived | Matches configured model over a long run | Must · MVP-J | derived |
| FR-SIM-003 | House islanding event via the real telemetry path | Brief §9 | Identical shape to a real event | Must · MVP-J | user |
| FR-SIM-004 | Hub faults, individually addressable and background-injectable | Brief §9 | Each type targetable/observable | Must · MVP-J | user |
| FR-SIM-005 | Opt-out/reserve-change requests, on demand or background | Brief §9 | Produces the expected shape | Must · MVP-J | user |
| FR-SIM-006 | Per-hub firmware/protocol limits, configurable subset | Brief §9 | Configured hub never exceeds its limit | Should · MVP-J | user |
| FR-SIM-007 | Simulated hubs authenticate exactly as real ones, even under load test | Brief §1.2 | Load-test harness cannot bypass enrollment | Must · MVP-J | user |
| FR-SIM-008 | Substation SCADA from real zone load rescaled, labelled a proxy, injectable faults | Reviewer proposal — unverified (label the proxy) | Proxy label present; fault injection produces correct flag | Must · MVP-J | reviewer proposal — unverified |
| FR-SIM-009 | Utility OpenADR VTN firing events on demand/schedule, incl. mid-event cancellation | Derived | Cancellation exercises `FR-DISP-017` | Must · MVP-J | derived |
| FR-SIM-010 | ERCOT QSE counterparty for bids/holds/awards, incl. partial award | Derived | Partial award handled without assuming full award | Must · MVP-J | derived |
| FR-SIM-011 | Simulate a large-load stress/event signal that triggers `LARGE_LOAD` dispatch through the standard event-based control mode, the same way the simulated OpenADR VTN event (`FR-SIM-009`) triggers a `PARTNER_CAPACITY` obligation | Service-agnostic dispatch — parity with every other simulated counterparty signal | Triggers a real, billed dispatch every time the contract is active, following the identical path exercised for `FR-SIM-009` | Must · MVP-J | derived |
| FR-SIM-012 | Deterministic, seedable fault-injection script for the judged demo | Vision §5.4 | Same seed reproduces the same sequence | Must · MVP-J | derived |
| FR-SIM-013 | Drive ≥ 10,000 simulated hubs from a load generator on a separate LAN host (R35, Q24): against the node when it can carry them within its budget — after the micro-benchmarks, or with the guest's RAM raised (register Q26) — and otherwise against the replica VM with the same chart and profile (`06` §1.9.1) | Brief §4; a co-located generator makes every performance number unverifiable (ARC-005, ARC-029); the 10,000-hub profile with every component exceeds the node's pod memory (R35) | Load test sustains cadence within budget with the generator's own saturation metrics below their limits for the whole run; the report and every chart built from it name the environment (node or replica VM) | Must · MVP-B | user |
| FR-SIM-014 | Tag every injected fault for ground-truth validation | Derived | KPI-12 validated against the fault log | Must · MVP-J | derived |
| FR-SIM-015 | Simulate DNP3/ICCP/IEEE 2030.5 counterparties sufficient for `SCAD` conformance testing | Brief §3.4 | `FR-SCAD-015`'s suite passes against these | Must · MVP-J (R2: the ICCP and IEEE 2030.5 counterparts; MVP-J: the DNP3 RTU and master) | user |
| FR-SIM-016 | Simulate at least three concurrently active `MOBILE_TEEEF` units' commissioning/precondition checks and deployment lifecycle, to exercise scheduling contention with the home fleet in the judged demo | Brief §3.1; decision register Q19 | A scripted commissioning failure blocks dispatch, per `FR-SAFE-018`; the demo runs with three units contending for scheduling attention against active home-fleet obligations | Must · MVP-J | user |
| FR-SIM-017 | Simulate a corridor's line-current response to a `PIPELINE_AC` smoothing command | Brief §3.1 (H1/H2 dispatch) | Response fixture is reproducible from a recorded seed | Must · MVP-J | derived |
| FR-SIM-018 | Run `agent-sim` and the fault proxy as separate deployments on a LAN host off the node, connecting through the node's LAN-only MQTT port (Q21); publish their own saturation metrics; mark a run invalid when the generator saturates; inject faults only through the simulator's fault API | Performance evidence must measure the system, not its co-located generator; the system must not test itself (R35; JDG-028, ARC-029) | A run records generator host, CPU and lag; a saturated-generator fixture marks the run invalid; no fault path bypasses the fault API | Must · MVP-J | derived |
| FR-SIM-019 | Let `grid-sim` emit, on demand and by script: ERCOT set-point trajectories, manual deployments and recalls, status changes and emergency actions; EEA levels; frequency events (e.g., 59.85 Hz for 60 s); ICCP/QSE-link loss; lessee `MOBILE_TEEEF` deployment requests with declared qualifying outages and switching-order IDs | The fixtures of R17, R19, R20, R25 and R26 need a counterparty that produces them | Each event type is reproducible from a seed and reaches the orchestrator through the same interface a real counterparty would use | Must · MVP-J | derived |
| FR-SIM-020 | Let `agent-sim` implement the device-side safety rules the design assumes: the pinned safe-stop root and DV-17 (a `safe-stop-only` key can sign only stop/cease at setpoint 0), per-issuer epoch floors, autonomous-response reason codes with the autonomous ΔP, energy registers and `boot_id`, IEEE 1547 settings read-back | The mock hubs must exercise what real firmware is asked to support (Q2; R16, R26, R33) | A stop signed by the Safe-Stop Authority is accepted and anything else under that key is rejected; the telemetry schema passes the device-contract conformance suite | Must · MVP-J | derived |

### 3.20 `AI` — AI agent

| ID | Statement | Rationale | Acceptance criterion | Priority | Source |
|---|---|---|---|---|---|
| FR-AI-001 | Tool calls against orchestrator APIs only, never a direct dispatch write path | Brief §1 | No code path writes a command directly | Must · MVP-B | user |
| FR-AI-002 | Cloud backend default, local OpenAI-compatible fallback, config-selectable; the exact model ID (e.g., `claude-haiku-4-5-20251001`) is configuration, recorded in every decision trace | Brief §1; decision register R12 | Backend/model switch, no code change; the model ID used for a given decision is retrievable from its trace entry | Must · MVP-B | user |
| FR-AI-003 | Propose an allocation with rationale for novel conflicts; never delays a control-loop tick | Brief §1 | Slow/unavailable model never delays a `DISP` tick | Must · R2 | user |
| FR-AI-004 | Independent validation (contract, OPA, guardian) of every AI proposal and human confirmation of every one before it applies; an applied proposal is a time-boxed, versioned constraint set (R49) | Brief §1; "approved automatically" and "requires confirmation" cannot both hold (ARC-049) | Out-of-policy proposal blocked pre-effect; no proposal applies without a recorded human confirmation | Must · R2 | user |
| FR-AI-005 | Natural-language explanation grounded strictly in a retrieved, cited trace entry | Brief §1 | Every explanation cites a real trace entry | Must · MVP-B | user |
| FR-AI-006 | Operator copilot over live state, no dispatch action itself | Brief §1 | Copilot session never issues a state-changing call | Must · MVP-B | user |
| FR-AI-007 | Incident triage/summarization | Brief §1 | Clustered-alarm fixture → one coherent summary | Should · R2 | user |
| FR-AI-008 | Structured intake of an unstructured request, always human-confirmed before `ARB` | Brief §1 | No intake reaches `ARB` unconfirmed | Must · R2 | user |
| FR-AI-009 | Record every prompt/tool call/response/model version in `TRACE` | Brief §1 | Past decision stays explainable after a model change | Must · MVP-B | user |
| FR-AI-010 | Prompt-injection and data-leakage controls | Brief §1 | Adversarial prompt fixture doesn't leak or bypass validation | Must · MVP-B | user |
| FR-AI-011 | Cost/rate budget per register V-22 ($25/day and $200/month hard caps), graceful degradation to deterministic templates on exhaustion; CI uses a mock | Brief §1 | Degrades gracefully, no operator-facing failure | Must · MVP-B | user |
| FR-AI-012 | Deterministic-only fallback on model unavailability/policy violation | Brief §1 | Simulated outage leaves dispatch paths unaffected | Must · MVP-B | user |
| FR-AI-013 | Classify every prompt-context field as personal or non-personal (`FR-PRIV-001`); strip/aggregate personal fields before any cloud-model call, with every aggregate meeting the 15/15 floor of register V-18. On this node/demo deployment, **decline** any query that fundamentally requires personal-data reasoning (no local model fits the node's pod memory budget, R2/R35); in a production deployment with a local model available, route such queries to the local model only. In the judged demo the decline is shown only as a scripted privacy beat beside the pre-send check log, or kept for Q&A (JDG-027) | D5 binding correction; decision register Q17 | No personal field ever appears in a cloud-model prompt in any environment; on this node, a personal-data-dependent query fixture is declined with a clear message that names the privacy rule; a production-configured environment with a local model routes the identical fixture to the local model instead of declining | Must · MVP-B | user |
| FR-AI-014 | Automated pre-send check logging that no personal-data field was present in a cloud-model prompt | D5 enforcement | Every cloud prompt has a logged, passing pre-send check | Must · MVP-B | derived |
| FR-AI-015 | Keep AI-off parity: with `ai-agent` switched off, every decision, settlement line and deterministic explanation is unchanged; the console offers the switch to authorized roles | The AI is a garnish, never load-bearing; a judge must see that nothing else changes (JDG-018); UI-GLB-08, UI-DSP-13 | Switching the agent off during a run changes no decision-trace hash and no invoice line; the deterministic explanation renders unchanged | Must · MVP-B | derived |

### 3.21 `PRIV` — Privacy of personal data

| ID | Statement | Rationale | Acceptance criterion | Priority | Source |
|---|---|---|---|---|---|
| FR-PRIV-001 | Maintain a data-classification registry marking every field as personal (identity/address, ESI ID/meter data, load-inferable routines, location) or non-personal | D5 | Every field in the schema is classified | Must · MVP-J | user |
| FR-PRIV-002 | Record a documented lawful basis and purpose per personal-data processing category | D5 | Every category has a recorded basis/purpose | Must · MVP-J | regulation / user |
| FR-PRIV-003 | Apply data minimization: no screen/export/service uses a personal field beyond its documented purpose | D5 | Field-use review finds no unjustified use | Must · MVP-J | regulation / user |
| FR-PRIV-004 | Provide a data-subject **access**-request fulfilment API, invoked by Base's existing support channel (this system does not build a homeowner-facing request-intake surface); return the held personal data within the deadline of register V-19 (30-day internal target; 45-day legal ceiling with a documented extension); the same deadline applies to `FR-PRIV-005…007` | D5; decision register Q16 | A request submitted via the support channel and routed to this API returns the correct data within the V-19 target in a test | Must · MVP-J | regulation / user |
| FR-PRIV-005 | Provide a data-subject **correction**-request fulfilment API, invoked by Base's existing support channel | D5; decision register Q16 | A correction submitted via the support channel and routed to this API is verified propagated | Must · MVP-J | regulation / user |
| FR-PRIV-006 | Provide a data-subject **deletion**-request fulfilment API, invoked by Base's existing support channel, subject to documented retention overrides with the override reason recorded; erasure is crypto-shredding that survives backups (`FR-PRIV-014`) | D5; decision register Q16, R38 | Deletion executes or a recorded override applies, when invoked via the support channel's call to this API | Must · MVP-J | regulation / user |
| FR-PRIV-007 | Provide a data-subject **opt-out**-of-non-essential-processing fulfilment API, invoked by Base's existing support channel, distinct from a grid-service opt-out | D5; decision register Q16 | Opt-out enforced and distinguishable from `FR-TWIN-006` when invoked via the support channel's call to this API | Must · MVP-J | regulation / user |
| FR-PRIV-008 | Enforce a documented retention schedule per personal-data category, no longer than any privacy-driven limit | D5 | Category retained no longer than its documented limit | Must · R2 (MVP-J: the schedule documented; the node holds no real personal data, Q21) | regulation / user |
| FR-PRIV-009 | Log every access to personal data (who, when, field, purpose), distinct from the dispatch decision trail | D5 "access logging" | Access log entry exists for every sampled access | Must · MVP-J | user |
| FR-PRIV-010 | Detect and notify a personal-data breach within the applicable regulatory timeline, per a documented runbook | D5 | Simulated breach fixture triggers the runbook within the timeline | Must · MVP-J (R2: automated breach detection; MVP-J: the runbook) | regulation / user |
| FR-PRIV-011 | Prevent any personal-data export to a third party, including a cloud LLM; `ai-agent` receives only non-personal/aggregated data, and every outbound or LLM-bound aggregate meets the 15/15 floor of register V-18 | D5 binding correction | No code path exports a classified-personal field externally; an aggregate below the floor is refused | Must · MVP-J | user |
| FR-PRIV-012 | Apply the same authentication, encryption and least-privilege controls to every personal-data field, no exception path | Defense in depth | No personal field is reachable outside `FR-SEC-003/004/010/012`'s controls | Must · MVP-J | derived |
| FR-PRIV-013 | Hold ERCOT requests for premise- or device-level ADER data until the user answers register Q12: the ERCOT lanes stay simulated and no real per-home data leaves the system; if Q12 is answered yes, such data goes to ERCOT only, on the regulatory and contractual basis disclosed at enrolment, and every other counterparty keeps receiving aggregates (V-18) | ERCOT's ADER rules can require premise- or device-level net-MW and SOC series and per-premise allocation factors; D5 allows no sharing today, so the series stay on the platform until Q12 is answered (GRD-042; Q12; `06-reviews/05` claim 4) | A premise-level request fixture is recorded and held with the Q12 reference; with a Q12 = yes configuration it is served to ERCOT only and logged | Must · MVP-J | regulation / user |
| FR-PRIV-014 | Wrap each subject's data key with a per-subject key held in a KMS/HSM or a key store that is never included in database backups; erasure destroys that key; document backup retention against the V-19 deadline | Restoring an older backup must not resurrect erased personal data (R38; ARC-022) | After an erasure, restoring a pre-erasure backup yields unreadable data for that subject | Must · MVP-J | regulation / user |

### 3.22 `RPT` — Insights & reporting

| ID | Statement | Rationale | Acceptance criterion | Priority | Source |
|---|---|---|---|---|---|
| FR-RPT-001 | Report which hours each customer type "owned" fleet capacity — planned vs realized, in kW and $ (the ownership map, `FR-DE-050`) | Brief §2 insight quality; shows the fleet is time-shared, not sold once (R24) | Time-allocation view across all nine types, reconciling with plan duals and realized traces | Must · MVP-B | derived |
| FR-RPT-002 | Report delivered-vs-committed trends, flag one heading toward breach | Derived | Flagged before it becomes a live incident | Must · MVP-B | derived |
| FR-RPT-003 | Report capture ratio over time with its reference values — fixed schedule, today's rule allocator and perfect foresight | Measures the software on real prices (R24) | Ratio + all three references shown | Must · MVP-B | reviewer proposal — unverified |
| FR-RPT-004 | Report P10/P50 per hub per program, exportable | Proposed P10 need | Export usable in a contract conversation | Must · MVP-B | reviewer proposal — unverified |
| FR-RPT-005 | Export the orchestrator-measured facts each business case needs (per-hub P10/P50, delivered vs committed, availability, capture ratio, M&V overlap), each with provenance, to the Projects Deck, which keeps the business-case condition board (D0f); the orchestrator computes no business-case verdict | Brief §3.1; business-case evaluation belongs to the Projects Deck (JDG-026) | The export carries provenance for every value; no met/partly/unknown status is computed in this system | Should · R2 | derived |
| FR-RPT-006 | Report fleet health/reliability trends | Vision persona 6.3 | Filterable by hub/site/bank/fault category | Must · R2 | derived |
| FR-RPT-007 | Report M&V variance trends, flag degrading obligations | Vision persona 6.7 | Flagged before next settlement | Should · MVP-B | derived |
| FR-RPT-008 | Exportable (CSV/PDF) reports | Brief §2 usability | Working export per report type | Should · MVP-B (R2: PDF) | derived |
| FR-RPT-009 | Report guardian/security event trends, incl. rejected AI proposals | Vision persona 6.9/6.10 | Three categories distinguishable | Must · R2 | derived |
| FR-RPT-010 | Alert when a monitored `PIPELINE_AC` corridor's public grid data changes, or a synthetic reading crosses a defined threshold — the H3 monitoring profile's own designed output is an alert, exactly as any signal-only profile's output is a reading rather than a command | ISO 18086, SP21424, 49 CFR §192.473(c) | Threshold-crossing fixture produces the labelled alert defined by the H3 profile's own schema | Should · MVP-J | regulation / reviewer proposal — unverified |
| FR-RPT-011 | Compute KPI-22 **value of orchestration**: replay the real ERCOT year (2025-09-23 → 2026-09-22 corpus, gaps labelled) with the same simulated fleet and contract set under four policies — perfect foresight, the orchestrator, today's rule-based allocator (a faithful port of `control_engine.py`) and a fixed seasonal schedule — and report per hub-year net value ($/hub-yr), firm-interval compliance, reserve violations, kWh claimed by two buyers, AS hold compliance and buyback cost, each labelled "real ERCOT prices, simulated fleet"; extends `FR-DE-122` | The single number behind the "why": it measures the software itself, not a business case (R24; JDG-005; D0f) | The report is reproducible from the corpus and the seed; all four policies run through the same replay harness (`FR-MV-009`); the operations screen shows it as tile 1 | Must · MVP-B | derived |
| FR-RPT-012 | Report breach-risk lead time and calibration from replays: the distribution of lead times from the first `AT_RISK` flag to each firm breach (target register V-41: median ≥ 60 min, P10 ≥ 15 min) and a calibration plot of predicted breach probability vs realized | One lead-time target and proof that the prediction is calibrated (R24; JDG-012) | The report shows the lead-time distribution against V-41 and the calibration plot over ≥ 30 replayed days | Must · MVP-B | derived |
| FR-RPT-013 | Publish performance evidence in a benchmark report (`bench/`): method, hardware, profile, seed and duration for every result; tick p99, telemetry → twin p99, command → acknowledgement p95, solve times, messages/s and MiB per 1,000 hubs; the 1k → 2k → 5k → 10k scaling curve; one before/after optimization (e.g., bucketed LP vs per-hub LP); only measured numbers are shown, and 100,000 hubs stays a labelled model until measured | Performance must be measured, not asserted (R24; JDG-020) | The report regenerates from recorded runs; every number on the operations strip links to its run | Must · MVP-B | derived |

## 4. Non-functional requirements

Product NFRs are `NFR-201…` (K1); architecture NFR-001…034 and platform NFR-500…552 are separate families. The Build
column follows the rule of §1.

| ID | Category | Statement | Target | Build | Source |
|---|---|---|---|---|---|
| NFR-201 | Performance | Control-loop tick fits its cadence budget (cycle per register V-03) | ≤ 2 s / ≤ 10 s, compute ≤ 80%; tick compute including the guardian's verdict p99 ≤ 250 ms at 10,000 hubs (KPI-25, `FR-DE-012`) | MVP-J | user |
| NFR-202 | Performance | SCED-aligned re-evaluation | ≥ every 5 min | MVP-J | user |
| NFR-203 | Performance | Intraday re-plan incl. solve time | ≥ every 15 min; solve within register V-20 (target 45 s, ceiling 120 s) | MVP-B | user |
| NFR-204 | Performance | Day-ahead solve margin before the ERCOT day-ahead market close (10:00 CT) | Solve within register V-20 (target 300 s, ceiling 900 s at demo scale); ≥ 4 h buffer before 10:00 CT | MVP-B | derived |
| NFR-205 | Performance | Telemetry ingest-to-available latency | ≤ 5 s at 10 s cadence | MVP-J | derived |
| NFR-206 | Performance | Console live-update latency, including under load shedding | ≤ 2 s; control-room channels (alarms, stop and kill-switch state, firm obligations) keep 1-s updates under every shedding level, and only analytic views slow down (R48; ARC-062) | MVP-J | derived |
| NFR-207 | Availability | Core dispatch path during a firm window | **Production:** ≥ 99.9% in firm windows, ≥ 99.5% overall, with a dependency matrix naming each command-path hop's degrade mode. **Node (single-node demo profile — a test target, not an SLO):** during the 24-h demo-profile soak and the three rehearsals, no firm interval falls below KPI-01 because of a platform fault, and every entry into AUTONOMOUS is recorded with its cause (ARC-036) | MVP-J (node test target); production target from the cutover | derived |
| NFR-208 | Availability | Utility/SCADA telemetry feed | ≥ 99% | MVP-J | reviewer proposal — unverified |
| NFR-209 | Availability | No unnotified maintenance overlap with a firm window | 0 | MVP-J | derived |
| NFR-210 | Scalability | Concurrent simulated hubs with the load generator off the node — on the node when it can carry them, otherwise on the replica VM, labelled (R35, Q26) | ≥ 10,000 measured; 100,000 a documented model, labelled until measured | MVP-B | user |
| NFR-211 | Scalability | Message-bus consumer lag at 10,000 hubs | < 1% delayed > 5 s | MVP-B | derived |
| NFR-212 | Scalability | Telemetry hypertable performance at scale | Sustains cadence; aggregates within budget | MVP-B | derived |
| NFR-213 | Security | Identity coverage, incl. AI tool calls | 100%; 0 shared/static credentials | MVP-J | user |
| NFR-214 | Security | Dispatch-command attributability | 100% resolve to a signed identity | MVP-J | derived |
| NFR-215 | Security | Container supply-chain hygiene | SBOM + scan every image | MVP-J | user |
| NFR-216 | Security | Secret handling | 0 plaintext secrets anywhere | MVP-J | derived |
| NFR-217 | Usability | First-session operator task completion | ≤ 10 min (assumption). **Verification:** a formative usability test — 5 participants × 3 demo tasks (respond to an `AT_RISK` obligation, trace an invoice line, engage and release a bank stop) — with task times and SUS published, failures included (JDG-022; G3-J item 7); the summative test with ≥ 32 participants is `R2` | MVP-B (R2: summative test) | assumption |
| NFR-218 | Usability | Plain-language console statuses | 0 undocumented codes shown. **Verification:** a CI scan of every status, reason and error code the console can render against the status glossary, plus the heuristics review checklist of `04-ui` §10.1 | MVP-J | derived |
| NFR-219 | Maintainability | CI test coverage of Must FRs, per build tag | 100% of the Must FRs of a build have ≥ 1 passing automated test when that build closes. **Verification:** `05-testing/build_traceability.py` reports uncovered Must FRs per build tag and fails CI | MVP-J | derived |
| NFR-220 | Maintainability | Config/threshold changes need no redeploy | Verified | MVP-J | derived |
| NFR-221 | Maintainability | Requirement traceability | Every FR ↔ component ↔ test case. **Verification:** `05-testing/build_traceability.py` regenerates `04-traceability-matrix.md` in CI and fails on a dangling ID, an FR without a test, or an FR count that disagrees with §5 of this document (JDG-024) | MVP-J | derived |
| NFR-222 | Portability | Zero host-specific state | 0 audit findings | MVP-J | user |
| NFR-223 | Portability | No dependency on unrelated co-located services | 0 findings | MVP-J | user |
| NFR-224 | Observability | End-to-end trace correlation | One trace ID, `ARB`→`DISP`→`DEV`→`BILL`/`TRACE` | MVP-J | derived |
| NFR-225 | Observability | Every §3 KPI is a live metric/dashboard | 100% of the KPIs whose data exists in the current build | MVP-J | derived |
| NFR-226 | Data retention | **Node** (this deployment): raw telemetry 7 days; 1-minute M&V data, commands, audit/decision-trace and settlement retained for the node's full operational life. **Production target:** raw telemetry tiered to object storage for ≥ 13 months; billing/settlement/decision-audit on write-once storage for 7 years | 7 days raw (node, all its life for the rest); ≥ 13 mo raw via tiering + 7 yr M&V/settlement/trace (production, pending Q5 confirmation) | MVP-J (node); production target from the cutover | derived (decision register R9, Q5) |
| NFR-227 | Data retention | Audit/decision-trace immutability & retention | ≥ settlement period, append-only | MVP-J | derived |
| NFR-228 | Performance | `scada-gateway` point latency and time-sync accuracy | Within KPI-04 budget; clock sync ≤ 100 ms (assumption); hubs with clock skew > 250 ms are excluded from a bank loop's add-back (register V-34) | MVP-J | assumption |
| NFR-229 | Security | AI-agent cost/rate budget enforcement (register V-22) | 100% of periods within budget | MVP-B | derived |
| NFR-230 | Observability | AI outputs and proposals as observable as deterministic decisions | Every AI output and proposal in the same dashboards | MVP-B | derived |
| NFR-231 | Privacy | Personal-data field coverage under `PRIV` controls | 100% of classified-personal fields | MVP-J | derived (D5) |
| NFR-232 | Privacy | Zero personal-data egress to any third party or cloud LLM | 0 findings in an egress audit | MVP-J | user (D5) |
| NFR-233 | Performance | End-to-end firm response | Full output p99 ≤ 240 s from event receipt; requirement ≤ 300 s (reviewer proposal — unverified) — register V-34, per-segment budgets in `02-architecture/01` | MVP-J | derived |
| NFR-234 | Performance | Guardian time budget | Admission and signing p99 ≤ 250 ms per batch of ≤ 2,000 commands; no verdict within 2 × the budget is a TIMEOUT — never a veto, never a stop (register V-35, R31) | MVP-J | derived |
| NFR-235 | Safety | Stop propagation | A scoped stop reaches every reachable hub within one control cycle through either signer — the guardian or the Safe-Stop Authority — including with the guardian pod stopped; a reconnecting hub reads the retained stop state on subscribe (R16, V-16) | MVP-J | user |
| NFR-236 | Availability | Singleton-loop and shard-leader failover | ≤ 10 s p95, ≤ 15 s max (register V-02), demonstrated on the node with one warm standby per shard group (R35) | MVP-J | derived |

## 5. Requirement counts

Counts by area and build tag (v3.3; v3.2 had 323 FRs — 66 were added, none removed). The traceability generator checks
these totals (`NFR-221`).

| Area | FRs | `MVP-J` | `MVP-B` | `R2` | Area | FRs | `MVP-J` | `MVP-B` | `R2` |
|---|---|---|---|---|---|---|---|---|---|
| ING | 18 | 12 | 1 | 5 | INT | 13 | 9 | 3 | 1 |
| DEV | 20 | 16 | 2 | 2 | SCAD | 20 | 11 | 0 | 9 |
| TWIN | 15 | 12 | 0 | 3 | SAFE | 33 | 31 | 1 | 1 |
| FCST | 11 | 0 | 8 | 3 | SEC | 15 | 14 | 0 | 1 |
| PLAN | 18 | 4 | 13 | 1 | OPS | 14 | 6 | 1 | 7 |
| DISP | 32 | 30 | 1 | 1 | UI | 27 | 13 | 8 | 6 |
| ARB | 13 | 12 | 0 | 1 | SIM | 20 | 19 | 1 | 0 |
| SVC | 16 | 14 | 1 | 1 | AI | 15 | 0 | 11 | 4 |
| CTR | 28 | 22 | 0 | 6 | PRIV | 14 | 13 | 0 | 1 |
| MV | 11 | 8 | 2 | 1 | RPT | 13 | 1 | 9 | 3 |
| BILL | 12 | 9 | 1 | 2 | | | | | |
| TRACE | 11 | 11 | 0 | 0 | **Total FRs** | **389** | **267** | **63** | **59** |
| | | | | | **NFRs (NFR-201…236)** | **36** | **28** | **8** | **0** |

An FR whose tag carries a note (e.g., "MVP-J (R2: signing)") is counted under its first tag. `MVP-J` is large in FR count
because most FRs of the core chain are small; the effort view is the story-based release map of
`03-epics-and-user-stories.md`.

## 6. Open questions and assumptions

Per `00-decision-register.md`, this document's earlier open questions on data-subject request intake, local-model
routing and `MOBILE_TEEEF` demo scale are **resolved** by the register's Q16, Q17 and Q19 defaults and folded into
`FR-PRIV-004..007`, `FR-AI-013` and `FR-SIM-016`. What remains open:

1. **Proposed, awaiting confirmation — register Q1.** The amended R3 (single-person stop engage at every scope with a
   15-min co-sign, Tier 2 release, automatic downward re-declarations) and the second approver per scope are the working
   design of `FR-SAFE-006…009`, `-020`, `-022`, `-031`.
2. **Assumption — retention periods beyond R9's split** (`NFR-226`, `FR-PRIV-008`): the 7-year production
   figure for M&V/settlement/trace is the register's Q5 *proposed* default, not yet a confirmed decision;
   confirm before production cutover.
3. **Open question — register Q10 ("zone" definition and release authority).** `FR-SAFE-007`'s "zone" scope
   is used generically here; the register's Q10 default (utility operating zone for utility-facing controls, ERCOT load
   zone for market-facing ones; only the engaging party releases) is the working design of `FR-SAFE-009`; re-check the
   zone-scope FRs once Q10 is answered.
4. **Open question — register Q2/Q3.** Whether real Base hub firmware supports the primitives `FR-DEV-006`,
   `FR-SAFE-019`, `FR-SAFE-025` and `FR-DEV-019/020` assume (signed commands, sequence numbers and epochs, the safe-stop
   root and DV-17, settings read-back, reason codes, a local fallback schedule) is unconfirmed for real hardware; the mock
   agents implement all of it (`FR-SIM-020`), and `SHADOW` mode (`FR-DISP-032`) needs none of it.
5. **Open questions — register Q6, Q7, Q8, Q9, Q12, Q25** (QSE model and dual participation, AS durations, the SCADA
   commands utilities will send, deferral performance and overlap accounting, per-premise data to ERCOT, the demo
   territory) affect `FR-CTR-021`, `FR-CTR-006`, `FR-SCAD-002`, `FR-CTR-004/016`, `FR-PRIV-013` and `FR-ING-001`
   respectively — tracked in the register, not duplicated here.
6. **Open question — register Q11 and Q13.** The DNP3 Secure Authentication and ICCP licences (`FR-SCAD-003`, `-006`,
   `-020`) and the sign-off of the stop ramps and the fleet ramp table (`FR-SAFE-006…008`, `-028`, `-030`).
7. **Assumption — KPI-11/12/15/16 targets** remain proposed, not sourced; KPI-13 follows V-41 and KPI-21 follows V-19.
8. **Open question — `FR-CTR-016` precedence tie-break authority**, unresolved since v1.0.
9. **Open question — `FR-ING-001` zone list completeness.** The competitive-area zones of the ERCOT-lane partition
   (R27a) and `LZ_CPS`/`LZ_AEN` for the NOIE partitions are configuration; confirm the full zone list (and any per-partner
   settlement-point variations, e.g. CoServ, GVEC) with the business-case workstream before `R2` expands the fleet's
   modeled territory.

## 7. Cross-references

- `00-decision-register.md` — single source of truth for cross-document conflicts and open questions; wins
  over this document until this document is updated to match.
- `01-vision-scope-personas.md` — KPIs, personas, narratives, demo storyline.
- `03-epics-and-user-stories.md` — epics and stories implementing these FRs.
- `02-architecture/01-system-architecture.md` — topology incl. `scada-gateway`, `ai-agent`.
- `02-architecture/02-domain-model-and-interfaces.md` — ledger, `CTR`/`MV`/`BILL`/`TRACE`, `SVC` profile schema.
- `02-architecture/03-decision-engine.md` — `PLAN`/`DISP`/`ARB`, generic profile execution, R5/R13.
- `02-architecture/04-external-data-integration.md` — `ING`/`INT` detailed design; §15.2 maps its
  `FR-ING-101…173` back to `FR-ING-001…016` above.
- `02-architecture/05-failure-modes-and-recovery.md` — full failure catalogue.
- `02-architecture/06-platform-and-operations.md` — `OPS`, runbooks, alert rules, R2/R9 node-capacity detail.
- `02-architecture/07-scada-integration.md` — full `SCAD` design incl. D4 command safety, R6, Q10/Q11/Q12/Q13.
- `03-security/01-threat-model.md`, `03-security/02-security-architecture.md` — `SAFE`/`SEC`/`AI`/`PRIV`
  guardrails in full, R1 (guardian as sole signer).
- `04-ui/01-ui-ux-specification.md` — `UI` detailed screens, incl. new-role views and `MOBILE_TEEEF` styling.
- `05-testing/*` — tests proving every acceptance criterion above.
- `G:\OpenGrid\docs\business-case\01-reviewer-claims-verification.md` — fact-check record for every
  `reviewer proposal — unverified` tag (owned by the business-case workstream, not this system).
- `06-reviews/*` — the four adversarial reviews, the claims check (`05-claims-verification.md`) and the dispositions of
  every finding against this document (`resolution/A1-product-brief.md`).
