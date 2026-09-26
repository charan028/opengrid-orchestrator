# Resolution A1 — Brief and product documents

Status: v1.0 · 2026-09-25 · Owner: product owner (brief, vision, functional requirements, epics and stories) · Scope:
every review finding whose Location cites `00-brief.md`, `01-product/01-vision-scope-personas.md`,
`01-product/02-functional-requirements.md` or `01-product/03-epics-and-user-stories.md`, and every
`00-decision-register.md` v0.2 item assigned to them · Result (all edited in place, each with a "Changes in this version"
list): brief v0.1 → **v0.2**; vision v3.2 → **v3.3**; functional requirements v3.2 → **v3.3**; epics and stories v1.2 →
**v1.3**.

**Method.** Each finding was treated as a claim to be proven: its quoted evidence and its reasoning were checked against
the text of my documents before anything changed ("Verified?" column). Regulatory facts were checked against
`06-reviews/05-claims-verification.md`, which arrived during this pass; its verdicts are applied (§7). The register was
revised at 10:15 and again at 10:34 during this pass; both revisions were re-read and applied (§8). The register wins where it and a review
differ; register items are cited as R*nn* / V-*nn* / Q*nn*. Nothing was dropped (D0a): no customer type, business case or
requirement was removed — the FR count rose from 323 to 389 and the story count from 153 to 237; every deferral is an `R2`
build tag with the design unchanged. No code, installs or server access were involved.

**Dispositions.** *Fixed* — everything the finding or register item asks of these documents is done, and nothing is left
for another document. *Partly fixed* — the part that belongs to these documents is done; the rest belongs to the
document named in the Note (and in §6 where it needs a change). *Rejected* — not done, with the reason. *Deferred R2* —
the specification itself is postponed (not used: every design is specified now; `R2` build tags only sequence building).
*User decision Qnn* — the register's proposed default is applied and waits for the user's answer.

## 1. Findings that cite these documents

