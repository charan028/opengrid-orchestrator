# A6 — SCADA integration (`02-architecture/07-scada-integration.md`): dispositions of the review findings

Status: v1.0 · 2026-09-25 · Owner: SCADA integration lead (owner of `07-scada-integration.md`, now v0.3) · Inputs: the four
adversarial reviews (`06-reviews/01…04`), the primary-source claims check (`06-reviews/05-claims-verification.md`) and
`00-decision-register.md` v0.2, which wins wherever it speaks.

**Method.** Every finding that cites `07` (or "SC"), and every finding or register item assigned to `07` by the
resolution brief, was treated as a claim to be proven. Each was checked against the v0.2 text of `07` (quoted location)
and, for a rule claim, against the primary text: ADER Governing Document 3.3 (local text of the ERCOT `.docx`), ERCOT
Nodal Protocols Sections 3, 6, 8 and 16 (local text of the ERCOT `.docx` files; Section 16 downloaded and parsed in this
pass), the RTC+B Load Resource and Telemetry decks (local PDF text), the ICCP Handbook v4.07, PURA §39.918 and its SB 231
amendment (claims check claim 5), the NREL IEEE 1547-2018 highlights and the MISO IEEE 1547 guideline (local PDF text),
and the Step Function and pydnp3/opendnp3 sources (claims check claim 12 plus two direct reads). No WebFetch summary was
used as evidence for a number. Nothing was dropped (D0a); no existing ID was renumbered (the new test IDs were moved to a
free range before any document used them, §4).

Columns: **Verified?** Yes / Partly / No, with the evidence · **Disposition** Fixed / Partly fixed / Rejected / Deferred R2
/ User decision Qn / No change needed · **Where** the sections of `07` v0.3 · **Note** what remains for another owner.
"Fixed (07 part)" means everything the finding asks of `07` is done and the rest belongs to the named documents.

## 1. Findings citing `07`

### 1.1 Grid, ERCOT market and SCADA review (GRD)

