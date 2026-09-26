# Resolution A3 — Decision engine and external-data integration

Status: v1.0 · 2026-09-25 · Owner: decision-engine lead (grid-control and ERCOT-market engineer) · Documents changed:
`02-architecture/03-decision-engine.md` v0.1 → v0.2 and `02-architecture/04-external-data-integration.md` v0.1 → v0.2.

**Method.** Every finding that cites 03 or 04, and every register item assigned to them, was treated as a claim to be
proven. The quoted evidence was checked against the v0.1 text (backups kept in the session scratchpad); reviewer
arithmetic was re-derived; ERCOT and statutory facts were taken from the primary-source verdicts of
`06-reviews/05-claims-verification.md` ("claims check #n") and, where that file is silent, from primary texts already
extracted in the session (ADER Governing Document Phase 3.3, Nodal Protocols §3.9.1 and §6.5.9.3, the RTC+B Load Resource
and telemetry decks, the Austin Energy RCA) — never from a WebFetch summary alone. The register (v0.2) wins where it
decides a point. Binding rules respected: nothing is dropped (D0a) — every change keeps its service type and changes how
it is dispatched, measured, constrained or sequenced; ERCOT rules and statutes are constraints, never a judgment of a
service's value (D0b); research stays outside the engine (D0f); no personal data is shared (D5). No ID was renumbered.

Columns: **Verified?** Yes / Partly / No, with one line of evidence (and the re-derived arithmetic). **Disposition:**
Fixed / Partly fixed / Rejected / Deferred R2 / User decision Qn. **Where:** section and IDs of 03 (DE) or 04 (EXT).

## 1. Grid, ERCOT market and SCADA review (GRD-)