| ID | Verified? (evidence in the documents before this pass) | Disposition | Where (after this pass) | Note |
|---|---|---|---|---|
| ARC-001 | Partly — brief §2 does say judging is on "the real, working system" and no document named a date, team or build order; the quoted schedule sentence is from `06`/strategy, not the brief | Partly fixed | Brief §2 (date, team and build order with the Q22/Q23 defaults), §7 build tags; a build tag on all 389 FRs, 36 NFRs and 237 stories; E23-S06 walking skeleton by day 5; `03` release map | G3-J (≈ 150 cases), the build-plan ADR and decoupling the decommission from production are `05-testing`/`06`'s; the effort gap is raised for Q22/Q23 (§6 item 1) |
| ARC-028 | Yes — brief §4 says "never touch them"; the reboot is in `03-NF` §8.4 and TC-NFR-016; the brief is the side that is right | Partly fixed | Brief §4: never reboot the shared host; node loss on a replica VM; k3s stopped only with the host owner's written consent (R46) | Test cases, the calendar and the chaos tooling are `05-testing`/`06`'s |
| ARC-031 | Yes — vision §4.2 carried "production multi-zone Kubernetes (architecture goal, not delivered here)" verbatim; the contradiction is `06`'s cutover plan with no cloud chosen | Partly fixed; User decision Q4 | Vision §4.2 (multi-zone arrives with the production cutover), §5.2 (cutover after the judged demo, plan B one cloud VM, genesis record R36); brief §4 | `06`'s calendar, the interim off-node bucket and plan B are `06`'s |
| ARC-034 | Partly — the quotes are from `07`; vision §5.1 made "`scada-gateway` conformance-tested with the full two-tier command-safety policy" MVP scope with no licence status, implying ICCP in the MVP | Partly fixed; User decision Q11 | Vision §5.1 (`MVP-J`: DNP3 over TLS; ICCP labelled `SIM`), §8.11; brief §3.4; `FR-SCAD-006`, new `FR-SCAD-020`; E14-S09 | `07` §12 and §13.2 corrections are `07`'s |
| ARC-036 | Yes — product NFR-207 (NFR-007 before K1) read "≥ 99.9%; ≥ 99.5% overall" with no environment and no dependency budget | Partly fixed | `NFR-207`: production target with a dependency matrix, and a node test target (24-h demo-profile soak and rehearsals, every AUTONOMOUS entry recorded); new `NFR-236` (failover per V-02) | The dependency matrix, step-ca off the restart path and OPA caching are `01`/`06`'s (R31) |
| ARC-062 | Yes — `NFR-206` is ≤ 2 s and `05` §2.8 L1 slows the console to 5 s under shedding | Partly fixed | `NFR-206`: control-room channels (alarms, stop state, firm obligations) keep 1-s updates under every shedding level (R48); E18-S01 | `05` §2.8 L1 must read "analytic channels only" (A8 raised the same) |
| GRD-001 | Yes — `FR-DISP-012` applied "react to a price spike within one tick" to every premise, ADER members included; claims check 1 confirms that an ALR's online AS arrives inside its SCED set point | Partly fixed | `FR-DISP-012` rewritten; new `FR-DISP-024`, `-025`, `FR-ARB-013`, `FR-SAFE-029`; E06-S06, S11, S12; brief §3.1 allocation priority; vision §8.2, §5.4 | DE §2.3/§8.5/§8.6.5, FM §2.12.2 and TC-FUN-114/129/202 belong to `03`, `05` and `05-testing` |
| GRD-010 | Yes — `FR-SAFE-007`/`-008` acceptance read "Does not take effect until the distinct second approver confirms" | Partly fixed; User decision Q1 | `FR-SAFE` area note, `-006…009`, `-020`, `-022`, new `-031`; E15-S03…S06, S08, S10, S16; vision §1, §6.1, §7 principle 6, §8.12 | TC-FUN-503/505 and `03-security` §5.2/§6.5 still show the first R3 (A8 §6 item 1) |
| GRD-012 | Partly — brief §3.1's partner figures (40 + 100 + 50 = 190 MW) are there; the 50 MW/min cap without a firm exemption is in DE/SEC | Partly fixed; User decision Q13 | New `FR-SAFE-030` (V-30 ramp table: contracted firm ramps pre-staged, coincident starts > 50 MW announced, discretionary caps); E15-S15 | Guardian table G-04/G-05 and TC-FUN-265 are `03-security`/`05-testing`'s; values unsigned until Q13 |
| GRD-015 | Yes — brief §3.1 described partner capacity as events only and `FR-CTR-003` had no tolling terms; claims check 9 confirms the Austin Energy toll and AE's control over charge/discharge timing | Partly fixed | Brief §3.1 (`TOLLING` and `EVENT` variants); `FR-CTR-003`, new `FR-CTR-020`, `FR-PLAN-018`; E09-S10, E05-S12; vision §6.6, §8.4 | DE §2.2/§2.6/§6.7.2 are `03`'s |
| GRD-016 | Yes — `FR-ING-001` made `LZ_CPS`/`LZ_AEN` the fleet's settlement zones; claims checks 4 and 15 confirm one LSE per ALR-ADER and 0 MW of ADER approved in those zones | Partly fixed | `FR-ING-001` (settlement role per zone; competitive-area partition); new `FR-CTR-019` (territory roles, NOIE consent, per-utility tariffs); brief §3.3; E09-S09 | EXT §11.1.3, DE §6.3/§10.5 and TC-FUN-117 are `04`/`03`/`05-testing`'s |
| GRD-020 | Partly — `FR-DEV-013` is capability negotiation only and says nothing wrong; the quoted "reported, not overridden" is FM's | Partly fixed | New `FR-DEV-019` (settings read-back at enrolment, boot and each rollout ring; signed accepted profile; quarantine from ADER and firm pools; exposed-MW figure); `FR-DEV-013` cross-reference; `FR-SIM-020`; E02-S12 | Built `R2` with the design specified now; the rollout-ring gate (FM C1) is `05`'s |
| GRD-021 | Yes — `FR-INT-003` covered bids, holds and awards only: no instruction type, acknowledgement or execution tracking | Partly fixed; User decision Q15 | New `FR-INT-012` (`IsoInstruction` intake incl. a VDI entry form, ack timer, execution, trace — `MVP-J`), `FR-DISP-024`, `FR-UI-026` (QSE desk), `FR-SEC-015`; vision §6.17 QSE-desk persona; E13-S06, S07 | The desk console builds `R2`; the role code belongs in `03-security` §5.1 (§6 item 3); runbooks RB-023/041 are `05`'s |
| GRD-035 | Partly — `FR-CTR-007` exists but carries no baseline rule; the quoted CBL text is DE §10.3 | Partly fixed | New `FR-MV-010` (battery-aware baseline, `DIRECT_HUB_METER` default, event-day exclusions); E10-S06 | DE §10.3/A-DE-37 is `03`'s; built `R2` |
| GRD-036 | Yes — `FR-MV-001` rested on a "revenue-grade" bar with no certification basis | Partly fixed | `FR-MV-001`, `-002`; new `FR-MV-011` (certification precondition per counterparty; AMI default otherwise); E10-S07 | DE §10.1 and SC §1.3.6 are `03`/`07`'s |
| GRD-037 | Yes — `FR-BILL-006` guaranteed "one kWh, one buyer" in the ledger only | Partly fixed | `FR-BILL-006`; new `FR-CTR-028` (methods per contract pair, overlap predicted at admission, clause required), `FR-BILL-011` (disclosure on invoices); E09-S18, E11-S09 | DE §10.4 is `03`'s |
| GRD-038 | Yes — `FR-FCST-003` labelled the proxy but nothing stopped a firm declaration on it | Partly fixed | `FR-FCST-003`; new `FR-FCST-011`; E04-S06; vision §6.15, §8.0 | DE §5.2/§5.3/§7.2 are `03`'s |
| GRD-047 | Yes — `FR-DISP-007` blocked all charging in a need window with no rule for homeowner-reserve recovery | Partly fixed | `FR-DISP-007` (recovery only within bank headroom, capped, lowest SOC first, excused per contract); E06-S04 | DE C17/C7(b), `FR-DE-041` and SEC G-03 are `03`/`03-security`'s |
| GRD-048 | Yes — brief §3.1 read "(co-ops and wires utilities under SB 415)" with no TDU terms and `FR-CTR-004` had no reservation calendar; claims check 10 corrects the statute to PURA §35.153 and flags 16 TAC §25.58 as proposed | Partly fixed | Brief §3.1 (TDU variant, §35.153); `FR-CTR-004`; new `FR-CTR-022`; E09-S14 | DE §2.6 is `03`'s; built `R2` (the demo's deferral sits in a NOIE partition, R27a) |
| GRD-050 | Yes — brief §4 named only the managed multi-zone cluster as the production target | Partly fixed; User decision Q6 | Brief §4: the production ICCP connectivity depends on the QSE model (physical sites and private interconnect with Base as QSE; the third-party QSE's interface otherwise) | Physical sites, interconnect, failover drills and the third-party SLA are `07`/`06`'s |
| GRD-053 | Yes — `FR-SCAD-011`'s acceptance read "Direct-operate without select is rejected" | Partly fixed | `FR-SCAD-011` (R29: per-point SBO/DO, sequencing, SA anti-replay, state preconditions, `COMMAND_SEQ` only without SA); E14-S06; brief §3.4 | TC-INT-706 is `05-testing`'s |
| GRD-060 | Yes — `FR-DISP-003` read exactly "Need = measured (net of fleet) + prior fleet output − (rating − margin)" | Partly fixed | `FR-DISP-003` (fleet output at the SCADA sample's source time, ≤ 1 s; kVA/per-phase per R18); E06-S02 | TC-FUN-141 is `05-testing`'s |
| JDG-001 | Yes — counts reproduced exactly: 151 of 153 stories MVP (144 Must, 38 of size L) and 315 of 323 FRs MVP; no effort, staffing or order anywhere | Fixed (effort gap raised as Q22/Q23) | `02` §1 rule and a build tag in every Priority cell (389 FRs: 267 `MVP-J`, 63 `MVP-B`, 59 `R2`), NFR Build column; 237 stories tagged (151/44/42); `03` release map (builds, anchors, capacity, lanes, walking-skeleton-first order, milestones); vision §5.1 | At the anchors `MVP-J` is ≈ 292 pd against ≤ 128 pd of capacity (4 FTE, m = 2) — §6 item 1 |
| JDG-003 | Yes — vision §5.4 was a 14-step tour with no breach-prediction, performance or real-data moment | Partly fixed | Vision §5.4.1 (7-minute, 7-beat script), §5.4.2 (five-minute cut), §5.4.3 (setup, scorecard, fallback, Q&A backups), §5.4.4 (the 14 steps as the unattended rehearsal plus five scenes); E23-S01, S07 | TC-E2E-020…041 are `05-testing`'s; the screen mapping is in `04-ui` §13 (A8) |
| JDG-005 | Yes — vision §1 led the "why" with the reviewers' IRR table (−3.6% / 4.1% / 9.2%) | Partly fixed | Vision §1 (thesis sentence; IRR table replaced by a Projects Deck link and a one-line note; value-of-orchestration paragraph); §3.1 KPI-22 first; new `FR-RPT-011`; E22-S06 | Extending `FR-DE-122` to the four policies is `03`'s |
| JDG-008 | Yes — `FR-SCAD-003…019` were all MVP with five protocols and IEC 62351 security; claims check 12 confirms the stack facts (no Python binding, non-production licence, no TLS in the open-source default build) | Partly fixed; User decision Q11 | `FR-SCAD` build tags (DNP3 over TLS `MVP-J`; IEC 104 Could · R2 per V-28; IEEE 2030.5, ICCP, SA, redundancy, commissioning `R2`); new `FR-SCAD-020`; E14 split (S03, S04, S07, S10 `R2`); brief §3.4 | The week-1 stack spike and ADR-072/073 are `07`'s |
| JDG-009 | Yes — vision §4.2 put "a real device protocol beyond the mock agent" out of scope and no mode ran the brain without commanding | Partly fixed | New `FR-DEV-017` (`DeviceAdapter`), `FR-DISP-032` (`SHADOW`); vision §4.4, §8.17; brief §1, §6; E02-S09, E06-S16 | The interface definition in `01`/`02` is architecture's |
| JDG-010 | Yes — E05-S06 criterion 2, `FR-PLAN-008` and `FR-BILL-004` treated a diverted award as a normal priced outcome; vision §8.3 said the same | Partly fixed | `FR-PLAN-008`, `FR-BILL-004`; E05-S06, new S09 (forward release), E11-S03; vision §8.3; brief §3.1, §3.2 | The UI mock was fixed by A8; DE §7.4/§10.5 are `03`'s |
| JDG-011 | Partly — these documents already separated 10:00 CT (`FR-PLAN-010`, brief §4) and 14:00 CT (`FR-INT-004`, KPI-06); the conflation was in `04-ui` and DE §7.1 | Partly fixed | `FR-INT-004`, `FR-PLAN-010` (both deadlines named), new `FR-UI-025`; KPI-06; brief §4; E13-S03, E18-S17, E05-S10 | DE §7.1 is `03`'s; the UI was fixed by A8 |
| JDG-012 | Yes — KPI-13 targeted "≥ 1 intraday re-plan cycle"; `FR-PLAN-012` and `FR-SAFE-015` "≥ one cycle ahead" | Partly fixed | KPI-13 (V-41, calibration plot); `FR-PLAN-012`, `FR-SAFE-015`; new `FR-RPT-012`; E22-S07 | Architecture NFR-008 is `01`'s |
| JDG-013 | Yes — vision §3 had 21 KPI rows and E23-S05 asked for all of them on one screen | Partly fixed | Vision §3.1 (8-KPI headline in display order) and §3.2 (drill-down); new KPI-22…25; `FR-UI-007`; E18-S04, E23-S05 | TC-E2E-033/038 are `05-testing`'s |
| JDG-017 | Yes — `FR-ING-001` placed the fleet in `LZ_CPS`/`LZ_AEN`; claims check 15 shows 0 MW of ADER approved there | Partly fixed; User decision Q25 | `FR-ING-001`, new `FR-CTR-019`; brief §3.3 (R27a demo territory); vision §5.3, §5.4.1; E09-S09 | DE §12.1 is `03`'s |
| JDG-018 | Partly — vision §5.4 step 12 made the copilot a storyline step; most wrapper-risk evidence is in `01` §5.3/§5.10 | Partly fixed | New `FR-AI-015` (AI-off parity); E20-S07; vision §5.4.1 beats 6 and 7 (AI off; one before/after optimization; DNP3 on the wire in the evidence) | The README "where the brain lives" and Examples A–C as tests are the README's, `03`'s and `05-testing`'s |
| JDG-023 | Yes — epics open question 1 called sizes "relative, assumption-based" pending velocity | Fixed | `03` conventions (S 0.5, M 1.5, L 4 pd); release map (effort per build and lane, daily burn-down, walking skeleton by day 5); E23-S06; open question 1 closed | — |
| JDG-024 | Yes — README says 318 FRs, `02` §5 said 323 | Partly fixed | `02` §5 recount (389 FRs and 36 NFRs, by build tag); `NFR-221` makes a count mismatch fail CI | The README counts are the README owner's (§6 item 2) |
| JDG-026 | Yes — `FR-UI-008`, `FR-RPT-005` and E23-S05 carried a met/partly/unknown business-case board in the console | Fixed | `FR-UI-008` (measured facts beside the claims, linked to the Projects Deck), `FR-RPT-005` (export to the Projects Deck, no verdict computed); E18-S04, E22-S04, E23-S05; vision §4.2, §5.4.4 step 14, §6.13 | The UI side was fixed by A8 |
| JDG-027 | Yes — `FR-AI-013` declines personal-data questions on the node with no demo framing | Fixed | `FR-AI-013` (a scripted privacy beat beside the pre-send check log, or Q&A); E20-S06; vision §5.4.3 | — |
| JDG-028 | Partly — `FR-DEV-014` already required the identical device path; nothing published the contract, separated the generator or restricted faults to the simulator's API; TR-R2 is `05-testing`'s | Partly fixed | `FR-DEV-014`; new `FR-DEV-018` (contract package, conformance suite, N/N-1), `FR-SIM-018` (off-node generator, fault API only); E02-S10, E19-S09 | `02` owns the contract (R33); TR-R2 is `05-testing`'s |
| JDG-029 | Yes — nothing in these documents gave an evaluator an install path | Partly fixed | New `FR-OPS-014` (`PROFILE=demo`, `make demo` on k3d, quick start, operator quick reference, integrator guide, runbook index); E17-S10; vision §4.4 | The `make` targets and the seeding are `06`'s |

**Per-document verdict items (no finding ID).**

| Review verdict | What changed |
|---|---|
| ARC §3/§4 on the brief: Redis named, event cadence ambiguous, no team or date, no partition vocabulary, no build-for-J line | Brief §4 Valkey (R43) and V-03/V-32 cadences; §2 date, team, build order; §6 fleet allocator, execution shard, conflict component, epoch, scope broadcast; §2/§7 build tags |
| ARC §4 on `02`: NFR-007 scoped, NFR-006 under shedding, K1 | `NFR-207`, `NFR-206`; K1 already applied (NFR-201…), new product NFRs continue at `NFR-233` |
| GRD §3 on the brief: partner capacity as events, TEEEF without isolation, ERCOT roles, priority below firm, 10-s cycle | Brief §3.1 rows and allocation priority (L2 above T1), §3.3 roles, §4 cadences |
| GRD §3 on `02`: missing FRs for ADER tracking, ERCOT-available telemetry, COP, VDI, EEA posture, settings conformance, TEEEF limits | `FR-DISP-024/025`, `FR-ARB-013`, `FR-SAFE-029`, `FR-PLAN-017`, `FR-INT-012/013`, `FR-UI-026`, `FR-SAFE-027`, `FR-DEV-019`, `FR-CTR-024` |
| JDG §5.5/§5.6/§6.2/§7/§8 | Build tags per story and FR; all nine types dispatchable in `MVP-J` (vision §5.3); the 7-minute script; the Insights and value-of-orchestration FRs; vision §4.4 |

## 2. Register items assigned to these documents

| ID | Verified? (state before this pass) | Disposition | Where (after this pass) | Note |
|---|---|---|---|---|
| R3 (amended) | Yes — the `FR-SAFE` area note, `-006…009`, `-020`, `-022`, E15 and the vision implemented the first R3 (zone/fleet engage waited for a second approver; declared-capacity changes Tier 1) | Fixed; User decision Q1 | `FR-SAFE` area note (four classes, V-12…V-15), `-006…009`, `-020`, `-022`, `-031`; E15-S03…S06, S08, S10, S16; vision §1, §6.1, §6.11, §6.13, §8.12, §10 item 2 | R3 stays proposed until Q1; the revised Q1 default approvers (shift supervisor for bank and zone, executive on call or a second shift supervisor for fleet, never the system admin) are applied (§8) |
| R16 | Yes — no independent stop path in these documents | Fixed | Brief §5 (`safe-stop`, namespace `og-safestop`), §1, §9; `FR-SAFE-025`, `-032`, `NFR-235`, `FR-SIM-020`; E15-S12; vision §1, §8.12 | Security FRs (FR-SEC-204/205, DV-17) are `03-security`'s; the UI tags it `MVP-J` and so does the release map (A8 asked) |
| R17 | Yes — the ERCOT lanes were ordinary lower-tier calls; no `IsoInstruction`, COP or ERCOT-visible capability | Fixed | Brief §3.1, §4, §6; `FR-DISP-012`, `-024`, `-025`, `FR-ARB-013`, `FR-SAFE-029`, `FR-PLAN-017`, `FR-INT-003`, `-012`, `-013`, `FR-CTR-026`; E06-S11, S12, E05-S08, E13-S06, E09-S16; vision §7 principle 14, §8.2, §8.3 | The revised R17 is applied: telemetered AS capability, not offers, is the boundary (`FR-SAFE-029`) and NCLR disqualification lasts ≥ 6 months (`FR-CTR-026`). The NCLR variant and the full QSE-desk console build `R2`; VDI entry, acknowledgement and execution tracking are `MVP-J` (`FR-INT-012`) |
| R18 | Yes — kW law against kVA ratings; no phase; recharge without add-back | Fixed | `FR-DISP-003`, `-028`, `-030`, `FR-TWIN-013`, `FR-CTR-004`, `-027`; brief §3.1, §3.2, §6; E03-S03, E06-S02, S04, S14 | — |
| R19 | Yes — no emergency posture | Fixed; User decision Q14 | `FR-SAFE-027`, `FR-PLAN-016`, `FR-ING-017`, `FR-SIM-019`; brief §3.1, §9; vision §8.15; E15-S13, E05-S11, E01-S08 | Framed as operator policy per claims check 7 |
| R20 | Yes — TEEEF allowed grid-parallel support and Base-initiated close | Fixed; User decision Q20 | Brief §3.1; `FR-CTR-017`, `-024`, `-025`, `FR-BILL-005`; E09-S07, S15; vision §6.5, §8.8 | Aligned with claims check 5 (SB 231; co-ops and munis outside §39.918) |
| R21 | Yes — no build tags; everything MVP | Fixed; User decision Q22, Q23 | Every FR/NFR/story tagged; `03` release map; brief §2, §7; vision §5 | Effort gap: §6 item 1 |
| R22 | Partly — `FR-TRACE-008` required a preceding entry; no degraded journal | Fixed | `FR-TRACE-008`, `-011`; E12-S06; brief §9 | — |
| R23 | Yes — absent | Fixed | Brief §1, §5, §6; `FR-DEV-017`, `FR-DISP-032`, `FR-OPS-014`; E02-S09, E06-S16, E17-S10; vision §4.4, §8.17 | — |
| R24 | Yes — no Insights, value of orchestration, performance strip or 7-minute script | Fixed | Vision §1, §3, §5.4; `FR-UI-007`, `-023`, `-024`, `FR-RPT-001`, `-003`, `-011…013`, `FR-MV-008`; E18-S04, S15, S16, E22-S01, S06…S08, E23-S05, S07 | `FR-DE-050` to Must is `03`'s (§6 item 5) |
| R25 | Yes — no QSE desk, ICCP-loss rule or independent utility stop path | Fixed; User decision Q15 | `FR-DISP-026`, `FR-INT-012`, `FR-UI-026`, `FR-UI-027`, `FR-SAFE-033`, `FR-SEC-015`; vision §6.17, §8.16; E06-S12, E13-S06, S07, E15-S18, E16-S06, E18-S18 | The role code `QSD` (console alias `QSE`) is now in `03-security` §5.1 v0.2 and cited here |
| R26 | Yes — absent | Fixed | `FR-DEV-019`, `-020`, `FR-DISP-027`, `FR-TWIN-015`, `FR-SAFE-028`, `FR-SIM-020`; E02-S12, S13, E03-S06, E06-S13, E15-S14; vision §8.15 | Settings conformance builds `R2` |
| R27 | Yes — no roles, tolling, dual-participation modes, SB 415 variant, PJM self-serve or cycle budget | Fixed | Brief §3.1, §3.3; `FR-ING-001`, `FR-CTR-003`, `-019…023`, `FR-PLAN-018`; E09-S09…S12, S14, E05-S12; vision §8.4, §8.5, §8.9 | Aligned with claims checks 4, 9, 10, 11, 15 |
| R27a | Yes — the fleet sat only in NOIE zones | Fixed; User decision Q25 | Brief §3.3; `FR-ING-001`; vision §5.3, §5.4.1 | — |
| R28 | Partly — HOLD, recovery and loop rules were absent from the FRs | Fixed | `FR-DISP-005`, `-007`, `-031`, `FR-TWIN-014`, `FR-CTR-027`; brief §3.1 (`PIPELINE_AC` shift factor), §3.2; vision §8.7 | — |
| R29 | Yes — `FR-SCAD-011` demanded SBO everywhere | Fixed | `FR-SCAD-011`; brief §3.4; E14-S06; vision §8.11 | — |
| R30 | Yes — the brief had no allocator/shard vocabulary | Fixed | Brief §5 `dispatcher`, §6; `02` area index; vision §8.10 | — |
| R31 | Yes — `FR-SAFE-014` "fail-safe hold" did not separate timeout from veto | Fixed | `FR-SAFE-014`, `-022`, `NFR-234`; brief §9; vision §7 principle 7; E15-S02, S10 | — |
| R35 | Yes — `FR-SIM-013` put 10,000 hubs "on the single node" with a co-located generator | Fixed; User decision Q24, Q26 | Brief §4, §5; `FR-SIM-013`, `-018`, `NFR-210`; E19-S07, S09, E18-S16; vision KPI-25, beat 7, §5.4.3 | The 10:34 revision is applied: the 10,000-hub runs use the node only if it can carry them, otherwise the replica VM, labelled (Q26; `06` §1.9.1) |
| R38 | Yes — erasure did not address backups | Fixed | `FR-PRIV-006`, `-014`; E21-S09; vision §1, §8.14 | — |
| R40 / V-29 | Yes — `FR-TWIN-009` "stale" had no thresholds | Fixed | `FR-TWIN-009`; E03-S01, E02-S07; brief §6 | — |
| R41 / V-25 | Yes — `FR-OPS-001` implied a paging route per KPI | Fixed | `FR-OPS-001`, `-002`; E17-S01, S07; brief §4 | — |
| R43 | Yes — `contracts` as one deployable | Fixed | Brief §4, §5; `02` area index | — |
| R44 | Yes — ICCP as a real MVP path | Fixed; User decision Q11 | `FR-SCAD-006`, `-020`; brief §3.4; E14-S09 | — |
| R10 / R47 | Yes — `FR-SVC-004` required the full-year replay for every change | Fixed | `FR-SVC-004`; E08-S01; brief §3.5; vision §4.3 | The full-year gate as a nightly job builds `R2`; until then a loosening change stays inactive |
| R48 | Partly — no clip-or-defer rule; `NFR-206` silent on shedding | Fixed | `FR-DISP-029`, `NFR-206`; E06-S15; vision §1, §8.10 | — |
| R49 | Yes — `FR-AI-004` allowed approval "above threshold" only | Fixed | `FR-ARB-009`, `FR-AI-004`; E07-S06, E20-S02; brief §1; vision §8.13 | — |
| V-03 / V-32 | Yes — "10 s, faster during events"; tick 2 s only for firm events | Fixed | Brief §4; `FR-DISP-002`, `FR-DEV-003`; E02-S13 | — |
| V-12…V-17 | Partly — ramps present; no expiries, co-sign rule, stop classes, sequencing or ≥ 15-min release | Fixed | `FR-SAFE` area note, `-006…009`, `-028`; E15-S03…S06, S08, S14; vision §8.12 | Ramp values unsigned (Q13) |
| V-18 / V-19 | Partly — KPI-21 "≤ 30 days"; no aggregation floor | Fixed | KPI-21; `FR-AI-013`, `-014`, `FR-PRIV-004`, `-011`; E21-S03, E20-S06 | — |
| V-20 | Yes — `NFR-204` "≤ 30 min solve" | Fixed | `NFR-203`, `NFR-204` | — |
| V-22 | Partly — budget unquantified | Fixed | `FR-AI-011`, `NFR-229` | — |
| V-27 | Yes — `FR-SVC-015` let no change touch a running event | Fixed | `FR-SVC-015`; E08-S07 | — |
| V-28 | Yes — `FR-SCAD-004` "Should (MVP)" | Fixed | `FR-SCAD-004`, `-018` (Could · R2); E14-S02, S04 | — |
| V-30 | Yes — absent | Fixed; User decision Q13 | `FR-SAFE-030`, `FR-DISP-030`, `FR-ARB-013`; E15-S15 | — |
| V-33 / Q7 | Partly — brief "ECRS (1 h)" without source; `FR-CTR-006` unquantified | Fixed | Brief §3.1, §3.2; `FR-CTR-006`; vision §8.3; E09-S03 | ECRS 1 h and Non-Spin 4 h → 2 h at NPRR1309 per claims check 6 |
| V-34 / V-35 | Partly — KPI-03 "≤ 5 min"; no end-to-end or guardian budget | Fixed | KPI-03; `NFR-233`, `NFR-234`, `NFR-228` | — |
| V-37 | Yes — `FR-SEC-015` listed 13 roles of its own | Fixed | `FR-SEC-015` points to `03-security` §5.1 as authoritative and names `QSD` and `FSE`; vision §6.17, §8.8; E16-S06, E09-S07; `FR-CTR-024` | `QSD` and `FSE` were added by `03-security` v0.2 during this pass |
| V-41 | Yes (see JDG-012) | Fixed | KPI-13, `FR-PLAN-012`, `FR-SAFE-015`, `FR-RPT-012`; E22-S07 | — |
| K1 | Yes — product NFRs were already NFR-201…232 in `02` | No change needed | New product NFRs continue at `NFR-233` | — |
| C-23 | Yes — brief §7 already had the right test file names | Fixed | Brief §7 tree adds the register, `build_traceability.py` and the `06-reviews` files | — |
| D0a | Yes — checked: no type, case or requirement removed | No change needed | FRs 323 → 389; stories 153 → 237; no "drop"/"not workable"/"stays out" wording | — |
| D0f | Yes — the IRR table and the condition board sat in these documents | Fixed | Vision §1, §4.2, §6.13; `FR-UI-008`, `FR-RPT-005` | — |
| D5 / Q12 | Partly — no rule for ERCOT premise-level requests | Fixed; User decision Q12 | `FR-PRIV-013`; brief §3.3; vision §1, §8.14; E21-S08 | ERCOT lanes simulated until Q12 |
| Q10 | — | User decision Q10 | `FR-SAFE-009` applies the default (only the engaging party releases) | — |

## 3. Related findings served by these documents (they do not cite them)

| ID | What changed in the product documents |
|---|---|
| GRD-002, GRD-013 | ERCOT-visible capability from ledger-free capacity, offers and an energy bid covering telemetry, the ISO-boundary invariant: `FR-ARB-013`, `FR-SAFE-029`, `FR-SAFE-030` |
| GRD-003, GRD-006, GRD-007, GRD-008 | kVA/per-phase regulation, phase in topology, recharge add-back, outcome/share-based performance: `FR-DISP-028`, `FR-DISP-030`, `FR-TWIN-013`, `FR-CTR-027` |
| GRD-004 | EEA posture and pre-positioning: `FR-SAFE-027`, `FR-PLAN-016`, `FR-ING-017` |
| GRD-005, GRD-018, GRD-019 | Statute-shaped `MOBILE_TEEEF`, no Base close, cold-load planning, `MOBILE_DER`: `FR-CTR-024`, `FR-CTR-025` |
| GRD-009, GRD-031 | Freeze on autonomous response; volt-var-aware capability: `FR-DISP-027`, `FR-DEV-020`, `FR-TWIN-015` |
| GRD-011 | Independent utility stop path; V-07 autonomy: `FR-SAFE-033`; vision §8.16 |
| GRD-014 | Hold the set point flat on link loss: `FR-DISP-026` |
| GRD-017 | NCLR variant with SCED awards, 15-min baseline and disqualification (claims check 3): `FR-CTR-026` |
| GRD-022, GRD-023 | 2-s cadence for ADER members; COP: `FR-DISP-002`, `FR-DEV-003`, `FR-PLAN-017`, `FR-INT-013` |
| GRD-024 | Dual participation per partner; ERS exclusion: `FR-CTR-021` |
| GRD-025 | Stop sequencing and frequency gating: `FR-SAFE-028` |
| GRD-027, GRD-028, GRD-030, GRD-051 | Switching-order freshness, one integrating loop, filtered feedforward, HOLD rule: `FR-TWIN-014`, `FR-DISP-031`, `FR-DISP-005` |
| GRD-034 | Shift factor as a profile parameter: brief §3.1; vision §8.7 |
| GRD-039, GRD-040 | ERCOT award schemas: `FR-INT-003` |
| GRD-041 | Automatic downward re-declarations: `FR-SAFE-031`, `FR-SAFE-020` |
| GRD-042 | Premise-level data held until Q12: `FR-PRIV-013` |
| GRD-043, GRD-045 | Cycle budget; no fleet recharge into the net-load peak: `FR-PLAN-018`, `FR-DISP-030` |
| GRD-046 | QSE desk and "what ERCOT sees": `FR-UI-026`, `FR-UI-027`; E13-S07, E18-S18 |
| GRD-049 | PJM self-serve and CSP exclusion: `FR-CTR-023` |
| GRD-054, GRD-055 | `COMMAND_SEQ` only without SA; one hub-state table: `FR-SCAD-011`, `FR-TWIN-009` |
| GRD-056, GRD-057 | ECRS 1 h; per-ADER and system caps: `FR-CTR-006`, `FR-PLAN-007` |
| ARC-004, ARC-043, ARC-049 | Timeout ≠ veto; pre-image before signing; time-boxed AI constraint sets: `FR-SAFE-014`, `FR-TRACE-008`, `FR-ARB-009`, `FR-AI-004` |
| ARC-005, ARC-029 | Load generator off the node: `FR-SIM-013`, `FR-SIM-018`, `NFR-210` |
| ARC-015, ARC-016 | Connectivity vs eligibility; end-to-end latency: `FR-TWIN-009`, `NFR-233` |
| ARC-022, ARC-023 | Crypto-shredding outside backups; insert-only numeric settlement: `FR-PRIV-014`, `FR-BILL-012` |
| ARC-024 | Stop exempt from waiting on the guardian: `FR-SAFE-025` |
| ARC-033, ARC-054, ARC-061 | Paging budget; tiered profile gate; clip or defer: `FR-OPS-001`, `FR-SVC-004`, `FR-DISP-029` |
| RT-001, RT-002, RT-010 | Safe-Stop Authority and the independent epoch authority: `FR-SAFE-025`, `FR-SAFE-032`, `NFR-235`; brief §5 |
| RT-007 | Signed, anchored local journal: `FR-TRACE-011` |
| RT-009 | No real association with controls under the TLS-only exception: E14-S02 |
| RT-011 | AI-drafted flag and guardian dry-run preview on intake: E20-S04 |
| RT-012 | Cross-principal cumulative windows: `FR-SAFE-026` |
| JDG-002 | Judged demo on 2026-10-21 (Q22 default), cutover after it: brief §2, §4; vision §5.2 |
| JDG-004, JDG-020 | Insights view, performance strip and benchmark report: `FR-UI-023`, `FR-UI-024`, `FR-RPT-013` |
| JDG-006, JDG-015, JDG-016 | Demo values profile and 2,000 live hubs; record/replay of real data: brief §4; `FR-ING-018` |
| JDG-014, JDG-022, JDG-030 | G3-J, formative usability, clock port and seeds in the walking skeleton: E23-S06, S08; `NFR-217` |
| JDG-019 | Seven-screen judged console by build tags: `FR-UI-*` tags; E18 split |

## 4. Disposition counts

§1 and §2 hold **84 rows** (39 findings, 45 register items).

| Disposition | Findings (§1) | Register items (§2) | Total |
|---|---|---|---|
| Fixed | 4 (JDG-001, -023, -026, -027) | 42 | **46** |
| Partly fixed (the product part done; the rest another document's — §6 and the Notes) | 35 | 0 | **35** |
| No change needed (verified) | 0 | 2 (K1, D0a) | **2** |
| User decision Qnn only (default applied) | 0 | 1 (Q10) | **1** |
| Rejected | 0 | 0 | **0** |
| Deferred R2 | 0 | 0 | **0** (every design is specified now; `R2` tags only sequence building) |

Also waiting on a user decision (register default applied): ARC-031 (Q4), ARC-034 and JDG-008 (Q11), GRD-010 (Q1), GRD-012
(Q13), GRD-021 (Q15), GRD-050 (Q6), JDG-001 (Q22, Q23), JDG-017 (Q25); and R3 (Q1), R19 (Q14), R20 (Q20), R21 (Q22, Q23),
R25 (Q15), R27a (Q25), R35 (Q24, Q26), R44 (Q11), V-30 (Q13), D5 (Q12). No finding was rejected: every claim that cites these
documents was borne out, at least in part; where the claims check refined a reviewer's fact (NCLR disqualification rather
than suspension, PURA §35.153 rather than §35.152, ECRS 1 h), the refined fact was applied.

## 5. New IDs

| Family | New IDs | Build |
|---|---|---|
| FR (66) | `FR-ING-017`, `-018`; `FR-DEV-017…020`; `FR-TWIN-013…015`; `FR-FCST-011`; `FR-PLAN-016…018`; `FR-DISP-024…032`; `FR-ARB-013`; `FR-SVC-016`; `FR-CTR-019…028`; `FR-MV-010`, `-011`; `FR-BILL-011`, `-012`; `FR-TRACE-011`; `FR-INT-012`, `-013`; `FR-SCAD-020`; `FR-SAFE-025…033`; `FR-OPS-014`; `FR-UI-023…027`; `FR-SIM-018…020`; `FR-AI-015`; `FR-PRIV-013`, `-014`; `FR-RPT-011…013` | Per row in `02` (e.g., `FR-SAFE-025` `MVP-J`, `FR-DISP-032` `MVP-B`, `FR-CTR-022` `R2`) |
| NFR (4) | `NFR-233…236` | `MVP-J` |
| KPI (4) | KPI-22 value of orchestration, KPI-23 reserve violations, KPI-24 kWh claimed or billed twice, KPI-25 control-loop tick p99 | Headline scorecard |
| Stories (84) | E01-S07…S10; E02-S08…S13; E03-S06…S08; E04-S06, S07; E05-S08…S12; E06-S11…S17; E08-S09, S10; E09-S09…S18; E10-S06, S07; E11-S07…S09; E12-S06; E13-S06, S07; E14-S09, S10; E15-S12…S18; E16-S07; E17-S07…S10; E18-S10…S18; E19-S09…S11; E20-S07, S08; E21-S07…S09; E22-S06…S08; E23-S06…S08 | Per story in `03` |
| Document sections | Brief §3.3; vision §3.1/§3.2, §4.4, §5.3, §5.4.1…§5.4.4, persona §6.17, principles 14–15, narratives §8.15…§8.17; `03` release map subsections (builds, effort and capacity, order, milestones) | — |

Amended in place (IDs unchanged): `FR-ING-001`, `-011`; `FR-DEV-003`, `-013…015`; `FR-TWIN-009`; `FR-FCST-003`;
`FR-PLAN-007`, `-008`, `-010`, `-012`; `FR-DISP-002`, `-003`, `-005`, `-007`, `-008`, `-011`, `-012`; `FR-ARB-009`;
`FR-SVC-004`, `-005`, `-015`; `FR-CTR-003`, `-004`, `-006`, `-014`, `-017`; `FR-MV-001`, `-002`, `-008`, `-009`;
`FR-BILL-003…006`; `FR-TRACE-008`; `FR-INT-003`, `-004`; `FR-SCAD-004…006`, `-011`, `-019`; `FR-SAFE-006…009`, `-014`,
`-015`, `-020`, `-022`; `FR-SEC-004`, `-015`; `FR-OPS-001`, `-002`; `FR-UI-007`, `-008`, `-015`; `FR-SIM-013`, `-015`;
`FR-AI-004`, `-011`, `-013`; `FR-PRIV-004`, `-006`, `-008`, `-010`, `-011`; `FR-RPT-001`, `-003`, `-005`, `-008`;
`NFR-201`, `-203`, `-204`, `-206`, `-207`, `-210`, `-217…219`, `-221`, `-225`, `-228…230`; KPI-03, -06, -07, -08, -09, -10,
-13, -17, -18, -19, -21; stories E02-S05, S07; E03-S01, S03; E04-S02, S05; E05-S06, S07; E06-S02…S04, S06; E07-S06;
E08-S01, S04, S06, S07; E09-S03, S04, S07; E10-S05; E11-S02, S03, S05, S06; E13-S02, S03; E14-S02, S04, S06, S08;
E15-S02…S06, S08, S10; E16-S02, S05, S06; E17-S01…S03, S05, S06; E18-S01, S03, S04, S06, S07, S09; E19-S07, S08;
E20-S02…S04, S06; E21-S03, S04; E22-S01, S03, S04; E23-S01, S03…S05. `FR-ING-011` and `FR-CTR-014` rose from Could to
Must; `FR-SCAD-004`/`-018` are Could.

## 6. Unresolved cross-document issues

| # | Owner | Issue | Needed change |
|---|---|---|---|
| 1 | User (Q22, Q23) | At the anchors (S 0.5, M 1.5, L 4 pd) `MVP-J` is ≈ 292 pd (151 stories) and `MVP-B` ≈ 80 pd, against a build-window capacity of 16–128 pd (1–4 FTE, m = 1–2). The judge's bottom-up Line A (≈ 59 pd) assumes thin first implementations. | Decide after the day-5 walking-skeleton measurement: move the date, add capacity, or present Line A only (`03` release map, "Two estimates, one open decision") |
| 2 | `00-README.md` | Counts: "318 FRs in 22 areas, 32 NFRs" and "19 ADRs" (JDG-024, ARC-064) | 389 FRs in 22 areas; 36 product NFRs (NFR-201…236); the architecture ADR count from `01` |
| 3 | `03-security/02` (resolved during this pass) | §5.1 had no QSE-desk role code (R25, V-37) | `03-security` v0.2 added `QSD` (console alias `QSE`) and `FSE`; `FR-SEC-015`, `FR-CTR-024`, vision §6.17, §8.8 and E16-S06, E09-S07 cite them — nothing left |
| 4 | Register / `06` | R35's demo infrastructure set (no Valkey) vs R43 (acknowledgement correlation in Valkey) and `06` (Valkey on the node); the brief §4 now names Valkey as the Redis-protocol store | State whether the demo values profile carries Valkey or correlates in NATS KV |
| 5 | `02-architecture/03` | `FR-DE-050` still Should (R24 makes it Must); `FR-DE-122` must add today's rule allocator and the fixed schedule (KPI-22); `FR-DE-013`'s 120-s design target vs V-34 (240 s); Example A's squeezed-base-point variant (R17) | Update the DE rows and examples |
| 6 | `02-architecture/01` | NFR-008's breach lead time vs V-41; §12 "rejected" under saturation (ARC-061) | Cite V-41; "clipped or deferred" (R48) |
| 7 | `05-testing` | Tests still encode the old behaviour: TC-FUN-503/505 (R3), TC-INT-706 (R29), TC-FUN-141 (GRD-060), TC-FUN-122 (ECRS 1 h), TC-FUN-129/202/114 (R17), TC-E2E-008 (R20), TC-E2E-020…041 (the 7-minute script); 66 new FRs and 84 new stories need `Covers:` links | Update the cases, add cases for the new FRs, regenerate the matrix (`NFR-219`, `NFR-221`) |
| 8 | `05-testing` | G3-J item 4 asks for identical decision-trace hashes across two system runs; ARC-029 shows timing defeats that; E23-S01/S07 use replay from recorded version vectors (component-level) plus semantic invariants | State the same determinism criterion in G3-J |
| 9 | `02-architecture/05` | §2.8 L1 slows every console channel to 5 s (ARC-062) | "Analytic channels only"; control-room channels keep 1 s (`NFR-206`) |
| 10 | `02-architecture/07` | §12 and §13.2 errata (ARC-034); stack choice with TLS terminated outside the process if needed (claims check 12) | Correct the sections; record the week-1 spike |
| 11 | Register (resolved during this pass) | V-33 said "ECRS per Q7" and R20 lacked SB 231 | The 10:15 revision filled V-33, answered Q7 and added SB 231 to R20; these documents follow it (§8) — nothing left |
| 12 | `03-security/02` | Red-team numbering FR-SEC-204/205 vs product `FR-SAFE-025`/`-032` | Cross-reference the pairs so tests cover both |
| 13 | Claims check | Its §3 item 5 says "Remove any §39.918 revenue or partner lane for the fleet"; D0a forbids removal, and R20 keeps `MOBILE_TEEEF` as leased restoration units with no market revenue — which is what the statute allows | No change to scope; read "remove" as "no market-revenue lane", which these documents already state |
| 14 | `04-ui` (A8 §6 items 9 and 10) | A8 asked the product documents for the Insights, Safety, Grid & ISO desk and out-of-band FRs, the 8-KPI set, JDG-010/-012/-026 rewrites and a decision on beat 5's AI veto | Done here: `FR-UI-023…027`, `FR-SAFE-025`, KPI-22…25, `FR-PLAN-008`, `FR-BILL-004`, KPI-13, `FR-UI-008`, `FR-RPT-005`; beat 5 now vetoes a customer call that would breach the reserve floor (AI proposals are `R2`); the release map confirms the SSA/out-of-band stop as `MVP-J` (E15-S12) and "what ERCOT sees" as `MVP-B` (E18-S18) — nothing left for the UI |
| 15 | `02-architecture/02` | The minimal VDI entry form of `FR-INT-012` (`MVP-J`) needs the `IsoInstruction` caller and acknowledgement-time fields (A8 §6 item 6) | Confirm the fields in 02 §1.2 (A2 reports them added) |
| 16 | `05-testing/01`, `02-architecture/06` | "E6" names two environments: `05-testing/01` §6 defines E6 as the production-like managed multi-zone cluster; `06` §1.9 and register Q26 use E6 for the replica VM (8 vCPU / 16 GiB, no co-tenants) that carries the 10,000-hub evidence when the node cannot | One environment table; the product documents say "the replica VM" and cite R35/Q26 until it is settled (A5 §6 item 6 already asks `05-testing` to redefine E6) |
| 17 | `03-security/02`, register | `03-security` SoD-03 keeps a narrow exception that lets the system admin (`SAD`) co-sign a fleet-scope stop after the fact while on call (A-49), and A7 §4 item 11(b) asks the register to confirm it; the register's revised Q1 default says the co-signer is never the system admin | The product documents follow the register (never `SAD`); `03-security` withdraws the exception, or the register's Q1 answer restores it |

## 7. Alignment with the claims check (`06-reviews/05-claims-verification.md`)

| Claim | Findings | Verdict | Applied in |
|---|---|---|---|
| 1 ALR set point carries online AS | GRD-001 | Confirmed | Brief §6; vision §8.3; `FR-DISP-012`, `-025` |
| 2 Proxy AS offers | GRD-002 | Confirmed, broader | `FR-SAFE-029` (telemetered AS capability ≤ ledger-free capacity — telemetered capability, not offers, is the boundary, per the revised R17); `FR-ARB-013` (offers and an energy bid still cover telemetry); brief §3.1 |
| 3 NCLR behaviour | GRD-017 | Partly confirmed | `FR-CTR-026` (SCED awards ingested, 15-min baseline, disqualification for ≥ 6 months); brief §6; E09-S16 |
| 4 One LSE/DSP per ALR; premise data on request | GRD-016, -042 | Confirmed | `FR-CTR-019`; `FR-PRIV-013`; brief §3.3 |
| 5 PURA §39.918 and SB 231 | GRD-005 | Confirmed, tightened | `FR-CTR-024`; brief §3.1; vision §8.8 |
| 6 AS durations | GRD-056, Q7 | Confirmed; CRA "ECRS 2 h" stale | `FR-CTR-006`; brief §3.1, §3.2; vision §8.3; E09-S03 |
| 7 NPRR1002 scope | GRD-004 | Confirmed: registered ESRs only | `FR-SAFE-027` (operator policy); brief §9; vision §8.15; E15-S13 |
| 8 GVEC dual use | GRD-024 | Partly confirmed | Brief §3.3 (concurrent use after qualification is an assumption) |
| 9 Austin Energy toll | GRD-015 | Confirmed | Brief §3.1; `FR-CTR-020` |
| 10 SB 415 | GRD-048 | Confirmed; PURA §35.153; rule proposed | Brief §3.1; `FR-CTR-022`; E09-S14 |
| 11 ComEd PLC | GRD-049 | Partly confirmed | `FR-CTR-023` (self-serve; CSP-registered premises excluded because PJM demand-response load reductions are added back to the PLC; PLC method verified per customer) |
| 12 DNP3 stack | JDG-008 | Confirmed | Brief §3.4; `FR-SCAD-003` tag note |
| 14 UDSP every 4 s, base ramp from a deck | GRD-022, -013 | Partly confirmed | `FR-DISP-025` (configurable base ramp, labelled) |
| 15 ADER limits and NOIE consent | JDG-017 | Partly confirmed; limits superseded | `FR-PLAN-007` (500/100/100 MW, ≤ 90% per QSE, no ECRS headroom); brief §3.1, §3.3 |

## 8. Register revisions applied during this pass (v0.2 text revised at 10:15 and at 10:34)

Both revisions were re-read in full and checked against these documents before this file was closed.

| Register change | Applied in |
|---|---|
| R17: the ISO-boundary invariant bounds telemetered AS capability by ledger-free capacity for the product's duration — telemetered capability, not offers, is the boundary | `FR-SAFE-029`, `FR-ARB-013`; brief §3.1 |
| R17: NCLR performance against the 15-min meter interval; two failures in 365 days disqualify for ≥ 6 months; SCED still awards AS to NCLRs | `FR-CTR-026`; E09-S16; brief §6 |
| R19: NPRR1002 binds registered storage resources, not ADERs; R19 rests on grid behaviour | `FR-SAFE-027`; brief §9; vision §8.15 |
| R20: SB 231 (mobile, ≤ 5 MW, prior commission authorization); §39.918 does not apply to co-ops and municipal utilities | `FR-CTR-024`; brief §3.1; vision §8.8 |
| R27: verified facts (Austin toll terms, PURA §35.153, ADER limits, GVEC, ComEd) | Brief §3.1, §3.3; `FR-CTR-020`, `-022`, `-023`, `FR-PLAN-007` |
| Q1 default: co-signer or approver = shift supervisor for bank and zone; executive on call (or a second shift supervisor) for fleet; never the system admin (SoD-03) | `FR-SAFE` area note, `-006`, `-008`; E15-S03, S05; vision §6.11, §6.13, §8.12, §10 item 2 |
| Q7 answered by evidence; V-33 = ECRS 1 h, Non-Spin 4 h (2 h at NPRR1309), Regulation and RRS 30 min | `FR-CTR-006`; brief §3.1, §3.2; vision §8.3; E09-S03 |
| Q12 text (premise/device MW and SOC series, allocation factors) | `FR-PRIV-013` |
| R35 (10:34): the `demo` profile (2,000 hubs) fits at ≈ 92% of pod memory; the 10,000-hub profile needs the micro-benchmark savings, more VM memory (Q26) or the replica VM | Brief §4 (node facts, scale targets); `FR-SIM-013`, `NFR-210`; E19-S07, E18-S16; vision KPI-25, beat 7, §5.1, §10 item 6 |
| Q4 (10:34): decommission 2026-10-30 assumed | Brief §4; vision §5.2 |
| Q27 (10:34): demo and test windows avoid the host's ClamAV update times; the orchestrator never touches host services | Brief §4; vision §10 item 6; `03` open questions |