| ID | Verified? | Disposition | Where | Note |
|---|---|---|---|---|
| GRD-001 | Yes — 07 v0.2 §5.7 bound base points to a T3 `POWER_PROFILE` call and AS to a T2 hold plus a deployment on `NDPL`; claims check claim 1 confirms that CLR online Non-Spin/ECRS are delivered inside the base point and UDSP (Protocols §6.5.7.6.2.4(6), §6.5.7.6.2.3(7), §6.5.7.4.1(3); RTC+B LR deck slides 8, 12) | Fixed (07 part) | §5.1 (ISO instruction object), §5.2, §5.7 (ALR variant), §6.2 rule 8, §3.2.4 bindings, §6.1 | `03` owns the NPC regulator and must remove the T2/T3 split, Example A variant and price response on on-line ADER members; H-SCADA-41 |
| GRD-002 | Yes — 07 v0.2 §2.3 computed MPC/LPC from $P^{cap}_i$ (physical after L0–L2) and FR-SCADA-005 reduced telemetry only for L2 blocks; claims check claim 2: proxy AS offers for every qualified Resource every SCED run, MW = MPC for Load Resources (Protocols §6.5.7.3(5), (12)) — broader than the reviewer said | Fixed | §2.3 (ERCOT-visible capability, four invariants), §1.3.1, §3.2.3, FR-SCADA-005, -016, -093, FM-SCADA-053, TC-INT-769/770, H-SCADA-43/44 | Guardian's independent invariant check is for `03-security` (H-SCADA-44) |
| GRD-003 | Yes — 07 v0.2 §4.2 "`BANK_P` is the measured load $M_b$", ratings in kVA, and §4.11 $R_b$ = min(kVA ratings, kW `BANK_LIMIT`) | Fixed (07 part) | §2.8 unit typing, §3.0 `quantity`/`phase`, §3.1.8 AO 5, §4.2 regulated quantity, §4.11 inputs (worked 8,500 kW/3,000 kVAr check reproduced: 1,084 kW), FR-SCADA-099, FM-SCADA-058, TC-INT-776 | The control law is `03` §8.6.1; `02` adds unit-typed ratings |
| GRD-005 | Yes — statute text (§39.918(c), (d)(1)–(2)); claims check claim 5 adds SB 231 (mobile, < 12 h to move, ≤ 5 MW, bidding, PUCT authorization) and that co-ops and munis are not TDUs; 07 v0.2 §1.3.2 omitted the isolation clause and §3.1.11 carried grid-following points | Fixed (07 part) | §1.3.2, §2.1 (`UNIT` never in an ADER), §3.1.11 (statute-shaped, `MOBILE_DER` variant), §5.2, §5.6, FR-SCADA-025, -052, -104, TC-INT-780 | The profile itself is `03` §2.6; the billing line is under legal review |
| GRD-006 | Yes — 07 v0.2 ingested `BANK_I_A/B/C` and never used them; no `phase` anywhere | Fixed (07 part) | §2.2 `phase`, §4.2, §4.11 per-phase need, §4.12 per-phase step check, AI 57–58, FR-SCADA-099, H-SCADA-48 | `02` adds `phase` to service points and transformers |
| GRD-007 | Yes (for the 07 inputs) — 07 v0.2 supplied fleet output only as discharge; the recharge formula is `03`'s | Fixed (07 part) | §4.11 (signed fleet contribution gives the add-back headroom), §5.3 | Formula in `03` and in the guardian (G-03) |
| GRD-008 | Partly — the PI defect is `03`'s; 07 v0.2 §5.3 stated one performance definition | Fixed (07 part) | §5.3 completion and performance: outcome-based or share-based per contract (R18, Q9) | Example B re-run in `03` |
| GRD-009 | Partly — 07 v0.2 `PFRC` = 0 "unless qualified" matches the GD (PFR requested, not required, GD §5.c, §6); the cancelling integrators are in `03`/`05`; 07 lacked reason codes, autonomous ΔP and a freeze of its closed loops | Fixed (07 part) | §2.3, AI 50–51, §3.2.3 `PFRC`, §4.11 autonomous-response hold, §4.12 exclusion; UDSPs exclude expected PFR (Protocols §6.5.7.4.1(1)) | Trust penalties and substitution freezes are `03`/`05` |
| GRD-010 | Yes (07 v0.2 §6.4/§6.5 made zone and fleet engage Tier 2) | Fixed | §3.1.7 "Engage (R3)", §6.4, §6.5, §6.11, FR-SCADA-058 | Register R3 amended; UI in `04-ui` |
| GRD-011 | Yes — every stop path in 07 v0.2, including CROB 10, traversed `scada-gateway` and a signer | Fixed (07 part) | §6.13 (CSIP or permit-service path), DBI 4, FR-SCADA-102, FM-SCADA-059, TC-INT-778, H-SCADA-47 | Needs hub firmware (Q2); built later; V-07 autonomy is `02`/`05` |
| GRD-012 | Partly — the ramp table is the guardian's (V-30); 07's part is the telemetered ramp | Fixed (07 part) | §2.3 ramp rows capped at the guardian-permitted share | `03-security` owns the table |
| GRD-013 | Yes — 07 v0.2 §2.3 ramps were physical; ECRS capability = 10-min change ÷ 10 (telemetry deck slide 4); HDL/LDL = telemetered power ± 5 × normal ramp rate (Protocols §6.5.7.2(6)–(7), claims check 14) | Fixed | §2.3 invariant 2, §3.2.3 NURR/NDRR/EURR/EDRR/ECRR/NSRR, FR-SCADA-093, TC-INT-770 | — |
| GRD-014 | Yes — 07 v0.2 §3.2.4, §5.7, FM-SCADA-009/-030 "hold the last base point ≤ 1 interval, then the energy call → 0 kW" | Fixed (07 part) | §3.2.11 normative text, §3.2.4, §5.7, §6.8, FM-SCADA-009/-030, FR-SCADA-096, TC-INT-773 | Already carried by `03` (DM-10) and `05` (FM-MKT-011, FM-SCADA-009/-030, runbook RB-071) at the time of writing |
| GRD-015 | Yes (claims check claim 9: the AE agreement gives AE control of charge/discharge timing) | Fixed (07 part) | §5.2, §5.4 `TOLLING` variant binding; §2.3 held headroom | Profile variant in `03`, contract model in `contracts` |
| GRD-016 | Yes (claims check claim 4: one Load Zone, one LSE, one DSP per ALR-ADER; 0 MW approved in LZ_AEN/LZ_CPS) | Fixed (07 part) | §2.2 item 2, §5.7 note (NOIE consent) | Role model in `contracts` (R27) |
| GRD-017 | Partly — XML deployment and hold-until-recall confirmed; claims check claim 3: the GD's MBMA baseline is the 15-min interval before the instruction (not a 5-min average) and two failures mean disqualification (not suspension) | Fixed | §1.3.1 performance row, §3.2.4, §5.7 NCLR variant, FR-SCADA-095, FM-SCADA-055, TC-INT-772 | Which baseline ERCOT applies is confirmed with ERCOT (§13.2) |
| GRD-018 | Partly — the cold-load plan is `03`'s; 07's readiness interlock ignored it | Fixed (07 part) | §5.6 readiness includes the pickup-block check | `03` §8.6.7, TC-FUN-179 |
| GRD-019 | Yes — 07 v0.2 §3.1.11 CROB 3 `BREAKER_CLOSE_PERMISSIVE` "Tier 2" for Base | Fixed | §3.1.11 (BI 11 `READY_TO_ENERGIZE`, CROB 3 `BREAKER_CLOSE` lessee-only with AO 7 switching-order ID, BI 21, BI 22, CROB 7), §5.6, §6.3, §6.4, §6.11, FR-SCADA-052, FM-SCADA-061 | Indexes kept, names changed |
| GRD-020 | Yes — nothing in 07 v0.2 verified the attested settings | Fixed (07 part) | §1.3.2, §2.2, §6.14, AI 52, FR-SCADA-103, FM-SCADA-060, TC-INT-779 | Read-back needs firmware (Q2); rollout gate in `05` |
| GRD-021 | Partly — no ISO-instruction type or QSE desk in 07 v0.2; Protocols §16.2.1(n) (verified in this pass) requires the 24×7 center of QSEs that are WAN Participants, narrower than "a QSE representing resources" | Fixed (07 part) | §3.2.10, §6.11 (QSE desk role), FR-SCADA-097, FM-SCADA-064, H-SCADA-45/49 | Role code in `03-security` §5.1 (V-37); staffing is Q15 |
| GRD-022 | Partly — 07 v0.2 required 2-s reporting in AS intervals and events but not for on-line ADER members; UDSP every 4 s is in the Protocols, the 4-min base ramp only in a training deck (claims check 14) | Fixed | §2.7, §1.3.1 UDSP row, §3.2.4, §7.5 throughput, FR-SCADA-094, H-SCADA-27 | Throughput (50,000 msg/s at 100k hubs) is an input to `06` and `02` |
| GRD-023 | Yes (Protocols §3.9.1(1)–(3) verified) | Fixed (07 part) | §3.2.12, §1.3.1 COP row, FR-SCADA-098, FM-SCADA-056, TC-INT-775, H-SCADA-42 | Entity in `02`; production in `planner` |
| GRD-024 | Partly — 07 v0.2 §2.1 applied the old Q6 default; claims check claim 8: GVEC's ADER qualification confirmed, concurrent use after qualification not confirmed | User decision Q6 (new default applied) | §2.1, §13.1 | — |
| GRD-025 | Yes — 07 v0.2 §6.5 ramped stops without ERCOT sequencing or frequency gating | Fixed | §6.5 (protective vs non-protective, V-16), release V-17, §6.2, FR-SCADA-060, TC-INT-713 | Values unsigned (Q13) |
| GRD-026 | Partly — 07 v0.2 §4.8 downgraded a frozen value only to UNCERTAIN (the A3 quote is `03`'s), but ignored deadbands and correlated signals, and its step rule treated field switching as bad data | Fixed | §3.0 `source_deadband`, §4.3, §4.8 rules 2–3, FM-SCADA-002/-011/-026, FR-SCADA-035, TC-INT-730 | `03` A-DE-07 aligns |
| GRD-027 | Yes — 07 v0.2 §2.2/§4.13 relied on SCADA switch status only | Fixed (07 part) | §2.2, §4.1 OMS row, §4.13, FR-SCADA-040, -105, FM-SCADA-063, TC-INT-781, H-SCADA-50 | G-12 wording in `03-security` |
| GRD-028 | Yes — 07 v0.2 §6.2 covered only the orchestrator's own loops | Fixed (07 part) | §5.3 intake (one integrating loop), §4.11, FR-SCADA-111, TC-INT-784 | Oscillation attribution in `03` |
| GRD-029 | Yes — `LTC_TAP` ingested and unused in 07 v0.2 | Fixed (07 part) | §4.2 LTC counting, AI 56, counter 2, FR-SCADA-106, TC-INT-782 | Reversal cap in `03` |
| GRD-031 | Partly — capability is `03`'s | Fixed (07 part) | §2.3 guardian-permitted range includes apparent-power headroom with volt-var priority | `03` §8.2 |
| GRD-034 | Yes — the quoted "1 A ≈ 234 kW" is `03`'s, but 07 v0.2 §5.5 used the same radial $P=\sqrt3 V\Delta I PF$ | Fixed (07 part) | §4.5 sensitivity $k_{line}$, §5.5, FR-SCADA-042, TC-INT-735 | Controller in `03` §8.6.6 |
| GRD-036 | Yes — 07 v0.2 §1.3.6 said "revenue-grade" without a certification basis | Fixed (07 part) | §1.3.6, §4.4, §5.3 M&V inputs | M&V defaults in `03` §10 |
| GRD-039 | Partly — the schema is `02`'s; 07 v0.2 bound awards to 300-s updates | Fixed (07 part) | §3.2.4 awards after every SCED run; DAM awards hourly | `02` §4.3 schema |
| GRD-041 | Yes — 07 v0.2 §6.4 Tier 1 included "any change to a customer's declared capacity" | Fixed (07 part) | §6.4 automatic row, AI 31, §1.3.6, H-SCADA-21 | Register R3 amended |
| GRD-042 | Yes — GD §5.c–§5.e (local text) and claims check claim 4 | User decision Q12 (rule documented, default off) | §1.3.1, §2.9, §7.8, FR-SCADA-110, TC-SEC-717 | A real ADER cannot operate before Q12 is answered yes |
| GRD-046 | Partly — the views are `04-ui`'s; 07 supplies their data | Fixed (07 part) | §3.2.10, H-SCADA-49 | `04-ui` specifies the views |
| GRD-048 | Yes (claims check claim 10: PURA §35.153, not §35.152; §25.58 still proposed) | Fixed (07 part) | §1.3.2, §5.3 (TDU variant discharges only on the TDU's direction) | Variant in `03` and `contracts` |
| GRD-050 | Yes — 07 v0.2 option A named ERCOT WAN nodes without the physical sites they need | User decision Q6 (both options documented) | §3.2.1 prerequisites of A and SLA of B, §13.1 | `06` adds sites if A is chosen |
| GRD-051 | Yes — `03` required 3 samples within 0.1·R, 07 v0.2 60 s continuous | Fixed | §4.11 (V-38; HOLD = max(held, scheduled)), FR-SCADA-038, FM-SCADA-001, TC-INT-729 | `03` §8.6.1 aligns |
| GRD-052 | Yes (claims check claim 13) — the defect is in `05`; 07 §3.1.12 already had IIN2.3 overflow and IIN1.7 restart | No change needed | §3.1.12 | `05` FM-SCADA-006 now reads IIN2.3 (corrected by its owner) |
| GRD-053 | Yes — 07's per-point SBO/DO column vs FR-SCAD-011 "rejected everywhere" | Fixed (07 part) | §3.1.7 note (column normative), FR-SCADA-022, TC-INT-706 | `01-product/02` FR-SCAD-011 already aligned; `05-testing` TC-INT-706 still refuses DO everywhere but the e-stop |
| GRD-054 | Partly — 07 v0.2 already made the check conditional on `SEQ_REQUIRED` but did not default it off on SA associations and presented it as the D4a protection | Fixed | §3.1.8 AO 6, §6.3, §3.1.1, FR-SCADA-022, -055 | Register R29 |
| GRD-055 | Yes — 07 v0.2 offline at 120 s vs 180 s (`05`) and 60 min (`02`) | Fixed | §2.4 (V-29), AI 17–19, 53–55, FR-SCADA-010 | `02` §2.1 owns the table and adds `LATE` for the band between 2 × cadence and 3 missed reports |
| GRD-056 | Yes (claims check claim 6) — 07 already used ECRS 1 h | No change needed | §1.3.1 adds Non-Spin 2 h pending NPRR1309 | `03`, TC-FUN-122 |
| GRD-057 | Yes (GD §5.d last paragraph, local text) | Fixed (07 part) | §1.3.1, §2.3 AS capability capped at qualified MW | Offer validation in `03` C10 |
| GRD-058 | Yes — 07 v0.2 inherited a single 10-s A1 bound | Fixed (07 part) | §3.0 `a1_max_s`, §4.9, §4.10, FR-SCADA-107, TC-INT-783 | `03` §4.2 already takes the bound per path |
| GRD-059 | Yes — 07 v0.2 made DNP3 RTU polling the primary path | Fixed | §1.1, §4.1, §4.2 (exception path), §4.3, §4.9, FR-SCADA-033 | The demo still polls the `grid-sim` RTU (DNP3 is the MVP-J protocol) |

### 1.2 Architecture and SRE review (ARC)

| ID | Verified? | Disposition | Where | Note |
|---|---|---|---|---|
| ARC-003 | Partly — the global chain tip is `01`'s design; 07 v0.2 §6.10 already chained per stream but did not name its stream or say what happens when the audit store is down | Fixed (07 part) | §6.10 (per-stream chain, JCS header, local journal), FR-SCADA-065, H-SCADA-32 | `01` removes the global tip (R22) |
| ARC-005 | Yes — 07 v0.2 §7.5 "Core 0.5 vCPU / 512 MiB + 0.25 vCPU / 256 MiB per adapter" with active + standby = 1,280–2,560 MiB against 160 MiB in `06` | Fixed (07 part) | §7.3 (one adapter per link on the node, standby only in production), §7.5 (resources only in `06` §1.8; planning inputs), ADR-071, A-SC-20 | `06` §1.8 caps 384 MiB core + 128 MiB per adapter (MB-02) and keeps 07's v0.2 estimate (512 + 256 MiB) only as its upper-bound scenario, as 07 §7.5 now states |
| ARC-006 | Yes — 07 v0.2 §7.10 used its own subjects (`scada.meas.*`, `calls.in.scada`, `guardian.constraints`) | Fixed (07 part) | §7.10 now uses the names of `02` §5 (`SCADA`, `INTAKE`, `CAPABILITY`, `AUDIT` streams; `guard.ctl.scada`; KV `scada-state`), §7.6 | `02` §5.6 maps the earlier 07 names; `06` sizes the streams |
| ARC-008 | Yes — 07 v0.2 §6.8 kept latched restrictions in `guardian` and §7.3 in NATS KV | Fixed (07 part) | §7.3, §6.8, H-SCADA-14 (latched states in one PostgreSQL table read by the guardian's interlock service) | Guardian HA ADR in `01` (R31) |
| ARC-010 | Yes — 07 v0.2 §7.3 kept latched states in a NATS KV bucket | Fixed | §7.3, §6.3 (sequence resync), ADR-076 amended, FR-SCADA-108, FM-SCADA-065, TC-INT-785 | DR case in `05-testing` |
| ARC-014 | Yes — 07 v0.2 §2.2 relied on "contracts eligibility" with no Enrollment entity | Fixed (07 part) | §2.1, §2.2 (`Enrollment`, versioned VR membership) | `02` defines the entities (R37) |
| ARC-015 | Yes — 07 v0.2 §2.4 used 120 s | Fixed | §2.4 (V-29) | `02` owns the table (R40) |
| ARC-016 | Yes — 07 v0.2 §2.7/§6.7 design p99 ≤ 120 s; alignment by hub clock | Fixed (07 part) | §2.7, §4.10, §4.11, §6.7, §7.7, FR-SCADA-037 (V-34, R39: 240 s; receipt-time alignment; skew > 250 ms excluded) | `01` owns the end-to-end table |
| ARC-019 | Partly — the SCADA pre-check is a read-only query, not a command submission path; the contradiction lies in `01`/`02`/`05`/`06` | Fixed (07 part) | §6.6 (sequence and note follow `01` §6.11 and §8.1: `call.in.scada`, `sub.<class>.<shard>`, `cmd.<shard>.<hub_id>` by `guardian` only, `guard.ctl.scada`), §7.10, H-SCADA-12 | — |
| ARC-026 | Partly — VR membership must change on switching (correct behaviour); the defect is subjects and epochs keyed on topology partitions | Fixed (07 part) | §2.2, §4.13 (topology is data; shards keyed by hub hash) | `01`/`02`/`03` (R30) |
| ARC-027 | Yes — ADR-071…078 were not ratified anywhere | Fixed (07 part) | §0.4, §7.12 (all marked proposed; ADR-072, -073, -076 amended) | Verdicts recorded in `01` §17.2 (071 ratified with amendment; 072 and 076 in part; the rest ratified) |
| ARC-034 | Yes — 07 v0.2 §12 said both "TLS + SA on every simulated link" and "TLS-only exception"; §13.2 item 7 said the vision excluded TEEEF and pipeline dispatch, which `01-vision` §4.1 no longer does (checked) | Fixed | §7.2 (spike), §3.2.9 (ICCP `SIM`), §12 (one profile per link), §13.2 item 7, ADR-072/-073, FR-SCADA-109 | Licences stay register Q11 |
| ARC-037 | Yes — 07 v0.2 had no priority class for SCADA pre-checks | Fixed (07 part) | §6.6 steps 7 and 10 (UTILITY and SAFE_STOP classes, R31) | Guardian queues in `03-security` |
| ARC-042 | Yes — 07 v0.2 step 4 called `contracts`, which also runs month-end batches | Fixed (07 part) | §6.6 step 4, §7.1, H-SCADA-16 (`contracts-rt`, R43) | Split in `01`/`06` |

### 1.3 Red team (RT)

| ID | Verified? | Disposition | Where | Note |
|---|---|---|---|---|
| RT-001 | Yes — 07 v0.2 §6.8 row "guardian unavailable": restrictive controls "accepted as queued" until `guardian` recovers; FR-SCADA-057 the same | Fixed | §6.5 independence, §6.8 (Safe-Stop Authority executes the smallest containing stop scope; both signers down → `DOWNSTREAM_FAIL`), §3.1.12, FR-SCADA-057, FM-SCADA-036, TC-INT-712, H-SCADA-46 | `03-security/02` §4.4, §6.5, §6.9 and §19.2 already list the gateway as a caller of the SSA while `guardian` is unavailable (and §6.5 supersedes 07 v0.2's "accepted as queued"); the register's R16 text names only the other triggers (§5 below) |
| RT-003 | Yes — 07 v0.2 AO 5 `BANK_LIMIT` and the bank measurement could come from the same master with no corroboration | Fixed (07 part) | §3.1.8 AO 5, §4.15 (CTL-150 rules 1–3), FR-SCADA-100, FM-SCADA-057, TC-INT-777, E2E-15 | CTL-150 text in `03-security` (G-18, DET-084); 07's bound aligned to G-18's 10% of the bank rating per 5 min [A] (A-SC-16) |
| RT-005 | Partly — the finding targets G-07/G-08 in `03-security`; 07's closed loops used hub frequency implicitly | Fixed (07 part) | §4.11 (hold judged on the utility's `FREQUENCY` point or an ERCOT feed; hub median corroboration only) | CTL-151 in `03-security` |
| RT-009 | Yes — 07 v0.2 §8.2 allowed a TLS-only exception without limiting it to `grid-sim` | Fixed | §3.1.1, §8.2 hard gate, §3.1.12, FR-SCADA-101, FM-SCADA-062, TC-SEC-716 | PJM Jetstream and ERCOT ICCP need association-specific exceptions (§5 below) |
| RT-014 | Yes (for 07's cross-checks) — 07 v0.2 treated the fleet sum as always available | Fixed (07 part) | §4.15 rule 4 (correlated silence degrades the fleet-sum source) | Trust scoring in `03-security` (CTL-050/052) |

### 1.4 Judging and product review (JDG)

| ID | Verified? | Disposition | Where | Note |
|---|---|---|---|---|
| JDG-008 | Partly — confirmed: Step Function `dnp3` has no Python binding and a non-commercial, non-production licence; opendnp3's wrapper exposes TLS but the default build lacks it (claims check 12 and direct reads); **not** confirmed: "a non-Python sidecar breaks ADR-001" — ADR-071 already runs every stack as its own adapter process | Fixed | §7.2 (week-1 spike, five pass criteria), §12 (MVP-J = DNP3 over TLS: one outstation association and one master poll), FR tags, ADR-072, FR-SCADA-109 | Stack licence remains register Q11 |
| JDG-017 | Yes (claims check claim 15: 0 MW approved in LZ_AEN/LZ_CPS) | Fixed (07 part) | §5.7 note (ALR on a competitive-area partition, R27a) | Territory model in `03` and `01-product` |
| JDG-018 | Yes (for "DNP3 on the wire") | Fixed (07 part) | §7.2 pass criterion 5 (packet capture in the evidence pack), §12 | Evidence pack in `05-testing` |

## 2. Register items assigned to `07`

| ID | Verified? | Disposition | Where | Note |
|---|---|---|---|---|
| R3 (amended) | Register decision; 07 v0.2 used the pre-amendment tiers | Fixed | §3.1.7–§3.1.8 tier columns, §6.4, §6.5, §6.11, FR-SCADA-058 | Q1 still open (co-signer per scope) |
| R4, V-16, V-17 | Register decision | Fixed | §6.5 (one signed broadcast per scope on the retained topic; protective and non-protective stops; release), FR-SCADA-060 | Ramps unsigned (Q13) |
| R5 | Already applied in 07 v0.2 (§4.8, §4.11) | No change needed | §4.8, §4.11 | — |
| R6 | Already applied (§0.1, §3.4) | No change needed | §0.1, §3.4 | — |
| R16, V-11 | Register decision; RT-001 verified | Fixed | §0.1, §0.3, §1.2, §6.1, §6.5, §6.8, §7.1, §7.10, FR-SCADA-057, H-SCADA-46 | Gateway trigger listed in `03-security/02` §4.4, §6.5, §6.9, §19.2 (§5) |
| R17 | Register decision; ERCOT facts verified (claims check 1–3, 14; Protocols §3.9.1, §6.5.7.3, §6.5.7.4.1) | Fixed | §0.3, §2.3, §3.2.3–§3.2.12, §5.1, §5.7, §6.2, FR-SCADA-093…098 | MPC − LPC interpretation to confirm with ERCOT (§5) |
| R18 | Register decision; GRD-003/-006 verified | Fixed | §2.2, §2.8, §3.0, §3.1.8 AO 5, §4.2, §4.11, §5.3, FR-SCADA-099 | — |
| R20 | Register decision; statute verified | Fixed | §1.1, §1.3.2, §2.1, §3.1.10–§3.1.11, §4.6, §5.2, §5.6, §6.1, §6.4, FR-SCADA-025, -052, -104 | Q20 sign-off open |
| R21 | Register decision | Fixed | §0.3 item 13, §0.4 build tags, a build tag on every FR-SCADA (111 of 111), §12 | — |
| R22 | Register decision | Fixed | §6.10, §7.10, FR-SCADA-065 | — |
| R25 | Register decision; NOG §7.3.4 and Protocols §3.9.1(2) cited | Fixed | §3.2.10, §3.2.11 (normative text), §6.13, FR-SCADA-096, -097, -102 | Same text in `03`/`05` |
| R26 | Register decision | Fixed | §2.2, §2.3, §4.11, §6.14, AI 50–52, FR-SCADA-103 | Firmware (Q2) |
| R28, V-38 | Register decision; GRD-026/-027/-028/-029/-051/-058/-059, GRD-034 verified | Fixed | §2.2, §3.0, §4.1–§4.13, §5.3, §5.5 | `03` owns the laws |
| R29 | Register decision; GRD-053/-054 verified | Fixed | §3.1.7, §3.1.8, §6.3, FR-SCADA-022, -055 | FR-SCAD-011 and TC-INT-706 in other documents |
| R30, R31, R32, R36 | Register decisions | Fixed | §2.2, §4.13, §6.3, §6.6, §7.3, ADR-076 | — |
| R33, R34 | Register decisions | Fixed | §6.5 (retained scope topic published by `safe-stop`, releases relayed by `device-gateway`), §6.6, §7.10 | Names from `02` §3 and §5 |
| R35, R14 | Register decisions; ARC-005 verified | Fixed | §7.3, §7.5, A-SC-20 | `06` §1.8 |
| R37 | Register decision | Fixed | §2.2, §3.2.10, §3.2.12, H-SCADA-40 | `02` defines the entities |
| R39, V-34 | Register decision | Fixed | §2.7, §4.10, §4.11, §6.7, §7.7, §11.3, TC-INT-711 | `01` owns the table |
| R40, V-29 | Register decision | Fixed | §2.4, AI 17–19 and 53–55, FR-SCADA-010 | `LATE` state in `02` §2.1 |
| R43 | Register decision | Fixed | §6.6, §7.1, H-SCADA-16 | — |
| R44 | Register decision; licence facts verified (claims check 12) | User decision Q11 | §3.2.9, §7.2, §12, ADR-072/-073, FR-SCADA-109 | Spike in week 1 |
| R45, V-25 | Register decisions | Fixed | §7.11 | — |
| V-03, V-32 | Register values | Fixed | §2.4, §2.7, H-SCADA-27 | — |
| V-12…V-15 | Register values | Fixed | §6.4, FM-SCADA-051, A-SC-07 | — |
| V-18 | Register value; 07 v0.2 used a 5-hub floor | Fixed | §0.3, §2.2, §2.9, FR-SCADA-015, FM-SCADA-050, TC-SEC-715 | 15% test for real-time points is an interpretation (A-SC-08) |
| V-28 | Register value; FR-SCADA-029 already "Could (R2)" | No change needed | §3.3 | — |
| V-30, V-33 | Register values | Fixed | §2.3, §1.3.1 | Non-Spin 2 h pending NPRR1309 |
| Q6, Q11, Q12, Q15 defaults | Register defaults | User decision Q6/Q11/Q12/Q15 (defaults applied) | §2.1, §3.2.1, §7.2, §2.9, §3.2.10, §13.1 | — |

## 3. Counts

Findings citing or assigned to `07` (sections 1.1–1.4): **68** — Fixed **63** (of which "07 part" 45), Partly fixed
**0**, User decision **3** (GRD-024 Q6, GRD-042 Q12, GRD-050 Q6), No change needed **2** (GRD-052, GRD-056), Rejected
**0**, Deferred R2 **0** (designs are complete; the build tags in `07` say what is built later). By review: GRD 46, ARC
14, RT 5, JDG 3. Register items (section 2): **29** rows — Fixed 24, User decision 2 (R44/Q11; the Q6/Q11/Q12/Q15
defaults), No change needed 3 (R5, R6, V-28). No finding was rejected outright; the overstated or wrong elements are
listed in section 5. ARC-006 and ARC-019 were first recorded as partly fixed and moved to fixed once `02` §5 and `01`
§6.11/§8.1 published the single tables that `07` §6.6 and §7.10 now use.

## 4. New IDs in `07` v0.3

- **Functional requirements:** FR-SCADA-093 (ERCOT-visible invariants), -094 (UDSP tracking feed and 2-s members), -095
  (NCLR variant), -096 (ICCP/QSE-link loss), -097 (ISO instructions), -098 (COP consistency), -099 (unit typing and
  phase), -100 (CTL-150 corroboration), -101 (Secure Authentication gate), -102 (independent stop path), -103 (IEEE 1547
  settings conformance), -104 (statute-shaped mobile units), -105 (OMS feed), -106 (LTC counting), -107 (A1 per path),
  -108 (durable latched states), -109 (stack spike and ICCP `SIM` label), -110 (ERCOT data export gate), -111 (one
  integrating loop per bank).
- **Failure modes:** FM-SCADA-053…065.
- **Hooks:** H-SCADA-41…50.
- **Tests:** TC-INT-769…785, TC-SEC-716…717. (First drafted as TC-INT-743…759, which `05-testing/02` had already allocated to its own extensions 743…768; moved to the next free range before any other document referenced them.)
- **Points (spare offsets, no renumbering):** DNP3 template AI 50–58 and DBI 4; mobile-unit BI 21–22, CROB 7, AO 7; bank
  template AI 22–23 and counter 2. Renamed with the index kept: mobile-unit BI 11 `READY_TO_ENERGIZE` (was
  `CLOSE_PERMISSIVE_GRANTED`), CROB 3 `BREAKER_CLOSE` (was `BREAKER_CLOSE_PERMISSIVE`); AI 18 `HUBS_STALE` now counts V-29
  `SILENT`.
- **Assumptions:** A-SC-16…20; A-SC-01, -07, -08, -13 re-based on register values.
- **ADRs:** none added; ADR-071, -072, -073, -076 amended in line with the verdicts of `01` §17.2 (071 ratified with
  amendment; 072 and 076 ratified in part; 073, 074, 075, 077, 078 ratified).

## 5. Reviewer claims found wrong or overstated, and unresolved cross-document issues

**Claims found wrong or overstated (each checked against the primary text):**

1. **GRD-017** — "two failures in 365 days can suspend the resource" understates the rule: Protocols §8.1.1.4.3(5) says
   two failures "shall result in disqualification"; and the GD's meter-before/meter-after baseline is the kWh of the full
   15-min interval before the instruction (DR Baseline Methodologies §2.2), not a 5-min average — the 5-min figure is the
   generic NCLR telemetry baseline of §8.1.1.4.3(3)(e) (claims check claim 3).
2. **GRD-022 / GRD-001** — the 4-s UDSP cadence is a Protocol value (§6.5.7.4.1(3)); the "4-min base ramp" appears only in
   an ERCOT training deck (claims check claim 14). `07` makes the ramp shape a parameter.
3. **GRD-021** — the 24×7 control or operations center of Protocols §16.2.1(n) binds QSEs that are WAN Participants
   (verified in this pass); with a third-party QSE the QSE's center meets it — narrower than "a QSE representing resources".
4. **GRD-005** — correct, but the cited texas.public.law page is stale (no SB 231); the rule is tighter than stated (mobile,
   ≤ 5 MW) and does not reach co-ops or munis (claims check claim 5).
5. **GRD-054** — "as a requirement it will stall integrations": `07` v0.2 already made `COMMAND_SEQ` conditional
   (`SEQ_REQUIRED`); what was wrong was its default and framing, now fixed.
6. **GRD-026** — the quoted A3 frozen rule is `03`'s; `07` v0.2 only downgraded frozen values to UNCERTAIN. The substance
   (deadbands, correlated signals, field switching) still applied to `07`.
7. **GRD-009** — `07`'s `PFRC` = 0 "unless qualified" was consistent with the GD (PFR requested, not required).
8. **GRD-052** — correct against `05`; `07` §3.1.12 was already right.
9. **GRD-002** — understated rather than wrong: ERCOT's proxy AS offer applies to every qualified Resource every SCED
   run, with MW = MPC for Load Resources (Protocols §6.5.7.3(5)).
10. **JDG-008** — "a non-Python sidecar breaks ADR-001" is wrong for this design: ADR-071 already isolates each protocol
    stack in its own adapter process behind a canonical interface; the real costs are a second toolchain and build time.
11. **ARC-019** — the SCADA pre-check by request/reply is a read-only validation query, not a command-submission path; the
    contradiction the finding describes is in `01`/`02`/`05`/`06`.
12. **ARC-026** — VR membership changing on switching is required behaviour; the defect is subjects and epochs keyed on
    topology partitions, outside `07`.
13. **ARC-003** — `07` v0.2 already specified per-stream chains; the global chain tip is `01`'s.

**Unresolved cross-document issues (owners named).** Checked against the owners' documents as they stood at 10:50 on
2026-09-25; items raised earlier in this pass that the owners already carry are listed at the end.

1. **Register R16 text** — `03-security/02` lists `scada-gateway` (and `api`) as callers of the Safe-Stop Authority while
   `guardian` is unavailable ("an authorized utility's stop … only for scopes in the SSA's cached, signed entitlement
   snapshot, never fleet scope", §4.4 and §6.5; §6.9; network allow-list §19.2); the register's R16 still names only
   guardian forwarding, the out-of-band token and the watchdog, and should record the fourth trigger. `03-security` §6.5
   speaks of "an authorized utility's stop": `07` §6.8 executes a utility's block, lowered cap or cease arriving while
   `guardian` is down as the smallest containing stop scope (a stop-only equivalent at least as restrictive) —
   `03-security` should confirm that reading.
2. **Register R17 vs the ADER GD wording** — the GD says MPC − LPC equals the difference between the "greatest possible"
   injection and withdrawal; R17 requires ledger-free capability. `07` reads "possible" as possible for ERCOT dispatch
   [A]; confirm with ERCOT at registration (`07` §13.2 item 3). Until then the design follows the register.
3. **Register Q11 scope** — RT-009's hard gate (no real association carries controls without licensed Secure
   Authentication) cannot be met by counterparties that do not support application-layer authentication at all (PJM
   Jetstream, ERCOT ICCP without IEC 62351-4). `07` limits the TLS-only exception to `grid-sim` and routes those two
   through association-specific exceptions approved by SEC and the user; the user should confirm this reading with Q11.
4. **Register Q12** — because the GD makes premise/device data a condition when ERCOT requests it, a real ADER cannot be
   operated until Q12 is answered yes; the register's default (ERCOT lanes simulated) is consistent with that.
5. **`05-testing/02-test-cases-functional.md`** — TC-INT-706 still refuses direct operate on every point but the
   emergency stop, contrary to R29 and FR-SCAD-011; TC-INT-712 variant (b) (`guardian` stopped) still expects `dispatcher` to remove the
   hubs and latch the state, not execution through the Safe-Stop Authority within one control cycle, which
   `03-security/02` §6.5 now requires; bodies are needed for TC-INT-769…785 and TC-SEC-716…717; TC-SEC-032 variant B should also run through the
   gateway's SSA trigger.
6. **`05-failure-modes-and-recovery.md`** — import FM-SCADA-053…065 with RPN scoring. The alerts and runbooks `07` §9
   names for them are existing IDs of its catalogues (ALR-037, -191, -261, -262, -266, -267, -268; RB-005, -014, -024,
   -025, -026, -028, -064, -071, -072), so nothing new needs allocating; FM-SCADA-060 and -065 overlap FM-DEV-037 and
   FM-PLT-033 and are cross-referenced as their SCADA views.
7. **Register Q26** — the 10,000-hub profile (and with it `07`'s MVP-J aggregation scale of 10,000 hubs) fits the node
   only if the VM's RAM is raised; `07` §7.5 and §13.2 note it, the answer is the user's.

Already carried by their owners at the time of the check (no action): the R25 link-loss rule in `03` (DM-10) and `05`
(FM-MKT-011, FM-SCADA-009, -030, runbook RB-071); `05` FM-SCADA-006 corrected to IIN2.3; ALR-260…269 in `05` §5.3 and
FM-SCADA-022…052 in its §3.5.1; `03` §4.2 A1 bound per path and §8.6.10 ADER net-power regulator (T2/T3 split removed);
`02` §2.1 `LATE` state for the V-29 gap, §1 `phase` and unit-typed ratings, `IsoInstruction`, `CurrentOperatingPlan`,
and §5 the single stream table; `01` §6.11/§8.1 command path and permissions and §17.2 ADR verdicts; `01-product/02`
FR-SCAD-011 (per-point SBO/DO) and the `R2` tags of FR-SCAD-004/005/006/017/018; `03-security/02` CTL-150 (G-18,
DET-084), the `QSD` role, DV-20/CTL-156 for the direct counterparty stop path; `04-ui` QSE-desk views, the "what ERCOT
sees" panel and no energize or close control for mobile units (UI-OPS-07, UI-HUB-11); `06` §1.8 caps with `07`'s v0.2
estimate kept only as its upper-bound scenario, and the 2-s ADER member volume (V-32; 50,000 msg/s at 100,000 hubs) in
its volume model.

**Aligned in `07` during the final check (10:40–10:50):** the Alert and RB columns of §9 now follow `05`'s catalogues
(RB-071 for ICCP/QSE link loss on FM-SCADA-009, -029, -030; RB-054, -028, -022, -014, -035, -064, -060, -059, -025 on
rows 035–050 that `05` had already assigned; ALR-037/RB-005 and ALR-191/RB-072 on FM-SCADA-060 and -065); the
counterparty-limit bound of §4.15 and A-SC-16 follows CTL-150 (10% of the bank rating per 5 min, DET-084) instead of
`07`'s earlier max(10%, declared step) per 15 min and 10%/min slew; the new test IDs moved to TC-INT-769…785; the register
range in the status line is Q1–Q27.