| ID | Verified? | Disposition | Where | Note |
|---|---|---|---|---|
| GRD-001 | Yes — v0.1 put `ERCOT_ENERGY` at T3, split one instruction into a T2 deployment and a T3 "base point minus the AS component" (§8.6.5), and priced a squeezed base point in Example A's variant (1,200 − 258 = 942 kW short, re-derived). ALR = SCED-dispatched CLR; online Non-Spin/ECRS ride the 4-s UDSP with no separate message; compliance on UDSP/CLREDP and Set Point Deviation (claims check #1) | Fixed | DE §2.1 (`IsoInstruction`), §2.3 (L2), §8.4 (A6), §8.5 (variant deleted), §8.6.4, §8.6.5, §8.6.10, §8.11 (pre-emption deleted); FR-DE-136, -139, -141 | FM §2.12.2 (BREACH_IMMINENT), FR-DISP-012 and TC-FUN-114/-129/-202 belong to other owners (§5 below) |
| GRD-002 | Yes, and broader — ERCOT builds a proxy AS offer for every qualified Resource in every SCED run, at MPC for Load Resources, capped by telemetered AS capability; a missing energy bid is replaced by the LPC–MPC range at VOLL (Prot. §6.5.7.3(5),(9),(12); claims check #2). v0.1 §7.2–§7.3 had no rule for what ERCOT is shown | Fixed | DE §7.3, §7.5 (ledger-free, guardian-permitted capability; proxy-offer guard; guardian invariant), §6.5 C22; FR-DE-137 | Telemetered AS capability = only MW Base accepts being awarded, always covered by offers |
| GRD-003 | Yes — re-derived: the kW law asks 8,500 − 7,900 = 600 kW; holding 8,000 kVA at Q = 3,000 kvar needs P ≤ √(8,000² − 3,000²) = 7,416 kW, i.e. 1,084 kW; 600/1,084 = 55%, so ≈ 45% under-relief (≈ 50% like-for-like with a 100 kVA margin: 1,192 kW) | Fixed | DE §8.6.1 (apparent power or per-phase current, fleet P and Q added back, unit-typed ratings), §8.5 Example B (kW law leaves 8,170 kVA = 102%), §0.3; FR-DE-144 | The reviewer's percentage is, if anything, conservative |
| GRD-004 | Yes — +30% × 39.2 kWh = 11.76 kWh per hub, ≈ 1.18 GWh at 100,000 hubs (re-derived). Legal basis narrower than implied: NPRR1002 binds registered ESRs, not the ADER Load-Resource fleet (claims check #7) — adopted as policy | Fixed | DE §8.6.9, §6.5 C23, §8.13 DM-14, §8.16 (non-protective stops held in EEA); FR-DE-147. EXT §11.10 (notices), §12.10; FR-ING-177, -180 | Q14 default and RP-59 belong to the register and 05 |
| GRD-005 | Yes, tightened — §39.918(b)(1),(c),(d) confirmed; SB 231 adds mobile, movable < 12 h, ≤ 5 MW, competitive bidding, prior PUCT authorization; not applicable to co-ops or municipal utilities (claims check #5). v0.1 had `GRID_PARALLEL` and "planned support" in the TEEEF profile | Fixed | DE §2.6 (`MOBILE_TEEEF` statute-shaped; `MOBILE_DER` variant), §8.6.7, §6.7.7; FR-DE-148, -075 | Register R20 still cites the stale texas.public.law text (§5) |
| GRD-006 | Yes — re-derived: 8,000 kVA / (√3 × 12.47 kV) = 370.4 A per phase; 395 A = 107%; a 97% three-phase total implies phases A/B ≈ 341 A | Fixed | DE §8.6.1 (per-phase law), §8.3 (phase in eligibility signatures), §4.2 (phase-aware records); FR-DE-144 | `phase` on ServiceTransformer/ServicePoint is 02's |
| GRD-007 | Yes — re-derived: with $M_k$ containing the fleet's charging, $g_{k+1}\le H-g_k$ alternates H, 0, H … from $g_0=0$; with a ramp it hovers at ≈ H/2; d cycles of SCADA delay give period 2(d+1) | Fixed | DE §8.6.1 ($H^{reb}$ from gross quantities), §6.5 C7(c); FR-DE-145 | Same formula required in guardian G-03 (03-security) |
| GRD-008 | Yes — re-derived the v0.1 law's fixed point: with the large load held at 302 kW, u = (700 + 0.3·423 − 140)/1.3 = 528.4 kW and the bank 130 kW under its limit (the reviewer's ≈ 530 / ≈ 130); with per-cycle re-arbitration the large load rises to 500 kW and u falls to 482.7 kW (kVA-consistent: ≈ 489 / 531 kW) | Fixed | DE §8.6.1 (performance basis `OUTCOME`/`SHARE`), §8.5 Example B in closed loop (SHARE 665/302 kW, $3,080/h; OUTCOME 200/500 kW, $0/h; energy 1,012 ≤ 1,224 and 1,788 ≤ 2,040 kWh); FR-DE-146 | Reviewer's number reproduced under its assumption; the full closed loop is worse |
| GRD-009 | Yes — v0.1 §8.6.2/§8.6.5 integral trims, §8.8 trust decay and substitution had no freeze; the 36 mHz / 5% droop defaults are the reviewer's source (not re-read) | Fixed | DE §8.6 preamble, §8.6.1 FROZEN state, §8.6.10, §8.8, §8.12, §8.13 DM-15; FR-DE-149 | PFR declaration at registration is 07's (register R26) |
| GRD-010 | Yes — v0.1 §8.15(b) made zone/fleet stops Tier 2 before execution; register R3 amended | Fixed | DE §8.15(b) (single-person engage, 15-min co-sign, V-15), §8.16 initiators; FR-DE-095 | FR-SAFE-007/008 and TC-FUN-503/505 are other owners' |
| GRD-011 | Yes — v0.1 §8.15(a): fallback "while it has an orchestrator heartbeat less than 15 min old"; DM §3.5 said the opposite | Fixed | DE §8.15(a) (V-07 verbatim), §8.7 (independent utility stop path), §8.16.1; FR-DE-090 | CSIP/permit-service path is 07 §6.13 |
| GRD-012 | Yes — re-derived: 40 + 100 + 50 = 190 MW at 50 MW/min = 3.8 min > 3 min; 600 MW = 12 min; 10% × 1,100 MW = 110 MW/min; firm starts at K/3 per minute are 63 MW/min for 190 MW | Fixed | DE §8.12 ramp-governance table (V-30), §7.5 pre-staging and announcement; FR-DE-089 | Guardian G-04 and TC-FUN-265 alignment are other owners' |
| GRD-013 | Yes — HDL/LDL = telemetered power ± 5 × normal ramp, clipped to MPC/LPC; AS awards capped by telemetered capability (claims check #14) | Fixed | DE §7.5 (ramp = min(physical, guardian-permitted share); invariant telemetered ramp × 5 min ≤ permitted change), §8.12; FR-DE-137 | That ERCOT accepts the controller-limited ramp is A-DE-44 |
| GRD-014 | Yes — v0.1 §4.1 and DM-10: "Hold last base point ≤ 1 interval, then energy request 0" | Fixed | DE §4.1, §8.13 DM-10 (hold flat, QSE-desk sequence), §8.6.10; FR-DE-139 | FM-MKT-011 (05) must match |
| GRD-015 | Yes — RCA 26-1526: "battery tolling agreement"; AE gets "operational control over the timing of charging and discharging" of the reserved portion (claims check #9) | Fixed | DE §2.6 `TOLLING`, §2.4 rule 6, §6.5 C21, §8.6.2; FR-DE-151. EXT §12.3 | Event variant kept |
| GRD-016 | Yes — ALR ADER needs one load zone, one LSE and one DSP; injections are negative load in the LSE QSE's settlement; 0 MW approved in `LZ_AEN`/`LZ_CPS` (claims checks #4, #15). v0.1 EXT §11.1.3 booked those zones as the fleet's own value and DE §6.3 used the Oncor charge everywhere | Fixed | DE §2.4 territory role model, §6.3 ($v^E$, per-utility $w_b$), §8.10, §10.5; FR-DE-152. EXT §3.2 `settlement_role`, §11.1.3, §11.11; FR-ING-174, -175 | — |
| GRD-017 | Partly — XML deployment, hold until recall, 95%/150% band confirmed; but SCED still awards AS to NCLRs; the GD's baseline is the full 15-min interval before the instruction (the 5-min average is the Protocols' generic rule); two failures in 365 days mean **disqualification**, not a possible suspension (claims check #3) | Fixed | DE §2.6 `NCLR` variants, §8.6.4, §8.5 Example C (NCLR variant), §10.3; FR-DE-140, -072 | Fixed with the corrections; which baseline ERCOT applies is A-DE-43 |
| GRD-018 | Yes — re-derived with PSRC 075 as checked in business case G11 (2.8× plateau, 5.3× at 0.4 s) and 4.0 kW/home: 150 × 4.0 = 600 kW → 1,680 kW at the plateau; 1,000/(4 × 2.8) = 89 homes/MW; TC-FUN-179's 800 kW → 2,240 kW | Fixed | DE §8.6.7 (≤ 80 homes per 1 MVA at 0.9 × kVA; blocks 47–94 homes from the short-time rating), §6.7.7; FR-DE-075, -042 | TC-FUN-179 rewrite is 05-testing's |
| GRD-019 | Yes — the TDU leases and operates (§39.918(b)); v0.1 §8.6.7 allowed a Base-initiated close with Tier 2 | Fixed | DE §8.6.7 (readiness only; the lessee closes under its switching-order ID), §2.6; FR-DE-148, -075 | CROB change is 07's |
| GRD-020 | Yes — GD 3.3 §5.c requires an attestation that ride-through settings match Nodal Operating Guide §2.6.2.1(2)/§2.9.2(3) (text read); v0.1 had no per-hub conformance gate | Partly fixed | DE §8.2 (eligibility), §8.8, §12.6 (fleet MW exposed); FR-DE-150 | Read-back, signed settings profile and rollout gate belong to 07/05/02 (register R26) |
| GRD-021 | Yes — no ISO-instruction call type, no VDI handling in v0.1 | Fixed | DE §2.1 `IsoInstruction` incl. VDIs and manual ECRS via the QSE desk, §8.13 DM-10; FR-DE-136 | QSE-desk staffing is register Q15 |
| GRD-022 | Yes — 4-s UDSP (Protocols §6.5.7.4.1(3)); the 4-min base ramp is a training-deck value, not a Protocol value (claims check #14) | Fixed | DE §3.1, §4.1, §4.3 (2-s members of an on-line ADER, V-03/V-32), §8.6.10 (4-min ramp configurable, A-DE-55); FR-DE-011, -020, -056 | Throughput (100,000 × 0.5 Hz = 50,000 msg/s) must be sized in 06 |
| GRD-023 | Yes — Protocols §3.9.1: COP for each hour of the next seven days, updated ≤ 60 min after an availability change, with AS capability by product (text read) | Fixed | DE §7.5 COP, §7.1, §6.10; FR-DE-138 | `CurrentOperatingPlan` entity is 02's |
| GRD-024 | Partly — GVEC's Base aggregation is qualified; its 4CP/arbitrage use is shown for 2025, before qualification; concurrent use after qualification unconfirmed (claims check #8) | Fixed | DE §2.4 dual-participation modes (partner-as-QSE; Base-as-QSE with before-the-fact visibility), §7.5; FR-DE-152; A-DE-41 | Q6 default change is the register's |
| GRD-025 | Yes — v0.1 §8.16 ramped every stop without telemetry/COP first and without frequency gating | Fixed | DE §8.16.1 (V-16 protective vs non-protective, sequence, 59.95 Hz/EEA hold), §8.16.3 (V-17); FR-DE-096, -098 | — |
| GRD-026 | Yes — v0.1 §4.2: frozen = "identical to the last 30 samples while … variance non-zero"; steps without SOE → A3 | Fixed | DE §4.2 (correlated-signal and deadband test; confirmed steps are topology events); FR-DE-158. EXT §12.2 deadbanded reporting | RTU deadband intake is 07 §4.8 |
| GRD-027 | Yes — v0.1 froze topology on unknown switching state; G-12's 24-h age rule is 03-security's | Fixed | DE §8.10 (OMS feed precondition; freshness = GIS version + applied orders), §4.1; FR-DE-159. EXT §12.4 OMS feed | G-12 wording belongs to 03-security |
| GRD-028 | Yes — v0.1 §8.12 "one controller per regulated quantity" covered only internal loops | Fixed | DE §8.6.1, §8.12; FR-DE-160, -088. EXT §12.2 second loop | — |
| GRD-029 | Yes — v0.1 §8.12 relied on ramping slower than LTC delays; `LTC_TAP` unused | Fixed | DE §8.6.1 (LTC counting, reversal cap A-DE-50), §8.12; FR-DE-162 | — |
| GRD-030 | Yes — re-derived: ±0.5–1% of an 8 MW full scale is ±40–80 kW against 25 kW deadbands | Fixed | DE §8.6.1 (45-s low-pass, 3σ fast path, per-bank deadbands from σ), §5.3, §12.4 churn KPI; FR-DE-161 | — |
| GRD-031 | Yes — v0.1 §8.8 decayed trust on volt-watt curtailment (FM exempted it); §8.2 ignored volt-var priority | Fixed | DE §8.2 ($P^{act}$ with $Q^{vv}$), §8.8 (no trust penalty; substitution to other transformers); FR-DE-060, -061, -149 | — |
| GRD-032 | Yes — re-derived: 950 × 5 kW = 4,750 kW < 8,400 kW; guardian G-02 export ≤ 0.8 × kVA vs dispatcher κ = 1.0 | Fixed | DE §8.10 (one distribution-defaults source), §8.5 Example A re-run with caps (4.0 kW home load, 7 kW export — inside the verified 6–7 kW), §5.3, §7.2; FR-DE-083, -053, -163 | G-01 must apply the export limit to net export at the meter (§5) |
| GRD-033 | Yes — v0.1 reverse-flow default at bank level only | Fixed | DE §8.10 table (feeder heads, unconfirmed regulators, reclose practice); FR-DE-163 | — |
| GRD-034 | Yes — re-derived: √3 × 138 kV × 0.98 = 234 kW per A at SF = 1; SF 0.1–0.3 → 0.78–2.34 MW per A (the SF range itself is the reviewer's, unverified) | Fixed | DE §8.6.6 (SF parameter, open loop without it, achieved ΔI reported), §2.6; FR-DE-164, -074. EXT §12.2, §12.7 | — |
| GRD-035 | Yes — logic checked against v0.1 §10.3 (10-of-10 with ±20% adjustment at the service point) | Fixed | DE §10.3 (`CBL_NET_OF_BATTERY`); FR-DE-165. EXT §12.9 battery channel | — |
| GRD-036 | Yes — v0.1 §10.1 asserted "revenue-grade" with no certification basis | Fixed | DE §10.1 (certification precondition; AMI default otherwise); FR-DE-173 | ANSI C12.20 class 0.5 labelled reviewer proposal — unverified |
| GRD-037 | Yes | Fixed | DE §2.4 (measurement methods, overlap predicted at admission, clauses), §10.4; FR-DE-166 | Tagged R2 (sequencing only) |
| GRD-038 | Yes — v0.1 labelled `SYNTHETIC` but did not stop firm declarations from it | Fixed | DE §5.1–§5.2 firm-fitness, §6.5 C13, §7.2; FR-DE-167. EXT §3.2 `firm_fitness`, §6; FR-ING-176 | — |
| GRD-039 | Yes — awards are hourly MW at MCPC (DAM) and per SCED run (RT); no "hold hours" or $/kW-yr in ERCOT awards (RTC+B telemetry deck) | Fixed | DE §7.3 award objects; FR-DE-142. EXT §12.6 | DM §4.3 example schema is 02's |
| GRD-040 | Yes — M-C110525-01: renamed to Set Point Deviation, AASP replaces AABP, new AS Imbalance Settlement (claims check #1); v0.1 valued −NL × SPP over whole premise load | Fixed | DE §10.5 (billing determinants per resource type and settlement party; time-weighted RT awards; incremental-NPC P&L); FR-DE-110 | Exact formulas remain A-DE-38 until reconciled with statements |
| GRD-041 | Yes — v0.1 §8.15(b) made any declared-capacity change Tier 1 | Fixed | DE §7.2, §8.15(b) (automatic downward re-declarations, telemetry and COP updates); FR-DE-052, -095 | Register R3 already amended |
| GRD-043 | Yes — v0.1 priced wear only linearly ($0.03/kWh); a daily toll implies ≈ 386 cycles/yr (business case G8) | Fixed | DE §6.3 $\Theta_b$, §6.5 C18, §5.3; FR-DE-155 | Tagged R2 |
| GRD-044 | Yes — re-derived from v0.1 Example C: 45 kW × 2 h × $4/MW-h = $0.36; × $1,000/MW-h = $90 | Fixed | DE §7.4 (CVaR95 conditional on the trigger, hard cap, shown in the Tier 1 confirmation), §5.2; FR-DE-168 | Tagged R2 (§7.4 is default off) |
| GRD-045 | Yes — v0.1 limited recharge only at bank level | Fixed | DE §6.5 C19, §8.6.1, §8.12, §8.16.3; FR-DE-156 | V-30 net-peak HE20–21 |
| GRD-047 | Yes — v0.1 C17 ranks recovery first while C7(b)/G-03 veto need-window charging | Fixed | DE §6.5 C17, C7(b), §8.6.1; FR-DE-157, -041 | — |
| GRD-048 | Yes in substance — but the statute is PURA §35.153 (not §35.152) and 16 TAC §25.58 is still proposed (Project 59523); contract MW discharged only on TDU direction (claims check #10) | Fixed | DE §2.6 `TDU_SB415`, §2.4 rule 7, §6.5 C20; FR-DE-153 | Tagged R2 |
| GRD-049 | Partly — PLC = customer load at the 5 peaks plus Load Drop Estimates plus losses, with a ComEd adjustment and a class-average fallback; "residential" is not in the text (claims check #11) | Fixed | DE §2.6, §8.6.8 (toward meter net load ≈ 0), §6.7.6, §10.3; FR-DE-043, -076, -154 | Non-firm by default (R27, register C-16) |
| GRD-051 | Yes — v0.1 HOLD kept $u_{k-1}$ below the schedule; recovery criteria differed from 07 | Fixed | DE §8.6.1 (HOLD = max(held, scheduled); V-38 return); FR-DE-069 | — |
| GRD-055 | Yes — v0.1 DE stale after 3 reports vs DM 15 min vs SC 120 s | Fixed | DE §4.1, §8.8 (V-29 connectivity and eligibility); FR-DE-020 | — |
| GRD-056 | Yes — NPRR1282 set ECRS to 1 h from RTC+B go-live 2025-12-05; the CRA's "2 h" is stale; Non-Spin 4 h, 2 h at NPRR1309 go-live (claims check #6) | Fixed | DE §6.3 $H_k$, §2.6, §8.6.4; FR-DE-072 | TC-FUN-122 fix is 05-testing's |
| GRD-057 | Yes — GD 3.3 §5.d: AS offers/responsibility ≤ the MW in the ERCOT-signed submission (text read) | Fixed | DE §6.5 C10, §7.3, §7.5; FR-DE-143 | — |
| GRD-058 | Yes — ICCP/EMS secondary paths commonly 4–15 s old | Fixed | DE §4.2 (A1 threshold per path from commissioning), §3.4; FR-DE-017, -158 | — |

## 2. Architecture and SRE review (ARC-)

| ID | Verified? | Disposition | Where | Note |
|---|---|---|---|---|
| ARC-002 | Yes — v0.1 §8.3 "components are arbitrated independently and in parallel"; §9.4 trace "partitions: 17"; §3.4 "8 partition shards" | Fixed | DE §8.3 (one fenced fleet allocator; hash-keyed execution shards), §8.1, §3.1; FR-DE-062 | Shard and routing tables are 01's (R30) |
| ARC-004 | Yes — v0.1 §8.14 "No verdict within 100 ms counts as a veto"; DM-07 "3 consecutive such cycles request a scoped safe stop" | Fixed | DE §8.13 DM-07a (VETO) / DM-07b (TIMEOUT holds, pages, never stops), §8.14 (V-35 250 ms per batch); FR-DE-092 | — |
| ARC-009 | Partly — cites FR-DE-084; the epoch source and equality check are 01/06's | Fixed | DE §8.15(a) (epoch equality, floors per issuer and shard, R32), §8.3 (two-phase handover); FR-DE-084 | 03 part only |
| ARC-012 | Yes — v0.1 §10.1–§10.3 assumed meter data that 02 does not define | Fixed | DE §10.1 (device-signed `MeterBlock`s with energy registers and `boot_id`; dedupe key), §4.1; FR-DE-105 | Entities are 02's (R33, R37) |
| ARC-013 | Yes — v0.1 S11 wrote traces and ledger updates asynchronously after publishing | Fixed | DE §8.1 S6 (ledger committed before any submission; single writer), §2.4; FR-DE-170 | — |
| ARC-016 | Yes — re-derived: detection 2 s + admission 5 s + stagger 30 s + R13 ramp 180 s + hub ramp 10 s = 227 s > the 120-s target (ARC range 190–257 s) | Fixed | DE §3.4 (240 s design, 300 s requirement, V-34), §4.2 (skew > 250 ms excluded), §8.6.1; FR-DE-013 | End-to-end table is 01's (R39) |
| ARC-018 | Yes — v0.1 §8.15 assigned `seq` in the dispatcher with no submission idempotency | Fixed | DE §8.15(a) (submission id, derived `command_id`, re-issue ≥ 2 s after ack timeout, stop classes exempt from rate limit); FR-DE-094 | — |
| ARC-019 | Partly — v0.1 S9 "Guardian-signed commands released to `device-gateway`" was ambiguous about who publishes | Fixed | DE §8.1 S10 (the guardian publishes per 01's sequence), sequence diagram | NATS permission table is 01/02's |
| ARC-024 | Yes — v0.1 §8.16.1: unreachable hubs stop "at TTL expiry or at the heartbeat gate (≤ 15 min)" | Fixed | DE §8.16.1 (retained scope stop, fallback only while no stop, stops exempt from rate limit), §8.14 (Safe-Stop Authority) | Escrowed guardian stops were not chosen by the register (R16) |
| ARC-025 | Partly — 03 §3.4 cadence was ambiguous; the demand model is 01's | Partly fixed | DE §3.1, §3.4, §4.1 (2-s cadence for events and on-line ADER members, V-03/V-32) | Per-service demand model belongs to 01/06 |
| ARC-026 | Yes — v0.1 FR-DE-084 re-partitioned within one cycle with no handover | Fixed | DE §8.3 (topology is data; no shard change), FR-DE-084 | — |
| ARC-041 | Yes — v0.1 DM-03 vs 05 CONSERVATIVE criteria differed | Fixed | DE §8.13 (DM → `05` fleet-mode mapping, one set of criteria); FR-DE-091 | — |
| ARC-043 | Yes — v0.1 S11 (trace) after S9 (publish) | Fixed | DE §8.1 S9 (pre-image before signing), §9.3; FR-DE-171 | — |
| ARC-044 | Yes — 01's full trace per tick vs 03's compaction; 03's volume re-derived: 360 + 430 = 790 MB/day at 10k hubs | Fixed | DE §9.3 (compaction rule stated for adoption by 01; one volume model for 02/06) | The change needed is in 01 |
| ARC-045 | Yes — v0.1 §9.3 anchored hourly | Fixed | DE §9.3 (60-s checkpoint, ≤ 5-min off-node anchor, V-23; incremental verification); FR-DE-100 | — |
| ARC-048 | Yes — FR-DE-093 hot standby impossible with one dispatcher on the node | Fixed | DE FR-DE-093 (one warm standby per shard group on the node, R35), §8.13 DM-08 | — |
| ARC-049 | Yes — v0.1 §11.3 applied an approved proposal once | Fixed | DE §11.3 (time-boxed, versioned constraint set; confirmation always); FR-DE-116 | — |
| ARC-054 | Yes — re-derived counts: 365 × 3 = 1,095 DA, 365 × 96 = 35,040 ID, 365 × 288 = 105,120 SCED solves | Fixed | DE §2.7 (risk-tiered activation gate, R10/R47); FR-DE-131, -134 | CI cost documented in 06 |
| ARC-055 | Partly — only register C-15 (`LARGE_LOAD` signal loss) is 03's | Partly fixed | DE §2.6, §8.6.3; FR-DE-071 | The rest of ARC-055 belongs to the register owner |
| ARC-056 | Yes — v0.1 §8.14 "the guardian reads a `fleet-state` replica" (common mode) | Fixed | DE §4.3, §8.14 (hub-reported values; separate estimator replica in production) | — |

## 3. Red-team report (RT-)

| ID | Verified? | Disposition | Where | Note |
|---|---|---|---|---|
| RT-002 | Partly — 03 §8.14 is cited; the remedy (Safe-Stop Authority, epoch authority) is 03-security's (R16) | Fixed | DE §8.14 (stops also signed by the SSA; epoch authority), §8.16 (SSA trigger; release never through SSA); FR-DE-096, -098 | 03 part only |
| RT-008 | Yes — the dispatcher authored its own trace; no independent withholding check | Fixed | DE §8.14 (withholding detector in `contracts-rt`; guardian co-signs the arbitration inputs hash), §9.2; FR-DE-172 | Tagged R2 (RT's demo gate does not require it) |

## 4. Judging and product review (JDG-)

| ID | Verified? | Disposition | Where | Note |
|---|---|---|---|---|
| JDG-004 | Yes — FR-DE-050 was Should; price of firmness had no consumer | Fixed | DE FR-DE-050 (Must), §6.10, §12.6; FR-DE-175 | The Insights view is 04-ui's |
| JDG-005 | Yes — no metric of what the orchestrator adds | Fixed | DE §12.1 (four policies incl. a port of `control_engine.py`), §12.4; FR-DE-169, -122 | Measures the software, not a business case (D0f) |
| JDG-007 | Yes — 03 §9.3 per-stream chains with an hourly root vs 01's global chain tip | Fixed | DE §9.3 (R22: per-stream chains, batch Merkle roots, 60-s checkpoints, 5-min anchor, local journal) | 01 must drop the chain tip |
| JDG-010 | Yes — the UI mock ("Non-Spin … Displaced $412") contradicts 03 §2.4 rule 2 | Fixed | DE §8.5 (AS ring-fenced in every example), §10.5 (buyback only for §7.4 releases and capability losses), §2.4 rule 2 | UI mock, E05-S06, FR-PLAN-008, FR-BILL-004 are other owners' |
| JDG-011 | No for 03 — v0.1 §1.3/§7.1 already separated 10:00 DAM offers and 14:00 declarations; the defect is in 04-ui | Fixed | DE §7.1 (both deadlines named explicitly) | UI-PLN-01/UI-OBL-04 fix is 04-ui's |
| JDG-012 | Yes — four lead-time targets across documents | Fixed | DE §8.11 (V-41 only, calibration plot), §12.4; FR-DE-086 | — |
| JDG-016 | Yes — 60 s × 1,440 min = 1,440 token requests a day (register §D) | Fixed | EXT §10 (own key; token reuse), §4.2, §7 (`REPLAY` mode), §11.1.4 D-1; FR-ING-178, -179, -149 | Applying the fix to the live simulators waits for the user (Q18) |
| JDG-017 | Yes — 0 MW of ADER approved in `LZ_AEN`/`LZ_CPS`; DSP (NOIE) consent per premise (claims check #15) | Fixed | DE §2.4, §12.1 (R27a partitions); FR-DE-152, -122. EXT §11.1.3 | — |
| JDG-021 | Partly — 03 §3.4 set 100 ms per batch; per-command OPA and inserts are 01's design | Fixed | DE §3.4, §8.14 (V-35; one OPA evaluation per batch; Merkle-batch signing; priority queues) | 03 part only |

## 5. Register items assigned to 03/04

| Item | Verified? | Disposition | Where | Note |
|---|---|---|---|---|
| R17 (ERCOT instructions, capability, COP, variants, settlement) | Register decision; ERCOT facts per claims checks #1–#4, #14, #15 | Fixed | DE §2.1, §2.3, §2.4, §2.6, §6.5 C22, §7.3, §7.5, §8.4, §8.5, §8.6.4, §8.6.5, §8.6.10, §8.11, §10.5 | "Failure counter" implemented as disqualification after two failures (claims check #3) |
| R18 (kVA/per-phase, add-back, recharge, performance basis, Example B closed loop) | Register decision | Fixed | DE §8.6.1, §8.5 Example B, §6.5 C7 | — |
| R19 (EEA posture, pre-positioning) | Register decision; NPRR1002 scope per claims check #7 | Fixed | DE §8.6.9, §6.5 C23, DM-14; EXT §11.10 | Policy, not compliance |
| R20 (TEEEF statute shape, `MOBILE_DER`, cold-load) | Register decision; SB 231 per claims check #5 | Fixed | DE §2.6, §6.7.7, §8.6.7 | — |
| R24 (FR-DE-050 Must, value of orchestration, V-41) | Register decision | Fixed | DE §6.10, §8.11, §12.1, §12.6 | — |
| R26 (freeze, volt-var capability, frequency-gated stops) | Register decision | Fixed | DE §8.2, §8.6, §8.8, §8.12, §8.16 | — |
| R27, R27a (tolling, territory roles, dual participation, SB 415, PJM, tariffs, cycle budget) | Register decision; claims checks #8–#11, #15 | Fixed | DE §2.4, §2.6, §6.3, §6.5 C18–C21, §8.6.2, §8.6.8, §12.1; EXT §3.2, §11.1.3, §11.11 | — |
| R28 (deferral controller details) | Register decision | Fixed | DE §4.2, §8.6.1, §8.6.6, §8.10, §8.12 | — |
| R30 (fleet allocator, shards, ledger before submission) | Register decision | Fixed | DE §8.1, §8.3, §8.4 | — |
| R31 (TIMEOUT ≠ VETO; V-35; hub-reported values) | Register decision | Fixed | DE §8.13, §8.14 | — |
| R39 / V-34 (240 s design, 300 s requirement) | Register decision; arithmetic in ARC-016 row | Fixed | DE §3.4, FR-DE-013, §12.4 | — |
| R42 (DM codes onto fleet modes) | Register decision | Fixed | DE §8.13 | — |
| R49 (AI constraint sets) | Register decision | Fixed | DE §11.3 | — |
| R3 amended, R4, R16, V-12…V-17 | Register decision | Fixed | DE §8.15(b), §8.16 | — |
| R22, R32, R33, R37, R40, R43 | Register decision | Fixed | DE §8.1, §8.15, §9.3, §10.1, §10.6, §4.1 | — |
| R10/R47, R13, V-27 | Register decision | Fixed | DE §2.7, §8.6.1 | — |
| R21 (build tags) | Register decision | Fixed | DE all 175 FR-DE rows (104 MVP-J, 49 MVP-B, 22 R2); EXT all 80 FR-ING rows | Sequencing only |
| R23 (`SHADOW`) | Register decision | Fixed | DE FR-DE-174 | — |
| V-07, V-18, V-20, V-22, V-23, V-29, V-30, V-33, V-38, V-41 | Register values | Fixed | DE §6.8, §8.8, §8.11, §8.12, §8.15, §9.3, §11.2, §11.4 | — |
| Q7 (AS durations) | Claims check #6 resolves: ECRS 1 h, Non-Spin 4 h (2 h at NPRR1309) | Fixed | DE §6.3, §2.6 | The user may confirm; profile fields |
| Q18 (ERCOT key, token reuse, host clock) | Register question | User decision Q18 | EXT §10, §14.2; FR-ING-178 | Default implemented: separate key |
| Register C-15 (`LARGE_LOAD` signal loss) | Register assigns it to 03 | Fixed | DE §8.6.3, FR-DE-071 | — |

## 6. Disposition counts (findings only)

| Review | Rows | Fixed | Partly fixed | Rejected | Deferred R2 | User decision |
|---|---|---|---|---|---|---|
| GRD | 52 | 51 | 1 (GRD-020) | 0 | 0 | 0 |
| ARC | 20 | 18 | 2 (ARC-025, ARC-055) | 0 | 0 | 0 |
| RT | 2 | 2 | 0 | 0 | 0 | 0 |
| JDG | 9 | 9 | 0 | 0 | 0 | 0 |
| **Total** | **83** | **80** | **3** | **0** | **0** | **0** |

Several fixes carry the build tag `R2` (e.g. FR-DE-140 NCLR, -153 SB 415, -155 cycle budget, -166 external overlap, -168
CVaR release, -172 withholding detector): the design is complete now and built after the judged demo — sequencing, not a
deferral of the resolution.

## 7. New IDs

- **03:** FR-DE-136 … FR-DE-175 (40 requirements); DM-07a/DM-07b (split of DM-07), DM-14 … DM-16; A-DE-41 … A-DE-56
  (A-DE-28 withdrawn into A-DE-51); request kind `SCHEDULE`; control blocks `ISO_NPC_REGULATION`, `ISO_XML_DEPLOYMENT`,
  `SCHEDULED_NPC`, `TOLLING_SCHEDULE`, `SELF_SERVE_NET_LOAD`, `ISLAND_FORMING`; performance blocks `SET_POINT_TRACKING`,
  `NCLR_DEPLOYMENT_BAND`, `OUTCOME_LOADING`, `AVAILABILITY`; M&V blocks `AMI_INTERVAL`, `CBL_NET_OF_BATTERY`,
  `METER_BEFORE_METER_AFTER`, `SCADA_OUTCOME`; variants `ALR`/`NCLR`, `EVENT`/`TOLLING`, `COOP`/`TDU_SB415`,
  `TEEEF`/`MOBILE_DER`; constraints C18 … C23; reason codes `R-ISO-VISIBLE`, `R-ISO-INSTRUCTION`, `R-FREEZE-FREQ`,
  `R-EEA-POSTURE`; forecast product F-NETPEAK.
- **04:** sources S10 (ERCOT operating notices), S11 (per-utility tariff tables); counterparty G9 (frequency and grid
  conditions); FR-ING-174 … FR-ING-180; A-ING-19 … A-ING-23; subjects `ops.ercot.notices.v1`, `ops.ercot.eea.v1`,
  `ref.tariff.v1`; record fields `settlement_role`, `firm_fitness`.

## 8. Changed expected results (for test and demo owners)

| Item | v0.1 | v0.2 |
|---|---|---|
| Example A (FR-DE-063 oracle) | T1 8,400; AS 1,000; energy base point squeezed to 100 kW; pipeline 158 of 250 kW (naive 0) | T1 8,400 (G1 6,490 + reserved ADER share 1,508 + G3 402); Non-Spin followed on the UDSP from its ring-fence; pipeline **250 of 250 kW** (naive 0); price of firmness ≈ $302 per 5-min interval; 17:31 disturbance → T1 8,163 kW (102%), pipeline 0, ADER untouched |
| Example B | 665 / 302 kW static | `SHARE` 665 / 302 kW in closed loop; `OUTCOME` 200 / 500 kW, bank at 7,900 kVA, $0/h; v0.1 law decays to ≈ 489 kW |
| Example C | 355 kW re-homed, $0.36 / $90 | Unchanged for ALR; NCLR deployed variant scores 89–94% → a counted failure |
| Firm full output | p99 ≤ 120 s | p99 ≤ 240 s design, ≤ 300 s requirement (V-34) |
| Guardian budget | 100 ms, timeout = veto → stop | 250 ms per batch of ≤ 2,000; timeout holds and pages (V-35) |
| ECRS hold | 2 h default | 1 h (V-33, claims check #6) |
| Breach lead time | Several targets | Median ≥ 60 min, P10 ≥ 15 min, calibration plot (V-41) |

## 9. Unresolved cross-document issues (owners other than 03/04)

1. **03-security §6.3 guardian checks.** G-01 compares the program export limit with hub output |P|; it must apply to
   net export at the meter (P − L) as the one distribution-defaults source does (DE §8.10), or Example A's 11 kW hubs are
   vetoed. G-02/G-03 must use the kVA and add-back formulas of DE §8.6.1 (GRD-003, -007); G-04/G-05 the V-30 ramp table
   (GRD-012, -013); G-12 should apply only while a switching order is open or inference disagrees (GRD-027).
2. **05-failure-modes.** FM §2.12.2 still pre-empts ERCOT dispatch at AT_RISK/BREACH_IMMINENT (GRD-001); RP-59's EEA
   reserve raise (GRD-004) must follow R19; FM-MKT-011 must match DM-10 (GRD-014); fleet modes should absorb DM-14…DM-16.
3. **05-testing.** TC-FUN-114, -129, -202 encode the squeezed base point (GRD-001); TC-FUN-141 (kVA), -146 (recharge
   add-back), -179 (cold-load: 800 kW is 2,240 kW at the plateau), -122 (ECRS 1 h), -265 (ramp table), TC-E2E-008
   (TEEEF grid-parallel), and the Example A–C oracles of §8 above; new cases listed in DE §12.3.
4. **01-product / vision storyline and the JDG script.** Beat 3's "energy squeezed to 100 kW; pipeline 158 of 250 kW"
   and beat 2's "$1,680 per 5-min interval" must follow Example A v0.2; FR-DISP-012 must restrict price response to
   off-line or unregistered premises; FR-DISP-003 wording (GRD-060); FR-SAFE-007/008 single-person engage (GRD-010).
5. **04-ui.** The AS "Displaced $412" mock (JDG-010), the 10:00/14:00 markers (JDG-011), the Insights view fed by DE §12.6
   (JDG-004), and the "what ERCOT sees" / EEA board views (GRD-046).
6. **02-domain-model.** `IsoInstruction`, `CurrentOperatingPlan`, `Reservation` (single writer: fleet allocator), unit-typed
   ratings replacing `Bank.rated_kw`, `phase` on ServiceTransformer/ServicePoint, the AS award schema without `hold_hours`
   and `price_per_kw_yr` (GRD-039), the tolled-share SOC sub-ledger, the battery-exchange meter channel, and the command
   envelope names (`pre`, `exp`) used in DE §8.15.
7. **01-system-architecture.** The shard and call-routing tables (R30), the end-to-end latency table matching DE §3.4
   (V-34), adoption of the trace compaction rule (ARC-044), the guardian as publisher of signed commands (ARC-019), and the
   removal of the global chain tip (JDG-007).
8. **07-scada.** Already on R17; to align with DE §7.5: ADER MPC/LPC use non-ERCOT *reservations* (not instantaneous
   requests) and a regulation margin $h_v$ on both edges; the telemetered ramp is controller-limited (A-DE-44, to confirm
   with ERCOT); PFR capability declared where hub droop is enabled (R26).
9. **06-platform.** Size telemetry for 2-s reports from every member of an on-line ADER (100,000 hubs × 0.5 Hz = 50,000
   msg/s, GRD-022) and document the CI cost of the full-year replay gate (R47).
10. **Register v0.2 text.** V-33 can now read "ECRS 1 h; Non-Spin 4 h (2 h at NPRR1309 go-live)" (claims check #6); R17's
    NCLR "failure counter" should say two failures in 365 days disqualify (claims check #3); R20 cites the stale
    texas.public.law text of §39.918 — SB 231 adds the mobile/12-h/5-MW limits (claims check #5); R27's SB 415 reference
    is PURA §35.153 (claims check #10).

## 10. Reviewer claims found wrong or overstated

- **GRD-017:** "two failures in 365 days can suspend the resource" understates the rule — Protocols §8.1.1.4.3(5) says
  disqualification; and the GD's NCLR baseline is the full 15-min interval before the instruction, not a 5-min average
  (claims check #3).
- **GRD-008:** the ≈ 530 kW decay holds only if the large load stays at 302 kW; with per-cycle re-arbitration the v0.1 law
  decays further, to ≈ 483–489 kW. Direction right, number optimistic.
- **GRD-003:** the "≈ 45%" under-relief compares 600 kW (with margin) to 1,084 kW (without); like-for-like it is ≈ 50%.
  Conservative, not wrong.
- **GRD-004:** NPRR1002's charging suspension binds registered ESRs, not an ADER Load-Resource fleet (claims check #7); the
  posture stands as policy.
- **GRD-048:** the storage-contract statute is PURA §35.153, and 16 TAC §25.58 is still a proposed rule (claims check #10).
- **GRD-022:** the 4-min UDSP base ramp is an ERCOT training value, not a Protocol value (claims check #14).
- **GRD-049:** "residential" is not in ComEd's PLC text; the PLC adds back PJM Load Drop Estimates (claims check #11).
- **GRD-034:** the 0.1–0.3 shift-factor range is unverified; kept as a labelled reviewer claim and made a profile parameter.
- **JDG-011:** 03 already separated 10:00 DAM offers from 14:00 declarations; the defect is only in 04-ui.
